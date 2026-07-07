/**
 * Shared storage quota + download coordination for audio/ebook caches.
 *
 * Playback always wins: background cache downloads must not compete with the
 * active <audio> element for Pi/debrid bandwidth.
 */

const MIN_HEADROOM_BYTES = 48 * 1024 * 1024;
const MAX_HEADROOM_BYTES = 128 * 1024 * 1024;
const HEADROOM_FRACTION = 0.08;

/** How much free space we try to keep (scales down on smaller quotas). */
export function quotaHeadroomBytes(quota: number): number {
  if (!quota) return MIN_HEADROOM_BYTES;
  return Math.min(MAX_HEADROOM_BYTES, Math.max(MIN_HEADROOM_BYTES, quota * HEADROOM_FRACTION));
}

export async function hasStorageRoom(extraBytes = 0): Promise<boolean> {
  try {
    if (!navigator.storage?.estimate) return true;
    const { usage = 0, quota = 0 } = await navigator.storage.estimate();
    if (!quota) return true;
    return quota - usage - extraBytes > quotaHeadroomBytes(quota);
  } catch {
    return true;
  }
}

/** Normalize cache keys so relative/absolute URLs and stray query params match. */
export function cacheStorageKey(url: string): string {
  try {
    const u = new URL(url, window.location.origin);
    return `${u.origin}${u.pathname}`;
  } catch {
    return url;
  }
}

// ---- Download coordination: audiobook playback wins over background work ----

let audioPlaybackActive = false;
/** Wall-clock ms when the current play session started (null when idle). */
let playbackStartedAt: number | null = null;
/** Hard pause — set while the audio element is stalled/buffering. */
let downloadPaused = false;
/** Soft throttle after sustained buffering. */
let downloadThrottled = false;
let throttleWaiters: Array<() => void> = [];

/** Grace period after play starts before background caching may begin. */
const CACHE_WARMUP_MS = 90_000;

/** True while a book is loaded in the player. */
export function setAudioPlaybackActive(active: boolean): void {
  const was = audioPlaybackActive;
  audioPlaybackActive = active;
  if (active && !was) {
    playbackStartedAt = Date.now();
  } else if (!active) {
    playbackStartedAt = null;
  }
}

/**
 * Hard pause background downloads while the audio element is buffering.
 * Unlike throttle, this stops cache work immediately so playback can recover.
 */
export function setMediaDownloadPaused(paused: boolean): void {
  downloadPaused = paused;
  if (!paused) wakeThrottleWaiters();
}

/**
 * Soft throttle after sustained buffering — cache may proceed slowly once the
 * stall clears, but still yields to playback.
 */
export function setMediaDownloadThrottled(throttled: boolean): void {
  downloadThrottled = throttled;
  if (!throttled) wakeThrottleWaiters();
}

function wakeThrottleWaiters(): void {
  if (downloadPaused || downloadThrottled) return;
  const waiters = throttleWaiters;
  throttleWaiters = [];
  waiters.forEach((resolve) => resolve());
}

export function isAudioPlaybackActive(): boolean {
  return audioPlaybackActive;
}

export function shouldDeferEbookDownload(): boolean {
  return shouldDeferCacheDownload();
}

/** True when background audio cache work must wait for playback. */
export function shouldDeferCacheDownload(): boolean {
  if (downloadPaused) return true;
  if (!audioPlaybackActive) return false;
  // Never compete during the startup buffer window.
  if (playbackStartedAt != null && Date.now() - playbackStartedAt < CACHE_WARMUP_MS) {
    return true;
  }
  // After warmup, still yield while actively throttled from sustained stalls.
  return downloadThrottled;
}

export async function waitForDownloadSlot(): Promise<void> {
  while (shouldDeferCacheDownload() || downloadThrottled) {
    if (downloadPaused || downloadThrottled) {
      await new Promise<void>((resolve) => throttleWaiters.push(resolve));
    } else {
      // Playback warmup window — poll until it elapses (no waiter to wake us).
      await new Promise((r) => setTimeout(r, 1_000));
    }
  }
}

/** Extra delay between chunks while playback is starved for bandwidth. */
export async function throttleDelay(baseMs: number): Promise<void> {
  if (!downloadThrottled && !downloadPaused) return;
  await new Promise((r) => setTimeout(r, Math.max(baseMs, downloadPaused ? 3000 : 1500)));
}
