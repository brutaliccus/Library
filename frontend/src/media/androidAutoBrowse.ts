/**
 * Android Auto browse tree — Continue Listening, ABS library, Store (library titles only).
 */
import api from "../api/client";
import type { BookSummary } from "../types/book";
import type { PluginListenerHandle } from "@capacitor/core";
import { LibraryAuto, type BrowseChild } from "./libraryAutoPlugin";
import { toAbsoluteArtworkUrl } from "./playerMediaSession";

export const AA_NOW_PLAYING = "now_playing";

export const AA_ROOT = "library_root";
export const AA_CONTINUE = "continue";
export const AA_ABS = "abs";
export const AA_STORE = "store";
export const AA_ABS_GENRE_PREFIX = "abs/genre:";
export const AA_ABS_UNGROUPED = "abs/ungrouped";
export const AA_STORE_GENRE_PREFIX = "store/genre:";
export const AA_PLAY_ABS_PREFIX = "play/abs/";
export const AA_PLAY_RD_HIST_PREFIX = "play/rdhist/";
export const AA_PLAY_RD_LIB_PREFIX = "play/rdlib/";

interface ABSItem {
  itemId: string;
  title: string;
  author: string;
  coverUrl?: string;
  progress?: number;
  isFinished?: boolean;
}

interface InProgressABS {
  itemId: string;
  title: string;
  author: string;
  coverUrl?: string;
  isFinished?: boolean;
}

interface RDHistoryItem {
  id: number;
  title: string;
  author: string;
  coverUrl: string;
  tracks: Array<{
    index: number;
    title: string;
    contentUrl: string;
    mimeType: string;
    startOffset: number;
    duration: number;
  }>;
  progressSeconds: number;
  currentTrackIndex: number;
  trackPositionSeconds: number;
}

interface RDLibraryItem {
  id: number;
  googleVolumeId: string;
  title: string;
  author: string;
  coverUrl: string;
  streamStatus: string;
  tracks: RDHistoryItem["tracks"];
  progressSeconds: number;
}

interface LibraryIndex {
  absByTitle: Map<string, string>;
  rdByVolumeId: Map<string, RDLibraryItem>;
  rdByTitle: Map<string, RDLibraryItem>;
}

const rdHistPlayCache = new Map<string, RDHistoryItem>();
const rdLibPlayCache = new Map<string, RDLibraryItem>();

let libraryIndex: LibraryIndex | null = null;
let libraryIndexAt = 0;
const INDEX_TTL_MS = 5 * 60 * 1000;

let browseListener: PluginListenerHandle | null = null;

function normTitle(title: string): string {
  return title.toLowerCase().replace(/\s+/g, " ").trim();
}

function coverUri(url?: string, absItemId?: string): string | undefined {
  if (url?.trim()) {
    const abs = toAbsoluteArtworkUrl(url);
    if (abs) return abs;
  }
  if (absItemId) {
    return toAbsoluteArtworkUrl(`/api/stream/abs/proxy/cover/${absItemId}`);
  }
  return undefined;
}

function absCover(itemId: string): string | undefined {
  return coverUri(undefined, itemId);
}

function pctLabel(progress: number, durationSec?: number): string {
  if (progress > 0 && progress <= 1) return `${Math.round(progress * 100)}%`;
  if (durationSec && durationSec > 0) return "";
  return "";
}

async function getLibraryIndex(): Promise<LibraryIndex> {
  const now = Date.now();
  if (libraryIndex && now - libraryIndexAt < INDEX_TTL_MS) {
    return libraryIndex;
  }

  const absByTitle = new Map<string, string>();
  const rdByVolumeId = new Map<string, RDLibraryItem>();
  const rdByTitle = new Map<string, RDLibraryItem>();

  try {
    const [absRes, rdRes] = await Promise.all([
      api.get("/library/abs/collection"),
      api.get("/library"),
    ]);

    const genres = absRes.data?.genres ?? {};
    const ungrouped: ABSItem[] = absRes.data?.ungrouped ?? [];
    for (const items of Object.values(genres) as ABSItem[][]) {
      for (const item of items) {
        if (item.title && item.itemId) {
          absByTitle.set(normTitle(item.title), item.itemId);
        }
      }
    }
    for (const item of ungrouped) {
      if (item.title && item.itemId) {
        absByTitle.set(normTitle(item.title), item.itemId);
      }
    }

    for (const item of (rdRes.data?.items ?? []) as RDLibraryItem[]) {
      if (item.googleVolumeId) rdByVolumeId.set(item.googleVolumeId, item);
      if (item.title) rdByTitle.set(normTitle(item.title), item);
    }
  } catch {
    // Offline / logged out — empty index
  }

  libraryIndex = { absByTitle, rdByVolumeId, rdByTitle };
  libraryIndexAt = now;
  return libraryIndex;
}

