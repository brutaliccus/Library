package com.freiverse.library;

import android.content.Context;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import androidx.annotation.Nullable;
import androidx.media3.common.AudioAttributes;
import androidx.media3.common.C;
import androidx.media3.common.MediaItem;
import androidx.media3.common.PlaybackException;
import androidx.media3.common.Player;
import androidx.media3.datasource.DefaultDataSource;
import androidx.media3.datasource.DefaultHttpDataSource;
import androidx.media3.exoplayer.DefaultLoadControl;
import androidx.media3.exoplayer.ExoPlayer;
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory;
import java.util.ArrayList;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Single native audio owner for Android Auto / lock-screen play when the
 * WebView is frozen. WebView HTML5 {@code <audio>} must not also decode while
 * this player is active — attach UI to this session instead of re-buffering.
 */
public final class LibraryNativePlayer {

    private static final String TAG = "LibraryNativePlayer";
    /** Position ticks for MediaSession scrubber — keep cheap (no metadata). */
    private static final long POSITION_TICK_MS = 2_000;
    /** Cap Exo readahead — large progressive buffers OOM mid-AA-play. */
    private static final int MIN_BUFFER_MS = 12_000;
    private static final int MAX_BUFFER_MS = 28_000;
    private static final int PLAYBACK_BUFFER_MS = 1_500;
    private static final int REBUFFER_MS = 3_000;
    private static final int TARGET_BUFFER_BYTES = 2 * 1024 * 1024;

    public interface Listener {
        /**
         * @param displayDurationMs chapter/track duration for AA scrubber
         * @param displayPositionMs chapter/track-local position for AA scrubber
         * @param bookGlobalPositionMs full-book position for resume / JS attach
         */
        void onNativePlaying(
            String mediaId,
            String title,
            String artist,
            String album,
            String coverUrl,
            boolean playing,
            long displayDurationMs,
            long displayPositionMs,
            long bookGlobalPositionMs,
            float speed,
            int trackIndex
        );

        void onNativeStopped();

        void onNativeError(String message);
    }

    public static final class TrackSpec {
        public final String contentUrl;
        public final String title;
        public final long startOffsetMs;
        public final long durationMs;
        public final String mimeType;

        public TrackSpec(
            String contentUrl,
            String title,
            long startOffsetMs,
            long durationMs,
            String mimeType
        ) {
            this.contentUrl = contentUrl;
            this.title = title != null ? title : "";
            this.startOffsetMs = Math.max(0, startOffsetMs);
            this.durationMs = Math.max(0, durationMs);
            this.mimeType = mimeType != null ? mimeType : "";
        }
    }

    /** Chapter markers in book-global milliseconds (ABS / embedded). */
    public static final class ChapterSpec {
        public final String title;
        public final long startMs;
        public final long endMs;

        public ChapterSpec(String title, long startMs, long endMs) {
            this.title = title != null ? title : "";
            this.startMs = Math.max(0, startMs);
            this.endMs = Math.max(this.startMs, endMs);
        }
    }

    /** AA / lock-screen scrubber scope (chapter preferred, else track). */
    public static final class DisplayScope {
        public final String label;
        public final long startMs;
        public final long durationMs;
        public final long positionMs;

        public DisplayScope(String label, long startMs, long durationMs, long positionMs) {
            this.label = label != null ? label : "";
            this.startMs = Math.max(0, startMs);
            this.durationMs = Math.max(0, durationMs);
            this.positionMs = Math.max(0, positionMs);
        }
    }

    public static final class Playable {
        public final String mediaId;
        public final String title;
        public final String author;
        public final String coverUrl;
        public final String authToken;
        public final long positionMs;
        public final int trackIndex;
        public final long totalDurationMs;
        public final List<TrackSpec> tracks;
        public final List<ChapterSpec> chapters;

        public Playable(
            String mediaId,
            String title,
            String author,
            String coverUrl,
            String authToken,
            long positionMs,
            int trackIndex,
            long totalDurationMs,
            List<TrackSpec> tracks
        ) {
            this(
                mediaId,
                title,
                author,
                coverUrl,
                authToken,
                positionMs,
                trackIndex,
                totalDurationMs,
                tracks,
                null
            );
        }

