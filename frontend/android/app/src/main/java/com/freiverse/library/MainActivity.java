package com.freiverse.library;

import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.Window;
import android.webkit.WebView;
import androidx.core.content.pm.PackageInfoCompat;
import androidx.core.splashscreen.SplashScreen;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsControllerCompat;
import com.getcapacitor.Bridge;
import com.getcapacitor.BridgeActivity;
import java.io.File;

public class MainActivity extends BridgeActivity {

    private static final String PREFS = "library_webview_cache";
    private static final String KEY_VERSION_CODE = "cleared_for_version_code";
    /** Matches capacitor.config.ts android.backgroundColor / gray-950. */
    private static final int APP_BACKGROUND = Color.parseColor("#030712");

    @Override
    public void onCreate(Bundle savedInstanceState) {
        // Must run before super.onCreate so Android 12+ hands off the system
        // splash cleanly into AppTheme.NoActionBar (see styles.xml).
        SplashScreen.installSplashScreen(this);
        applyDarkSystemBars();

        registerPlugin(LibraryAutoPlugin.class);
        registerPlugin(AppUpdatePlugin.class);
        registerPlugin(ThemeIconPlugin.class);
        // Heal aliases disabled by older builds; do not switch launcher icons.
        ThemeIconHelper.ensureSafeAliases(this);
        // Bust HTTP/resource caches BEFORE Bridge loads the SPA so new
        // android_asset files win — without wiping localStorage / IndexedDB.
        boolean refreshed = refreshWebAssetCacheIfVersionChanged(/* allowMark */ false);
        super.onCreate(savedInstanceState);
        applyDarkSystemBars();
        paintWebViewBackground();
        if (refreshed) {
            // Cache was cleared before Bridge created the WebView, so the first
            // load already sees fresh assets. Do NOT webView.reload() here —
            // a post-splash reload left white status/nav bars after APK updates.
            markWebViewCacheCleared();
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
        applyDarkSystemBars();
        resumeWebViewTimers();
        clearWebViewCacheOnVersionChange();
    }

    /**
     * After an APK install, Chromium may keep stale bundled SPA assets.
     * Clear HTTP/resource cache once per versionCode — keep localStorage
     * (login), IndexedDB (offline books), and cookies intact.
     */
    private void clearWebViewCacheOnVersionChange() {
        if (refreshWebAssetCacheIfVersionChanged(/* allowMark */ true)) {
            // Only reload when Bridge already loaded (e.g. late detection in
            // onResume). Paint dark chrome first so bars never flash white.
            applyDarkSystemBars();
            paintWebViewBackground();
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
                webView.setBackgroundColor(APP_BACKGROUND);
                webView.reload();
            }
        } catch (Exception ignored) {
            /* bridge not ready */
        }
    }

    private void paintWebViewBackground() {
        try {
            Bridge bridge = getBridge();
            WebView webView = bridge != null ? bridge.getWebView() : null;
            if (webView != null) {
                webView.setBackgroundColor(APP_BACKGROUND);
            }
            Window window = getWindow();
            if (window != null) {
                View decor = window.getDecorView();
                decor.setBackgroundColor(APP_BACKGROUND);
            }
        } catch (Exception ignored) {
            /* bridge not ready */
        }
    }

    /**
     * Force dark status + navigation bars immediately. Theme.SplashScreen and
     * DayNight defaults otherwise leave stock white system chrome after an
     * APK-update launch until the activity is fully restarted.
     */
    private void applyDarkSystemBars() {
        try {
            Window window = getWindow();
            if (window == null) {
                return;
            }
            window.setStatusBarColor(APP_BACKGROUND);
            window.setNavigationBarColor(APP_BACKGROUND);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                window.setNavigationBarDividerColor(APP_BACKGROUND);
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                window.setNavigationBarContrastEnforced(false);
                window.setStatusBarContrastEnforced(false);
            }
            WindowCompat.setDecorFitsSystemWindows(window, true);
            WindowInsetsControllerCompat insets =
                WindowCompat.getInsetsController(window, window.getDecorView());
            if (insets != null) {
                insets.setAppearanceLightStatusBars(false);
                insets.setAppearanceLightNavigationBars(false);
            }
        } catch (Exception ignored) {
            /* window not ready */
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
