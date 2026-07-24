package com.freiverse.library;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
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
    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    @Override
    public void onCreate() {
        super.onCreate();

        mediaSession = new MediaSessionCompat(this, "LibraryAuto");
        mediaSession.setFlags(
            MediaSessionCompat.FLAG_HANDLES_MEDIA_BUTTONS
                | MediaSessionCompat.FLAG_HANDLES_TRANSPORT_CONTROLS
        );
        mediaSession.setSessionActivity(LibraryAutoBridge.sessionActivityIntent(this));
        mediaSession.setCallback(new MediaSessionCompat.Callback() {
            @Override
            public void onPlay() {
                LibraryAutoBridge bridge = LibraryAutoBridge.getInstance();
                bridge.requestAudioFocusForPlay();
                // Flip the session state immediately so the Android Auto button
                // updates without waiting for the (possibly throttled) WebView
                // round-trip. The next syncPlayback from JS confirms/corrects it.
                bridge.setPlayingOptimistic(true);
                bridge.dispatch("play", null);
            }

            @Override
            public void onPause() {
                LibraryAutoBridge bridge = LibraryAutoBridge.getInstance();
                bridge.setPlayingOptimistic(false);
                // Keep audio focus so a subsequent play from AA/lock screen
                // doesn't race a fresh focus request while the WebView wakes.
                bridge.dispatch("pause", null);
            }

            @Override
            public void onStop() {
                LibraryAutoBridge.getInstance().abandonAudioFocus();
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
                LibraryAutoBridge.getInstance().seekRelativeAndDispatch(
                    LibraryAutoBridge.SKIP_MS,
                    "seekforward"
                );
            }

            @Override
            public void onRewind() {
                LibraryAutoBridge.getInstance().seekRelativeAndDispatch(
                    -LibraryAutoBridge.SKIP_MS,
                    "seekbackward"
                );
            }

            @Override
            public void onCustomAction(String action, Bundle extras) {
                if (LibraryAutoBridge.CUSTOM_REWIND_15.equals(action)) {
                    onRewind();
                } else if (LibraryAutoBridge.CUSTOM_FORWARD_15.equals(action)) {
                    onFastForward();
                }
            }

            @Override
            public void onPlayFromMediaId(String mediaId, Bundle extras) {
                LibraryAutoBridge bridge = LibraryAutoBridge.getInstance();
                bridge.requestAudioFocusForPlay();
                // Optimistic play so AA shows playing while the book loads.
                if (bridge.isActive()) {
                    bridge.setPlayingOptimistic(true);
                }
                Bundle payload = new Bundle();
                payload.putString("mediaId", mediaId);
                bridge.dispatch("playmedia", payload);
            }
        });

        setSessionToken(mediaSession.getSessionToken());
        LibraryAutoBridge.getInstance().attach(this, mediaSession);
        createNotificationChannel();

        // If we restored a paused session after process death, show Now Playing in AA.
        if (LibraryAutoBridge.getInstance().isActive()) {
            mainHandler.post(this::promoteToForeground);
        }
    }

    @Override
    public void onDestroy() {
        stopForegroundPlayback();
        LibraryAutoBridge.getInstance().abandonAudioFocus();
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

        PendingIntent contentIntent = LibraryAutoBridge.sessionActivityIntent(this);

        // Compact: −15 | play/pause | +15 (audiobook-first). Expanded also has chapter skip.
        MediaStyle style = new MediaStyle()
            .setMediaSession(mediaSession.getSessionToken())
            .setShowActionsInCompactView(0, 2, 4)
            .setShowCancelButton(true)
            .setCancelButtonIntent(
                MediaButtonReceiver.buildMediaButtonPendingIntent(
                    this,
                    PlaybackStateCompat.ACTION_STOP
                )
            );

        NotificationCompat.Builder builder = new NotificationCompat.Builder(this, CHANNEL_ID)
            // Headphones — never the notification bell (AA may fall back to smallIcon
            // when a custom ±15 vector fails to render on some head units).
            .setSmallIcon(R.drawable.ic_stat_media)
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
            R.drawable.ic_media_rewind_15,
            "-15 seconds",
            MediaButtonReceiver.buildMediaButtonPendingIntent(
                this,
                PlaybackStateCompat.ACTION_REWIND
            )
        );

        builder.addAction(
            R.drawable.ic_media_skip_prev,
            "Previous chapter",
            MediaButtonReceiver.buildMediaButtonPendingIntent(
                this,
                PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS
            )
        );

        if (bridge.isPlaying()) {
            builder.addAction(
                R.drawable.ic_media_pause,
                "Pause",
                MediaButtonReceiver.buildMediaButtonPendingIntent(
                    this,
                    PlaybackStateCompat.ACTION_PAUSE
                )
            );
        } else {
            builder.addAction(
                R.drawable.ic_media_play,
                "Play",
                MediaButtonReceiver.buildMediaButtonPendingIntent(
                    this,
                    PlaybackStateCompat.ACTION_PLAY
                )
            );
        }

        builder.addAction(
            R.drawable.ic_media_skip_next,
            "Next chapter",
            MediaButtonReceiver.buildMediaButtonPendingIntent(
                this,
                PlaybackStateCompat.ACTION_SKIP_TO_NEXT
            )
        );

        builder.addAction(
            R.drawable.ic_media_forward_15,
            "+15 seconds",
            MediaButtonReceiver.buildMediaButtonPendingIntent(
                this,
                PlaybackStateCompat.ACTION_FAST_FORWARD
            )
        );

        return builder.build();
    }
}
