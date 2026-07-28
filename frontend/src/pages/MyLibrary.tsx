import { useState, useCallback, useRef, useEffect, useLayoutEffect, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import api from "../api/client";
import { useAuth } from "../hooks/useAuth";
import { useToast } from "../contexts/ToastContext";
import { usePlayer } from "../contexts/PlayerContext";
import ABSBookCard from "../components/ABSBookCard";
import BookCardSkeleton from "../components/BookCardSkeleton";
import SeriesDrilldown from "../components/SeriesDrilldown";
import AuthImage from "../components/AuthImage";
import CoverImage from "../components/CoverImage";
import Modal from "../components/Modal";
import {
  Library,
  Play,
  Trash2,
  Loader2,
  Search,
  Compass,
  BookOpen,
  Headphones,
  Layers,
  ChevronLeft,
  ChevronRight,
  X,
  RefreshCw,
  Download,
  ListMusic,
  Heart,
  CheckCircle2,
} from "lucide-react";
import CompactFilterSelect from "../components/CompactFilterSelect";
import ContinueShelves from "../components/ContinueShelves";
import { getProgress, clearProgress } from "../utils/readingProgress";
import { isBookCached } from "../utils/audioCache";
import {
  getOfflineProgress,
  getRdOfflineManifest,
  isAbsOfflineReady,
  isEbookOfflineReady,
  isLikelyOffline,
  isRdOfflineReady,
  listDownloadedItems,
  progressKeyForRd,
  type OfflineManifest,
} from "../utils/offlinePlayback";
import {
  removeAbsOffline,
  removeEbookOffline,
  removeRdOffline,
} from "../utils/downloadOffline";
import {
  absCollectionHasOrphans,
  mergeAbsCollection,
  mergeKavitaCollection,
  mergeStreamingLibrary,
  shouldBustLibraryCollectionCache,
  softRefreshLibraryCollectionQueries,
  stripCollectionEntriesFromPersist,
} from "../utils/shelfQueryCache";
import { useOnlineStatus } from "../hooks/useOnlineStatus";
import SaveOfflineButton from "../components/SaveOfflineButton";
import ShelfCardMeta from "../components/ShelfCardMeta";
import {
  loadLibraryScrollMemory,
  saveLibraryScrollMemory,
  type LibraryScrollMemory,
} from "../utils/libraryScrollMemory";

interface LibraryItem {
  id: number;
  googleVolumeId: string;
  title: string;
  author: string;
  coverUrl: string;
  genre: string;
  genres?: string[];
  seriesName?: string;
  sequence?: string;
  magnetLink: string;
  streamStatus: string;
  progressSeconds: number;
  totalSeconds: number;
  tracks: Array<{
    index: number; title: string; contentUrl: string; mimeType: string;
    startOffset: number; duration: number;
  }>;
  createdAt: string;
  updatedAt: string;
}

interface ABSItem {
  itemId: string;
  title: string;
  author: string;
  coverUrl: string;
  genres: string[];
  series: Array<{ id?: string; name: string; sequence: string }>;
  seriesName?: string;
  sequence?: string;
  duration: number;
  progress: number;
  isFinished: boolean;
  narrator: string;
  numTracks: number;
  addedAt?: number;
}

interface ABSSeries {
  id: string;
  name: string;
  books: Array<ABSItem & { sequence: string }>;
  bookCount: number;
  totalDuration: number;
  coverUrl: string;
}

interface SearchResult {
  title: string;
  author: string;
  coverUrl: string;
  source: "abs" | "rd" | "kavita";
  itemId?: string;
  libraryItemId?: number;
  googleVolumeId?: string;
  seriesId?: number;
  chapterId?: number;
  streamStatus?: string;
  tracks?: any[];
}

interface KavitaItem {
  seriesId: number;
  title: string;
  author: string;
  coverUrl: string;
  chapterId: number | null;
  genres?: string[];
  seriesName?: string;
  sequence?: string;
  series?: Array<{ name: string; sequence: string }>;
  addedAt?: number;
  source: "kavita";
}

type Tab = "abs" | "collection" | "ebooks" | "downloaded" | "want" | "finished";
type MediaFilter = "all" | "audiobooks" | "ebooks";
type TabView = "all" | "genre" | "series" | "author";

export type NavigateToBook = (
  title: string,
  author?: string,
  target?: { ebookChapterId?: number; ebookSeriesId?: number; absItemId?: string }
) => void;

/** Series label from local item metadata (no Hardcover). */
function localSeriesName(item: {
  seriesName?: string;
  series?: Array<{ name?: string }>;
}): string {
  const sn = (item.seriesName || "").trim();
  if (sn) return sn;
  for (const s of item.series || []) {
    const n = (s?.name || "").trim();
    if (n) return n;
  }
  return "";
}


/** Series index / sequence for Calibre-style "Series Name (1)" labels. */
function localSeriesSequence(item: {
  sequence?: string;
  seriesName?: string;
  series?: Array<{ name?: string; sequence?: string }>;
}): string {
  const direct = String(item.sequence || "").replace(/^#/, "").trim();
  if (direct) return direct;
  const name = localSeriesName(item);
  if (name) {
    for (const s of item.series || []) {
      if ((s?.name || "").trim() === name) {
        const seq = String(s?.sequence || "").replace(/^#/, "").trim();
        if (seq) return seq;
      }
    }
  }
  for (const s of item.series || []) {
    const seq = String(s?.sequence || "").replace(/^#/, "").trim();
    if (seq) return seq;
  }
  return "";
}

/** Group items into multi-book series shelves from local metadata. */
function groupItemsByLocalSeries<T extends { seriesName?: string; series?: Array<{ name?: string; sequence?: string }>; sequence?: string; coverUrl?: string; duration?: number }>(
  items: T[],
  idOf: (item: T) => string | number,
): Array<{
  id: string;
  name: string;
  books: Array<T & { sequence: string; itemId?: string }>;
  bookCount: number;
  totalDuration: number;
  coverUrl: string;
}> {
  const groups = new Map<string, {
    id: string;
    name: string;
    books: Array<T & { sequence: string; itemId?: string }>;
    bookCount: number;
    totalDuration: number;
    coverUrl: string;
    _seen: Set<string | number>;
  }>();
  for (const item of items) {
    const name = localSeriesName(item);
    if (!name) continue;
    const key = name.toLowerCase();
    let bucket = groups.get(key);
    if (!bucket) {
      bucket = {
        id: `local:${key}`,
        name,
        books: [],
        bookCount: 0,
        totalDuration: 0,
        coverUrl: "",
        _seen: new Set(),
      };
      groups.set(key, bucket);
    }
    const iid = idOf(item);
    if (bucket._seen.has(iid)) continue;
    bucket._seen.add(iid);
    const seq = String(item.sequence || item.series?.find((s) => s.name === name)?.sequence || "");
    const book = { ...item, sequence: seq, itemId: String(iid) };
    bucket.books.push(book);
    if (!bucket.coverUrl && item.coverUrl) bucket.coverUrl = item.coverUrl;
  }
  const out: Array<{
    id: string;
    name: string;
    books: Array<T & { sequence: string; itemId?: string }>;
    bookCount: number;
    totalDuration: number;
    coverUrl: string;
  }> = [];
  for (const bucket of groups.values()) {
    const { _seen: _, ...rest } = bucket;
    if (rest.books.length < 2) continue;
    rest.books.sort((a, b) => {
      const fa = parseFloat(a.sequence || "999");
      const fb = parseFloat(b.sequence || "999");
      if (!Number.isNaN(fa) && !Number.isNaN(fb)) return fa - fb;
      return String(a.sequence).localeCompare(String(b.sequence));
    });
    rest.bookCount = rest.books.length;
    rest.totalDuration = Math.round(
      rest.books.reduce((sum, b) => sum + (Number(b.duration) || 0), 0)
    );
    out.push(rest);
  }
  out.sort((a, b) => a.name.localeCompare(b.name));
  return out;
}

export default function MyLibrary() {
  const { user, sessionReady } = useAuth();
  const { toast } = useToast();
  const { playABS, playRD, nowPlaying, expanded } = usePlayer();
  const liftForMini = Boolean(nowPlaying && !expanded);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const online = useOnlineStatus();
  const offline = !online || isLikelyOffline();

  // Restore tab/filters/scroll after Back from abs/ebook details (route unmounts this page).
  const savedUiRef = useRef<LibraryScrollMemory | null>(loadLibraryScrollMemory());
  const savedUi = savedUiRef.current;

  const [tab, setTab] = useState<Tab>(() => savedUi?.tab ?? "abs");
  const [absView, setAbsView] = useState<TabView>(() => savedUi?.absView ?? "all");
  const [ebookView, setEbookView] = useState<TabView>(() => savedUi?.ebookView ?? "all");
  const [collectionView, setCollectionView] = useState<TabView>(() => savedUi?.collectionView ?? "all");
  const [mediaFilter, setMediaFilter] = useState<MediaFilter>(() => savedUi?.mediaFilter ?? "all");
  const [filterGenre, setFilterGenre] = useState(() => savedUi?.filterGenre ?? "");
  const [filterSeries, setFilterSeries] = useState(() => savedUi?.filterSeries ?? "");
  const [filterAuthor, setFilterAuthor] = useState(() => savedUi?.filterAuthor ?? "");
  const [searchQuery, setSearchQuery] = useState(() => savedUi?.searchQuery ?? "");
  const [debouncedQuery, setDebouncedQuery] = useState(() => savedUi?.searchQuery ?? "");
  const [resolvingId, setResolvingId] = useState<number | null>(null);
  const [scanning, setScanning] = useState(false);
  const [cachedAbsIds, setCachedAbsIds] = useState<Set<string>>(new Set());
  const [cachedRdIds, setCachedRdIds] = useState<Set<number>>(new Set());
  const [cachedEbookIds, setCachedEbookIds] = useState<Set<number>>(new Set());
  const [downloadedItems, setDownloadedItems] = useState<
    Array<OfflineManifest & { cached: true }>
  >([]);
  const [continueModal, setContinueModal] = useState<{
    chapterId: number;
    item: KavitaItem;
    progress: NonNullable<ReturnType<typeof getProgress>>;
  } | null>(null);

  const scrollYRef = useRef(savedUi?.scrollY ?? 0);
  const scrollRestoredRef = useRef(false);

  const persistLibraryUi = useCallback(
    (scrollY?: number) => {
      const y = scrollY ?? (typeof window !== "undefined" ? window.scrollY : scrollYRef.current);
      scrollYRef.current = y;
      saveLibraryScrollMemory({
        tab,
        absView,
        ebookView,
        collectionView,
        mediaFilter,
        filterGenre,
        filterSeries,
        filterAuthor,
        searchQuery,
        scrollY: y,
      });
    },
    [
      tab,
      absView,
      ebookView,
      collectionView,
      mediaFilter,
      filterGenre,
      filterSeries,
      filterAuthor,
      searchQuery,
    ]
  );

  const debounceRef = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => {
    debounceRef.current = setTimeout(() => setDebouncedQuery(searchQuery), 300);
    return () => clearTimeout(debounceRef.current);
  }, [searchQuery]);

  // Track document scroll while on My Library (window scrolls on mobile + desktop).
  useEffect(() => {
    const onScroll = () => {
      scrollYRef.current = window.scrollY;
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      window.removeEventListener("scroll", onScroll);
      persistLibraryUi(scrollYRef.current);
    };
  }, [persistLibraryUi]);

  const {
    data: absCollection,
    isLoading: absLoading,
    isFetching: absFetching,
  } = useQuery({
    queryKey: ["abs-collection"],
    queryFn: async ({ client }) => {
      const { data } = await api.get("/library/abs/collection", {
        params: shouldBustLibraryCollectionCache() ? { refresh: true } : undefined,
      });
      const fresh = data as {
        genres: Record<string, ABSItem[]>;
        ungrouped: ABSItem[];
        totalItems: number;
      };
      const prev = client.getQueryData<typeof fresh>(["abs-collection"]);
      // Full snapshot: merge-by-id with prune (add/update/drop deleted).
      const merged = mergeAbsCollection(prev, fresh, { pruneMissing: true });
      if (absCollectionHasOrphans(prev, fresh)) {
        // Persist only — in-memory uses merged return value.
        stripCollectionEntriesFromPersist();
      }
      return merged ?? fresh;
    },
    staleTime: 30 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    refetchOnWindowFocus: false,
    // Replace nested genre arrays when data changes (no structural share of stale buckets).
    structuralSharing: false,
    enabled: !!user && sessionReady,
  });

  const { data: rdLibrary, isLoading: rdLoading, isFetching: rdFetching } = useQuery({
    queryKey: ["streaming-library"],
    queryFn: async ({ client }) => {
      const { data } = await api.get("/library");
      const fresh = data as { items: LibraryItem[] };
      const prev = client.getQueryData<typeof fresh>(["streaming-library"]);
      const merged = mergeStreamingLibrary(prev, fresh, { pruneMissing: true });
      return merged ?? fresh;
    },
    staleTime: 10 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    refetchOnWindowFocus: false,
    structuralSharing: false,
    enabled: !!user && sessionReady,
  });

  const {
    data: kavitaCollection,
    isLoading: kavitaLoading,
    isFetching: kavitaFetching,
    isError: kavitaError,
    refetch: refetchKavita,
  } = useQuery({
    queryKey: ["kavita-collection"],
    queryFn: async ({ client }) => {
      const { data } = await api.get("/library/kavita/collection", {
        params: shouldBustLibraryCollectionCache() ? { refresh: true } : undefined,
      });
      const fresh = data as { items: KavitaItem[]; totalItems: number };
      const prev = client.getQueryData<typeof fresh>(["kavita-collection"]);
      const merged = mergeKavitaCollection(prev, fresh, { pruneMissing: true });
      return merged ?? fresh;
    },
    staleTime: 30 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    refetchOnWindowFocus: false,
    structuralSharing: false,
    enabled: !!user && sessionReady,
  });

  const { data: wantAlertsData, isLoading: wantLoading } = useQuery({
    queryKey: ["availability-alerts"],
    queryFn: async () => {
      const { data } = await api.get("/books/availability-alerts");
      return data as {
        alerts: Array<{
          volumeId: string;
          title: string;
          author: string;
          coverUrl: string;
          createdAt?: string | null;
        }>;
      };
    },
    enabled: !!user && sessionReady && tab === "want",
    staleTime: 60 * 1000,
  });

  const { data: streamHistoryFinished } = useQuery({
    queryKey: ["stream-history-finished"],
    queryFn: async () => {
      const { data } = await api.get("/stream/rd/history");
      return data as {
        items: Array<{
          id: number;
          title: string;
          author: string;
          coverUrl: string;
          progressSeconds: number;
          totalSeconds: number;
          status: string;
        }>;
      };
    },
    enabled: !!user && sessionReady && tab === "finished",
    staleTime: 60 * 1000,
  });

  const removeAlertMutation = useMutation({
    mutationFn: async (volumeId: string) => {
      await api.delete(`/books/availability-alerts/${encodeURIComponent(volumeId)}`);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["availability-alerts"] });
      toast("Removed from Want list", "info");
    },
    onError: () => toast("Failed to remove alert", "error"),
  });

  const libraryTitles = useMemo(() => {
    const titles = new Set<string>();
    if (absCollection) {
      const items = [...Object.values(absCollection.genres).flat(), ...absCollection.ungrouped];
      items.forEach((i) => i.title && titles.add(i.title));
    }
    if (kavitaCollection?.items) {
      kavitaCollection.items.forEach((i) => i.title && titles.add(i.title));
    }
    return Array.from(titles);
  }, [absCollection, kavitaCollection]);

  const { data: formatMatches } = useQuery({
    queryKey: ["format-matches", libraryTitles],
    queryFn: async () => {
      const { data } = await api.post("/library/format-matches", { titles: libraryTitles });
      return data as Record<string, { hasEbook: boolean; hasAudio: boolean }>;
    },
    staleTime: 5 * 60 * 1000,
    enabled: libraryTitles.length > 0,
  });

  const { data: searchResults, isLoading: searchLoading } = useQuery({
    queryKey: ["library-search", debouncedQuery, mediaFilter],
    queryFn: async () => {
      const params = new URLSearchParams({ q: debouncedQuery, media: mediaFilter });
      const { data } = await api.get(`/library/search?${params}`);
      return data as { results: SearchResult[] };
    },
    enabled: !offline && debouncedQuery.length >= 2,
  });

  const refreshCacheFlags = useCallback(async () => {
    const absIds = new Set<string>();
    const rdIds = new Set<number>();
    const ebookIds = new Set<number>();
    const downloaded = await listDownloadedItems();
    for (const m of downloaded) {
      if (m.source === "abs") absIds.add(m.itemId);
      else if (m.source === "rd" && m.libraryItemId != null) rdIds.add(m.libraryItemId);
      else if (m.source === "ebook") ebookIds.add(m.chapterId);
    }
    // Also mark ready by probing known manifests even if listDownloaded missed a key.
    for (const m of downloaded) {
      if (m.source === "abs" && (await isAbsOfflineReady(m.itemId))) absIds.add(m.itemId);
      if (m.source === "rd" && (await isRdOfflineReady(m))) {
        if (m.libraryItemId != null) rdIds.add(m.libraryItemId);
      }
      if (m.source === "ebook" && (await isEbookOfflineReady(m.chapterId))) {
        ebookIds.add(m.chapterId);
      }
    }
    setCachedAbsIds(absIds);
    setCachedRdIds(rdIds);
    setCachedEbookIds(ebookIds);
    setDownloadedItems(downloaded);
  }, []);

  useEffect(() => {
    void refreshCacheFlags();
    const onUpdate = () => void refreshCacheFlags();
    window.addEventListener("audio-cache-updated", onUpdate);
    window.addEventListener("ebook-cache-updated", onUpdate);
    return () => {
      window.removeEventListener("audio-cache-updated", onUpdate);
      window.removeEventListener("ebook-cache-updated", onUpdate);
    };
  }, [refreshCacheFlags]);

  const removeMutation = useMutation({
    mutationFn: (id: number) => api.delete(`/library/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["streaming-library"] });
      toast("Removed from library", "info");
    },
  });

  const handleRefreshLibrary = useCallback(async () => {
    setScanning(true);
    try {
      // Fire-and-forget ABS wait: do not block UI on scan_library_and_wait (~30–240s).
      // Backend still scans + cleans orphans in the background when wait=false.
      const [absResult] = await Promise.allSettled([
        api.post("/library/abs/scan", null, {
          params: { wait: false },
          timeout: 60_000,
        }),
        api.post("/library/kavita/scan", null, { timeout: 60_000 }),
      ]);
      const deferred =
        absResult.status === "fulfilled" &&
        Boolean((absResult.value.data as { deferred?: boolean } | undefined)?.deferred);

      // Keep cached shelf visible; soft-refresh (invalidate + refetch) once immediately.
      await softRefreshLibraryCollectionQueries(queryClient);

      toast(
        deferred
          ? "Library updating — scan running in background; new books will appear shortly"
          : "Library refreshed — ABS + Kavita scanned",
        deferred ? "info" : "success",
      );

      // Background soft-poll while ABS indexes — never purge (stale-while-revalidate).
      void (async () => {
        for (let i = 0; i < 5; i++) {
          await new Promise((r) => setTimeout(r, i < 2 ? 2500 : 5000));
          try {
            await softRefreshLibraryCollectionQueries(queryClient);
          } catch {
            // ignore background poll errors
          }
        }
      })();
    } catch {
      toast("Library scan failed", "error");
    } finally {
      setScanning(false);
    }
  }, [queryClient, toast]);

  // Reset shelf filters when switching media tabs (skip mount so restored filters survive).
  const tabFilterInitRef = useRef(false);
  useEffect(() => {
    if (!tabFilterInitRef.current) {
      tabFilterInitRef.current = true;
      return;
    }
    setFilterGenre("");
    setFilterSeries("");
    setFilterAuthor("");
  }, [tab]);

  // Restore scrollY after shelf content has enough height (React Query often paints fast).
  useLayoutEffect(() => {
    if (scrollRestoredRef.current) return;
    const targetY = savedUiRef.current?.scrollY ?? 0;
    if (targetY <= 0) {
      scrollRestoredRef.current = true;
      return;
    }

    let cancelled = false;
    let attempts = 0;
    let raf = 0;
    const tryRestore = () => {
      if (cancelled || scrollRestoredRef.current) return;
      attempts += 1;
      const maxScroll = Math.max(
        0,
        document.documentElement.scrollHeight - window.innerHeight
      );
      if (maxScroll >= targetY - 8 || attempts >= 48) {
        window.scrollTo(0, Math.min(targetY, maxScroll));
        // Only lock once the page is tall enough (or we gave up).
        if (maxScroll >= targetY - 8 || attempts >= 48) {
          scrollRestoredRef.current = true;
        }
        return;
      }
      raf = requestAnimationFrame(tryRestore);
    };
    raf = requestAnimationFrame(tryRestore);
    const t1 = window.setTimeout(tryRestore, 120);
    const t2 = window.setTimeout(tryRestore, 400);
    return () => {
      cancelled = true;
      cancelAnimationFrame(raf);
      window.clearTimeout(t1);
      window.clearTimeout(t2);
    };
  }, [tab, absCollection, kavitaCollection, searchQuery, debouncedQuery]);

  const handlePlayABS = useCallback(
    async (itemId: string) => {
      if (offline && !(await isAbsOfflineReady(itemId))) {
        toast("Not downloaded — save this book while online to play offline", "info");
        return;
      }
      try {
        await playABS(itemId);
      } catch (err) {
        const msg =
          err instanceof Error && err.message.startsWith("Offline")
            ? err.message
            : "Failed to start playback";
        toast(msg, "error");
      }
    },
    [playABS, toast, offline]
  );

  const removeDownloaded = useCallback(
    async (item: OfflineManifest) => {
      try {
        if (item.source === "abs") await removeAbsOffline(item.itemId);
        else if (item.source === "rd") {
          await removeRdOffline({
            libraryItemId: item.libraryItemId,
            streamHistoryId: item.streamHistoryId,
            tracks: item.tracks,
          });
        } else await removeEbookOffline(item.chapterId);
        toast("Removed from this device", "info");
        void refreshCacheFlags();
      } catch {
        toast("Could not remove download", "error");
      }
    },
    [toast, refreshCacheFlags]
  );

  const handlePlayRD = useCallback(
    async (item: LibraryItem) => {
      if (item.streamStatus !== "ready" || item.tracks.length === 0) return;

      const startOffline = async (): Promise<boolean> => {
        const manifest = getRdOfflineManifest({ libraryItemId: item.id });
        const tracks = manifest?.tracks?.length ? manifest.tracks : item.tracks;
        if (!tracks?.length) return false;
        if (!(await isBookCached(tracks))) return false;
        const local = getOfflineProgress(progressKeyForRd({ libraryItemId: item.id }) || "");
        playRD(
          tracks,
          manifest?.title || item.title,
          manifest?.author || item.author,
          manifest?.coverUrl || item.coverUrl,
          manifest?.streamHistoryId,
          {
            startAt: local?.time || 0,
            trackIndex: local?.trackIndex || 0,
            trackPositionSeconds: local?.trackLocal || 0,
          },
          item.id
        );
        return true;
      };

      if (isLikelyOffline()) {
        if (await startOffline()) return;
        toast("Offline playback unavailable — download this book while online first", "error");
        return;
      }

      try {
        // /play returns a StreamHistory id (the library item id is NOT one) so
        // playback progress actually saves, plus the last saved position.
        const { data } = await api.post(`/library/${item.id}/play`);
        const local = getOfflineProgress(progressKeyForRd({ libraryItemId: item.id }) || "");
        const serverStart = data.progressSeconds || 0;
        const resume =
          local && local.time > serverStart + 5
            ? {
                startAt: local.time,
                trackIndex: local.trackIndex,
                trackPositionSeconds: local.trackLocal,
              }
            : {
                startAt: serverStart,
                trackIndex: data.currentTrackIndex || 0,
                trackPositionSeconds: data.trackPositionSeconds || 0,
              };
        playRD(
          data.tracks?.length > 0 ? data.tracks : item.tracks,
          item.title,
          item.author,
          item.coverUrl,
          data.streamHistoryId ?? undefined,
          resume,
          item.id
        );
      } catch {
        if (await startOffline()) return;
        toast("Could not start playback — check your connection and try again", "error");
      }
    },
    [playRD, toast]
  );

  const handleReadEbook = useCallback(
    async (chapterId: number, item: KavitaItem) => {
      if (offline && !(await isEbookOfflineReady(chapterId))) {
        toast("Not downloaded — save this ebook while online to read offline", "info");
        return;
      }
      const progress = getProgress(chapterId);
      if (progress) {
        setContinueModal({ chapterId, item, progress });
      } else {
        persistLibraryUi();
        navigate(`/read/${chapterId}`);
      }
    },
    [navigate, offline, toast, persistLibraryUi]
  );

  const handleContinueReading = useCallback(
    (chapterId: number) => {
      setContinueModal(null);
      persistLibraryUi();
      navigate(`/read/${chapterId}`);
    },
    [navigate, persistLibraryUi]
  );

  const handleStartFromBeginning = useCallback(
    (chapterId: number) => {
      clearProgress(chapterId);
      setContinueModal(null);
      persistLibraryUi();
      navigate(`/read/${chapterId}`);
    },
    [navigate, persistLibraryUi]
  );

  const handleNavigateToBook = useCallback(
    async (title: string, author?: string, target?: { ebookChapterId?: number; ebookSeriesId?: number; absItemId?: string }) => {
      persistLibraryUi();
      if (target?.ebookSeriesId != null) {
        navigate(`/library/ebook/${target.ebookSeriesId}`);
        return;
      }
      if (target?.ebookChapterId != null) {
        navigate(`/read/${target.ebookChapterId}`);
        return;
      }
      if (target?.absItemId) {
        // Books already in the library get their own detail page (synopsis from
        // ABS) — never dump the user into store search results.
        navigate(`/library/abs/${encodeURIComponent(target.absItemId)}`);
        return;
      }
      try {
        const q = author
          ? `intitle:${JSON.stringify(title)} inauthor:${author}`
          : title;
        const { data } = await api.get(`/books/search?q=${encodeURIComponent(q)}&pageSize=5`);
        const books = (data as { books?: { id: string; title: string }[] })?.books;
        if (books?.length) {
          const titleLower = title.toLowerCase();
          const match = books.find((b) => {
            const bt = b.title.toLowerCase();
            return bt === titleLower || bt.includes(titleLower) || titleLower.includes(bt);
          }) || books[0];
          navigate(`/book/${encodeURIComponent(match.id)}`);
        } else {
          navigate(`/search?q=${encodeURIComponent(title)}`);
        }
      } catch {
        navigate(`/search?q=${encodeURIComponent(title)}`);
      }
    },
    [navigate, persistLibraryUi]
  );

  const handleResolveRD = useCallback(
    async (item: LibraryItem) => {
      if (!item.magnetLink) {
        toast("No magnet link. Go to the book page and stream from there.", "error");
        return;
      }
      setResolvingId(item.id);
      try {
        const { data: startData } = await api.post("/stream/rd/resolve", {
          magnet_link: item.magnetLink,
          title: item.title,
          author: item.author || "",
          cover_url: item.coverUrl || "",
        });
        const taskId = startData.taskId;
        if (!taskId) { toast("Failed to start resolution", "error"); return; }
        let done = false;
        while (!done) {
          await new Promise((r) => setTimeout(r, 2000));
          try {
            const { data: status } = await api.get(`/stream/rd/status/${taskId}`);
            if (status.status === "ready" && status.tracks?.length > 0) {
              // The resolve already stored tracks on the library item server-side
              queryClient.invalidateQueries({ queryKey: ["streaming-library"] });
              playRD(
                status.tracks,
                item.title,
                item.author,
                item.coverUrl,
                status.streamHistoryId ?? undefined,
                {
                  startAt: status.progressSeconds || 0,
                  trackIndex: status.currentTrackIndex || 0,
                  trackPositionSeconds: status.trackPositionSeconds || 0,
                }
              );
              toast(`"${item.title}" is ready!`, "success");
              done = true;
            } else if (status.status === "error") {
              toast(status.error || "Resolution failed", "error");
              done = true;
            }
          } catch {
            toast("Lost connection to resolver", "error");
            done = true;
          }
        }
      } catch (err: any) {
        toast(err.response?.data?.detail || "Failed to resolve stream", "error");
      } finally {
        setResolvingId(null);
      }
    },
    [playRD, toast, queryClient]
  );

  const isSearching = debouncedQuery.length >= 2;

  const allAbsItems = useMemo(() => {
    if (!absCollection) return [] as ABSItem[];
    const items = [...Object.values(absCollection.genres).flat(), ...absCollection.ungrouped];
    const deduped = items.filter((item, idx, arr) => arr.findIndex((i) => i.itemId === item.itemId) === idx);
    return deduped.sort((a, b) => (b.addedAt || 0) - (a.addedAt || 0));
  }, [absCollection]);

  const absFilterOptions = useMemo(() => {
    const genres = new Set<string>();
    const authors = new Set<string>();
    const series = new Set<string>();
    for (const item of allAbsItems) {
      (item.genres || []).forEach((g) => g && genres.add(g));
      if (item.author) authors.add(item.author);
      const sn = localSeriesName(item);
      if (sn) series.add(sn);
    }
    return {
      genres: Array.from(genres).sort(),
      series: Array.from(series).sort((a, b) => a.localeCompare(b)),
      authors: Array.from(authors).sort(),
    };
  }, [allAbsItems]);

  const filteredAbsItems = useMemo(() => {
    const filtered = allAbsItems.filter((item) => {
      if (filterGenre && !(item.genres || []).some((g) => g === filterGenre || g.toLowerCase().includes(filterGenre.toLowerCase()))) {
        return false;
      }
      if (filterSeries && localSeriesName(item) !== filterSeries) return false;
      if (filterAuthor && item.author !== filterAuthor) return false;
      return true;
    });
    // Cached / downloaded first, then uncached (for offline browsing).
    return [...filtered].sort((a, b) => {
      const ac = cachedAbsIds.has(a.itemId) ? 0 : 1;
      const bc = cachedAbsIds.has(b.itemId) ? 0 : 1;
      if (ac !== bc) return ac - bc;
      return (b.addedAt || 0) - (a.addedAt || 0);
    });
  }, [allAbsItems, filterGenre, filterSeries, filterAuthor, cachedAbsIds]);

  const absByGenre = useMemo(() => {
    const groups: Record<string, ABSItem[]> = {};
    for (const item of filteredAbsItems) {
      const gs = item.genres?.length ? item.genres : ["Uncategorized"];
      for (const g of gs) {
        (groups[g] ??= []).push(item);
      }
    }
    return Object.fromEntries(Object.entries(groups).sort(([a], [b]) => a.localeCompare(b)));
  }, [filteredAbsItems]);

  const absByAuthor = useMemo(() => {
    const groups: Record<string, ABSItem[]> = {};
    for (const item of filteredAbsItems) {
      const a = item.author || "Unknown Author";
      (groups[a] ??= []).push(item);
    }
    return Object.fromEntries(Object.entries(groups).sort(([a], [b]) => a.localeCompare(b)));
  }, [filteredAbsItems]);

  const absSeriesLocal = useMemo(
    () => groupItemsByLocalSeries(filteredAbsItems, (i) => i.itemId) as ABSSeries[],
    [filteredAbsItems]
  );

  const finishedAbsItems = useMemo(() => {
    return allAbsItems.filter((item) => item.isFinished || (item.progress || 0) >= 0.95);
  }, [allAbsItems]);

  const finishedRdItems = useMemo(() => {
    const items = streamHistoryFinished?.items || [];
    return items.filter((h) => {
      if (h.status === "finished") return true;
      if (h.totalSeconds > 0 && h.progressSeconds / h.totalSeconds >= 0.95) return true;
      return false;
    });
  }, [streamHistoryFinished]);

  const allEbookItems = useMemo(() => {
    const items = [...(kavitaCollection?.items || [])];
    return items.sort((a, b) => (b.addedAt || 0) - (a.addedAt || 0));
  }, [kavitaCollection]);

  const ebookFilterOptions = useMemo(() => {
    const genres = new Set<string>();
    const authors = new Set<string>();
    const series = new Set<string>();
    for (const item of allEbookItems) {
      (item.genres || []).forEach((g) => g && genres.add(g));
      if (item.author) authors.add(item.author);
      const sn = localSeriesName(item);
      if (sn) series.add(sn);
    }
    return {
      genres: Array.from(genres).sort(),
      series: Array.from(series).sort((a, b) => a.localeCompare(b)),
      authors: Array.from(authors).sort(),
    };
  }, [allEbookItems]);

  const filteredEbookItems = useMemo(() => {
    const filtered = allEbookItems.filter((item) => {
      if (filterGenre && !(item.genres || []).includes(filterGenre)) return false;
      if (filterSeries && localSeriesName(item) !== filterSeries) return false;
      if (filterAuthor && item.author !== filterAuthor) return false;
      return true;
    });
    return [...filtered].sort((a, b) => {
      const ac = a.chapterId != null && cachedEbookIds.has(a.chapterId) ? 0 : 1;
      const bc = b.chapterId != null && cachedEbookIds.has(b.chapterId) ? 0 : 1;
      if (ac !== bc) return ac - bc;
      return (b.addedAt || 0) - (a.addedAt || 0);
    });
  }, [allEbookItems, filterGenre, filterSeries, filterAuthor, cachedEbookIds]);

  const ebookByGenre = useMemo(() => {
    const groups: Record<string, KavitaItem[]> = {};
    for (const item of filteredEbookItems) {
      const gs = item.genres?.length ? item.genres : ["Uncategorized"];
      for (const g of gs) (groups[g] ??= []).push(item);
    }
    return Object.fromEntries(Object.entries(groups).sort(([a], [b]) => a.localeCompare(b)));
  }, [filteredEbookItems]);

  const ebookByAuthor = useMemo(() => {
    const groups: Record<string, KavitaItem[]> = {};
    for (const item of filteredEbookItems) {
      const a = item.author || "Unknown Author";
      (groups[a] ??= []).push(item);
    }
    return Object.fromEntries(Object.entries(groups).sort(([a], [b]) => a.localeCompare(b)));
  }, [filteredEbookItems]);

  const ebookSeriesLocal = useMemo(
    () => groupItemsByLocalSeries(filteredEbookItems, (i) => i.seriesId),
    [filteredEbookItems]
  );

  const collectionItemsSorted = useMemo(() => {
    const items = [...(rdLibrary?.items || [])];
    return items.sort((a, b) => {
      const ta = a.createdAt ? new Date(a.createdAt).getTime() : 0;
      const tb = b.createdAt ? new Date(b.createdAt).getTime() : 0;
      return tb - ta;
    });
  }, [rdLibrary]);

  const collectionFilterOptions = useMemo(() => {
    const genres = new Set<string>();
    const authors = new Set<string>();
    const series = new Set<string>();
    for (const item of collectionItemsSorted) {
      if (item.genre) genres.add(item.genre);
      (item.genres || []).forEach((g) => g && genres.add(g));
      if (item.author) authors.add(item.author);
      const sn = localSeriesName(item);
      if (sn) series.add(sn);
    }
    return {
      genres: Array.from(genres).sort(),
      series: Array.from(series).sort((a, b) => a.localeCompare(b)),
      authors: Array.from(authors).sort(),
    };
  }, [collectionItemsSorted]);

  const filteredCollectionItems = useMemo(() => {
    const filtered = collectionItemsSorted.filter((item) => {
      const itemGenres = item.genres?.length ? item.genres : (item.genre ? [item.genre] : []);
      if (filterGenre && !itemGenres.includes(filterGenre) && (item.genre || "Uncategorized") !== filterGenre) {
        return false;
      }
      if (filterSeries && localSeriesName(item) !== filterSeries) return false;
      if (filterAuthor && item.author !== filterAuthor) return false;
      return true;
    });
    return [...filtered].sort((a, b) => {
      const ac = cachedRdIds.has(a.id) ? 0 : 1;
      const bc = cachedRdIds.has(b.id) ? 0 : 1;
      if (ac !== bc) return ac - bc;
      const ta = a.createdAt ? new Date(a.createdAt).getTime() : 0;
      const tb = b.createdAt ? new Date(b.createdAt).getTime() : 0;
      return tb - ta;
    });
  }, [collectionItemsSorted, filterGenre, filterSeries, filterAuthor, cachedRdIds]);

  const collectionByGenre = useMemo(() => {
    const groups: Record<string, LibraryItem[]> = {};
    for (const item of filteredCollectionItems) {
      const gs = item.genres?.length ? item.genres : [item.genre || "Uncategorized"];
      for (const g of gs) (groups[g] ??= []).push(item);
    }
    return Object.fromEntries(Object.entries(groups).sort(([a], [b]) => a.localeCompare(b)));
  }, [filteredCollectionItems]);

  const collectionByAuthor = useMemo(() => {
    const groups: Record<string, LibraryItem[]> = {};
    for (const item of filteredCollectionItems) {
      const a = item.author || "Unknown Author";
      (groups[a] ??= []).push(item);
    }
    return Object.fromEntries(Object.entries(groups).sort(([a], [b]) => a.localeCompare(b)));
  }, [filteredCollectionItems]);

  const rdSeriesLocal = useMemo(
    () => groupItemsByLocalSeries(filteredCollectionItems, (i) => i.id),
    [filteredCollectionItems]
  );

  const handlePersonalCollectionNavigate = useCallback(
    async (item: LibraryItem) => {
      persistLibraryUi();
      const vid = item.googleVolumeId || "";
      if (vid && !vid.startsWith("rd:")) {
        navigate(`/book/${encodeURIComponent(vid)}`);
        return;
      }
      try {
        const { data } = await api.get(
          `/library/search?q=${encodeURIComponent(item.title)}&media=all`
        );
        const results = (data as { results?: SearchResult[] })?.results || [];
        const abs = results.find((r) => r.source === "abs" && r.itemId);
        if (abs?.itemId) {
          navigate(`/library/abs/${encodeURIComponent(abs.itemId)}`);
          return;
        }
        const kav = results.find((r) => r.source === "kavita" && r.chapterId != null);
        if (kav?.chapterId != null) {
          navigate(`/read/${kav.chapterId}`);
          return;
        }
      } catch {
        /* fall through */
      }
      navigate(`/search?q=${encodeURIComponent(item.title)}`);
    },
    [navigate, persistLibraryUi]
  );

  const FilterBar = ({
    options,
  }: {
    options: { genres: string[]; series: string[]; authors: string[] };
  }) => (
    <div className="flex w-full flex-nowrap items-center gap-1.5 sm:gap-2">
      <CompactFilterSelect
        label="Genre"
        value={filterGenre}
        options={options.genres}
        allLabel="All genres"
        onChange={setFilterGenre}
        className="flex-1 min-w-0"
      />
      <CompactFilterSelect
        label="Series"
        value={filterSeries}
        options={options.series}
        allLabel="All series"
        onChange={setFilterSeries}
        className="flex-1 min-w-0"
      />
      <CompactFilterSelect
        label="Author"
        value={filterAuthor}
        options={options.authors}
        allLabel="All authors"
        onChange={setFilterAuthor}
        className="flex-1 min-w-0"
      />
      {(filterGenre || filterSeries || filterAuthor) && (
        <button
          type="button"
          onClick={() => {
            setFilterGenre("");
            setFilterSeries("");
            setFilterAuthor("");
          }}
          className="px-2 py-1.5 text-xs text-gray-400 hover:text-gray-200 shrink-0"
        >
          Clear
        </button>
      )}
    </div>
  );

  const viewToggle = (view: TabView, setView: (v: TabView) => void) => (
    <div className="flex w-full gap-1 bg-gray-800/30 p-0.5 rounded-md">
      {(["all", "genre", "series", "author"] as const).map((v) => (
        <button
          key={v}
          onClick={() => setView(v)}
          className={`flex-1 min-w-0 px-1.5 sm:px-3 py-1.5 rounded text-[11px] sm:text-xs font-medium transition-colors text-center whitespace-nowrap ${
            view === v ? "bg-gray-700 text-gray-100" : "text-gray-500 hover:text-gray-300"
          }`}
        >
          {v === "all" ? "All" : v === "genre" ? "By Genre" : v === "series" ? "By Series" : "By Author"}
        </button>
      ))}
    </div>
  );

  const renderTabChrome = () =>
    !isSearching ? (
    <>
      <div className="flex flex-nowrap gap-1 bg-gray-800/50 p-1 rounded-lg overflow-x-auto [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden max-w-full">
        <button
          onClick={() => setTab("abs")}
          className={`flex items-center gap-1.5 px-3 sm:px-4 py-2 rounded-md text-sm font-medium transition-colors whitespace-nowrap shrink-0 ${
            tab === "abs" ? "bg-emerald-600 text-white" : "text-gray-400 hover:text-gray-200"
          }`}
        >
          <Headphones size={14} />
          Audiobooks
        </button>
        <button
          onClick={() => setTab("ebooks")}
          className={`flex items-center gap-1.5 px-3 sm:px-4 py-2 rounded-md text-sm font-medium transition-colors whitespace-nowrap shrink-0 ${
            tab === "ebooks" ? "bg-amber-600 text-white" : "text-gray-400 hover:text-gray-200"
          }`}
        >
          <BookOpen size={14} />
          eBooks
        </button>
        <button
          onClick={() => setTab("collection")}
          className={`flex items-center gap-1.5 px-3 sm:px-4 py-2 rounded-md text-sm font-medium transition-colors whitespace-nowrap shrink-0 ${
            tab === "collection" ? "bg-teal-700 text-white" : "text-gray-400 hover:text-gray-200"
          }`}
        >
          <Layers size={14} />
          My Collection
        </button>
        <button
          onClick={() => setTab("downloaded")}
          className={`flex items-center gap-1.5 px-3 sm:px-4 py-2 rounded-md text-sm font-medium transition-colors whitespace-nowrap shrink-0 ${
            tab === "downloaded" ? "bg-brand-600 text-white" : "text-gray-400 hover:text-gray-200"
          }`}
        >
          <Download size={14} />
          Downloads
          {downloadedItems.length > 0 && (
            <span className="text-[10px] opacity-80">({downloadedItems.length})</span>
          )}
        </button>
        <button
          onClick={() => setTab("want")}
          className={`flex items-center gap-1.5 px-3 sm:px-4 py-2 rounded-md text-sm font-medium transition-colors whitespace-nowrap shrink-0 ${
            tab === "want" ? "bg-rose-700 text-white" : "text-gray-400 hover:text-gray-200"
          }`}
        >
          <Heart size={14} />
          Want
        </button>
        <button
          onClick={() => setTab("finished")}
          className={`flex items-center gap-1.5 px-3 sm:px-4 py-2 rounded-md text-sm font-medium transition-colors whitespace-nowrap shrink-0 ${
            tab === "finished" ? "bg-violet-700 text-white" : "text-gray-400 hover:text-gray-200"
          }`}
        >
          <CheckCircle2 size={14} />
          Finished
        </button>
      </div>

      {tab === "abs" && viewToggle(absView, setAbsView)}
      {tab === "ebooks" && viewToggle(ebookView, setEbookView)}
      {tab === "collection" && viewToggle(collectionView, setCollectionView)}

      {tab === "abs" && <FilterBar options={absFilterOptions} />}
      {tab === "ebooks" && <FilterBar options={ebookFilterOptions} />}
      {tab === "collection" && <FilterBar options={collectionFilterOptions} />}
    </>
  ) : null;

  return (
    <div
      className={`max-w-7xl mx-auto px-4 lg:px-6 pt-8 ${
        liftForMini
          ? "pb-[calc(4.5rem+env(safe-area-inset-bottom,0px))]"
          : "pb-[calc(5rem+env(safe-area-inset-bottom,0px))]"
      } lg:pb-8`}
    >
      <Modal
        title="Continue reading?"
        show={!!continueModal}
        onClose={() => setContinueModal(null)}
      >
        {continueModal && (
          <div className="space-y-4">
            <div className="flex gap-3">
              {continueModal.item.coverUrl ? (
                <CoverImage
                  src={continueModal.item.coverUrl}
                  alt=""
                  className="w-16 h-24 rounded object-cover shrink-0"
                />
              ) : (
                <div className="w-16 h-24 rounded bg-gray-700 shrink-0 flex items-center justify-center">
                  <BookOpen size={24} className="text-gray-500" />
                </div>
              )}
              <div className="flex-1 min-w-0">
                <p className="font-medium text-gray-100">{continueModal.item.title}</p>
                {continueModal.progress.bookTitle && (
                  <p className="text-sm text-gray-500 mt-0.5">{continueModal.progress.bookTitle}</p>
                )}
                <p className="text-xs text-amber-400 mt-2">
                  Page {continueModal.progress.viewportPage + 1} of {continueModal.progress.totalViewportPages ?? "?"}
                  {continueModal.progress.totalKavitaPages && continueModal.progress.totalKavitaPages > 1 && (
                    <span className="text-gray-500"> · Ch. {continueModal.progress.page + 1}/{continueModal.progress.totalKavitaPages}</span>
                  )}
                </p>
              </div>
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => handleContinueReading(continueModal.chapterId)}
                className="flex-1 px-4 py-2 bg-amber-600 text-white rounded-lg font-medium hover:bg-amber-500 transition-colors"
              >
                Continue
              </button>
              <button
                onClick={() => handleStartFromBeginning(continueModal.chapterId)}
                className="flex-1 px-4 py-2 bg-gray-700 text-gray-200 rounded-lg font-medium hover:bg-gray-600 transition-colors"
              >
                Start from beginning
              </button>
            </div>
          </div>
        )}
      </Modal>

      {/* Header */}
      <div className="flex items-center justify-between mb-3 lg:mb-6">
        <div className="flex items-center gap-3">
          <Library className="text-brand-400" size={28} />
          <div>
            <h1 className="text-2xl font-bold text-gray-100">My Library</h1>
            <p className="text-sm text-gray-400">
              {absLoading && !absCollection
                ? "Loading library…"
                : [
                    absCollection ? `${absCollection.totalItems} audiobooks` : "",
                    kavitaCollection?.totalItems ? `${kavitaCollection.totalItems} ebooks` : "",
                    rdLibrary?.items?.length ? `${rdLibrary.items.length} in collection` : "",
                  ]
                    .filter(Boolean)
                    .join(" · ")}
              {(absFetching || kavitaFetching || rdFetching) &&
                (absCollection || kavitaCollection || rdLibrary) && (
                  <span className="ml-2 inline-flex items-center gap-1 text-gray-500">
                    <Loader2 size={12} className="animate-spin" />
                    Updating
                  </span>
                )}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleRefreshLibrary}
            disabled={scanning || offline}
            className="inline-flex items-center justify-center p-2.5 bg-gray-700 text-gray-200 rounded-lg hover:bg-gray-600 transition-colors disabled:opacity-50"
            title={
              offline
                ? "Unavailable offline"
                : scanning
                  ? "Scanning library…"
                  : "Rescan library and remove stale entries"
            }
            aria-label={scanning ? "Scanning library" : "Refresh library"}
          >
            <RefreshCw size={16} className={scanning ? "animate-spin" : ""} />
          </button>
          <button
            type="button"
            onClick={() => navigate("/")}
            disabled={offline}
            className="inline-flex items-center justify-center p-2.5 bg-brand-600 text-white rounded-lg hover:bg-brand-500 transition-colors disabled:opacity-50"
            title={offline ? "Unavailable offline" : "Browse"}
            aria-label="Browse"
          >
            <Compass size={16} />
          </button>
          <button
            type="button"
            disabled={offline}
            onClick={() => navigate("/history")}
            className="inline-flex items-center justify-center p-2.5 bg-gray-800 text-gray-300 rounded-lg hover:bg-gray-700 transition-colors disabled:opacity-50"
            title="Listening history"
            aria-label="Listening history"
          >
            <ListMusic size={16} />
          </button>
        </div>
      </div>

      {offline && (
        <div className="mb-4 rounded-lg border border-amber-800/50 bg-amber-950/40 px-3 py-2 text-xs text-amber-200">
          Showing your last-synced catalog. Downloaded titles are listed first; others are greyed out until you reconnect.
        </div>
      )}

      {/* Mobile: bottom floating pill. Desktop: sticky search + tabs under nav. */}
      <div
        className={`lg:hidden z-40 fixed left-0 right-0 px-4 pointer-events-none ${
          liftForMini
            ? "bottom-[calc(5rem+0.75rem+env(safe-area-inset-bottom,0px))]"
            : "bottom-[calc(0.75rem+env(safe-area-inset-bottom,0px))]"
        }`}
      >
        <div className="pointer-events-auto relative max-w-xl mx-auto">
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={offline ? "Search unavailable offline" : "Search your library..."}
            disabled={offline}
            className="w-full pl-5 pr-14 py-3 bg-gray-900/90 backdrop-blur-md border border-gray-700/70 rounded-full text-sm text-gray-100 shadow-lg shadow-black/40 focus:outline-none focus:ring-2 focus:ring-brand-500/80 focus:border-brand-500/50 placeholder:text-gray-500 disabled:opacity-50"
            aria-label="Search your library"
          />
          {searchQuery ? (
            <button
              type="button"
              onClick={() => setSearchQuery("")}
              className="absolute right-1.5 top-1/2 -translate-y-1/2 p-2.5 rounded-full text-gray-400 hover:text-gray-100 hover:bg-gray-800/80 transition-colors"
              aria-label="Clear library search"
            >
              <X size={18} />
            </button>
          ) : (
            <span
              className="absolute right-1.5 top-1/2 -translate-y-1/2 p-2.5 rounded-full bg-brand-600/90 text-white pointer-events-none shadow-md shadow-brand-900/30"
              aria-hidden
            >
              <Search size={18} strokeWidth={2.25} />
            </span>
          )}
        </div>
      </div>

      <div
        className="hidden lg:block z-40 -mx-4 lg:-mx-6 bg-gray-950/95 backdrop-blur-sm border-b border-gray-800/80 pt-1 pb-3 mb-4 space-y-3 sticky top-[calc(3.5rem+env(safe-area-inset-top,0px))] pl-[max(1.5rem,env(safe-area-inset-left,0px))] pr-[max(1.5rem,env(safe-area-inset-right,0px))]"
      >
        <div className="relative">
          <Search size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={offline ? "Search unavailable offline" : "Search your library..."}
            disabled={offline}
            className="w-full pl-10 pr-10 py-2.5 bg-gray-800 border border-gray-700 rounded-xl text-sm text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent placeholder:text-gray-500 disabled:opacity-50"
            aria-label="Search your library"
          />
          {searchQuery && (
            <button
              type="button"
              onClick={() => setSearchQuery("")}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
              aria-label="Clear library search"
            >
              <X size={16} />
            </button>
          )}
        </div>
        <div className="space-y-3">{renderTabChrome()}</div>
      </div>

      {!isSearching && <div className="lg:hidden mb-4 space-y-3">{renderTabChrome()}</div>}

      {/* Search Results */}
      {isSearching ? (
        <div>
          <div className="flex items-center gap-3 mb-3 flex-wrap">
            <h2 className="text-sm font-medium text-gray-400">
              {searchLoading ? "Searching..." : `Results for "${debouncedQuery}"`}
            </h2>
            <div className="flex gap-1 bg-gray-800/30 p-0.5 rounded-md">
              {(["all", "audiobooks", "ebooks"] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setMediaFilter(m)}
                  className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
                    mediaFilter === m ? "bg-gray-700 text-gray-100" : "text-gray-500 hover:text-gray-300"
                  }`}
                >
                  {m === "all" ? "All" : m === "audiobooks" ? "Audiobooks" : "eBooks"}
                </button>
              ))}
            </div>
          </div>
          {searchResults?.results && searchResults.results.length > 0 ? (
            <div className="space-y-1">
              {searchResults.results.map((r, i) => (
                <button
                  key={`${r.source}-${r.itemId || r.libraryItemId || r.seriesId || i}`}
                  onClick={() => {
                    if (r.source === "rd" && r.googleVolumeId) {
                      persistLibraryUi();
                      navigate(`/book/${encodeURIComponent(r.googleVolumeId)}`);
                    } else if (r.source === "abs") {
                      handleNavigateToBook(r.title, r.author, { absItemId: r.itemId });
                    } else if (r.source === "kavita") {
                      handleNavigateToBook(r.title, r.author, {
                        ebookSeriesId: r.seriesId ?? undefined,
                        ebookChapterId: r.seriesId == null ? r.chapterId ?? undefined : undefined,
                      });
                    }
                  }}
                  className="w-full flex items-center gap-3 p-2.5 rounded-lg hover:bg-gray-800/60 transition-colors text-left group"
                >
                  {r.coverUrl ? (
                    r.source === "kavita" ? (
                      <AuthImage
                        src={r.coverUrl}
                        alt=""
                        className="w-10 h-14 rounded object-cover shrink-0"
                        fallback={
                          <div className="w-10 h-14 rounded bg-gray-700 shrink-0 flex items-center justify-center">
                            <BookOpen size={14} className="text-gray-500" />
                          </div>
                        }
                      />
                    ) : (
                      <CoverImage src={r.coverUrl} alt="" className="w-10 h-14 rounded object-cover shrink-0" />
                    )
                  ) : (
                    <div className="w-10 h-14 rounded bg-gray-700 shrink-0 flex items-center justify-center">
                      <BookOpen size={14} className="text-gray-500" />
                    </div>
                  )}
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-gray-100 truncate">{r.title}</p>
                    {r.author && <p className="text-xs text-gray-400 truncate">{r.author}</p>}
                  </div>
                  <span className={`px-2 py-0.5 text-[10px] font-semibold rounded-full shrink-0 ${
                    r.source === "abs"
                      ? "bg-emerald-900/40 text-emerald-400"
                      : r.source === "kavita"
                        ? "bg-amber-900/40 text-amber-400"
                        : "bg-teal-900/40 text-teal-400"
                  }`}>
                    {r.source === "abs" ? "ABS" : r.source === "kavita" ? "eBook" : "Collection"}
                  </span>
                  {r.source === "abs" ? (
                    <Headphones size={16} className="text-gray-600 group-hover:text-emerald-400 transition-colors shrink-0" />
                  ) : r.source === "kavita" ? (
                    <BookOpen size={16} className="text-gray-600 group-hover:text-amber-400 transition-colors shrink-0" />
                  ) : (
                    <Layers size={16} className="text-gray-600 group-hover:text-brand-400 transition-colors shrink-0" />
                  )}
                </button>
              ))}
            </div>
          ) : !searchLoading ? (
            <p className="text-sm text-gray-500 text-center py-12">No results found</p>
          ) : null}
        </div>
      ) : (
        <>
          <ContinueShelves />

          {/* ABS Tab */}
          {tab === "abs" && (
            <div>
              {absView === "all" && (
                <div>
                  {absLoading && !absCollection ? (
                    <LibraryGridSkeleton />
                  ) : filteredAbsItems.length > 0 ? (
                    <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-7 lg:grid-cols-9 xl:grid-cols-11 gap-2">
                      {filteredAbsItems.map((item) => (
                        <ABSBookCard
                          key={item.itemId}
                          itemId={item.itemId}
                          title={item.title}
                          author={item.author}
                          coverUrl={item.coverUrl}
                          duration={item.duration}
                          progress={item.progress}
                          onNavigate={handleNavigateToBook}
                          hasEbook={formatMatches?.[item.title]?.hasEbook}
                          cached={cachedAbsIds.has(item.itemId)}
                          unavailable={offline && !cachedAbsIds.has(item.itemId)}
                          seriesName={localSeriesName(item)}
                          sequence={localSeriesSequence(item)}
                        />
                      ))}
                    </div>
                  ) : (
                    <EmptyABS onBrowse={() => navigate("/")} onDownloads={() => navigate("/downloads")} />
                  )}
                </div>
              )}

              {absView === "genre" && (
                <div className="space-y-6">
                  {absLoading && !absCollection ? (
                    <LibraryGridSkeleton />
                  ) : (
                    <>
                      {Object.entries(absByGenre).map(([genre, items]) => (
                        <ABSGenreRow
                          key={genre}
                          genre={genre}
                          items={items}
                          onNavigate={handleNavigateToBook}
                          formatMatches={formatMatches}
                          cachedIds={cachedAbsIds}
                          offline={offline}
                        />
                      ))}
                      {Object.keys(absByGenre).length === 0 && (
                        <EmptyABS onBrowse={() => navigate("/")} onDownloads={() => navigate("/downloads")} />
                      )}
                    </>
                  )}
                </div>
              )}

              {absView === "author" && (
                <div className="space-y-6">
                  {absLoading && !absCollection ? (
                    <LibraryGridSkeleton />
                  ) : (
                    Object.entries(absByAuthor).map(([author, items]) => (
                      <ABSGenreRow
                        key={author}
                        genre={author}
                        items={items}
                        onNavigate={handleNavigateToBook}
                        formatMatches={formatMatches}
                        cachedIds={cachedAbsIds}
                        offline={offline}
                      />
                    ))
                  )}
                </div>
              )}

              {absView === "series" && (
                absLoading && !absCollection ? (
                  <LibraryGridSkeleton />
                ) : absSeriesLocal.length > 0 ? (
                  <SeriesDrilldown
                    series={absSeriesLocal}
                    onOpen={(itemId, title, author) =>
                      handleNavigateToBook(title, author, { absItemId: itemId })
                    }
                    cachedIds={cachedAbsIds}
                    offline={offline}
                  />
                ) : (
                  <p className="text-sm text-gray-500 text-center py-12">
                    No multi-book series found in your audiobook library yet.
                  </p>
                )
              )}
            </div>
          )}

          {/* Ebooks Tab */}
          {tab === "ebooks" && (
            <div>
              {kavitaLoading && !kavitaCollection && <LibraryGridSkeleton />}
              {kavitaError && !kavitaCollection && (
                <div className="text-center py-16">
                  <p className="text-red-400 mb-4">Failed to load ebooks. Check Kavita connection.</p>
                  <button onClick={() => refetchKavita()} className="px-4 py-2 bg-gray-700 text-gray-200 rounded-lg hover:bg-gray-600">
                    Retry
                  </button>
                </div>
              )}
              {!kavitaError && allEbookItems.length > 0 && (
                <>
                  {ebookView === "all" && (
                    <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-7 lg:grid-cols-9 xl:grid-cols-11 gap-2">
                      {filteredEbookItems.map((item) => (
                        <EbookCard
                          key={item.seriesId}
                          item={item}
                          onNavigateToBook={handleNavigateToBook}
                          hasAudio={formatMatches?.[item.title]?.hasAudio}
                          cached={item.chapterId != null && cachedEbookIds.has(item.chapterId)}
                          unavailable={
                            offline &&
                            (item.chapterId == null || !cachedEbookIds.has(item.chapterId))
                          }
                        />
                      ))}
                    </div>
                  )}
                  {ebookView === "genre" && (
                    <div className="space-y-6">
                      {Object.entries(ebookByGenre).map(([genre, items]) => (
                        <EbookGenreRow
                          key={genre}
                          genre={genre}
                          items={items}
                          onNavigateToBook={handleNavigateToBook}
                          formatMatches={formatMatches}
                          cachedIds={cachedEbookIds}
                          offline={offline}
                        />
                      ))}
                    </div>
                  )}
                  {ebookView === "series" && (
                    ebookSeriesLocal.length > 0 ? (
                      <div className="space-y-6">
                        {ebookSeriesLocal.map((s) => (
                          <EbookGenreRow
                            key={s.id || s.name}
                            genre={s.name}
                            items={s.books as KavitaItem[]}
                            onNavigateToBook={handleNavigateToBook}
                            formatMatches={formatMatches}
                            cachedIds={cachedEbookIds}
                            offline={offline}
                          />
                        ))}
                      </div>
                    ) : (
                      <p className="text-sm text-gray-500 text-center py-12">
                        No multi-book series found in your ebook library yet.
                      </p>
                    )
                  )}
                  {ebookView === "author" && (
                    <div className="space-y-6">
                      {Object.entries(ebookByAuthor).map(([author, items]) => (
                        <EbookGenreRow
                          key={author}
                          genre={author}
                          items={items}
                          onNavigateToBook={handleNavigateToBook}
                          formatMatches={formatMatches}
                          cachedIds={cachedEbookIds}
                          offline={offline}
                        />
                      ))}
                    </div>
                  )}
                </>
              )}
              {!kavitaLoading && !kavitaError && allEbookItems.length === 0 && (
                <div className="text-center py-16">
                  <BookOpen className="mx-auto mb-4 text-gray-600" size={40} />
                  <h3 className="text-base font-semibold text-gray-300 mb-2">No ebooks on your server</h3>
                  <p className="text-sm text-gray-500 mb-4">Add EPUB or PDF files to your Kavita library, then hit Refresh</p>
                </div>
              )}
            </div>
          )}

          {/* My Collection Tab (personal / streaming collection) */}
          {tab === "collection" && (
            <div>
              {rdLoading && !rdLibrary && <LibraryGridSkeleton />}
              {!rdLoading && collectionItemsSorted.length === 0 && (
                <div className="text-center py-16">
                  <Layers className="mx-auto mb-4 text-gray-600" size={40} />
                  <h3 className="text-base font-semibold text-gray-300 mb-2">No items yet</h3>
                  <p className="text-sm text-gray-500 mb-4">
                    Books you add from Browse or your library appear here — keep this short list for quick access
                  </p>
                  <button onClick={() => navigate("/")} className="px-5 py-2 bg-brand-600 text-white rounded-lg text-sm font-medium hover:bg-brand-500 transition-colors">
                    Browse
                  </button>
                </div>
              )}
              {collectionView === "all" && filteredCollectionItems.length > 0 && (
                <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 xl:grid-cols-10 gap-2">
                  {filteredCollectionItems.map((item) => (
                    <RDCard
                      key={item.id}
                      item={item}
                      isResolving={resolvingId === item.id}
                      onPlay={() => handlePlayRD(item)}
                      onResolve={() => handleResolveRD(item)}
                      onRemove={() => removeMutation.mutate(item.id)}
                      onNavigate={() => handlePersonalCollectionNavigate(item)}
                      unavailable={offline && !cachedRdIds.has(item.id)}
                      cached={cachedRdIds.has(item.id)}
                    />
                  ))}
                </div>
              )}
              {collectionView === "genre" && Object.entries(collectionByGenre).map(([genre, items]) => (
                <div key={genre} className="mb-6">
                  <h3 className="text-sm font-semibold text-gray-300 mb-3">{genre}</h3>
                  <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 xl:grid-cols-10 gap-2">
                    {items.map((item) => (
                      <RDCard
                        key={item.id}
                        item={item}
                        isResolving={resolvingId === item.id}
                        onPlay={() => handlePlayRD(item)}
                        onResolve={() => handleResolveRD(item)}
                        onRemove={() => removeMutation.mutate(item.id)}
                        onNavigate={() => handlePersonalCollectionNavigate(item)}
                        unavailable={offline && !cachedRdIds.has(item.id)}
                        cached={cachedRdIds.has(item.id)}
                      />
                    ))}
                  </div>
                </div>
              ))}
              {collectionView === "series" && (
                rdSeriesLocal.length > 0 ? (
                  rdSeriesLocal.map((s) => (
                    <div key={s.id || s.name} className="mb-6">
                      <h3 className="text-sm font-semibold text-gray-300 mb-3">
                        {s.name}
                        <span className="text-gray-500 font-normal ml-2">{s.bookCount}</span>
                      </h3>
                      <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 xl:grid-cols-10 gap-2">
                        {s.books.map((item) => (
                          <RDCard
                            key={item.id}
                            item={item as LibraryItem}
                            isResolving={resolvingId === item.id}
                            onPlay={() => handlePlayRD(item as LibraryItem)}
                            onResolve={() => handleResolveRD(item as LibraryItem)}
                            onRemove={() => removeMutation.mutate(item.id)}
                            onNavigate={() => handlePersonalCollectionNavigate(item as LibraryItem)}
                            unavailable={offline && !cachedRdIds.has(item.id)}
                            cached={cachedRdIds.has(item.id)}
                          />
                        ))}
                      </div>
                    </div>
                  ))
                ) : (
                  <p className="text-sm text-gray-500 text-center py-12">
                    No multi-book series found in your personal collection yet.
                  </p>
                )
              )}
              {collectionView === "author" && Object.entries(collectionByAuthor).map(([author, items]) => (
                <div key={author} className="mb-6">
                  <h3 className="text-sm font-semibold text-gray-300 mb-3">{author}</h3>
                  <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 xl:grid-cols-10 gap-2">
                    {items.map((item) => (
                      <RDCard
                        key={item.id}
                        item={item}
                        isResolving={resolvingId === item.id}
                        onPlay={() => handlePlayRD(item)}
                        onResolve={() => handleResolveRD(item)}
                        onRemove={() => removeMutation.mutate(item.id)}
                        onNavigate={() => handlePersonalCollectionNavigate(item)}
                        unavailable={offline && !cachedRdIds.has(item.id)}
                        cached={cachedRdIds.has(item.id)}
                      />
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Downloaded tab — local cache only */}
          {tab === "downloaded" && (
            <div>
              {downloadedItems.length === 0 ? (
                <div className="text-center py-16">
                  <Download className="mx-auto mb-4 text-gray-600" size={40} />
                  <h3 className="text-base font-semibold text-gray-300 mb-2">Nothing downloaded yet</h3>
                  <p className="text-sm text-gray-500 mb-4 max-w-md mx-auto">
                    Open a book and tap Save offline, or listen/read while online — files stay on this device for offline play.
                  </p>
                </div>
              ) : (
                <div className="space-y-2">
                  {downloadedItems.map((item) => {
                    const key =
                      item.source === "abs"
                        ? `abs:${item.itemId}`
                        : item.source === "ebook"
                          ? `ebook:${item.chapterId}`
                          : `rd:${item.libraryItemId ?? item.streamHistoryId}`;
                    return (
                      <div
                        key={key}
                        className="flex items-center gap-3 p-2.5 rounded-lg bg-gray-800/40 border border-gray-800"
                      >
                        {item.coverUrl ? (
                          <CoverImage
                            src={item.coverUrl}
                            alt=""
                            className="w-12 h-[4.5rem] rounded object-cover shrink-0"
                          />
                        ) : (
                          <div className="w-12 h-[4.5rem] rounded bg-gray-700 shrink-0 flex items-center justify-center">
                            {item.source === "ebook" ? (
                              <BookOpen size={16} className="text-gray-500" />
                            ) : (
                              <Headphones size={16} className="text-gray-500" />
                            )}
                          </div>
                        )}
                        <div className="flex-1 min-w-0">
                          <p className="text-sm text-gray-100 truncate">{item.title}</p>
                          {item.author && (
                            <p className="text-xs text-gray-400 truncate">{item.author}</p>
                          )}
                          <p className="text-[10px] text-gray-500 mt-0.5">
                            {item.source === "abs"
                              ? "Audiobook"
                              : item.source === "ebook"
                                ? "eBook"
                                : "My Collection"}
                          </p>
                        </div>
                        <div className="flex items-center gap-1.5 shrink-0">
                          {item.source === "ebook" ? (
                            <button
                              type="button"
                              onClick={() => {
                                persistLibraryUi();
                                navigate(`/read/${item.chapterId}`);
                              }}
                              className="px-2.5 py-1.5 text-xs font-medium rounded-md bg-amber-600 text-white hover:bg-amber-500"
                            >
                              Read
                            </button>
                          ) : (
                            <button
                              type="button"
                              onClick={() => {
                                if (item.source === "abs") void handlePlayABS(item.itemId);
                                else {
                                  const rdItem = rdLibrary?.items?.find(
                                    (i) => i.id === item.libraryItemId
                                  );
                                  if (rdItem) void handlePlayRD(rdItem);
                                  else {
                                    playRD(
                                      item.tracks,
                                      item.title,
                                      item.author,
                                      item.coverUrl,
                                      item.streamHistoryId,
                                      0,
                                      item.libraryItemId
                                    );
                                  }
                                }
                              }}
                              className="px-2.5 py-1.5 text-xs font-medium rounded-md bg-emerald-600 text-white hover:bg-emerald-500 inline-flex items-center gap-1"
                            >
                              <Play size={12} /> Play
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={() => void removeDownloaded(item)}
                            className="p-1.5 rounded-md text-gray-400 hover:text-red-300 hover:bg-gray-800"
                            title="Remove from this device"
                          >
                            <Trash2 size={14} />
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {tab === "want" && (
            <div>
              {wantLoading && (
                <p className="text-sm text-gray-500 flex items-center gap-2 py-8 justify-center">
                  <Loader2 size={16} className="animate-spin" /> Loading Want list…
                </p>
              )}
              {!wantLoading && (wantAlertsData?.alerts?.length ?? 0) === 0 && (
                <div className="text-center py-16">
                  <Heart className="mx-auto mb-4 text-gray-600" size={40} />
                  <h3 className="text-base font-semibold text-gray-300 mb-2">Want list is empty</h3>
                  <p className="text-sm text-gray-500 mb-4 max-w-md mx-auto">
                    On a Browse book page, tap Notify me when available to watch for downloads.
                  </p>
                  <button
                    type="button"
                    onClick={() => navigate("/")}
                    className="text-sm text-brand-400 hover:text-brand-300"
                  >
                    Browse catalog
                  </button>
                </div>
              )}
              {!wantLoading && (wantAlertsData?.alerts?.length ?? 0) > 0 && (
                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-3">
                  {wantAlertsData!.alerts.map((alert) => (
                    <div
                      key={alert.volumeId}
                      className="rounded-lg border border-gray-800 bg-gray-900/50 overflow-hidden flex flex-col"
                    >
                      <button
                        type="button"
                        onClick={() => {
                          persistLibraryUi();
                          navigate(`/book/${encodeURIComponent(alert.volumeId)}`);
                        }}
                        className="text-left flex-1"
                      >
                        {alert.coverUrl ? (
                          <CoverImage
                            src={alert.coverUrl}
                            alt=""
                            className="w-full aspect-[2/3] object-cover"
                          />
                        ) : (
                          <div className="w-full aspect-[2/3] bg-gray-800 flex items-center justify-center">
                            <BookOpen size={24} className="text-gray-600" />
                          </div>
                        )}
                        <div className="p-2">
                          <p className="text-xs font-medium text-gray-100 line-clamp-2">{alert.title}</p>
                          {alert.author && (
                            <p className="text-[10px] text-gray-500 truncate mt-0.5">{alert.author}</p>
                          )}
                        </div>
                      </button>
                      <button
                        type="button"
                        onClick={() => removeAlertMutation.mutate(alert.volumeId)}
                        disabled={removeAlertMutation.isPending}
                        className="mx-2 mb-2 px-2 py-1 text-[11px] rounded-md text-gray-400 hover:text-red-300 hover:bg-gray-800 transition-colors"
                      >
                        Remove
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {tab === "finished" && (
            <div>
              {finishedAbsItems.length === 0 && finishedRdItems.length === 0 ? (
                <div className="text-center py-16">
                  <CheckCircle2 className="mx-auto mb-4 text-gray-600" size={40} />
                  <h3 className="text-base font-semibold text-gray-300 mb-2">No finished books yet</h3>
                  <p className="text-sm text-gray-500 max-w-md mx-auto">
                    Titles you finish (≈95%+ or marked finished) show up here.
                  </p>
                </div>
              ) : (
                <div className="space-y-8">
                  {finishedAbsItems.length > 0 && (
                    <div>
                      <h3 className="text-sm font-semibold text-gray-300 mb-3 flex items-center gap-2">
                        <Headphones size={14} className="text-emerald-400" />
                        Library ({finishedAbsItems.length})
                      </h3>
                      <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 xl:grid-cols-8 gap-2">
                        {finishedAbsItems.map((item) => (
                          <ABSBookCard
                            key={item.itemId}
                            itemId={item.itemId}
                            title={item.title}
                            author={item.author}
                            coverUrl={item.coverUrl}
                            duration={item.duration}
                            progress={item.progress}
                            onNavigate={() => {
                              persistLibraryUi();
                              navigate(`/library/book/${encodeURIComponent(item.itemId)}`);
                            }}
                            seriesName={localSeriesName(item)}
                            sequence={item.sequence}
                          />
                        ))}
                      </div>
                    </div>
                  )}
                  {finishedRdItems.length > 0 && (
                    <div>
                      <h3 className="text-sm font-semibold text-gray-300 mb-3 flex items-center gap-2">
                        <ListMusic size={14} className="text-teal-400" />
                        Streams ({finishedRdItems.length})
                      </h3>
                      <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 xl:grid-cols-8 gap-2">
                        {finishedRdItems.map((item) => (
                          <button
                            key={item.id}
                            type="button"
                            onClick={() => navigate("/")}
                            className="text-left group"
                          >
                            <div className="aspect-[2/3] rounded-lg overflow-hidden bg-gray-800 mb-1.5">
                              {item.coverUrl ? (
                                <CoverImage
                                  src={item.coverUrl}
                                  alt=""
                                  className="w-full h-full object-cover group-hover:scale-105 transition-transform"
                                />
                              ) : (
                                <div className="w-full h-full flex items-center justify-center text-gray-600">
                                  <Headphones size={20} />
                                </div>
                              )}
                            </div>
                            <p className="text-[11px] text-gray-200 line-clamp-2 leading-snug">{item.title}</p>
                            {item.author && (
                              <p className="text-[10px] text-gray-500 truncate">{item.author}</p>
                            )}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function EbookGenreRow({
  genre,
  items,
  onNavigateToBook,
  formatMatches,
  cachedIds,
  offline,
}: {
  genre: string;
  items: KavitaItem[];
  onNavigateToBook: NavigateToBook;
  formatMatches?: Record<string, { hasEbook: boolean; hasAudio: boolean }>;
  cachedIds?: Set<number>;
  offline?: boolean;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const scroll = (dir: "left" | "right") => {
    if (!scrollRef.current) return;
    const amount = scrollRef.current.clientWidth * 0.75;
    scrollRef.current.scrollBy({ left: dir === "left" ? -amount : amount, behavior: "smooth" });
  };
  return (
    <section>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold text-gray-200">{genre} <span className="text-xs text-gray-500 font-normal ml-1">({items.length})</span></h3>
        <div className="flex gap-1">
          <button onClick={() => scroll("left")} className="p-1 rounded text-gray-500 hover:bg-gray-800 hover:text-gray-300 transition-colors">
            <ChevronLeft size={14} />
          </button>
          <button onClick={() => scroll("right")} className="p-1 rounded text-gray-500 hover:bg-gray-800 hover:text-gray-300 transition-colors">
            <ChevronRight size={14} />
          </button>
        </div>
      </div>
      <div ref={scrollRef} className="grid grid-flow-col auto-cols-[20%] sm:auto-cols-[14%] md:auto-cols-[10%] lg:auto-cols-[8%] xl:auto-cols-[6.5%] gap-2 overflow-x-auto pb-2 scroll-smooth scrollbar-hide">
        {items.map((item) => {
          const cached = item.chapterId != null && !!cachedIds?.has(item.chapterId);
          return (
            <EbookCard
              key={item.seriesId}
              item={item}
              onNavigateToBook={onNavigateToBook}
              hasAudio={formatMatches?.[item.title]?.hasAudio}
              cached={cached}
              unavailable={offline && (item.chapterId == null || !cached)}
            />
          );
        })}
      </div>
    </section>
  );
}

function ABSGenreRow({
  genre,
  items,
  onNavigate,
  formatMatches,
  cachedIds,
  offline,
}: {
  genre: string;
  items: ABSItem[];
  onNavigate?: NavigateToBook;
  formatMatches?: Record<string, { hasEbook: boolean; hasAudio: boolean }>;
  cachedIds?: Set<string>;
  offline?: boolean;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const scroll = (dir: "left" | "right") => {
    if (!scrollRef.current) return;
    const amount = scrollRef.current.clientWidth * 0.75;
    scrollRef.current.scrollBy({ left: dir === "left" ? -amount : amount, behavior: "smooth" });
  };

  return (
    <section>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold text-gray-200">{genre} <span className="text-xs text-gray-500 font-normal ml-1">({items.length})</span></h3>
        <div className="flex gap-1">
          <button onClick={() => scroll("left")} className="p-1 rounded text-gray-500 hover:bg-gray-800 hover:text-gray-300 transition-colors">
            <ChevronLeft size={14} />
          </button>
          <button onClick={() => scroll("right")} className="p-1 rounded text-gray-500 hover:bg-gray-800 hover:text-gray-300 transition-colors">
            <ChevronRight size={14} />
          </button>
        </div>
      </div>
      <div ref={scrollRef} className="grid grid-flow-col auto-cols-[20%] sm:auto-cols-[14%] md:auto-cols-[10%] lg:auto-cols-[8%] xl:auto-cols-[6.5%] gap-2 overflow-x-auto pb-2 scroll-smooth scrollbar-hide">
        {items.map((item) => (
          <ABSBookCard
            key={item.itemId}
            itemId={item.itemId}
            title={item.title}
            author={item.author}
            coverUrl={item.coverUrl}
            duration={item.duration}
            progress={item.progress}
            onNavigate={onNavigate}
            hasEbook={formatMatches?.[item.title]?.hasEbook}
            cached={cachedIds?.has(item.itemId)}
            unavailable={offline && !cachedIds?.has(item.itemId)}
            seriesName={localSeriesName(item)}
            sequence={localSeriesSequence(item)}
          />
        ))}
      </div>
    </section>
  );
}

function EbookCard({
  item,
  onNavigateToBook,
  hasAudio,
  cached,
  unavailable,
}: {
  item: KavitaItem;
  onNavigateToBook: NavigateToBook;
  hasAudio?: boolean;
  cached?: boolean;
  unavailable?: boolean;
}) {
  const [imgError, setImgError] = useState(false);

  useEffect(() => {
    setImgError(false);
  }, [item.coverUrl, item.seriesId]);

  const handleClick = () => {
    onNavigateToBook(item.title, item.author, { ebookSeriesId: item.seriesId });
  };

  const fallback = (
    <div className="absolute inset-0 flex items-center justify-center text-gray-600">
      <BookOpen size={24} />
    </div>
  );
  const showCover = Boolean(item.coverUrl) && !imgError;

  return (
    <div
      className={`group flex flex-col relative ${
        unavailable ? "opacity-45 grayscale-[0.35]" : ""
      }`}
    >
      <div
        className="relative aspect-[2/3] bg-gray-900 overflow-hidden rounded-lg border border-gray-800 group-hover:border-amber-600/50 transition-all duration-200 group-hover:shadow-lg group-hover:shadow-amber-900/10 group-hover:-translate-y-0.5 cursor-pointer"
        onClick={handleClick}
      >
        {showCover ? (
          <CoverImage
            src={item.coverUrl}
            alt={item.title}
            className="absolute inset-0 w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
            loading="lazy"
            onError={() => setImgError(true)}
          />
        ) : (
          fallback
        )}
        <div className="absolute inset-0 bg-black/0 group-hover:bg-black/40 transition-colors flex items-center justify-center pointer-events-none">
          <BookOpen size={24} className="text-white opacity-0 group-hover:opacity-100 transition-opacity drop-shadow-lg" />
        </div>
        {cached && (
          <span className="absolute top-1 left-1 px-1 py-0.5 rounded bg-black/65 text-[8px] font-semibold text-amber-300">
            Offline
          </span>
        )}
        <div className="absolute bottom-1 right-1 flex items-center gap-0.5">
          <BookOpen size={10} className="text-amber-400 drop-shadow" />
          {hasAudio && <Headphones size={10} className="text-emerald-400 drop-shadow" />}
        </div>
      </div>
      <ShelfCardMeta
        title={item.title}
        author={item.author}
        seriesName={localSeriesName(item)}
        sequence={localSeriesSequence(item)}
        titleClassName="hover:text-amber-400 transition-colors"
        onTitleClick={handleClick}
      />
    </div>
  );
}

function RDCard({ item, isResolving, onPlay, onResolve, onRemove, onNavigate, unavailable, cached }: {
  item: LibraryItem;
  isResolving: boolean;
  onPlay: () => void;
  onResolve: () => void;
  onRemove: () => void;
  onNavigate: () => void;
  unavailable?: boolean;
  cached?: boolean;
}) {
  const canPlay = item.streamStatus === "ready" && item.tracks.length > 0;
  return (
    <div className={`group flex flex-col relative ${unavailable ? "opacity-45 grayscale-[0.35]" : ""}`}>
      <div
        className="relative aspect-[2/3] bg-gray-900 overflow-hidden rounded-lg border border-gray-800 group-hover:border-gray-600 transition-all duration-200 group-hover:shadow-lg group-hover:shadow-black/20 group-hover:-translate-y-0.5 cursor-pointer"
        onClick={onNavigate}
      >
        <CoverImage
          src={item.coverUrl}
          alt={item.title}
          className="w-full h-full object-cover"
          loading="lazy"
          fallback={
            <div className="w-full h-full flex items-center justify-center text-gray-700"><BookOpen size={16} /></div>
          }
        />
        {cached && (
          <span className="absolute top-1 left-1 px-1 py-0.5 rounded bg-black/65 text-[8px] font-semibold text-emerald-300">
            Offline
          </span>
        )}
        {item.totalSeconds > 0 && item.progressSeconds > 0 && (
          <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-gray-700">
            <div className="h-full bg-brand-500" style={{ width: `${Math.round((item.progressSeconds / item.totalSeconds) * 100)}%` }} />
          </div>
        )}
      </div>
      <ShelfCardMeta
        title={item.title}
        author={item.author}
        seriesName={localSeriesName(item)}
        sequence={localSeriesSequence(item)}
        titleClassName="hover:text-brand-400 transition-colors"
        onTitleClick={onNavigate}
      >
        <div className="flex gap-1 mt-0.5 items-center">
          {canPlay && (
            <button
              onClick={(e) => { e.stopPropagation(); onPlay(); }}
              className="flex-1 flex items-center justify-center gap-0.5 py-1 bg-emerald-700 text-white text-[9px] font-medium rounded hover:bg-emerald-600 transition-colors"
              title="Listen"
            >
              <Play size={8} /> Listen
            </button>
          )}
          <button
            onClick={(e) => { e.stopPropagation(); onRemove(); }}
            className="p-1 text-gray-600 hover:text-red-400 transition-colors"
            title="Remove from My Collection"
          >
            <Trash2 size={10} />
          </button>
        </div>
      </ShelfCardMeta>
    </div>
  );
}

function LibraryGridSkeleton({ count = 18 }: { count?: number }) {
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-sm text-gray-400">
        <Loader2 size={16} className="animate-spin text-brand-400" />
        Loading your library…
      </div>
      <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-7 lg:grid-cols-9 xl:grid-cols-11 gap-2">
        {Array.from({ length: count }, (_, i) => (
          <BookCardSkeleton key={i} />
        ))}
      </div>
    </div>
  );
}

function EmptyABS({ onBrowse, onDownloads }: { onBrowse: () => void; onDownloads?: () => void }) {
  return (
    <div className="text-center py-16">
      <Headphones className="mx-auto mb-4 text-gray-600" size={40} />
      <h3 className="text-base font-semibold text-gray-300 mb-2">No audiobooks yet</h3>
      <p className="text-sm text-gray-500 mb-4 max-w-md mx-auto">
        Find a book in Browse → Get audiobook → track it under Requests → Listen here.
      </p>
      <div className="flex flex-wrap items-center justify-center gap-2">
        <button onClick={onBrowse} className="px-5 py-2 bg-emerald-600 text-white rounded-lg text-sm font-medium hover:bg-emerald-500 transition-colors">
          Browse
        </button>
        {onDownloads && (
          <button onClick={onDownloads} className="px-5 py-2 bg-gray-800 text-gray-200 border border-gray-700 rounded-lg text-sm font-medium hover:bg-gray-700 transition-colors">
            Requests
          </button>
        )}
      </div>
    </div>
  );
}
