/**
 * My Library collection cache helpers.
 *
 * Stale-while-revalidate: keep showing the persisted/memory shelf while a
 * background fetch runs. Fresh full snapshots REPLACE by id (add/update/prune).
 * Soft-refresh (invalidate) is the default after scans/adds so phones never
 * blank the shelf for 30s. Hard purge remains for rare ASIN orphan wipes.
 *
 * Persist blobs are origin-scoped (v6) so multi-library devices never mix catalogs.
 * v6 busts stale Mayfair/Hardcover series snapshots after ABS seriesName preference.
 */
import type { QueryClient } from "@tanstack/react-query";
import { currentOrigin } from "../api/libraryRegistry";

/** Base prefix; use shelfPersistKey() for the active origin. */
export const SHELF_PERSIST_KEY_PREFIX = "rq-shelf-cache-v6:";
export const SHELF_PERSIST_LEGACY_KEYS = [
  "rq-shelf-cache-v2",
  "rq-shelf-cache-v3",
  "rq-shelf-cache-v4",
  "rq-shelf-cache-v5",
] as const;

/** Origin-scoped localStorage key for shelf query persistence. */
export function shelfPersistKey(origin?: string): string {
  const o = (origin || currentOrigin() || "default").replace(/\/+$/, "") || "default";
  return `${SHELF_PERSIST_KEY_PREFIX}${o}`;
}

/** Query key prefixes persisted for My Library (and related shelves). */
export const LIBRARY_COLLECTION_PREFIXES = [
  "abs-collection",
  "kavita-collection",
  "streaming-library",
] as const;

export type AbsCollectionItem = {
  itemId?: string;
  title?: string;
  genres?: string[];
  addedAt?: number;
};

export type AbsCollectionData = {
  genres?: Record<string, AbsCollectionItem[]>;
  ungrouped?: AbsCollectionItem[];
  totalItems?: number;
};

export type KavitaCollectionItem = {
  seriesId?: number;
  title?: string;
  addedAt?: number;
};

export type KavitaCollectionData = {
  items?: KavitaCollectionItem[];
  totalItems?: number;
};

export type StreamingLibraryData = {
  items?: Array<{ id?: number; title?: string }>;
};

/** Stable identity set for ABS collection payloads (deduped itemIds). */
export function absCollectionItemIds(data: AbsCollectionData | null | undefined): string[] {
  if (!data) return [];
  const ids = new Set<string>();
  for (const bucket of Object.values(data.genres || {})) {
    for (const item of bucket || []) {
      const id = (item?.itemId || "").trim();
      if (id) ids.add(id);
    }
  }
  for (const item of data.ungrouped || []) {
    const id = (item?.itemId || "").trim();
    if (id) ids.add(id);
  }
  return Array.from(ids).sort();
}

/** Compact signature: count + sorted ids (detect orphan / replace-needed snapshots). */
export function absCollectionSignature(data: AbsCollectionData | null | undefined): string {
  const ids = absCollectionItemIds(data);
  return `${ids.length}:${ids.join(",")}`;
}

/**
 * True when the cached snapshot still contains itemIds the server no longer returns.
 * Fresh full snapshots should prune those ids (merge with pruneMissing).
 */
export function absCollectionHasOrphans(
  cached: AbsCollectionData | null | undefined,
  fresh: AbsCollectionData | null | undefined,
): boolean {
  if (!cached || !fresh) return false;
  const freshIds = new Set(absCollectionItemIds(fresh));
  if (freshIds.size === 0) return absCollectionItemIds(cached).length > 0;
  return absCollectionItemIds(cached).some((id) => !freshIds.has(id));
}

function _flattenAbsItems(data: AbsCollectionData): AbsCollectionItem[] {
  const byId = new Map<string, AbsCollectionItem>();
  for (const bucket of Object.values(data.genres || {})) {
    for (const item of bucket || []) {
      const id = (item?.itemId || "").trim();
      if (id) byId.set(id, item);
    }
  }
  for (const item of data.ungrouped || []) {
    const id = (item?.itemId || "").trim();
    if (id) byId.set(id, item);
  }
  return Array.from(byId.values());
}

