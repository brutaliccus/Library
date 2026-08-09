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
import { cacheCover, clearAbsCoverCache, clearCover } from "./coverCache";
import { cacheBookEbook, clearEbookCache, isEbookCached } from "./ebookCache";
import {
  getAbsOfflineManifest,
  getEbookOfflineManifest,
  getRdOfflineManifest,
  isEbookOfflineReady,
  removeAbsOfflineManifest,
  removeEbookOfflineManifest,
  removeRdOfflineManifest,
  saveAbsOfflineManifest,
  saveEbookOfflineManifest,
  saveRdOfflineManifest,
} from "./offlinePlayback";
import type { AbsChapter, Track } from "../types/player";

export type OfflineBookMeta = {
  narrator?: string;
  subtitle?: string;
  seriesName?: string;
  sequence?: string;
  description?: string;
  asin?: string;
  genres?: string[];
  publishedYear?: string;
};

function normalizeOfflineMeta(data: Record<string, unknown> | null | undefined): OfflineBookMeta {
  if (!data || typeof data !== "object") return {};
  const genresRaw = data.genres;
  const genres = Array.isArray(genresRaw)
    ? genresRaw.map((g) => String(g || "").trim()).filter(Boolean)
    : undefined;
  return {
    narrator: String(data.narrator || "").trim() || undefined,
    subtitle: String(data.subtitle || "").trim() || undefined,
    seriesName: String(data.seriesName || "").trim() || undefined,
    sequence: String(data.sequence || "").trim() || undefined,
    description: String(data.description || "").trim() || undefined,
    asin: String(data.asin || "").trim() || undefined,
    genres: genres?.length ? genres : undefined,
    publishedYear: String(data.publishedYear || "").trim() || undefined,
  };
}

/** Kavita MangaFormat Pdf = 4 */
const KAVITA_PDF_FORMAT = 4;

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
async function fetchAbsChaptersFallback(pathId: string): Promise<AbsChapter[] | undefined> {
  try {
    const ch = await api.get<{ chapters: unknown }>(`/stream/abs/${pathId}/chapters`);
    return normalizeAbsChapters(ch.data?.chapters);
  } catch {
    return undefined;
  }
}

