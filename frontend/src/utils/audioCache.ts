/**
 * Local audiobook cache — downloads tracks while you listen so resume is instant.
 * Works for debrid streams (/api/stream/rd/proxy/…) and ABS library playback
 * (/api/stream/abs/proxy/audio/…).
 *
 * Design notes:
 * - Every chunk is persisted to the Cache API the moment it arrives
 *   (keyed as `<track>?audioCachePart=N`), so a dropped connection, app kill,
 *   or page reload never loses progress — the next attempt resumes mid-file.
 * - When all chunks are present they are assembled into one canonical entry
 *   and the parts are deleted.
 * - Playback should NOT rely on the service worker intercepting <audio>
 *   requests: Android WebView routes media element traffic around service
 *   workers. Use getCachedTrackObjectUrl() to play the local copy directly.
 * - Background downloads NEVER compete with active playback during the warmup
 *   window or while the audio element is buffering.
 */

import { toAbsoluteUrl } from "../api/instanceUrl";
import {
  cacheStorageKey,
  hasStorageRoom,
  shouldDeferCacheDownload,
  throttleDelay,
  waitForDownloadSlot,
} from "./mediaStorage";

const AUDIO_CACHE = "audio-tracks-v1";
/** Smaller chunks = less bandwidth hogging per request on a Pi link. */
const CHUNK_SIZE = 2 * 1024 * 1024;
/** Let playback establish its buffer before any background download starts. */
const START_DELAY_MS = 12_000;
/** Gap between chunks so playback traffic is never fully starved. */
const INTER_CHUNK_DELAY_MS = 800;
/** Attempts per chunk (connection AND body read) before failing the track pass. */
const CHUNK_RETRIES = 5;
/** Extra passes over tracks that failed. */
const MAX_TRACK_PASSES = 12;
/** Pause between retry passes when no progress was made on disk. */
const PASS_RETRY_DELAY_MS = 8_000;
/** Faster retry when we already have partial chunks saved. */
const PASS_RETRY_PARTIAL_MS = 2_000;
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

async function cachedResponseMeta(
  url: string
): Promise<{ resp: Response; size: number } | null> {
  if (!cacheSupported() || !url || !isCacheableUrl(url)) return null;
  const cache = await caches.open(AUDIO_CACHE);
  const key = cacheStorageKey(url);
  const resp = (await cache.match(key)) || (await cache.match(url));
  if (!resp) return null;
  const len = Number(resp.headers.get("content-length") || 0);
  if (len > 0) return { resp, size: len };
  try {
    const blob = await resp.clone().blob();
    if (blob.size === 0) return null;
    return { resp, size: blob.size };
  } catch {
    return null;
  }
}

/** Fast check (no blob read) — is the full track on disk? */
export async function isTrackFullyCached(url: string): Promise<boolean> {
  if (!cacheSupported() || !url || !isCacheableUrl(url)) return false;
  try {
    const cache = await caches.open(AUDIO_CACHE);
    const key = cacheStorageKey(url);
    if (await cache.match(key)) return true;
    const state = await loadExistingParts(cache, key);
    return (
      state.nextIndex > 0 &&
      state.total != null &&
      state.total > 0 &&
      state.offset >= state.total
    );
  } catch {
    return false;
  }
}

/** Build a blob URL from persisted chunk parts when assembly hasn't run yet. */
async function objectUrlFromParts(storageKey: string): Promise<string | null> {
  const cache = await caches.open(AUDIO_CACHE);
  const state = await loadExistingParts(cache, storageKey);
  if (state.nextIndex === 0) return null;
  if (state.total != null && state.offset < state.total) return null;

  const parts: Blob[] = [];
  for (let i = 0; i < state.nextIndex; i++) {
    const resp = await cache.match(partKey(storageKey, i));
    if (!resp) return null;
    const blob = await resp.blob();
    if (blob.size === 0) return null;
    parts.push(blob);
  }
  const full = new Blob(parts, { type: state.contentType });
  if (full.size === 0) return null;
  return URL.createObjectURL(full);
}

/**
 * Object URL for a fully-downloaded track, or null when not cached.
 * Callers own the URL and must revoke it via URL.revokeObjectURL().
 *
 * Android WebView cannot read the Cache API through a service worker for
 * <audio> — this blob URL is the only offline playback path on native.
 */
