package com.freiverse.library;

import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.support.v4.media.MediaBrowserCompat;
import android.support.v4.media.MediaDescriptionCompat;
import android.support.v4.media.MediaMetadataCompat;
import android.support.v4.media.session.MediaSessionCompat;
import android.support.v4.media.session.PlaybackStateCompat;
import android.util.Log;
import androidx.annotation.Nullable;
import androidx.media.MediaBrowserServiceCompat;
import java.lang.ref.WeakReference;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.json.JSONArray;
import org.json.JSONObject;

/** Shared playback + browse state between the Capacitor web layer and Android Auto. */
public final class LibraryAutoBridge {

    public static final String MEDIA_ROOT_ID = "library_root";
    public static final String CONTINUE_ID = "continue";
    public static final String LIBRARY_ID = "library";
    public static final String NOW_PLAYING_ID = "now_playing";

    /** Custom MediaSession actions — visible on Android Auto with ±15 icons. */
    public static final String CUSTOM_REWIND_15 = "seek_back_15";
    public static final String CUSTOM_FORWARD_15 = "seek_forward_15";
    /** Keep in sync with JS MEDIA_SKIP_SECONDS / in-app transport. */
    public static final long SKIP_MS = 15_000;

    private static final String TAG = "LibraryAutoBridge";
    private static final String PREFS = "library_auto_session";
    private static final String BROWSE_PREFS = "library_auto_browse";
    private static final String PLAYABLE_PREFS = "library_auto_playable";
    private static final long BROWSE_TIMEOUT_MS = 8_000;

    public interface ActionListener {
        void onAction(String action, @Nullable Bundle extras);
    }

    public interface BrowseRequestEmitter {
        void emitBrowseRequest(String parentId, String requestId);
    }

    private static final class PendingBrowse {

        final String parentId;
        /** Null when this is a background refresh after serving cache. */
        @Nullable
        final MediaBrowserServiceCompat.Result<List<MediaBrowserCompat.MediaItem>> result;
        final Runnable timeout;

        PendingBrowse(
            String parentId,
            @Nullable MediaBrowserServiceCompat.Result<List<MediaBrowserCompat.MediaItem>> result,
            Runnable timeout
        ) {
            this.parentId = parentId;
            this.result = result;
            this.timeout = timeout;
        }
    }

    private static final LibraryAutoBridge INSTANCE = new LibraryAutoBridge();

    public static LibraryAutoBridge getInstance() {
        return INSTANCE;
    }

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final List<ActionListener> listeners = new ArrayList<>();
    private final Map<String, PendingBrowse> pendingBrowses = new HashMap<>();

    private WeakReference<LibraryMediaBrowserService> serviceRef = new WeakReference<>(null);
    private WeakReference<MediaSessionCompat> sessionRef = new WeakReference<>(null);
    private WeakReference<BrowseRequestEmitter> emitterRef = new WeakReference<>(null);
    private WeakReference<Context> appContextRef = new WeakReference<>(null);

    private String title = "";
    private String artist = "";
    private String album = "";
    private Bitmap artwork;
    private boolean active = false;
    private boolean playing = false;
    private long durationMs = 0;
    private long positionMs = 0;
    private float playbackSpeed = 1.0f;
    private long lastNotificationUpdateMs = 0;
    private long lastPersistSessionMs = 0;
    private static final long PERSIST_SESSION_MIN_INTERVAL_MS = 15_000;

    private AudioManager audioManager;
    private AudioFocusRequest focusRequest;
    private boolean hasAudioFocus = false;
    /** True when we were playing before a transient focus loss (call / nav prompt). */
    private boolean resumeAfterFocusGain = false;
    /**
     * Ignore stale playing=false syncs from the WebView for a short window after
     * an optimistic AA/lock-screen play. Otherwise the first position tick still
     * reports paused and flips the session back (~0.5s play-then-pause).
     */
    private long ignorePausedSyncUntilElapsed = 0;
    private static final long PAUSED_SYNC_GRACE_MS = 2_500;
    /**
     * After AA/lock play we request native audio focus, then the WebView HTML5
     * {@code <audio>} element requests its own focus. That steals focus from us
     * and used to dispatch pause (~0.5s of audio then stop). Ignore focus loss
     * briefly so WebView can take over without killing playback.
     */
    private long ignoreFocusLossUntilElapsed = 0;
    private static final long FOCUS_LOSS_GRACE_MS = 8_000;
    /** Longer grace after multi-minute idle — WebView thaw can take seconds. */
    private static final long FOCUS_LOSS_GRACE_LONG_IDLE_MS = 16_000;
    /** ElapsedRealtime when we last entered paused (for deep-idle wake sizing). */
    private long pausedAtElapsed = 0;
    /**
     * Latched when AA/lock play starts after a long pause — survives clearing
     * {@link #pausedAtElapsed} during optimistic play so the soft-wake path
     * still uses the deep-idle retry schedule.
     */
    private boolean deepIdlePlayLatched = false;
    /**
     * When true, ExoPlayer owns PCM and the WebView must not start HTML5 audio
     * for the same session (attach UI only). Cleared on handoff / stop.
     */
    private boolean nativeOwnsPlayback = false;
    private String nativeMediaId = "";

