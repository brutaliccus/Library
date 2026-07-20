package com.freiverse.library;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(LibraryAutoPlugin.class);
        registerPlugin(AppUpdatePlugin.class);
        registerPlugin(ThemeIconPlugin.class);
        // Re-apply saved themed launcher / Android Auto icons on cold start.
        ThemeIconHelper.apply(this, ThemeIconHelper.getSavedTheme(this));
        super.onCreate(savedInstanceState);
    }
}
