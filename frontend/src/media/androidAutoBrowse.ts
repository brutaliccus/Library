/**
 * Android Auto browse tree — Continue Listening + alphabetical Library (A–Z jump).
 *
 * Live loads go through the WebView + API. When the phone is locked, Chromium is
 * often frozen / Doze-throttled, so we also push a durable native cache that
 * LibraryMediaBrowserService can serve without JS.
 */
import api from "../api/client";
import type { PluginListenerHandle } from "@capacitor/core";
import { Capacitor } from "@capacitor/core";
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
  currentTime?: number;
  duration?: number;
  progress?: number;
  updatedAt?: string | number;
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
let prefetchInFlight: Promise<void> | null = null;
let lastPrefetchAt = 0;
const PREFETCH_MIN_INTERVAL_MS = 60_000;

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

async function persistBrowseFolder(
  parentId: string,
  children: BrowseChild[],
  opts?: { allowEmpty?: boolean }
): Promise<void> {
  if (Capacitor.getPlatform() !== "android") return;
  try {
    await LibraryAuto.cacheBrowseChildren({
      parentId,
      children,
      allowEmpty: opts?.allowEmpty === true,
    });
  } catch {
    /* plugin unavailable */
  }
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
    // Offline / data-restricted — keep memory cache; never pretend "empty library".
    if (absItemsCache) return absItemsCache;
    throw new Error("ABS collection unavailable");
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

  // Throw on failure so callers do not persist [] over a warm native cache
  // (locked phone / restricted data often fails these APIs).
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
    // Warm native ExoPlayer cache: offline manifest first, else /offline API.
    void warmAbsPlayableCache(item);
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
    // Warm native ExoPlayer cache while unlocked — locked AA play needs URLs.
    // RD API tracks ship startOffset:0; must recalc + resolve before caching
    // or AA seeks track-local time into the wrong file / whole-book timeline.
    void warmRdPlayableCache(item);
  }

  return children.slice(0, 24);
}

function parseUpdatedAtMs(raw?: string | number | null): number | null {
  if (raw == null || raw === "") return null;
  if (typeof raw === "number" && Number.isFinite(raw)) {
    // ABS lastUpdate is often epoch ms; treat small values as seconds.
    return raw < 1e12 ? raw * 1000 : raw;
  }
  const ms = Date.parse(String(raw));
  return Number.isFinite(ms) ? ms : null;
}

async function warmRdPlayableCache(item: RDHistoryItem): Promise<void> {
  try {
    const { recalcTrackOffsets, resolveTrackResume } = await import(
      "../utils/resumeProgress"
    );
    const { cacheRdPlayable } = await import("./aaPlayableCache");
    const tracks = item.tracks.map((t) => ({ ...t }));
    const total = recalcTrackOffsets(tracks);
    const durationsKnown = tracks.every((t) => (t.duration || 0) > 0);
    const resume = resolveTrackResume({
      tracks,
      globalSeconds: item.progressSeconds || 0,
      trackIndex: item.currentTrackIndex,
      trackLocal: item.trackPositionSeconds,
      preferTrackHints: !durationsKnown,
    });
    await cacheRdPlayable(
      item.id,
      item.title,
      item.author,
      item.coverUrl,
      tracks,
      total > 0 ? total : tracks.reduce((s, t) => s + (t.duration || 0), 0),
      resume.trackLocal,
      resume.trackIndex
    );
  } catch {
    /* offline / API fail — browse still works from cache */
  }
}