    private LibraryAutoBridge() {}

    public void attach(LibraryMediaBrowserService service, MediaSessionCompat session) {
        serviceRef = new WeakReference<>(service);
        sessionRef = new WeakReference<>(session);
        ensureAppContext(service.getApplicationContext());
        audioManager = (AudioManager) service.getApplicationContext()
            .getSystemService(Context.AUDIO_SERVICE);
        restorePersistedSession(service.getApplicationContext());
        LibraryNativePlayer.getInstance().addListener(nativeListener);
        refreshSession(true);
    }

    private final LibraryNativePlayer.Listener nativeListener =
        new LibraryNativePlayer.Listener() {
            @Override
            public void onNativePlaying(
                String mediaId,
                String title,
                String artist,
                String album,
                String coverUrl,
                boolean playing,
                long durationMs,
                long positionMs,
                float speed,
                int trackIndex
            ) {
                nativeOwnsPlayback = true;
                nativeMediaId = mediaId != null ? mediaId : "";
                // Keep a short settle window so OEM focus blips don't pause Exo.
                ignoreFocusLossUntilElapsed =
                    SystemClock.elapsedRealtime() + FOCUS_LOSS_GRACE_MS;
                update(
                    title,
                    artist,
                    album,
                    null,
                    true,
                    playing,
                    durationMs,
                    positionMs,
                    speed
                );
                Context ctx = appContextRef.get();
                if (ctx != null) {
                    LibraryMediaBrowserService svc = serviceRef.get();
                    if (svc != null) {
                        svc.promoteToForeground();
                    }
                }
            }

            @Override
            public void onNativeStopped() {
                nativeOwnsPlayback = false;
                nativeMediaId = "";
            }

            @Override
            public void onNativeError(String message) {
                Log.w(TAG, "Native playback error: " + message);
                // Fall back to WebView sticky path if still latched.
                nativeOwnsPlayback = false;
            }
        };

    public boolean isNativeOwningPlayback() {
        return nativeOwnsPlayback && LibraryNativePlayer.getInstance().isOwning();
    }

    @Nullable
    public String getNativeMediaId() {
        return nativeMediaId.isEmpty() ? null : nativeMediaId;
    }

    /** Persist a playable queue so locked AA can start ExoPlayer without JS. */
    public void putPlayableCache(String mediaId, JSONObject playableJson) {
        if (mediaId == null || mediaId.isEmpty() || playableJson == null) {
            return;
        }
        Context ctx = appContextRef.get();
        if (ctx == null) {
            return;
        }
        try {
            playableJson.put("mediaId", mediaId);
            ctx
                .getSharedPreferences(PLAYABLE_PREFS, Context.MODE_PRIVATE)
                .edit()
                .putString(mediaId, playableJson.toString())
                .apply();
            // Cap entries — keep Continue Listening warm without unbounded growth.
            SharedPreferences prefs =
                ctx.getSharedPreferences(PLAYABLE_PREFS, Context.MODE_PRIVATE);
            if (prefs.getAll().size() > 40) {
                // Drop oldest-ish by rewriting only recent keys we touch; simple trim:
                // leave as-is unless severely over — SharedPreferences is small JSON.
                Log.i(TAG, "Playable cache size=" + prefs.getAll().size());
            }
        } catch (Exception e) {
            Log.w(TAG, "putPlayableCache failed", e);
        }
    }

    @Nullable
    public LibraryNativePlayer.Playable getPlayableCache(String mediaId) {
        if (mediaId == null || mediaId.isEmpty()) {
            return null;
        }
        Context ctx = appContextRef.get();
        if (ctx == null) {
            return null;
        }
        String raw =
            ctx
                .getSharedPreferences(PLAYABLE_PREFS, Context.MODE_PRIVATE)
                .getString(mediaId, null);
        if (raw == null || raw.isEmpty()) {
            return null;
        }
        try {
            return LibraryNativePlayer.parsePlayable(new JSONObject(raw));
        } catch (Exception e) {
            Log.w(TAG, "getPlayableCache parse failed", e);
            return null;
        }
    }

    /**
     * Start ExoPlayer from a cached playable. Returns true if native took ownership
     * (caller should skip / cancel WebView sticky wake storms).
     */
    public boolean tryNativePlayFromMediaId(String mediaId) {
        if (mediaId == null || mediaId.isEmpty() || "now_playing".equals(mediaId)) {
            // now_playing: resume native if already owning, else false → JS play().
            if ("now_playing".equals(mediaId) && isNativeOwningPlayback()) {
                LibraryNativePlayer.getInstance().resume();
                return true;
            }
            if ("now_playing".equals(mediaId)) {
                // Try last known media id from session title — not enough; fall through.
                return false;
            }
        }
        LibraryNativePlayer.Playable playable = getPlayableCache(mediaId);
        if (playable == null) {
            Log.i(TAG, "No native playable cache for " + mediaId);
            return false;
        }
        Context ctx = appContextRef.get();
        if (ctx == null) {
            return false;
        }
        // Optimistic session metadata before first Exo tick.
        update(
            playable.title,
            playable.author,
            playable.author,
            null,
            true,
            true,
            playable.totalDurationMs,
            playable.positionMs + trackOffset(playable),
            1.0f
        );
        nativeOwnsPlayback = true;
        nativeMediaId = mediaId;
        LibraryNativePlayer.getInstance().play(ctx, playable);
        return true;
    }