        public Playable(
            String mediaId,
            String title,
            String author,
            String coverUrl,
            String authToken,
            long positionMs,
            int trackIndex,
            long totalDurationMs,
            List<TrackSpec> tracks,
            List<ChapterSpec> chapters
        ) {
            this.mediaId = mediaId;
            this.title = title != null ? title : "";
            this.author = author != null ? author : "";
            this.coverUrl = coverUrl != null ? coverUrl : "";
            this.authToken = authToken != null ? authToken : "";
            this.positionMs = Math.max(0, positionMs);
            this.trackIndex = Math.max(0, trackIndex);
            this.totalDurationMs = Math.max(0, totalDurationMs);
            this.tracks = tracks != null ? tracks : new ArrayList<>();
            this.chapters = chapters != null ? chapters : new ArrayList<>();
        }
    }

    private static final LibraryNativePlayer INSTANCE = new LibraryNativePlayer();

    public static LibraryNativePlayer getInstance() {
        return INSTANCE;
    }

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private ExoPlayer player;
    private final List<Listener> listeners = new ArrayList<>();
    private String mediaId = "";
    private String title = "";
    private String artist = "";
    private String album = "";
    private String coverUrl = "";
    private String lastAuthToken = "";
    private long totalDurationMs = 0;
    private List<TrackSpec> tracks = new ArrayList<>();
    private List<ChapterSpec> chapters = new ArrayList<>();
    /** Book-global start of the active AA display scope (chapter or track). */
    private volatile long displayScopeStartMs = 0;
    private volatile boolean owning = false;
    private final Runnable tickRunnable = this::tickPosition;

    /**
     * Main-thread mirror of ExoPlayer state for off-main readers. ExoPlayer
     * hard-crashes ("Player is accessed on the wrong thread") when touched from
     * the CapacitorPlugins thread — which is exactly where
     * getNativePlaybackState() runs when the app UI attaches mid-AA-play.
     * Refreshed by every emitState()/tick (~2s) on the main thread.
     */
    private volatile boolean mirrorPlaying = false;
    private volatile long mirrorPositionMs = 0;
    private volatile long mirrorPositionAtElapsed = 0;
    private volatile float mirrorSpeed = 1f;
    private volatile int mirrorTrackIndex = 0;

    private LibraryNativePlayer() {}

    public synchronized void addListener(Listener l) {
        if (l != null && !listeners.contains(l)) {
            listeners.add(l);
        }
    }

    public synchronized void removeListener(Listener l) {
        listeners.remove(l);
    }

    /** @deprecated Prefer {@link #addListener}; kept for single-listener call sites. */
    public void setListener(@Nullable Listener l) {
        listeners.clear();
        if (l != null) {
            listeners.add(l);
        }
    }

    private synchronized List<Listener> copyListeners() {
        return new ArrayList<>(listeners);
    }

    public boolean isOwning() {
        return owning && player != null;
    }

    public boolean isPlaying() {
        if (Looper.myLooper() != Looper.getMainLooper()) {
            // Never touch ExoPlayer off-main — it throws IllegalStateException
            // and takes the whole app down mid-playback.
            return owning && mirrorPlaying;
        }
        return player != null && player.isPlaying();
    }

    @Nullable
    public String getMediaId() {
        return mediaId.isEmpty() ? null : mediaId;
    }

    public long getPositionMs() {
        if (Looper.myLooper() != Looper.getMainLooper()) {
            long base = mirrorPositionMs;
            long at = mirrorPositionAtElapsed;
            if (mirrorPlaying && at > 0) {
                base += (long) ((android.os.SystemClock.elapsedRealtime() - at) * mirrorSpeed);
            }
            return Math.max(0, base);
        }
        if (player == null) {
            return 0;
        }
        int idx = Math.max(0, player.getCurrentMediaItemIndex());
        long local = Math.max(0, player.getCurrentPosition());
        // Prefer playlist-relative if tracks carry offsets via window.
        return local + trackStartOffsetMs(idx);
    }

    private long trackStartOffsetMs(int idx) {
        // Prefer the playlist item at idx (tag = startOffsetMs). Fall back to current.
        if (player == null) {
            return 0;
        }
        MediaItem item = null;
        if (idx >= 0 && idx < player.getMediaItemCount()) {
            item = player.getMediaItemAt(idx);
        }
        if (item == null) {
            item = player.getCurrentMediaItem();
        }
        if (item == null || item.localConfiguration == null) {
            return 0;
        }
        Object tag = item.localConfiguration.tag;
        if (tag instanceof Long) {
            return (Long) tag;
        }
        return 0;
    }

