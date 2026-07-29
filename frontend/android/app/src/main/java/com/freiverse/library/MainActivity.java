package com.freiverse.library;

import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.webkit.WebView;
import androidx.core.content.pm.PackageInfoCompat;
import com.getcapacitor.Bridge;
import com.getcapacitor.BridgeActivity;
import java.io.File;

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
        // Bust HTTP/resource caches BEFORE Bridge loads the SPA so new
        // android_asset files win — without wiping localStorage / IndexedDB.
        boolean refreshed = refreshWebAssetCacheIfVersionChanged(/* allowMark */ false);
        super.onCreate(savedInstanceState);
        if (refreshed) {
            markWebViewCacheCleared();
            reloadBridgeWebView();
        } else {
            clearWebViewCacheOnVersionChange();
        }
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
     * After an APK install, Chromium may keep stale bundled SPA assets.
     * Clear HTTP/resource cache once per versionCode and reload — keep
     * localStorage (login), IndexedDB (offline books), and cookies intact.
     */
    private void clearWebViewCacheOnVersionChange() {
        if (refreshWebAssetCacheIfVersionChanged(/* allowMark */ true)) {
            reloadBridgeWebView();
        }
    }

    /** @return true if a cache refresh ran for a new versionCode */
    private boolean refreshWebAssetCacheIfVersionChanged(boolean allowMark) {
        try {
            long code = currentVersionCode();
            SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
            long prev = prefs.getLong(KEY_VERSION_CODE, -1L);
            if (prev == code) {
                return false;
            }
            refreshWebAssetCache();
            if (allowMark) {
                prefs.edit().putLong(KEY_VERSION_CODE, code).apply();
            }
            return true;
        } catch (Exception ignored) {
            /* package info / webview not ready */
            return false;
        }
    }

    private void markWebViewCacheCleared() {
        try {
            long code = currentVersionCode();
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().putLong(KEY_VERSION_CODE, code).apply();
        } catch (Exception ignored) {
            /* ignore */
        }
    }

    private long currentVersionCode() throws Exception {
        return PackageInfoCompat.getLongVersionCode(
            getPackageManager().getPackageInfo(getPackageName(), 0)
        );
    }

    private void refreshWebAssetCache() {
        // Temporary WebView works before Bridge exists (pre-super.onCreate).
        try {
            WebView probe = new WebView(this);
            probe.clearCache(true);
            probe.destroy();
        } catch (Exception ignored) {
            /* WebView provider may not be ready */
        }
        try {
            Bridge bridge = getBridge();
            WebView webView = bridge != null ? bridge.getWebView() : null;
            if (webView != null) {
                webView.clearCache(true);
            }
        } catch (Exception ignored) {
            /* bridge not ready */
        }
        // Drop Chromium HTTP cache dir only — never delete WebView profile /
        // Local Storage / IndexedDB under app_webview or files/WebView.
        deleteDir(new File(getCacheDir(), "WebView"));
        deleteDir(new File(getCacheDir(), "org.chromium.android_webview"));
    }

    private void reloadBridgeWebView() {
        try {
            Bridge bridge = getBridge();
            WebView webView = bridge != null ? bridge.getWebView() : null;
            if (webView != null) {
                webView.reload();
            }
        } catch (Exception ignored) {
            /* bridge not ready */
        }
    }

    private static void deleteDir(File dir) {
        if (dir == null || !dir.exists()) {
            return;
        }
        File[] children = dir.listFiles();
        if (children != null) {
            for (File child : children) {
                if (child.isDirectory()) {
                    deleteDir(child);
                } else {
                    //noinspection ResultOfMethodCallIgnored
                    child.delete();
                }
            }
        }
        //noinspection ResultOfMethodCallIgnored
        dir.delete();
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
