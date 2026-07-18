/**
 * Offline playback manifests + local listening progress.
 *
 * Online play always talks to ABS/our API for sessions and resume position.
 * Fully cached books still need track metadata (and a position) to start —
 * those live here so offline play can skip /play and /library/.../play.
 */
import type { AbsChapter, Track } from "../types/player";
import { isBookCached } from "./audioCache";

const MANIFEST_KEY = "offline-playback-manifests-v1";
const PROGRESS_KEY = "offline-playback-progress-v1";

export interface AbsOfflineManifest {
  source: "abs";
  itemId: string;
  title: string;
  author: string;
  coverUrl: string;
  tracks: Track[];
  totalDuration: number;
  absChapters?: AbsChapter[];
  updatedAt: number;
}

export interface RdOfflineManifest {
  source: "rd";
  /** Streaming library item id when played from My Library */
  libraryItemId?: number;
  streamHistoryId?: number;
  title: string;
  author: string;
  coverUrl: string;
  tracks: Track[];
  totalDuration: number;
  updatedAt: number;
}

export type OfflineManifest = AbsOfflineManifest | RdOfflineManifest;

export interface OfflineProgress {
  time: number;
  trackIndex: number;
  trackLocal: number;
  updatedAt: number;
}

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* quota / private mode */
  }
}

function absKey(itemId: string): string {
  return `abs:${itemId}`;
}

function rdLibraryKey(id: number): string {
  return `rd:lib:${id}`;
}

function rdHistoryKey(id: number): string {
  return `rd:hist:${id}`;
}

export function saveAbsOfflineManifest(
  manifest: Omit<AbsOfflineManifest, "source" | "updatedAt">
): void {
  const all = readJson<Record<string, OfflineManifest>>(MANIFEST_KEY, {});
  all[absKey(manifest.itemId)] = {
    ...manifest,
    source: "abs",
    updatedAt: Date.now(),
  };
  writeJson(MANIFEST_KEY, all);
}

export function saveRdOfflineManifest(
  manifest: Omit<RdOfflineManifest, "source" | "updatedAt">
): void {
  const all = readJson<Record<string, OfflineManifest>>(MANIFEST_KEY, {});
  const entry: RdOfflineManifest = {
    ...manifest,
    source: "rd",
    updatedAt: Date.now(),
  };
  if (manifest.libraryItemId != null) {
    all[rdLibraryKey(manifest.libraryItemId)] = entry;
  }
  if (manifest.streamHistoryId != null) {
    all[rdHistoryKey(manifest.streamHistoryId)] = entry;
  }
  // Always keep a title-stable fallback key when we only have history id
  if (manifest.libraryItemId == null && manifest.streamHistoryId != null) {
    all[rdHistoryKey(manifest.streamHistoryId)] = entry;
  }
  writeJson(MANIFEST_KEY, all);
}

export function getAbsOfflineManifest(itemId: string): AbsOfflineManifest | null {
  const all = readJson<Record<string, OfflineManifest>>(MANIFEST_KEY, {});
  const m = all[absKey(itemId)];
  return m?.source === "abs" ? m : null;
}

export function getRdOfflineManifest(opts: {
  libraryItemId?: number;
  streamHistoryId?: number;
}): RdOfflineManifest | null {
  const all = readJson<Record<string, OfflineManifest>>(MANIFEST_KEY, {});
  if (opts.libraryItemId != null) {
    const m = all[rdLibraryKey(opts.libraryItemId)];
    if (m?.source === "rd") return m;
  }
  if (opts.streamHistoryId != null) {
    const m = all[rdHistoryKey(opts.streamHistoryId)];
    if (m?.source === "rd") return m;
  }
  return null;
}

export function progressKeyForAbs(itemId: string): string {
  return absKey(itemId);
}

export function progressKeyForRd(opts: {
  libraryItemId?: number;
  streamHistoryId?: number;
}): string | null {
  if (opts.libraryItemId != null) return rdLibraryKey(opts.libraryItemId);
  if (opts.streamHistoryId != null) return rdHistoryKey(opts.streamHistoryId);
  return null;
}

export function saveOfflineProgress(key: string, progress: Omit<OfflineProgress, "updatedAt">): void {
  if (!key || progress.time < 0) return;
  const all = readJson<Record<string, OfflineProgress>>(PROGRESS_KEY, {});
  all[key] = { ...progress, updatedAt: Date.now() };
  writeJson(PROGRESS_KEY, all);
}

export function getOfflineProgress(key: string): OfflineProgress | null {
  if (!key) return null;
  const all = readJson<Record<string, OfflineProgress>>(PROGRESS_KEY, {});
  return all[key] ?? null;
}

/** True when every track for a saved ABS manifest is fully on disk. */
export async function isAbsOfflineReady(itemId: string): Promise<boolean> {
  const m = getAbsOfflineManifest(itemId);
  if (!m?.tracks.length) return false;
  return isBookCached(m.tracks);
}

export async function isRdOfflineReady(opts: {
  libraryItemId?: number;
  streamHistoryId?: number;
}): Promise<boolean> {
  const m = getRdOfflineManifest(opts);
  if (!m?.tracks.length) return false;
  return isBookCached(m.tracks);
}

export function isLikelyOffline(): boolean {
  return typeof navigator !== "undefined" && navigator.onLine === false;
}
