import { useParams, Link, useLocation, useNavigate } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import api from "../api/client";
import { usePlayer } from "../contexts/PlayerContext";
import { useToast } from "../contexts/ToastContext";
import StarRating from "../components/StarRating";
import DownloadPanel from "../components/DownloadPanel";
import {
  BookOpen, ArrowLeft, Headphones, Loader2, Library, Check, Play, Bell, BellOff,
} from "lucide-react";
import type { BookDetail as BookDetailType } from "../types/book";
import { useState, useCallback, useRef, useMemo } from "react";
import CoverImage from "../components/CoverImage";

interface ABSMatch {
  title: string;
  author: string;
  itemId: string;
  coverUrl: string;
}

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

function formatPublishedDate(raw: string): string {
  const s = raw.trim();
  if (/^\d{4}$/.test(s)) return s;
  const iso = s.match(/^(\d{4})-(\d{2})(?:-(\d{2}))?/);
  if (iso) {
    const year = iso[1];
    const month = parseInt(iso[2], 10);
    const day = iso[3] ? parseInt(iso[3], 10) : null;
    if (day && month >= 1 && month <= 12) {
      return `${day} ${MONTHS[month - 1]} ${year}`;
    }
    if (month >= 1 && month <= 12) return `${MONTHS[month - 1]} ${year}`;
    return year;
  }
  return s;
}

function formatBookDetailsLine(book: BookDetailType): string | null {
  const parts: string[] = [];
  if (book.pageCount > 0) {
    parts.push(`${book.pageCount.toLocaleString()} Pages`);
  }
  if (book.publishedDate) {
    parts.push(`Published ${formatPublishedDate(book.publishedDate)}`);
  }
  if (book.publisher) {
    parts.push(book.publisher);
  }
  return parts.length > 0 ? parts.join(", ") : null;
}

