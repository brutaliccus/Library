import { useState, useMemo, useCallback, useRef, useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import api from "../api/client";
import { useToast } from "../contexts/ToastContext";
import { usePlayer } from "../contexts/PlayerContext";
import ResultCard from "./ResultCard";
import Modal from "./Modal";
import { Search, Headphones, BookText } from "lucide-react";
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

interface SearchResponse {
  results: SearchResult[];
  count: number;
  totalFetched?: number;
  hiddenCount?: number;
  matchCounts?: { exact: number; likely: number; weak: number };
}

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
  const [cacheTrigger, setCacheTrigger] = useState(0);
  const [aaTrigger, setAaTrigger] = useState(0);
  const [requestingIdx, setRequestingIdx] = useState<number | null>(null);
  const [streamingIdx, setStreamingIdx] = useState<number | null>(null);
  const [streamProgress, setStreamProgress] = useState<{ detail: string; progress: number } | null>(null);
  const [mediaFilter, setMediaFilter] = useState<MediaFilter>("all");
  const [resultFilter, setResultFilter] = useState("");
  const [abbQuery, setAbbQuery] = useState("");
  const [searchPhase, setSearchPhase] = useState("");
  const [searchProgress, setSearchProgress] = useState(0);
  const [showWeakMatches, setShowWeakMatches] = useState(false);
  const [liveSearch, setLiveSearch] = useState(false);
  const [liveLoading, setLiveLoading] = useState(false);
  const [liveError, setLiveError] = useState(false);
  const [liveData, setLiveData] = useState<SearchResponse | null>(null);
  const pollRef = useRef(false);
  const liveAbortRef = useRef<AbortController | null>(null);
  const liveResultCountRef = useRef(0);
  const liveTimeoutRef = useRef<number | null>(null);

  const armLiveTimeout = useCallback((ac: AbortController, hasResults: boolean) => {
    if (liveTimeoutRef.current) window.clearTimeout(liveTimeoutRef.current);
    const ms = hasResults ? 90_000 : 180_000;
    liveTimeoutRef.current = window.setTimeout(() => ac.abort(), ms);
  }, []);

  const defaultAbbQuery = useMemo(() => {
    if (title && seriesIndex) return `${title} book ${seriesIndex}`;
    if (seriesName && seriesIndex) return `${seriesName} book ${seriesIndex}`;
    return title;
  }, [title, seriesName, seriesIndex]);

  const searchParams = useMemo(() => {
    const p = new URLSearchParams();
    p.set("title", title);
    if (author) p.set("author", author);
    if (subtitle) p.set("subtitle", subtitle);
    if (seriesName) p.set("series_name", seriesName);
    if (seriesIndex) p.set("series_index", seriesIndex);
    const trimmedAbb = abbQuery.trim();
    if (trimmedAbb) p.set("abb_query", trimmedAbb);
    return p;
  }, [title, author, subtitle, seriesName, seriesIndex, abbQuery]);

  const searchHint = seriesIndex
    ? `Searching for Book ${seriesIndex} in ${seriesName || title}…`
    : null;

  const cacheQuery = useQuery({
    queryKey: ["search-indexers-cache", title, author, subtitle, seriesName, seriesIndex, cacheTrigger],
    queryFn: async ({ signal }) => {
      const { data } = await api.get(
        `/search?${searchParams.toString()}&exclude_aa=true&live=false`,
        { signal, timeout: 45_000 }
      );
      return data as SearchResponse;
    },
    enabled: cacheTrigger > 0 && !liveSearch,
  });

  const aaQuery = useQuery({
    queryKey: ["search-aa", title, author, subtitle, seriesName, seriesIndex, aaTrigger],
    queryFn: async ({ signal }) => {
      const { data } = await api.get(`/search/annas-archive?${searchParams.toString()}`, {
        signal,
        timeout: 45_000,
      });
      return data as SearchResponse;
    },
    enabled: aaTrigger > 0,
  });

  const indexerResults = (liveSearch ? liveData?.results : cacheQuery.data?.results) ?? [];
  const aaResults = aaQuery.data?.results ?? [];
  const indexerLoading = liveSearch
    ? liveLoading
    : cacheQuery.isLoading || cacheQuery.isFetching;
  const aaLoading = aaQuery.isLoading || aaQuery.isFetching;
  // AA failures must not hard-fail the whole Find Downloads panel.
  const error = liveSearch
    ? (liveError && indexerResults.length === 0 ? new Error("Live search failed") : null)
    : (cacheQuery.error && indexerResults.length === 0 ? cacheQuery.error : null);
  const isSearching = (cacheTrigger > 0 || liveSearch || aaTrigger > 0) && (indexerLoading || aaLoading);

  const hasSearched = cacheTrigger > 0 || liveSearch || aaTrigger > 0;
  const searchMeta = liveSearch ? liveData : cacheQuery.data;
  const totalFetched = searchMeta?.totalFetched ?? indexerResults.length;
  const hiddenCount = searchMeta?.hiddenCount ?? 0;
  const matchCounts = searchMeta?.matchCounts;

  const combinedResults = useMemo(
    () => [...indexerResults, ...aaResults],
    [indexerResults, aaResults]
  );

  const relevanceFiltered = useMemo(() => {
    if (showWeakMatches) return combinedResults;

    const isAbb = (r: SearchResult) => /audiobook\s*bay/i.test(r.indexer || "");
    const isStrong = (r: SearchResult) =>
      r.matchTier === "exact" || r.matchTier === "likely";

    let indexers = indexerResults.filter((r) => (r.matchScore ?? 0) > 0);
    const strongIndexers = indexers.filter(isStrong);
    if (strongIndexers.length > 0) {
      // Keep non-ABB hits (e.g. Knaben) even when weak — ABB strong matches used to hide them.
      const weakNonAbb = indexers.filter(
        (r) => r.matchTier === "weak" && !isAbb(r)
      );
      indexers = [...strongIndexers, ...weakNonAbb];
    } else {
      // No strong matches — hide weak noise by default (toggle reveals them)
      indexers = [];
    }

    let aa = aaResults.filter((r) => (r.matchScore ?? 0) > 0);
    const strongAa = aa.filter(isStrong);
    aa = strongAa.length > 0 ? strongAa : [];

    return [...indexers, ...aa];
  }, [indexerResults, aaResults, showWeakMatches, combinedResults]);

  const weakHiddenCount = useMemo(() => {
    if (showWeakMatches) return 0;
    return combinedResults.filter((r) => r.matchTier === "weak").length;
  }, [combinedResults, showWeakMatches]);

  const { data: streamHistoryData } = useQuery({
    queryKey: ["stream-history-check"],
    queryFn: async () => {
      const { data } = await api.get("/stream/rd/history/check");
      return data as { known: Record<string, { id: number; status: string; hasProgress: boolean }> };
    },
  });

  useEffect(() => {
    if (liveSearch) return;
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
      if (sec < 2) {
        setSearchPhase("Checking download cache…");
        setSearchProgress(20 + sec * 15);
      } else if (sec < 20) {
        setSearchPhase("Loading cached results…");
        setSearchProgress(Math.min(80, 50 + sec * 2));
      } else {
        setSearchPhase("Cache lookup is slow — still waiting…");
        setSearchProgress(Math.min(88, 80 + (sec - 20) * 0.3));
      }
    };
    tick();
    const id = setInterval(tick, 400);
    return () => clearInterval(id);
  }, [indexerLoading, hasSearched, aaLoading, liveSearch]);

  useEffect(() => {
    if (liveSearch) return;
    if (hasSearched && !indexerLoading && searchProgress < 90 && searchProgress > 0) {
      setSearchPhase("Checking Real-Debrid cache…");
      setSearchProgress(90);
    }
  }, [hasSearched, indexerLoading, searchProgress, liveSearch]);

  const runLiveStream = useCallback(async () => {
    liveAbortRef.current?.abort();
    const ac = new AbortController();
    liveAbortRef.current = ac;

    setLiveSearch(true);
    setLiveLoading(true);
    setLiveError(false);
    liveResultCountRef.current = 0;
    setLiveData({
      results: [],
      count: 0,
      totalFetched: 0,
      hiddenCount: 0,
      matchCounts: { exact: 0, likely: 0, weak: 0 },
    });
    setSearchPhase("Starting live search…");
    setSearchProgress(5);
    setResultFilter("");
    setAaTrigger((n) => n + 1);

    const token = localStorage.getItem("access_token") || "";
    const url = `/api/search/live-stream?${searchParams.toString()}`;

    armLiveTimeout(ac, false);

    try {
      const resp = await fetch(url, {
        signal: ac.signal,
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!resp.ok || !resp.body) {
        throw new Error(`Live search failed (${resp.status})`);
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed) continue;
          let msg: Record<string, unknown>;
          try {
            msg = JSON.parse(trimmed) as Record<string, unknown>;
          } catch {
            continue;
          }
          const type = msg.type as string;
          if (type === "status") {
            const phase = String(msg.phase || "Searching…");
            setSearchPhase(phase);
            const page = Number(msg.page || 0);
            const pages = Number(msg.pages || 0);
            if (pages > 0 && page > 0) {
              setSearchProgress(Math.min(88, 8 + (page / pages) * 70));
            }
          } else if (type === "batch") {
            const phase = String(msg.phase || "Loading results…");
            setSearchPhase(phase);
            const page = Number(msg.page || 0);
            const pages = Number(msg.pages || 0);
            if (pages > 0 && page > 0) {
              setSearchProgress(Math.min(90, 10 + (page / pages) * 75));
            } else if (msg.source === "knaben") {
              setSearchProgress((p) => Math.max(p, 85));
            }
            const results = (msg.results as SearchResult[]) || [];
            liveResultCountRef.current = results.length;
            armLiveTimeout(ac, results.length > 0);
            setLiveData({
              results,
              count: results.length,
              totalFetched: Number(msg.totalSoFar || results.length),
              hiddenCount: Number(msg.hiddenCount || 0),
              matchCounts: (msg.matchCounts as SearchResponse["matchCounts"]) || {
                exact: 0,
                likely: 0,
                weak: 0,
              },
            });
          } else if (type === "done") {
            const results = (msg.results as SearchResult[]) || [];
            liveResultCountRef.current = results.length;
            armLiveTimeout(ac, results.length > 0);
            setLiveData({
              results,
              count: Number(msg.count || results.length),
              totalFetched: Number(msg.totalFetched || results.length),
              hiddenCount: Number(msg.hiddenCount || 0),
              matchCounts: (msg.matchCounts as SearchResponse["matchCounts"]) || {
                exact: 0,
                likely: 0,
                weak: 0,
              },
            });
            setSearchPhase("Search complete");
            setSearchProgress(100);
          } else if (type === "error") {
            throw new Error(String(msg.detail || "Search failed"));
          }
        }
      }
    } catch (err) {
      if ((err as Error)?.name === "AbortError") {
        if (liveResultCountRef.current > 0) {
          toast("Search timed out — showing partial results", "info");
        } else {
          setLiveError(true);
          toast("Live indexer search timed out — try Find Downloads (cache) first", "error");
        }
        return;
      }
      console.warn("[download-panel] live stream failed", err);
      if (liveResultCountRef.current > 0) {
        toast("Live search ended early — showing partial results", "info");
      } else {
        setLiveError(true);
        toast("Live indexer search failed — try again", "error");
      }
    } finally {
      if (liveTimeoutRef.current) window.clearTimeout(liveTimeoutRef.current);
      if (liveAbortRef.current === ac) {
        setLiveLoading(false);
        setSearchProgress((p) => (p < 100 ? 100 : p));
      }
    }
  }, [searchParams, toast, armLiveTimeout]);

  useEffect(() => {
    return () => {
      liveAbortRef.current?.abort();
    };
  }, []);

  const handleFindDownloads = () => {
    liveAbortRef.current?.abort();
    setModalOpen(true);
    setLiveSearch(false);
    setLiveLoading(false);
    setLiveError(false);
    setLiveData(null);
    setSearchProgress(0);
    setSearchPhase("Checking download cache…");
    setShowWeakMatches(false);
    setResultFilter("");
    setCacheTrigger((prev) => prev + 1);
    setAaTrigger((prev) => prev + 1);
  };

  const handleRefresh = () => {
    void runLiveStream();
  };

  const filteredResults = useMemo(() => {
    if (!relevanceFiltered.length) return [];
    let list =
      mediaFilter === "all"
        ? relevanceFiltered
        : relevanceFiltered.filter((r) => r.mediaType === mediaFilter);
    const q = resultFilter.trim().toLowerCase();
    if (q) {
      const tokens = q.split(/\s+/).filter(Boolean);
      list = list.filter((r) => {
        const hay = `${r.title || ""} ${r.indexer || ""} ${r.author || ""} ${r.mediaType || ""}`.toLowerCase();
        return tokens.every((t) => hay.includes(t));
      });
    }
    return list;
  }, [relevanceFiltered, mediaFilter, resultFilter]);

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
        onClose={() => {
          liveAbortRef.current?.abort();
          setModalOpen(false);
        }}
        title={`Downloads — ${title}`}
        size="lg"
      >
        <div className="flex items-center justify-between -mt-1 mb-3 gap-3">
          <p className="text-xs text-gray-500">
            {liveSearch
              ? liveLoading
                ? "Live search — Jackett (~2 ABB pages) & Knaben"
                : "Live search complete"
              : "Cached results — instant from our indexer database"}
          </p>
          <button
            type="button"
            onClick={handleRefresh}
            disabled={liveLoading}
            className="text-sm text-brand-400 hover:text-brand-300 transition-colors disabled:opacity-50 shrink-0"
          >
            Refresh from indexers
          </button>
        </div>

        <div className="mb-4 space-y-1.5">
          <label htmlFor="abb-query" className="text-xs text-gray-400 block">
            AudioBook Bay search terms
            <span className="text-gray-500">
              {" "}
              — Jackett only returns ~2 pages; narrow the query for popular series
            </span>
          </label>
          <input
            id="abb-query"
            type="text"
            value={abbQuery}
            onChange={(e) => setAbbQuery(e.target.value)}
            placeholder={defaultAbbQuery || "Title, book #, narrator, format…"}
            disabled={liveLoading}
            className="w-full px-3 py-2 rounded-lg bg-gray-900 border border-gray-700/60 text-sm text-gray-100 placeholder:text-gray-500 focus:outline-none focus:ring-1 focus:ring-brand-500/60 disabled:opacity-60"
          />
          {!abbQuery.trim() && defaultAbbQuery && (
            <p className="text-xs text-gray-500">
              Using auto query: <span className="text-gray-400">{defaultAbbQuery}</span>
            </p>
          )}
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
                  <span>
                    {liveLoading ? "Found" : "Searched"} {totalFetched} torrents
                    {liveLoading ? " so far" : ""} from AudioBook Bay & Knaben.{" "}
                  </span>
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
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="relative flex-1 min-w-0">
                <Search
                  size={14}
                  className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500 pointer-events-none"
                />
                <input
                  type="search"
                  value={resultFilter}
                  onChange={(e) => setResultFilter(e.target.value)}
                  placeholder="Filter results (narrator, format, book #…)"
                  className="w-full pl-9 pr-3 py-2 rounded-lg bg-gray-900 border border-gray-700/60 text-sm text-gray-100 placeholder:text-gray-500 focus:outline-none focus:ring-1 focus:ring-brand-500/60"
                />
              </div>
              <div className="flex gap-1 bg-gray-900 rounded-lg p-1 self-start sm:self-auto shrink-0">
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
            {resultFilter.trim() && (
              <p className="text-xs text-gray-500">
                {filteredResults.length} match{filteredResults.length !== 1 ? "es" : ""} for “
                {resultFilter.trim()}”
                {filteredResults.length === 0 ? " — try another filter" : ""}
              </p>
            )}
            {filteredResults.length === 0 ? (
              <div className="text-center py-8 text-gray-500">
                <p>
                  {resultFilter.trim()
                    ? "No results match that filter"
                    : mediaFilter === "all"
                      ? "No matching results"
                      : `No ${mediaFilter}s found`}
                </p>
              </div>
            ) : (
              filteredResults.map((result) => {
                const origIdx = combinedResults.indexOf(result);
                const known = streamHistoryData?.known || {};
                const histEntry =
                  known[result.magnetUrl || ""] || known[result.downloadUrl || ""] || null;
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
            {liveSearch
              ? "Querying Jackett & Knaben…"
              : "Loading from indexer cache…"}
          </p>
        )}
      </Modal>
    </>
  );
}
