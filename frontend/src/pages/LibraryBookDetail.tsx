import { useParams, Link, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import api from "../api/client";
import { libraryQueryKey } from "../utils/libraryQueryKeys";
import { resolveShareOrigin } from "../api/inviteLink";
import { usePlayer } from "../contexts/PlayerContext";
import { useToast } from "../contexts/ToastContext";
import { useAuth } from "../hooks/useAuth";
import {
  BookOpen,
  Check,
  ChevronLeft,
  Clock,
  Download,
  Headphones,
  HelpCircle,
  ListPlus,
  Loader2,
  Mic,
  Share2,
  Store,
  Tags,
  Trash2,
} from "lucide-react";
import CoverImage from "../components/CoverImage";
import SaveOfflineButton from "../components/SaveOfflineButton";
import Modal from "../components/Modal";
import QuickReviewWizard from "../components/admin/QuickReviewWizard";
import { useOnlineStatus } from "../hooks/useOnlineStatus";
import { softRefreshLibraryCollectionQueries } from "../utils/shelfQueryCache";
import {
  getOfflineProgress,
  progressKeyForAbs,
} from "../utils/offlinePlayback";

interface ABSItemDetail {
  itemId: string;
  title: string;
  subtitle: string;
  author: string;
  narrator: string;
  description: string;
  publisher: string;
  publishedYear: string;
  genres: string[];
  series: Array<{ id: string; name: string; sequence: string }>;
  duration: number;
  numTracks: number;
  coverUrl: string;
}

interface LocalSeriesBook {
  itemId: string;
  title: string;
  author?: string;
  coverUrl?: string;
  sequence?: string;
}

interface StoreSeriesBook {
  id: string;
  title: string;
  subtitle?: string;
  coverUrl?: string;
  authors?: string[];
  sequence?: string;
  availability?: {
    inLibrary?: boolean;
    available?: boolean;
    catalogOnly?: boolean;
  };
}

function formatDuration(secs: number): string {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function normTitle(t: string): string {
  return t.trim().toLowerCase().replace(/[^a-z0-9]+/g, " ");
}

function formatSeriesSeq(seq?: string | null): string {
  if (seq == null) return "";
  const cleaned = String(seq).replace(/^#+\s*/, "").trim();
  if (!cleaned) return "";
  // Keep decimal sequences (2.1, 3.2) intact — do not coerce via Number/int.
  if (/^\d+\.\d+$/.test(cleaned)) {
    return cleaned.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  }
  if (/^\d+$/.test(cleaned)) {
    return String(parseInt(cleaned, 10));
  }
  return cleaned;
}

function seqSortKey(seq?: string | null): number {
  const n = parseFloat(formatSeriesSeq(seq) || "NaN");
  return Number.isFinite(n) ? n : 9999;
}

const iconBtn =
  "inline-flex items-center justify-center w-11 h-11 rounded-xl bg-gray-800 text-gray-300 border border-gray-700 hover:bg-gray-700 transition-colors disabled:opacity-50";
const iconBtnDanger =
  "inline-flex items-center justify-center w-11 h-11 rounded-xl bg-red-950/50 text-red-300 border border-red-800/60 hover:bg-red-900/60 hover:text-red-200 transition-colors";
const mobileIconBtn =
  "inline-flex items-center justify-center flex-1 min-w-0 h-12 max-w-[4.5rem] rounded-xl bg-gray-800 text-gray-300 border border-gray-700 hover:bg-gray-700 transition-colors disabled:opacity-50";
const mobileIconBtnDanger =
  "inline-flex items-center justify-center flex-1 min-w-0 h-12 max-w-[4.5rem] rounded-xl bg-red-950/50 text-red-300 border border-red-800/60 hover:bg-red-900/60 hover:text-red-200 transition-colors";
const seriesStripClass =
  "grid grid-flow-col auto-cols-[7.5rem] sm:auto-cols-[8rem] md:auto-cols-[8.5rem] lg:auto-cols-[9rem] gap-3 overflow-x-auto pb-2 scroll-smooth scrollbar-hide";

export default function LibraryBookDetail() {
  const { itemId: rawItemId } = useParams<{ itemId: string }>();
  const itemId = rawItemId ? decodeURIComponent(rawItemId) : undefined;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { playABS, addToUpNext } = usePlayer();
  const { toast } = useToast();
  const { user } = useAuth();
  const online = useOnlineStatus();
  const isAdmin = user?.role === "admin";
  const [playLoading, setPlayLoading] = useState(false);
  const [storeLoading, setStoreLoading] = useState(false);
  const [showDelete, setShowDelete] = useState(false);
  const [showEditMetadata, setShowEditMetadata] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [shareBusy, setShareBusy] = useState(false);

  const { data: item, isLoading, error } = useQuery({
    queryKey: ["abs-item-detail", itemId],
    queryFn: async () => {
      const { data } = await api.get(`/library/abs/item/${encodeURIComponent(itemId!)}`);
      return data as ABSItemDetail;
    },
    enabled: !!itemId,
    staleTime: 5 * 60 * 1000,
  });

  const seriesName = item?.series?.find((s) => s.name)?.name?.trim() || "";

  const { data: localSeriesGroups, isLoading: localSeriesLoading } = useQuery({
    queryKey: libraryQueryKey("abs-series"),
    queryFn: async () => {
      const { data } = await api.get("/library/abs/series");
      return data as {
        series: Array<{
          id: string;
          name: string;
          books: LocalSeriesBook[];
          bookCount: number;
        }>;
      };
    },
    enabled: !!seriesName,
    staleTime: 10 * 60 * 1000,
  });

  const localSeries = useMemo(() => {
    if (!seriesName || !localSeriesGroups?.series?.length) return null;
    const key = seriesName.toLowerCase();
    return (
      localSeriesGroups.series.find((s) => s.name.toLowerCase() === key) ||
      localSeriesGroups.series.find((s) => s.name.toLowerCase().includes(key) || key.includes(s.name.toLowerCase())) ||
      null
    );
  }, [localSeriesGroups, seriesName]);

  const { data: storeMatch } = useQuery({
    queryKey: ["lib-detail-store-match", item?.title, item?.author],
    queryFn: async () => {
      const q = item!.author
        ? `intitle:${JSON.stringify(item!.title)} inauthor:${item!.author}`
        : item!.title;
      const { data } = await api.get(`/books/search?q=${encodeURIComponent(q)}&pageSize=5`);
      const books = (data as { books?: { id: string; title: string }[] })?.books || [];
      if (!books.length) return null;
      const titleLower = item!.title.toLowerCase();
      const match =
        books.find((b) => {
          const bt = b.title.toLowerCase();
          return bt === titleLower || bt.includes(titleLower) || titleLower.includes(bt);
        }) || books[0];
      return match.id as string;
    },
    enabled: !!item?.title && online,
    staleTime: 30 * 60 * 1000,
    retry: 1,
  });

  const { data: storeSeries, isLoading: storeSeriesLoading } = useQuery({
    queryKey: ["book-series", storeMatch],
    queryFn: async () => {
      const { data } = await api.get(`/books/series/${encodeURIComponent(storeMatch!)}`);
      return data as {
        seriesName: string | null;
        books: StoreSeriesBook[];
        currentBookIndex: number;
      };
    },
    enabled: !!storeMatch && online,
    staleTime: 30 * 60 * 1000,
    refetchOnWindowFocus: false,
  });

  const absByTitle = useMemo(() => {
    const map = new Map<string, LocalSeriesBook>();
    for (const b of localSeries?.books || []) {
      if (b.itemId && b.title) map.set(normTitle(b.title), b);
    }
    return map;
  }, [localSeries]);

  const storeSeqByTitle = useMemo(() => {
    const map = new Map<string, string>();
    for (const b of storeSeries?.books || []) {
      const seq = formatSeriesSeq(b.sequence);
      if (seq && b.title) map.set(normTitle(b.title), seq);
    }
    return map;
  }, [storeSeries]);

  const handlePlay = async () => {
    if (!item) return;
    setPlayLoading(true);
    try {
      await playABS(item.itemId);
    } catch (err) {
      const msg =
        err instanceof Error && err.message.startsWith("Offline")
          ? err.message
          : "Failed to start playback";
      toast(msg, "error");
    } finally {
      setPlayLoading(false);
    }
  };

  const canShare = user?.role === "admin" || !!user?.canShareBooks;
  const hasLocalProgress = (() => {
    if (!itemId) return false;
    const p = getOfflineProgress(progressKeyForAbs(itemId));
    return !!p && (p.time > 5 || p.trackIndex > 0 || p.trackLocal > 5);
  })();

  const handleShare = async () => {
    if (!itemId) return;
    setShareBusy(true);
    try {
      const { data } = await api.post("/share", { item_id: itemId });
      const path = (data as { path?: string }).path || `/share/${(data as { token: string }).token}`;
      const serverUrl = ((data as { url?: string }).url || "").trim();
      const origin = resolveShareOrigin();
      const url = serverUrl.startsWith("http")
        ? serverUrl
        : origin
          ? `${origin}${path}`
          : `${window.location.origin}${path}`;
      if (navigator.share) {
        try {
          await navigator.share({
            title: item?.title || "Shared audiobook",
            text: item?.title ? `Listen to ${item.title}` : "Shared audiobook",
            url,
          });
          toast("Share sheet opened", "success");
          return;
        } catch (err) {
          if ((err as { name?: string })?.name === "AbortError") return;
        }
      }
      await navigator.clipboard.writeText(url);
      toast("Share link copied", "success");
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Could not create share link";
      toast(typeof detail === "string" ? detail : "Could not create share link", "error");
    } finally {
      setShareBusy(false);
    }
  };

  const handleAddToUpNext = () => {
    if (!item) return;
    addToUpNext({
      source: "abs",
      id: item.itemId,
      title: item.title,
      author: item.author || "",
      coverUrl: item.coverUrl || "",
    });
    toast(`Added "${item.title}" to Up Next`, "success");
  };

  const handleViewInStore = async () => {
    if (!item) return;
    if (storeMatch) {
      navigate(`/book/${encodeURIComponent(storeMatch)}`);
      return;
    }
    setStoreLoading(true);
    try {
      const q = item.author
        ? `intitle:${JSON.stringify(item.title)} inauthor:${item.author}`
        : item.title;
      const { data } = await api.get(`/books/search?q=${encodeURIComponent(q)}&pageSize=5`);
      const books = (data as { books?: { id: string; title: string }[] })?.books;
      if (books?.length) {
        const titleLower = item.title.toLowerCase();
        const match =
          books.find((b) => {
            const bt = b.title.toLowerCase();
            return bt === titleLower || bt.includes(titleLower) || titleLower.includes(bt);
          }) || books[0];
        navigate(`/book/${encodeURIComponent(match.id)}`);
      } else {
        toast("No store page found for this book", "info");
      }
    } catch {
      toast("Couldn't reach the store catalog", "error");
    } finally {
      setStoreLoading(false);
    }
  };

  const handleDelete = async () => {
    if (!itemId) return;
    setDeleting(true);
    try {
      await api.delete(`/admin/library/abs/${encodeURIComponent(itemId)}`);
      queryClient.setQueryData(libraryQueryKey("abs-collection"), (prev: unknown) => {
        if (!prev || typeof prev !== "object") return prev;
        const data = prev as {
          genres?: Record<string, Array<{ itemId?: string }>>;
          ungrouped?: Array<{ itemId?: string }>;
          totalItems?: number;
        };
        const drop = (arr: Array<{ itemId?: string }> | undefined) =>
          (arr || []).filter((it) => (it?.itemId || "").trim() !== itemId);
        const genres: Record<string, Array<{ itemId?: string }>> = {};
        for (const [g, bucket] of Object.entries(data.genres || {})) {
          const next = drop(bucket);
          if (next.length) genres[g] = next;
        }
        const ungrouped = drop(data.ungrouped);
        const ids = new Set<string>();
        for (const bucket of Object.values(genres)) {
          for (const it of bucket) {
            const id = (it?.itemId || "").trim();
            if (id) ids.add(id);
          }
        }
        for (const it of ungrouped) {
          const id = (it?.itemId || "").trim();
          if (id) ids.add(id);
        }
        return { ...data, genres, ungrouped, totalItems: ids.size };
      });
      void softRefreshLibraryCollectionQueries(queryClient);
      toast("Audiobook deleted from library", "success");
      setShowDelete(false);
      navigate("/my-library", { replace: true });
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Failed to delete audiobook";
      toast(typeof detail === "string" ? detail : "Failed to delete audiobook", "error");
    } finally {
      setDeleting(false);
    }
  };

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

  if (error || !item) {
    return (
      <div className="max-w-5xl mx-auto px-4 py-16 text-center">
        <p className="text-gray-400">Book not found in your library</p>
        <Link to="/my-library" className="text-brand-400 hover:text-brand-300 mt-4 inline-block">
          Back to My Library
        </Link>
      </div>
    );
  }

  const seriesLine = item.series
    .filter((s) => s.name)
    .map((s) => {
      const seq = formatSeriesSeq(storeSeqByTitle.get(normTitle(item.title)) || s.sequence);
      return seq ? `${s.name} #${seq}` : s.name;
    })
    .join(" · ");

  const cover = item.coverUrl ? (
    <CoverImage src={item.coverUrl} alt={item.title} className="w-full rounded-xl shadow-2xl shadow-black/40" />
  ) : (
    <div className="w-full aspect-[2/3] bg-gray-800 rounded-xl flex items-center justify-center text-gray-700">
      <Headphones size={48} />
    </div>
  );

  // Icon actions (no Listen): Save Offline | Up Next | Share | View | Edit | Delete
  const iconActions = (variant: "desktop" | "mobile") => {
    const btn = variant === "mobile" ? mobileIconBtn : iconBtn;
    const danger = variant === "mobile" ? mobileIconBtnDanger : iconBtnDanger;
    const iconSize = variant === "mobile" ? 20 : 18;
    const offlineClass =
      variant === "mobile"
        ? "!h-12 !w-auto flex-1 min-w-0 max-w-[4.5rem] !rounded-xl"
        : undefined;
    return (
      <>
        {itemId && (
          <SaveOfflineButton
            target={{ kind: "abs", itemId }}
            iconOnly
            className={offlineClass}
          />
        )}
        <button
          type="button"
          onClick={handleAddToUpNext}
          title="Add to Up Next"
          aria-label="Add to Up Next"
          className={btn}
        >
          <ListPlus size={iconSize} />
        </button>
        {canShare && (
          <button
            type="button"
            onClick={() => void handleShare()}
            disabled={shareBusy}
            title="Share"
            aria-label="Share"
            className={btn}
          >
            {shareBusy ? <Loader2 size={iconSize} className="animate-spin" /> : <Share2 size={iconSize} />}
          </button>
        )}
        <button
          type="button"
          onClick={() => void handleViewInStore()}
          disabled={storeLoading || !online}
          title="View in Browse"
          aria-label="View in Browse"
          className={btn}
        >
          {storeLoading ? <Loader2 size={iconSize} className="animate-spin" /> : <Store size={iconSize} />}
        </button>
        {isAdmin && (
          <button
            type="button"
            onClick={() => setShowEditMetadata(true)}
            title="Edit metadata"
            aria-label="Edit metadata"
            className={btn}
          >
            <Tags size={iconSize} />
          </button>
        )}
        {isAdmin && (
          <button
            type="button"
            onClick={() => setShowDelete(true)}
            title="Delete"
            aria-label="Delete"
            className={danger}
          >
            <Trash2 size={iconSize} />
          </button>
        )}
      </>
    );
  };

  const desktopActions = (
    <>
      <button
        type="button"
        onClick={() => void handlePlay()}
        disabled={playLoading}
        title={hasLocalProgress ? "Resume" : "Listen"}
        aria-label={hasLocalProgress ? "Resume" : "Listen"}
        className="inline-flex items-center justify-center w-11 h-11 rounded-xl bg-emerald-600 text-white border border-emerald-500 hover:bg-emerald-500 transition-colors disabled:opacity-50"
      >
        {playLoading ? <Loader2 size={18} className="animate-spin" /> : <Headphones size={18} />}
      </button>
      {iconActions("desktop")}
    </>
  );

  const metaDetails = () => (
    <>
      {item.author && (
        <p className="text-gray-300 mt-1 md:mt-3 text-sm md:text-base">
          by <span className="text-gray-100 font-medium">{item.author}</span>
        </p>
      )}
      {seriesLine && <p className="text-xs md:text-sm text-brand-400 mt-0.5 md:mt-1">{seriesLine}</p>}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-2 md:mt-3 text-[11px] md:text-xs text-gray-400">
        {item.narrator && (
          <span className="inline-flex items-center gap-1">
            <Mic size={12} /> {item.narrator}
          </span>
        )}
        {item.duration > 0 && (
          <span className="inline-flex items-center gap-1">
            <Clock size={12} /> {formatDuration(item.duration)}
          </span>
        )}
        {item.publishedYear && <span>{item.publishedYear}</span>}
      </div>

      {item.genres.length > 0 && (
        <div className="flex flex-wrap gap-1 md:gap-1.5 mt-2 md:mt-3">
          {item.genres.map((g) => (
            <span
              key={g}
              className="px-1.5 md:px-2 py-0.5 text-[9px] md:text-[10px] bg-gray-800 text-gray-300 rounded-full border border-gray-700"
            >
              {g}
            </span>
          ))}
        </div>
      )}
    </>
  );

  // Prefer store series numbering (Hardcover keeps novella decimals like 2.1).
  // ABS/local metadata often stores only whole numbers for those entries.
  const useStoreSeries =
    !!storeSeries?.seriesName && (storeSeries.books?.length ?? 0) > 1;
  const useLocalSeries = !useStoreSeries && !!localSeries && localSeries.books.length > 1;
  const seriesLoading =
    (!!storeMatch && storeSeriesLoading) ||
    (!useStoreSeries && !!seriesName && localSeriesLoading);
  const displaySeriesName =
    (useStoreSeries && storeSeries?.seriesName) || localSeries?.name || seriesName;

  const availabilityBadge = (avail?: StoreSeriesBook["availability"], forceInLibrary?: boolean) => {
    const inLibrary = forceInLibrary || Boolean(avail?.inLibrary);
    const cached = Boolean(avail?.available);
    const catalogOnly = Boolean(avail?.catalogOnly) || (!cached && !inLibrary);
    if (inLibrary) {
      return (
        <span
          className="absolute top-1 right-1 flex items-center gap-0.5 px-1 py-0.5 rounded bg-emerald-900/90 text-emerald-300 text-[8px] font-medium"
          title="Already in library"
        >
          <Check size={8} strokeWidth={3} />
        </span>
      );
    }
    if (cached) {
      return (
        <span
          className="absolute top-1 right-1 flex items-center gap-0.5 px-1 py-0.5 rounded bg-emerald-900/90 text-emerald-300 text-[8px] font-medium"
          title="Cached — available to download"
        >
          <Download size={8} />
        </span>
      );
    }
    if (catalogOnly) {
      return (
        <span
          className="absolute top-1 right-1 flex items-center gap-0.5 px-1 py-0.5 rounded bg-amber-950/90 text-amber-300 text-[8px] font-medium"
          title="In catalog — not yet cached"
        >
          <HelpCircle size={8} />
        </span>
      );
    }
    return null;
  };

  const seriesSection = (
    <>
      {seriesLoading && (
        <div className="mt-8">
          <p className="text-sm text-gray-500">Looking up series…</p>
        </div>
      )}

      {!seriesLoading && useStoreSeries && storeSeries && (
        <div className="mt-8">
          <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
            <h2 className="text-lg font-semibold text-gray-100">
              More in {displaySeriesName}
              <span className="text-sm text-gray-500 font-normal ml-2">
                ({storeSeries.books.length} books)
              </span>
            </h2>
            {storeMatch && (
              <button
                type="button"
                onClick={() => navigate(`/series/${encodeURIComponent(storeMatch)}`)}
                className="shrink-0 px-3 py-1.5 rounded-lg bg-brand-900/40 border border-brand-700/50 text-sm font-medium text-brand-300 hover:bg-brand-900/60"
              >
                More in this series
              </button>
            )}
          </div>
          <div className={seriesStripClass}>
            {[...storeSeries.books]
              .map((sb) => {
                const local = absByTitle.get(normTitle(sb.title));
                // Store/Hardcover sequence wins — local ABS often drops novella decimals.
                const seq = formatSeriesSeq(sb.sequence || local?.sequence);
                return { sb, local, seq };
              })
              .sort((a, b) => seqSortKey(a.seq) - seqSortKey(b.seq))
              .map(({ sb, local, seq }) => {
                const absId = local?.itemId;
                const isCurrent =
                  absId === itemId ||
                  (!!storeMatch && sb.id === storeMatch) ||
                  normTitle(sb.title) === normTitle(item.title);
                return (
                  <button
                    key={sb.id}
                    type="button"
                    onClick={() => {
                      if (isCurrent) return;
                      if (absId) navigate(`/library/abs/${encodeURIComponent(absId)}`);
                      else navigate(`/book/${encodeURIComponent(sb.id)}`);
                    }}
                    className="group text-left flex flex-col h-full"
                  >
                    <div
                      className={`relative aspect-[2/3] bg-gray-900 overflow-hidden rounded-lg border transition-all duration-200 group-hover:-translate-y-0.5 ${
                        isCurrent
                          ? "border-brand-500 ring-1 ring-brand-500/30"
                          : "border-gray-800 group-hover:border-gray-600 group-hover:shadow-lg group-hover:shadow-black/20"
                      }`}
                    >
                      {sb.coverUrl ? (
                        <CoverImage src={sb.coverUrl} alt={sb.title} className="w-full h-full object-cover" loading="lazy" />
                      ) : (
                        <div className="w-full h-full flex items-center justify-center text-gray-700">
                          <BookOpen size={16} />
                        </div>
                      )}
                      {seq && (
                        <span className="absolute top-0.5 left-0.5 px-1 py-0.5 bg-black/70 text-[8px] text-gray-300 rounded font-mono">
                          #{seq}
                        </span>
                      )}
                      {availabilityBadge(sb.availability, !!absId)}
                    </div>
                    <div className="pt-1.5 px-0.5 pb-0.5 flex flex-col gap-0.5">
                      <h3 className="text-[11px] font-bold text-gray-100 line-clamp-2 leading-snug">{sb.title}</h3>
                    </div>
                  </button>
                );
              })}
          </div>
        </div>
      )}

      {!seriesLoading && useLocalSeries && localSeries && (
        <div className="mt-8">
          <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
            <h2 className="text-lg font-semibold text-gray-100">
              More in {displaySeriesName}
              <span className="text-sm text-gray-500 font-normal ml-2">
                ({localSeries.books.length} books)
              </span>
            </h2>
          </div>
          <div className={seriesStripClass}>
            {[...localSeries.books]
              .map((sb) => ({
                sb,
                seq: formatSeriesSeq(storeSeqByTitle.get(normTitle(sb.title)) || sb.sequence),
              }))
              .sort((a, b) => seqSortKey(a.seq) - seqSortKey(b.seq))
              .map(({ sb, seq }) => {
                const isCurrent = sb.itemId === itemId;
                return (
                  <button
                    key={sb.itemId}
                    type="button"
                    onClick={() => {
                      if (!isCurrent) navigate(`/library/abs/${encodeURIComponent(sb.itemId)}`);
                    }}
                    className="group text-left flex flex-col h-full"
                  >
                    <div
                      className={`relative aspect-[2/3] bg-gray-900 overflow-hidden rounded-lg border transition-all duration-200 group-hover:-translate-y-0.5 ${
                        isCurrent
                          ? "border-brand-500 ring-1 ring-brand-500/30"
                          : "border-gray-800 group-hover:border-gray-600 group-hover:shadow-lg group-hover:shadow-black/20"
                      }`}
                    >
                      {sb.coverUrl ? (
                        <CoverImage src={sb.coverUrl} alt={sb.title} className="w-full h-full object-cover" loading="lazy" />
                      ) : (
                        <div className="w-full h-full flex items-center justify-center text-gray-700">
                          <Headphones size={16} />
                        </div>
                      )}
                      {seq && (
                        <span className="absolute top-0.5 left-0.5 px-1.5 py-0.5 bg-black/70 text-[9px] text-gray-200 rounded font-mono">
                          #{seq}
                        </span>
                      )}
                      {availabilityBadge(undefined, true)}
                    </div>
                    <div className="pt-1.5 px-0.5 pb-0.5 flex flex-col gap-0.5">
                      <h3 className="text-xs font-bold text-gray-100 line-clamp-2 leading-snug">{sb.title}</h3>
                    </div>
                  </button>
                );
              })}
          </div>
        </div>
      )}
    </>
  );

  return (
    <div className="max-w-5xl mx-auto px-4 py-8">
      <div className="mb-5">
        <button
          type="button"
          onClick={() => window.history.back()}
          title="Back"
          aria-label="Back"
          className="inline-flex items-center justify-center w-10 h-10 rounded-full bg-gradient-to-br from-gray-800 to-gray-900 text-gray-200 border border-gray-600/80 shadow-md shadow-black/30 hover:from-gray-700 hover:to-gray-800 hover:text-white hover:border-gray-500 transition-all"
        >
          <ChevronLeft size={22} strokeWidth={2.5} className="-ml-0.5" />
        </button>
      </div>

      <div className="md:hidden mb-5">
        <div className="flex gap-3 items-start">
          <div className="w-[9.5rem] shrink-0">{cover}</div>
          <div className="flex-1 min-w-0 pt-0.5">
            <h1 className="text-lg font-bold text-gray-100 leading-snug line-clamp-3">{item.title}</h1>
            {item.subtitle && (
              <p className="text-sm text-gray-400 mt-0.5 line-clamp-2">{item.subtitle}</p>
            )}
            {metaDetails()}
          </div>
        </div>
        <button
          type="button"
          onClick={() => void handlePlay()}
          disabled={playLoading}
          className="mt-4 w-full inline-flex items-center justify-center gap-2 px-4 py-3 text-base font-semibold rounded-xl bg-emerald-600 text-white hover:bg-emerald-500 transition-colors disabled:opacity-50"
        >
          {playLoading ? <Loader2 size={20} className="animate-spin" /> : <Headphones size={20} />}
          {hasLocalProgress ? "Resume" : "Listen"}
        </button>
        <div className="mt-3 flex w-full items-center justify-center gap-2">
          {iconActions("mobile")}
        </div>
      </div>

      <div className="flex flex-col md:flex-row gap-8">
        <div className="hidden md:block w-64 shrink-0">{cover}</div>
        <div className="flex-1 min-w-0">
          <div className="hidden md:block">
            <h1 className="text-2xl sm:text-3xl font-bold text-gray-100 leading-tight">{item.title}</h1>
            {item.subtitle && <p className="text-base sm:text-lg text-gray-400 mt-1">{item.subtitle}</p>}
            {metaDetails()}
          </div>

          <div className="hidden md:flex flex-wrap items-center gap-2 mt-4">{desktopActions}</div>

          {item.description && (
            <div className="mt-6">
              <h2 className="text-lg font-semibold text-gray-100 mb-3">Synopsis</h2>
              <div
                className="text-gray-300 text-sm leading-relaxed prose prose-invert prose-sm max-w-none"
                dangerouslySetInnerHTML={{ __html: item.description }}
              />
            </div>
          )}

          {seriesSection}
        </div>
      </div>

      <Modal title="Delete audiobook" show={showDelete} onClose={() => !deleting && setShowDelete(false)}>
        <p className="text-sm text-gray-400 mb-4">
          Permanently delete <span className="text-gray-200">{item.title}</span> from the library?
          Audiobook files on disk and the Audiobookshelf entry will be removed. This cannot be undone.
        </p>
        <div className="flex gap-2 justify-end">
          <button
            type="button"
            onClick={() => setShowDelete(false)}
            disabled={deleting}
            className="px-3 py-1.5 text-gray-300 hover:bg-gray-700 rounded-lg disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void handleDelete()}
            disabled={deleting}
            className="px-3 py-1.5 bg-red-600 text-white rounded-lg hover:bg-red-500 disabled:opacity-50"
          >
            {deleting ? "Deleting..." : "Delete"}
          </button>
        </div>
      </Modal>

      {isAdmin && itemId && (
        <QuickReviewWizard
          itemId={itemId}
          title={item.title}
          open={showEditMetadata}
          onClose={() => setShowEditMetadata(false)}
          onApplied={() => {
            void queryClient.invalidateQueries({ queryKey: ["abs-item-detail", itemId] });
            void softRefreshLibraryCollectionQueries(queryClient);
          }}
        />
      )}
    </div>
  );
}
