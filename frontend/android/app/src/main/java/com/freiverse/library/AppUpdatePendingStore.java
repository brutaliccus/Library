package com.freiverse.library;

import android.content.Context;
import android.content.SharedPreferences;

public final class AppUpdatePendingStore {

    private static final String PREFS = "library_app_update_pending";

    private AppUpdatePendingStore() {}

    public static void save(
        Context context,
        String releaseKey,
        String downloadUrl,
        String authToken,
        String title,
        String body
    ) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString("releaseKey", releaseKey != null ? releaseKey : "")
            .putString("downloadUrl", downloadUrl != null ? downloadUrl : "")
            .putString("authToken", authToken != null ? authToken : "")
            .putString("title", title != null ? title : "")
            .putString("body", body != null ? body : "")
            .apply();
    }

    public static String getReleaseKey(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString("releaseKey", "");
    }

    public static String getDownloadUrl(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString("downloadUrl", "");
    }

    public static String getAuthToken(Context context) {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString("authToken", "");
    }

    public static void clear(Context context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().clear().apply();
    }
}