function matchStoreBook(book: BookSummary, index: LibraryIndex): BrowseChild | null {
  const rd = index.rdByVolumeId.get(book.id);
  if (rd && rd.streamStatus === "ready" && rd.tracks?.length) {
    const mediaId = `${AA_PLAY_RD_LIB_PREFIX}${rd.id}`;
    rdLibPlayCache.set(mediaId, rd);
    return {
      mediaId,
      title: book.title,
      subtitle: book.authors?.[0] || rd.author || "",
      browsable: false,
      iconUri: coverUri(rd.coverUrl) || coverUri(book.coverUrl),
    };
  }

  const absId = index.absByTitle.get(normTitle(book.title));
  if (absId) {
    return {
      mediaId: `${AA_PLAY_ABS_PREFIX}${absId}`,
      title: book.title,
      subtitle: book.authors?.[0] || "",
      browsable: false,
      iconUri: absCover(absId) || coverUri(book.coverUrl),
    };
  }

  const rdTitle = index.rdByTitle.get(normTitle(book.title));
  if (rdTitle && rdTitle.streamStatus === "ready" && rdTitle.tracks?.length) {
    const mediaId = `${AA_PLAY_RD_LIB_PREFIX}${rdTitle.id}`;
    rdLibPlayCache.set(mediaId, rdTitle);
    return {
      mediaId,
      title: book.title,
      subtitle: book.authors?.[0] || rdTitle.author || "",
      browsable: false,
      iconUri: coverUri(rdTitle.coverUrl) || coverUri(book.coverUrl),
    };
  }

  return null;
}

async function loadContinueListening(): Promise<BrowseChild[]> {
  rdHistPlayCache.clear();
  const children: BrowseChild[] = [];

  try {
    const [absRes, rdRes] = await Promise.all([
      api.get("/stream/abs/in-progress"),
      api.get("/stream/rd/history/in-progress"),
    ]);

    for (const item of (absRes.data?.items ?? []) as InProgressABS[]) {
      if (item.isFinished) continue;
      children.push({
        mediaId: `${AA_PLAY_ABS_PREFIX}${item.itemId}`,
        title: item.title,
        subtitle: item.author || "Audiobookshelf",
        browsable: false,
        iconUri: coverUri(item.coverUrl, item.itemId),
      });
    }

    for (const item of (rdRes.data?.items ?? []) as RDHistoryItem[]) {
      if (!item.tracks?.length) continue;
      const mediaId = `${AA_PLAY_RD_HIST_PREFIX}${item.id}`;
      rdHistPlayCache.set(mediaId, item);
      children.push({
        mediaId,
        title: item.title,
        subtitle: item.author || "Streaming",
        browsable: false,
        iconUri: coverUri(item.coverUrl),
      });
    }
  } catch {
    // ignore
  }

  return children.slice(0, 24);
}

async function loadAbsRoot(): Promise<BrowseChild[]> {
  const children: BrowseChild[] = [];
  try {
    const { data } = await api.get("/library/abs/collection");
    const genres = data?.genres ?? {};
    const ungrouped: ABSItem[] = data?.ungrouped ?? [];

    for (const name of Object.keys(genres).sort()) {
      const items = genres[name] as ABSItem[];
      const sample = items[0];
      children.push({
        mediaId: `${AA_ABS_GENRE_PREFIX}${encodeURIComponent(name)}`,
        title: name,
        subtitle: `${items.length} title${items.length === 1 ? "" : "s"}`,
        browsable: true,
        iconUri: sample ? coverUri(sample.coverUrl, sample.itemId) : undefined,
      });
    }

    if (ungrouped.length > 0) {
      const sample = ungrouped[0];
      children.push({
        mediaId: AA_ABS_UNGROUPED,
        title: "Other",
        subtitle: `${ungrouped.length} title${ungrouped.length === 1 ? "" : "s"}`,
        browsable: true,
        iconUri: sample ? coverUri(sample.coverUrl, sample.itemId) : undefined,
      });
    }
  } catch {
    // ignore
  }
  return children;
}

async function loadAbsGenre(parentId: string): Promise<BrowseChild[]> {
  const encoded = parentId.slice(AA_ABS_GENRE_PREFIX.length);
  const genreName = decodeURIComponent(encoded);
  try {
    const { data } = await api.get("/library/abs/collection");
    const items = (data?.genres?.[genreName] ?? []) as ABSItem[];
    return items.slice(0, 40).map((item) => ({
      mediaId: `${AA_PLAY_ABS_PREFIX}${item.itemId}`,
      title: item.title,
      subtitle: item.author || pctLabel(item.progress ?? 0),
      browsable: false,
      iconUri: coverUri(item.coverUrl, item.itemId),
    }));
  } catch {
    return [];
  }
}

async function loadAbsUngrouped(): Promise<BrowseChild[]> {
  try {
    const { data } = await api.get("/library/abs/collection");
    const items = (data?.ungrouped ?? []) as ABSItem[];
    return items.slice(0, 40).map((item) => ({
      mediaId: `${AA_PLAY_ABS_PREFIX}${item.itemId}`,
      title: item.title,
      subtitle: item.author || "",
      browsable: false,
      iconUri: coverUri(item.coverUrl, item.itemId),
    }));
  } catch {
    return [];
  }
}

