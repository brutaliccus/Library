import { useState, useCallback, useRef, useEffect, useMemo } from "react";
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
  BookOpen,
  Headphones,
  Radio,
  ChevronLeft,
  ChevronRight,
  X,
  RefreshCw,
} from "lucide-react";
import { getProgress, clearProgress } from "../utils/readingProgress";
import { isBookCached } from "../utils/audioCache";
import {
  getOfflineProgress,
  getRdOfflineManifest,
  isLikelyOffline,
  progressKeyForRd,
} from "../utils/offlinePlayback";

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

interface PersonalSeries {
  id: string;
  name: string;
  books: Array<LibraryItem & { sequence?: string; itemId?: string }>;
  bookCount: number;
  coverUrl: string;
}

interface ABSItem {
  itemId: string;
  title: string;
  author: string;
  coverUrl: string;
  genres: string[];
  series: Array<{ id: string; name: string; sequence: string }>;
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
  addedAt?: number;
  source: "kavita";
}

type Tab = "abs" | "streams" | "ebooks";
type MediaFilter = "all" | "audiobooks" | "ebooks";
type TabView = "all" | "genre" | "series" | "author";

export type NavigateToBook = (
  title: string,
  author?: string,
  target?: { ebookChapterId?: number; absItemId?: string }
) => void;

