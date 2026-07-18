/**
 * Android Auto browse tree — Continue Listening + alphabetical Library (A–Z jump).
 */
import api from "../api/client";
import type { PluginListenerHandle } from "@capacitor/core";
import { LibraryAuto, type BrowseChild } from "./libraryAutoPlugin";
import { toAbsoluteArtworkUrl } from "./playerMediaSession";

export const AA_NOW_PLAYING = "now_playing";

export const AA_ROOT = "library_root";
export const AA_CONTINUE = "continue";
export const AA_LIBRARY = "library";
export const AA_LIBRARY_LETTER_PREFIX = "library/letter:";
export const AA_PLAY_ABS_PREFIX = "play/abs/";
export const AA_PLAY_RD_HIST_PREFIX = "play/rdhist/";

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

const rdHistPlayCache = new Map<string, RDHistoryItem>();

let absItemsCache: ABSItem[] | null = null;
let absItemsCacheAt = 0;
const INDEX_TTL_MS = 5 * 60 * 1000;

let browseListener: PluginListenerHandle | null = null;

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

function sortTitle(title: string): string {
  return title.replace(/^(the|a|an)\s+/i, "").trim().toLowerCase();
}

function letterBucket(title: string): string {
  const t = sortTitle(title);
  const ch = t[0]?.toUpperCase() ?? "";
  if (ch >= "A" && ch <= "Z") return ch;
  return "#";
}

async function getAllAbsItems(): Promise<ABSItem[]> {
  const now = Date.now();
  if (absItemsCache && now - absItemsCacheAt < INDEX_TTL_MS) {
    return absItemsCache;
  }

  const all: ABSItem[] = [];
  const seen = new Set<string>();

  try {
    const { data } = await api.get("/library/abs/collection");
    const genres = data?.genres ?? {};
    const ungrouped: ABSItem[] = data?.ungrouped ?? [];

    for (const items of Object.values(genres) as ABSItem[][]) {
      for (const item of items) {
        if (item.itemId && item.title && !seen.has(item.itemId)) {
          seen.add(item.itemId);
          all.push(item);
        }
      }
    }
    for (const item of ungrouped) {
      if (item.itemId && item.title && !seen.has(item.itemId)) {
        seen.add(item.itemId);
        all.push(item);
      }
    }
  } catch {
    // Offline / logged out — return stale cache if any
    return absItemsCache ?? [];
  }

  all.sort((a, b) => sortTitle(a.title).localeCompare(sortTitle(b.title)));
  absItemsCache = all;
  absItemsCacheAt = now;
  return all;
}

function absItemToChild(item: ABSItem): BrowseChild {
  return {
    mediaId: `${AA_PLAY_ABS_PREFIX}${item.itemId}`,
    title: item.title,
    subtitle: item.author || "",
    browsable: false,
    iconUri: coverUri(item.coverUrl, item.itemId),
  };
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

async function loadLibraryRoot(): Promise<BrowseChild[]> {
  const items = await getAllAbsItems();
  const letters = new Set(items.map((i) => letterBucket(i.title)));
  const children: BrowseChild[] = [];

  for (let code = 65; code <= 90; code++) {
    const letter = String.fromCharCode(code);
    if (!letters.has(letter)) continue;
    const count = items.filter((i) => letterBucket(i.title) === letter).length;
    children.push({
      mediaId: `${AA_LIBRARY_LETTER_PREFIX}${letter}`,
      title: letter,
      subtitle: `${count} title${count === 1 ? "" : "s"}`,
      browsable: true,
    });
  }

  if (letters.has("#")) {
    const count = items.filter((i) => letterBucket(i.title) === "#").length;
    children.push({
      mediaId: `${AA_LIBRARY_LETTER_PREFIX}#`,
      title: "#",
      subtitle: `${count} title${count === 1 ? "" : "s"}`,
      browsable: true,
    });
  }

  return children;
}

async function loadLibraryLetter(parentId: string): Promise<BrowseChild[]> {
  const letter = parentId.slice(AA_LIBRARY_LETTER_PREFIX.length);
  const items = await getAllAbsItems();
  return items
    .filter((i) => letterBucket(i.title) === letter)
    .map(absItemToChild);
}

export async function loadBrowseChildren(parentId: string): Promise<BrowseChild[]> {
  if (parentId === AA_CONTINUE) return loadContinueListening();
  if (parentId === AA_LIBRARY) return loadLibraryRoot();
  if (parentId.startsWith(AA_LIBRARY_LETTER_PREFIX)) return loadLibraryLetter(parentId);
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
  /** Explicit resume (not toggle) — required after phone-call / car interruptions. */
  play: () => void;
  togglePlay: () => void;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function handlePlayMediaId(
  mediaId: string,
  handlers: AutoPlayHandlers
): Promise<void> {
  try {
    await LibraryAuto.bringToForeground();
    // Give the WebView time to resume after a cold start / interruption.
    await sleep(450);
  } catch {
    /* native only */
  }

  if (mediaId === AA_NOW_PLAYING) {
    // Explicit play — toggle would invert if native/web state desynced after a call.
    handlers.play();
    return;
  }

  if (mediaId.startsWith(AA_PLAY_ABS_PREFIX)) {
    const itemId = mediaId.slice(AA_PLAY_ABS_PREFIX.length);
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        await handlers.playABS(itemId);
        return;
      } catch {
        if (attempt < 3) await sleep(600 * (attempt + 1));
      }
    }
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
