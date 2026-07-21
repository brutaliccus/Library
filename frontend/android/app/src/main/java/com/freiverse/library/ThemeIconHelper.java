package com.freiverse.library;

import android.content.ComponentName;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.util.Log;

/**
 * Persists the preferred theme for Android Auto service selection.
 *
 * Launcher activity-alias switching via PackageManager was killing the Capacitor
 * WebView process on theme change (DONT_KILL_APP is not reliable when disabling
 * the active launcher). Theme changes now only update in-app CSS + web favicons;
 * the home-screen icon stays on the default aliases.
 */
public final class ThemeIconHelper {
    private static final String TAG = "ThemeIconHelper";
    private static final String PREFS = "library_theme_icon";
    private static final String KEY_THEME = "theme";
    private static final String KEY_HEALED = "aliases_healed_v1";
    public static final String DEFAULT_THEME = "ocean";
    private static final String[] THEMES = { "ocean", "ember", "forest", "dusk" };

    private ThemeIconHelper() {}

    public static String normalize(String theme) {
        if (theme == null) return DEFAULT_THEME;
        String t = theme.trim().toLowerCase();
        for (String known : THEMES) {
            if (known.equals(t)) return t;
        }
        return DEFAULT_THEME;
    }

    public static String getSavedTheme(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        return normalize(prefs.getString(KEY_THEME, DEFAULT_THEME));
    }

    public static Class<?> mediaBrowserServiceClass(String theme) {
        switch (normalize(theme)) {
            case "ember":
                return LibraryMediaBrowserServiceEmber.class;
            case "forest":
                return LibraryMediaBrowserServiceForest.class;
            case "dusk":
                return LibraryMediaBrowserServiceDusk.class;
            case "ocean":
            default:
                return LibraryMediaBrowserServiceOcean.class;
        }
    }

    /**
     * Persist theme preference only — do not toggle launcher aliases.
     * Also one-time heal: re-enable all aliases disabled by older builds.
     */
    public static void apply(Context context, String themeRaw) {
        String theme = normalize(themeRaw);
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        prefs.edit().putString(KEY_THEME, theme).apply();
        healAliasesOnce(context, prefs);
        Log.i(TAG, "Theme preference saved (launcher icon switch disabled): " + theme);
    }

    /** Cold start: heal aliases if needed; never disable components. */
    public static void ensureSafeAliases(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        healAliasesOnce(context, prefs);
    }

    private static void healAliasesOnce(Context context, SharedPreferences prefs) {
        if (prefs.getBoolean(KEY_HEALED, false)) return;
        PackageManager pm = context.getPackageManager();
        String pkg = context.getPackageName();
        for (String t : THEMES) {
            setEnabled(pm, new ComponentName(pkg, pkg + ".Launcher" + capitalize(t)), true);
            setEnabled(pm, new ComponentName(context, mediaBrowserServiceClass(t)), true);
        }
        prefs.edit().putBoolean(KEY_HEALED, true).apply();
        Log.i(TAG, "Re-enabled all theme launcher / AA aliases (one-time heal)");
    }

    private static void setEnabled(PackageManager pm, ComponentName component, boolean enable) {
        int state = enable
            ? PackageManager.COMPONENT_ENABLED_STATE_ENABLED
            : PackageManager.COMPONENT_ENABLED_STATE_DISABLED;
        try {
            pm.setComponentEnabledSetting(component, state, PackageManager.DONT_KILL_APP);
        } catch (Exception e) {
            Log.w(TAG, "Failed to toggle " + component.flattenToShortString(), e);
        }
    }

    private static String capitalize(String theme) {
        if (theme == null || theme.isEmpty()) return "Ocean";
        return Character.toUpperCase(theme.charAt(0)) + theme.substring(1);
    }
}