export default function MyLibrary() {
  const { user, sessionReady } = useAuth();
  const { toast } = useToast();
  const { playABS, playRD } = usePlayer();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [tab, setTab] = useState<Tab>("abs");
  const [absView, setAbsView] = useState<TabView>("all");
  const [ebookView, setEbookView] = useState<TabView>("all");
  const [rdView, setRdView] = useState<TabView>("all");
  const [mediaFilter, setMediaFilter] = useState<MediaFilter>("all");
  const [filterGenre, setFilterGenre] = useState("");
  const [filterSeries, setFilterSeries] = useState("");
  const [filterAuthor, setFilterAuthor] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [resolvingId, setResolvingId] = useState<number | null>(null);
  const [scanning, setScanning] = useState(false);
  const [continueModal, setContinueModal] = useState<{
    chapterId: number;
    item: KavitaItem;
    progress: NonNullable<ReturnType<typeof getProgress>>;
  } | null>(null);

  const debounceRef = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => {
    debounceRef.current = setTimeout(() => setDebouncedQuery(searchQuery), 300);
    return () => clearTimeout(debounceRef.current);
  }, [searchQuery]);

  const {
    data: absCollection,
    isLoading: absLoading,
    isFetching: absFetching,
  } = useQuery({
    queryKey: ["abs-collection"],
    queryFn: async () => {
      const { data } = await api.get("/library/abs/collection");
      return data as { genres: Record<string, ABSItem[]>; ungrouped: ABSItem[]; totalItems: number };
    },
    staleTime: 30 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    refetchOnWindowFocus: false,
    enabled: !!user && sessionReady,
  });

  const { data: absSeries, isLoading: absSeriesLoading } = useQuery({
    queryKey: ["abs-series"],
    queryFn: async () => {
      const { data } = await api.get("/library/abs/series");
      return data as { series: ABSSeries[] };
    },
    // Hardcover-backed (same as book-detail “More in this series”) — cache longer
    staleTime: 30 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    refetchOnWindowFocus: false,
    enabled: !!user && sessionReady && tab === "abs",
  });

  const { data: kavitaSeries, isLoading: kavitaSeriesLoading } = useQuery({
    queryKey: ["kavita-series"],
    queryFn: async () => {
      const { data } = await api.get("/library/kavita/series");
      return data as {
        series: Array<{
          id: string;
          name: string;
          books: KavitaItem[];
          bookCount: number;
          coverUrl: string;
        }>;
      };
    },
    staleTime: 30 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    refetchOnWindowFocus: false,
    enabled: !!user && sessionReady && tab === "ebooks",
  });

  const { data: rdLibrary, isLoading: rdLoading, isFetching: rdFetching } = useQuery({
    queryKey: ["streaming-library"],
    queryFn: async () => {
      const { data } = await api.get("/library");
      return data as { items: LibraryItem[] };
    },
    staleTime: 10 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    refetchOnWindowFocus: false,
    enabled: !!user && sessionReady,
  });

  const { data: personalSeries, isLoading: personalSeriesLoading } = useQuery({
    queryKey: ["personal-series"],
    queryFn: async () => {
      const { data } = await api.get("/library/series");
      return data as { series: PersonalSeries[] };
    },
    staleTime: 30 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    refetchOnWindowFocus: false,
    enabled: !!user && sessionReady && tab === "streams",
  });

  const {
    data: kavitaCollection,
    isLoading: kavitaLoading,
    isFetching: kavitaFetching,
    isError: kavitaError,
    refetch: refetchKavita,
  } = useQuery({
    queryKey: ["kavita-collection"],
    queryFn: async () => {
      const { data } = await api.get("/library/kavita/collection");
      return data as { items: KavitaItem[]; totalItems: number };
    },
    staleTime: 30 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    refetchOnWindowFocus: false,
    enabled: !!user && sessionReady,
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
    enabled: debouncedQuery.length >= 2,
  });

  const removeMutation = useMutation({
    mutationFn: (id: number) => api.delete(`/library/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["streaming-library"] });
      queryClient.invalidateQueries({ queryKey: ["personal-series"] });
      toast("Removed from library", "info");
    },
  });

  const handleRefreshLibrary = useCallback(async () => {
    setScanning(true);
    try {
      await Promise.allSettled([
        api.post("/library/abs/scan"),
        api.post("/library/kavita/scan"),
      ]);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["abs-collection"] }),
        queryClient.invalidateQueries({ queryKey: ["abs-series"] }),
        queryClient.invalidateQueries({ queryKey: ["kavita-collection"] }),
        queryClient.invalidateQueries({ queryKey: ["kavita-series"] }),
        queryClient.invalidateQueries({ queryKey: ["streaming-library"] }),
        queryClient.invalidateQueries({ queryKey: ["personal-series"] }),
      ]);
      toast("Library refreshed — ABS + Kavita scanned", "success");
    } catch {
      toast("Library scan failed", "error");
    } finally {
      setScanning(false);
    }
  }, [queryClient, toast]);

  // Reset shelf filters when switching media tabs
  useEffect(() => {
    setFilterGenre("");
    setFilterSeries("");
    setFilterAuthor("");
  }, [tab]);

  const handlePlayABS = useCallback(
    async (itemId: string) => {
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
    [playABS, toast]
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
    (chapterId: number, item: KavitaItem) => {
      const progress = getProgress(chapterId);
      if (progress) {
        setContinueModal({ chapterId, item, progress });
      } else {
        navigate(`/read/${chapterId}`);
      }
    },
    [navigate]
  );

  const handleContinueReading = useCallback(
    (chapterId: number) => {
      setContinueModal(null);
      navigate(`/read/${chapterId}`);
    },
    [navigate]
  );

  const handleStartFromBeginning = useCallback(
    (chapterId: number) => {
      clearProgress(chapterId);
      setContinueModal(null);
      navigate(`/read/${chapterId}`);
    },
    [navigate]
  );

  const handleNavigateToBook = useCallback(
    async (title: string, author?: string, target?: { ebookChapterId?: number; absItemId?: string }) => {
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
    [navigate]
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

  const absSeriesMembership = useMemo(() => {
    const byName = new Map<string, Set<string>>();
    for (const s of absSeries?.series || []) {
      byName.set(s.name, new Set(s.books.map((b) => b.itemId)));
    }
    return byName;
  }, [absSeries]);

  const absFilterOptions = useMemo(() => {
    const genres = new Set<string>();
    const authors = new Set<string>();
    for (const item of allAbsItems) {
      (item.genres || []).forEach((g) => g && genres.add(g));
      if (item.author) authors.add(item.author);
    }
    // Series names come from Hardcover groups only (not ABS junk labels)
    const series = (absSeries?.series || []).map((s) => s.name).sort((a, b) => a.localeCompare(b));
    return {
      genres: Array.from(genres).sort(),
      series,
      authors: Array.from(authors).sort(),
    };
  }, [allAbsItems, absSeries]);

  const filteredAbsItems = useMemo(() => {
    return allAbsItems.filter((item) => {
      if (filterGenre && !(item.genres || []).some((g) => g === filterGenre || g.toLowerCase().includes(filterGenre.toLowerCase()))) {
        return false;
      }
      if (filterSeries) {
        const members = absSeriesMembership.get(filterSeries);
        if (!members?.has(item.itemId)) return false;
      }
      if (filterAuthor && item.author !== filterAuthor) return false;
      return true;
    });
  }, [allAbsItems, filterGenre, filterSeries, filterAuthor, absSeriesMembership]);

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

  const allEbookItems = useMemo(() => {
    const items = [...(kavitaCollection?.items || [])];
    return items.sort((a, b) => (b.addedAt || 0) - (a.addedAt || 0));
  }, [kavitaCollection]);

  const ebookSeriesMembership = useMemo(() => {
    const byName = new Map<string, Set<number>>();
    for (const s of kavitaSeries?.series || []) {
      byName.set(s.name, new Set(s.books.map((b) => b.seriesId)));
    }
    return byName;
  }, [kavitaSeries]);

  const ebookFilterOptions = useMemo(() => {
    const genres = new Set<string>();
    const authors = new Set<string>();
    for (const item of allEbookItems) {
      (item.genres || []).forEach((g) => g && genres.add(g));
      if (item.author) authors.add(item.author);
    }
    const series = (kavitaSeries?.series || []).map((s) => s.name).sort((a, b) => a.localeCompare(b));
    return {
      genres: Array.from(genres).sort(),
      series,
      authors: Array.from(authors).sort(),
    };
  }, [allEbookItems, kavitaSeries]);

  const filteredEbookItems = useMemo(() => {
    return allEbookItems.filter((item) => {
      if (filterGenre && !(item.genres || []).includes(filterGenre)) return false;
      if (filterSeries) {
        const members = ebookSeriesMembership.get(filterSeries);
        if (!members?.has(item.seriesId)) return false;
      }
      if (filterAuthor && item.author !== filterAuthor) return false;
      return true;
    });
  }, [allEbookItems, filterGenre, filterSeries, filterAuthor, ebookSeriesMembership]);

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

  const rdItemsSorted = useMemo(() => {
    const items = [...(rdLibrary?.items || [])];
    return items.sort((a, b) => {
      const ta = a.createdAt ? new Date(a.createdAt).getTime() : 0;
      const tb = b.createdAt ? new Date(b.createdAt).getTime() : 0;
      return tb - ta;
    });
  }, [rdLibrary]);

  const rdSeriesMembership = useMemo(() => {
    const byName = new Map<string, Set<number>>();
    for (const s of personalSeries?.series || []) {
      byName.set(s.name, new Set(s.books.map((b) => b.id)));
    }
    return byName;
  }, [personalSeries]);

  const rdFilterOptions = useMemo(() => {
    const genres = new Set<string>();
    const authors = new Set<string>();
    for (const item of rdItemsSorted) {
      if (item.genre) genres.add(item.genre);
      (item.genres || []).forEach((g) => g && genres.add(g));
      if (item.author) authors.add(item.author);
    }
    const series = (personalSeries?.series || []).map((s) => s.name).sort((a, b) => a.localeCompare(b));
    return {
      genres: Array.from(genres).sort(),
      series,
      authors: Array.from(authors).sort(),
    };
  }, [rdItemsSorted, personalSeries]);

  const filteredRdItems = useMemo(() => {
    return rdItemsSorted.filter((item) => {
      const itemGenres = item.genres?.length ? item.genres : (item.genre ? [item.genre] : []);
      if (filterGenre && !itemGenres.includes(filterGenre) && (item.genre || "Uncategorized") !== filterGenre) {
        return false;
      }
      if (filterSeries) {
        const members = rdSeriesMembership.get(filterSeries);
        if (!members?.has(item.id)) return false;
      }
      if (filterAuthor && item.author !== filterAuthor) return false;
      return true;
    });
  }, [rdItemsSorted, filterGenre, filterSeries, filterAuthor, rdSeriesMembership]);

  const rdByGenre = useMemo(() => {
    const groups: Record<string, LibraryItem[]> = {};
    for (const item of filteredRdItems) {
      const gs = item.genres?.length ? item.genres : [item.genre || "Uncategorized"];
      for (const g of gs) (groups[g] ??= []).push(item);
    }
    return Object.fromEntries(Object.entries(groups).sort(([a], [b]) => a.localeCompare(b)));
  }, [filteredRdItems]);

  const rdByAuthor = useMemo(() => {
    const groups: Record<string, LibraryItem[]> = {};
    for (const item of filteredRdItems) {
      const a = item.author || "Unknown Author";
      (groups[a] ??= []).push(item);
    }
    return Object.fromEntries(Object.entries(groups).sort(([a], [b]) => a.localeCompare(b)));
  }, [filteredRdItems]);

  const handlePersonalCollectionNavigate = useCallback(
    async (item: LibraryItem) => {
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
    [navigate]
  );

  const FilterBar = ({
    options,
  }: {
    options: { genres: string[]; series: string[]; authors: string[] };
  }) => (
    <div className="flex flex-wrap gap-2 mb-4">
      <select
        value={filterGenre}
        onChange={(e) => setFilterGenre(e.target.value)}
        className="px-2.5 py-1.5 bg-gray-800 border border-gray-700 rounded-lg text-xs text-gray-200"
      >
        <option value="">All genres</option>
        {options.genres.map((g) => (
          <option key={g} value={g}>{g}</option>
        ))}
      </select>
      <select
        value={filterSeries}
        onChange={(e) => setFilterSeries(e.target.value)}
        className="px-2.5 py-1.5 bg-gray-800 border border-gray-700 rounded-lg text-xs text-gray-200"
      >
        <option value="">All series</option>
        {options.series.map((s) => (
          <option key={s} value={s}>{s}</option>
        ))}
      </select>
      <select
        value={filterAuthor}
        onChange={(e) => setFilterAuthor(e.target.value)}
        className="px-2.5 py-1.5 bg-gray-800 border border-gray-700 rounded-lg text-xs text-gray-200 max-w-[200px]"
      >
        <option value="">All authors</option>
        {options.authors.map((a) => (
          <option key={a} value={a}>{a}</option>
        ))}
      </select>
      {(filterGenre || filterSeries || filterAuthor) && (
        <button
          type="button"
          onClick={() => {
            setFilterGenre("");
            setFilterSeries("");
            setFilterAuthor("");
          }}
          className="px-2.5 py-1.5 text-xs text-gray-400 hover:text-gray-200"
        >
          Clear filters
        </button>
      )}
    </div>
  );

  const viewToggle = (view: TabView, setView: (v: TabView) => void) => (
    <div className="flex gap-1 mb-4 bg-gray-800/30 p-0.5 rounded-md w-fit flex-wrap">
      {(["all", "genre", "series", "author"] as const).map((v) => (
        <button
          key={v}
          onClick={() => setView(v)}
          className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
            view === v ? "bg-gray-700 text-gray-100" : "text-gray-500 hover:text-gray-300"
          }`}
        >
          {v === "all" ? "All" : v === "genre" ? "By Genre" : v === "series" ? "By Series" : "By Author"}
        </button>
      ))}
    </div>
  );

  return (
    <div className="max-w-7xl mx-auto px-4 lg:px-6 py-8">
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
      <div className="flex items-center justify-between mb-6">
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
            onClick={handleRefreshLibrary}
            disabled={scanning}
            className="flex items-center gap-2 px-3 py-2 bg-gray-700 text-gray-200 rounded-lg hover:bg-gray-600 transition-colors text-sm font-medium disabled:opacity-50"
            title="Rescan library and remove stale entries"
          >
            <RefreshCw size={15} className={scanning ? "animate-spin" : ""} />
            {scanning ? "Scanning..." : "Refresh"}
          </button>
          <button
            onClick={() => navigate("/")}
            className="flex items-center gap-2 px-4 py-2 bg-brand-600 text-white rounded-lg hover:bg-brand-500 transition-colors text-sm font-medium"
          >
            <Search size={16} />
            Browse Store
          </button>
        </div>
      </div>

      {/* Search */}
      <div className="relative mb-6">
        <Search size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Search your library..."
          className="w-full pl-10 pr-10 py-2.5 bg-gray-800 border border-gray-700 rounded-xl text-sm text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent placeholder:text-gray-500"
        />
        {searchQuery && (
          <button onClick={() => setSearchQuery("")} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300">
            <X size={16} />
          </button>
        )}
      </div>

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
                  {m === "all" ? "All" : m === "audiobooks" ? "Audiobooks" : "Ebooks"}
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
                      navigate(`/book/${encodeURIComponent(r.googleVolumeId)}`);
                    } else if (r.source === "abs") {
                      handleNavigateToBook(r.title, r.author, { absItemId: r.itemId });
                    } else if (r.source === "kavita") {
                      handleNavigateToBook(r.title, r.author, { ebookChapterId: r.chapterId ?? undefined });
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
                        : "bg-purple-900/40 text-purple-400"
                  }`}>
                    {r.source === "abs" ? "ABS" : r.source === "kavita" ? "Ebook" : "RD"}
                  </span>
                  {r.source === "abs" ? (
                    <Headphones size={16} className="text-gray-600 group-hover:text-emerald-400 transition-colors shrink-0" />
                  ) : r.source === "kavita" ? (
                    <BookOpen size={16} className="text-gray-600 group-hover:text-amber-400 transition-colors shrink-0" />
                  ) : (
                    <Radio size={16} className="text-gray-600 group-hover:text-purple-400 transition-colors shrink-0" />
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
          {/* Tabs */}
          <div className="flex gap-1 mb-6 bg-gray-800/50 p-1 rounded-lg w-fit">
            <button
              onClick={() => setTab("abs")}
              className={`flex items-center gap-1.5 px-4 py-2 rounded-md text-sm font-medium transition-colors ${
                tab === "abs" ? "bg-emerald-600 text-white" : "text-gray-400 hover:text-gray-200"
              }`}
            >
              <Headphones size={14} />
              Audiobookshelf
            </button>
            <button
              onClick={() => setTab("ebooks")}
              className={`flex items-center gap-1.5 px-4 py-2 rounded-md text-sm font-medium transition-colors ${
                tab === "ebooks" ? "bg-amber-600 text-white" : "text-gray-400 hover:text-gray-200"
              }`}
            >
              <BookOpen size={14} />
              Ebooks
            </button>
            <button
              onClick={() => setTab("streams")}
              className={`flex items-center gap-1.5 px-4 py-2 rounded-md text-sm font-medium transition-colors ${
                tab === "streams" ? "bg-purple-600 text-white" : "text-gray-400 hover:text-gray-200"
              }`}
            >
              <Radio size={14} />
              Personal Collection
            </button>
          </div>

          {/* ABS Tab */}
          {tab === "abs" && (
            <div>
              {viewToggle(absView, setAbsView)}
              <FilterBar options={absFilterOptions} />

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
                          onPlay={handlePlayABS}
                          onNavigate={handleNavigateToBook}
                          hasEbook={formatMatches?.[item.title]?.hasEbook}
                        />
                      ))}
                    </div>
                  ) : (
                    <EmptyABS onBrowse={() => navigate("/")} />
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
                        <ABSGenreRow key={genre} genre={genre} items={items} onPlay={handlePlayABS} onNavigate={handleNavigateToBook} formatMatches={formatMatches} />
                      ))}
                      {Object.keys(absByGenre).length === 0 && (
                        <EmptyABS onBrowse={() => navigate("/")} />
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
                      <ABSGenreRow key={author} genre={author} items={items} onPlay={handlePlayABS} onNavigate={handleNavigateToBook} formatMatches={formatMatches} />
                    ))
                  )}
                </div>
              )}

              {absView === "series" && (
                absSeriesLoading ? (
                  <div className="flex items-center justify-center py-12 text-gray-400 gap-2">
                    <Loader2 size={16} className="animate-spin" />
                    Matching series (Hardcover)…
                  </div>
                ) : absSeries?.series && absSeries.series.length > 0 ? (
                  <SeriesDrilldown series={absSeries.series} onPlay={handlePlayABS} />
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
              {viewToggle(ebookView, setEbookView)}
              <FilterBar options={ebookFilterOptions} />
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
                        />
                      ))}
                    </div>
                  )}
                  {ebookView === "genre" && (
                    <div className="space-y-6">
                      {Object.entries(ebookByGenre).map(([genre, items]) => (
                        <EbookGenreRow key={genre} genre={genre} items={items} onNavigateToBook={handleNavigateToBook} formatMatches={formatMatches} />
                      ))}
                    </div>
                  )}
                  {ebookView === "series" && (
                    kavitaSeriesLoading ? (
                      <div className="flex items-center justify-center py-12 text-gray-400 gap-2">
                        <Loader2 size={16} className="animate-spin" />
                        Matching series (Hardcover)…
                      </div>
                    ) : kavitaSeries?.series && kavitaSeries.series.length > 0 ? (
                      <div className="space-y-6">
                        {kavitaSeries.series.map((s) => (
                          <EbookGenreRow
                            key={s.id || s.name}
                            genre={s.name}
                            items={s.books}
                            onNavigateToBook={handleNavigateToBook}
                            formatMatches={formatMatches}
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
                        <EbookGenreRow key={author} genre={author} items={items} onNavigateToBook={handleNavigateToBook} formatMatches={formatMatches} />
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

          {/* Personal Collection Tab */}
          {tab === "streams" && (
            <div>
              {viewToggle(rdView, setRdView)}
              <FilterBar options={rdFilterOptions} />
              {rdLoading && !rdLibrary && <LibraryGridSkeleton />}
              {!rdLoading && rdItemsSorted.length === 0 && (
                <div className="text-center py-16">
                  <BookOpen className="mx-auto mb-4 text-gray-600" size={40} />
                  <h3 className="text-base font-semibold text-gray-300 mb-2">No items yet</h3>
                  <p className="text-sm text-gray-500 mb-4">
                    Books you explicitly add to Personal Collection appear here — streams are not auto-added
                  </p>
                  <button onClick={() => navigate("/")} className="px-5 py-2 bg-brand-600 text-white rounded-lg text-sm font-medium hover:bg-brand-500 transition-colors">
                    Browse Books
                  </button>
                </div>
              )}
              {rdView === "all" && filteredRdItems.length > 0 && (
                <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 xl:grid-cols-10 gap-2">
                  {filteredRdItems.map((item) => (
                    <RDCard
                      key={item.id}
                      item={item}
                      isResolving={resolvingId === item.id}
                      onPlay={() => handlePlayRD(item)}
                      onResolve={() => handleResolveRD(item)}
                      onRemove={() => removeMutation.mutate(item.id)}
                      onNavigate={() => handlePersonalCollectionNavigate(item)}
                    />
                  ))}
                </div>
              )}
              {rdView === "genre" && Object.entries(rdByGenre).map(([genre, items]) => (
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
                      />
                    ))}
                  </div>
                </div>
              ))}
              {rdView === "series" && (
                personalSeriesLoading ? (
                  <div className="flex items-center justify-center py-12 text-gray-400 gap-2">
                    <Loader2 size={16} className="animate-spin" />
                    Matching series (Hardcover)…
                  </div>
                ) : personalSeries?.series && personalSeries.series.length > 0 ? (
                  personalSeries.series.map((s) => (
                    <div key={s.id || s.name} className="mb-6">
                      <h3 className="text-sm font-semibold text-gray-300 mb-3">
                        {s.name}
                        <span className="text-gray-500 font-normal ml-2">{s.bookCount}</span>
                      </h3>
                      <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 xl:grid-cols-10 gap-2">
                        {s.books.map((item) => (
                          <RDCard
                            key={item.id}
                            item={item}
                            isResolving={resolvingId === item.id}
                            onPlay={() => handlePlayRD(item)}
                            onResolve={() => handleResolveRD(item)}
                            onRemove={() => removeMutation.mutate(item.id)}
                            onNavigate={() => handlePersonalCollectionNavigate(item)}
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
              {rdView === "author" && Object.entries(rdByAuthor).map(([author, items]) => (
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
                      />
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function EbookGenreRow({ genre, items, onNavigateToBook, formatMatches }: { genre: string; items: KavitaItem[]; onNavigateToBook: NavigateToBook; formatMatches?: Record<string, { hasEbook: boolean; hasAudio: boolean }> }) {
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
          <EbookCard key={item.seriesId} item={item} onNavigateToBook={onNavigateToBook} hasAudio={formatMatches?.[item.title]?.hasAudio} />
        ))}
      </div>
    </section>
  );
}

function ABSGenreRow({ genre, items, onPlay, onNavigate, formatMatches }: { genre: string; items: ABSItem[]; onPlay: (id: string) => void; onNavigate?: NavigateToBook; formatMatches?: Record<string, { hasEbook: boolean; hasAudio: boolean }> }) {
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
            onPlay={onPlay}
            onNavigate={onNavigate}
            hasEbook={formatMatches?.[item.title]?.hasEbook}
          />
        ))}
      </div>
    </section>
  );
}

function EbookCard({ item, onNavigateToBook, hasAudio }: { item: KavitaItem; onNavigateToBook: NavigateToBook; hasAudio?: boolean }) {
  const [imgError, setImgError] = useState(false);
  const [isVisible, setIsVisible] = useState(false);
  const cardRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = cardRef.current;
    if (!el || !item.coverUrl) return;
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting) setIsVisible(true);
      },
      { rootMargin: "100px", threshold: 0.01 }
    );
    io.observe(el);
    return () => io.disconnect();
  }, [item.coverUrl]);

  const handleClick = () => {
    if (item.chapterId) {
      onNavigateToBook(item.title, item.author, { ebookChapterId: item.chapterId });
    } else {
      onNavigateToBook(item.title, item.author);
    }
  };

  const fallback = (
    <div className="w-full h-full flex items-center justify-center text-gray-600">
      <BookOpen size={24} />
    </div>
  );
  const showCover = item.coverUrl && !imgError && isVisible;
  return (
    <div ref={cardRef} className="group bg-gray-800/50 rounded-lg overflow-hidden border border-gray-800 hover:border-amber-600/50 hover:bg-gray-800 transition-all duration-200 hover:shadow-lg hover:shadow-amber-900/10 hover:-translate-y-0.5 h-full relative">
      <div className="relative aspect-[2/3] bg-gray-900 overflow-hidden cursor-pointer" onClick={handleClick}>
        {showCover ? (
          <CoverImage
            src={item.coverUrl}
            alt={item.title}
            className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
            loading="lazy"
            onError={() => setImgError(true)}
          />
        ) : (
          fallback
        )}
        <div className="absolute inset-0 bg-black/0 group-hover:bg-black/40 transition-colors flex items-center justify-center">
          <BookOpen size={24} className="text-white opacity-0 group-hover:opacity-100 transition-opacity drop-shadow-lg" />
        </div>
        <div className="absolute bottom-1 right-1 flex items-center gap-0.5">
          <BookOpen size={10} className="text-amber-400 drop-shadow" />
          {hasAudio && <Headphones size={10} className="text-emerald-400 drop-shadow" />}
        </div>
      </div>
      <div className="p-1.5">
        <h3 className="text-[10px] font-semibold text-gray-100 line-clamp-2 leading-tight cursor-pointer hover:text-amber-400 transition-colors" onClick={handleClick}>{item.title}</h3>
        {item.author && <p className="text-[9px] text-gray-400 line-clamp-1">{item.author}</p>}
      </div>
    </div>
  );
}

function RDCard({ item, isResolving, onPlay, onResolve, onRemove, onNavigate }: {
  item: LibraryItem;
  isResolving: boolean;
  onPlay: () => void;
  onResolve: () => void;
  onRemove: () => void;
  onNavigate: () => void;
}) {
  const canPlay = item.streamStatus === "ready" && item.tracks.length > 0;
  return (
    <div className="group bg-gray-800/50 rounded-lg overflow-hidden border border-gray-800 hover:border-gray-700 transition-colors relative">
      <div className="relative aspect-[2/3] bg-gray-900 cursor-pointer" onClick={onNavigate}>
        <CoverImage
          src={item.coverUrl}
          alt={item.title}
          className="w-full h-full object-cover"
          loading="lazy"
          fallback={
            <div className="w-full h-full flex items-center justify-center text-gray-700"><BookOpen size={16} /></div>
          }
        />
        {item.totalSeconds > 0 && item.progressSeconds > 0 && (
          <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-gray-700">
            <div className="h-full bg-purple-500" style={{ width: `${Math.round((item.progressSeconds / item.totalSeconds) * 100)}%` }} />
          </div>
        )}
      </div>
      <div className="p-1.5">
        <h3 className="text-[10px] font-semibold text-gray-100 line-clamp-2 leading-tight cursor-pointer hover:text-brand-400 transition-colors" onClick={onNavigate}>{item.title}</h3>
        <div className="flex gap-1 mt-1">
          {canPlay ? (
            <button onClick={onPlay} className="flex-1 flex items-center justify-center gap-0.5 py-1 bg-purple-600 text-white text-[9px] font-medium rounded hover:bg-purple-500 transition-colors">
              <Play size={8} /> Play
            </button>
          ) : item.magnetLink ? (
            <button onClick={onResolve} disabled={isResolving} className="flex-1 flex items-center justify-center gap-0.5 py-1 bg-brand-600 text-white text-[9px] font-medium rounded hover:bg-brand-500 disabled:opacity-50 transition-colors">
              {isResolving ? <Loader2 size={8} className="animate-spin" /> : <Play size={8} />}
              {isResolving ? "..." : "Stream"}
            </button>
          ) : null}
          <button onClick={onRemove} className="p-1 text-gray-600 hover:text-red-400 transition-colors" title="Remove">
            <Trash2 size={10} />
          </button>
        </div>
      </div>
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

function EmptyABS({ onBrowse }: { onBrowse: () => void }) {
  return (
    <div className="text-center py-16">
      <Headphones className="mx-auto mb-4 text-gray-600" size={40} />
      <h3 className="text-base font-semibold text-gray-300 mb-2">No audiobooks on your server</h3>
      <p className="text-sm text-gray-500 mb-4">Request books to add them to your Audiobookshelf library</p>
      <button onClick={onBrowse} className="px-5 py-2 bg-emerald-600 text-white rounded-lg text-sm font-medium hover:bg-emerald-500 transition-colors">
        Browse Books
      </button>
    </div>
  );
}
