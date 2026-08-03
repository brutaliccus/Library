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
    /** True while AA selected a title and native/WebView audio is still starting. */
    private boolean buffering = false;
    private long durationMs = 0;
    /** Display / MediaSession scrubber position (may be chapter-scoped from WebView). */
    private long positionMs = 0;
    /**
     * Book-global position for cold AA resume. Never overwrite this with
     * chapter-scoped MediaSession position from WebView sync.
     */
    private long bookGlobalPositionMs = 0;
    /** Wall-clock ms when {@link #bookGlobalPositionMs} was last trusted. */
    private long bookGlobalUpdatedAtMs = 0;
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
    /** Last metadata signature from ExoPlayer — avoid setMetadata storms. */
    private String lastNativeMetaKey = "";
    private Boolean lastNativePlaying = null;
    /** Skip redundant MediaMetadata pushes when title/art/duration unchanged. */
    private String lastSessionMetaSig = "";

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
                long displayDurationMs,
                long displayPositionMs,
                long bookGlobalMs,
                float speed,
                int trackIndex
            ) {
                nativeOwnsPlayback = true;
                nativeMediaId = mediaId != null ? mediaId : "";
                // Keep a short settle window so OEM focus blips don't pause Exo.
                ignoreFocusLossUntilElapsed =
                    SystemClock.elapsedRealtime() + FOCUS_LOSS_GRACE_MS;

                // CRITICAL: ExoPlayer ticks ~1 Hz. Calling update()→setMetadata
                // (with album-art bitmap) every second OOMs / freezes phones after
                // ~20–30s via binder + NotificationManager storms. Position-only
                // updates are cheap; full metadata only when title/track changes.
                String metaKey =
                    (mediaId != null ? mediaId : "")
                        + "|"
                        + (title != null ? title : "")
                        + "|"
                        + (artist != null ? artist : "")
                        + "|"
                        + (album != null ? album : "")
                        + "|"
                        + trackIndex
                        + "|"
                        + displayDurationMs;
                boolean metaChanged = !metaKey.equals(lastNativeMetaKey);
                boolean playChanged =
                    lastNativePlaying == null || lastNativePlaying != playing;
                lastNativePlaying = playing;

                // Resume / cold-start use book-global; AA scrubber uses display scope.
                bookGlobalPositionMs = Math.max(0, bookGlobalMs);
                if (metaChanged) {
                    lastNativeMetaKey = metaKey;
                    Log.i(
                        TAG,
                        "Native metadata applied mediaId="
                            + mediaId
                            + " track="
                            + trackIndex
                            + " displayDurMs="
                            + displayDurationMs
                    );
                    update(
                        title,
                        artist,
                        album,
                        null,
                        true,
                        playing,
                        displayDurationMs,
                        displayPositionMs,
                        speed
                    );
                    LibraryMediaBrowserService svc = serviceRef.get();
                    if (svc != null) {
                        svc.promoteToForeground();
                    }
                } else if (playChanged) {
                    updatePosition(playing, displayPositionMs, speed);
                } else {
                    updatePosition(playing, displayPositionMs, speed);
                }
            }

            @Override
            public void onNativeStopped() {
                nativeOwnsPlayback = false;
                nativeMediaId = "";
                lastNativeMetaKey = "";
                lastNativePlaying = null;
                buffering = false;
            }

            @Override
            public void onNativeError(String message) {
                Log.w(TAG, "Native playback error: " + message);
                // Fall back to WebView sticky path if still latched.
                nativeOwnsPlayback = false;
                lastNativeMetaKey = "";
                lastNativePlaying = null;
                buffering = false;
                refreshSession(true);
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
            if (!playableJson.has("positionUpdatedAt")) {
                playableJson.put("positionUpdatedAt", System.currentTimeMillis());
            }
            SharedPreferences prefs =
                ctx.getSharedPreferences(PLAYABLE_PREFS, Context.MODE_PRIVATE);
            // Never let an older warm-cache / chapter-reload stamp overwrite a
            // newer phone listening position (car-entry progress revert).
            String existingRaw = prefs.getString(mediaId, null);
            if (existingRaw != null && !existingRaw.isEmpty()) {
                try {
                    JSONObject existing = new JSONObject(existingRaw);
                    long incomingTs = playableJson.optLong("positionUpdatedAt", 0);
                    long existingTs = existing.optLong("positionUpdatedAt", 0);
                    LibraryNativePlayer.Playable incoming =
                        LibraryNativePlayer.parsePlayable(playableJson);
                    LibraryNativePlayer.Playable prior =
                        LibraryNativePlayer.parsePlayable(existing);
                    if (incoming != null && prior != null) {
                        long incomingGlobal =
                            Math.max(0, incoming.positionMs + trackOffset(incoming));
                        long priorGlobal =
                            Math.max(0, prior.positionMs + trackOffset(prior));
                        boolean incomingOlder =
                            existingTs > 0
                                && incomingTs > 0
                                && incomingTs + 2_000 < existingTs;
                        boolean sameEraMissingTs =
                            (existingTs <= 0 || incomingTs <= 0)
                                && priorGlobal > incomingGlobal + 5_000;
                        if (incomingOlder || sameEraMissingTs) {
                            // Keep fresher position / track index; adopt new tracks/chapters.
                            playableJson.put("position", prior.positionMs / 1000.0);
                            playableJson.put("trackIndex", prior.trackIndex);
                            playableJson.put(
                                "positionUpdatedAt",
                                Math.max(existingTs, incomingTs)
                            );
                            Log.i(
                                TAG,
                                "putPlayableCache kept fresher position for "
                                    + mediaId
                                    + " priorGlobalMs="
                                    + priorGlobal
                                    + " incomingGlobalMs="
                                    + incomingGlobal
                            );
                        }
                    }
                } catch (Exception mergeEx) {
                    Log.w(TAG, "putPlayableCache merge skipped", mergeEx);
                }
            }
            prefs.edit().putString(mediaId, playableJson.toString()).apply();
            // Cap entries — keep Continue Listening warm without unbounded growth.
            if (prefs.getAll().size() > 40) {
                // Drop oldest-ish by rewriting only recent keys we touch; simple trim:
                // leave as-is unless severely over — SharedPreferences is small JSON.
                Log.i(TAG, "Playable cache size=" + prefs.getAll().size());
            }
            // If Exo is already on this title, apply chapter markers immediately.
            if (
                mediaId.equals(nativeMediaId)
                    || mediaId.equals(getPersistedMediaId())
            ) {
                LibraryNativePlayer.Playable parsed =
                    LibraryNativePlayer.parsePlayable(playableJson);
                if (parsed != null && !parsed.chapters.isEmpty()) {
                    LibraryNativePlayer.getInstance().updateChapters(parsed.chapters);
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "putPlayableCache failed", e);
        }
    }

    /**
     * Lightweight resume-position stamp during phone HTML5 playback so AA cold
     * start does not revive a stale car-session offset.
     */
    public void stampPlayableCachePosition(String mediaId, long bookGlobalMs, int trackIndex) {
        if (mediaId == null || mediaId.isEmpty() || bookGlobalMs < 0) {
            return;
        }
        Context ctx = appContextRef.get();
        if (ctx == null) {
            return;
        }
        try {
            SharedPreferences prefs =
                ctx.getSharedPreferences(PLAYABLE_PREFS, Context.MODE_PRIVATE);
            String raw = prefs.getString(mediaId, null);
            if (raw == null || raw.isEmpty()) {
                return;
            }
            JSONObject o = new JSONObject(raw);
            long existingTs = o.optLong("positionUpdatedAt", 0);
            long now = System.currentTimeMillis();
            // Ignore stale stamps (e.g. late Exo tick after phone advanced further).
            if (existingTs > now + 2_000) {
                return;
            }
            LibraryNativePlayer.Playable base = LibraryNativePlayer.parsePlayable(o);
            if (base == null) {
                return;
            }
            long priorGlobal = Math.max(0, base.positionMs + trackOffset(base));
            if (
                existingTs > 0
                    && bookGlobalMs + 5_000 < priorGlobal
                    && now < existingTs + 60_000
            ) {
                // Incoming is meaningfully behind a recent stamp — keep newer.
                return;
            }
            LibraryNativePlayer.Playable updated =
                playableWithGlobalPosition(base, bookGlobalMs);
            o.put("position", updated.positionMs / 1000.0);
            o.put(
                "trackIndex",
                trackIndex >= 0 ? trackIndex : updated.trackIndex
            );
            o.put("positionUpdatedAt", now);
            prefs.edit().putString(mediaId, o.toString()).apply();
            setBookGlobalPositionMs(bookGlobalMs);
            persistMediaId(mediaId);
        } catch (Exception e) {
            Log.w(TAG, "stampPlayableCachePosition(mediaId) failed", e);
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
    /**
     * Resume ExoPlayer if still alive; otherwise cold-start from persisted
     * mediaId + playable cache. Does not require Capacitor / WebView.
     */
    public boolean tryNativeResumeOrRestart() {
        if (tryNativeResume()) {
            return true;
        }
        String mid = !nativeMediaId.isEmpty() ? nativeMediaId : getPersistedMediaId();
        if (mid == null || mid.isEmpty()) {
            Log.i(TAG, "Native resume/restart: no persisted mediaId");
            return false;
        }
        Log.i(TAG, "Native cold-start from playable cache mediaId=" + mid);
        return tryNativePlayFromMediaId(mid);
    }

    /**
     * Immediately publish Now Playing + BUFFERING so Android Auto leaves the
     * browse "loading" spinner while ExoPlayer / WebView start the title.
     */
    public void beginPlayFromMediaId(String mediaId) {
        if (mediaId == null || mediaId.isEmpty() || NOW_PLAYING_ID.equals(mediaId)) {
            return;
        }
        persistMediaId(mediaId);
        String nextTitle = title;
        String nextArtist = artist;
        long nextDuration = durationMs;
        long nextPosition = positionMs;
        LibraryNativePlayer.Playable playable = getPlayableCache(mediaId);
        if (playable != null) {
            nextTitle = playable.title != null ? playable.title : nextTitle;
            long globalPos = Math.max(0, playable.positionMs + trackOffset(playable));
            int tIdx =
                Math.min(
                    Math.max(0, playable.trackIndex),
                    Math.max(0, playable.tracks.size() - 1)
                );
            LibraryNativePlayer.DisplayScope scope =
                LibraryNativePlayer.resolveDisplayScope(
                    globalPos,
                    tIdx,
                    playable.totalDurationMs,
                    playable.tracks,
                    playable.chapters
                );
            nextArtist =
                !scope.label.isEmpty()
                    ? scope.label
                    : (playable.author != null ? playable.author : nextArtist);
            nextDuration = Math.max(0, scope.durationMs);
            nextPosition = Math.max(0, scope.positionMs);
        } else {
            AutoBrowseNode node = findCachedPlayableNode(mediaId);
            if (node != null) {
                if (node.title != null && !node.title.isEmpty()) {
                    nextTitle = node.title;
                }
                if (node.subtitle != null) {
                    nextArtist = node.subtitle;
                }
            }
        }
        if (nextTitle == null || nextTitle.isEmpty()) {
            nextTitle = "Loading…";
        }
        this.buffering = true;
        this.active = true;
        this.playing = false;
        this.title = nextTitle;
        this.artist = nextArtist != null ? nextArtist : "";
        this.durationMs = nextDuration;
        this.positionMs = nextPosition;
        ignorePausedSyncUntilElapsed =
            SystemClock.elapsedRealtime() + PAUSED_SYNC_GRACE_MS;
        refreshSession(true);
        notifyRootChanged();
        Log.i(TAG, "AA beginPlayFromMediaId mediaId=" + mediaId + " title=" + nextTitle);
    }

    @Nullable
    private AutoBrowseNode findCachedPlayableNode(String mediaId) {
        for (AutoBrowseNode n : getBrowseCache(CONTINUE_ID)) {
            if (mediaId.equals(n.mediaId)) {
                return n;
            }
        }
        // Letter folders are cached as library/letter:X — scan recent prefs lightly.
        Context ctx = appContextRef.get();
        if (ctx == null) {
            return null;
        }
        Map<String, ?> all =
            ctx.getSharedPreferences(BROWSE_PREFS, Context.MODE_PRIVATE).getAll();
        for (Map.Entry<String, ?> e : all.entrySet()) {
            String key = e.getKey();
            if (key == null || !key.startsWith("node:")) {
                continue;
            }
            Object val = e.getValue();
            if (!(val instanceof String) || ((String) val).isEmpty()) {
                continue;
            }
            for (AutoBrowseNode n : getBrowseCache(key.substring("node:".length()))) {
                if (mediaId.equals(n.mediaId)) {
                    return n;
                }
            }
        }
        return null;
    }

    public boolean tryNativePlayFromMediaId(String mediaId) {
        if (mediaId == null || mediaId.isEmpty()) {
            return false;
        }
        if ("now_playing".equals(mediaId)) {
            return tryNativeResumeOrRestart();
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
        // Resolve freshest BOOK-GLOBAL resume: playable cache (phone walk progress /
        // warm Continue) vs persisted car-session bookGlobal. Prefer newer
        // positionUpdatedAt; when timestamps are close/missing take farther.
        // Never let a stale car snapshot rewind past fresher phone progress.
        String persistedId = getPersistedMediaId();
        long cacheGlobal = Math.max(0, playable.positionMs + trackOffset(playable));
        long sessionGlobal = Math.max(0, bookGlobalPositionMs);
        long cacheTs = readPlayablePositionUpdatedAt(mediaId);
        long sessionTs = bookGlobalUpdatedAtMs;
        long trustedGlobal = pickFreshestGlobalMs(
            cacheGlobal,
            cacheTs,
            sessionGlobal,
            sessionTs
        );
        if (trustedGlobal <= 0) {
            trustedGlobal = Math.max(cacheGlobal, sessionGlobal);
        }
        if (
            mediaId.equals(persistedId)
                || cacheGlobal > 0
                || sessionGlobal > 0
        ) {
            if (playableHasUsableOffsets(playable) || playable.tracks.size() <= 1) {
                playable = playableWithGlobalPosition(playable, trustedGlobal);
            }
        }
        long globalPos = playable.positionMs + trackOffset(playable);
        bookGlobalPositionMs = globalPos;
        bookGlobalUpdatedAtMs = System.currentTimeMillis();
        Log.i(
            TAG,
            "Native play resume mediaId="
                + mediaId
                + " cacheGlobalMs="
                + cacheGlobal
                + " sessionGlobalMs="
                + sessionGlobal
                + " chosenMs="
                + globalPos
                + " cacheTs="
                + cacheTs
                + " sessionTs="
                + sessionTs
        );
        int tIdx =
            Math.min(
                Math.max(0, playable.trackIndex),
                Math.max(0, playable.tracks.size() - 1)
            );
        LibraryNativePlayer.DisplayScope scope =
            LibraryNativePlayer.resolveDisplayScope(
                globalPos,
                tIdx,
                playable.totalDurationMs,
                playable.tracks,
                playable.chapters
            );
        // Optimistic session metadata before first Exo tick — chapter/track scoped.
        update(
            playable.title,
            !scope.label.isEmpty() ? scope.label : playable.author,
            playable.author,
            null,
            true,
            true,
            scope.durationMs,
            scope.positionMs,
            1.0f
        );
        nativeOwnsPlayback = true;
        nativeMediaId = mediaId;
        persistMediaId(mediaId);
        LibraryNativePlayer.getInstance().play(ctx, playable);
        return true;
    }

    /** True when track startOffsets form a usable book timeline (not all-zero). */
    private static boolean playableHasUsableOffsets(LibraryNativePlayer.Playable playable) {
        if (playable == null || playable.tracks.isEmpty()) {
            return false;
        }
        if (playable.tracks.size() == 1) {
            return true;
        }
        for (int i = 1; i < playable.tracks.size(); i++) {
            if (playable.tracks.get(i).startOffsetMs > 0) {
                return true;
            }
        }
        // Multi-file with all-zero offsets: trust trackIndex+local from cache only.
        return false;
    }

    /** Seek a cached playable to a book-global position (ms). */
    private static LibraryNativePlayer.Playable playableWithGlobalPosition(
        LibraryNativePlayer.Playable playable,
        long globalMs
    ) {
        if (playable == null || playable.tracks.isEmpty() || globalMs <= 0) {
            return playable;
        }
        int idx = 0;
        long local = globalMs;
        for (int i = 0; i < playable.tracks.size(); i++) {
            LibraryNativePlayer.TrackSpec tr = playable.tracks.get(i);
            long start = tr.startOffsetMs;
            long dur = tr.durationMs;
            long end = dur > 0 ? start + dur : Long.MAX_VALUE;
            if (globalMs >= start && globalMs < end) {
                idx = i;
                local = Math.max(0, globalMs - start);
                break;
            }
            if (i == playable.tracks.size() - 1) {
                idx = i;
                local = Math.max(0, globalMs - start);
            }
        }
        return new LibraryNativePlayer.Playable(
            playable.mediaId,
            playable.title,
            playable.author,
            playable.coverUrl,
            playable.authToken,
            local,
            idx,
            playable.totalDurationMs,
            playable.tracks,
            playable.chapters
        );
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
        // Ensure AA transport shows PLAYING + pause/skip actions immediately
        // (auto-resume used to leave a paused/restored session chrome).
        this.active = true;
        this.buffering = false;
        this.playing = true;
        ignorePausedSyncUntilElapsed =
            SystemClock.elapsedRealtime() + PAUSED_SYNC_GRACE_MS;
        refreshSession(true);
        return true;
    }

    public boolean tryNativePause() {
        if (!isNativeOwningPlayback()) {
            return false;
        }
        LibraryNativePlayer.getInstance().pause();
        stampPlayableCachePosition();
        return true;
    }

    /** Write current Exo position back into playable prefs for cold restart. */
    private void stampPlayableCachePosition() {
        String mid = !nativeMediaId.isEmpty() ? nativeMediaId : getPersistedMediaId();
        if (mid == null || mid.isEmpty()) {
            return;
        }
        long global = Math.max(0, LibraryNativePlayer.getInstance().getPositionMs());
        stampPlayableCachePosition(mid, global, -1);
    }

    private long readPlayablePositionUpdatedAt(String mediaId) {
        Context ctx = appContextRef.get();
        if (ctx == null || mediaId == null || mediaId.isEmpty()) {
            return 0;
        }
        try {
            String raw =
                ctx
                    .getSharedPreferences(PLAYABLE_PREFS, Context.MODE_PRIVATE)
                    .getString(mediaId, null);
            if (raw == null || raw.isEmpty()) {
                return 0;
            }
            return new JSONObject(raw).optLong("positionUpdatedAt", 0);
        } catch (Exception e) {
            return 0;
        }
    }

    /**
     * Newer timestamp wins; when within 5s (or either missing), take farther
     * progress so a car reconnect cannot rewind a phone walk session.
     */
    private static long pickFreshestGlobalMs(
        long aMs,
        long aTs,
        long bMs,
        long bTs
    ) {
        if (aMs <= 0 && bMs <= 0) {
            return 0;
        }
        if (aMs <= 0) {
            return bMs;
        }
        if (bMs <= 0) {
            return aMs;
        }
        if (aTs > 0 && bTs > 0) {
            if (aTs > bTs + 5_000) {
                return aMs;
            }
            if (bTs > aTs + 5_000) {
                return bMs;
            }
        }
        return Math.max(aMs, bMs);
    }

    public boolean tryNativeSeekRelative(long deltaMs) {
        if (!isNativeOwningPlayback()) {
            return false;
        }
        LibraryNativePlayer.getInstance().seekRelative(deltaMs);
        return true;
    }

    /**
     * Seek from the AA / lock-screen scrubber (chapter/track-local display ms).
     * Converts to book-global before ExoPlayer seek.
     */
    public boolean tryNativeSeekToDisplay(long displayPositionMs) {
        if (!isNativeOwningPlayback()) {
            return false;
        }
        long global =
            LibraryNativePlayer.getInstance().displayToGlobalMs(displayPositionMs);
        bookGlobalPositionMs = global;
        LibraryNativePlayer.getInstance().seekTo(global);
        return true;
    }

    /** Seek to a book-global position (phone UI / JS). */
    public boolean tryNativeSeekTo(long bookGlobalPositionMs) {
        if (!isNativeOwningPlayback()) {
            return false;
        }
        this.bookGlobalPositionMs = Math.max(0, bookGlobalPositionMs);
        LibraryNativePlayer.getInstance().seekTo(bookGlobalPositionMs);
        return true;
    }

    public boolean tryNativeSkipNext() {
        if (!isNativeOwningPlayback()) {
            return false;
        }
        // Prefer chapter markers (ABS) — track skip is a no-op on single-file books.
        LibraryNativePlayer.getInstance().skipToNextChapterOrTrack();
        return true;
    }

    public boolean tryNativeSkipPrevious() {
        if (!isNativeOwningPlayback()) {
            return false;
        }
        LibraryNativePlayer.getInstance().skipToPreviousChapterOrTrack();
        return true;
    }

    /** WebView is becoming the audio owner — stop ExoPlayer without wiping AA session. */
    public void handOffNativeToWebView() {
        nativeOwnsPlayback = false;
        buffering = false;
        LibraryNativePlayer.getInstance().handOffToWebView();
    }

    public void stopNativePlayback() {
        nativeOwnsPlayback = false;
        nativeMediaId = "";
        buffering = false;
        LibraryNativePlayer.getInstance().stopAndReleaseOwnership();
    }

    /** Record book-global position from WebView sync (distinct from scrubber pos). */
    public void setBookGlobalPositionMs(long globalMs) {
        if (globalMs > 0) {
            // Do not let an older Exo/AA tick rewind a newer phone stamp.
            long now = System.currentTimeMillis();
            if (
                bookGlobalUpdatedAtMs > 0
                    && globalMs + 5_000 < bookGlobalPositionMs
                    && now < bookGlobalUpdatedAtMs + 15_000
            ) {
                return;
            }
            bookGlobalPositionMs = globalMs;
            bookGlobalUpdatedAtMs = now;
        }
    }

    public long getBookGlobalPositionMs() {
        return bookGlobalPositionMs;
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
        if (!active) {
            buffering = false;
        }
        this.durationMs = Math.max(0, durationMs);
        this.positionMs = Math.max(0, positionMs);
        this.playbackSpeed = playbackSpeed > 0 ? playbackSpeed : 1.0f;

        boolean rootChanged =
            wasActive != this.active || !previousRootKey.equals(nowPlayingRootKey());
        refreshSession(true);
        long now = System.currentTimeMillis();
        if (now - lastPersistSessionMs >= PERSIST_SESSION_MIN_INTERVAL_MS) {
            lastPersistSessionMs = now;
            persistSession();
        }
        if (rootChanged) {
            notifyRootChanged();
        }
    }

    /** Position / transport-only sync — avoids rebuilding browse tree artwork. */
    public void updatePosition(boolean playing, long positionMs, float playbackSpeed) {
        // WebView may resume audio after BT reconnect while native still has
        // active=false (session cleared). Promote so AA shows Now Playing.
        if (playing && !active && (!title.isEmpty() || !nativeMediaId.isEmpty())) {
            this.active = true;
        }
        this.playing = applyPlayingSync(playing);
        this.positionMs = Math.max(0, positionMs);
        this.playbackSpeed = playbackSpeed > 0 ? playbackSpeed : 1.0f;
        refreshSession(playing || buffering);
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
            buffering = false;
            return true;
        }
        if (SystemClock.elapsedRealtime() < ignorePausedSyncUntilElapsed) {
            return this.playing;
        }
        if (this.playing) {
            pausedAtElapsed = SystemClock.elapsedRealtime();
        }
        buffering = false;
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
        // Resume / car reconnect may fire play before JS re-syncs metadata.
        // Promote a restored or buffering session so AA shows transport controls.
        if (!active && playing && (!title.isEmpty() || !nativeMediaId.isEmpty())) {
            this.active = true;
        }
        if (!active) {
            return;
        }
        this.playing = playing;
        if (playing) {
            buffering = false;
            // Sample before clearing pause timestamp — grace size depends on idle depth.
            boolean deep = isDeepIdlePause() || deepIdlePlayLatched;
            deepIdlePlayLatched = deep;
            long grace = deep ? FOCUS_LOSS_GRACE_LONG_IDLE_MS : FOCUS_LOSS_GRACE_MS;
            pausedAtElapsed = 0;
            long now = SystemClock.elapsedRealtime();
            ignorePausedSyncUntilElapsed = now + PAUSED_SYNC_GRACE_MS;
            ignoreFocusLossUntilElapsed = now + grace;
        } else {
            buffering = false;
            ignorePausedSyncUntilElapsed = 0;
            ignoreFocusLossUntilElapsed = 0;
            deepIdlePlayLatched = false;
            pausedAtElapsed = SystemClock.elapsedRealtime();
        }
        refreshSession(true);
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
        buffering = false;
        lastNativeMetaKey = "";
        lastNativePlaying = null;
        lastSessionMetaSig = "";
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
            // Skip background JS refresh while ExoPlayer owns PCM — waking the
            // WebView mid-play is a known OOM contributor on some OEMs.
            if (emitter != null && !isNativeOwningPlayback()) {
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
            .putLong("bookGlobalPositionMs", bookGlobalPositionMs)
            .putLong("bookGlobalUpdatedAtMs", bookGlobalUpdatedAtMs)
            .putFloat("playbackSpeed", playbackSpeed);
        String mid = !nativeMediaId.isEmpty() ? nativeMediaId : getPersistedMediaId();
        if (mid != null && !mid.isEmpty()) {
            ed.putString("mediaId", mid);
        }
        ed.apply();
    }

    /** Remember which book AA should cold-restart after idle. */
    public void rememberMediaId(String mediaId) {
        persistMediaId(mediaId);
    }

    private void persistMediaId(String mediaId) {
        if (mediaId == null || mediaId.isEmpty() || "now_playing".equals(mediaId)) {
            return;
        }
        nativeMediaId = mediaId;
        Context ctx = appContextRef.get();
        if (ctx == null) {
            return;
        }
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString("mediaId", mediaId)
            .apply();
    }

    @Nullable
    public String getPersistedMediaId() {
        if (!nativeMediaId.isEmpty()) {
            return nativeMediaId;
        }
        Context ctx = appContextRef.get();
        if (ctx == null) {
            return null;
        }
        String mid =
            ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString("mediaId", "");
        return mid != null && !mid.isEmpty() ? mid : null;
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
        this.bookGlobalPositionMs = prefs.getLong("bookGlobalPositionMs", 0);
        this.bookGlobalUpdatedAtMs = prefs.getLong("bookGlobalUpdatedAtMs", 0);
        // Legacy builds only stored scrubber position — do not treat it as global
        // when it may be chapter-scoped. Prefer 0 so playable cache wins.
        if (this.bookGlobalPositionMs <= 0) {
            this.bookGlobalPositionMs = 0;
            this.bookGlobalUpdatedAtMs = 0;
        }
        this.playbackSpeed = prefs.getFloat("playbackSpeed", 1.0f);
        String mid = prefs.getString("mediaId", "");
        if (mid != null && !mid.isEmpty()) {
            this.nativeMediaId = mid;
        }
        Log.i(
            TAG,
            "Restored AA session metadata: " + title + " mediaId=" + this.nativeMediaId
        );
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

        int state;
        if (!active) {
            state = PlaybackStateCompat.STATE_NONE;
        } else if (playing) {
            state = PlaybackStateCompat.STATE_PLAYING;
        } else if (buffering) {
            state = PlaybackStateCompat.STATE_BUFFERING;
        } else {
            state = PlaybackStateCompat.STATE_PAUSED;
        }

        // Custom ±15 actions: AA often reserves side slots for chapter skip when
        // SKIP_TO_PREVIOUS/NEXT are set; custom actions keep seek buttons visible.
        PlaybackStateCompat.Builder stateBuilder = new PlaybackStateCompat.Builder()
            .setActions(actions)
            // Explicit updateTime so AA keeps extrapolating position across
            // chapter metadata swaps (without it, some head units freeze the timer).
            .setState(state, positionMs, playing ? playbackSpeed : 0f, SystemClock.elapsedRealtime())
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
            );
        String activeMediaId =
            !nativeMediaId.isEmpty()
                ? nativeMediaId
                : (getPersistedMediaId() != null ? getPersistedMediaId() : "");
        if (activeMediaId != null && !activeMediaId.isEmpty()) {
            // Helps AA match browse selection → Now Playing / controls UI.
            stateBuilder.setActiveQueueItemId(activeMediaId.hashCode() & 0xffffffffL);
        }
        session.setPlaybackState(stateBuilder.build());

        if (!active) {
            lastSessionMetaSig = "";
            session.setMetadata(null);
            LibraryMediaBrowserService service = serviceRef.get();
            if (service != null) {
                service.stopForegroundPlayback();
            }
            return;
        }

        if (metadataMayHaveChanged) {
            // Skip no-op metadata pushes — binder copies of album art are expensive
            // and repeating them mid-play freezes some OEM system_servers.
            String artSig =
                (artwork != null && !artwork.isRecycled())
                    ? (artwork.getWidth() + "x" + artwork.getHeight() + "@" + System.identityHashCode(artwork))
                    : "noart";
            String midForMeta = activeMediaId != null ? activeMediaId : "";
            String metaSig =
                title + "|" + artist + "|" + album + "|" + durationMs + "|" + midForMeta + "|" + artSig;
            if (!metaSig.equals(lastSessionMetaSig)) {
                lastSessionMetaSig = metaSig;
                MediaMetadataCompat.Builder metaBuilder = new MediaMetadataCompat.Builder()
                    .putString(MediaMetadataCompat.METADATA_KEY_TITLE, title)
                    .putString(MediaMetadataCompat.METADATA_KEY_ARTIST, artist)
                    .putString(MediaMetadataCompat.METADATA_KEY_ALBUM, album)
                    .putLong(MediaMetadataCompat.METADATA_KEY_DURATION, durationMs);
                if (!midForMeta.isEmpty()) {
                    metaBuilder.putString(
                        MediaMetadataCompat.METADATA_KEY_MEDIA_ID,
                        midForMeta
                    );
                }
                // Never put a null bitmap — that clears cover art on many AA units.
                // One bitmap key is enough; duplicating ALBUM_ART + DISPLAY_ICON
                // copies the same pixels into the system binder payload twice.
                if (artwork != null && !artwork.isRecycled()) {
                    metaBuilder.putBitmap(MediaMetadataCompat.METADATA_KEY_ALBUM_ART, artwork);
                }
                session.setMetadata(metaBuilder.build());
            }
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
