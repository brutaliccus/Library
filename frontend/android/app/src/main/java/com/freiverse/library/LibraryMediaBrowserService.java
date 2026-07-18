package com.freiverse.library;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Bundle;
import android.support.v4.media.MediaBrowserCompat;
import android.support.v4.media.session.MediaSessionCompat;
import android.support.v4.media.session.PlaybackStateCompat;
import androidx.annotation.NonNull;
import androidx.annotation.Nullable;
import androidx.core.app.NotificationCompat;
import androidx.media.MediaBrowserServiceCompat;
import androidx.media.app.NotificationCompat.MediaStyle;
import androidx.media.session.MediaButtonReceiver;
import java.util.List;

/** Android Auto entry point — browse tree + media session + lock-screen controls. */
public class LibraryMediaBrowserService extends MediaBrowserServiceCompat {

    private static final String CHANNEL_ID = "library_playback";
    private static final int NOTIFICATION_ID = 1001;

    private MediaSessionCompat mediaSession;
    private boolean foregroundActive = false;

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
        createNotificationChannel();
    }

    @Override
    public void onDestroy() {
        stopForegroundPlayback();
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
        if (LibraryAutoBridge.getInstance().isActive()) {
            promoteToForeground();
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

    /** Foreground service + media notification — required for lock-screen transport controls. */
    void promoteToForeground() {
        if (mediaSession == null) {
            return;
        }
        Notification notification = buildNotification();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
            );
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }
        foregroundActive = true;
    }

    void updateForegroundNotification() {
        if (!foregroundActive || mediaSession == null) {
            return;
        }
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) {
            nm.notify(NOTIFICATION_ID, buildNotification());
        }
    }

    void stopForegroundPlayback() {
        if (!foregroundActive) {
            return;
        }
        stopForeground(STOP_FOREGROUND_REMOVE);
        foregroundActive = false;
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }
        NotificationChannel channel = new NotificationChannel(
            CHANNEL_ID,
            "Audiobook playback",
            NotificationManager.IMPORTANCE_LOW
        );
        channel.setDescription("Now playing controls");
        channel.setShowBadge(false);
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) {
            nm.createNotificationChannel(channel);
        }
    }

    private Notification buildNotification() {
        LibraryAutoBridge bridge = LibraryAutoBridge.getInstance();
        String title = bridge.getTitle();
        String artist = bridge.getArtist();
        if (title.isEmpty()) {
            title = "Library";
        }

        Intent launchIntent = new Intent(this, MainActivity.class);
        launchIntent.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent contentIntent = PendingIntent.getActivity(
            this,
            0,
            launchIntent,
            PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT
        );

        MediaStyle style = new MediaStyle()
            .setMediaSession(mediaSession.getSessionToken())
            .setShowActionsInCompactView(0, 1, 2);

        NotificationCompat.Builder builder = new NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_notification)
            .setContentTitle(title)
            .setContentText(artist)
            .setLargeIcon(bridge.getArtwork())
            .setContentIntent(contentIntent)
            .setStyle(style)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setOnlyAlertOnce(true)
            .setOngoing(true)
            .setShowWhen(false);

        builder.addAction(
            R.drawable.ic_stat_notification,
            "Previous",
            MediaButtonReceiver.buildMediaButtonPendingIntent(
                this,
                PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS
            )
        );

        if (bridge.isPlaying()) {
            builder.addAction(
                R.drawable.ic_stat_notification,
                "Pause",
                MediaButtonReceiver.buildMediaButtonPendingIntent(
                    this,
                    PlaybackStateCompat.ACTION_PAUSE
                )
            );
        } else {
            builder.addAction(
                R.drawable.ic_stat_notification,
                "Play",
                MediaButtonReceiver.buildMediaButtonPendingIntent(
                    this,
                    PlaybackStateCompat.ACTION_PLAY
                )
            );
        }

        builder.addAction(
            R.drawable.ic_stat_notification,
            "Next",
            MediaButtonReceiver.buildMediaButtonPendingIntent(
                this,
                PlaybackStateCompat.ACTION_SKIP_TO_NEXT
            )
        );

        return builder.build();
    }
}
