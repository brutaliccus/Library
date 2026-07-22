/**
 * Explicit "Save offline" downloads — fetch play metadata, persist manifests,
 * and fill the Cache API without waiting for background listen-to-cache.
 */
import api from "../api/client";
import { toAbsoluteUrl } from "../api/instanceUrl";
import {
  cacheBookAudio,
  clearAbsBookCache,
  clearBookCache,
  clearBookCacheForTracks,
  isBookCached,
  type CacheableTrack,
} from "./audioCache";
import { cacheBookEbook, clearEbookCache, isEbookCached } from "./ebookCache";
import {
  getAbsOfflineManifest,
  getEbookOfflineManifest,
  getRdOfflineManifest,
  removeAbsOfflineManifest,
  removeEbookOfflineManifest,
  removeRdOfflineManifest,
  saveAbsOfflineManifest,
  saveEbookOfflineManifest,
  saveRdOfflineManifest,
} from "./offlinePlayback";
import type { AbsChapter, Track } from "../types/player";

export type OfflineDownloadKind = "abs" | "rd" | "ebook";

export type OfflineDownloadState = "idle" | "downloading" | "downloaded" | "error";

function absolutizeTracks<T extends CacheableTrack>(tracks: T[]): T[] {
  return tracks.map((t) => ({
    ...t,
    contentUrl: toAbsoluteUrl(t.contentUrl),
  }));
}

function normalizeAbsChapters(raw: unknown): AbsChapter[] | undefined {
  if (!Array.isArray(raw) || raw.length === 0) return undefined;
  return raw.map((c, i) => {
    const o = (c && typeof c === "object" ? c : {}) as Record<string, unknown>;
    const endRaw = o.end;
    return {
      id: typeof o.id === "number" ? o.id : i,
      title: String(o.title ?? `Chapter ${i + 1}`),
      start: Number(o.start) || 0,
      end: endRaw != null && endRaw !== "" ? Number(endRaw) : null,
    } satisfies AbsChapter;
  });
}

/** Fetch ABS track URLs for offline cache — prefers metadata (no play session). */
async function fetchAbsOfflinePlayInfo(itemId: string): Promise<{
  tracks: Track[];
  title: string;
  author: string;
  coverUrl: string;
  duration: number;
  chapters?: AbsChapter[];
  sessionId?: string;
}> {
  const pathId = encodeURIComponent(itemId);

  // Preferred: library-item metadata → proxy URLs (never requires Listen first).
  try {
    const { data } = await api.get(`/stream/abs/${pathId}/offline`);
    const tracks = absolutizeTracks((data.tracks || []) as Track[]);
    if (tracks.length) {
      return {
        tracks,
        title: data.title || "Audiobook",
        author: data.author || "",
        coverUrl: data.coverUrl ? toAbsoluteUrl(data.coverUrl) : "",
        duration: data.duration || 0,
        chapters: normalizeAbsChapters(data.chapters),
      };
    }
  } catch {
    // Fall through to /play handshake.
  }

  // Fallback: same handshake as playABS (starts a short-lived ABS session).
  void api.post(`/stream/abs/${pathId}/warmup`).catch(() => {});
  const { data } = await api.post(`/stream/abs/${pathId}/play`);
  const tracks = absolutizeTracks((data.tracks || []) as Track[]);
  const sessionId = data.sessionId as string | undefined;

  // Close the session immediately — we only needed stream URLs for caching.
  if (sessionId) {
    void api
      .post(`/stream/abs/${sessionId}/close`, {
        currentTime: 0,
        duration: data.duration || 0,
      })
      .catch(() => {});
  }

  let chapters = normalizeAbsChapters(data.chapters);
  if (!chapters) {
    try {
      const ch = await api.get<{ chapters: unknown }>(`/stream/abs/${pathId}/chapters`);
      chapters = normalizeAbsChapters(ch.data?.chapters);
    } catch {
      /* optional */
    }
  }

  return {
    tracks,
    title: data.title || "Audiobook",
    author: data.author || "",
    coverUrl: data.coverUrl ? toAbsoluteUrl(data.coverUrl) : "",
    duration: data.duration || 0,
    chapters,
    sessionId,
  };
}

export async function absDownloadState(itemId: string): Promise<"downloaded" | "idle"> {
  const m = getAbsOfflineManifest(itemId);
  if (m?.tracks.length && (await isBookCached(m.tracks))) return "downloaded";
  return "idle";
}

