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
        void onNativePlaying(
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
            this.mediaId = mediaId;
            this.title = title != null ? title : "";
            this.author = author != null ? author : "";
            this.coverUrl = coverUrl != null ? coverUrl : "";
            this.authToken = authToken != null ? authToken : "";
            this.positionMs = Math.max(0, positionMs);
            this.trackIndex = Math.max(0, trackIndex);
            this.totalDurationMs = Math.max(0, totalDurationMs);
            this.tracks = tracks != null ? tracks : new ArrayList<>();
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
    private boolean owning = false;
    private final Runnable tickRunnable = this::tickPosition;

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
        return player != null && player.isPlaying();
    }

    @Nullable
    public String getMediaId() {
        return mediaId.isEmpty() ? null : mediaId;
    }

    public long getPositionMs() {
        if (player == null) {
            return 0;
        }
        int idx = Math.max(0, player.getCurrentMediaItemIndex());
        long local = Math.max(0, player.getCurrentPosition());
        // Prefer playlist-relative if tracks carry offsets via window.
        return local + trackStartOffsetMs(idx);
    }

    private long trackStartOffsetMs(int idx) {
        // Stored on MediaItem tag when prepared.
        if (player == null) {
            return 0;
        }
        MediaItem item = player.getCurrentMediaItem();
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
            Log.i(TAG, "Handed off to WebView at posMs=" + pos);
        });
    }

    public void seekTo(long positionMs) {
        mainHandler.post(() -> {
            if (player == null || !owning) {
                return;
            }
            // Seek within current window for simplicity; chapter skip still goes via JS/native bridge.
            long local = Math.max(0, positionMs - trackStartOffsetMs(player.getCurrentMediaItemIndex()));
            player.seekTo(local);
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
            .setUserAgent("LibraryAndroidAuto/1.52");
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
        long pos = 0;
        int trackIndex = 0;
        if (player != null) {
            trackIndex = Math.max(0, player.getCurrentMediaItemIndex());
            pos = Math.max(0, player.getCurrentPosition()) + trackStartOffsetMs(trackIndex);
        }
        float speed = player != null ? player.getPlaybackParameters().speed : 1f;
        long dur = totalDurationMs;
        if (dur <= 0 && player != null && player.getDuration() > 0) {
            dur = player.getDuration() + trackStartOffsetMs(trackIndex);
        }
        for (Listener l : copyListeners()) {
            l.onNativePlaying(
                mediaId,
                title,
                artist,
                album,
                coverUrl,
                playing,
                dur,
                pos,
                speed > 0 ? speed : 1f,
                trackIndex
            );
        }
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
        return new Playable(
            mediaId,
            o.optString("title", ""),
            o.optString("author", ""),
            o.optString("coverUrl", ""),
            o.optString("authToken", ""),
            Math.round(o.optDouble("position", 0) * 1000.0),
            o.optInt("trackIndex", 0),
            Math.round(o.optDouble("totalDuration", 0) * 1000.0),
            tracks
        );
    }
}
