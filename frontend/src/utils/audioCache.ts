/**
 * Local audiobook cache — downloads tracks while you listen so resume is instant.
 * Works for debrid streams (/api/stream/rd/proxy/…) and ABS library playback
 * (/api/stream/abs/proxy/audio/…).
 *
 * Design notes:
 * - Every 8 MB chunk is persisted to the Cache API the moment it arrives
 *   (keyed as `<track>?audioCachePart=N`), so a dropped connection, app kill,
 *   or page reload never loses progress — the next attempt resumes mid-file.
 * - When all chunks are present they are assembled into one canonical entry
 *   (cheap: a multi-part Blob references the on-disk chunks) and the parts are
 *   deleted.
 * - Playback should NOT rely on the service worker intercepting <audio>
 *   requests: Android WebView routes media element traffic around service
 *   workers. Use getCachedTrackObjectUrl() to play the local copy directly.
 */

import {
  cacheStorageKey,
  hasStorageRoom,
  throttleDelay,
  waitForDownloadSlot,
} from "./mediaStorage";

const AUDIO_CACHE = "audio-tracks-v1";
/** Download in resumable ranged chunks so a failure only loses one chunk. */
const CHUNK_SIZE = 8 * 1024 * 1024;
/** Head start for the playback buffer before background downloading begins. */
const START_DELAY_MS = 8_000;
/** Small gap between chunks so playback traffic is never fully starved. */
const INTER_CHUNK_DELAY_MS = 250;
/** Attempts per chunk before giving up on the track (for this pass). */
const CHUNK_RETRIES = 3;
/** Query param marking a partially-downloaded chunk entry. */
const PART_PARAM = "audioCachePart";
/** Chunk header carrying the full file size so resumes know the target. */
const TOTAL_HEADER = "x-audio-total-size";

export interface CacheableTrack {
  contentUrl: string;
}

const inFlight = new Set<string>();

let lastCacheError: string | null = null;

export { setMediaDownloadThrottled as setAudioCachePaused } from "./mediaStorage";

/** Most recent download failure reason (for the Settings diagnostics line). */
export function audioCacheLastError(): string | null {
  return lastCacheError;
}