    public void play(Context context, Playable playable) {
        if (context == null || playable == null || playable.tracks.isEmpty()) {
            return;
        }
        mainHandler.post(() -> playOnMain(context.getApplicationContext(), playable));
    }

    private void playOnMain(Context app, Playable playable) {
        ensurePlayer(app, playable.authToken);

        mediaId = playable.mediaId;
        title = playable.title;
        artist = playable.author;
        album = playable.author;
        coverUrl = playable.coverUrl;
        totalDurationMs = playable.totalDurationMs;
        tracks = new ArrayList<>(playable.tracks);
        chapters = new ArrayList<>(playable.chapters);
        owning = true;

        List<MediaItem> items = new ArrayList<>();
        for (TrackSpec t : playable.tracks) {
            if (t.contentUrl == null || t.contentUrl.isEmpty()) {
                continue;
            }
            MediaItem.Builder b = new MediaItem.Builder()
                .setUri(Uri.parse(t.contentUrl))
                .setTag(t.startOffsetMs);
            if (!t.mimeType.isEmpty()) {
                b.setMimeType(t.mimeType);
            }
            items.add(b.build());
        }
        if (items.isEmpty()) {
            owning = false;
            for (Listener l : copyListeners()) {
                l.onNativeError("No playable tracks");
            }
            return;
        }

        // positionMs is track-local (seconds→ms from JS); trackIndex selects the file.
        int startIdx = Math.min(playable.trackIndex, items.size() - 1);
        long seekInTrack = Math.max(0, playable.positionMs);

        player.setMediaItems(items, startIdx, seekInTrack);
        player.prepare();
        player.play();
        emitState(true);
        scheduleTick();
        Log.i(
            TAG,
            "Native play started mediaId="
                + mediaId
                + " tracks="
                + items.size()
                + " maxBufferMs="
                + MAX_BUFFER_MS
        );
    }

    public void resume() {
        mainHandler.post(() -> {
            if (player == null || !owning) {
                return;
            }
            player.play();
            emitState(true);
            scheduleTick();
        });
    }

    public void pause() {
        mainHandler.post(() -> {
            if (player == null) {
                return;
            }
            player.pause();
            emitState(false);
            mainHandler.removeCallbacks(tickRunnable);
        });
    }

    public void stopAndReleaseOwnership() {
        mainHandler.post(() -> {
            mainHandler.removeCallbacks(tickRunnable);
            if (player != null) {
                try {
                    player.stop();
                    player.clearMediaItems();
                } catch (Exception e) {
                    Log.w(TAG, "stop failed", e);
                }
            }
            boolean was = owning;
            owning = false;
            mediaId = "";
            mirrorPlaying = false;
            if (was) {
                for (Listener l : copyListeners()) {
                    l.onNativeStopped();
                }
            }
        });
    }

    /** WebView is taking over — release ExoPlayer without clearing session metadata. */
    public void handOffToWebView() {
        mainHandler.post(() -> {
            mainHandler.removeCallbacks(tickRunnable);
            long pos = getPositionMs();
            if (player != null) {
                try {
                    player.pause();
                    player.stop();
                    player.clearMediaItems();
                } catch (Exception e) {
                    Log.w(TAG, "handOff stop failed", e);
                }
            }
            owning = false;
            mirrorPlaying = false;
            Log.i(TAG, "Handed off to WebView at posMs=" + pos);
        });
    }

    public void seekTo(long positionMs) {
        mainHandler.post(() -> {
            if (player == null || !owning) {
                return;
            }
            // Map book-global ms onto the correct playlist window.
            int count = player.getMediaItemCount();
            int targetIdx = player.getCurrentMediaItemIndex();
            long local = Math.max(0, positionMs);
            for (int i = 0; i < count; i++) {
                long start = trackStartOffsetMs(i);
                long nextStart =
                    i + 1 < count ? trackStartOffsetMs(i + 1) : Long.MAX_VALUE;
                if (positionMs >= start && positionMs < nextStart) {
                    targetIdx = i;
                    local = Math.max(0, positionMs - start);
                    break;
                }
                if (i == count - 1) {
                    targetIdx = i;
                    local = Math.max(0, positionMs - start);
                }
            }
            player.seekTo(targetIdx, local);
            emitState(player.isPlaying());
        });
    }

