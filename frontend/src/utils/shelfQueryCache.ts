/**
 * My Library collection cache helpers.
 *
 * Persisted react-query shelves must REPLACE on fetch (never merge item lists).
 * After ABS/Kavita scans, drop in-memory + localStorage collection rows so phones
 * cannot keep orphan ASIN titles alongside newly fixed items.
 *
 * Persist blobs are origin-scoped (v5) so multi-library devices never mix catalogs.
 */
import type { QueryClient } from "@tanstack/react-query";
import { currentOrigin } from "../api/libraryRegistry";

/** Base prefix; use shelfPersistKey() for the active origin. */
export const SHELF_PERSIST_KEY_PREFIX = "rq-shelf-cache-v5:";
export const SHELF_PERSIST_LEGACY_KEYS = [
  "rq-shelf-cache-v2",
  "rq-shelf-cache-v3",
  "rq-shelf-cache-v4",
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

export type AbsCollectionData = {
  genres?: Record<string, Array<{ itemId?: string; title?: string }>>;
  ungrouped?: Array<{ itemId?: string; title?: string }>;
  totalItems?: number;
};

export type KavitaCollectionData = {
  items?: Array<{ seriesId?: number; title?: string }>;
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
 * Fresh data always wins wholesale — callers should replace, not merge.
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

/** Drop library collection queries so the next fetch rewrites persist from scratch. */
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

/** One-time drop of prior persist generations (ASIN-tainted shelf snapshots). */
export function clearLegacyShelfPersist(
  storage: Pick<Storage, "removeItem"> = localStorage,
): void {
  for (const key of SHELF_PERSIST_LEGACY_KEYS) {
    try {
      storage.removeItem(key);
    } catch {
      // ignore
    }
  }
}
