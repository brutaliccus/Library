import { snapPlaybackSpeed } from "./playbackSpeed";

const KEY = "library_default_playback_rate";

/** Cached user default speed (from settings). Falls back to 1.0. */
export function getCachedDefaultPlaybackRate(): number {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw == null) return 1;
    return snapPlaybackSpeed(parseFloat(raw));
  } catch {
    return 1;
  }
}

export function setCachedDefaultPlaybackRate(rate: number): void {
  try {
    localStorage.setItem(KEY, String(snapPlaybackSpeed(rate)));
  } catch {
    /* ignore */
  }
}

/** Resolve rate for a book: per-book override wins, else user default. */
export function resolveBookPlaybackRate(bookRate: number | null | undefined): number {
  if (bookRate != null && isFinite(bookRate) && bookRate > 0) {
    return snapPlaybackSpeed(bookRate);
  }
  return getCachedDefaultPlaybackRate();
}
