/**
 * My Library collection cache helpers.
 *
 * Stale-while-revalidate: keep showing the persisted/memory shelf while a
 * background fetch runs. Fresh full snapshots REPLACE by id (add/update/prune).
 * Soft-refresh (invalidate) is the default after scans/adds so phones never
 * blank the shelf for 30s. Hard purge remains for rare ASIN orphan wipes.
 *
 * Persist blobs are origin-scoped (v9) so multi-library devices never mix catalogs.
 * v6 busts stale Mayfair/Hardcover series snapshots after ABS seriesName preference.
 * v7 busts Kavita shelves collapsed by seriesId-only merge (multi-volume series
 * like Burning Witch were reduced to one card; count stuck near series count).
 * v8 busts shelves that still collapsed multi-file chapters (same chapterId) and
 * inflated ABS counts from ASIN duplicates / chapter-folder fragments.
 * v9 busts shelves that ignored file-scoped ebook_applied.json overrides after Kavita refresh.
 */
import type { QueryClient } from "@tanstack/react-query";
import { currentOrigin } from "../api/libraryRegistry";
import { libraryQueryKey } from "./libraryQueryKeys";

/** Base prefix; use shelfPersistKey() for the active origin. */
export const SHELF_PERSIST_KEY_PREFIX = "rq-shelf-cache-v9:";
export const SHELF_PERSIST_LEGACY_KEYS = [
  "rq-shelf-cache-v2",
  "rq-shelf-cache-v3",
  "rq-shelf-cache-v4",
  "rq-shelf-cache-v5",
  "rq-shelf-cache-v6",
  "rq-shelf-cache-v7",
  "rq-shelf-cache-v8",
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
  volumeId?: number | null;
  chapterId?: number | null;
  /** Distinct file identity when multiple ebooks share one Kavita chapter. */
  fileKey?: string | null;
  fileName?: string | null;
  title?: string;
  addedAt?: number;
};

export type KavitaCollectionData = {
  items?: KavitaCollectionItem[];
  totalItems?: number;
};