export async function getCachedTrackObjectUrl(url: string): Promise<string | null> {
  if (!cacheSupported() || !url || !isCacheableUrl(url)) return null;
  try {
    const meta = await cachedResponseMeta(url);
    if (meta) {
      const blob = await meta.resp.blob();
      if (blob.size > 0) return URL.createObjectURL(blob);
    }
    return await objectUrlFromParts(cacheStorageKey(url));
  } catch {
    return null;
  }
}

/**
 * Prefer cache when the track is fully local; otherwise use the stream URL.
 * When `cached` is returned, `revoke` must be called via URL.revokeObjectURL.
 */
export async function resolvePlaybackSource(
  url: string
): Promise<{ src: string; cached: boolean; revoke?: () => void }> {
  if (await isTrackFullyCached(url)) {
    const objectUrl = await getCachedTrackObjectUrl(url);
    if (objectUrl) {
      return {
        src: objectUrl,
        cached: true,
        revoke: () => URL.revokeObjectURL(objectUrl),
      };
    }
  }
  return { src: url, cached: false };
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

interface ChunkResult {
  status: 200 | 206;
  blob: Blob;
  contentType: string | null;
  totalFromRange: number | null;
}

/**
 * Fetch one ranged chunk INCLUDING its body, retrying on any failure.
 * The body read must be inside the retry loop: a proxy stream that dies
 * mid-transfer rejects blob() or yields an empty blob.
 */
function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...(extra || {}) };
  try {
    const token = localStorage.getItem("access_token");
    if (token) headers.Authorization = `Bearer ${token}`;
  } catch {
    /* ignore */
  }
  return headers;
}

