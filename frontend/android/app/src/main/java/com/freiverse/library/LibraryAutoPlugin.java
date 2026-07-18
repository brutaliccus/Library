package com.freiverse.library;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Bundle;
import android.util.Base64;
import android.util.Log;
import androidx.core.content.ContextCompat;
import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.json.JSONException;
import org.json.JSONObject;

@CapacitorPlugin(name = "LibraryAuto")
public class LibraryAutoPlugin extends Plugin
    implements LibraryAutoBridge.ActionListener, LibraryAutoBridge.BrowseRequestEmitter {

    private static final String TAG = "LibraryAuto";

    private final Map<String, PluginCall> actionHandlers = new HashMap<>();
    private String cachedArtworkUrl = null;
    private Bitmap cachedArtwork = null;

    @Override
    public void load() {
        super.load();
        LibraryAutoBridge.getInstance().addActionListener(this);
        LibraryAutoBridge.getInstance().setBrowseRequestEmitter(this);
    }

    @Override
    protected void handleOnDestroy() {
        LibraryAutoBridge.getInstance().removeActionListener(this);
        LibraryAutoBridge.getInstance().setBrowseRequestEmitter(null);
        super.handleOnDestroy();
    }

    @Override
    public void emitBrowseRequest(String parentId, String requestId) {
        JSObject data = new JSObject();
        data.put("parentId", parentId);
        data.put("requestId", requestId);
        notifyListeners("browseRequest", data);
    }

    @PluginMethod
    public void resolveBrowseChildren(PluginCall call) {
        String requestId = call.getString("requestId");
        if (requestId == null || requestId.isEmpty()) {
            call.reject("requestId required");
            return;
        }

        final String rid = requestId;
        new Thread(() -> {
            List<AutoBrowseNode> nodes = new ArrayList<>();
            try {
                JSArray children = call.getArray("children");
                if (children != null) {
                    for (Object raw : children.toList()) {
                        if (!(raw instanceof JSONObject)) {
                            continue;
                        }
                        JSONObject o = (JSONObject) raw;
                        String iconUri = o.optString("iconUri", null);
                        if (iconUri != null && iconUri.isEmpty()) {
                            iconUri = null;
                        }
                        Bitmap iconBitmap = null;
                        if (iconUri != null) {
                            try {
                                iconBitmap = urlToBitmap(iconUri);
                            } catch (IOException ex) {
                                Log.w(TAG, "Browse icon load failed: " + iconUri, ex);
                            }
                        }
                        nodes.add(
                            new AutoBrowseNode(
                                o.optString("mediaId", ""),
                                o.optString("title", ""),
                                o.optString("subtitle", ""),
                                o.optBoolean("browsable", false),
                                iconUri,
                                iconBitmap
                            )
                        );
                    }
                }
            } catch (JSONException ex) {
                Log.w(TAG, "Failed to parse browse children", ex);
            }

            new android.os.Handler(android.os.Looper.getMainLooper()).post(() -> {
                LibraryAutoBridge.getInstance().resolveBrowseChildren(rid, nodes);
                call.resolve();
            });
        }).start();
    }

    @PluginMethod
    public void bringToForeground(PluginCall call) {
        bringActivityToForeground();
        call.resolve();
    }

    private void bringActivityToForeground() {
        if (getActivity() == null || getContext() == null) {
            return;
        }
        android.content.Intent intent = new android.content.Intent(getContext(), getActivity().getClass());
        intent.addFlags(
            android.content.Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
                | android.content.Intent.FLAG_ACTIVITY_SINGLE_TOP
                | android.content.Intent.FLAG_ACTIVITY_NEW_TASK
        );
        getActivity().startActivity(intent);
    }

    @PluginMethod
    public void syncPlayback(PluginCall call) {
        boolean active = call.getBoolean("active", false);
        boolean playing = call.getBoolean("playing", false);
        boolean positionOnly = call.getBoolean("positionOnly", false);
        double positionSec = call.getDouble("position", 0.0);
        float playbackRate = call.getFloat("playbackRate", 1.0f);

        if (!active) {
            cachedArtworkUrl = null;
            cachedArtwork = null;
            LibraryAutoBridge.getInstance().clear();
            call.resolve();
            return;
        }

        if (positionOnly) {
            LibraryAutoBridge.getInstance().updatePosition(
                playing,
                Math.round(positionSec * 1000),
                playbackRate
            );
            call.resolve();
            return;
        }

        String title = call.getString("title", "");
        String artist = call.getString("artist", "");
        String album = call.getString("album", "");
        double durationSec = call.getDouble("duration", 0.0);

        Bitmap artwork = null;
        try {
            JSArray artworkArray = call.getArray("artwork");
            if (artworkArray != null) {
                List<JSONObject> artworkList = artworkArray.toList();
                for (JSONObject artworkJson : artworkList) {
                    String src = artworkJson.optString("src", null);
                    if (src != null) {
                        artwork = getCachedArtwork(src);
                        break;
                    }
                }
            }
        } catch (JSONException | IOException ex) {
            Log.w(TAG, "Unable to load artwork", ex);
        }

        LibraryAutoBridge.getInstance().update(
            title,
            artist,
            album,
            artwork,
            true,
            playing,
            Math.round(durationSec * 1000),
            Math.round(positionSec * 1000),
            playbackRate
        );
        android.content.Context ctx = getContext();
        if (ctx != null) {
            android.content.Intent intent = new android.content.Intent(
                ctx,
                LibraryMediaBrowserService.class
            );
            ContextCompat.startForegroundService(ctx, intent);
        }

        call.resolve();
    }

    private Bitmap getCachedArtwork(String url) throws IOException {
        if (url != null && url.equals(cachedArtworkUrl) && cachedArtwork != null) {
            return cachedArtwork;
        }
        Bitmap bitmap = urlToBitmap(url);
        if (bitmap != null) {
            cachedArtworkUrl = url;
            cachedArtwork = bitmap;
        }
        return bitmap;
    }

    @PluginMethod(returnType = PluginMethod.RETURN_CALLBACK)
    public void setActionHandler(PluginCall call) {
        call.setKeepAlive(true);
        String action = call.getString("action");
        if (action != null) {
            actionHandlers.put(action, call);
        } else {
            call.resolve();
        }
    }

    @Override
    public void onAction(String action, Bundle extras) {
        // Only foreground when picking a title from the browse tree — not for
        // play/pause transport controls (bringing the WebView forward mid-resume
        // races audio.play() and breaks Android Auto / lock-screen play).
        if ("playmedia".equals(action)) {
            bringActivityToForeground();
            android.content.Context ctx = getContext();
            if (ctx != null) {
                android.content.Intent serviceIntent = new android.content.Intent(
                    ctx,
                    LibraryMediaBrowserService.class
                );
                ContextCompat.startForegroundService(ctx, serviceIntent);
            }
        }

        PluginCall handler = actionHandlers.get(action);
        if (handler == null || PluginCall.CALLBACK_ID_DANGLING.equals(handler.getCallbackId())) {
            Log.d(TAG, "No JS handler for action: " + action);
            return;
        }

        JSObject data = new JSObject();
        data.put("action", action);
        if (extras != null) {
            if (extras.containsKey("seekTimeMs")) {
                data.put("seekTime", extras.getLong("seekTimeMs") / 1000.0);
            }
            if (extras.containsKey("mediaId")) {
                data.put("mediaId", extras.getString("mediaId"));
            }
        }
        handler.resolve(data);
    }

    private Bitmap urlToBitmap(String url) throws IOException {
        if (url == null || url.isEmpty() || url.startsWith("blob:")) {
            return null;
        }

        if (url.startsWith("http")) {
            HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
            connection.setDoInput(true);
            connection.setConnectTimeout(8000);
            connection.setReadTimeout(8000);
            connection.connect();
            try (InputStream inputStream = connection.getInputStream()) {
                return BitmapFactory.decodeStream(inputStream);
            }
        }

        int base64Index = url.indexOf(";base64,");
        if (base64Index != -1) {
            String base64Data = url.substring(base64Index + 8);
            byte[] decoded = Base64.decode(base64Data, Base64.DEFAULT);
            return BitmapFactory.decodeByteArray(decoded, 0, decoded.length);
        }

        return null;
    }
}
