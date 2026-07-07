/**
 * Shared storage quota + download coordination for audio/ebook caches.
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
let downloadThrottled = false;
let throttleWaiters: Array<() => void> = [];

/** True while a book is actively playing (not merely mounted). */
export function setAudioPlaybackActive(active: boolean): void {
  audioPlaybackActive = active;
}

/**
 * Throttle background downloads during sustained buffering instead of stopping
 * them entirely (which prevented cache from ever completing).
 */
export function setMediaDownloadThrottled(throttled: boolean): void {
  downloadThrottled = throttled;
  if (!throttled) {
    const waiters = throttleWaiters;
    throttleWaiters = [];
    waiters.forEach((resolve) => resolve());
  }
}

/** @deprecated Use setMediaDownloadThrottled — kept for call-site compatibility. */
export function setMediaDownloadPaused(paused: boolean): void {
  setMediaDownloadThrottled(paused);
}

export function isAudioPlaybackActive(): boolean {
  return audioPlaybackActive;
}

export function shouldDeferEbookDownload(): boolean {
  return audioPlaybackActive || downloadThrottled;
}

export async function waitForDownloadSlot(): Promise<void> {
  if (!downloadThrottled) return;
  return new Promise((resolve) => throttleWaiters.push(resolve));
}

/** Extra delay between chunks while playback is starved for bandwidth. */
export async function throttleDelay(baseMs: number): Promise<void> {
  if (!downloadThrottled) return;
  await new Promise((r) => setTimeout(r, Math.max(baseMs, 1500)));
}
