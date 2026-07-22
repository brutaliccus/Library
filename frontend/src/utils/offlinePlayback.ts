/**
 * Offline playback manifests + local listening progress.
 *
 * Online play always talks to ABS/our API for sessions and resume position.
 * Fully cached books still need track metadata (and a position) to start —
 * those live here so offline play can skip /play and /library/.../play.
 *
 * Storage is origin-scoped so multi-library installs never mix catalogs.
 */
import type { AbsChapter, Track } from "../types/player";
import { currentOrigin } from "../api/libraryRegistry";
import { isBookCached } from "./audioCache";
import { isEbookCached } from "./ebookCache";
import { isLikelyOffline as networkOffline } from "./networkStatus";

const MANIFEST_PREFIX = "offline-playback-manifests-v2:";
const PROGRESS_PREFIX = "offline-playback-progress-v2:";
const LEGACY_MANIFEST_KEY = "offline-playback-manifests-v1";
const LEGACY_PROGRESS_KEY = "offline-playback-progress-v1";

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

export interface EbookOfflineManifest {
  source: "ebook";
  chapterId: number;
  title: string;
  author: string;
  coverUrl: string;
  isPdf: boolean;
  updatedAt: number;
}

export type OfflineManifest = AbsOfflineManifest | RdOfflineManifest | EbookOfflineManifest;

export interface OfflineProgress {
  time: number;
  trackIndex: number;
  trackLocal: number;
  updatedAt: number;
}

function scopeSuffix(): string {
  return (currentOrigin() || "default").replace(/\/+$/, "") || "default";
}

function manifestKey(): string {
  return `${MANIFEST_PREFIX}${scopeSuffix()}`;
}