async function fetchAbsOfflinePlayInfo(
  itemId: string,
  shareToken?: string
): Promise<{
  tracks: Track[];
  title: string;
  author: string;
  coverUrl: string;
  duration: number;
  chapters?: AbsChapter[];
  meta: OfflineBookMeta;
  sessionId?: string;
}> {
  const pathId = encodeURIComponent(itemId);

  // Guest share: public offline endpoint (no JWT / no ABS play tracking).
  if (shareToken) {
    const { data } = await api.get(`/share/${encodeURIComponent(shareToken)}/offline`);
    const tracks = absolutizeTracks((data.tracks || []) as Track[]);
    if (!tracks.length) throw new Error("No audio tracks found");
    let chapters = normalizeAbsChapters(data.chapters);
    if (!chapters && data.absItemId) {
      chapters = await fetchAbsChaptersFallback(encodeURIComponent(String(data.absItemId)));
    }
    return {
      tracks,
      title: data.title || "Audiobook",
      author: data.author || "",
      coverUrl: data.coverUrl ? toAbsoluteUrl(data.coverUrl) : "",
      duration: data.duration || 0,
      chapters,
      meta: normalizeOfflineMeta(data),
    };
  }

  // Preferred: library-item metadata → proxy URLs (never requires Listen first).
  try {
    const { data } = await api.get(`/stream/abs/${pathId}/offline`);
    const tracks = absolutizeTracks((data.tracks || []) as Track[]);
    if (tracks.length) {
      let chapters = normalizeAbsChapters(data.chapters);
      if (!chapters) {
        chapters = await fetchAbsChaptersFallback(pathId);
      }
      return {
        tracks,
        title: data.title || "Audiobook",
        author: data.author || "",
        coverUrl: data.coverUrl ? toAbsoluteUrl(data.coverUrl) : "",
        duration: data.duration || 0,
        chapters,
        meta: normalizeOfflineMeta(data),
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
    chapters = await fetchAbsChaptersFallback(pathId);
  }

  return {
    tracks,
    title: data.title || "Audiobook",
    author: data.author || "",
    coverUrl: data.coverUrl ? toAbsoluteUrl(data.coverUrl) : "",
    duration: data.duration || 0,
    chapters,
    meta: normalizeOfflineMeta(data),
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

export async function ebookDownloadState(chapterId: number): Promise<"downloaded" | "idle"> {
  return (await isEbookOfflineReady(chapterId)) ? "downloaded" : "idle";
}

export async function downloadAbsOffline(
  itemId: string,
  onProgress?: (done: number, total: number) => void,
  shareToken?: string
): Promise<void> {
  const info = await fetchAbsOfflinePlayInfo(itemId, shareToken);
  if (!info.tracks.length) throw new Error("No audio tracks to download");

  // Cover first so shelf artwork is ready while audio downloads.
  if (info.coverUrl) {
    await cacheCover(info.coverUrl);
  }

  saveAbsOfflineManifest({
    itemId,
    title: info.title,
    author: info.author,
    coverUrl: info.coverUrl,
    tracks: info.tracks,
    totalDuration: info.duration,
    absChapters: info.chapters,
    ...info.meta,
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

  const coverUrl = opts.coverUrl ? toAbsoluteUrl(opts.coverUrl) : opts.coverUrl || "";
  if (coverUrl) {
    await cacheCover(coverUrl);
  }

  saveRdOfflineManifest({
    libraryItemId: opts.libraryItemId,
    streamHistoryId: opts.streamHistoryId,
    title: opts.title,
    author: opts.author || "",
    coverUrl,
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
  onProgress?: (done: number, total: number) => void;
}): Promise<void> {
  let isPdf = opts.isPdf;
  let pages = 0;
  let title = opts.title;
  try {
    const { data } = await api.get(`/library/reader/${opts.chapterId}/book-info`);
    if (typeof data?.seriesFormat === "number") {
      isPdf = data.seriesFormat === KAVITA_PDF_FORMAT;
    }
    if (typeof data?.pages === "number") pages = data.pages;
    if (data?.bookTitle || data?.seriesName) {
      title = data.bookTitle || data.seriesName || title;
    }
  } catch {
    /* keep caller hint */
  }
  if (isPdf == null) isPdf = true;

  const coverUrl = opts.coverUrl ? toAbsoluteUrl(opts.coverUrl) : opts.coverUrl || "";
  if (coverUrl) {
    await cacheCover(coverUrl);
  }

  saveEbookOfflineManifest({
    chapterId: opts.chapterId,
    title,
    author: opts.author || "",
    coverUrl,
    isPdf,
    pages: pages || undefined,
    pagesCached: false,
  });

  if (isPdf) {
    const ok = await cacheBookEbook(opts.chapterId, true, { immediate: true });
    if (!ok && !(await isEbookCached(opts.chapterId, true))) {
      throw new Error("Ebook download failed — try again while online");
    }
    return;
  }

  // EPUB: cache the source .epub for foliate-js (same pattern as PDF).
  opts.onProgress?.(0, 1);
  const ok = await cacheBookEbook(opts.chapterId, false, { immediate: true });
  if (!ok && !(await isEbookCached(opts.chapterId, false))) {
    throw new Error("Ebook download failed — try again while online");
  }
  opts.onProgress?.(1, 1);
  saveEbookOfflineManifest({
    chapterId: opts.chapterId,
    title,
    author: opts.author || "",
    coverUrl,
    isPdf: false,
    pages: pages || undefined,
    pagesCached: false,
  });
}

export async function removeAbsOffline(itemId: string): Promise<void> {
  const m = getAbsOfflineManifest(itemId);
  await clearAbsBookCache(itemId);
  await clearAbsCoverCache(itemId);
  if (m?.coverUrl) await clearCover(m.coverUrl);
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
  if (m?.coverUrl) await clearCover(m.coverUrl);
  removeRdOfflineManifest(opts);
}

export async function removeEbookOffline(chapterId: number): Promise<void> {
  const m = getEbookOfflineManifest(chapterId);
  await clearEbookCache(chapterId);
  if (m?.coverUrl) await clearCover(m.coverUrl);
  removeEbookOfflineManifest(chapterId);
}
