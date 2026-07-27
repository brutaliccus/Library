package com.freiverse.library;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.Base64;
import android.util.Log;
import android.webkit.WebView;
import androidx.annotation.Nullable;
import androidx.core.content.ContextCompat;
import com.getcapacitor.Bridge;
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
    /** Short thaw for brief pauses (call / skip). */
    private static final long PLAY_WAKE_DELAY_MS = 400;
    /** Brief pause: one quick deliver + one backup. */
    private static final long[] PLAY_RETRY_DELAYS_MS = { 400, 1_200 };
    /** After multi-minute idle the Chromium WebView often needs seconds to thaw. */
    private static final long[] PLAY_RETRY_DELAYS_DEEP_MS = { 600, 1_500, 3_000, 5_000, 7_500 };
    private static final long WAKE_LOCK_MS = 12_000;

    private final Map<String, PluginCall> actionHandlers = new HashMap<>();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final List<PendingAction> pendingActions = new ArrayList<>();
    private String cachedArtworkUrl = null;
    private Bitmap cachedArtwork = null;

    /** Sticky play/playmedia until retries exhaust or user pauses (not optimistic play). */
    private PendingAction stickyPlay = null;
    private int stickyPlayAttempt = 0;
    private long stickyPlayStartedElapsed = 0;
    private long stickyPlayUntilElapsed = 0;
    private boolean stickyPlayDeepIdle = false;
    private PowerManager.WakeLock playWakeLock = null;
    private final Runnable stickyPlayRetryRunnable = this::runStickyPlayAttempt;

    private static final class PendingAction {
        final String action;
        final Bundle extras;

        PendingAction(String action, Bundle extras) {
            this.action = action;
            this.extras = extras;
        }
    }

    @Override
    public void load() {
        super.load();
        Context ctx = getContext();
        if (ctx != null) {
            LibraryAutoBridge.getInstance().ensureAppContext(ctx);
        }
        LibraryAutoBridge.getInstance().addActionListener(this);
        LibraryAutoBridge.getInstance().setBrowseRequestEmitter(this);
    }

    @Override
    protected void handleOnDestroy() {
        cancelStickyPlay();
        releasePlayWakeLock();
        LibraryAutoBridge.getInstance().removeActionListener(this);
        LibraryAutoBridge.getInstance().setBrowseRequestEmitter(null);
        super.handleOnDestroy();
    }

    @Override
    protected void handleOnResume() {
        super.handleOnResume();
        // Activity came up (AA soft-wake or user opened app) — thaw + flush play.
        softWakeWebView();
        // Do not cancel on bridge.isPlaying() — that flag is optimistic from AA onPlay.
        if (stickyPlay != null && SystemClock.elapsedRealtime() < stickyPlayUntilElapsed) {
            mainHandler.removeCallbacks(stickyPlayRetryRunnable);
            mainHandler.postDelayed(stickyPlayRetryRunnable, 150);
        }
        flushAllPending();
    }

    @Override
    public void emitBrowseRequest(String parentId, String requestId) {
        // Locked / Doze: thaw timers so the browse listener can answer (or at
        // least so a later refresh lands). Native cache covers the empty case.
        softWakeWebView();
        acquirePlayWakeLock();
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
            List<AutoBrowseNode> nodes = parseBrowseChildren(call.getArray("children"));
            new Handler(Looper.getMainLooper()).post(() -> {
                LibraryAutoBridge.getInstance().resolveBrowseChildren(rid, nodes);
                call.resolve();
            });
        }).start();
    }

    /**
     * Proactive persist of Continue / Library folders while the app is awake so
     * Android Auto can browse with the phone locked (no live JS/API required).
     */
    @PluginMethod
    public void cacheBrowseChildren(PluginCall call) {
        String parentId = call.getString("parentId");
        if (parentId == null || parentId.isEmpty()) {
            call.reject("parentId required");
            return;
        }
        Context ctx = getContext();
        if (ctx != null) {
            LibraryAutoBridge.getInstance().ensureAppContext(ctx);
        }
        final String pid = parentId;
        // Only clear a warm cache when JS confirms a live empty folder (not API fail).
        boolean allowEmpty = call.getBoolean("allowEmpty", false);
        // Icons optional for cache — skip network bitmap fetch to keep this fast.
        List<AutoBrowseNode> nodes = parseBrowseChildrenSkipBitmaps(call.getArray("children"));
        LibraryAutoBridge.getInstance().putBrowseCache(pid, nodes, allowEmpty);
        call.resolve();
    }

    private List<AutoBrowseNode> parseBrowseChildren(@Nullable JSArray children) {
        List<AutoBrowseNode> nodes = new ArrayList<>();
        if (children == null) {
            return nodes;
        }
        try {
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
        } catch (JSONException ex) {
            Log.w(TAG, "Failed to parse browse children", ex);
        }
        return nodes;
    }

    private List<AutoBrowseNode> parseBrowseChildrenSkipBitmaps(@Nullable JSArray children) {
        List<AutoBrowseNode> nodes = new ArrayList<>();
        if (children == null) {
            return nodes;
        }
        try {
            for (Object raw : children.toList()) {
                if (!(raw instanceof JSONObject)) {
                    continue;
                }
                JSONObject o = (JSONObject) raw;
                String iconUri = o.optString("iconUri", null);
                if (iconUri != null && iconUri.isEmpty()) {
                    iconUri = null;
                }
                nodes.add(
                    new AutoBrowseNode(
                        o.optString("mediaId", ""),
                        o.optString("title", ""),
                        o.optString("subtitle", ""),
                        o.optBoolean("browsable", false),
                        iconUri,
                        null
                    )
                );
            }
        } catch (JSONException ex) {
            Log.w(TAG, "Failed to parse browse children for cache", ex);
        }
        return nodes;
    }

    @PluginMethod
    public void bringToForeground(PluginCall call) {
        softWakeWebView();
        bringActivityToForeground();
        call.resolve();
    }

    /**
     * Thaw a Doze / background-frozen WebView so Capacitor callbacks and
     * audio.play() can run without requiring the user to open the app UI.
     * resumeTimers() is process-global and is the critical piece after idle.
     */
    private void softWakeWebView() {
        try {
            Bridge bridge = getBridge();
            if (bridge == null) {
                return;
            }
            WebView webView = bridge.getWebView();
            if (webView == null) {
                return;
            }
            webView.post(() -> {
                try {
                    webView.onResume();
                    webView.resumeTimers();
                } catch (Exception e) {
                    Log.w(TAG, "softWakeWebView failed", e);
                }
            });
        } catch (Exception e) {
            Log.w(TAG, "softWakeWebView unavailable", e);
        }
    }

    private void acquirePlayWakeLock() {
        try {
            Context ctx = getContext();
            if (ctx == null) {
                return;
            }
            if (playWakeLock != null && playWakeLock.isHeld()) {
                playWakeLock.acquire(WAKE_LOCK_MS);
                return;
            }
            PowerManager pm = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
            if (pm == null) {
                return;
            }
            playWakeLock =
                pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "library:aa_play_wake");
            playWakeLock.setReferenceCounted(false);
            playWakeLock.acquire(WAKE_LOCK_MS);
        } catch (Exception e) {
            Log.w(TAG, "acquirePlayWakeLock failed", e);
        }
    }

    private void releasePlayWakeLock() {
        try {
            if (playWakeLock != null && playWakeLock.isHeld()) {
                playWakeLock.release();
            }
        } catch (Exception e) {
            Log.w(TAG, "releasePlayWakeLock failed", e);
        }
        playWakeLock = null;
    }

    private void bringActivityToForeground() {
        android.content.Context ctx = getContext();
        if (ctx == null) {
            return;
        }
        android.content.Intent intent = new android.content.Intent(ctx, MainActivity.class);
        intent.addFlags(
            android.content.Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
                | android.content.Intent.FLAG_ACTIVITY_SINGLE_TOP
                | android.content.Intent.FLAG_ACTIVITY_NEW_TASK
                | android.content.Intent.FLAG_ACTIVITY_CLEAR_TOP
                | android.content.Intent.FLAG_ACTIVITY_NO_USER_ACTION
        );
        intent.putExtra("library_media_resume", true);
        try {
            ctx.startActivity(intent);
        } catch (Exception e) {
            Log.w(TAG, "bringActivityToForeground failed", e);
        }
    }

    private void ensurePlaybackService() {
        android.content.Context ctx = getContext();
        if (ctx == null) {
            return;
        }
        Class<?> serviceClass = ThemeIconHelper.mediaBrowserServiceClass(
            ThemeIconHelper.getSavedTheme(ctx)
        );
        android.content.Intent serviceIntent = new android.content.Intent(ctx, serviceClass);
        try {
            ContextCompat.startForegroundService(ctx, serviceIntent);
        } catch (Exception e) {
            Log.w(TAG, "startForegroundService failed", e);
        }
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
            cancelStickyPlay();
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
            maybeConfirmStickyPlay(playing);
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
        ensurePlaybackService();
        maybeConfirmStickyPlay(playing);

        call.resolve();
    }

    /**
     * JS sync saying playing=true may be optimistic (AA settle grace). Only stop
     * sticky retries once enough time has passed for audio to actually start —
     * especially after multi-minute idle thaws.
     */
    private void maybeConfirmStickyPlay(boolean playing) {
        if (!playing || stickyPlay == null) {
            return;
        }
        long minConfirmMs = stickyPlayDeepIdle ? 2_500 : 900;
        if (SystemClock.elapsedRealtime() - stickyPlayStartedElapsed >= minConfirmMs) {
            Log.i(TAG, "Sticky AA play confirmed by JS sync");
            cancelStickyPlay();
        }
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
            flushPendingFor(action);
            // Handlers re-registered after WebView thaw — retry sticky play.
            if (
                stickyPlay != null
                    && action.equals(stickyPlay.action)
                    && SystemClock.elapsedRealtime() < stickyPlayUntilElapsed
            ) {
                mainHandler.removeCallbacks(stickyPlayRetryRunnable);
                mainHandler.postDelayed(stickyPlayRetryRunnable, 100);
            }
        } else {
            call.resolve();
        }
    }

    private void flushPendingFor(String action) {
        List<PendingAction> due = new ArrayList<>();
        synchronized (pendingActions) {
            for (int i = pendingActions.size() - 1; i >= 0; i--) {
                if (action.equals(pendingActions.get(i).action)) {
                    due.add(0, pendingActions.remove(i));
                }
            }
        }
        for (PendingAction p : due) {
            deliverToJs(p.action, p.extras);
        }
    }

    private void flushAllPending() {
        List<PendingAction> due;
        synchronized (pendingActions) {
            due = new ArrayList<>(pendingActions);
            pendingActions.clear();
        }
        for (PendingAction p : due) {
            deliverToJs(p.action, p.extras);
        }
    }

    private void cancelStickyPlay() {
        stickyPlay = null;
        stickyPlayAttempt = 0;
        stickyPlayStartedElapsed = 0;
        stickyPlayUntilElapsed = 0;
        stickyPlayDeepIdle = false;
        mainHandler.removeCallbacks(stickyPlayRetryRunnable);
        releasePlayWakeLock();
        LibraryAutoBridge.getInstance().clearDeepIdlePlayLatch();
    }

    private void beginStickyPlay(String action, Bundle extras, boolean deepIdle) {
        stickyPlay = new PendingAction(action, extras);
        stickyPlayAttempt = 0;
        stickyPlayDeepIdle = deepIdle;
        stickyPlayStartedElapsed = SystemClock.elapsedRealtime();
        long[] delays = deepIdle ? PLAY_RETRY_DELAYS_DEEP_MS : PLAY_RETRY_DELAYS_MS;
        stickyPlayUntilElapsed =
            stickyPlayStartedElapsed + delays[delays.length - 1] + 2_500;
        acquirePlayWakeLock();
        softWakeWebView();
        ensurePlaybackService();
        // Soft-wake timers first; only bring the activity up for deep idle so the
        // phone UI isn't flashed on every short AA pause/resume.
        if (deepIdle) {
            bringActivityToForeground();
        }
        mainHandler.removeCallbacks(stickyPlayRetryRunnable);
        scheduleStickyPlayAttempt(delays[0]);
    }

    private void scheduleStickyPlayAttempt(long delayMs) {
        mainHandler.removeCallbacks(stickyPlayRetryRunnable);
        mainHandler.postDelayed(stickyPlayRetryRunnable, delayMs);
    }

    private void runStickyPlayAttempt() {
        if (stickyPlay == null) {
            return;
        }
        if (SystemClock.elapsedRealtime() >= stickyPlayUntilElapsed) {
            Log.w(TAG, "Sticky AA play timed out without confirmation");
            cancelStickyPlay();
            return;
        }

        long[] delays = stickyPlayDeepIdle ? PLAY_RETRY_DELAYS_DEEP_MS : PLAY_RETRY_DELAYS_MS;

        softWakeWebView();
        ensurePlaybackService();
        // Deep idle: activity wake from the start. Short pause: escalate only if
        // the first timer-only deliver didn't get a JS confirm.
        if (stickyPlayDeepIdle || stickyPlayAttempt >= 1) {
            bringActivityToForeground();
        }
        LibraryAutoBridge.getInstance().requestAudioFocusForPlay();

        PluginCall handler = actionHandlers.get(stickyPlay.action);
        boolean missing =
            handler == null || PluginCall.CALLBACK_ID_DANGLING.equals(handler.getCallbackId());
        if (missing) {
            Log.d(
                TAG,
                "Sticky AA play waiting for JS handler (attempt "
                    + stickyPlayAttempt
                    + "): "
                    + stickyPlay.action
            );
            synchronized (pendingActions) {
                pendingActions.add(new PendingAction(stickyPlay.action, stickyPlay.extras));
                while (pendingActions.size() > 8) {
                    pendingActions.remove(0);
                }
            }
        } else {
            Log.i(
                TAG,
                "Delivering sticky AA play attempt "
                    + stickyPlayAttempt
                    + " action="
                    + stickyPlay.action
            );
            deliverToJs(stickyPlay.action, stickyPlay.extras);
        }

        stickyPlayAttempt++;
        if (stickyPlayAttempt < delays.length) {
            long nextDelay = delays[stickyPlayAttempt];
            long already = delays[stickyPlayAttempt - 1];
            scheduleStickyPlayAttempt(Math.max(200, nextDelay - already));
        } else {
            mainHandler.postDelayed(
                () -> {
                    if (stickyPlay == null) {
                        return;
                    }
                    if (SystemClock.elapsedRealtime() >= stickyPlayUntilElapsed) {
                        Log.w(TAG, "Sticky AA play exhausted retries");
                        cancelStickyPlay();
                    }
                },
                2_500
            );
        }
    }

    @Override
    public void onAction(String action, Bundle extras) {
        boolean isPlay = "play".equals(action) || "playmedia".equals(action);
        boolean needsWake =
            isPlay
                || "seekto".equals(action)
                || "seekforward".equals(action)
                || "seekbackward".equals(action);

        if (needsWake) {
            softWakeWebView();
            ensurePlaybackService();
            acquirePlayWakeLock();
        }

        if (isPlay) {
            boolean deepIdle = LibraryAutoBridge.getInstance().isDeepIdlePlay();
            Log.i(
                TAG,
                "AA play wake deepIdle="
                    + deepIdle
                    + " idleMs="
                    + LibraryAutoBridge.getInstance().millisSincePause()
            );
            beginStickyPlay(action, extras, deepIdle);
            return;
        }

        if ("pause".equals(action) || "stop".equals(action)) {
            cancelStickyPlay();
        }

        PluginCall handler = actionHandlers.get(action);
        boolean missing =
            handler == null || PluginCall.CALLBACK_ID_DANGLING.equals(handler.getCallbackId());

        if (missing) {
            Log.d(TAG, "Queueing AA action until JS handler ready: " + action);
            synchronized (pendingActions) {
                pendingActions.add(new PendingAction(action, extras));
                while (pendingActions.size() > 8) {
                    pendingActions.remove(0);
                }
            }
            // Retry delivery after WebView has a chance to re-register handlers.
            mainHandler.postDelayed(() -> {
                softWakeWebView();
                PluginCall h = actionHandlers.get(action);
                if (h != null && !PluginCall.CALLBACK_ID_DANGLING.equals(h.getCallbackId())) {
                    flushPendingFor(action);
                }
            }, PLAY_WAKE_DELAY_MS + 200);
            return;
        }

        if (needsWake) {
            // Seek path: brief thaw then deliver (keep ±15 snappy).
            mainHandler.postDelayed(() -> deliverToJs(action, extras), PLAY_WAKE_DELAY_MS);
            return;
        }

        deliverToJs(action, extras);
    }

    private void deliverToJs(String action, Bundle extras) {
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
