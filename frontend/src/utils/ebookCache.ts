/**
 * Local ebook cache — downloads PDF/EPUB source files while you read so reopen is instant.
 * Yields bandwidth to active audiobook playback.
 */

import { toAbsoluteUrl } from "../api/instanceUrl";
import {
  cacheStorageKey,
  hasStorageRoom,
  shouldDeferEbookDownload,
  throttleDelay,
  waitForDownloadSlot,
} from "./mediaStorage";

const EBOOK_CACHE = "ebook-files-v1";
const CHUNK_SIZE = 8 * 1024 * 1024;
const START_DELAY_MS = 8_000;
const INTER_CHUNK_DELAY_MS = 250;

const inFlight = new Set<number>();

function cacheSupported(): boolean {
  return typeof caches !== "undefined";
}

function ebookPdfUrl(chapterId: number): string {
  return `/api/library/reader/${chapterId}/pdf`;
}

function ebookFileUrl(chapterId: number): string {
  return `/api/library/reader/${chapterId}/file`;
}

function notifyCacheUpdated(): void {
  window.dispatchEvent(new CustomEvent("ebook-cache-updated"));
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function requestPersistentStorage(): Promise<void> {
  try {
    if (navigator.storage?.persist && !(await navigator.storage.persisted())) {
      await navigator.storage.persist();
    }
  } catch {
    // Best-effort
  }
}

export function readerFileUrlForChapter(chapterId: number, isPdf: boolean): string {
  const path = isPdf ? ebookPdfUrl(chapterId) : ebookFileUrl(chapterId);
  return toAbsoluteUrl(path);
}

export async function isEbookCached(chapterId: number, isPdf = true): Promise<boolean> {
  if (!cacheSupported()) return false;
  try {
    const cache = await caches.open(EBOOK_CACHE);
    const url = cacheStorageKey(readerFileUrlForChapter(chapterId, isPdf));
    return Boolean(await cache.match(url));
  } catch {
    return false;
  }
}

async function fetchChunk(url: string, offset: number): Promise<Response | null> {
  await waitForDownloadSlot();
  const headers = { Range: `bytes=${offset}-${offset + CHUNK_SIZE - 1}` };
  try {
    return await fetch(url, { headers, credentials: "include" });
  } catch {
    await sleep(2_000);
    await waitForDownloadSlot();
    try {
      return await fetch(url, { headers, credentials: "include" });
    } catch {
      return null;
    }
  }
}

async function downloadFile(cache: Cache, url: string): Promise<boolean> {
  const storageKey = cacheStorageKey(url);
  const parts: Blob[] = [];
  let offset = 0;
  let total: number | null = null;
  let contentType = "application/octet-stream";

  while (total == null || offset < total) {
    if (shouldDeferEbookDownload()) {
      await sleep(1_500);
      continue;
    }
    await waitForDownloadSlot();
    if (!(await hasStorageRoom())) {
      console.warn("[ebookCache] stopping — storage quota nearly full");
      return false;
    }

    const resp = await fetchChunk(url, offset);
    if (!resp) return false;

    if (resp.status === 416) {
      total = offset;
      break;
    }
    if (resp.status === 200) {
      const blob = await resp.blob().catch(() => null);
      if (!blob || blob.size === 0) return false;
      parts.length = 0;
      parts.push(blob);
      contentType = resp.headers.get("content-type") || contentType;
      total = blob.size;
      break;
    }
    if (resp.status !== 206) {
      console.warn(`[ebookCache] chunk fetch failed (${resp.status})`);
      return false;
    }

    contentType = resp.headers.get("content-type") || contentType;
    const rangeMatch = /bytes\s+\d+-\d+\/(\d+)/.exec(resp.headers.get("content-range") || "");
    if (rangeMatch) total = Number(rangeMatch[1]);

    const blob = await resp.blob().catch(() => null);
    if (!blob || blob.size === 0) return false;
    parts.push(blob);
    offset += blob.size;
    if (total == null && blob.size < CHUNK_SIZE) total = offset;
    if (total == null || offset < total) {
      await throttleDelay(INTER_CHUNK_DELAY_MS);
      await sleep(INTER_CHUNK_DELAY_MS);
    }
  }

  const full = new Blob(parts, { type: contentType });
  if (full.size === 0) return false;
  try {
    await cache.put(
      storageKey,
      new Response(full, {
        status: 200,
        headers: {
          "Content-Type": contentType,
          "Content-Length": String(full.size),
          "Accept-Ranges": "bytes",
        },
      })
    );
    return true;
  } catch (e) {
    console.warn("[ebookCache] cache.put failed", e);
    return false;
  }
}

export async function cacheBookEbook(chapterId: number, isPdf = true): Promise<void> {
  if (!cacheSupported() || inFlight.has(chapterId)) return;
  inFlight.add(chapterId);

  try {
    await sleep(START_DELAY_MS);
    if (shouldDeferEbookDownload()) {
      await sleep(5_000);
    }
    await requestPersistentStorage();
    const cache = await caches.open(EBOOK_CACHE);
    const url = cacheStorageKey(readerFileUrlForChapter(chapterId, isPdf));
    if (await cache.match(url)) return;
    if (await downloadFile(cache, url)) notifyCacheUpdated();
  } finally {
    inFlight.delete(chapterId);
  }
}

export async function clearEbookCache(chapterId: number): Promise<void> {
  if (!cacheSupported()) return;
  try {
    const cache = await caches.open(EBOOK_CACHE);
    const prefix = `/api/library/reader/${chapterId}/`;
    const keys = await cache.keys();
    await Promise.all(
      keys
        .filter((req) => new URL(req.url).pathname.startsWith(prefix))
        .map((req) => cache.delete(req))
    );
    notifyCacheUpdated();
  } catch {
    // Best-effort
  }
}

export async function clearAllEbookCache(): Promise<void> {
  if (!cacheSupported()) return;
  try {
    await caches.delete(EBOOK_CACHE);
    notifyCacheUpdated();
  } catch {
    // ignore
  }
}

export async function ebookCacheUsageBytes(): Promise<number> {
  if (!cacheSupported()) return 0;
  try {
    const cache = await caches.open(EBOOK_CACHE);
    const keys = await cache.keys();
    let total = 0;
    for (const req of keys) {
      const resp = await cache.match(req);
      if (!resp) continue;
      const len = resp.headers.get("content-length");
      if (len) {
        total += Number(len);
      } else {
        const blob = await resp.blob();
        total += blob.size;
      }
    }
    return total;
  } catch {
    return 0;
  }
}

export async function ebookCacheEntryCount(): Promise<number> {
  if (!cacheSupported()) return 0;
  try {
    const cache = await caches.open(EBOOK_CACHE);
    return (await cache.keys()).length;
  } catch {
    return 0;
  }
}
