/**
 * Android on-disk audiobook cache — large tracks that cannot become blob URLs
 * (32MB RAM cap) are appended to app files and played via Capacitor file URLs
 * / ExoPlayer file:// URIs.
 */
import { Capacitor } from "@capacitor/core";
import { LibraryAuto } from "../media/libraryAutoPlugin";
import { cacheStorageKey } from "./mediaStorage";

function isAndroidNative(): boolean {
  try {
    return Capacitor.getPlatform() === "android" && Capacitor.isNativePlatform();
  } catch {
    return false;
  }
}

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error("FileReader failed"));
    reader.onload = () => {
      const result = String(reader.result || "");
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(blob);
  });
}

export async function androidDiskCacheEnabled(): Promise<boolean> {
  return isAndroidNative();
}

export async function androidDiskIsComplete(url: string): Promise<boolean> {
  if (!isAndroidNative() || !url) return false;
  try {
    const st = await LibraryAuto.getAudioDiskCacheUri({
      storageKey: cacheStorageKey(url),
    });
    return Boolean(st.complete && st.uri);
  } catch {
    return false;
  }
}

/** file:// URI for ExoPlayer, or null. */
export async function androidDiskFileUri(url: string): Promise<string | null> {
  if (!isAndroidNative() || !url) return null;
  try {
    const st = await LibraryAuto.getAudioDiskCacheUri({
      storageKey: cacheStorageKey(url),
    });
    if (st.complete && st.uri) return st.uri;
  } catch {
    /* ignore */
  }
  return null;
}

/** WebView-playable URL (Capacitor converted), or null. */
export async function androidDiskWebSrc(url: string): Promise<string | null> {
  const fileUri = await androidDiskFileUri(url);
  if (!fileUri) return null;
  try {
    // file:///data/... → https://localhost/_capacitor_file_/...
    return Capacitor.convertFileSrc(fileUri);
  } catch {
    return fileUri;
  }
}

export async function androidDiskPartialSize(url: string): Promise<number> {
  if (!isAndroidNative() || !url) return 0;
  try {
    const st = await LibraryAuto.getAudioDiskCacheUri({
      storageKey: cacheStorageKey(url),
    });
    return Number(st.partialSize || st.size || 0);
  } catch {
    return 0;
  }
}

export async function androidDiskAppendChunk(
  url: string,
  blob: Blob,
  opts: { offset: number; total: number | null; contentType: string }
): Promise<boolean> {
  if (!isAndroidNative() || !url || blob.size === 0) return false;
  try {
    const data = await blobToBase64(blob);
    const res = await LibraryAuto.appendAudioDiskCache({
      storageKey: cacheStorageKey(url),
      data,
      contentType: opts.contentType,
      total: opts.total ?? undefined,
      offset: opts.offset,
    });
    return Boolean(res.ok);
  } catch (e) {
    console.warn("[audioDiskCache] append failed", e);
    return false;
  }
}

export async function androidDiskFinalize(url: string): Promise<string | null> {
  if (!isAndroidNative() || !url) return null;
  try {
    const res = await LibraryAuto.finalizeAudioDiskCache({
      storageKey: cacheStorageKey(url),
    });
    if (res.ok && res.uri) {
      try {
        return Capacitor.convertFileSrc(res.uri);
      } catch {
        return res.uri;
      }
    }
  } catch (e) {
    console.warn("[audioDiskCache] finalize failed", e);
  }
  return null;
}

export async function androidDiskDeleteForUrl(url: string): Promise<void> {
  if (!isAndroidNative() || !url) return;
  try {
    await LibraryAuto.deleteAudioDiskCache({ storageKey: cacheStorageKey(url) });
  } catch {
    /* ignore */
  }
}

export async function androidDiskDeleteByPathPrefix(prefix: string): Promise<void> {
  if (!isAndroidNative() || !prefix) return;
  try {
    await LibraryAuto.deleteAudioDiskCache({ urlPrefix: prefix });
  } catch {
    /* ignore */
  }
}

export async function androidDiskClearAll(): Promise<void> {
  if (!isAndroidNative()) return;
  try {
    await LibraryAuto.deleteAudioDiskCache({ all: true });
  } catch {
    /* ignore */
  }
}

/**
 * Materialize Cache API parts onto disk without assembling a giant Blob in JS.
 * Used when a book was "downloaded" under the old 32MB blob cap.
 */
export async function androidDiskMaterializeFromParts(
  storageKey: string,
  partFetcher: (index: number) => Promise<Blob | null>,
  contentType: string,
  total: number | null
): Promise<string | null> {
  if (!isAndroidNative()) return null;
  try {
    const existing = await LibraryAuto.getAudioDiskCacheUri({ storageKey });
    if (existing.complete && existing.uri) {
      return Capacitor.convertFileSrc(existing.uri);
    }
  } catch {
    /* continue */
  }

  let offset = 0;
  for (let i = 0; ; i++) {
    const blob = await partFetcher(i);
    if (!blob || blob.size === 0) break;
    const data = await blobToBase64(blob);
    const res = await LibraryAuto.appendAudioDiskCache({
      storageKey,
      data,
      contentType,
      total: total ?? undefined,
      offset,
    });
    if (!res.ok) return null;
    offset += blob.size;
  }
  if (offset === 0) return null;
  const fin = await LibraryAuto.finalizeAudioDiskCache({ storageKey });
  if (fin.ok && fin.uri) {
    try {
      return Capacitor.convertFileSrc(fin.uri);
    } catch {
      return fin.uri;
    }
  }
  return null;
}