export default function BookDetailPage() {
  // Splat route /book/* — OL ids contain slashes (OL:/works/OL…W); :volumeId truncates them.
  const params = useParams();
  const rawVolumeId = params["*"] ?? params.volumeId;
  const volumeId = rawVolumeId
    ? (() => {
        try {
          return decodeURIComponent(rawVolumeId);
        } catch {
          return rawVolumeId;
        }
      })()
    : undefined;
  const location = useLocation();
  const navigate = useNavigate();
  const { playABS, playRD } = usePlayer();
  const ebookChapterId = (location.state as { ebookChapterId?: number })?.ebookChapterId;
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [absLoading, setAbsLoading] = useState(false);
  const [smartStreamLoading, setSmartStreamLoading] = useState(false);
  const [smartStreamDetail, setSmartStreamDetail] = useState("");
  const pollRef = useRef(false);

  const { data: book, isLoading, error } = useQuery({
    queryKey: ["book-detail", volumeId],
    queryFn: async () => {
      const { data } = await api.get(`/books/${encodeURIComponent(volumeId!)}`);
      return data as BookDetailType;
    },
    enabled: !!volumeId,
  });

  const { data: grRating } = useQuery({
    queryKey: ["book-rating", volumeId],
    queryFn: async () => {
      const { data } = await api.get(`/books/rating/${encodeURIComponent(volumeId!)}`);
      return data as {
        goodreadsRating: number;
        goodreadsCount: number;
        goodreadsReviewCount: number;
        source?: string;
      };
    },
    enabled: !!volumeId,
    staleTime: 60 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    refetchOnWindowFocus: false,
  });

  const { data: seriesData, isLoading: seriesLoading } = useQuery({
    queryKey: ["book-series", volumeId],
    queryFn: async () => {
      const { data } = await api.get(`/books/series/${encodeURIComponent(volumeId!)}`);
      return data as {
        seriesName: string | null;
        books: Array<{ id: string; title: string; subtitle: string; coverUrl: string; authors: string[]; sequence: string; publishedDate: string }>;
        currentBookIndex: number;
      };
    },
    enabled: !!volumeId && !!book,
    staleTime: 30 * 60 * 1000,
    refetchOnWindowFocus: false,
  });

  const { data: absData } = useQuery({
    queryKey: ["abs-search", book?.title],
    queryFn: async () => {
      const { data } = await api.get(`/stream/abs/search?q=${encodeURIComponent(book!.title)}`);
      return data as { items: ABSMatch[] };
    },
    enabled: !!book?.title,
  });

  const { data: libraryCheck } = useQuery({
    queryKey: ["library-check", volumeId],
    queryFn: async () => {
      const { data } = await api.get(`/library/check/${encodeURIComponent(volumeId!)}`);
      return data as { inLibrary: boolean; item: any };
    },
    enabled: !!volumeId,
  });

  const catalogSeriesName = book?.seriesName || seriesData?.seriesName || undefined;

  const seriesIndex = useMemo(() => {
    if (book?.seriesBookNumber) {
      return String(book.seriesBookNumber).replace(/^#/, "").trim();
    }
    if (!seriesData || seriesData.currentBookIndex < 0) return undefined;
    const cur = seriesData.books[seriesData.currentBookIndex];
    if (cur?.sequence) return String(cur.sequence);
    return String(seriesData.currentBookIndex + 1);
  }, [book?.seriesBookNumber, seriesData]);

  // Series strip often has Hardcover art when the detail payload has none.
  const seriesCoverUrl = useMemo(() => {
    const books = seriesData?.books;
    if (!books?.length) return "";
    const idx = seriesData?.currentBookIndex ?? -1;
    if (idx >= 0 && books[idx]?.coverUrl) return books[idx].coverUrl;
    const byId = books.find((b) => b.id === volumeId && b.coverUrl);
    if (byId) return byId.coverUrl;
    const titleKey = (book?.title || "").trim().toLowerCase();
    if (!titleKey) return "";
    const byTitle = books.find(
      (b) => b.coverUrl && b.title.trim().toLowerCase() === titleKey
    );
    return byTitle?.coverUrl || "";
  }, [seriesData, volumeId, book?.title]);

  const { data: ebookMatch } = useQuery({
    queryKey: [
      "ebook-match",
      book?.title,
      book?.authors?.join(","),
      catalogSeriesName,
      seriesIndex,
    ],
    queryFn: async () => {
      const params = new URLSearchParams({ title: book!.title });
      const authorStr = book!.authors?.join(", ") || "";
      if (authorStr) params.set("author", authorStr);
      if (catalogSeriesName) params.set("seriesName", catalogSeriesName);
      if (seriesIndex) params.set("seriesIndex", seriesIndex);
      const { data } = await api.get(`/library/ebook-match?${params}`);
      return data as { chapterId: number | null; title?: string; seriesId?: number };
    },
    enabled: !!book?.title && !ebookChapterId,
  });

  const { data: globalLibCheck } = useQuery({
    queryKey: ["global-lib-check", book?.title],
    queryFn: async () => {
      const author = book!.authors?.join(", ") || "";
      const { data } = await api.get(`/library/in-library-global?title=${encodeURIComponent(book!.title)}&author=${encodeURIComponent(author)}`);
      return data as { inLibrary: boolean };
    },
    enabled: !!book?.title,
    staleTime: 5 * 60 * 1000,
  });

  const { data: availability } = useQuery({
    queryKey: ["book-availability", volumeId],
    queryFn: async () => {
      const { data } = await api.get(`/books/availability/${encodeURIComponent(volumeId!)}`);
      return data as { available: boolean; matchCount?: number };
    },
    enabled: !!volumeId,
    staleTime: 60 * 1000,
  });

  const { data: alertStatus } = useQuery({
    queryKey: ["availability-alert", volumeId],
    queryFn: async () => {
      const { data } = await api.get(
        `/books/availability-alerts/${encodeURIComponent(volumeId!)}`
      );
      return data as { watching: boolean };
    },
    enabled: !!volumeId && availability?.available === false,
    staleTime: 30 * 1000,
  });

  const canRead = !!ebookChapterId || !!ebookMatch?.chapterId;
  const readChapterId = ebookChapterId ?? ebookMatch?.chapterId ?? 0;

  const notifyMutation = useMutation({
    mutationFn: async (watch: boolean) => {
      if (watch) {
        const { data } = await api.post("/books/availability-alerts", {
          volumeId,
          title: book?.title || "",
          author: book?.authors?.join(", ") || "",
          coverUrl: book?.coverUrlLarge || book?.coverUrl || "",
        });
        return data as { watching: boolean; alreadyAvailable?: boolean; message?: string };
      }
      const { data } = await api.delete(
        `/books/availability-alerts/${encodeURIComponent(volumeId!)}`
      );
      return data as { watching: boolean };
    },
    onSuccess: (data: {
      watching: boolean;
      alreadyAvailable?: boolean;
      message?: string;
    }) => {
      queryClient.invalidateQueries({ queryKey: ["availability-alert", volumeId] });
      queryClient.invalidateQueries({ queryKey: ["book-availability", volumeId] });
      if (data.alreadyAvailable) {
        toast(data.message || "Already available to download", "success");
        return;
      }
      toast(
        data.watching
          ? "We'll notify you when this is in the cache"
          : "Notification cancelled",
        "success"
      );
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Could not update notification", "error");
    },
  });

  const addToLibMutation = useMutation({
    mutationFn: async () => {
      const cover = book?.coverUrlLarge || book?.coverUrl || "";
      await api.post("/library", {
        google_volume_id: volumeId,
        title: book?.title || "",
        author: book?.authors?.join(", ") || "",
        cover_url: cover,
        genre: book?.mainCategory || (book?.categories?.[0]) || "",
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["library-check", volumeId] });
      queryClient.invalidateQueries({ queryKey: ["streaming-library"] });
      toast("Added to your library!", "success");
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Failed to add to library", "error");
    },
  });

  const absMatch = absData?.items?.find((item) => {
    if (!book?.title) return false;
    const a = book.title.toLowerCase().replace(/[^a-z0-9\s]/g, "").trim();
    const b = item.title.toLowerCase().replace(/[^a-z0-9\s]/g, "").trim();
    return a === b || a.includes(b) || b.includes(a);
  });

  const handleListen = async (itemId: string) => {
    setAbsLoading(true);
    try {
      await playABS(itemId);
    } catch (err) {
      const msg =
        err instanceof Error && err.message.startsWith("Offline")
          ? err.message
          : "Failed to start playback";
      toast(msg, "error");
    } finally {
      setAbsLoading(false);
    }
  };

  const handleSmartStream = useCallback(async () => {
    if (!book) return;
    setSmartStreamLoading(true);
    setSmartStreamDetail("Searching for streams...");

    const coverUrl = book.coverUrlLarge || book.coverUrl || "";
    const authorStr = book.authors?.join(", ") || "";

    try {
      const { data: startData } = await api.post("/stream/rd/smart-stream", {
        title: book.title,
        author: authorStr,
        cover_url: coverUrl,
        subtitle: book.subtitle || "",
        series_name: catalogSeriesName || "",
        series_index: seriesIndex || "",
      });
      const taskId = startData.taskId;
      if (!taskId) {
        toast("Failed to start stream search", "error");
        return;
      }

      pollRef.current = true;
      while (pollRef.current) {
        await new Promise((r) => setTimeout(r, 2000));
        try {
          const { data: status } = await api.get(`/stream/rd/status/${taskId}`);
          if (status.status === "ready" && status.tracks?.length > 0) {
            playRD(
              status.tracks,
              book.title,
              authorStr,
              coverUrl,
              status.streamHistoryId ?? undefined,
              {
                startAt: status.progressSeconds || 0,
                trackIndex: status.currentTrackIndex || 0,
                trackPositionSeconds: status.trackPositionSeconds || 0,
              }
            );
            toast(`Now streaming "${book.title}"`, "success");
            if (status.streamHistoryId) {
              queryClient.invalidateQueries({ queryKey: ["stream-history"] });
            }
            break;
          } else if (status.status === "error") {
            toast(status.error || "Could not find a stream", "error");
            break;
          } else {
            setSmartStreamDetail(status.detail || "Processing...");
          }
        } catch {
          toast("Lost connection to stream resolver", "error");
          break;
        }
      }
    } catch (err: any) {
      toast(err.response?.data?.detail || "Stream search failed", "error");
    } finally {
      setSmartStreamLoading(false);
      setSmartStreamDetail("");
      pollRef.current = false;
    }
  }, [book, catalogSeriesName, seriesIndex, playRD, toast, queryClient]);

  if (isLoading) {
    return (
      <div className="max-w-5xl mx-auto px-4 py-8">
        <div className="animate-pulse">
          <div className="h-6 w-24 bg-gray-800 rounded mb-8" />
          <div className="flex flex-col md:flex-row gap-8">
            <div className="w-28 md:w-64 shrink-0 aspect-[2/3] bg-gray-800 rounded-xl" />
            <div className="flex-1 space-y-4">
              <div className="h-8 bg-gray-800 rounded w-3/4" />
              <div className="h-5 bg-gray-800 rounded w-1/2" />
              <div className="h-32 bg-gray-800 rounded w-full mt-6" />
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (error || !book) {
    return (
      <div className="max-w-5xl mx-auto px-4 py-16 text-center">
        <p className="text-gray-400">Book not found</p>
        <Link to="/" className="text-brand-400 hover:text-brand-300 mt-4 inline-block">
          Go back home
        </Link>
      </div>
    );
  }

  const coverCandidates = [
    book.coverUrlLarge,
    book.coverUrl,
    seriesCoverUrl,
  ].filter((u, i, arr): u is string => !!u && arr.indexOf(u) === i);
  const coverUrl = coverCandidates[0] || "";
  const authorStr = book.authors.join(", ");
  const detailsLine = formatBookDetailsLine(book);

  const coverPlaceholder = (className: string) => (
    <div className={`${className} aspect-[2/3] bg-gray-800 flex items-center justify-center text-gray-700`}>
      <BookOpen size={48} />
    </div>
  );

  const renderCover = (className: string) =>
    coverUrl ? (
      <CoverImage
        src={coverCandidates[0]}
        fallbackSrc={coverCandidates.slice(1)}
        alt={book.title}
        className={`${className} aspect-[2/3] object-cover`}
        fallback={coverPlaceholder(className)}
      />
    ) : (
      coverPlaceholder(className)
    );

  const renderActions = (compact: boolean) => {
    const base = compact
      ? "inline-flex items-center justify-center gap-1 px-2 py-2.5 text-xs font-medium rounded-lg w-full min-h-[2.75rem]"
      : "inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg";

    return (
      <>
        {absMatch && (
          <button
            onClick={() => handleListen(absMatch.itemId)}
            disabled={absLoading}
            className={`${base} bg-emerald-600 text-white hover:bg-emerald-500 transition-colors disabled:opacity-50`}
          >
            {absLoading ? <Loader2 size={compact ? 14 : 16} className="animate-spin" /> : <Headphones size={compact ? 14 : 16} />}
            Listen
          </button>
        )}
        {canRead && (
          <button
            onClick={() => navigate(`/read/${readChapterId}`)}
            className={`${base} bg-amber-600 text-white hover:bg-amber-500 transition-colors`}
          >
            <BookOpen size={compact ? 14 : 16} />
            Read
          </button>
        )}
        <button
          onClick={handleSmartStream}
          disabled={smartStreamLoading}
          className={`${base} bg-purple-600 text-white hover:bg-purple-500 transition-colors disabled:opacity-50`}
        >
          {smartStreamLoading ? <Loader2 size={compact ? 14 : 16} className="animate-spin" /> : <Play size={compact ? 14 : 16} />}
          Stream
        </button>
        {libraryCheck?.inLibrary ? (
          <span
            className={`${base} bg-gray-800 text-gray-300 border border-gray-700 justify-center`}
          >
            <Check size={compact ? 14 : 16} className="text-brand-400 shrink-0" />
            {compact ? "Saved" : "In Personal Collection"}
          </span>
        ) : (
          <button
            onClick={() => addToLibMutation.mutate()}
            disabled={addToLibMutation.isPending}
            className={`${base} bg-brand-600 text-white hover:bg-brand-500 transition-colors disabled:opacity-50`}
          >
            {addToLibMutation.isPending ? (
              <Loader2 size={compact ? 14 : 16} className="animate-spin" />
            ) : (
              <Library size={compact ? 14 : 16} />
            )}
            {compact ? "Add" : "+ to Personal Collection"}
          </button>
        )}
        {availability?.available === false && (
          <button
            onClick={() => notifyMutation.mutate(!alertStatus?.watching)}
            disabled={notifyMutation.isPending}
            className={`${base} ${
              alertStatus?.watching
                ? "bg-amber-900/40 text-amber-200 border border-amber-700/50 hover:bg-amber-900/60"
                : "bg-gray-800 text-gray-200 border border-gray-700 hover:bg-gray-700 hover:border-gray-600"
            } transition-colors disabled:opacity-50`}
          >
            {notifyMutation.isPending ? (
              <Loader2 size={compact ? 14 : 16} className="animate-spin" />
            ) : alertStatus?.watching ? (
              <BellOff size={compact ? 14 : 16} />
            ) : (
              <Bell size={compact ? 14 : 16} />
            )}
            {compact
              ? alertStatus?.watching
                ? "Watching"
                : "Notify"
              : alertStatus?.watching
                ? "Cancel notify"
                : "Notify me when available"}
          </button>
        )}
      </>
    );
  };

  const titleBlock = (
    <>
      <h1 className="text-2xl sm:text-3xl font-bold text-gray-100 leading-tight">
        {book.title}
      </h1>
      {book.subtitle && (
        <p className="text-base sm:text-lg text-gray-400 mt-1">{book.subtitle}</p>
      )}
      {authorStr && (
        <p className="text-gray-300 mt-2 sm:mt-3">
          by <span className="text-gray-100 font-medium">{authorStr}</span>
        </p>
      )}
      <div className="mt-2 sm:mt-3 flex flex-col gap-1">
        {grRating && grRating.goodreadsRating > 0 ? (
          <div className="flex items-center gap-2">
            <StarRating rating={grRating.goodreadsRating} count={grRating.goodreadsCount} size={16} />
            <span className="text-[11px] text-gray-500 font-medium">
              {grRating.source === "goodreads" ? "Goodreads" : "Hardcover"}
            </span>
          </div>
        ) : book.averageRating > 0 ? (
          <StarRating rating={book.averageRating} count={book.ratingsCount} size={16} />
        ) : null}
        {grRating && grRating.goodreadsReviewCount > 0 && (
          <p className="text-[11px] text-gray-500">
            {grRating.goodreadsReviewCount.toLocaleString()} reviews
            {grRating.source === "goodreads" ? " on Goodreads" : " on Hardcover"}
          </p>
        )}
      </div>
    </>
  );

  const noticesBlock = (
    <>
      {(globalLibCheck?.inLibrary || !!absMatch || !!ebookMatch?.chapterId) &&
        !libraryCheck?.inLibrary && (
        <div className="flex items-center gap-2 mt-4 px-3 py-2 bg-emerald-900/20 border border-emerald-800/30 rounded-lg">
          <Check size={14} className="text-emerald-400 shrink-0" />
          <p className="text-xs text-emerald-300">
            This book is already in the library
            {absMatch ? " (audiobook)" : ebookMatch?.chapterId ? " (ebook)" : ""}.
          </p>
        </div>
      )}
      {smartStreamLoading && smartStreamDetail && (
        <p className="mt-2 text-xs text-purple-400 flex items-center gap-1.5 animate-pulse">
          <Loader2 size={12} className="animate-spin" />
          {smartStreamDetail}
        </p>
      )}
      {!absMatch && absData && !smartStreamLoading && !globalLibCheck?.inLibrary && (
        <p className="mt-2 text-xs text-gray-500 flex items-center gap-1.5">
          <Headphones size={13} />
          Not in your Audiobookshelf library
        </p>
      )}
    </>
  );

  return (
    <div className="max-w-5xl mx-auto px-4 py-8">
      <Link
        to={-1 as any}
        className="inline-flex items-center gap-1 text-sm text-gray-400 hover:text-gray-200 transition-colors mb-6"
        onClick={(e) => {
          e.preventDefault();
          window.history.back();
        }}
      >
        <ArrowLeft size={16} />
        Back
      </Link>

      {/* Mobile: cover left, actions 2×2 right */}
      <div className="flex gap-4 mb-5 md:hidden">
        <div className="w-[7.5rem] shrink-0">
          {renderCover("w-full rounded-xl shadow-lg shadow-black/30")}
        </div>
        <div className="grid grid-cols-2 gap-2 flex-1 content-start self-start">
          {renderActions(true)}
        </div>
      </div>

      <div className="flex flex-col md:flex-row gap-8">
        <div className="hidden md:block w-64 shrink-0">
          {renderCover("w-full rounded-xl shadow-2xl shadow-black/40")}
        </div>

        <div className="flex-1 min-w-0">
          <div className="md:block">{titleBlock}</div>

          <div className="hidden md:flex flex-wrap items-center gap-2 mt-4">
            {renderActions(false)}
          </div>

          {noticesBlock}

          <div className="mt-6 mb-6">
            <DownloadPanel
              title={book.title}
              subtitle={book.subtitle}
              author={authorStr}
              coverUrl={coverUrl}
              seriesName={catalogSeriesName}
              seriesIndex={seriesIndex}
              googleVolumeId={volumeId}
            />
          </div>

          {book.description && (
            <div className="mt-2">
              <h2 className="text-lg font-semibold text-gray-100 mb-3">Synopsis</h2>
              <div
                className="text-gray-300 text-sm leading-relaxed prose prose-invert prose-sm max-w-none"
                dangerouslySetInnerHTML={{ __html: book.description }}
              />
              {detailsLine && (
                <p className="mt-4 text-xs italic text-gray-500 leading-relaxed">
                  {detailsLine}
                </p>
              )}
            </div>
          )}

          {!book.description && detailsLine && (
            <p className="mt-2 text-xs italic text-gray-500">{detailsLine}</p>
          )}
        </div>
      </div>

      {seriesLoading && (
        <div className="mt-10">
          <p className="text-sm text-gray-500">Looking up series…</p>
        </div>
      )}

      {!seriesLoading && seriesData?.seriesName && seriesData.books.length > 1 && (
        <div className="mt-10">
          <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
            <h2 className="text-lg font-semibold text-gray-100">
              More in {seriesData.seriesName}
              <span className="text-sm text-gray-500 font-normal ml-2">
                ({seriesData.books.length} books)
              </span>
            </h2>
            <button
              type="button"
              onClick={() => navigate(`/series/${encodeURIComponent(volumeId!)}`)}
              className="shrink-0 px-3 py-1.5 rounded-lg bg-brand-900/40 border border-brand-700/50 text-sm font-medium text-brand-300 hover:bg-brand-900/60"
            >
              More in this series
            </button>
          </div>
          <div className="grid grid-flow-col auto-cols-[20%] sm:auto-cols-[14%] md:auto-cols-[10%] lg:auto-cols-[8%] xl:auto-cols-[6.5%] gap-2 overflow-x-auto pb-2 scroll-smooth scrollbar-hide">
            {seriesData.books.map((sb) => (
              <button
                key={sb.id}
                onClick={() => {
                  if (sb.id !== volumeId) navigate(`/book/${encodeURIComponent(sb.id)}`);
                }}
                className={`group text-left flex flex-col rounded-lg overflow-hidden border transition-all duration-200 hover:-translate-y-0.5 h-full ${
                  sb.id === volumeId
                    ? "border-brand-500 bg-brand-900/20 ring-1 ring-brand-500/30"
                    : "border-gray-800 bg-gray-800/50 hover:border-gray-600"
                }`}
              >
                <div className="relative aspect-[2/3] bg-gray-900 overflow-hidden">
                  {sb.coverUrl ? (
                    <CoverImage src={sb.coverUrl} alt={sb.title} className="w-full h-full object-cover" loading="lazy" />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center text-gray-700">
                      <BookOpen size={16} />
                    </div>
                  )}
                  {sb.sequence && (
                    <span className="absolute top-0.5 left-0.5 px-1 py-0.5 bg-black/70 text-[8px] text-gray-300 rounded font-mono">
                      #{sb.sequence}
                    </span>
                  )}
                </div>
                <div className="p-1.5 flex flex-col gap-0.5 h-12">
                  <h3 className="text-[9px] font-semibold text-gray-100 line-clamp-2 leading-tight">{sb.title}</h3>
                </div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
