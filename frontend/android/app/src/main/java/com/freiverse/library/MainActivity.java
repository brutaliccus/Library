package com.freiverse.library;

import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.webkit.WebView;
import androidx.core.content.pm.PackageInfoCompat;
import com.getcapacitor.Bridge;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    private static final String PREFS = "library_webview_cache";
    private static final String KEY_VERSION_CODE = "cleared_for_version_code";

    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(LibraryAutoPlugin.class);
        registerPlugin(AppUpdatePlugin.class);
        registerPlugin(ThemeIconPlugin.class);
        // Heal aliases disabled by older builds; do not switch launcher icons.
        ThemeIconHelper.ensureSafeAliases(this);
        super.onCreate(savedInstanceState);
        clearWebViewCacheOnVersionChange();
    }

    @Override
    public void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        // AA / lock-screen soft-wake: thaw timers even if the activity was only
        // reordered to front without a full cold start.
        if (intent != null && intent.getBooleanExtra("library_media_resume", false)) {
            resumeWebViewTimers();
        }
    }

    @Override
    public void onResume() {
        super.onResume();
        resumeWebViewTimers();
        clearWebViewCacheOnVersionChange();
    }

    /**
     * After an APK install, Chromium may keep stale bundled SPA assets. Clear
     * once per versionCode so the new webDir contents load.
     */
    private void clearWebViewCacheOnVersionChange() {
        try {
            long code = PackageInfoCompat.getLongVersionCode(
                getPackageManager().getPackageInfo(getPackageName(), 0)
            );
            SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
            long prev = prefs.getLong(KEY_VERSION_CODE, -1L);
            if (prev == code) {
                return;
            }
            Bridge bridge = getBridge();
            WebView webView = bridge != null ? bridge.getWebView() : null;
            if (webView != null) {
                webView.clearCache(true);
            }
            prefs.edit().putLong(KEY_VERSION_CODE, code).apply();
        } catch (Exception ignored) {
            /* package info / bridge not ready */
        }
    }

    private void resumeWebViewTimers() {
        try {
            Bridge bridge = getBridge();
            if (bridge == null) {
                return;
            }
            WebView webView = bridge.getWebView();
            if (webView == null) {
                return;
            }
            webView.onResume();
            webView.resumeTimers();
        } catch (Exception ignored) {
            /* bridge not ready yet */
        }
    }
}
