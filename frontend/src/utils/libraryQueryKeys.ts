import { currentOrigin } from "../api/libraryRegistry";

/** Normalize a library origin for use in React Query keys + persist. */
export function libraryOriginKey(origin?: string | null): string {
  const o = (origin || currentOrigin() || "default").replace(/\/+$/, "") || "default";
  return o;
}

/**
 * Origin-scoped query key: ["abs-collection", "https://host", ...rest]
 * Each remembered library keeps its own in-memory cache for instant switches.
 */
export function libraryQueryKey(
  name: string,
  ...rest: unknown[]
): readonly unknown[] {
  return [name, libraryOriginKey(), ...rest];
}

/** Match any query whose first key segment equals `name` (all origins). */
export function libraryQueryKeyPrefix(name: string): readonly unknown[] {
  return [name];
}
