/**
 * Persist enough Now Playing info for Android Auto to resume after the app
 * process is killed (car power cycle) without unlocking the phone first.
 */
import type { NowPlaying } from "../types/player";

const KEY = "aa-resume-snapshot-v1";

export interface AaResumeSnapshot {
  source: "abs" | "rd";
  itemId?: string;
  streamHistoryId?: number;
  libraryItemId?: number;
  title: string;
  author: string;
  coverUrl: string;
  /** Global position seconds */
  position: number;
  trackIndex: number;
  trackLocal: number;
  updatedAt: number;
}

export function saveAaResumeSnapshot(
  np: NowPlaying,
  position: number,
  trackIndex: number,
  trackLocal: number
): void {
  try {
    const snap: AaResumeSnapshot = {
      source: np.source,
      itemId: np.itemId,
      streamHistoryId: np.streamHistoryId,
      libraryItemId: np.libraryItemId,
      title: np.title,
      author: np.author,
      coverUrl: np.coverUrl,
      position: Math.max(0, position),
      trackIndex: Math.max(0, trackIndex),
      trackLocal: Math.max(0, trackLocal),
      updatedAt: Date.now(),
    };
    localStorage.setItem(KEY, JSON.stringify(snap));
  } catch {
    /* ignore */
  }
}

export function loadAaResumeSnapshot(): AaResumeSnapshot | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const snap = JSON.parse(raw) as AaResumeSnapshot;
    if (!snap?.source || !snap.title) return null;
    // Ignore stale snapshots older than 30 days
    if (Date.now() - (snap.updatedAt || 0) > 30 * 24 * 60 * 60 * 1000) return null;
    return snap;
  } catch {
    return null;
  }
}

export function clearAaResumeSnapshot(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
}
