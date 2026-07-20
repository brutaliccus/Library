package com.freiverse.library;

import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;

import androidx.core.content.FileProvider;
import androidx.core.content.pm.PackageInfoCompat;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.BufferedInputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

@CapacitorPlugin(name = "AppUpdate")
public class AppUpdatePlugin extends Plugin {

    private static final String PREFS = "library_app_update_events";
    private static final String PENDING_DISMISS = "pending_dismiss";
    private static final String PENDING_UPDATE = "pending_update";
    private static final String PENDING_JS_DOWNLOAD = "pending_js_download";

    private static AppUpdatePlugin instance;
    private static volatile boolean inForeground = false;

    public interface ProgressListener {
        void onProgress(int percent);
    }

    public static boolean isInForeground() {
        return inForeground;
    }

    @Override
    public void load() {
        super.load();
        instance = this;
        drainPendingEvents();
    }

    @Override
    protected void handleOnResume() {
        super.handleOnResume();
        inForeground = true;
    }

    @Override
    protected void handleOnPause() {
        inForeground = false;
        super.handleOnPause();
    }

    @Override
    protected void handleOnDestroy() {
        if (instance == this) {
            instance = null;
        }
        super.handleOnDestroy();
    }

    @PluginMethod
    public void showUpdateAvailable(PluginCall call) {
        String title = call.getString("title", "Update available");
        String body = call.getString("body", "");
        String releaseKey = call.getString("releaseKey", "");
        String downloadUrl = call.getString("downloadUrl", "");
        String authToken = call.getString("authToken", "");
        if (releaseKey == null || releaseKey.isEmpty() || downloadUrl == null || downloadUrl.isEmpty()) {
            call.reject("releaseKey and downloadUrl are required");
            return;
        }
        AppUpdateNotifier.show(
            getContext(),
            title,
            body,
            releaseKey,
            downloadUrl,
            authToken != null ? authToken : ""
        );
        call.resolve();
    }

    @PluginMethod
    public void dismissUpdateNotification(PluginCall call) {
        AppUpdateNotifier.dismiss(getContext());
        call.resolve();
    }

    static void requestJsDownload(Context context) {
        if (instance != null) {
            instance.dispatchAppUpdateRequested(context);
            return;
        }
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putBoolean(PENDING_JS_DOWNLOAD, true)
            .apply();
    }

    private void dispatchAppUpdateRequested(Context context) {
        String releaseKey = AppUpdatePendingStore.getReleaseKey(context);
        String downloadUrl = AppUpdatePendingStore.getDownloadUrl(context);
        if (downloadUrl == null || downloadUrl.isEmpty()) {
            return;
        }
        JSObject data = new JSObject();
        data.put("releaseKey", releaseKey != null ? releaseKey : "");
        data.put("downloadUrl", downloadUrl);
        notifyListeners("appUpdateRequested", data, true);
    }

    static void dispatchDismissed(Context context, String releaseKey) {
        JSObject data = new JSObject();
        data.put("releaseKey", releaseKey != null ? releaseKey : "");
        if (instance != null) {
            instance.notifyListeners("appUpdateDismissed", data, true);
            return;
        }
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(PENDING_DISMISS, data.toString())
            .apply();
    }

    private void drainPendingEvents() {
        SharedPreferences prefs = getContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String dismiss = prefs.getString(PENDING_DISMISS, null);
        if (dismiss != null) {
            prefs.edit().remove(PENDING_DISMISS).apply();
            try {
                notifyListeners("appUpdateDismissed", new JSObject(dismiss), true);
            } catch (Exception ignored) {
                /* ignore */
            }
        }
        String update = prefs.getString(PENDING_UPDATE, null);
        if (update != null) {
            prefs.edit().remove(PENDING_UPDATE).apply();
            try {
                JSObject data = new JSObject(update);
                if (data.has("message")) {
                    notifyListeners("appUpdateFailed", data, true);
                } else {
                    notifyListeners("appUpdateNow", data, true);
                }
            } catch (Exception ignored) {
                notifyListeners("appUpdateNow", new JSObject(), true);
            }
        }
        if (prefs.getBoolean(PENDING_JS_DOWNLOAD, false)) {
            prefs.edit().remove(PENDING_JS_DOWNLOAD).apply();
            dispatchAppUpdateRequested(getContext());
        }
    }

    @PluginMethod
    public void getInstalledVersion(PluginCall call) {
        try {
            var pm = getContext().getPackageManager();
            var info = pm.getPackageInfo(getContext().getPackageName(), 0);
            JSObject ret = new JSObject();
            ret.put("versionCode", PackageInfoCompat.getLongVersionCode(info));
            ret.put("versionName", info.versionName != null ? info.versionName : "");
            call.resolve(ret);
        } catch (Exception e) {
            call.reject("Could not read app version", e);
        }
    }

