/**
 * Offline book cover cache - store cover bytes in Cache API while online so
 * shelf / player artwork still render with no network.
 */
import { toAbsoluteUrl } from "../api/instanceUrl";
import { cacheStorageKey } from "./mediaStorage";

const COVER_CACHE = "book-covers-v1";

function cacheSupported(): boolean {
  return typeof caches !== "undefined";
}

function notifyCoverCacheUpdated(): void {
  window.dispatchEvent(new CustomEvent("cover-cache-updated"));
}

export function coverCacheKey(url: string): string {
  return cacheStorageKey(toAbsoluteUrl(url));
}

export async function isCoverCached(url: string): Promise<boolean> {
  if (!url?.trim() || !cacheSupported()) return false;
  try {
    const cache = await caches.open(COVER_CACHE);
    const key = coverCacheKey(url);
    return Boolean((await cache.match(key)) || (await cache.match(url)));
  } catch {
    return false;
  }
}

/** Fetch and persist a cover. Returns true when cached (already or newly). */
export async function cacheCover(url: string): Promise<boolean> {
  const trimmed = (url || "").trim();
  if (!trimmed || !cacheSupported()) return false;
  const abs = toAbsoluteUrl(trimmed);
  const key = coverCacheKey(abs);
  try {
    const cache = await caches.open(COVER_CACHE);
    if ((await cache.match(key)) || (await cache.match(abs))) return true;
    const resp = await fetch(abs, { credentials: "include", mode: "cors" });
    if (!resp.ok) return false;
    const blob = await resp.blob();
    if (!blob.size) return false;
    const headers = new Headers(resp.headers);
    if (!headers.get("content-type")) {
      headers.set("content-type", blob.type || "image/jpeg");
    }
    await cache.put(key, new Response(blob, { status: 200, headers }));
    notifyCoverCacheUpdated();
    return true;
  } catch {
    return false;
  }
}

/**
 * Object URL for a cached cover, or null.
 * Caller must revoke via URL.revokeObjectURL when done.
 */
export async function getCachedCoverObjectUrl(url: string): Promise<string | null> {
  if (!url?.trim() || !cacheSupported()) return null;
  try {
    const cache = await caches.open(COVER_CACHE);
    const key = coverCacheKey(url);
    const match = (await cache.match(key)) || (await cache.match(toAbsoluteUrl(url)));
    if (!match) return null;
    const blob = await match.blob();
    if (!blob.size) return null;
    return URL.createObjectURL(blob);
  } catch {
    return null;
  }
}

/** Prefer cached blob URL; fall back to absolute network URL. */
export async function resolveCoverDisplayUrl(url: string): Promise<string> {
  const trimmed = (url || "").trim();
  if (!trimmed) return "";
  const cached = await getCachedCoverObjectUrl(trimmed);
  if (cached) return cached;
  return toAbsoluteUrl(trimmed);
}

export async function clearCover(url: string): Promise<void> {
  if (!url?.trim() || !cacheSupported()) return;
  try {
    const cache = await caches.open(COVER_CACHE);
    const key = coverCacheKey(url);
    await cache.delete(key);
    await cache.delete(toAbsoluteUrl(url));
    notifyCoverCacheUpdated();
  } catch {
    /* best-effort */
  }
}

/** Clear ABS proxy covers for an item id. */
export async function clearAbsCoverCache(itemId: string): Promise<void> {
  if (!itemId?.trim() || !cacheSupported()) return;
  try {
    const cache = await caches.open(COVER_CACHE);
    const keys = await cache.keys();
    await Promise.all(
      keys
        .filter((req) => {
          try {
            return new URL(req.url).pathname.includes(`/api/stream/abs/proxy/cover/${itemId}`);
          } catch {
            return req.url.includes(`/api/stream/abs/proxy/cover/${itemId}`);
          }
        })
        .map((req) => cache.delete(req))
    );
    notifyCoverCacheUpdated();
  } catch {
    /* best-effort */
  }
}

export async function clearAllCoverCache(): Promise<void> {
  if (!cacheSupported()) return;
  try {
    await caches.delete(COVER_CACHE);
    notifyCoverCacheUpdated();
  } catch {
    /* ignore */
  }
}