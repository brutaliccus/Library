package com.freiverse.library;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

@CapacitorPlugin(name = "ThemeIcon")
public class ThemeIconPlugin extends Plugin {

    @PluginMethod
    public void setTheme(PluginCall call) {
        String theme = call.getString("theme", ThemeIconHelper.DEFAULT_THEME);
        ThemeIconHelper.apply(getContext(), theme);
        JSObject ret = new JSObject();
        ret.put("theme", ThemeIconHelper.normalize(theme));
        call.resolve(ret);
    }

    @PluginMethod
    public void getTheme(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("theme", ThemeIconHelper.getSavedTheme(getContext()));
        call.resolve(ret);
    }
}