    public void seekRelative(long deltaMs) {
        mainHandler.post(() -> {
            if (player == null || !owning) {
                return;
            }
            long next = Math.max(0, player.getCurrentPosition() + deltaMs);
            player.seekTo(next);
            emitState(player.isPlaying());
        });
    }

    public void skipToNextTrack() {
        mainHandler.post(() -> {
            if (player == null || !owning) {
                return;
            }
            if (player.hasNextMediaItem()) {
                player.seekToNextMediaItem();
                player.play();
                emitState(true);
            }
        });
    }

    public void skipToPreviousTrack() {
        mainHandler.post(() -> {
            if (player == null || !owning) {
                return;
            }
            if (player.getCurrentPosition() > 3_000) {
                player.seekTo(0);
            } else if (player.hasPreviousMediaItem()) {
                player.seekToPreviousMediaItem();
            } else {
                player.seekTo(0);
            }
            player.play();
            emitState(true);
        });
    }

    private void ensurePlayer(Context app, String authToken) {
        String token = authToken != null ? authToken : "";
        if (player != null && token.equals(lastAuthToken)) {
            return;
        }
        if (player != null) {
            releasePlayerOnly();
        }
        lastAuthToken = token;
        DefaultHttpDataSource.Factory http = new DefaultHttpDataSource.Factory()
            .setConnectTimeoutMs(15_000)
            .setReadTimeoutMs(30_000)
            .setAllowCrossProtocolRedirects(true)
            .setUserAgent("LibraryAndroidAuto/1.53");
        if (!token.isEmpty()) {
            java.util.Map<String, String> headers = new java.util.HashMap<>();
            headers.put("Authorization", "Bearer " + token);
            http.setDefaultRequestProperties(headers);
        }
        // DefaultDataSource supports file:// (offline disk cache) + http(s).
        // Progressive HTTP streams — never assemble full-track blobs in RAM.
        DefaultDataSource.Factory dataSourceFactory = new DefaultDataSource.Factory(app, http);
        DefaultMediaSourceFactory mediaSourceFactory = new DefaultMediaSourceFactory(app)
            .setDataSourceFactory(dataSourceFactory);

        DefaultLoadControl loadControl = new DefaultLoadControl.Builder()
            .setBufferDurationsMs(
                MIN_BUFFER_MS,
                MAX_BUFFER_MS,
                PLAYBACK_BUFFER_MS,
                REBUFFER_MS
            )
            .setTargetBufferBytes(TARGET_BUFFER_BYTES)
            .setPrioritizeTimeOverSizeThresholds(true)
            .build();

        AudioAttributes audioAttrs = new AudioAttributes.Builder()
            .setUsage(C.USAGE_MEDIA)
            .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC)
            .build();

