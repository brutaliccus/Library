package com.freiverse.library;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;

public final class AppUpdateNotifier {

    public static final String CHANNEL_ID = "library_app_update_v1";
    public static final int NOTIFICATION_ID = 41_001;

    private AppUpdateNotifier() {}

    public static void ensureChannel(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationChannel channel = new NotificationChannel(
            CHANNEL_ID,
            "App updates",
            NotificationManager.IMPORTANCE_DEFAULT
        );
        channel.setDescription("Notifies when a new Library APK is available");
        NotificationManager nm = context.getSystemService(NotificationManager.class);
        if (nm != null) nm.createNotificationChannel(channel);
    }

    public static void show(
        Context context,
        String title,
        String body,
        String releaseKey,
        String downloadUrl,
        String authToken,
        boolean required
    ) {
        if (AppUpdatePlugin.isInForeground()) {
            return;
        }

        ensureChannel(context);
        AppUpdatePendingStore.save(context, releaseKey, downloadUrl, authToken, title, body);

        int pendingFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            pendingFlags |= PendingIntent.FLAG_IMMUTABLE;
        }

        Intent updateIntent = new Intent(context, AppUpdateActionReceiver.class);
        updateIntent.setAction(AppUpdateActionReceiver.ACTION_UPDATE_NOW);
        PendingIntent updatePending =
            PendingIntent.getBroadcast(context, NOTIFICATION_ID, updateIntent, pendingFlags);

        Intent openIntent = context.getPackageManager().getLaunchIntentForPackage(context.getPackageName());
        if (openIntent != null) {
            openIntent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
            openIntent.putExtra("url", "/settings");
        }
        PendingIntent contentPending =
            openIntent != null
                ? PendingIntent.getActivity(context, NOTIFICATION_ID + 2, openIntent, pendingFlags)
                : null;

        NotificationCompat.Builder builder = new NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_notification)
            .setContentTitle(title != null ? title : "Update available")
            .setContentText(body != null ? body : "A new version of Library is ready.")
            .setStyle(
                new NotificationCompat.BigTextStyle()
                    .bigText(body != null ? body : "A new version of Library is ready.")
            )
            .setCategory(
                required
                    ? NotificationCompat.CATEGORY_ALARM
                    : NotificationCompat.CATEGORY_RECOMMENDATION
            )
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setPriority(
                required
                    ? NotificationCompat.PRIORITY_HIGH
                    : NotificationCompat.PRIORITY_DEFAULT
            )
            .setOngoing(required)
            .setAutoCancel(!required)
            .addAction(0, required ? "Update now" : "Update now", updatePending);

        if (!required) {
            Intent dismissIntent = new Intent(context, AppUpdateActionReceiver.class);
            dismissIntent.setAction(AppUpdateActionReceiver.ACTION_DISMISS);
            dismissIntent.putExtra(AppUpdateActionReceiver.EXTRA_RELEASE_KEY, releaseKey);
            PendingIntent dismissPending =
                PendingIntent.getBroadcast(context, NOTIFICATION_ID + 1, dismissIntent, pendingFlags);
            builder.setDeleteIntent(dismissPending);
            builder.addAction(0, "Dismiss", dismissPending);
        }

        if (contentPending != null) {
            builder.setContentIntent(contentPending);
        }

        NotificationManagerCompat.from(context).notify(NOTIFICATION_ID, builder.build());
    }

    /** Soft (dismissible) update notification. */
    public static void show(
        Context context,
        String title,
        String body,
        String releaseKey,
        String downloadUrl,
        String authToken
    ) {
        show(context, title, body, releaseKey, downloadUrl, authToken, false);
    }

    public static void dismiss(Context context) {
        NotificationManagerCompat.from(context).cancel(NOTIFICATION_ID);
    }
}