function _regroupAbsItems(items: AbsCollectionItem[]): AbsCollectionData {
  const genres: Record<string, AbsCollectionItem[]> = {};
  const ungrouped: AbsCollectionItem[] = [];
  const seenInGenre: Record<string, Set<string>> = {};
  for (const item of items) {
    const id = (item?.itemId || "").trim();
    if (!id) continue;
    const mapped = (item.genres as string[] | undefined) || [];
    if (!mapped.length) {
      ungrouped.push(item);
      continue;
    }
    for (const top of mapped) {
      const key = String(top);
      seenInGenre[key] ??= new Set();
      if (seenInGenre[key].has(id)) continue;
      seenInGenre[key].add(id);
      (genres[key] ??= []).push(item);
    }
  }
  const sortedGenres = Object.fromEntries(
    Object.entries(genres).sort(([a], [b]) => a.localeCompare(b)),
  );
  const byAdded = (a: AbsCollectionItem, b: AbsCollectionItem) =>
    (Number(b.addedAt) || 0) - (Number(a.addedAt) || 0);
  for (const bucket of Object.values(sortedGenres)) bucket.sort(byAdded);
  ungrouped.sort(byAdded);
  // Unique books only (same book may sit in multiple genre buckets).
  const totalItems = items.filter((it) => (it?.itemId || "").trim()).length;
  return { genres: sortedGenres, ungrouped, totalItems };
}

/**
 * Merge ABS collection snapshots by itemId.
 * - Always upserts fresh items over cached.
 * - When pruneMissing, drops cached ids absent from fresh (full-snapshot replace).
 * - When not pruning (incomplete soft-poll), keeps cached-only ids so the shelf
 *   does not shrink while ABS is still indexing.
 */
export function mergeAbsCollection<T extends AbsCollectionData>(
  cached: T | null | undefined,
  fresh: T | null | undefined,
  opts?: { pruneMissing?: boolean },
): T | null | undefined {
  if (!fresh) return cached;
  if (!cached) return fresh;
  const prune = opts?.pruneMissing !== false;
  const map = new Map<string, AbsCollectionItem>();
  if (!prune) {
    for (const item of _flattenAbsItems(cached)) {
      const id = (item?.itemId || "").trim();
      if (id) map.set(id, item);
    }
  }
  for (const item of _flattenAbsItems(fresh)) {
    const id = (item?.itemId || "").trim();
    if (id) map.set(id, item);
  }
  return _regroupAbsItems(Array.from(map.values())) as T;
}

/**
 * Merge Kavita collection snapshots by seriesId.
 * Same prune semantics as {@link mergeAbsCollection}.
 */
export function mergeKavitaCollection<T extends KavitaCollectionData>(
  cached: T | null | undefined,
  fresh: T | null | undefined,
  opts?: { pruneMissing?: boolean },
): T | null | undefined {
  if (!fresh) return cached;
  if (!cached) return fresh;
  const prune = opts?.pruneMissing !== false;
  const map = new Map<number, KavitaCollectionItem>();
  if (!prune) {
    for (const item of cached.items || []) {
      const id = item?.seriesId;
      if (id != null && Number.isFinite(id)) map.set(Number(id), item);
    }
  }
  for (const item of fresh.items || []) {
    const id = item?.seriesId;
    if (id != null && Number.isFinite(id)) map.set(Number(id), item);
  }
  const items = Array.from(map.values()).sort(
    (a, b) => (Number(b.addedAt) || 0) - (Number(a.addedAt) || 0),
  );
  return { ...fresh, items, totalItems: items.length };
}

/**
 * Merge streaming/personal library by numeric id.
 */
export function mergeStreamingLibrary<T extends StreamingLibraryData>(
  cached: T | null | undefined,
  fresh: T | null | undefined,
  opts?: { pruneMissing?: boolean },
): T | null | undefined {
  if (!fresh) return cached;
  if (!cached) return fresh;
  const prune = opts?.pruneMissing !== false;
  const map = new Map<number, NonNullable<StreamingLibraryData["items"]>[number]>();
  if (!prune) {
    for (const item of cached.items || []) {
      const id = item?.id;
      if (id != null && Number.isFinite(id)) map.set(Number(id), item);
    }
  }
  for (const item of fresh.items || []) {
    const id = item?.id;
    if (id != null && Number.isFinite(id)) map.set(Number(id), item);
  }
  return { ...fresh, items: Array.from(map.values()) as T["items"] };
}