    private static long trackOffset(LibraryNativePlayer.Playable playable) {
        int idx = Math.min(playable.trackIndex, Math.max(0, playable.tracks.size() - 1));
        if (idx >= 0 && idx < playable.tracks.size()) {
            return playable.tracks.get(idx).startOffsetMs;
        }
        return 0;
    }

    /** Resume native player if it owns the session. */
    public boolean tryNativeResume() {
        if (!isNativeOwningPlayback()) {
            return false;
        }
        LibraryNativePlayer.getInstance().resume();
        return true;
    }

    public boolean tryNativePause() {
        if (!isNativeOwningPlayback()) {
            return false;
        }
        LibraryNativePlayer.getInstance().pause();
        return true;
    }

    public boolean tryNativeSeekRelative(long deltaMs) {
        if (!isNativeOwningPlayback()) {
            return false;
        }
        LibraryNativePlayer.getInstance().seekRelative(deltaMs);
        return true;
    }

    public boolean tryNativeSeekTo(long positionMs) {
        if (!isNativeOwningPlayback()) {
            return false;
        }
        LibraryNativePlayer.getInstance().seekTo(positionMs);
        return true;
    }

    public boolean tryNativeSkipNext() {
        if (!isNativeOwningPlayback()) {
            return false;
        }
        LibraryNativePlayer.getInstance().skipToNextTrack();
        return true;
    }

    public boolean tryNativeSkipPrevious() {
        if (!isNativeOwningPlayback()) {
            return false;
        }
        LibraryNativePlayer.getInstance().skipToPreviousTrack();
        return true;
    }

    /** WebView is becoming the audio owner — stop ExoPlayer without wiping AA session. */
    public void handOffNativeToWebView() {
        nativeOwnsPlayback = false;
        LibraryNativePlayer.getInstance().handOffToWebView();
    }

    public void stopNativePlayback() {
        nativeOwnsPlayback = false;
        nativeMediaId = "";
        LibraryNativePlayer.getInstance().stopAndReleaseOwnership();
    }

    /** So JS can persist browse folders before MediaBrowserService has attached. */
    public void ensureAppContext(Context context) {
        if (context == null) {
            return;
        }
        Context app = context.getApplicationContext();
        appContextRef = new WeakReference<>(app);
        if (audioManager == null) {
            audioManager = (AudioManager) app.getSystemService(Context.AUDIO_SERVICE);
        }
    }

    public void setBrowseRequestEmitter(BrowseRequestEmitter emitter) {
        emitterRef = new WeakReference<>(emitter);
    }

    public void addActionListener(ActionListener listener) {
        if (!listeners.contains(listener)) {
            listeners.add(listener);
        }
    }

    public void removeActionListener(ActionListener listener) {
        listeners.remove(listener);
    }

    public void update(
        String title,
        String artist,
        String album,
        @Nullable Bitmap artwork,
        boolean active,
        boolean playing,
        long durationMs,
        long positionMs,
        float playbackSpeed
    ) {
        boolean wasActive = this.active;
        String previousRootKey = nowPlayingRootKey();

        this.title = title != null ? title : "";
        this.artist = artist != null ? artist : "";
        this.album = album != null ? album : "";
        if (artwork != null) {
            this.artwork = artwork;
        } else if (!active) {
            this.artwork = null;
        }
        this.active = active;
        this.playing = applyPlayingSync(playing);
        this.durationMs = Math.max(0, durationMs);
        this.positionMs = Math.max(0, positionMs);
        this.playbackSpeed = playbackSpeed > 0 ? playbackSpeed : 1.0f;

        boolean rootChanged =
            wasActive != active || !previousRootKey.equals(nowPlayingRootKey());
        refreshSession(true);
        persistSession();
        if (rootChanged) {
            notifyRootChanged();
        }
    }

    /** Position / transport-only sync — avoids rebuilding browse tree artwork. */
    public void updatePosition(boolean playing, long positionMs, float playbackSpeed) {
        this.playing = applyPlayingSync(playing);
        this.positionMs = Math.max(0, positionMs);
        this.playbackSpeed = playbackSpeed > 0 ? playbackSpeed : 1.0f;
        refreshSession(false);
        long now = System.currentTimeMillis();
        if (now - lastPersistSessionMs >= PERSIST_SESSION_MIN_INTERVAL_MS) {
            lastPersistSessionMs = now;
            persistSession();
        }
    }