async function loadStoreRoot(): Promise<BrowseChild[]> {
  try {
    const { data } = await api.get("/books/genres");
    const genres = data?.genres ?? [];
    return genres.map((g: { slug: string; name: string }) => ({
      mediaId: `${AA_STORE_GENRE_PREFIX}${g.slug}`,
      title: g.name,
      subtitle: "In your library only",
      browsable: true,
    }));
  } catch {
    return [];
  }
}

async function loadStoreGenre(parentId: string): Promise<BrowseChild[]> {
  const slug = parentId.slice(AA_STORE_GENRE_PREFIX.length);
  try {
    const [booksRes, index] = await Promise.all([
      api.get(`/books/category/${encodeURIComponent(slug)}?pageSize=40`),
      getLibraryIndex(),
    ]);
    const books = (booksRes.data?.books ?? []) as BookSummary[];
    const children: BrowseChild[] = [];
    for (const book of books) {
      const match = matchStoreBook(book, index);
      if (match) children.push(match);
    }
    return children;
  } catch {
    return [];
  }
}

export async function loadBrowseChildren(parentId: string): Promise<BrowseChild[]> {
  if (parentId === AA_CONTINUE) return loadContinueListening();
  if (parentId === AA_ABS) return loadAbsRoot();
  if (parentId === AA_STORE) return loadStoreRoot();
  if (parentId.startsWith(AA_ABS_GENRE_PREFIX)) return loadAbsGenre(parentId);
  if (parentId === AA_ABS_UNGROUPED) return loadAbsUngrouped();
  if (parentId.startsWith(AA_STORE_GENRE_PREFIX)) return loadStoreGenre(parentId);
  return [];
}

export interface AutoPlayHandlers {
  playABS: (itemId: string) => Promise<void>;
  playRD: (
    tracks: RDHistoryItem["tracks"],
    title: string,
    author?: string,
    coverUrl?: string,
    streamHistoryId?: number,
    resume?:
      | number
      | {
          startAt?: number;
          trackIndex?: number;
          trackPositionSeconds?: number;
        }
  ) => void;
  togglePlay: () => void;
}

export async function handlePlayMediaId(
  mediaId: string,
  handlers: AutoPlayHandlers
): Promise<void> {
  if (mediaId === AA_NOW_PLAYING) {
    handlers.togglePlay();
    return;
  }

  try {
    await LibraryAuto.bringToForeground();
  } catch {
    /* native only */
  }

  if (mediaId.startsWith(AA_PLAY_ABS_PREFIX)) {
    await handlers.playABS(mediaId.slice(AA_PLAY_ABS_PREFIX.length));
    return;
  }

  if (mediaId.startsWith(AA_PLAY_RD_HIST_PREFIX)) {
    let item = rdHistPlayCache.get(mediaId);
    if (!item) {
      const histId = parseInt(mediaId.slice(AA_PLAY_RD_HIST_PREFIX.length), 10);
      if (!isNaN(histId)) {
        try {
          const { data } = await api.get("/stream/rd/history/in-progress");
          item = (data?.items ?? []).find((i: RDHistoryItem) => i.id === histId);
          if (item) rdHistPlayCache.set(mediaId, item);
        } catch {
          /* ignore */
        }
      }
    }
    if (!item?.tracks?.length) return;
    handlers.playRD(item.tracks, item.title, item.author, item.coverUrl, item.id, {
      startAt: item.progressSeconds,
      trackIndex: item.currentTrackIndex,
      trackPositionSeconds: item.trackPositionSeconds,
    });
    return;
  }

  if (mediaId.startsWith(AA_PLAY_RD_LIB_PREFIX)) {
    let item = rdLibPlayCache.get(mediaId);
    if (!item) {
      const libId = parseInt(mediaId.slice(AA_PLAY_RD_LIB_PREFIX.length), 10);
      if (!isNaN(libId)) {
        try {
          const { data } = await api.get("/library");
          item = (data?.items ?? []).find((i: RDLibraryItem) => i.id === libId);
          if (item) rdLibPlayCache.set(mediaId, item);
        } catch {
          /* ignore */
        }
      }
    }
    if (!item?.tracks?.length) return;
    handlers.playRD(
      item.tracks,
      item.title,
      item.author,
      item.coverUrl,
      undefined,
      item.progressSeconds > 0 ? item.progressSeconds : 0
    );
  }
}

export async function startAndroidAutoBrowseListener(): Promise<void> {
  if (browseListener) return;

  browseListener = await LibraryAuto.addListener(
    "browseRequest",
    async (event: { parentId: string; requestId: string }) => {
      try {
        const children = await loadBrowseChildren(event.parentId);
        await LibraryAuto.resolveBrowseChildren({
          requestId: event.requestId,
          children,
        });
      } catch {
        await LibraryAuto.resolveBrowseChildren({
          requestId: event.requestId,
          children: [],
        });
      }
    }
  );
}

export async function stopAndroidAutoBrowseListener(): Promise<void> {
  if (browseListener) {
    await browseListener.remove();
    browseListener = null;
  }
}
