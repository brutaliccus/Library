package com.freiverse.library;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

public class AppUpdateActionReceiver extends BroadcastReceiver {

    public static final String ACTION_UPDATE_NOW = "com.freiverse.library.APP_UPDATE_NOW";
    public static final String ACTION_DISMISS = "com.freiverse.library.APP_UPDATE_DISMISS";
    public static final String EXTRA_RELEASE_KEY = "releaseKey";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null) return;
        String action = intent.getAction();
        if (ACTION_DISMISS.equals(action)) {
            String releaseKey = intent.getStringExtra(EXTRA_RELEASE_KEY);
            if (releaseKey == null || releaseKey.isEmpty()) {
                releaseKey = AppUpdatePendingStore.getReleaseKey(context);
            }
            AppUpdateNotifier.dismiss(context);
            AppUpdatePendingStore.clear(context);
            AppUpdatePlugin.dispatchDismissed(context, releaseKey);
            return;
        }
        if (ACTION_UPDATE_NOW.equals(action)) {
            AppUpdateNotifier.dismiss(context);
            Intent launch = context.getPackageManager().getLaunchIntentForPackage(context.getPackageName());
            if (launch == null) return;
            launch.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            launch.putExtra("library_pending_app_update", true);
            context.startActivity(launch);
            AppUpdatePlugin.requestJsDownload(context);
        }
    }
}