    /** Drop stale paused syncs while an optimistic play is still settling. */
    private boolean applyPlayingSync(boolean playing) {
        if (playing) {
            ignorePausedSyncUntilElapsed = 0;
            pausedAtElapsed = 0;
            deepIdlePlayLatched = false;
            return true;
        }
        if (SystemClock.elapsedRealtime() < ignorePausedSyncUntilElapsed) {
            return this.playing;
        }
        if (this.playing) {
            pausedAtElapsed = SystemClock.elapsedRealtime();
        }
        return false;
    }

    private String nowPlayingRootKey() {
        if (!active) {
            return "";
        }
        return title + "|" + artist + "|" + (artwork != null ? "art" : "noart");
    }

    /**
     * Flip only the playing flag and push it to the session right away.
     * Used when an Android Auto transport control fires, before the WebView
     * has processed the action — otherwise the play/pause button appears stuck
     * until the JS round-trip completes (seconds when the app is backgrounded).
     */
    public void setPlayingOptimistic(boolean playing) {
        if (!active) {
            return;
        }
        this.playing = playing;
        if (playing) {
            // Sample before clearing pause timestamp — grace size depends on idle depth.
            boolean deep = isDeepIdlePause() || deepIdlePlayLatched;
            deepIdlePlayLatched = deep;
            long grace = deep ? FOCUS_LOSS_GRACE_LONG_IDLE_MS : FOCUS_LOSS_GRACE_MS;
            pausedAtElapsed = 0;
            long now = SystemClock.elapsedRealtime();
            ignorePausedSyncUntilElapsed = now + PAUSED_SYNC_GRACE_MS;
            ignoreFocusLossUntilElapsed = now + grace;
        } else {
            ignorePausedSyncUntilElapsed = 0;
            ignoreFocusLossUntilElapsed = 0;
            deepIdlePlayLatched = false;
            pausedAtElapsed = SystemClock.elapsedRealtime();
        }
        refreshSession(false);
    }

    /** Milliseconds since last pause; 0 if playing / never paused this session. */
    public long millisSincePause() {
        if (pausedAtElapsed <= 0) {
            return 0;
        }
        return Math.max(0, SystemClock.elapsedRealtime() - pausedAtElapsed);
    }

    /** True when paused long enough that the WebView is often frozen/Doze-throttled. */
    public boolean isDeepIdlePause() {
        return millisSincePause() >= 90_000;
    }

    /**
     * Whether the in-flight AA/lock play came from a multi-minute pause.
     * Remains true through optimistic play until playback is confirmed or cancelled.
     */
    public boolean isDeepIdlePlay() {
        return deepIdlePlayLatched || isDeepIdlePause();
    }

    public void clearDeepIdlePlayLatch() {
        deepIdlePlayLatched = false;
    }

    private long focusLossGraceMs() {
        return isDeepIdlePlay() ? FOCUS_LOSS_GRACE_LONG_IDLE_MS : FOCUS_LOSS_GRACE_MS;
    }

    public boolean isActive() {
        return active;
    }

    public boolean isPlaying() {
        return playing;
    }

    public String getTitle() {
        return title;
    }

    public String getArtist() {
        return artist;
    }

    @Nullable
    public Bitmap getArtwork() {
        return artwork;
    }

    public void clear() {
        stopNativePlayback();
        abandonAudioFocus();
        resumeAfterFocusGain = false;
        ignorePausedSyncUntilElapsed = 0;
        ignoreFocusLossUntilElapsed = 0;
        pausedAtElapsed = 0;
        deepIdlePlayLatched = false;
        update("", "", "", null, false, false, 0, 0, 1.0f);
        Context ctx = appContextRef.get();
        if (ctx != null) {
            ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().clear().apply();
        }
    }

    public void dispatch(String action, @Nullable Bundle extras) {
        for (ActionListener listener : new ArrayList<>(listeners)) {
            listener.onAction(action, extras);
        }
    }

    /**
     * ±15s from AA / lock screen. Updates session position immediately so the
     * scrubber moves before the WebView seek round-trip confirms.
     */
    public void seekRelativeAndDispatch(long deltaMs, String action) {
        if (Looper.myLooper() != Looper.getMainLooper()) {
            mainHandler.post(() -> seekRelativeAndDispatch(deltaMs, action));
            return;
        }
        long max = durationMs > 0 ? durationMs : Long.MAX_VALUE;
        positionMs = Math.max(0, Math.min(max, positionMs + deltaMs));
        refreshSession(false);
        dispatch(action, null);
    }