async function warmAbsPlayableCache(item: InProgressABS): Promise<void> {
  try {
    const {
      getAbsOfflineManifest,
      getOfflineProgress,
      progressKeyForAbs,
    } = await import("../utils/offlinePlayback");
    const { pickResumeSeconds, resolveTrackResume } = await import(
      "../utils/resumeProgress"
    );
    const { cacheAbsPlayable } = await import("./aaPlayableCache");
    const local = getOfflineProgress(progressKeyForAbs(item.itemId));
    const serverSec = Number(item.currentTime) || 0;
    const globalSec = pickResumeSeconds({
      serverSeconds: serverSec,
      serverUpdatedAtMs: parseUpdatedAtMs(item.updatedAt),
      localSeconds: local?.time,
      localUpdatedAtMs: local?.updatedAt,
    });

    const m = getAbsOfflineManifest(item.itemId);
    if (m?.tracks?.length) {
      const resume = resolveTrackResume({
        tracks: m.tracks,
        globalSeconds: globalSec,
        trackIndex: local?.trackIndex,
        trackLocal: local?.trackLocal,
      });
      await cacheAbsPlayable(
        item.itemId,
        m.title || item.title,
        m.author || item.author,
        m.coverUrl || item.coverUrl || "",
        m.tracks,
        m.totalDuration,
        resume.trackLocal,
        resume.trackIndex,
        m.absChapters
      );
      return;
    }
    // Never-played-but-in-progress: /offline gives proxy URLs without a session.
    const { data } = await api.get(`/stream/abs/${encodeURIComponent(item.itemId)}/offline`);
    if (!data?.tracks?.length) return;
    const resume = resolveTrackResume({
      tracks: data.tracks,
      globalSeconds: globalSec,
      trackIndex: local?.trackIndex,
      trackLocal: local?.trackLocal,
    });
    await cacheAbsPlayable(
      item.itemId,
      data.title || item.title,
      data.author || item.author,
      data.coverUrl || item.coverUrl || "",
      data.tracks,
      data.duration || 0,
      resume.trackLocal,
      resume.trackIndex
    );
  } catch {
    /* offline / API fail — browse still works from cache */
  }
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

/**
 * Warm the native browse cache while the app is foregrounded / networked so
 * Android Auto still has Continue + Library when the phone is locked later.
 */
export async function prefetchAndroidAutoBrowseCache(force = false): Promise<void> {
  if (Capacitor.getPlatform() !== "android") return;
  // Avoid disk + MediaBrowser notify storms while audio is playing — the
  // durable cache is already warm from the last foreground prefetch.
  try {
    const { isAudioPlaybackActive } = await import("../utils/mediaStorage");
    if (isAudioPlaybackActive()) return;
  } catch {
    /* ignore */
  }
  const now = Date.now();
  if (!force && now - lastPrefetchAt < PREFETCH_MIN_INTERVAL_MS) return;
  if (prefetchInFlight) return prefetchInFlight;

  prefetchInFlight = (async () => {
    try {
      try {
        const continueChildren = await loadContinueListening();
        // Live confirm — allow clearing Continue when nothing is in progress.
        await persistBrowseFolder(AA_CONTINUE, continueChildren, { allowEmpty: true });
      } catch (err) {
        console.warn("Android Auto Continue prefetch skipped:", err);
      }

      const libraryRoot = await loadLibraryRoot();
      await persistBrowseFolder(AA_LIBRARY, libraryRoot, { allowEmpty: true });

      const items = await getAllAbsItems();
      const byLetter = new Map<string, BrowseChild[]>();
      for (const item of items) {
        const letter = letterBucket(item.title);
        const list = byLetter.get(letter) ?? [];
        list.push(absItemToChild(item));
        byLetter.set(letter, list);
      }
      for (const [letter, children] of byLetter) {
        await persistBrowseFolder(`${AA_LIBRARY_LETTER_PREFIX}${letter}`, children);
      }
      lastPrefetchAt = Date.now();
    } catch (err) {
      console.warn("Android Auto browse prefetch failed:", err);
    } finally {
      prefetchInFlight = null;
    }
  })();

  return prefetchInFlight;
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
  // If native ExoPlayer already owns *this* mediaId, attach-only (no second decoder).
  // A different mediaId must fall through so browse→play can switch titles.
  try {
    const { isNativePlaybackOwner, handOffNativeToWebView } = await import("./libraryAuto");
    if (isNativePlaybackOwner()) {
      try {
        const st = await LibraryAuto.getNativePlaybackState();
        if (st?.nativeOwner && st.mediaId && st.mediaId === mediaId) {
          handlers.play();
          return;
        }
      } catch {
        /* ignore */
      }
      await handOffNativeToWebView().catch(() => {});
    }
  } catch {
    /* ignore */
  }

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
    // Warm native playable cache for next locked-phone AA play.
    void warmRdPlayableCache(item);
    handlers.playRD(item.tracks, item.title, item.author, item.coverUrl, item.id, {
      startAt: item.progressSeconds,
      trackIndex: item.currentTrackIndex,
      trackPositionSeconds: item.trackPositionSeconds,
    });
  }
}

export async function startAndroidAutoBrowseListener(): Promise<void> {
  if (browseListener) {
    void prefetchAndroidAutoBrowseCache();
    return;
  }

  browseListener = await LibraryAuto.addListener(
    "browseRequest",
    async (event: { parentId: string; requestId: string }) => {
      try {
        const children = await loadBrowseChildren(event.parentId);
        // Live success only — allowEmpty so a real empty Continue can clear stale rows.
        await persistBrowseFolder(event.parentId, children, { allowEmpty: true });
        await LibraryAuto.resolveBrowseChildren({
          requestId: event.requestId,
          children,
        });
      } catch {
        // API blocked / WebView thaw failed — native keeps prior cache.
        await LibraryAuto.resolveBrowseChildren({
          requestId: event.requestId,
          children: [],
        });
      }
    }
  );

  void prefetchAndroidAutoBrowseCache(true);
}

export async function stopAndroidAutoBrowseListener(): Promise<void> {
  if (browseListener) {
    await browseListener.remove();
    browseListener = null;
  }
}