    @PluginMethod
    public void downloadAndInstall(PluginCall call) {
        String url = call.getString("url");
        String authToken = call.getString("authToken", "");
        if (url == null || url.isEmpty()) {
            call.reject("url is required");
            return;
        }

        new Thread(() -> {
            try {
                File apk = downloadApkFile(
                    getContext(),
                    url,
                    authToken != null ? authToken : "",
                    percent -> {
                        JSObject data = new JSObject();
                        data.put("percent", percent);
                        notifyListeners("downloadProgress", data);
                    }
                );
                bridge
                    .getActivity()
                    .runOnUiThread(() -> {
                        try {
                            launchPackageInstaller(getContext(), apk);
                            JSObject result = new JSObject();
                            result.put("filePath", apk.getAbsolutePath());
                            call.resolve(result);
                        } catch (Exception e) {
                            call.reject(e.getMessage() != null ? e.getMessage() : "Install failed", e);
                        }
                    });
            } catch (Exception e) {
                call.reject(e.getMessage() != null ? e.getMessage() : "Download failed", e);
            }
        })
            .start();
    }

    private static File downloadApkFile(
        Context context,
        String urlString,
        String authToken,
        ProgressListener listener
    ) throws Exception {
        File dir = new File(context.getCacheDir(), "updates");
        if (!dir.exists() && !dir.mkdirs()) {
            throw new Exception("Could not create update folder");
        }
        File out = new File(dir, "library-update.apk");
        if (out.exists() && !out.delete()) {
            throw new Exception("Could not replace previous download");
        }

        URL url = new URL(urlString);
        HttpURLConnection conn = (HttpURLConnection) url.openConnection();
        conn.setInstanceFollowRedirects(true);
        conn.setRequestMethod("GET");
        conn.setRequestProperty("User-Agent", "Library-Android-AppUpdate");
        if (authToken != null && !authToken.isEmpty()) {
            conn.setRequestProperty("Authorization", "Bearer " + authToken);
        }
        conn.setConnectTimeout(60_000);
        conn.setReadTimeout(300_000);
        conn.connect();

        int code = conn.getResponseCode();
        // Follow one redirect manually if needed (GitHub asset URLs).
        if (code == HttpURLConnection.HTTP_MOVED_PERM
            || code == HttpURLConnection.HTTP_MOVED_TEMP
            || code == HttpURLConnection.HTTP_SEE_OTHER
            || code == 307
            || code == 308) {
            String loc = conn.getHeaderField("Location");
            conn.disconnect();
            if (loc == null || loc.isEmpty()) {
                throw new Exception("Redirect without Location");
            }
            conn = (HttpURLConnection) new URL(loc).openConnection();
            conn.setInstanceFollowRedirects(true);
            conn.setRequestMethod("GET");
            conn.setRequestProperty("User-Agent", "Library-Android-AppUpdate");
            conn.setConnectTimeout(60_000);
            conn.setReadTimeout(300_000);
            conn.connect();
            code = conn.getResponseCode();
        }

        if (code < 200 || code >= 300) {
            String err = "HTTP " + code;
            InputStream errStream = conn.getErrorStream();
            if (errStream != null) {
                try (errStream) {
                    byte[] buf = errStream.readAllBytes();
                    if (buf.length > 0) {
                        err = err + ": " + new String(buf).trim();
                    }
                }
            }
            conn.disconnect();
            throw new Exception(err);
        }

        int total = conn.getContentLength();
        try (
            InputStream raw = conn.getInputStream();
            BufferedInputStream in = new BufferedInputStream(raw);
            FileOutputStream fos = new FileOutputStream(out)
        ) {
            byte[] buffer = new byte[8192];
            long downloaded = 0;
            int read;
            while ((read = in.read(buffer)) != -1) {
                fos.write(buffer, 0, read);
                downloaded += read;
                if (total > 0 && listener != null) {
                    int pct = (int) Math.min(100, (downloaded * 100) / total);
                    listener.onProgress(pct);
                }
            }
        } finally {
            conn.disconnect();
        }

        if (listener != null) {
            listener.onProgress(100);
        }
        return out;
    }

    private static void launchPackageInstaller(Context context, File apk) throws Exception {
        var activity =
            instance != null && instance.getBridge() != null ? instance.getBridge().getActivity() : null;
        if (activity == null) {
            throw new Exception("Activity not available");
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            if (!activity.getPackageManager().canRequestPackageInstalls()) {
                Intent settings = new Intent(
                    Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + activity.getPackageName())
                );
                settings.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                activity.startActivity(settings);
                throw new Exception(
                    "Allow \"Install unknown apps\" for Library in Settings, then tap Update again."
                );
            }
        }

        Uri uri = FileProvider.getUriForFile(
            context,
            context.getPackageName() + ".fileprovider",
            apk
        );

        Intent install = new Intent(Intent.ACTION_VIEW);
        install.setDataAndType(uri, "application/vnd.android.package-archive");
        install.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        install.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        activity.startActivity(install);
    }
}
