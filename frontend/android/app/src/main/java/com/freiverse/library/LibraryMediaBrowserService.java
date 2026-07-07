package com.freiverse.library;

import android.content.Intent;
import android.os.Bundle;
import android.support.v4.media.MediaBrowserCompat;
import android.support.v4.media.session.MediaSessionCompat;
import androidx.annotation.NonNull;
import androidx.annotation.Nullable;
import androidx.media.MediaBrowserServiceCompat;
import androidx.media.session.MediaButtonReceiver;
import java.util.List;

/** Android Auto entry point — browse tree + media session controls. */
public class LibraryMediaBrowserService extends MediaBrowserServiceCompat {

    private MediaSessionCompat mediaSession;

    @Override
    public void onCreate() {
        super.onCreate();

        mediaSession = new MediaSessionCompat(this, "LibraryAuto");
        mediaSession.setFlags(
            MediaSessionCompat.FLAG_HANDLES_MEDIA_BUTTONS
                | MediaSessionCompat.FLAG_HANDLES_TRANSPORT_CONTROLS
        );
        mediaSession.setCallback(new MediaSessionCompat.Callback() {
            @Override
            public void onPlay() {
                // Flip the session state immediately so the Android Auto button
                // updates without waiting for the (possibly throttled) WebView
                // round-trip. The next syncPlayback from JS confirms/corrects it.
                LibraryAutoBridge.getInstance().setPlayingOptimistic(true);
                LibraryAutoBridge.getInstance().dispatch("play", null);
            }

            @Override
            public void onPause() {
                LibraryAutoBridge.getInstance().setPlayingOptimistic(false);
                LibraryAutoBridge.getInstance().dispatch("pause", null);
            }

            @Override
            public void onStop() {
                LibraryAutoBridge.getInstance().dispatch("stop", null);
            }

            @Override
            public void onSkipToNext() {
                LibraryAutoBridge.getInstance().dispatch("nexttrack", null);
            }

            @Override
            public void onSkipToPrevious() {
                LibraryAutoBridge.getInstance().dispatch("previoustrack", null);
            }

            @Override
            public void onSeekTo(long pos) {
                Bundle extras = new Bundle();
                extras.putLong("seekTimeMs", pos);
                LibraryAutoBridge.getInstance().dispatch("seekto", extras);
            }

            @Override
            public void onFastForward() {
                LibraryAutoBridge.getInstance().dispatch("seekforward", null);
            }

            @Override
            public void onRewind() {
                LibraryAutoBridge.getInstance().dispatch("seekbackward", null);
            }

            @Override
            public void onPlayFromMediaId(String mediaId, Bundle extras) {
                Bundle payload = new Bundle();
                payload.putString("mediaId", mediaId);
                LibraryAutoBridge.getInstance().dispatch("playmedia", payload);
            }
        });

        setSessionToken(mediaSession.getSessionToken());
        LibraryAutoBridge.getInstance().attach(this, mediaSession);
    }

    @Override
    public void onDestroy() {
        if (mediaSession != null) {
            mediaSession.setActive(false);
            mediaSession.release();
            mediaSession = null;
        }
        super.onDestroy();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (mediaSession != null) {
            MediaButtonReceiver.handleIntent(mediaSession, intent);
        }
        return START_STICKY;
    }

    @Nullable
    @Override
    public BrowserRoot onGetRoot(
        @NonNull String clientPackageName,
        int clientUid,
        @Nullable Bundle rootHints
    ) {
        return new BrowserRoot(LibraryAutoBridge.MEDIA_ROOT_ID, null);
    }

    @Override
    public void onLoadChildren(
        @NonNull String parentId,
        @NonNull Result<List<MediaBrowserCompat.MediaItem>> result
    ) {
        LibraryAutoBridge bridge = LibraryAutoBridge.getInstance();
        if (bridge.isStaticParent(parentId)) {
            result.sendResult(bridge.buildRootChildren());
            return;
        }
        bridge.requestBrowseChildren(parentId, result);
    }

    void notifyRootChildrenChanged() {
        notifyChildrenChanged(LibraryAutoBridge.MEDIA_ROOT_ID);
    }
}