export async function rdDownloadState(opts: {
  libraryItemId?: number;
  streamHistoryId?: number;
  tracks?: CacheableTrack[];
}): Promise<"downloaded" | "idle"> {
  const m = getRdOfflineManifest(opts);
  const tracks = m?.tracks?.length ? m.tracks : opts.tracks;
  if (tracks?.length && (await isBookCached(tracks))) return "downloaded";
  return "idle";
}

export async function ebookDownloadState(
  chapterId: number,
  isPdf = true
): Promise<"downloaded" | "idle"> {
  const m = getEbookOfflineManifest(chapterId);
  const pdf = m?.isPdf ?? isPdf;
  if (await isEbookCached(chapterId, pdf)) return "downloaded";
  return "idle";
}

export async function downloadAbsOffline(
  itemId: string,
  onProgress?: (done: number, total: number) => void
): Promise<void> {
  const info = await fetchAbsOfflinePlayInfo(itemId);
  if (!info.tracks.length) throw new Error("No audio tracks to download");

  saveAbsOfflineManifest({
    itemId,
    title: info.title,
    author: info.author,
    coverUrl: info.coverUrl,
    tracks: info.tracks,
    totalDuration: info.duration,
    absChapters: info.chapters,
  });

  await cacheBookAudio(info.tracks, { immediate: true, onProgress });
  if (!(await isBookCached(info.tracks))) {
    throw new Error("Download incomplete — try again while online");
  }
}

export async function downloadRdOffline(opts: {
  libraryItemId?: number;
  streamHistoryId?: number;
  title: string;
  author?: string;
  coverUrl?: string;
  tracks: CacheableTrack[];
  totalDuration?: number;
  onProgress?: (done: number, total: number) => void;
}): Promise<void> {
  let tracks = opts.tracks;
  if (opts.libraryItemId != null && (!tracks.length || !tracks[0]?.contentUrl)) {
    const { data } = await api.post(`/library/${opts.libraryItemId}/play`);
    tracks = data.tracks?.length ? data.tracks : tracks;
    if (data.streamHistoryId != null) opts.streamHistoryId = data.streamHistoryId;
  }
  if (!tracks?.length) throw new Error("No audio tracks to download");
  tracks = absolutizeTracks(tracks);

  saveRdOfflineManifest({
    libraryItemId: opts.libraryItemId,
    streamHistoryId: opts.streamHistoryId,
    title: opts.title,
    author: opts.author || "",
    coverUrl: opts.coverUrl ? toAbsoluteUrl(opts.coverUrl) : opts.coverUrl || "",
    tracks: tracks as never,
    totalDuration: opts.totalDuration || 0,
  });

  await cacheBookAudio(tracks, { immediate: true, onProgress: opts.onProgress });
  if (!(await isBookCached(tracks))) {
    throw new Error("Download incomplete — try again while online");
  }
}

export async function downloadEbookOffline(opts: {
  chapterId: number;
  title: string;
  author?: string;
  coverUrl?: string;
  isPdf?: boolean;
}): Promise<void> {
  const isPdf = opts.isPdf ?? true;
  saveEbookOfflineManifest({
    chapterId: opts.chapterId,
    title: opts.title,
    author: opts.author || "",
    coverUrl: opts.coverUrl || "",
    isPdf,
  });
  const ok = await cacheBookEbook(opts.chapterId, isPdf, { immediate: true });
  if (!ok && !(await isEbookCached(opts.chapterId, isPdf))) {
    throw new Error("Ebook download failed — try again while online");
  }
}

export async function removeAbsOffline(itemId: string): Promise<void> {
  await clearAbsBookCache(itemId);
  removeAbsOfflineManifest(itemId);
}

export async function removeRdOffline(opts: {
  libraryItemId?: number;
  streamHistoryId?: number;
  tracks?: CacheableTrack[];
}): Promise<void> {
  const m = getRdOfflineManifest(opts);
  const tracks = m?.tracks || opts.tracks;
  if (tracks?.length) {
    await clearBookCacheForTracks(tracks);
  } else if (opts.libraryItemId != null) {
    // Best-effort: no URL prefix without tracks
    await clearBookCache("h", opts.libraryItemId).catch(() => undefined);
  }
  removeRdOfflineManifest(opts);
}

export async function removeEbookOffline(chapterId: number): Promise<void> {
  await clearEbookCache(chapterId);
  removeEbookOfflineManifest(chapterId);
}
