import { useState, useMemo, useCallback, useRef, useEffect } from "react";
import { useQuery, useQueries, useQueryClient } from "@tanstack/react-query";
import api from "../api/client";
import { useToast } from "../contexts/ToastContext";
import { usePlayer } from "../contexts/PlayerContext";
import ResultCard from "./ResultCard";
import Modal from "./Modal";
import { Search, Headphones, BookText, Loader2 } from "lucide-react";
import type { SearchResult } from "../types/book";

interface Props {
  title: string;
  author: string;
  coverUrl?: string;
  subtitle?: string;
  seriesName?: string;
  seriesIndex?: string;
}

type MediaFilter = "all" | "audiobook" | "ebook";

function SearchProgressBar({ phase, progress }: { phase: string; progress: number }) {
  const pct = Math.min(100, Math.max(0, progress));
  return (
    <div className="mb-5">
      <div className="flex justify-between text-xs text-gray-400 mb-1.5 gap-3">
        <span className="min-w-0">{phase}</span>
        <span className="shrink-0 tabular-nums">{Math.round(pct)}%</span>
      </div>
      <div className="h-2.5 bg-gray-900 rounded-full overflow-hidden border border-gray-700/50">
        <div
          className="h-full bg-brand-500 transition-[width] duration-500 ease-out rounded-full"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export default function DownloadPanel({
  title, author, coverUrl, subtitle, seriesName, seriesIndex,
}: Props) {
  const { toast } = useToast();
  const { playRD } = usePlayer();
  const queryClient = useQueryClient();
  const [modalOpen, setModalOpen] = useState(false);
  const [searchTrigger, setSearchTrigger] = useState(0);
  const [requestingIdx, setRequestingIdx] = useState<number | null>(null);
  const [streamingIdx, setStreamingIdx] = useState<number | null>(null);
  const [streamProgress, setStreamProgress] = useState<{ detail: string; progress: number } | null>(null);
  const [mediaFilter, setMediaFilter] = useState<MediaFilter>("all");
  const [searchPhase, setSearchPhase] = useState("");
  const [searchProgress, setSearchProgress] = useState(0);
  const [showWeakMatches, setShowWeakMatches] = useState(false);
  const [liveSearch, setLiveSearch] = useState(false);
  const pollRef = useRef(false);

  interface SearchResponse {
    results: SearchResult[];
    count: number;
    totalFetched?: number;
    hiddenCount?: number;
    matchCounts?: { exact: number; likely: number; weak: number };
  }

  const searchParams = useMemo(() => {
    const p = new URLSearchParams();
    p.set("title", title);
    if (author) p.set("author", author);
    if (subtitle) p.set("subtitle", subtitle);
    if (seriesName) p.set("series_name", seriesName);
    if (seriesIndex) p.set("series_index", seriesIndex);
    return p;
  }, [title, author, subtitle, seriesName, seriesIndex]);

  const searchHint = seriesIndex
    ? `Searching for Book ${seriesIndex} in ${seriesName || title}…`
    : null;

  const [indexersQuery, aaQuery] = useQueries({
    queries: [
      {
        queryKey: ["search-indexers", title, author, subtitle, seriesName, seriesIndex, searchTrigger, liveSearch],
        queryFn: async () => {
          const liveParam = liveSearch ? "true" : "false";
          const { data } = await api.get(
            `/search?${searchParams.toString()}&exclude_aa=true&live=${liveParam}`
          );
          return data as SearchResponse;
        },
        enabled: searchTrigger > 0,
      },
      {
        queryKey: ["search-aa", title, author, subtitle, seriesName, seriesIndex, searchTrigger],
        queryFn: async () => {
          const { data } = await api.get(`/search/annas-archive?${searchParams.toString()}`);
          return data as SearchResponse;
        },
        enabled: searchTrigger > 0,
      },
    ],
  });

  const indexerResults = indexersQuery.data?.results ?? [];
  const aaResults = aaQuery.data?.results ?? [];
  const indexerLoading = indexersQuery.isLoading || indexersQuery.isFetching;
  const aaLoading = aaQuery.isLoading || aaQuery.isFetching;
  const error = indexersQuery.error || aaQuery.error;
  const isSearching = searchTrigger > 0 && (indexerLoading || aaLoading);

  const hasSearched = searchTrigger > 0;
  const searchMeta = indexersQuery.data;
  const totalFetched = searchMeta?.totalFetched ?? indexerResults.length;
  const hiddenCount = searchMeta?.hiddenCount ?? 0;
  const matchCounts = searchMeta?.matchCounts;

  const combinedResults = useMemo(
    () => [...indexerResults, ...aaResults],
    [indexerResults, aaResults]
  );

  const relevanceFiltered = useMemo(() => {
    if (showWeakMatches) return combinedResults;

    let indexers = indexerResults.filter((r) => (r.matchScore ?? 0) > 0);
    const strongIndexers = indexers.filter(
      (r) => r.matchTier === "exact" || r.matchTier === "likely"
    );
    if (strongIndexers.length > 0) {
      indexers = strongIndexers;
    } else if (indexers.length === 0 && indexerResults.length > 0) {
      // Backend should drop zero-relevance noise; keep nothing rather than unrelated junk.
      indexers = [];
    }
    return [...indexers, ...aaResults];
  }, [indexerResults, aaResults, showWeakMatches]);

  const weakHiddenCount = useMemo(() => {
    if (showWeakMatches) return 0;
    return indexerResults.filter((r) => r.matchTier === "weak").length;
  }, [indexerResults, showWeakMatches]);

  const { data: streamHistoryData } = useQuery({
    queryKey: ["stream-history-check"],
    queryFn: async () => {
      const { data } = await api.get("/stream/rd/history/check");
      return data as { known: Record<string, { id: number; status: string; hasProgress: boolean }> };
    },
  });

  useEffect(() => {
    if (!indexerLoading) {
      if (hasSearched) {
        if (aaLoading) {
          setSearchPhase("Searching Anna's Archive…");
          setSearchProgress(92);
        } else {
          setSearchPhase("Search complete");
          setSearchProgress(100);
        }
      }
      return;
    }

    const start = Date.now();
    const tick = () => {
      const sec = (Date.now() - start) / 1000;
      if (!liveSearch) {
        if (sec < 2) {
          setSearchPhase("Checking download cache…");
          setSearchProgress(20 + sec * 15);
        } else {
          setSearchPhase("Loading cached results…");
          setSearchProgress(Math.min(85, 50 + sec * 8));
        }
        return;
      }
      if (sec < 4) {
        setSearchPhase("Connecting to Prowlarr…");
        setSearchProgress(8 + sec * 3);
      } else if (sec < 25) {
        setSearchPhase("Searching AudioBook Bay & Knaben…");
        setSearchProgress(Math.min(58, 18 + sec * 1.6));
      } else if (sec < 60) {
        setSearchPhase("Searching ABB & Knaben (extra queries)…");
        setSearchProgress(Math.min(82, 58 + (sec - 25) * 0.55));
      } else {
        setSearchPhase("Still searching — can take up to 90 seconds…");
        setSearchProgress(Math.min(88, 82 + (sec - 60) * 0.12));
      }
    };
    tick();
    const id = setInterval(tick, 400);
    return () => clearInterval(id);
  }, [indexerLoading, hasSearched, aaLoading, liveSearch]);

  useEffect(() => {
    if (hasSearched && !indexerLoading && searchProgress < 90 && searchProgress > 0) {
      setSearchPhase("Checking Real-Debrid cache…");
      setSearchProgress(90);
    }
  }, [hasSearched, indexerLoading, searchProgress]);

  const handleFindDownloads = () => {
    setModalOpen(true);
    setLiveSearch(false);
    setSearchProgress(0);
    setSearchPhase("Checking download cache…");
    setShowWeakMatches(false);
    setSearchTrigger((prev) => prev + 1);
  };

  const handleRefresh = () => {
    setLiveSearch(true);
    setSearchProgress(0);
    setSearchPhase("Searching live indexers…");
    setSearchTrigger((prev) => prev + 1);
  };

  const filteredResults = useMemo(() => {
    if (!relevanceFiltered.length) return [];
    if (mediaFilter === "all") return relevanceFiltered;
    return relevanceFiltered.filter((r) => r.mediaType === mediaFilter);
  }, [relevanceFiltered, mediaFilter]);

  const handleRequest = async (result: SearchResult, index: number, mediaTypeOverride: string) => {
    const isAA = result.source === "annas_archive";
    const link = result.magnetUrl || result.downloadUrl;
    if (!isAA && !link) return;
    if (isAA && !result.aaMd5) return;
    setRequestingIdx(index);
    try {
      const mediaType = mediaTypeOverride || result.mediaType || "unknown";
      await api.post("/requests", {
        title: result.title,
        author: result.author || author || undefined,
        magnet_link: result.magnetUrl || undefined,
        download_url: result.downloadUrl || undefined,
        indexer: result.indexer,
        size_bytes: result.size,
        media_type: mediaType,
        source: result.source || "prowlarr",
        aa_md5: result.aaMd5 || undefined,
        aa_file_extension: isAA ? result.fileExtension || undefined : undefined,
      });
      const dest = mediaType === "ebook" ? "Kavita" : "Audiobookshelf";
      const via = isAA ? " (direct download)" : "";
      toast(`Requested "${result.title}"${via}. It will be added to ${dest}.`, "success");
    } catch (err: any) {
      toast(err.response?.data?.detail || "Failed to create request", "error");
    } finally {
      setRequestingIdx(null);
    }
  };

  const handleStream = useCallback(async (result: SearchResult, index: number) => {
    const link = result.magnetUrl || result.downloadUrl;
    if (!link) return;
    setStreamingIdx(index);
    setStreamProgress({ detail: "Sending to Real-Debrid...", progress: 0 });

    try {
      const { data: startData } = await api.post("/stream/rd/resolve", {
        magnet_link: result.magnetUrl || undefined,
        download_url: result.downloadUrl || undefined,
        title: result.title,
        author: author || "",
        cover_url: coverUrl || "",
        indexer: result.indexer || "",
      });

      const taskId = startData.taskId;
      if (!taskId) {
        toast("Failed to start stream resolution", "error");
        return;
      }

      pollRef.current = true;
      while (pollRef.current) {
        await new Promise((r) => setTimeout(r, 2000));
        try {
          const { data: status } = await api.get(`/stream/rd/status/${taskId}`);

          if (status.status === "ready") {
            if (!status.tracks || status.tracks.length === 0) {
              toast("No audio files found in this torrent", "error");
            } else {
              playRD(status.tracks, result.title, author, coverUrl, status.streamHistoryId, {
                startAt: status.progressSeconds || 0,
                trackIndex: status.currentTrackIndex || 0,
                trackPositionSeconds: status.trackPositionSeconds || 0,
              });
              toast(`Now streaming "${result.title}" from Real-Debrid`, "success");
              queryClient.invalidateQueries({ queryKey: ["stream-history-check"] });
              queryClient.invalidateQueries({ queryKey: ["stream-history"] });
            }
            break;
          } else if (status.status === "error") {
            toast(status.error || "Stream resolution failed", "error");
            break;
          } else {
            setStreamProgress({ detail: status.detail || "Processing...", progress: status.progress || 0 });
          }
        } catch {
          toast("Lost connection to stream resolver", "error");
          break;
        }
      }
    } catch (err: any) {
      const detail = err.response?.data?.detail || "Failed to start stream";
      toast(detail, "error");
    } finally {
      setStreamingIdx(null);
      setStreamProgress(null);
      pollRef.current = false;
    }
  }, [author, coverUrl, playRD, toast, queryClient]);

  return (
    <>
      <button
        type="button"
        onClick={handleFindDownloads}
        className="w-full flex items-center justify-center gap-2 px-6 py-3.5 bg-brand-600 text-white font-semibold rounded-xl hover:bg-brand-500 transition-colors"
      >
        <Search size={18} />
        {hasSearched && combinedResults.length > 0
          ? `View Downloads (${combinedResults.length})`
          : "Find Downloads"}
      </button>

      <Modal
        show={modalOpen}
        onClose={() => setModalOpen(false)}
        title={`Downloads — ${title}`}
        size="lg"
      >
        <div className="flex items-center justify-between -mt-1 mb-3 gap-3">
          <p className="text-xs text-gray-500">
            {liveSearch
              ? "Live search — querying Prowlarr indexers"
              : "Cached results — instant from our indexer database"}
          </p>
          <button
            type="button"
            onClick={handleRefresh}
            disabled={isSearching}
            className="text-sm text-brand-400 hover:text-brand-300 transition-colors disabled:opacity-50 shrink-0"
          >
            Refresh from indexers
          </button>
        </div>

        {searchHint && hasSearched && (
          <p className="text-xs text-gray-500 mb-3">{searchHint}</p>
        )}

        {isSearching && (
          <SearchProgressBar phase={searchPhase} progress={searchProgress} />
        )}

        {error && (
          <div className="p-4 bg-red-900/30 text-red-400 rounded-xl text-sm mb-4">
            Search failed. Please try again.
          </div>
        )}

        {hasSearched && !isSearching && combinedResults.length === 0 && !error && (
          <div className="text-center py-12 text-gray-500">
            <p>No downloads found for this book</p>
          </div>
        )}

        {hasSearched && combinedResults.length > 0 && (
          <div className="space-y-3">
            <div className="flex flex-col gap-2">
              <p className="text-sm text-gray-400">
                {liveSearch && totalFetched > 0 && (
                  <span>Searched {totalFetched} torrents from AudioBook Bay & Knaben. </span>
                )}
                {!liveSearch && indexerResults.length > 0 && (
                  <span>Loaded from indexer cache. </span>
                )}
                Showing {filteredResults.length} result{filteredResults.length !== 1 ? "s" : ""}
                {matchCounts && (matchCounts.exact > 0 || matchCounts.likely > 0) && (
                  <span className="text-gray-500">
                    {" "}({matchCounts.exact} best match{matchCounts.exact !== 1 ? "es" : ""}
                    {matchCounts.likely > 0 ? `, ${matchCounts.likely} similar` : ""})
                  </span>
                )}
              </p>
              {(weakHiddenCount > 0 || hiddenCount > 0) && (
                <button
                  type="button"
                  onClick={() => setShowWeakMatches((v) => !v)}
                  className="text-xs text-brand-400 hover:text-brand-300 text-left w-fit"
                >
                  {showWeakMatches
                    ? "Hide weaker matches"
                    : `Show ${weakHiddenCount + hiddenCount} more results (other books / lower match)`}
                </button>
              )}
              <p className="text-xs text-gray-500">Sorted by match to this book, then seeders</p>
            </div>
            <div className="flex flex-col sm:flex-row sm:items-center justify-end gap-3">
              <span className="text-xs text-gray-500 sm:hidden">Filter by type</span>
              <div className="flex gap-1 bg-gray-900 rounded-lg p-1 self-start sm:self-auto">
                {([
                  { id: "all" as MediaFilter, label: "All", icon: null },
                  { id: "audiobook" as MediaFilter, label: "Audiobooks", icon: Headphones },
                  { id: "ebook" as MediaFilter, label: "eBooks", icon: BookText },
                ]).map(({ id, label, icon: Icon }) => (
                  <button
                    key={id}
                    onClick={() => setMediaFilter(id)}
                    className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                      mediaFilter === id
                        ? "bg-gray-800 text-white shadow-sm"
                        : "text-gray-400 hover:text-gray-200"
                    }`}
                  >
                    {Icon && <Icon size={13} />}
                    {label}
                  </button>
                ))}
              </div>
            </div>
            {filteredResults.length === 0 ? (
              <div className="text-center py-8 text-gray-500">
                <p>No {mediaFilter} results found</p>
              </div>
            ) : (
              filteredResults.map((result) => {
                const origIdx = combinedResults.indexOf(result);
                const known = streamHistoryData?.known || {};
                const histEntry = known[result.magnetUrl || ""] || known[result.downloadUrl || ""] || null;
                return (
                  <ResultCard
                    key={`${result.title}-${result.indexer}-${result.size}-${origIdx}`}
                    result={result}
                    onRequest={(r, typeOverride) => handleRequest(r, origIdx, typeOverride)}
                    onStream={(r) => handleStream(r, origIdx)}
                    requesting={requestingIdx === origIdx}
                    streaming={streamingIdx === origIdx}
                    streamProgress={streamingIdx === origIdx ? streamProgress : null}
                    streamHistory={histEntry}
                  />
                );
              })
            )}
          </div>
        )}

        {hasSearched && isSearching && combinedResults.length === 0 && !error && (
          <p className="text-xs text-gray-500 text-center mt-2">
            Prowlarr queries each indexer in turn. Anna&apos;s Archive runs in parallel afterward.
          </p>
        )}
      </Modal>
    </>
  );
}
