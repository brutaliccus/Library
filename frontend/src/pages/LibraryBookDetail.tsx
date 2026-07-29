import { useParams, Link, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import api from "../api/client";
import { usePlayer } from "../contexts/PlayerContext";
import { useToast } from "../contexts/ToastContext";
import { useAuth } from "../hooks/useAuth";
import {
  ArrowLeft, BookOpen, Headphones, Loader2, Mic, Clock, Store, Trash2, ListPlus, Share2,
} from "lucide-react";
import CoverImage from "../components/CoverImage";
import SaveOfflineButton from "../components/SaveOfflineButton";
import Modal from "../components/Modal";
import { useOnlineStatus } from "../hooks/useOnlineStatus";
import { softRefreshLibraryCollectionQueries } from "../utils/shelfQueryCache";
import type { AbsChapter } from "../types/player";
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

function formatDuration(secs: number): string {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function formatChapterTime(s: number): string {
  if (!s || !isFinite(s)) return "0:00";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  const pad = (n: number) => n.toString().padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

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

  const { data: chaptersData } = useQuery({
    queryKey: ["abs-chapters", itemId],
    queryFn: async () => {
      const { data } = await api.get(`/stream/abs/${encodeURIComponent(itemId!)}/chapters`);
      return data as { chapters: AbsChapter[] };
    },
    enabled: !!itemId && online,
    staleTime: 10 * 60 * 1000,
    retry: 1,
  });

  const { data: ebookMatch } = useQuery({
    queryKey: ["ebook-match-lib", item?.title, item?.author],
    queryFn: async () => {
      const params = new URLSearchParams({ title: item!.title });
      if (item!.author) params.set("author", item!.author);
      const s = item!.series?.[0];
      if (s?.name) params.set("seriesName", s.name);
      if (s?.sequence) params.set("seriesIndex", s.sequence);
      const { data } = await api.get(`/library/ebook-match?${params}`);
      return data as { chapterId: number | null };
    },
    enabled: !!item?.title,
    staleTime: 5 * 60 * 1000,
  });

  const handlePlay = async (startAt?: number) => {
    if (!item) return;
    setPlayLoading(true);
    try {
      await playABS(item.itemId, startAt != null ? { startAt } : undefined);
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
      const url = `${window.location.origin}${path}`;
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
          // User cancel — fall through to clipboard.
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
      // Optimistic local remove, then soft-refresh (keep shelf visible).
      queryClient.setQueryData(["abs-collection"], (prev: unknown) => {
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
    .map((s) => (s.sequence ? `${s.name} #${s.sequence}` : s.name))
    .join(" · ");

  const cover = item.coverUrl ? (
    <CoverImage src={item.coverUrl} alt={item.title} className="w-full rounded-xl shadow-2xl shadow-black/40" />
  ) : (
    <div className="w-full aspect-[2/3] bg-gray-800 rounded-xl flex items-center justify-center text-gray-700">
      <Headphones size={48} />
    </div>
  );

  const actions = (
    <>
      <button
        type="button"
        onClick={() => void handlePlay()}
        disabled={playLoading}
        className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-emerald-600 text-white hover:bg-emerald-500 transition-colors disabled:opacity-50"
      >
        {playLoading ? <Loader2 size={16} className="animate-spin" /> : <Headphones size={16} />}
        {hasLocalProgress ? "Resume" : "Listen"}
      </button>
      {canShare && (
        <button
          type="button"
          onClick={() => void handleShare()}
          disabled={shareBusy}
          title="Share listen link"
          className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-gray-800 text-gray-300 border border-gray-700 hover:bg-gray-700 transition-colors disabled:opacity-50"
        >
          {shareBusy ? <Loader2 size={16} className="animate-spin" /> : <Share2 size={16} />}
          Share
        </button>
      )}
      <button
        type="button"
        onClick={handleAddToUpNext}
        className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-gray-800 text-gray-300 border border-gray-700 hover:bg-gray-700 transition-colors"
      >
        <ListPlus size={16} />
        Add to Up Next
      </button>
      {ebookMatch?.chapterId ? (
        <button
          type="button"
          onClick={() => navigate(`/read/${ebookMatch.chapterId}`)}
          className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-amber-600 text-white hover:bg-amber-500 transition-colors"
        >
          <BookOpen size={16} />
          Read
        </button>
      ) : null}
      {itemId && <SaveOfflineButton target={{ kind: "abs", itemId }} />}
      <button
        type="button"
        onClick={handleViewInStore}
        disabled={storeLoading || !online}
        className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-gray-800 text-gray-300 border border-gray-700 hover:bg-gray-700 transition-colors disabled:opacity-50"
      >
        {storeLoading ? <Loader2 size={16} className="animate-spin" /> : <Store size={16} />}
        View in Browse
      </button>
      {isAdmin && (
        <button
          type="button"
          onClick={() => setShowDelete(true)}
          className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-red-900/40 text-red-300 border border-red-800/60 hover:bg-red-900/60 transition-colors"
        >
          <Trash2 size={16} />
          Delete
        </button>
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

      <div className="flex gap-4 mb-5 md:hidden">
        <div className="w-[7.5rem] shrink-0">{cover}</div>
        <div className="flex flex-col gap-2 flex-1 content-start self-start">{actions}</div>
      </div>

      <div className="flex flex-col md:flex-row gap-8">
        <div className="hidden md:block w-64 shrink-0">{cover}</div>
        <div className="flex-1 min-w-0">
          <h1 className="text-2xl sm:text-3xl font-bold text-gray-100 leading-tight">{item.title}</h1>
          {item.subtitle && <p className="text-base sm:text-lg text-gray-400 mt-1">{item.subtitle}</p>}
          {item.author && (
            <p className="text-gray-300 mt-2 sm:mt-3">
              by <span className="text-gray-100 font-medium">{item.author}</span>
            </p>
          )}
          {seriesLine && <p className="text-sm text-brand-400 mt-1">{seriesLine}</p>}

          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-3 text-xs text-gray-400">
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
            <div className="flex flex-wrap gap-1.5 mt-3">
              {item.genres.map((g) => (
                <span key={g} className="px-2 py-0.5 text-[10px] bg-gray-800 text-gray-300 rounded-full border border-gray-700">
                  {g}
                </span>
              ))}
            </div>
          )}

          <div className="hidden md:flex flex-wrap items-center gap-2 mt-4">{actions}</div>

          {item.description && (
            <div className="mt-6">
              <h2 className="text-lg font-semibold text-gray-100 mb-3">Synopsis</h2>
              <div
                className="text-gray-300 text-sm leading-relaxed prose prose-invert prose-sm max-w-none"
                dangerouslySetInnerHTML={{ __html: item.description }}
              />
            </div>
          )}

          {(chaptersData?.chapters?.length ?? 0) > 0 && (
            <div className="mt-8">
              <h2 className="text-lg font-semibold text-gray-100 mb-3">Chapters</h2>
              <ul className="space-y-1 max-h-80 overflow-y-auto pr-1">
                {chaptersData!.chapters.map((ch) => (
                  <li key={`${ch.id}-${ch.start}`}>
                    <button
                      type="button"
                      onClick={() => void handlePlay(ch.start)}
                      disabled={playLoading}
                      className="w-full text-left flex items-start gap-3 px-3 py-2 rounded-lg text-gray-300 hover:bg-gray-800 hover:text-white transition-colors disabled:opacity-50"
                    >
                      <span className="text-xs tabular-nums text-gray-500 shrink-0 pt-0.5">
                        {formatChapterTime(ch.start)}
                      </span>
                      <span className="text-sm flex-1 leading-snug">{ch.title}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
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
    </div>
  );
}