function setLastError(msg: string | null): void {
  lastCacheError = msg;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function cacheSupported(): boolean {
  return typeof caches !== "undefined";
}

function isCacheableUrl(url: string): boolean {
  return (
    url.includes("/api/stream/rd/proxy/") ||
    url.includes("/api/stream/abs/proxy/audio/")
  );
}

function partKey(storageKey: string, index: number): string {
  return `${storageKey}?${PART_PARAM}=${index}`;
}

function isPartUrl(url: string): boolean {
  return url.includes(`${PART_PARAM}=`);
}

/** "/api/stream/rd/proxy/h/12/0/sig" -> "/api/stream/rd/proxy/h/12/" */
function rowPrefixFromTrackUrl(url: string): string | null {
  const rd = /(\/api\/stream\/rd\/proxy\/[hl]\/\d+\/)/.exec(url);
  if (rd) return rd[1];
  const abs = /(\/api\/stream\/abs\/proxy\/audio\/[^/]+\/)/.exec(url);
  return abs ? abs[1] : null;
}

function notifyCacheUpdated(): void {
  window.dispatchEvent(new CustomEvent("audio-cache-updated"));
}

async function cacheHasUrl(cache: Cache, url: string): Promise<boolean> {
  const key = cacheStorageKey(url);
  if (await cache.match(key)) return true;
  return Boolean(await cache.match(url));
}

export async function isBookCached(tracks: CacheableTrack[]): Promise<boolean> {
  if (!cacheSupported() || tracks.length === 0) return false;
  try {
    const cache = await caches.open(AUDIO_CACHE);
    for (const t of tracks) {
      if (!t.contentUrl || !isCacheableUrl(t.contentUrl)) return false;
      if (!(await cacheHasUrl(cache, t.contentUrl))) return false;
    }
    return true;
  } catch {
    return false;
  }
}

/**
 * Object URL for a fully-downloaded track, or null when not cached.
 * Callers own the URL and must revoke it via URL.revokeObjectURL().
 * This is the playback path — Android WebView media requests bypass the
 * service worker, so the player must read the cache directly.
 */
export async function getCachedTrackObjectUrl(url: string): Promise<string | null> {
  if (!cacheSupported() || !url || !isCacheableUrl(url)) return null;
  try {
    const cache = await caches.open(AUDIO_CACHE);
    const resp =
      (await cache.match(cacheStorageKey(url))) || (await cache.match(url));
    if (!resp) return null;
    const blob = await resp.blob();
    if (blob.size === 0) return null;
    return URL.createObjectURL(blob);
  } catch {
    return null;
  }
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

async function fetchChunk(url: string, offset: number): Promise<Response | null> {
  const headers = { Range: `bytes=${offset}-${offset + CHUNK_SIZE - 1}` };
  for (let attempt = 0; attempt < CHUNK_RETRIES; attempt++) {
    await waitForDownloadSlot();
    try {
      return await fetch(url, { headers, credentials: "same-origin" });
    } catch {
      if (attempt < CHUNK_RETRIES - 1) await sleep(2_000 * (attempt + 1));
    }
  }
  return null;
}

interface PartsState {
  nextIndex: number;
  offset: number;
  total: number | null;
  contentType: string;
}

/** Scan persisted chunks so an interrupted download resumes where it stopped. */
async function loadExistingParts(cache: Cache, storageKey: string): Promise<PartsState> {
  const state: PartsState = {
    nextIndex: 0,
    offset: 0,
    total: null,
    contentType: "audio/mpeg",
  };
  for (;;) {
    const existing = await cache.match(partKey(storageKey, state.nextIndex));
    if (!existing) break;
    let size = Number(existing.headers.get("content-length") || 0);
    if (!size) {
      try {
        size = (await existing.clone().blob()).size;
      } catch {
        break;
      }
    }
    const total = Number(existing.headers.get(TOTAL_HEADER) || 0);
    if (total > 0) state.total = total;
    const ct = existing.headers.get("content-type");
    if (ct) state.contentType = ct;
    state.offset += size;
    state.nextIndex++;
  }
  return state;
}

async function putPart(
  cache: Cache,
  storageKey: string,
  index: number,
  blob: Blob,
  contentType: string,
  total: number | null
): Promise<boolean> {
  try {
    await cache.put(
      partKey(storageKey, index),
      new Response(blob, {
        status: 200,
        headers: {
          "Content-Type": contentType,
          "Content-Length": String(blob.size),
          [TOTAL_HEADER]: total != null ? String(total) : "",
        },
      })
    );
    return true;
  } catch (e) {
    console.warn("[audioCache] part write failed", e);
    setLastError("Could not write to local storage (quota full?)");
    return false;
  }
}

async function deleteParts(cache: Cache, storageKey: string, count: number): Promise<void> {
  for (let i = 0; i < count; i++) {
    await cache.delete(partKey(storageKey, i));
  }
}

/** Assemble persisted chunks into the canonical full-track cache entry. */
async function assembleParts(
  cache: Cache,
  storageKey: string,
  partCount: number,
  contentType: string
): Promise<boolean> {
  const parts: Blob[] = [];
  for (let i = 0; i < partCount; i++) {
    const resp = await cache.match(partKey(storageKey, i));
    if (!resp) return false;
    try {
      parts.push(await resp.blob());
    } catch {
      return false;
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
  } catch (e) {
    console.warn("[audioCache] cache.put failed", e);
    setLastError("Could not store the finished track (quota full?)");
    return false;
  }
  await deleteParts(cache, storageKey, partCount);
  return true;
}

/**
 * Download a single track in ranged chunks, persisting each chunk immediately.
 * Returns true when the fully assembled track ended up in the cache.
 */
async function downloadTrack(cache: Cache, url: string): Promise<boolean> {
  const storageKey = cacheStorageKey(url);
  const state = await loadExistingParts(cache, storageKey);

  while (state.total == null || state.offset < state.total) {
    await waitForDownloadSlot();
    if (!(await hasStorageRoom(CHUNK_SIZE))) {
      console.warn("[audioCache] stopping — storage quota nearly full");
      setLastError("Storage quota nearly full — downloads paused");
      return false;
    }

    const resp = await fetchChunk(url, state.offset);
    if (!resp) {
      setLastError("Network error while downloading — will resume next play");
      return false;
    }

    if (resp.status === 416) {
      // Read past the end — everything we have is the whole file.
      state.total = state.offset;
      break;
    }
    if (resp.status === 200) {
      // Server ignored the Range header and sent the whole file.
      const blob = await resp.blob().catch(() => null);
      if (!blob || blob.size === 0) return false;
      await deleteParts(cache, storageKey, state.nextIndex);
      state.contentType = resp.headers.get("content-type") || state.contentType;
      if (!(await putPart(cache, storageKey, 0, blob, state.contentType, blob.size))) {
        return false;
      }
      state.nextIndex = 1;
      state.offset = blob.size;
      state.total = blob.size;
      break;
    }
    if (resp.status !== 206) {
      console.warn(`[audioCache] chunk fetch failed (${resp.status})`);
      setLastError(`Server returned ${resp.status} while downloading`);
      return false;
    }

    state.contentType = resp.headers.get("content-type") || state.contentType;
    const rangeMatch = /bytes\s+\d+-\d+\/(\d+)/.exec(resp.headers.get("content-range") || "");
    if (rangeMatch) state.total = Number(rangeMatch[1]);

    const blob = await resp.blob().catch(() => null);
    if (!blob || blob.size === 0) {
      setLastError("Empty chunk received — will resume next play");
      return false;
    }
    if (!(await putPart(cache, storageKey, state.nextIndex, blob, state.contentType, state.total))) {
      return false;
    }
    state.nextIndex++;
    state.offset += blob.size;
    if (state.total == null && blob.size < CHUNK_SIZE) state.total = state.offset;

    notifyCacheUpdated();

    if (state.total == null || state.offset < state.total) {
      await throttleDelay(INTER_CHUNK_DELAY_MS);
      await sleep(INTER_CHUNK_DELAY_MS);
    }
  }

  const ok = await assembleParts(cache, storageKey, state.nextIndex, state.contentType);
  if (ok) setLastError(null);
  return ok;
}

export async function cacheBookAudio(
  tracks: CacheableTrack[],
  onProgress?: (done: number, total: number) => void,
): Promise<void> {
  if (!cacheSupported() || tracks.length === 0) return;
  const cacheable = tracks.filter((t) => t.contentUrl && isCacheableUrl(t.contentUrl));
  if (cacheable.length === 0) return;

  const prefix = rowPrefixFromTrackUrl(cacheable[0].contentUrl) || cacheable[0].contentUrl;
  if (inFlight.has(prefix)) return;
  inFlight.add(prefix);

  try {
    await sleep(START_DELAY_MS);
    await requestPersistentStorage();
    const cache = await caches.open(AUDIO_CACHE);
    let done = 0;
    for (const t of tracks) {
      const url = t.contentUrl ? cacheStorageKey(t.contentUrl) : "";
      if (!isCacheableUrl(url)) {
        done++;
        continue;
      }
      const already = await cacheHasUrl(cache, url);
      if (already) {
        done++;
        onProgress?.(done, tracks.length);
        continue;
      }
      if (await downloadTrack(cache, url)) {
        notifyCacheUpdated();
      }
      done++;
      onProgress?.(done, tracks.length);
    }
  } finally {
    inFlight.delete(prefix);
  }
}

export async function clearBookCacheForTracks(tracks: CacheableTrack[]): Promise<void> {
  const first = tracks.find((t) => t.contentUrl && rowPrefixFromTrackUrl(t.contentUrl));
  if (!first) return;
  const url = first.contentUrl;
  const rd = /\/proxy\/([hl])\/(\d+)\//.exec(url);
  if (rd) {
    await clearBookCache(rd[1] as "h" | "l", Number(rd[2]));
    return;
  }
  const abs = /\/proxy\/audio\/([^/]+)\//.exec(url);
  if (abs) await clearAbsBookCache(abs[1]);
}

async function clearByPathPrefix(prefix: string): Promise<void> {
  if (!cacheSupported()) return;
  try {
    const cache = await caches.open(AUDIO_CACHE);
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

export async function clearBookCache(kind: "h" | "l", rowId: number): Promise<void> {
  await clearByPathPrefix(`/api/stream/rd/proxy/${kind}/${rowId}/`);
}

export async function clearAbsBookCache(itemId: string): Promise<void> {
  await clearByPathPrefix(`/api/stream/abs/proxy/audio/${itemId}/`);
}

export async function clearAllAudioCache(): Promise<void> {
  if (!cacheSupported()) return;
  try {
    await caches.delete(AUDIO_CACHE);
    notifyCacheUpdated();
  } catch {
    // ignore
  }
}

/** Total bytes on disk — includes in-progress chunks so progress is visible. */
export async function audioCacheUsageBytes(): Promise<number> {
  if (!cacheSupported()) return 0;
  try {
    const cache = await caches.open(AUDIO_CACHE);
    const keys = await cache.keys();
    let total = 0;
    for (const req of keys) {
      const resp = await cache.match(req);
      if (!resp) continue;
      const len = resp.headers.get("content-length");
      if (len) {
        total += Number(len);
      } else {
        try {
          total += (await resp.blob()).size;
        } catch {
          /* skip */
        }
      }
    }
    return total;
  } catch {
    return 0;
  }
}

/** Number of fully downloaded tracks (in-progress chunk entries excluded). */
export async function audioCacheEntryCount(): Promise<number> {
  if (!cacheSupported()) return 0;
  try {
    const cache = await caches.open(AUDIO_CACHE);
    const keys = await cache.keys();
    return keys.filter((req) => !isPartUrl(req.url)).length;
  } catch {
    return 0;
  }
}