/**
 * Soft refresh: mark collection queries stale and refetch active views.
 * Keeps in-memory + persist data visible (stale-while-revalidate).
 * During the bust window, collection queryFns pass refresh=true to skip
 * short-TTL server caches so soft-polls see newly indexed books.
 */
let _collectionBustUntil = 0;

/** True while My Library soft-refresh / post-scan polls should bypass server caches. */
export function shouldBustLibraryCollectionCache(): boolean {
  return Date.now() < _collectionBustUntil;
}

/** Open a short window where collection fetches request refresh=true. */
export function markLibraryCollectionCacheBust(ms = 35_000): void {
  _collectionBustUntil = Math.max(_collectionBustUntil, Date.now() + ms);
}

export async function softRefreshLibraryCollectionQueries(
  queryClient: QueryClient,
  opts?: { refetch?: boolean; bustMs?: number },
): Promise<void> {
  markLibraryCollectionCacheBust(opts?.bustMs ?? 35_000);
  const keys = LIBRARY_COLLECTION_PREFIXES.map((p) => [p] as const);
  await Promise.all(keys.map((queryKey) => queryClient.invalidateQueries({ queryKey })));
  queryClient.invalidateQueries({ queryKey: ["abs-series"] });
  if (opts?.refetch !== false) {
    await Promise.all(
      keys.map((queryKey) => queryClient.refetchQueries({ queryKey, type: "active" })),
    );
  }
}

/**
 * Hard drop library collection queries so the next fetch rewrites persist from scratch.
 * Prefer {@link softRefreshLibraryCollectionQueries} for normal refresh/scan paths.
 */
export async function purgeLibraryCollectionQueries(
  queryClient: QueryClient,
  opts?: { refetch?: boolean },
): Promise<void> {
  const keys = LIBRARY_COLLECTION_PREFIXES.map((p) => [p] as const);
  await Promise.all(keys.map((queryKey) => queryClient.removeQueries({ queryKey })));
  // Also drop abs-series if present (not persisted, but can hold stale drilldowns).
  queryClient.removeQueries({ queryKey: ["abs-series"] });
  stripCollectionEntriesFromPersist();
  if (opts?.refetch) {
    await Promise.all(
      keys.map((queryKey) => queryClient.refetchQueries({ queryKey, type: "active" })),
    );
  }
}

/** Remove collection rows from the shelf persist blob immediately (sync). */
export function stripCollectionEntriesFromPersist(
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem"> = localStorage,
  persistKey: string = shelfPersistKey(),
): void {
  try {
    const raw = storage.getItem(persistKey);
    if (!raw) return;
    const saved = JSON.parse(raw) as { t?: number; entries?: [unknown, unknown][] };
    if (!saved || !Array.isArray(saved.entries)) {
      storage.removeItem(persistKey);
      return;
    }
    const kept = saved.entries.filter(([key]) => {
      const first = Array.isArray(key) ? String(key[0]) : "";
      return !(LIBRARY_COLLECTION_PREFIXES as readonly string[]).includes(first);
    });
    if (kept.length === 0) {
      storage.removeItem(persistKey);
      return;
    }
    storage.setItem(persistKey, JSON.stringify({ t: Date.now(), entries: kept }));
  } catch {
    try {
      storage.removeItem(persistKey);
    } catch {
      // ignore
    }
  }
}

/** One-time drop of prior persist generations (ASIN / wrong-series snapshots). */
export function clearLegacyShelfPersist(
  storage: Pick<Storage, "removeItem" | "key" | "length"> = localStorage,
): void {
  for (const key of SHELF_PERSIST_LEGACY_KEYS) {
    try {
      storage.removeItem(key);
    } catch {
      // ignore
    }
  }
  // Origin-scoped generations use `prefix + origin` (e.g. rq-shelf-cache-v5:https://…).
  const originPrefixes = [
    "rq-shelf-cache-v5:",
    "rq-shelf-cache-v4:",
    "rq-shelf-cache-v3:",
  ] as const;
  try {
    const toRemove: string[] = [];
    for (let i = 0; i < storage.length; i++) {
      const k = storage.key(i);
      if (!k) continue;
      if (originPrefixes.some((p) => k.startsWith(p))) toRemove.push(k);
    }
    for (const k of toRemove) {
      try {
        storage.removeItem(k);
      } catch {
        // ignore
      }
    }
  } catch {
    // ignore
  }
}
