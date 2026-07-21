package com.freiverse.library;

import android.content.ComponentName;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.util.Log;

/**
 * Persists UI theme and keeps exactly one Android Auto MediaBrowserService enabled.
 *
 * Launcher activity-alias switching via PackageManager can kill the Capacitor
 * WebView (DONT_KILL_APP is unreliable when disabling the active launcher).
 * Home-screen icon stays on whatever the manifest/PackageManager already has;
 * only AA MediaBrowser services are toggled here.
 */
public final class ThemeIconHelper {
    private static final String TAG = "ThemeIconHelper";
    private static final String PREFS = "library_theme_icon";
    private static final String KEY_THEME = "theme";
    /** v1 heal re-enabled ALL services (4 AA icons). v2 corrects that. */
    private static final String KEY_HEALED = "aliases_healed_v2";
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
     * Persist theme and enable only the matching Android Auto MediaBrowserService.
     * Does not toggle launcher aliases (crash-prone).
     */
    public static void apply(Context context, String themeRaw) {
        String theme = normalize(themeRaw);
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        prefs.edit().putString(KEY_THEME, theme).apply();
        applyMediaBrowserOnly(context, theme);
        prefs.edit().putBoolean(KEY_HEALED, true).apply();
        Log.i(TAG, "Android Auto icon theme -> " + theme);
    }

    /** Cold start: heal from older builds that enabled every themed AA service. */
    public static void ensureSafeAliases(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        applyMediaBrowserOnly(context, getSavedTheme(context));
        if (!prefs.getBoolean(KEY_HEALED, false)) {
            prefs.edit().putBoolean(KEY_HEALED, true).apply();
            Log.i(TAG, "Healed AA services to a single theme entry");
        }
    }

    /** Enable the theme's MediaBrowserService; disable the other three. */
    private static void applyMediaBrowserOnly(Context context, String theme) {
        PackageManager pm = context.getPackageManager();
        Class<?> target = mediaBrowserServiceClass(theme);
        // Enable target first so AA always has a browsable service.
        setEnabled(pm, new ComponentName(context, target), true);
        for (String t : THEMES) {
            Class<?> cls = mediaBrowserServiceClass(t);
            if (cls.equals(target)) continue;
            setEnabled(pm, new ComponentName(context, cls), false);
        }
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
}
