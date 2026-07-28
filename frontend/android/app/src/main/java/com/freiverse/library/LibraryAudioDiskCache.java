package com.freiverse.library;

import android.content.Context;
import android.net.Uri;
import android.util.Base64;
import android.util.Log;
import androidx.annotation.Nullable;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Locale;
import org.json.JSONObject;

/**
 * On-disk audiobook track cache for Android WebView + ExoPlayer offline play.
 *
 * Cache API blob URLs are capped (~32MB) to avoid OOM; large Save-offline books
 * land here as append-only files that both {@code <audio>} (via Capacitor
 * convertFileSrc) and native ExoPlayer (file://) can read.
 */
public final class LibraryAudioDiskCache {

    private static final String TAG = "LibraryAudioDisk";
    private static final String DIR_NAME = "audio-disk-cache";

    private LibraryAudioDiskCache() {}

    private static File root(Context ctx) {
        File dir = new File(ctx.getFilesDir(), DIR_NAME);
        if (!dir.exists() && !dir.mkdirs()) {
            Log.w(TAG, "Could not create " + dir);
        }
        return dir;
    }

    /** Stable short filename from the Cache API storage key / track URL. */
    public static String fileKey(String storageKey) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] dig = md.digest(storageKey.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(32);
            for (int i = 0; i < 16; i++) {
                sb.append(String.format(Locale.US, "%02x", dig[i]));
            }
            return sb.toString();
        } catch (Exception e) {
            return Integer.toHexString(storageKey.hashCode());
        }
    }

    private static File metaFile(Context ctx, String key) {
        return new File(root(ctx), key + ".meta.json");
    }

    private static File partialFile(Context ctx, String key) {
        return new File(root(ctx), key + ".partial");
    }

    private static File completeFile(Context ctx, String key) {
        return new File(root(ctx), key + ".bin");
    }

    public static synchronized boolean appendBase64(
        Context ctx,
        String storageKey,
        String base64Data,
        @Nullable String contentType,
        @Nullable Long totalBytes,
        int expectedOffset
    ) throws IOException {
        if (storageKey == null || storageKey.isEmpty() || base64Data == null) {
            return false;
        }
        String key = fileKey(storageKey);
        File complete = completeFile(ctx, key);
        if (complete.isFile() && complete.length() > 0) {
            return true;
        }
        byte[] chunk = Base64.decode(base64Data, Base64.DEFAULT);
        if (chunk.length == 0) {
            return false;
        }
        File partial = partialFile(ctx, key);
        long current = partial.isFile() ? partial.length() : 0;
        if (expectedOffset >= 0 && current != expectedOffset) {
            // Resume mismatch — truncate and let JS restart from 0 next pass.
            if (expectedOffset == 0) {
                // ok to start fresh
                if (partial.exists() && !partial.delete()) {
                    Log.w(TAG, "Could not reset partial " + partial);
                }
                current = 0;
            } else if (current > expectedOffset) {
                // Already have this data (retry) — treat as success.
                writeMeta(ctx, key, contentType, totalBytes, (int) current);
                return true;
            } else {
                Log.w(
                    TAG,
                    "offset mismatch key=" + key + " have=" + current + " want=" + expectedOffset
                );
                return false;
            }
        }
        try (FileOutputStream out = new FileOutputStream(partial, true)) {
            out.write(chunk);
        }
        long now = partial.length();
        writeMeta(ctx, key, contentType, totalBytes, (int) Math.min(now, Integer.MAX_VALUE));
        if (totalBytes != null && totalBytes > 0 && now >= totalBytes) {
            return finalizeKey(ctx, key);
        }
        return true;
    }

    public static synchronized boolean finalizeKey(Context ctx, String key) {
        File partial = partialFile(ctx, key);
        File complete = completeFile(ctx, key);
        if (complete.isFile() && complete.length() > 0) {
            if (partial.exists()) {
                //noinspection ResultOfMethodCallIgnored
                partial.delete();
            }
            return true;
        }
        if (!partial.isFile() || partial.length() == 0) {
            return false;
        }
        JSONObject meta = readMeta(ctx, key);
        long expected = meta != null ? meta.optLong("total", 0) : 0;
        if (expected > 0 && partial.length() < expected) {
            return false;
        }
        if (complete.exists() && !complete.delete()) {
            Log.w(TAG, "Could not replace " + complete);
            return false;
        }
        if (!partial.renameTo(complete)) {
            try (FileInputStream in = new FileInputStream(partial);
                 FileOutputStream out = new FileOutputStream(complete)) {
                byte[] buf = new byte[64 * 1024];
                int n;
                while ((n = in.read(buf)) > 0) {
                    out.write(buf, 0, n);
                }
            } catch (IOException e) {
                Log.w(TAG, "finalize copy failed", e);
                //noinspection ResultOfMethodCallIgnored
                complete.delete();
                return false;
            }
            //noinspection ResultOfMethodCallIgnored
            partial.delete();
        }
        if (meta != null) {
            try {
                meta.put("complete", true);
                meta.put("size", complete.length());
                writeMetaRaw(ctx, key, meta);
            } catch (Exception e) {
                Log.w(TAG, "meta finalize failed", e);
            }
        }
        return complete.isFile() && complete.length() > 0;
    }

    public static synchronized boolean finalizeStorageKey(Context ctx, String storageKey) {
        return finalizeKey(ctx, fileKey(storageKey));
    }

    @Nullable
    public static synchronized Uri getFileUri(Context ctx, String storageKey) {
        String key = fileKey(storageKey);
        File complete = completeFile(ctx, key);
        if (!complete.isFile() || complete.length() == 0) {
            return null;
        }
        return Uri.fromFile(complete);
    }

    public static synchronized long getCompleteSize(Context ctx, String storageKey) {
        File complete = completeFile(ctx, fileKey(storageKey));
        return complete.isFile() ? complete.length() : 0;
    }

    public static synchronized long getPartialSize(Context ctx, String storageKey) {
        String key = fileKey(storageKey);
        File complete = completeFile(ctx, key);
        if (complete.isFile()) {
            return complete.length();
        }
        File partial = partialFile(ctx, key);
        return partial.isFile() ? partial.length() : 0;
    }

    public static synchronized boolean isComplete(Context ctx, String storageKey) {
        return getCompleteSize(ctx, storageKey) > 0;
    }

    public static synchronized void deleteStorageKey(Context ctx, String storageKey) {
        String key = fileKey(storageKey);
        //noinspection ResultOfMethodCallIgnored
        completeFile(ctx, key).delete();
        //noinspection ResultOfMethodCallIgnored
        partialFile(ctx, key).delete();
        //noinspection ResultOfMethodCallIgnored
        metaFile(ctx, key).delete();
    }

    /** Delete all on-disk audio whose storage keys start with prefix (path match). */
    public static synchronized int deleteByUrlPrefix(Context ctx, String urlPrefix) {
        // Keys are hashed — we store original storageKey in meta for prefix deletes.
        File dir = root(ctx);
        File[] files = dir.listFiles();
        if (files == null || urlPrefix == null || urlPrefix.isEmpty()) {
            return 0;
        }
        int removed = 0;
        for (File f : files) {
            String name = f.getName();
            if (!name.endsWith(".meta.json")) {
                continue;
            }
            String key = name.substring(0, name.length() - ".meta.json".length());
            JSONObject meta = readMeta(ctx, key);
            if (meta == null) {
                continue;
            }
            String sk = meta.optString("storageKey", "");
            if (sk.isEmpty() || !sk.contains(urlPrefix)) {
                continue;
            }
            //noinspection ResultOfMethodCallIgnored
            completeFile(ctx, key).delete();
            //noinspection ResultOfMethodCallIgnored
            partialFile(ctx, key).delete();
            //noinspection ResultOfMethodCallIgnored
            f.delete();
            removed++;
        }
        return removed;
    }

    public static synchronized void clearAll(Context ctx) {
        File dir = root(ctx);
        File[] files = dir.listFiles();
        if (files == null) {
            return;
        }
        for (File f : files) {
            //noinspection ResultOfMethodCallIgnored
            f.delete();
        }
    }

    /**
     * Stream Cache-API-sourced base64 parts already held by JS into a complete file
     * without keeping the whole audiobook in JVM heap at once (JS sends one part).
     */
    public static synchronized boolean writeFullBase64Replace(
        Context ctx,
        String storageKey,
        String base64Data,
        @Nullable String contentType
    ) throws IOException {
        if (storageKey == null || base64Data == null) {
            return false;
        }
        String key = fileKey(storageKey);
        deleteStorageKey(ctx, storageKey);
        byte[] data = Base64.decode(base64Data, Base64.DEFAULT);
        if (data.length == 0) {
            return false;
        }
        File complete = completeFile(ctx, key);
        try (FileOutputStream out = new FileOutputStream(complete)) {
            out.write(data);
        }
        writeMeta(ctx, key, contentType, (long) data.length, data.length);
        JSONObject meta = readMeta(ctx, key);
        if (meta != null) {
            try {
                meta.put("storageKey", storageKey);
                meta.put("complete", true);
                meta.put("size", data.length);
                writeMetaRaw(ctx, key, meta);
            } catch (Exception ignored) {
            }
        }
        return true;
    }

    private static void writeMeta(
        Context ctx,
        String key,
        @Nullable String contentType,
        @Nullable Long totalBytes,
        int size
    ) {
        JSONObject meta = readMeta(ctx, key);
        if (meta == null) {
            meta = new JSONObject();
        }
        try {
            if (contentType != null && !contentType.isEmpty()) {
                meta.put("contentType", contentType);
            }
            if (totalBytes != null && totalBytes > 0) {
                meta.put("total", totalBytes);
            }
            meta.put("size", size);
            meta.put("complete", false);
            writeMetaRaw(ctx, key, meta);
        } catch (Exception e) {
            Log.w(TAG, "writeMeta failed", e);
        }
    }

    public static synchronized void setStorageKeyMeta(
        Context ctx,
        String storageKey,
        @Nullable String contentType,
        @Nullable Long totalBytes
    ) {
        String key = fileKey(storageKey);
        JSONObject meta = readMeta(ctx, key);
        if (meta == null) {
            meta = new JSONObject();
        }
        try {
            meta.put("storageKey", storageKey);
            if (contentType != null && !contentType.isEmpty()) {
                meta.put("contentType", contentType);
            }
            if (totalBytes != null && totalBytes > 0) {
                meta.put("total", totalBytes);
            }
            writeMetaRaw(ctx, key, meta);
        } catch (Exception e) {
            Log.w(TAG, "setStorageKeyMeta failed", e);
        }
    }

    private static void writeMetaRaw(Context ctx, String key, JSONObject meta) throws IOException {
        File f = metaFile(ctx, key);
        try (FileOutputStream out = new FileOutputStream(f)) {
            out.write(meta.toString().getBytes(StandardCharsets.UTF_8));
        }
    }

    @Nullable
    private static JSONObject readMeta(Context ctx, String key) {
        File f = metaFile(ctx, key);
        if (!f.isFile()) {
            return null;
        }
        try (FileInputStream in = new FileInputStream(f)) {
            byte[] buf = new byte[(int) Math.min(f.length(), 64 * 1024)];
            int off = 0;
            int n;
            while (off < buf.length && (n = in.read(buf, off, buf.length - off)) > 0) {
                off += n;
            }
            return new JSONObject(new String(buf, 0, off, StandardCharsets.UTF_8));
        } catch (Exception e) {
            return null;
        }
    }
}