    /** Request audio focus before resuming; returns false if focus was denied. */
    public boolean requestAudioFocusForPlay() {
        // Latch deep-idle before optimistic play clears the pause timestamp.
        if (isDeepIdlePause()) {
            deepIdlePlayLatched = true;
        }
        // Always arm the grace window — even if we already hold focus — because
        // the upcoming WebView audio.play() may still steal it from us.
        ignoreFocusLossUntilElapsed =
            SystemClock.elapsedRealtime() + focusLossGraceMs();

        if (audioManager == null) {
            Context ctx = appContextRef.get();
            if (ctx != null) {
                audioManager = (AudioManager) ctx.getSystemService(Context.AUDIO_SERVICE);
            }
        }
        if (audioManager == null) {
            return true;
        }
        if (hasAudioFocus) {
            return true;
        }

        int result;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            if (focusRequest == null) {
                // MUSIC (not SPEECH): matches HTML5 media focus usage and reduces
                // odd OEM focus hand-offs when the WebView starts the same stream.
                AudioAttributes attrs = new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build();
                focusRequest = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                    .setAudioAttributes(attrs)
                    .setOnAudioFocusChangeListener(this::onAudioFocusChange, mainHandler)
                    .setAcceptsDelayedFocusGain(true)
                    .setWillPauseWhenDucked(false)
                    .build();
            }
            result = audioManager.requestAudioFocus(focusRequest);
        } else {
            result = audioManager.requestAudioFocus(
                this::onAudioFocusChange,
                AudioManager.STREAM_MUSIC,
                AudioManager.AUDIOFOCUS_GAIN
            );
        }
        hasAudioFocus =
            result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED
                || result == AudioManager.AUDIOFOCUS_REQUEST_DELAYED;
        return hasAudioFocus || result == AudioManager.AUDIOFOCUS_REQUEST_DELAYED;
    }

    public void abandonAudioFocus() {
        if (audioManager == null || !hasAudioFocus) {
            hasAudioFocus = false;
            return;
        }
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && focusRequest != null) {
                audioManager.abandonAudioFocusRequest(focusRequest);
            } else {
                audioManager.abandonAudioFocus(this::onAudioFocusChange);
            }
        } catch (Exception e) {
            Log.w(TAG, "abandonAudioFocus failed", e);
        }
        hasAudioFocus = false;
    }

    private void onAudioFocusChange(int focusChange) {
        switch (focusChange) {
            case AudioManager.AUDIOFOCUS_LOSS:
            case AudioManager.AUDIOFOCUS_LOSS_TRANSIENT: {
                // Native ExoPlayer is playing under OUR focus request — only pause
                // for real external loss (call / other app), not settle races.
                if (isNativeOwningPlayback()) {
                    if (SystemClock.elapsedRealtime() < ignoreFocusLossUntilElapsed) {
                        Log.i(TAG, "Ignoring focus loss during native play settle");
                        return;
                    }
                    if (focusChange == AudioManager.AUDIOFOCUS_LOSS) {
                        resumeAfterFocusGain = false;
                        tryNativePause();
                        setPlayingOptimistic(false);
                        abandonAudioFocus();
                    } else {
                        resumeAfterFocusGain = playing || resumeAfterFocusGain;
                        tryNativePause();
                        setPlayingOptimistic(false);
                    }
                    break;
                }
                // WebView HTML5 audio taking focus after our MediaSession play —
                // do not pause or the user hears ~0.5s then silence.
                if (SystemClock.elapsedRealtime() < ignoreFocusLossUntilElapsed) {
                    Log.i(TAG, "Ignoring focus loss during play settle (WebView audio)");
                    hasAudioFocus = false;
                    return;
                }
                if (focusChange == AudioManager.AUDIOFOCUS_LOSS) {
                    resumeAfterFocusGain = false;
                    setPlayingOptimistic(false);
                    dispatch("pause", null);
                    abandonAudioFocus();
                } else {
                    // Phone call / nav prompt — remember to resume when focus returns.
                    resumeAfterFocusGain = playing || resumeAfterFocusGain;
                    setPlayingOptimistic(false);
                    dispatch("pause", null);
                }
                break;
            }
            case AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK:
                // Another app wants a brief duck (nav chime, etc.). Keep playing —
                // pausing here races lock-screen / AA play and causes play-then-pause.
                break;
            case AudioManager.AUDIOFOCUS_GAIN:
                hasAudioFocus = true;
                if (resumeAfterFocusGain && active) {
                    resumeAfterFocusGain = false;
                    setPlayingOptimistic(true);
                    if (!tryNativeResume()) {
                        dispatch("play", null);
                    }
                }
                break;
            default:
                break;
        }
    }

    public boolean isStaticParent(String parentId) {
        return MEDIA_ROOT_ID.equals(parentId);
    }

    public List<MediaBrowserCompat.MediaItem> buildRootChildren() {
        List<MediaBrowserCompat.MediaItem> items = new ArrayList<>();
        items.add(browsable(CONTINUE_ID, "Continue Listening", "In progress"));
        items.add(browsable(LIBRARY_ID, "Library", "All audiobooks A–Z"));

        if (active) {
            MediaDescriptionCompat description = new MediaDescriptionCompat.Builder()
                .setMediaId(NOW_PLAYING_ID)
                .setTitle(title.isEmpty() ? "Now Playing" : title)
                .setSubtitle(artist)
                .setDescription(album)
                .setIconBitmap(artwork)
                .build();
            items.add(
                new MediaBrowserCompat.MediaItem(
                    description,
                    MediaBrowserCompat.MediaItem.FLAG_PLAYABLE
                )
            );
        }
        return items;
    }

    /**
     * Persist a browse folder so Android Auto can show Continue / Library while
     * the phone is locked and the WebView cannot hit the network.
     *
     * @param allowEmpty when false, refuse to overwrite a non-empty cache with
     *     an empty list — locked-phone / data-restricted API failures often
     *     look like "no titles" and used to wipe Continue + Library in AA.
     */
    public void putBrowseCache(String parentId, List<AutoBrowseNode> nodes) {
        putBrowseCache(parentId, nodes, false);
    }

    public void putBrowseCache(
        String parentId,
        List<AutoBrowseNode> nodes,
        boolean allowEmpty
    ) {
        if (parentId == null || parentId.isEmpty()) {
            return;
        }
        Context ctx = appContextRef.get();
        if (ctx == null) {
            return;
        }
        try {
            JSONArray arr = new JSONArray();
            if (nodes != null) {
                for (AutoBrowseNode node : nodes) {
                    if (node == null || node.mediaId.isEmpty()) {
                        continue;
                    }
                    JSONObject o = new JSONObject();
                    o.put("mediaId", node.mediaId);
                    o.put("title", node.title);
                    o.put("subtitle", node.subtitle != null ? node.subtitle : "");
                    o.put("browsable", node.browsable);
                    if (node.iconUri != null && !node.iconUri.isEmpty()) {
                        o.put("iconUri", node.iconUri);
                    }
                    arr.put(o);
                }
            }
            if (arr.length() == 0 && !allowEmpty) {
                List<AutoBrowseNode> existing = getBrowseCache(parentId);
                if (!existing.isEmpty()) {
                    Log.i(
                        TAG,
                        "Keeping cached AA browse for "
                            + parentId
                            + " (refusing empty write)"
                    );
                    return;
                }
            }
            SharedPreferences prefs =
                ctx.getSharedPreferences(BROWSE_PREFS, Context.MODE_PRIVATE);
            String prev = prefs.getString("node:" + parentId, null);
            String next = arr.toString();
            // Skip no-op writes — notifyChildrenChanged during playback can
            // kick MediaBrowser clients into reloading every letter folder.
            if (next.equals(prev)) {
                return;
            }
            prefs
                .edit()
                .putString("node:" + parentId, next)
                .putLong("nodeAt:" + parentId, System.currentTimeMillis())
                .apply();
            Log.i(TAG, "Cached AA browse parent=" + parentId + " count=" + arr.length());
            // Late JS/API reply after a locked-phone empty browse — refresh AA.
            notifyParentChanged(parentId);
        } catch (Exception e) {
            Log.w(TAG, "putBrowseCache failed for " + parentId, e);
        }
    }

    public List<AutoBrowseNode> getBrowseCache(String parentId) {
        List<AutoBrowseNode> nodes = new ArrayList<>();
        if (parentId == null || parentId.isEmpty()) {
            return nodes;
        }
        Context ctx = appContextRef.get();
        if (ctx == null) {
            return nodes;
        }
        String raw = ctx
            .getSharedPreferences(BROWSE_PREFS, Context.MODE_PRIVATE)
            .getString("node:" + parentId, null);
        if (raw == null || raw.isEmpty()) {
            return nodes;
        }
        try {
            JSONArray arr = new JSONArray(raw);
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.getJSONObject(i);
                nodes.add(
                    new AutoBrowseNode(
                        o.optString("mediaId", ""),
                        o.optString("title", ""),
                        o.optString("subtitle", ""),
                        o.optBoolean("browsable", false),
                        o.optString("iconUri", null),
                        null
                    )
                );
            }
        } catch (Exception e) {
            Log.w(TAG, "getBrowseCache failed for " + parentId, e);
        }
        return nodes;
    }

    public void requestBrowseChildren(
        String parentId,
        MediaBrowserServiceCompat.Result<List<MediaBrowserCompat.MediaItem>> result
    ) {
        List<AutoBrowseNode> cached = getBrowseCache(parentId);
        BrowseRequestEmitter emitter = emitterRef.get();

        // Locked phone / cold AA: serve durable cache immediately so Continue +
        // Library are never empty when we previously synced them from JS.
        if (!cached.isEmpty()) {
            result.sendResult(toMediaItems(cached));
            if (emitter != null) {
                startBrowseRefresh(parentId, emitter);
            }
            return;
        }

        // No cache yet — wait for WebView (may be frozen; soft-wake happens in emitter).
        result.detach();
        if (emitter == null) {
            Log.w(TAG, "AA browse with no cache and no JS emitter: " + parentId);
            result.sendResult(new ArrayList<>());
            return;
        }

        final String requestId = UUID.randomUUID().toString();
        Runnable timeout = () -> {
            PendingBrowse pending = pendingBrowses.remove(requestId);
            if (pending == null || pending.result == null) {
                return;
            }
            List<AutoBrowseNode> lateCache = getBrowseCache(parentId);
            Log.w(
                TAG,
                "AA browse timeout parent="
                    + parentId
                    + " cached="
                    + lateCache.size()
            );
            pending.result.sendResult(toMediaItems(lateCache));
        };
        mainHandler.postDelayed(timeout, BROWSE_TIMEOUT_MS);
        pendingBrowses.put(requestId, new PendingBrowse(parentId, result, timeout));
        emitter.emitBrowseRequest(parentId, requestId);
    }

    /** Background refresh after serving cache; updates prefs + notifies AA. */
    private void startBrowseRefresh(String parentId, BrowseRequestEmitter emitter) {
        final String requestId = UUID.randomUUID().toString();
        Runnable timeout = () -> pendingBrowses.remove(requestId);
        mainHandler.postDelayed(timeout, BROWSE_TIMEOUT_MS);
        pendingBrowses.put(requestId, new PendingBrowse(parentId, null, timeout));
        emitter.emitBrowseRequest(parentId, requestId);
    }

    public void resolveBrowseChildren(String requestId, List<AutoBrowseNode> nodes) {
        PendingBrowse pending = pendingBrowses.remove(requestId);
        if (pending != null) {
            mainHandler.removeCallbacks(pending.timeout);
        }

        String parentId = pending != null ? pending.parentId : null;
        if (parentId != null && !parentId.isEmpty()) {
            // Never wipe a good locked-phone cache with a failed/empty JS reply.
            if (nodes != null && !nodes.isEmpty()) {
                putBrowseCache(parentId, nodes);
            } else {
                List<AutoBrowseNode> existing = getBrowseCache(parentId);
                if (existing.isEmpty()) {
                    putBrowseCache(parentId, nodes != null ? nodes : new ArrayList<>());
                } else {
                    Log.i(TAG, "Keeping cached AA browse for " + parentId + " (live empty)");
                }
            }
        }

        if (pending == null) {
            return;
        }
        if (pending.result != null) {
            List<AutoBrowseNode> toSend = nodes;
            if ((toSend == null || toSend.isEmpty()) && parentId != null) {
                List<AutoBrowseNode> cached = getBrowseCache(parentId);
                if (!cached.isEmpty()) {
                    toSend = cached;
                }
            }
            pending.result.sendResult(toMediaItems(toSend));
            return;
        }
        // Cache-first path: tell Android Auto the folder changed so it can reload.
        if (parentId != null && !parentId.isEmpty() && nodes != null && !nodes.isEmpty()) {
            notifyParentChanged(parentId);
        }
    }

    private void notifyParentChanged(String parentId) {
        LibraryMediaBrowserService service = serviceRef.get();
        if (service != null) {
            service.notifyChildrenChanged(parentId);
        }
    }

    private List<MediaBrowserCompat.MediaItem> toMediaItems(List<AutoBrowseNode> nodes) {
        List<MediaBrowserCompat.MediaItem> items = new ArrayList<>();
        if (nodes == null) {
            return items;
        }
        for (AutoBrowseNode node : nodes) {
            MediaDescriptionCompat.Builder builder = new MediaDescriptionCompat.Builder()
                .setMediaId(node.mediaId)
                .setTitle(node.title)
                .setSubtitle(node.subtitle);
            if (node.iconBitmap != null) {
                builder.setIconBitmap(node.iconBitmap);
            } else if (node.iconUri != null && !node.iconUri.isEmpty()) {
                builder.setIconUri(Uri.parse(node.iconUri));
            }
            MediaDescriptionCompat description = builder.build();
            int flags = node.browsable
                ? MediaBrowserCompat.MediaItem.FLAG_BROWSABLE
                : MediaBrowserCompat.MediaItem.FLAG_PLAYABLE;
            items.add(new MediaBrowserCompat.MediaItem(description, flags));
        }
        return items;
    }

    private MediaBrowserCompat.MediaItem browsable(String id, String title, String subtitle) {
        MediaDescriptionCompat description = new MediaDescriptionCompat.Builder()
            .setMediaId(id)
            .setTitle(title)
            .setSubtitle(subtitle)
            .build();
        return new MediaBrowserCompat.MediaItem(
            description,
            MediaBrowserCompat.MediaItem.FLAG_BROWSABLE
        );
    }

    private void notifyRootChanged() {
        LibraryMediaBrowserService service = serviceRef.get();
        if (service != null) {
            service.notifyRootChildrenChanged();
        }
    }

    private void persistSession() {
        Context ctx = appContextRef.get();
        if (ctx == null) {
            return;
        }
        lastPersistSessionMs = System.currentTimeMillis();
        SharedPreferences.Editor ed = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit();
        if (!active) {
            ed.clear().apply();
            return;
        }
        ed.putBoolean("active", true)
            .putBoolean("playing", playing)
            .putString("title", title)
            .putString("artist", artist)
            .putString("album", album)
            .putLong("durationMs", durationMs)
            .putLong("positionMs", positionMs)
            .putFloat("playbackSpeed", playbackSpeed)
            .apply();
    }

    private void restorePersistedSession(Context ctx) {
        SharedPreferences prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        if (!prefs.getBoolean("active", false)) {
            return;
        }
        this.active = true;
        this.playing = false; // never auto-start after process death
        this.title = prefs.getString("title", "");
        this.artist = prefs.getString("artist", "");
        this.album = prefs.getString("album", "");
        this.durationMs = prefs.getLong("durationMs", 0);
        this.positionMs = prefs.getLong("positionMs", 0);
        this.playbackSpeed = prefs.getFloat("playbackSpeed", 1.0f);
        Log.i(TAG, "Restored AA session metadata: " + title);
    }

    private void refreshSession(boolean metadataMayHaveChanged) {
        // MediaSessionCompat updates must happen on the main thread; Capacitor
        // plugin methods (syncPlayback) arrive on a bridge worker thread.
        if (Looper.myLooper() != Looper.getMainLooper()) {
            mainHandler.post(() -> refreshSession(metadataMayHaveChanged));
            return;
        }

        MediaSessionCompat session = sessionRef.get();
        if (session == null) {
            return;
        }

        long actions =
            PlaybackStateCompat.ACTION_PLAY
                | PlaybackStateCompat.ACTION_PAUSE
                | PlaybackStateCompat.ACTION_PLAY_PAUSE
                | PlaybackStateCompat.ACTION_SEEK_TO
                | PlaybackStateCompat.ACTION_REWIND
                | PlaybackStateCompat.ACTION_FAST_FORWARD
                | PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS
                | PlaybackStateCompat.ACTION_SKIP_TO_NEXT
                | PlaybackStateCompat.ACTION_PLAY_FROM_MEDIA_ID
                | PlaybackStateCompat.ACTION_STOP;

        int state = playing
            ? PlaybackStateCompat.STATE_PLAYING
            : (active ? PlaybackStateCompat.STATE_PAUSED : PlaybackStateCompat.STATE_NONE);

        // Custom ±15 actions: AA often reserves side slots for chapter skip when
        // SKIP_TO_PREVIOUS/NEXT are set; custom actions keep seek buttons visible.
        PlaybackStateCompat playbackState = new PlaybackStateCompat.Builder()
            .setActions(actions)
            // Explicit updateTime so AA keeps extrapolating position across
            // chapter metadata swaps (without it, some head units freeze the timer).
            .setState(state, positionMs, playbackSpeed, SystemClock.elapsedRealtime())
            .addCustomAction(
                new PlaybackStateCompat.CustomAction.Builder(
                    CUSTOM_REWIND_15,
                    "-15 seconds",
                    R.drawable.ic_media_rewind_15
                ).build()
            )
            .addCustomAction(
                new PlaybackStateCompat.CustomAction.Builder(
                    CUSTOM_FORWARD_15,
                    "+15 seconds",
                    R.drawable.ic_media_forward_15
                ).build()
            )
            .build();
        session.setPlaybackState(playbackState);

        if (!active) {
            session.setMetadata(null);
            LibraryMediaBrowserService service = serviceRef.get();
            if (service != null) {
                service.stopForegroundPlayback();
            }
            return;
        }

        if (metadataMayHaveChanged) {
            MediaMetadataCompat.Builder metaBuilder = new MediaMetadataCompat.Builder()
                .putString(MediaMetadataCompat.METADATA_KEY_TITLE, title)
                .putString(MediaMetadataCompat.METADATA_KEY_ARTIST, artist)
                .putString(MediaMetadataCompat.METADATA_KEY_ALBUM, album)
                .putLong(MediaMetadataCompat.METADATA_KEY_DURATION, durationMs);
            // Never put a null bitmap — that clears cover art on many AA units.
            // One bitmap key is enough; duplicating ALBUM_ART + DISPLAY_ICON
            // copies the same pixels into the system binder payload twice.
            if (artwork != null && !artwork.isRecycled()) {
                metaBuilder.putBitmap(MediaMetadataCompat.METADATA_KEY_ALBUM_ART, artwork);
            }
            session.setMetadata(metaBuilder.build());
        }
        session.setActive(true);

        LibraryMediaBrowserService service = serviceRef.get();
        if (service != null) {
            if (active) {
                service.promoteToForeground();
                long now = System.currentTimeMillis();
                if (metadataMayHaveChanged || now - lastNotificationUpdateMs >= 5_000) {
                    lastNotificationUpdateMs = now;
                    service.updateForegroundNotification();
                }
            } else {
                service.stopForegroundPlayback();
            }
        }
    }

    /** PendingIntent used for notification + session activity. */
    static PendingIntent sessionActivityIntent(Context context) {
        Intent launchIntent = new Intent(context, MainActivity.class);
        launchIntent.addFlags(
            Intent.FLAG_ACTIVITY_NEW_TASK
                | Intent.FLAG_ACTIVITY_SINGLE_TOP
                | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
                | Intent.FLAG_ACTIVITY_CLEAR_TOP
        );
        // Hint for resume-from-media-session (unlock / AA play).
        launchIntent.putExtra("library_media_resume", true);
        return PendingIntent.getActivity(
            context,
            0,
            launchIntent,
            PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT
        );
    }
}