/** Stable identity for one shelf card (series may expand to multiple volumes/files). */
export function kavitaCollectionItemKey(item: KavitaCollectionItem | null | undefined): string | null {
  const sid = item?.seriesId;
  if (sid == null || !Number.isFinite(sid)) return null;
  const fileKey = (item?.fileKey || "").trim();
  if (fileKey) return `${sid}:f:${fileKey}`;
  const chapterId = item?.chapterId;
  if (chapterId != null && Number.isFinite(chapterId)) return `${sid}:c:${chapterId}`;
  const volumeId = item?.volumeId;
  if (volumeId != null && Number.isFinite(volumeId)) return `${sid}:v:${volumeId}`;
  return `${sid}:s`;
}

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
 * Merge Kavita collection snapshots by volume/chapter identity.
 *
 * Multi-volume series expand to one shelf item per volume (same seriesId).
 * Merging on seriesId alone collapsed those cards (e.g. 3 Burning Witch
 * volumes → 1, shelf count stuck near Kavita series count).
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
  const map = new Map<string, KavitaCollectionItem>();
  if (!prune) {
    for (const item of cached.items || []) {
      const id = kavitaCollectionItemKey(item);
      if (id) map.set(id, item);
    }
  }
  for (const item of fresh.items || []) {
    const id = kavitaCollectionItemKey(item);
    if (id) map.set(id, item);
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

/**
 * Open a short window where collection fetches request refresh=true.
 * Prefer a single short bust after a scan settles — do not re-extend on every poll
 * (that caused Pi load spikes via repeated full ABS/Kavita rebuilds).
 */
export function markLibraryCollectionCacheBust(ms = 8_000): void {
  if (ms <= 0) return;
  _collectionBustUntil = Math.max(_collectionBustUntil, Date.now() + ms);
}

/**
 * Invalidate + refetch My Library collection queries (stale-while-revalidate).
 *
 * Default is cache-friendly: no refresh=true bust. Pass bustMs only once after a
 * scan is expected to have finished; background polls must use bustMs: 0.
 */

/** Shelf prefixes persisted per library origin (My Library / Home cold start). */
export const SHELF_PERSIST_PREFIXES = [
  ...LIBRARY_COLLECTION_PREFIXES,
  "trending-books",
  "new-releases",
  "home-shelves",
  "category-carousel",
  "genres",
  "curated-slugs",
] as const;

export const LIBRARY_ORIGIN_CHANGED_EVENT = "library:origin-changed";

let _persistPausedUntil = 0;

/** Pause shelf persist briefly so a switch cannot write library A into B's disk key. */
export function pauseShelfPersist(ms = 2000): void {
  _persistPausedUntil = Math.max(_persistPausedUntil, Date.now() + ms);
}

export function isShelfPersistPaused(): boolean {
  return Date.now() < _persistPausedUntil;
}

function normalizeOrigin(origin: string | null | undefined): string {
  return (origin || "").replace(/\/+$/, "") || "default";
}

/** Origin segment from an origin-scoped query key, or null if unscoped/legacy. */
export function originFromQueryKey(queryKey: unknown): string | null {
  if (!Array.isArray(queryKey) || queryKey.length < 2) return null;
  const second = queryKey[1];
  if (typeof second !== "string") return null;
  if (second === "default" || second.startsWith("http") || second.includes("://")) {
    return normalizeOrigin(second);
  }
  return null;
}

/** Write one origin's in-memory shelf queries to that origin's persist blob. */
export function flushShelfPersistToOrigin(
  queryClient: QueryClient,
  origin: string,
  prefixes: readonly string[] = SHELF_PERSIST_PREFIXES,
): void {
  const o = normalizeOrigin(origin);
  try {
    const entries: [unknown, unknown][] = [];
    for (const q of queryClient.getQueryCache().getAll()) {
      const first = Array.isArray(q.queryKey) ? String(q.queryKey[0]) : "";
      if (!(prefixes as readonly string[]).includes(first)) continue;
      if (q.state.status !== "success" || q.state.data === undefined) continue;
      const keyOrigin = originFromQueryKey(q.queryKey);
      // Only this library's rows — never dump every origin into one blob.
      if (keyOrigin !== o) continue;
      entries.push([q.queryKey, JSON.parse(JSON.stringify(q.state.data))]);
    }
    const key = shelfPersistKey(o);
    if (entries.length === 0) {
      // Keep an existing disk blob if memory has nothing for this origin yet
      // (e.g. switched away before that library's queries remounted).
      return;
    }
    localStorage.setItem(key, JSON.stringify({ t: Date.now(), entries }));
  } catch {
    // ignore
  }
}

/**
 * Persist every origin that currently has shelf data in memory to its own blob.
 * Prevents multi-library sessions from mixing catalogs into one localStorage key.
 */
export function flushAllShelfPersists(
  queryClient: QueryClient,
  prefixes: readonly string[] = SHELF_PERSIST_PREFIXES,
): void {
  const byOrigin = new Map<string, [unknown, unknown][]>();
  try {
    for (const q of queryClient.getQueryCache().getAll()) {
      const first = Array.isArray(q.queryKey) ? String(q.queryKey[0]) : "";
      if (!(prefixes as readonly string[]).includes(first)) continue;
      if (q.state.status !== "success" || q.state.data === undefined) continue;
      const o = originFromQueryKey(q.queryKey) || normalizeOrigin(currentOrigin());
      const list = byOrigin.get(o) || [];
      list.push([q.queryKey, JSON.parse(JSON.stringify(q.state.data))]);
      byOrigin.set(o, list);
    }
    for (const [o, entries] of byOrigin) {
      if (entries.length === 0) continue;
      localStorage.setItem(shelfPersistKey(o), JSON.stringify({ t: Date.now(), entries }));
    }
  } catch {
    // ignore
  }
}

/** Hydrate React Query from an origin-scoped shelf persist blob (does not clear other libraries). */
export function hydrateShelfPersistForOrigin(
  queryClient: QueryClient,
  origin: string,
  opts?: { maxAgeMs?: number; onlyIfMissing?: boolean },
): void {
  const o = normalizeOrigin(origin);
  const maxAge = opts?.maxAgeMs ?? 24 * 60 * 60 * 1000;
  const onlyIfMissing = opts?.onlyIfMissing !== false;
  const collectionSet = new Set<string>(LIBRARY_COLLECTION_PREFIXES as readonly string[]);
  try {
    const raw = localStorage.getItem(shelfPersistKey(o));
    if (!raw) return;
    const saved = JSON.parse(raw) as { t: number; entries: [unknown, unknown][] };
    if (!saved || Date.now() - saved.t >= maxAge || !Array.isArray(saved.entries)) return;
    for (const [key, data] of saved.entries) {
      let qk = Array.isArray(key) ? [...(key as unknown[])] : null;
      if (!qk || typeof qk[0] !== "string") continue;
      const name = String(qk[0]);
      const keyOrigin = originFromQueryKey(qk);
      if (!keyOrigin) {
        // Legacy unscoped row in this origin's blob → scope to this origin.
        qk = [name, o, ...qk.slice(1)];
      } else if (keyOrigin !== o) {
        // Wrong library leaked into this blob — skip.
        continue;
      }
      if (onlyIfMissing && queryClient.getQueryData(qk as readonly unknown[]) !== undefined) {
        continue;
      }
      const updatedAt =
        collectionSet.has(name) || name === "trending-books" || name === "new-releases"
          ? 0
          : saved.t;
      queryClient.setQueryData(qk as readonly unknown[], data, { updatedAt });
    }
  } catch {
    // ignore
  }
}

/**
 * When switching remembered libraries: flush the previous origin to its disk cache,
 * hydrate the next origin from its disk cache, keep both in memory (no clears).
 */
export function swapLibraryQueryCache(
  queryClient: QueryClient,
  prevOrigin: string | null | undefined,
  nextOrigin: string,
): void {
  const prev = normalizeOrigin(prevOrigin);
  const next = normalizeOrigin(nextOrigin);
  if (prev && prev === next) return;
  pauseShelfPersist(1500);
  if (prev && prev !== "default") {
    flushShelfPersistToOrigin(queryClient, prev);
  }
  hydrateShelfPersistForOrigin(queryClient, next, { onlyIfMissing: true });
  try {
    window.dispatchEvent(
      new CustomEvent(LIBRARY_ORIGIN_CHANGED_EVENT, { detail: { prev, next } }),
    );
  } catch {
    // ignore
  }
}

export async function softRefreshLibraryCollectionQueries(
  queryClient: QueryClient,
  opts?: { refetch?: boolean; bustMs?: number },
): Promise<void> {
  // Default 0: rely on server-side cache invalidation after ABS/Kavita scans.
  // Explicit bustMs > 0 is for a single post-scan catch-up only.
  if (opts?.bustMs != null && opts.bustMs > 0) {
    markLibraryCollectionCacheBust(opts.bustMs);
  }
  const keys = LIBRARY_COLLECTION_PREFIXES.map((p) => libraryQueryKey(p));
  await Promise.all(keys.map((queryKey) => queryClient.invalidateQueries({ queryKey })));
  queryClient.invalidateQueries({ queryKey: libraryQueryKey("abs-series") });
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
  const keys = LIBRARY_COLLECTION_PREFIXES.map((p) => libraryQueryKey(p));
  await Promise.all(keys.map((queryKey) => queryClient.removeQueries({ queryKey })));
  // Also drop abs-series if present (not persisted, but can hold stale drilldowns).
  queryClient.removeQueries({ queryKey: libraryQueryKey("abs-series") });
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
    "rq-shelf-cache-v8:",
    "rq-shelf-cache-v7:",
    "rq-shelf-cache-v6:",
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
