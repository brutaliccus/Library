package com.freiverse.library;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
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
    /** Drops stale async artwork loads when a newer syncPlayback wins the race. */
    private int artworkLoadGeneration = 0;

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
        LibraryNativePlayer.getInstance().addListener(nativePlayerListener);
    }

    private final LibraryNativePlayer.Listener nativePlayerListener =
        new LibraryNativePlayer.Listener() {
            @Override
            public void onNativePlaying(
                String mediaId,
                String title,
                String artist,
                String album,
                String coverUrl,
                boolean playing,
                long durationMs,
                long positionMs,
                float speed,
                int trackIndex
            ) {
                // Bridge already updates MediaSession; notify JS for attach/UI.
                JSObject data = new JSObject();
                data.put("mediaId", mediaId != null ? mediaId : "");
                data.put("title", title != null ? title : "");
                data.put("artist", artist != null ? artist : "");
                data.put("album", album != null ? album : "");
                data.put("coverUrl", coverUrl != null ? coverUrl : "");
                data.put("playing", playing);
                data.put("duration", durationMs / 1000.0);
                data.put("position", positionMs / 1000.0);
                data.put("playbackRate", speed);
                data.put("trackIndex", trackIndex);
                data.put("nativeOwner", true);
                notifyListeners("nativePlayback", data);
                // Confirm sticky — native audio is the confirmation, not WebView.
                if (playing) {
                    cancelStickyPlay();
                }
            }

            @Override
            public void onNativeStopped() {
                JSObject data = new JSObject();
                data.put("nativeOwner", false);
                data.put("playing", false);
                notifyListeners("nativePlayback", data);
            }

            @Override
            public void onNativeError(String message) {
                JSObject data = new JSObject();
                data.put("error", message != null ? message : "error");
                data.put("nativeOwner", false);
                notifyListeners("nativePlayback", data);
            }
        };

    @Override
    protected void handleOnDestroy() {
        cancelStickyPlay();
        releasePlayWakeLock();
        LibraryNativePlayer.getInstance().removeListener(nativePlayerListener);
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
    /**
     * Persist a full playable queue (track URLs + auth) so Android Auto can
     * start ExoPlayer while the phone is locked / WebView frozen.
     */
    @PluginMethod
    public void cachePlayableMedia(PluginCall call) {
        String mediaId = call.getString("mediaId");
        if (mediaId == null || mediaId.isEmpty()) {
            call.reject("mediaId required");
            return;
        }
        Context ctx = getContext();
        if (ctx != null) {
            LibraryAutoBridge.getInstance().ensureAppContext(ctx);
        }
        try {
            JSObject raw = call.getData();
            if (raw == null) {
                call.reject("playable payload required");
                return;
            }
            LibraryAutoBridge.getInstance().putPlayableCache(mediaId, new JSONObject(raw.toString()));
            call.resolve();
        } catch (Exception e) {
            call.reject("cachePlayableMedia failed: " + e.getMessage());
        }
    }

    /** Append a base64 audio chunk to the on-disk offline cache (large books). */
    @PluginMethod
    public void appendAudioDiskCache(PluginCall call) {
        Context ctx = getContext();
        if (ctx == null) {
            call.reject("no context");
            return;
        }
        String storageKey = call.getString("storageKey");
        String data = call.getString("data");
        String contentType = call.getString("contentType");
        Double total = call.getDouble("total");
        Integer offset = call.getInt("offset");
        if (storageKey == null || storageKey.isEmpty() || data == null) {
            call.reject("storageKey and data required");
            return;
        }
        try {
            LibraryAudioDiskCache.setStorageKeyMeta(
                ctx,
                storageKey,
                contentType,
                total != null ? total.longValue() : null
            );
            boolean ok =
                LibraryAudioDiskCache.appendBase64(
                    ctx,
                    storageKey,
                    data,
                    contentType,
                    total != null ? total.longValue() : null,
                    offset != null ? offset : -1
                );
            JSObject result = new JSObject();
            result.put("ok", ok);
            result.put("size", LibraryAudioDiskCache.getPartialSize(ctx, storageKey));
            call.resolve(result);
        } catch (Exception e) {
            call.reject("appendAudioDiskCache failed: " + e.getMessage());
        }
    }

    @PluginMethod
    public void finalizeAudioDiskCache(PluginCall call) {
        Context ctx = getContext();
        if (ctx == null) {
            call.reject("no context");
            return;
        }
        String storageKey = call.getString("storageKey");
        if (storageKey == null || storageKey.isEmpty()) {
            call.reject("storageKey required");
            return;
        }
        boolean ok = LibraryAudioDiskCache.finalizeStorageKey(ctx, storageKey);
        JSObject result = new JSObject();
        result.put("ok", ok);
        Uri uri = LibraryAudioDiskCache.getFileUri(ctx, storageKey);
        if (uri != null) {
            result.put("uri", uri.toString());
            result.put("path", uri.getPath());
        }
        result.put("size", LibraryAudioDiskCache.getCompleteSize(ctx, storageKey));
        call.resolve(result);
    }

    @PluginMethod
    public void getAudioDiskCacheUri(PluginCall call) {
        Context ctx = getContext();
        if (ctx == null) {
            call.reject("no context");
            return;
        }
        String storageKey = call.getString("storageKey");
        if (storageKey == null || storageKey.isEmpty()) {
            call.reject("storageKey required");
            return;
        }
        JSObject result = new JSObject();
        boolean complete = LibraryAudioDiskCache.isComplete(ctx, storageKey);
        result.put("complete", complete);
        result.put("size", LibraryAudioDiskCache.getCompleteSize(ctx, storageKey));
        result.put("partialSize", LibraryAudioDiskCache.getPartialSize(ctx, storageKey));
        Uri uri = LibraryAudioDiskCache.getFileUri(ctx, storageKey);
        if (uri != null) {
            result.put("uri", uri.toString());
            result.put("path", uri.getPath());
        }
        call.resolve(result);
    }

    @PluginMethod
    public void deleteAudioDiskCache(PluginCall call) {
        Context ctx = getContext();
        if (ctx == null) {
            call.reject("no context");
            return;
        }
        String storageKey = call.getString("storageKey");
        String urlPrefix = call.getString("urlPrefix");
        if (storageKey != null && !storageKey.isEmpty()) {
            LibraryAudioDiskCache.deleteStorageKey(ctx, storageKey);
        } else if (urlPrefix != null && !urlPrefix.isEmpty()) {
            LibraryAudioDiskCache.deleteByUrlPrefix(ctx, urlPrefix);
        } else if (Boolean.TRUE.equals(call.getBoolean("all", false))) {
            LibraryAudioDiskCache.clearAll(ctx);
        }
        call.resolve();
    }

    /** Snapshot of native ExoPlayer ownership — used when the app UI opens mid-play. */
    @PluginMethod
    public void getNativePlaybackState(PluginCall call) {
        LibraryNativePlayer np = LibraryNativePlayer.getInstance();
        JSObject data = new JSObject();
        boolean owning = LibraryAutoBridge.getInstance().isNativeOwningPlayback();
        data.put("nativeOwner", owning);
        data.put("playing", owning && np.isPlaying());
        String mid = LibraryAutoBridge.getInstance().getNativeMediaId();
        if (mid != null) {
            data.put("mediaId", mid);
        }
        data.put("position", owning ? np.getPositionMs() / 1000.0 : 0);
        call.resolve(data);
    }

    /** Return a cached playable JSON payload for UI attach without re-fetching. */
    @PluginMethod
    public void getPlayableMedia(PluginCall call) {
        String mediaId = call.getString("mediaId");
        if (mediaId == null || mediaId.isEmpty()) {
            call.reject("mediaId required");
            return;
        }
        Context ctx = getContext();
        if (ctx != null) {
            LibraryAutoBridge.getInstance().ensureAppContext(ctx);
        }
        String raw =
            ctx != null
                ? ctx
                    .getSharedPreferences("library_auto_playable", Context.MODE_PRIVATE)
                    .getString(mediaId, null)
                : null;
        if (raw == null || raw.isEmpty()) {
            call.resolve(new JSObject());
            return;
        }
        try {
            call.resolve(new JSObject(raw));
        } catch (Exception e) {
            call.reject("getPlayableMedia failed: " + e.getMessage());
        }
    }

    /**
     * WebView is about to decode audio — release ExoPlayer so we never have
     * two decoders fighting for focus / RAM.
     */
    @PluginMethod
    public void handOffNativePlayback(PluginCall call) {
        LibraryAutoBridge.getInstance().handOffNativeToWebView();
        call.resolve();
    }

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
        // Prefer iconUri over decoded bitmaps — loading every cover at full
        // resolution while resolving a letter folder OOMs mid-playback.
        return parseBrowseChildrenSkipBitmaps(children);
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
            artworkLoadGeneration++;
            cachedArtworkUrl = null;
            // Do not Bitmap.recycle() — MediaSession / notification may still
            // hold the same instance until the next metadata push.
            cachedArtwork = null;
            cancelStickyPlay();
            LibraryAutoBridge.getInstance().clear();
            call.resolve();
            return;
        }

        if (positionOnly) {
            // While native owns PCM, ignore WebView position ticks — they are
            // stale (HTML5 paused) and would thrash MediaSession / cause focus fights.
            if (LibraryAutoBridge.getInstance().isNativeOwningPlayback()) {
                call.resolve();
                return;
            }
            LibraryAutoBridge.getInstance().updatePosition(
                playing,
                Math.round(positionSec * 1000),
                playbackRate
            );
            maybeConfirmStickyPlay(playing);
            call.resolve();
            return;
        }

        if (LibraryAutoBridge.getInstance().isNativeOwningPlayback() && !playing) {
            // Stale WebView "paused" sync while ExoPlayer is active — drop it.
            call.resolve();
            return;
        }

        String title = call.getString("title", "");
        String artist = call.getString("artist", "");
        String album = call.getString("album", "");
        double durationSec = call.getDouble("duration", 0.0);
        final long positionMs = Math.round(positionSec * 1000);
        final long durationMs = Math.round(durationSec * 1000);

        String artworkSrc = null;
        try {
            JSArray artworkArray = call.getArray("artwork");
            if (artworkArray != null) {
                List<JSONObject> artworkList = artworkArray.toList();
                for (JSONObject artworkJson : artworkList) {
                    String src = artworkJson.optString("src", null);
                    if (src != null && !src.isEmpty()) {
                        artworkSrc = src;
                        break;
                    }
                }
            }
        } catch (JSONException ex) {
            Log.w(TAG, "Unable to parse artwork", ex);
        }

        // Fast path: cached art — never block the bridge on network decode.
        if (artworkSrc != null && artworkSrc.equals(cachedArtworkUrl) && cachedArtwork != null) {
            LibraryAutoBridge.getInstance().update(
                title,
                artist,
                album,
                cachedArtwork,
                true,
                playing,
                durationMs,
                positionMs,
                playbackRate
            );
            ensurePlaybackService();
            maybeConfirmStickyPlay(playing);
            call.resolve();
            return;
        }

        // Push metadata immediately without art; decode cover off-thread.
        LibraryAutoBridge.getInstance().update(
            title,
            artist,
            album,
            null,
            true,
            playing,
            durationMs,
            positionMs,
            playbackRate
        );
        ensurePlaybackService();
        maybeConfirmStickyPlay(playing);
        call.resolve();

        if (artworkSrc == null) {
            return;
        }
        final String loadUrl = artworkSrc;
        final String metaTitle = title;
        final String metaArtist = artist;
        final String metaAlbum = album;
        final boolean metaPlaying = playing;
        final float metaRate = playbackRate;
        final int loadGen = ++artworkLoadGeneration;
        new Thread(
            () -> {
                try {
                    Bitmap artwork = getCachedArtwork(loadUrl);
                    if (artwork == null) {
                        return;
                    }
                    mainHandler.post(
                        () -> {
                            if (loadGen != artworkLoadGeneration) {
                                return;
                            }
                            LibraryAutoBridge.getInstance().update(
                                metaTitle,
                                metaArtist,
                                metaAlbum,
                                artwork,
                                true,
                                metaPlaying,
                                durationMs,
                                positionMs,
                                metaRate
                            );
                        }
                    );
                } catch (IOException ex) {
                    Log.w(TAG, "Unable to load artwork", ex);
                }
            },
            "library-artwork"
        ).start();
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
        synchronized (this) {
            if (url != null && url.equals(cachedArtworkUrl) && cachedArtwork != null) {
                return cachedArtwork;
            }
        }
        Bitmap bitmap = urlToBitmap(url);
        if (bitmap != null) {
            synchronized (this) {
                // Drop the prior reference only — never recycle while the
                // MediaSession / notification may still display it.
                cachedArtworkUrl = url;
                cachedArtwork = bitmap;
            }
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
            // Native already started from MediaSession — do NOT sticky-wake the
            // WebView (that path storm-starts Activities and OOMs some OEMs).
            boolean nativeStarted =
                extras != null && extras.getBoolean("nativeStarted", false);
            if (nativeStarted || LibraryAutoBridge.getInstance().isNativeOwningPlayback()) {
                cancelStickyPlay();
                softWakeWebView();
                // Soft deliver once so JS can attach UI — no activity wake storm.
                mainHandler.postDelayed(
                    () -> deliverToJs(action, extras),
                    200
                );
                return;
            }
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
            if (extras.containsKey("nativeStarted")) {
                data.put("nativeStarted", extras.getBoolean("nativeStarted"));
            }
        }
        handler.resolve(data);
    }

    private static final int MAX_ARTWORK_EDGE_PX = 512;

    private Bitmap urlToBitmap(String url) throws IOException {
        if (url == null || url.isEmpty() || url.startsWith("blob:")) {
            return null;
        }

        byte[] bytes;
        if (url.startsWith("http")) {
            HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
            connection.setDoInput(true);
            connection.setConnectTimeout(8000);
            connection.setReadTimeout(8000);
            connection.connect();
            try (InputStream inputStream = connection.getInputStream()) {
                bytes = readAllBytes(inputStream);
            } finally {
                connection.disconnect();
            }
        } else {
            int base64Index = url.indexOf(";base64,");
            if (base64Index == -1) {
                return null;
            }
            String base64Data = url.substring(base64Index + 8);
            bytes = Base64.decode(base64Data, Base64.DEFAULT);
        }

        if (bytes == null || bytes.length == 0) {
            return null;
        }

        BitmapFactory.Options bounds = new BitmapFactory.Options();
        bounds.inJustDecodeBounds = true;
        BitmapFactory.decodeByteArray(bytes, 0, bytes.length, bounds);
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) {
            return null;
        }

        BitmapFactory.Options opts = new BitmapFactory.Options();
        opts.inSampleSize = calculateInSampleSize(
            bounds.outWidth,
            bounds.outHeight,
            MAX_ARTWORK_EDGE_PX,
            MAX_ARTWORK_EDGE_PX
        );
        opts.inPreferredConfig = Bitmap.Config.RGB_565;
        Bitmap decoded = BitmapFactory.decodeByteArray(bytes, 0, bytes.length, opts);
        if (decoded == null) {
            return null;
        }

        int w = decoded.getWidth();
        int h = decoded.getHeight();
        int maxEdge = Math.max(w, h);
        if (maxEdge <= MAX_ARTWORK_EDGE_PX) {
            return decoded;
        }
        float scale = (float) MAX_ARTWORK_EDGE_PX / (float) maxEdge;
        int nw = Math.max(1, Math.round(w * scale));
        int nh = Math.max(1, Math.round(h * scale));
        Bitmap scaled = Bitmap.createScaledBitmap(decoded, nw, nh, true);
        if (scaled != decoded && !decoded.isRecycled()) {
            decoded.recycle();
        }
        return scaled;
    }

    private static int calculateInSampleSize(int width, int height, int reqW, int reqH) {
        int inSampleSize = 1;
        if (height > reqH || width > reqW) {
            int halfH = height / 2;
            int halfW = width / 2;
            while ((halfH / inSampleSize) >= reqH && (halfW / inSampleSize) >= reqW) {
                inSampleSize *= 2;
            }
        }
        return Math.max(1, inSampleSize);
    }

    private static byte[] readAllBytes(InputStream inputStream) throws IOException {
        java.io.ByteArrayOutputStream buffer = new java.io.ByteArrayOutputStream();
        byte[] chunk = new byte[16 * 1024];
        int n;
        // Cap cover download — multi-MB covers are unnecessary for AA/notification.
        final int maxBytes = 2 * 1024 * 1024;
        int total = 0;
        while ((n = inputStream.read(chunk)) != -1) {
            total += n;
            if (total > maxBytes) {
                throw new IOException("Artwork exceeds 2MB download cap");
            }
            buffer.write(chunk, 0, n);
        }
        return buffer.toByteArray();
    }
}