        player = new ExoPlayer.Builder(app)
            .setMediaSourceFactory(mediaSourceFactory)
            .setLoadControl(loadControl)
            // LibraryAutoBridge owns audio focus for the MediaSession; avoid a
            // second focus request that would pause us via onAudioFocusChange.
            .setAudioAttributes(audioAttrs, /* handleAudioFocus= */ false)
            .setHandleAudioBecomingNoisy(true)
            .build();
        player.addListener(
            new Player.Listener() {
                @Override
                public void onIsPlayingChanged(boolean isPlaying) {
                    if (!owning) {
                        return;
                    }
                    emitState(isPlaying);
                    if (isPlaying) {
                        scheduleTick();
                    } else {
                        mainHandler.removeCallbacks(tickRunnable);
                    }
                }

                @Override
                public void onPlayerError(PlaybackException error) {
                    Log.w(TAG, "ExoPlayer error", error);
                    for (Listener l : copyListeners()) {
                        l.onNativeError(
                            error.getMessage() != null ? error.getMessage() : "playback error"
                        );
                    }
                }

                @Override
                public void onPlaybackStateChanged(int playbackState) {
                    if (playbackState == Player.STATE_ENDED && owning) {
                        if (player != null && player.hasNextMediaItem()) {
                            player.seekToNextMediaItem();
                            player.play();
                        } else {
                            emitState(false);
                        }
                    }
                }

                @Override
                public void onMediaItemTransition(
                    @Nullable MediaItem mediaItem,
                    int reason
                ) {
                    if (owning) {
                        // Track change — listeners must refresh metadata once.
                        emitState(player != null && player.isPlaying());
                    }
                }
            }
        );
    }

    private void releasePlayerOnly() {
        mainHandler.removeCallbacks(tickRunnable);
        if (player != null) {
            try {
                player.release();
            } catch (Exception e) {
                Log.w(TAG, "release failed", e);
            }
            player = null;
        }
        lastAuthToken = "";
    }

    public void release() {
        mainHandler.post(() -> {
            owning = false;
            mirrorPlaying = false;
            releasePlayerOnly();
        });
    }

    private void scheduleTick() {
        mainHandler.removeCallbacks(tickRunnable);
        mainHandler.postDelayed(tickRunnable, POSITION_TICK_MS);
    }

    private void tickPosition() {
        if (!owning || player == null || !player.isPlaying()) {
            return;
        }
        emitState(true);
        scheduleTick();
    }

    private void emitState(boolean playing) {
        if (!owning) {
            return;
        }
        long globalPos = 0;
        int trackIndex = 0;
        if (player != null) {
            trackIndex = Math.max(0, player.getCurrentMediaItemIndex());
            globalPos =
                Math.max(0, player.getCurrentPosition()) + trackStartOffsetMs(trackIndex);
        }
        float speed = player != null ? player.getPlaybackParameters().speed : 1f;
        DisplayScope scope = resolveDisplayScope(globalPos, trackIndex, totalDurationMs, tracks, chapters);
        displayScopeStartMs = scope.startMs;
        String scopeArtist =
            !scope.label.isEmpty() ? scope.label : (artist != null ? artist : "");
        // Refresh the off-main mirror before notifying listeners (book-global).
        mirrorPlaying = playing;
        mirrorPositionMs = globalPos;
        mirrorPositionAtElapsed = android.os.SystemClock.elapsedRealtime();
        mirrorSpeed = speed > 0 ? speed : 1f;
        mirrorTrackIndex = trackIndex;
        for (Listener l : copyListeners()) {
            l.onNativePlaying(
                mediaId,
                title,
                scopeArtist,
                album,
                coverUrl,
                playing,
                scope.durationMs,
                scope.positionMs,
                globalPos,
                speed > 0 ? speed : 1f,
                trackIndex
            );
        }
    }

    /** Convert AA scrubber (chapter/track-local) ms → book-global ms. */
    public long displayToGlobalMs(long displayPositionMs) {
        return Math.max(0, displayScopeStartMs + Math.max(0, displayPositionMs));
    }

    /**
     * Hot-swap chapter markers while native is already playing (ABS chapters
     * often arrive after the first audio tick). Triggers a metadata refresh.
     */
    public void updateChapters(List<ChapterSpec> nextChapters) {
        mainHandler.post(() -> {
            chapters =
                nextChapters != null
                    ? new ArrayList<>(nextChapters)
                    : new ArrayList<>();
            if (owning) {
                emitState(player != null && player.isPlaying());
            }
        });
    }

    public static DisplayScope resolveDisplayScope(
        long globalMs,
        int trackIndex,
        long totalDurationMs,
        List<TrackSpec> tracks,
        List<ChapterSpec> chapters
    ) {
        long g = Math.max(0, globalMs);
        if (chapters != null && !chapters.isEmpty()) {
            int idx = 0;
            for (int i = 0; i < chapters.size(); i++) {
                if (chapters.get(i).startMs <= g) {
                    idx = i;
                } else {
                    break;
                }
            }
            ChapterSpec ch = chapters.get(idx);
            long start = ch.startMs;
            long end = ch.endMs;
            if (end <= start) {
                end =
                    idx + 1 < chapters.size()
                        ? chapters.get(idx + 1).startMs
                        : Math.max(start, totalDurationMs);
            }
            long dur = Math.max(0, end - start);
            long local = Math.max(0, Math.min(g - start, dur > 0 ? dur : g - start));
            return new DisplayScope(ch.title, start, dur, local);
        }
        if (tracks != null && !tracks.isEmpty()) {
            int idx = Math.min(Math.max(0, trackIndex), tracks.size() - 1);
            TrackSpec tr = tracks.get(idx);
            long start = tr.startOffsetMs;
            long dur = tr.durationMs;
            // Single unknown-duration file: fall back to whole book.
            if (dur <= 0 && tracks.size() == 1 && totalDurationMs > 0) {
                dur = totalDurationMs;
                start = 0;
            }
            if (dur > 0) {
                long local = Math.max(0, Math.min(g - start, dur));
                String label =
                    tr.title != null && !tr.title.isEmpty()
                        ? tr.title
                        : ("Track " + (idx + 1));
                return new DisplayScope(label, start, dur, local);
            }
        }
        return new DisplayScope("", 0, Math.max(0, totalDurationMs), g);
    }

    @Nullable
    public static Playable parsePlayable(JSONObject o) {
        if (o == null) {
            return null;
        }
        String mediaId = o.optString("mediaId", "");
        if (mediaId.isEmpty()) {
            return null;
        }
        JSONArray tracksArr = o.optJSONArray("tracks");
        List<TrackSpec> tracks = new ArrayList<>();
        if (tracksArr != null) {
            for (int i = 0; i < tracksArr.length(); i++) {
                JSONObject t = tracksArr.optJSONObject(i);
                if (t == null) {
                    continue;
                }
                String url = t.optString("contentUrl", "");
                if (url.isEmpty()) {
                    continue;
                }
                tracks.add(
                    new TrackSpec(
                        url,
                        t.optString("title", ""),
                        Math.round(t.optDouble("startOffset", 0) * 1000.0),
                        Math.round(t.optDouble("duration", 0) * 1000.0),
                        t.optString("mimeType", "")
                    )
                );
            }
        }
        if (tracks.isEmpty()) {
            return null;
        }
        // Repair legacy caches that stored every startOffset as 0.
        boolean allZeroOffsets = true;
        boolean anyDuration = false;
        for (TrackSpec t : tracks) {
            if (t.startOffsetMs > 0) {
                allZeroOffsets = false;
            }
            if (t.durationMs > 0) {
                anyDuration = true;
            }
        }
        if (allZeroOffsets && anyDuration && tracks.size() > 1) {
            List<TrackSpec> fixed = new ArrayList<>();
            long offset = 0;
            for (TrackSpec t : tracks) {
                fixed.add(
                    new TrackSpec(
                        t.contentUrl,
                        t.title,
                        offset,
                        t.durationMs,
                        t.mimeType
                    )
                );
                offset += t.durationMs;
            }
            tracks = fixed;
        }
        long totalMs = Math.round(o.optDouble("totalDuration", 0) * 1000.0);
        if (totalMs <= 0) {
            long sum = 0;
            for (TrackSpec t : tracks) {
                sum += t.durationMs;
            }
            totalMs = sum;
        }
        List<ChapterSpec> chapters = new ArrayList<>();
        JSONArray chArr = o.optJSONArray("chapters");
        if (chArr != null) {
            for (int i = 0; i < chArr.length(); i++) {
                JSONObject c = chArr.optJSONObject(i);
                if (c == null) {
                    continue;
                }
                long startMs = Math.round(c.optDouble("start", 0) * 1000.0);
                long endMs;
                if (c.has("end") && !c.isNull("end")) {
                    endMs = Math.round(c.optDouble("end", 0) * 1000.0);
                } else if (i + 1 < chArr.length()) {
                    JSONObject next = chArr.optJSONObject(i + 1);
                    endMs =
                        next != null
                            ? Math.round(next.optDouble("start", 0) * 1000.0)
                            : totalMs;
                } else {
                    endMs = totalMs;
                }
                chapters.add(
                    new ChapterSpec(
                        c.optString("title", "Chapter " + (i + 1)),
                        startMs,
                        endMs
                    )
                );
            }
        }
        return new Playable(
            mediaId,
            o.optString("title", ""),
            o.optString("author", ""),
            o.optString("coverUrl", ""),
            o.optString("authToken", ""),
            Math.round(o.optDouble("position", 0) * 1000.0),
            o.optInt("trackIndex", 0),
            totalMs,
            tracks,
            chapters
        );
    }
}
