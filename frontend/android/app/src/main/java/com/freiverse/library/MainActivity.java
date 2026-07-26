package com.freiverse.library;

import android.content.Intent;
import android.os.Bundle;
import android.webkit.WebView;
import com.getcapacitor.Bridge;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(LibraryAutoPlugin.class);
        registerPlugin(AppUpdatePlugin.class);
        registerPlugin(ThemeIconPlugin.class);
        // Heal aliases disabled by older builds; do not switch launcher icons.
        ThemeIconHelper.ensureSafeAliases(this);
        super.onCreate(savedInstanceState);
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