async function fetchChunkWithBody(
  url: string,
  offset: number,
  opts?: { force?: boolean }
): Promise<ChunkResult | "eof" | null> {
  const absoluteUrl = toAbsoluteUrl(url);
  const headers = authHeaders({ Range: `bytes=${offset}-${offset + CHUNK_SIZE - 1}` });
  for (let attempt = 0; attempt < CHUNK_RETRIES; attempt++) {
    if (attempt > 0) await sleep(Math.min(12_000, 1_500 * 2 ** attempt));
    if (!opts?.force) {
      await waitForDownloadSlot();
      if (shouldDeferCacheDownload()) {
        // Playback reclaimed bandwidth — back off and let the outer loop retry.
        return null;
      }
    }
    try {
      // Native APK: absolute URL is cross-origin from https://localhost.
      // Bearer covers cases where cookies are not sent cross-origin.
      const resp = await fetch(absoluteUrl, { headers, credentials: "include" });
      if (resp.status === 416) return "eof";
      if (resp.status !== 206 && resp.status !== 200) {
        setLastError(`Server returned ${resp.status} while downloading`);
        continue;
      }
      const blob = await resp.blob();
      if (blob.size === 0) {
        setLastError("Empty chunk received — retrying");
        continue;
      }
      const rangeMatch = /bytes\s+\d+-\d+\/(\d+)/.exec(
        resp.headers.get("content-range") || ""
      );
      return {
        status: resp.status as 200 | 206,
        blob,
        contentType: resp.headers.get("content-type"),
        totalFromRange: rangeMatch ? Number(rangeMatch[1]) : null,
      };
    } catch {
      setLastError("Network error while downloading — retrying");
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
    if (size === 0) {
      // Corrupt empty part — delete and resume from here.
      await cache.delete(partKey(storageKey, state.nextIndex));
      break;
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
      const blob = await resp.blob();
      if (blob.size === 0) return false;
      parts.push(blob);
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
async function downloadTrack(
  cache: Cache,
  url: string,
  opts?: { force?: boolean }
): Promise<boolean> {
  const storageKey = cacheStorageKey(url);
  const state = await loadExistingParts(cache, storageKey);

  while (state.total == null || state.offset < state.total) {
    if (!opts?.force) {
      await waitForDownloadSlot();
      if (shouldDeferCacheDownload()) return false;
    }

    if (!(await hasStorageRoom(CHUNK_SIZE))) {
      console.warn("[audioCache] stopping — storage quota nearly full");
      setLastError("Storage quota nearly full — downloads paused");
      return false;
    }

    const chunk = await fetchChunkWithBody(url, state.offset, opts);
    if (chunk === "eof") {
      state.total = state.offset;
      break;
    }
    if (!chunk) {
      setLastError("Download paused for playback — will retry");
      return false;
    }

    if (chunk.status === 200) {
      await deleteParts(cache, storageKey, state.nextIndex);
      state.contentType = chunk.contentType || state.contentType;
      if (!(await putPart(cache, storageKey, 0, chunk.blob, state.contentType, chunk.blob.size))) {
        return false;
      }
      state.nextIndex = 1;
      state.offset = chunk.blob.size;
      state.total = chunk.blob.size;
      break;
    }

    state.contentType = chunk.contentType || state.contentType;
    if (chunk.totalFromRange) state.total = chunk.totalFromRange;

    if (!(await putPart(cache, storageKey, state.nextIndex, chunk.blob, state.contentType, state.total))) {
      return false;
    }
    state.nextIndex++;
    state.offset += chunk.blob.size;
    if (state.total == null && chunk.blob.size < CHUNK_SIZE) state.total = state.offset;

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

export type CacheBookAudioOptions = {
  /** Skip the post-play warmup delay (explicit "Save offline" taps). */
  immediate?: boolean;
  onProgress?: (done: number, total: number) => void;
};

/** Wait until another cacheBookAudio for the same book finishes (or give up). */
async function waitForInFlightClear(prefix: string, maxMs = 120_000): Promise<void> {
  const start = Date.now();
  while (inFlight.has(prefix) && Date.now() - start < maxMs) {
    await sleep(400);
  }
}

export async function cacheBookAudio(
  tracks: CacheableTrack[],
  onProgressOrOpts?: ((done: number, total: number) => void) | CacheBookAudioOptions,
): Promise<void> {
  const opts: CacheBookAudioOptions =
    typeof onProgressOrOpts === "function"
      ? { onProgress: onProgressOrOpts }
      : onProgressOrOpts || {};
  const onProgress = opts.onProgress;
  const force = Boolean(opts.immediate);

  if (!cacheSupported() || tracks.length === 0) return;
  const cacheable = tracks.filter((t) => t.contentUrl && isCacheableUrl(t.contentUrl));
  if (cacheable.length === 0) return;

  const prefix = rowPrefixFromTrackUrl(cacheable[0].contentUrl) || cacheable[0].contentUrl;
  if (inFlight.has(prefix)) {
    // Explicit Save offline must not bail silently if a background pass is running.
    await waitForInFlightClear(prefix);
    if (inFlight.has(prefix)) return;
    // Re-check — the other pass may have finished the book.
    if (await isBookCached(tracks)) {
      onProgress?.(tracks.length, tracks.length);
      return;
    }
  }
  inFlight.add(prefix);

  try {
    if (!force) await sleep(START_DELAY_MS);
    await requestPersistentStorage();
    const cache = await caches.open(AUDIO_CACHE);

    let done = tracks.length - cacheable.length;
    let pending = cacheable;

    for (let pass = 0; pass < MAX_TRACK_PASSES && pending.length > 0; pass++) {
      // Background listens yield to playback; explicit Save offline does not.
      if (!force) {
        while (shouldDeferCacheDownload()) {
          await sleep(2_000);
        }
      }

      if (pass > 0) {
        const hasPartial = await Promise.all(
          pending.map(async (t) => {
            const key = cacheStorageKey(t.contentUrl);
            return Boolean(await cache.match(partKey(key, 0)));
          })
        );
        const delay = hasPartial.some(Boolean) ? PASS_RETRY_PARTIAL_MS : PASS_RETRY_DELAY_MS;
        await sleep(delay);
      }

      const failed: CacheableTrack[] = [];
      for (const t of pending) {
        if (!force) {
          while (shouldDeferCacheDownload()) {
            await sleep(2_000);
          }
        }

        const url = t.contentUrl;
        const key = cacheStorageKey(url);
        if (await cacheHasUrl(cache, key)) {
          done++;
          onProgress?.(done, tracks.length);
          continue;
        }
        if (await downloadTrack(cache, url, { force })) {
          notifyCacheUpdated();
          done++;
          onProgress?.(done, tracks.length);
        } else {
          failed.push(t);
        }
      }
      pending = failed;
      if (pending.length > 0 && !(await hasStorageRoom(CHUNK_SIZE))) break;
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
