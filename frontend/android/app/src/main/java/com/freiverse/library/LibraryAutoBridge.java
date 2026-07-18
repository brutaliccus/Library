package com.freiverse.library;

import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.support.v4.media.MediaBrowserCompat;
import android.support.v4.media.MediaDescriptionCompat;
import android.support.v4.media.MediaMetadataCompat;
import android.support.v4.media.session.MediaSessionCompat;
import android.support.v4.media.session.PlaybackStateCompat;
import androidx.annotation.Nullable;
import androidx.media.MediaBrowserServiceCompat;
import java.lang.ref.WeakReference;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/** Shared playback + browse state between the Capacitor web layer and Android Auto. */
public final class LibraryAutoBridge {

    public static final String MEDIA_ROOT_ID = "library_root";
    public static final String CONTINUE_ID = "continue";
    public static final String LIBRARY_ID = "library";
    public static final String NOW_PLAYING_ID = "now_playing";

    private static final long BROWSE_TIMEOUT_MS = 12_000;

    public interface ActionListener {
        void onAction(String action, @Nullable Bundle extras);
    }

    public interface BrowseRequestEmitter {
        void emitBrowseRequest(String parentId, String requestId);
    }

    private static final class PendingBrowse {

        final MediaBrowserServiceCompat.Result<List<MediaBrowserCompat.MediaItem>> result;
        final Runnable timeout;

        PendingBrowse(
            MediaBrowserServiceCompat.Result<List<MediaBrowserCompat.MediaItem>> result,
            Runnable timeout
        ) {
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

    private LibraryAutoBridge() {}

    public void attach(LibraryMediaBrowserService service, MediaSessionCompat session) {
        serviceRef = new WeakReference<>(service);
        sessionRef = new WeakReference<>(session);
        refreshSession(true);
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
        this.playing = playing;
        this.durationMs = Math.max(0, durationMs);
        this.positionMs = Math.max(0, positionMs);
        this.playbackSpeed = playbackSpeed > 0 ? playbackSpeed : 1.0f;

        boolean rootChanged =
            wasActive != active || !previousRootKey.equals(nowPlayingRootKey());
        refreshSession(true);
        if (rootChanged) {
            notifyRootChanged();
        }
    }

    /** Position / transport-only sync — avoids rebuilding browse tree artwork. */
    public void updatePosition(boolean playing, long positionMs, float playbackSpeed) {
        this.playing = playing;
        this.positionMs = Math.max(0, positionMs);
        this.playbackSpeed = playbackSpeed > 0 ? playbackSpeed : 1.0f;
        refreshSession(false);
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
        refreshSession(false);
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
        update("", "", "", null, false, false, 0, 0, 1.0f);
    }

    public void dispatch(String action, @Nullable Bundle extras) {
        for (ActionListener listener : new ArrayList<>(listeners)) {
            listener.onAction(action, extras);
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

    public void requestBrowseChildren(
        String parentId,
        MediaBrowserServiceCompat.Result<List<MediaBrowserCompat.MediaItem>> result
    ) {
        result.detach();
        final String requestId = UUID.randomUUID().toString();

        Runnable timeout = () -> {
            PendingBrowse pending = pendingBrowses.remove(requestId);
            if (pending != null) {
                pending.result.sendResult(new ArrayList<>());
            }
        };
        mainHandler.postDelayed(timeout, BROWSE_TIMEOUT_MS);
        pendingBrowses.put(requestId, new PendingBrowse(result, timeout));

        BrowseRequestEmitter emitter = emitterRef.get();
        if (emitter != null) {
            emitter.emitBrowseRequest(parentId, requestId);
        } else {
            timeout.run();
        }
    }

    public void resolveBrowseChildren(String requestId, List<AutoBrowseNode> nodes) {
        PendingBrowse pending = pendingBrowses.remove(requestId);
        if (pending == null) {
            return;
        }
        mainHandler.removeCallbacks(pending.timeout);
        pending.result.sendResult(toMediaItems(nodes));
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
                | PlaybackStateCompat.ACTION_STOP;

        int state = playing
            ? PlaybackStateCompat.STATE_PLAYING
            : (active ? PlaybackStateCompat.STATE_PAUSED : PlaybackStateCompat.STATE_NONE);

        PlaybackStateCompat playbackState = new PlaybackStateCompat.Builder()
            .setActions(actions)
            .setState(state, positionMs, playbackSpeed)
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
            MediaMetadataCompat metadata = new MediaMetadataCompat.Builder()
                .putString(MediaMetadataCompat.METADATA_KEY_TITLE, title)
                .putString(MediaMetadataCompat.METADATA_KEY_ARTIST, artist)
                .putString(MediaMetadataCompat.METADATA_KEY_ALBUM, album)
                .putLong(MediaMetadataCompat.METADATA_KEY_DURATION, durationMs)
                .putBitmap(MediaMetadataCompat.METADATA_KEY_ALBUM_ART, artwork)
                .build();
            session.setMetadata(metadata);
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
}
