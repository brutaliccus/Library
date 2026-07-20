package com.freiverse.library;

import android.content.ComponentName;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.util.Log;

/** Switches launcher activity-aliases + Android Auto MediaBrowserService icons by theme. */
public final class ThemeIconHelper {
    private static final String TAG = "ThemeIconHelper";
    private static final String PREFS = "library_theme_icon";
    private static final String KEY_THEME = "theme";
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
     * Enable the target launcher/AA components first, then disable the others.
     * Disabling the currently-active launcher alias (or briefly having zero
     * launchers) can kill the process even with DONT_KILL_APP — which showed up
     * as "theme change crashes twice then stabilizes" when JS also re-applied
     * a speculative default theme on reload.
     */
    public static void apply(Context context, String themeRaw) {
        String theme = normalize(themeRaw);
        String saved = getSavedTheme(context);
        if (theme.equals(saved) && isTargetEnabled(context, theme)) {
            return;
        }

        PackageManager pm = context.getPackageManager();
        String pkg = context.getPackageName();

        // Enable target first so a launcher stays available throughout the switch.
        setEnabled(pm, new ComponentName(pkg, pkg + ".Launcher" + capitalize(theme)), true);
        setEnabled(pm, new ComponentName(context, mediaBrowserServiceClass(theme)), true);

        for (String t : THEMES) {
            if (t.equals(theme)) continue;
            setEnabled(pm, new ComponentName(pkg, pkg + ".Launcher" + capitalize(t)), false);
            setEnabled(pm, new ComponentName(context, mediaBrowserServiceClass(t)), false);
        }

        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_THEME, theme)
            .apply();
        Log.i(TAG, "App / Android Auto icon theme -> " + theme);
    }

    private static boolean isTargetEnabled(Context context, String theme) {
        PackageManager pm = context.getPackageManager();
        String pkg = context.getPackageName();
        try {
            int launcher = pm.getComponentEnabledSetting(
                new ComponentName(pkg, pkg + ".Launcher" + capitalize(theme))
            );
            int service = pm.getComponentEnabledSetting(
                new ComponentName(context, mediaBrowserServiceClass(theme))
            );
            boolean launcherOn = launcher == PackageManager.COMPONENT_ENABLED_STATE_ENABLED
                || (launcher == PackageManager.COMPONENT_ENABLED_STATE_DEFAULT && theme.equals(DEFAULT_THEME));
            boolean serviceOn = service == PackageManager.COMPONENT_ENABLED_STATE_ENABLED
                || (service == PackageManager.COMPONENT_ENABLED_STATE_DEFAULT && theme.equals(DEFAULT_THEME));
            return launcherOn && serviceOn;
        } catch (Exception e) {
            return false;
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

    private static String capitalize(String theme) {
        if (theme == null || theme.isEmpty()) return "Ocean";
        return Character.toUpperCase(theme.charAt(0)) + theme.substring(1);
    }
}