function progressKey(): string {
  return `${PROGRESS_PREFIX}${scopeSuffix()}`;
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

/** One-time migrate unscoped v1 → current origin (best-effort). */
function migrateLegacyIfNeeded(): void {
  try {
    const key = manifestKey();
    if (localStorage.getItem(key)) return;
    const legacy = localStorage.getItem(LEGACY_MANIFEST_KEY);
    if (!legacy) return;
    localStorage.setItem(key, legacy);
    const prog = localStorage.getItem(LEGACY_PROGRESS_KEY);
    if (prog && !localStorage.getItem(progressKey())) {
      localStorage.setItem(progressKey(), prog);
    }
  } catch {
    /* ignore */
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

function ebookKey(chapterId: number): string {
  return `ebook:${chapterId}`;
}

function allManifests(): Record<string, OfflineManifest> {
  migrateLegacyIfNeeded();
  return readJson<Record<string, OfflineManifest>>(manifestKey(), {});
}

function saveAllManifests(all: Record<string, OfflineManifest>): void {
  writeJson(manifestKey(), all);
}

export function saveAbsOfflineManifest(
  manifest: Omit<AbsOfflineManifest, "source" | "updatedAt">
): void {
  const all = allManifests();
  all[absKey(manifest.itemId)] = {
    ...manifest,
    source: "abs",
    updatedAt: Date.now(),
  };
  saveAllManifests(all);
}

export function saveRdOfflineManifest(
  manifest: Omit<RdOfflineManifest, "source" | "updatedAt">
): void {
  const all = allManifests();
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
  if (manifest.libraryItemId == null && manifest.streamHistoryId != null) {
    all[rdHistoryKey(manifest.streamHistoryId)] = entry;
  }
  saveAllManifests(all);
}

export function saveEbookOfflineManifest(
  manifest: Omit<EbookOfflineManifest, "source" | "updatedAt">
): void {
  const all = allManifests();
  all[ebookKey(manifest.chapterId)] = {
    ...manifest,
    source: "ebook",
    updatedAt: Date.now(),
  };
  saveAllManifests(all);
}

export function getAbsOfflineManifest(itemId: string): AbsOfflineManifest | null {
  const m = allManifests()[absKey(itemId)];
  return m?.source === "abs" ? m : null;
}

export function getRdOfflineManifest(opts: {
  libraryItemId?: number;
  streamHistoryId?: number;
}): RdOfflineManifest | null {
  const all = allManifests();
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

export function getEbookOfflineManifest(chapterId: number): EbookOfflineManifest | null {
  const m = allManifests()[ebookKey(chapterId)];
  return m?.source === "ebook" ? m : null;
}

export function listOfflineManifests(): OfflineManifest[] {
  const all = allManifests();
  const seen = new Set<string>();
  const out: OfflineManifest[] = [];
  for (const m of Object.values(all)) {
    const id =
      m.source === "abs"
        ? absKey(m.itemId)
        : m.source === "ebook"
          ? ebookKey(m.chapterId)
          : m.libraryItemId != null
            ? rdLibraryKey(m.libraryItemId)
            : m.streamHistoryId != null
              ? rdHistoryKey(m.streamHistoryId)
              : "";
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out.push(m);
  }
  return out.sort((a, b) => b.updatedAt - a.updatedAt);
}

export function removeAbsOfflineManifest(itemId: string): void {
  const all = allManifests();
  delete all[absKey(itemId)];
  saveAllManifests(all);
  removeOfflineProgress(absKey(itemId));
}

export function removeRdOfflineManifest(opts: {
  libraryItemId?: number;
  streamHistoryId?: number;
}): void {
  const all = allManifests();
  if (opts.libraryItemId != null) delete all[rdLibraryKey(opts.libraryItemId)];
  if (opts.streamHistoryId != null) delete all[rdHistoryKey(opts.streamHistoryId)];
  saveAllManifests(all);
  if (opts.libraryItemId != null) removeOfflineProgress(rdLibraryKey(opts.libraryItemId));
  if (opts.streamHistoryId != null) removeOfflineProgress(rdHistoryKey(opts.streamHistoryId));
}

export function removeEbookOfflineManifest(chapterId: number): void {
  const all = allManifests();
  delete all[ebookKey(chapterId)];
  saveAllManifests(all);
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
  const all = readJson<Record<string, OfflineProgress>>(progressKey(), {});
  all[key] = { ...progress, updatedAt: Date.now() };
  writeJson(progressKey(), all);
}

export function getOfflineProgress(key: string): OfflineProgress | null {
  if (!key) return null;
  migrateLegacyIfNeeded();
  const all = readJson<Record<string, OfflineProgress>>(progressKey(), {});
  return all[key] ?? null;
}

function removeOfflineProgress(key: string): void {
  const all = readJson<Record<string, OfflineProgress>>(progressKey(), {});
  delete all[key];
  writeJson(progressKey(), all);
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

export async function isEbookOfflineReady(chapterId: number): Promise<boolean> {
  const m = getEbookOfflineManifest(chapterId);
  if (!m) return isEbookCached(chapterId, true);
  return isEbookCached(chapterId, m.isPdf);
}

/** Downloaded items that are fully present in the Cache API. */
export async function listDownloadedItems(): Promise<
  Array<
    | (AbsOfflineManifest & { cached: true })
    | (RdOfflineManifest & { cached: true })
    | (EbookOfflineManifest & { cached: true })
  >
> {
  const manifests = listOfflineManifests();
  const out: Array<
    | (AbsOfflineManifest & { cached: true })
    | (RdOfflineManifest & { cached: true })
    | (EbookOfflineManifest & { cached: true })
  > = [];
  for (const m of manifests) {
    if (m.source === "abs") {
      if (await isBookCached(m.tracks)) out.push({ ...m, cached: true });
    } else if (m.source === "rd") {
      if (await isBookCached(m.tracks)) out.push({ ...m, cached: true });
    } else if (m.source === "ebook") {
      if (await isEbookCached(m.chapterId, m.isPdf)) out.push({ ...m, cached: true });
    }
  }
  return out;
}

export function isLikelyOffline(): boolean {
  return networkOffline();
}
