import { useState, useCallback, useRef, useEffect, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import api from "../api/client";
import { useAuth } from "../hooks/useAuth";
import { useToast } from "../contexts/ToastContext";
import { usePlayer } from "../contexts/PlayerContext";
import ABSBookCard from "../components/ABSBookCard";
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
  series: Array<{ id: string; name: string; sequence: string }>;
  duration: number;
  progress: number;
  isFinished: boolean;
  narrator: string;
  numTracks: number;
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
  source: "kavita";
}

type Tab = "abs" | "streams" | "ebooks";
type MediaFilter = "all" | "audiobooks" | "ebooks";
type TabView = "all" | "genre" | "series";

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

  const { data: absCollection } = useQuery({
    queryKey: ["abs-collection"],
    queryFn: async () => {
      const { data } = await api.get("/library/abs/collection");
      return data as { genres: Record<string, ABSItem[]>; ungrouped: ABSItem[]; totalItems: number };
    },
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
    enabled: !!user && sessionReady,
  });

  const { data: absSeries } = useQuery({
    queryKey: ["abs-series"],
    queryFn: async () => {
      const { data } = await api.get("/library/abs/series");
      return data as { series: ABSSeries[] };
    },
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
    enabled: absView === "series",
  });

  const { data: rdLibrary, isLoading: rdLoading } = useQuery({
    queryKey: ["streaming-library"],
    queryFn: async () => {
      const { data } = await api.get("/library");
      return data as { items: LibraryItem[] };
    },
    enabled: !!user && sessionReady,
  });

  const { data: kavitaCollection, isLoading: kavitaLoading, isError: kavitaError, refetch: refetchKavita } = useQuery({
    queryKey: ["kavita-collection"],
    queryFn: async () => {
      const { data } = await api.get("/library/kavita/collection");
      return data as { items: KavitaItem[]; totalItems: number };
    },
    staleTime: 5 * 60 * 1000,
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
      toast("Removed from library", "info");
    },
  });

  const handleRefreshLibrary = useCallback(async () => {
    setScanning(true);
    try {
      await api.post("/library/abs/scan");
      queryClient.invalidateQueries({ queryKey: ["abs-collection"] });
      queryClient.invalidateQueries({ queryKey: ["abs-series"] });
      toast("Library refreshed — stale entries removed", "success");
    } catch {
      toast("Library scan failed", "error");
    } finally {
      setScanning(false);
    }
  }, [queryClient, toast]);

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

  const rdByGenre = (() => {
    if (!rdLibrary?.items) return {};
    const groups: Record<string, LibraryItem[]> = {};
    for (const item of rdLibrary.items) {
      const g = item.genre || "Uncategorized";
      (groups[g] ??= []).push(item);
    }
    return groups;
  })();

  const rdBySeries = (() => {
    if (!rdLibrary?.items) return {};
    const seriesRe = /^(.+?)\s+0?\d{1,2}\s*[-–]\s*.+/;
    const groups: Record<string, LibraryItem[]> = {};
    for (const item of rdLibrary.items) {
      const m = item.title.match(seriesRe);
      const key = m ? m[1].trim() : "Standalone";
      (groups[key] ??= []).push(item);
    }
    return groups;
  })();

  const ebookByGenre = (() => {
    if (!kavitaCollection?.items) return {};
    const groups: Record<string, KavitaItem[]> = {};
    for (const item of kavitaCollection.items) {
      const g = item.author || "Unknown Author";
      (groups[g] ??= []).push(item);
    }
    return groups;
  })();

  const ebookBySeries = (() => {
    if (!kavitaCollection?.items) return {};
    const seriesRe = /^(.+?)\s+0?\d{1,2}\s*[-–]\s*.+/;
    const groups: Record<string, KavitaItem[]> = {};
    for (const item of kavitaCollection.items) {
      const m = item.title.match(seriesRe);
      const key = m ? m[1].trim() : item.title;
      (groups[key] ??= []).push(item);
    }
    return groups;
  })();

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
              {absCollection ? `${absCollection.totalItems} audiobooks` : ""}
              {kavitaCollection?.totalItems ? ` · ${kavitaCollection.totalItems} ebooks` : ""}
              {rdLibrary?.items?.length ? ` · ${rdLibrary.items.length} in collection` : ""}
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
              <div className="flex gap-1 mb-4 bg-gray-800/30 p-0.5 rounded-md w-fit">
                <button
                  onClick={() => setAbsView("all")}
                  className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
                    absView === "all" ? "bg-gray-700 text-gray-100" : "text-gray-500 hover:text-gray-300"
                  }`}
                >
                  All
                </button>
                <button
                  onClick={() => setAbsView("genre")}
                  className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
                    absView === "genre" ? "bg-gray-700 text-gray-100" : "text-gray-500 hover:text-gray-300"
                  }`}
                >
                  By Genre
                </button>
                <button
                  onClick={() => setAbsView("series")}
                  className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
                    absView === "series" ? "bg-gray-700 text-gray-100" : "text-gray-500 hover:text-gray-300"
                  }`}
                >
                  By Series
                </button>
              </div>

              {absView === "all" && absCollection && (
                <div>
                  {absCollection.totalItems > 0 ? (
                    <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-7 lg:grid-cols-9 xl:grid-cols-11 gap-2">
                      {[...Object.values(absCollection.genres).flat(), ...absCollection.ungrouped]
                        .filter((item, idx, arr) => arr.findIndex(i => i.itemId === item.itemId) === idx)
                        .map((item) => (
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

              {absView === "genre" && absCollection && (
                <div className="space-y-6">
                  {Object.entries(absCollection.genres).map(([genre, items]) => (
                    <ABSGenreRow key={genre} genre={genre} items={items} onPlay={handlePlayABS} onNavigate={handleNavigateToBook} formatMatches={formatMatches} />
                  ))}
                  {absCollection.ungrouped.length > 0 && (
                    <ABSGenreRow genre="Uncategorized" items={absCollection.ungrouped} onPlay={handlePlayABS} onNavigate={handleNavigateToBook} formatMatches={formatMatches} />
                  )}
                  {Object.keys(absCollection.genres).length === 0 && absCollection.ungrouped.length === 0 && (
                    <EmptyABS onBrowse={() => navigate("/")} />
                  )}
                </div>
              )}

              {absView === "series" && (
                absSeries?.series ? (
                  <SeriesDrilldown series={absSeries.series} onPlay={handlePlayABS} />
                ) : (
                  <div className="flex items-center justify-center py-12 text-gray-400 gap-2">
                    <Loader2 size={16} className="animate-spin" />
                    Loading series...
                  </div>
                )
              )}
            </div>
          )}

          {/* Ebooks Tab */}
          {tab === "ebooks" && (
            <div>
              <div className="flex gap-1 mb-4 bg-gray-800/30 p-0.5 rounded-md w-fit">
                {(["all", "genre", "series"] as const).map((v) => (
                  <button
                    key={v}
                    onClick={() => setEbookView(v)}
                    className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
                      ebookView === v ? "bg-gray-700 text-gray-100" : "text-gray-500 hover:text-gray-300"
                    }`}
                  >
                    {v === "all" ? "All" : v === "genre" ? "By Genre" : "By Series"}
                  </button>
                ))}
              </div>
              {kavitaLoading && (
                <div className="flex justify-center py-12 text-gray-400 gap-2">
                  <Loader2 size={16} className="animate-spin" />
                  Loading ebooks...
                </div>
              )}
              {kavitaError && (
                <div className="text-center py-16">
                  <p className="text-red-400 mb-4">Failed to load ebooks. Check Kavita connection.</p>
                  <button onClick={() => refetchKavita()} className="px-4 py-2 bg-gray-700 text-gray-200 rounded-lg hover:bg-gray-600">
                    Retry
                  </button>
                </div>
              )}
              {!kavitaLoading && !kavitaError && kavitaCollection?.items && kavitaCollection.items.length > 0 && (
                <>
                  {ebookView === "all" && (
                    <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-7 lg:grid-cols-9 xl:grid-cols-11 gap-2">
                      {kavitaCollection.items.map((item) => (
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
                    <div className="space-y-6">
                      {Object.entries(ebookBySeries).map(([series, items]) => (
                        <EbookGenreRow key={series} genre={series} items={items} onNavigateToBook={handleNavigateToBook} formatMatches={formatMatches} />
                      ))}
                    </div>
                  )}
                </>
              )}
              {!kavitaLoading && !kavitaError && (!kavitaCollection?.items || kavitaCollection.items.length === 0) && (
                <div className="text-center py-16">
                  <BookOpen className="mx-auto mb-4 text-gray-600" size={40} />
                  <h3 className="text-base font-semibold text-gray-300 mb-2">No ebooks on your server</h3>
                  <p className="text-sm text-gray-500 mb-4">Add EPUB or PDF files to your Kavita library</p>
                </div>
              )}
            </div>
          )}

          {/* Personal Collection Tab */}
          {tab === "streams" && (
            <div>
              <div className="flex gap-1 mb-4 bg-gray-800/30 p-0.5 rounded-md w-fit">
                {(["all", "genre", "series"] as const).map((v) => (
                  <button
                    key={v}
                    onClick={() => setRdView(v)}
                    className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
                      rdView === v ? "bg-gray-700 text-gray-100" : "text-gray-500 hover:text-gray-300"
                    }`}
                  >
                    {v === "all" ? "All" : v === "genre" ? "By Genre" : "By Series"}
                  </button>
                ))}
              </div>
              {rdLoading && (
                <div className="flex items-center justify-center py-12 text-gray-400 gap-2">
                  <Loader2 size={16} className="animate-spin" />
                  Loading...
                </div>
              )}
              {rdLibrary && rdLibrary.items.length === 0 && (
                <div className="text-center py-16">
                  <BookOpen className="mx-auto mb-4 text-gray-600" size={40} />
                  <h3 className="text-base font-semibold text-gray-300 mb-2">No items yet</h3>
                  <p className="text-sm text-gray-500 mb-4">Add books, stream, or request — they'll appear here</p>
                  <button onClick={() => navigate("/")} className="px-5 py-2 bg-brand-600 text-white rounded-lg text-sm font-medium hover:bg-brand-500 transition-colors">
                    Browse Books
                  </button>
                </div>
              )}
              {rdView === "all" && rdLibrary?.items && rdLibrary.items.length > 0 && (
                <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 xl:grid-cols-10 gap-2">
                  {rdLibrary.items.map((item) => (
                    <RDCard
                      key={item.id}
                      item={item}
                      isResolving={resolvingId === item.id}
                      onPlay={() => handlePlayRD(item)}
                      onResolve={() => handleResolveRD(item)}
                      onRemove={() => removeMutation.mutate(item.id)}
                      onNavigate={() => navigate(`/book/${encodeURIComponent(item.googleVolumeId)}`)}
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
                        onNavigate={() => navigate(`/book/${encodeURIComponent(item.googleVolumeId)}`)}
                      />
                    ))}
                  </div>
                </div>
              ))}
              {rdView === "series" && Object.entries(rdBySeries).map(([series, items]) => (
                <div key={series} className="mb-6">
                  <h3 className="text-sm font-semibold text-gray-300 mb-3">{series}</h3>
                  <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8 xl:grid-cols-10 gap-2">
                    {items.map((item) => (
                      <RDCard
                        key={item.id}
                        item={item}
                        isResolving={resolvingId === item.id}
                        onPlay={() => handlePlayRD(item)}
                        onResolve={() => handleResolveRD(item)}
                        onRemove={() => removeMutation.mutate(item.id)}
                        onNavigate={() => navigate(`/book/${encodeURIComponent(item.googleVolumeId)}`)}
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
