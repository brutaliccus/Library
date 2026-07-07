import { useParams, Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import api from "../api/client";
import { usePlayer } from "../contexts/PlayerContext";
import { useToast } from "../contexts/ToastContext";
import {
  ArrowLeft, BookOpen, Headphones, Loader2, Mic, Clock, Store,
} from "lucide-react";

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

/** Detail page for a book that's already in the Audiobookshelf library.
 * Shows synopsis and metadata from ABS directly — no store search round-trip. */
export default function LibraryBookDetail() {
  const { itemId: rawItemId } = useParams<{ itemId: string }>();
  const itemId = rawItemId ? decodeURIComponent(rawItemId) : undefined;
  const navigate = useNavigate();
  const { playABS } = usePlayer();
  const { toast } = useToast();
  const [playLoading, setPlayLoading] = useState(false);
  const [storeLoading, setStoreLoading] = useState(false);

  const { data: item, isLoading, error } = useQuery({
    queryKey: ["abs-item-detail", itemId],
    queryFn: async () => {
      const { data } = await api.get(`/library/abs/item/${encodeURIComponent(itemId!)}`);
      return data as ABSItemDetail;
    },
    enabled: !!itemId,
    staleTime: 5 * 60 * 1000,
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

  const handlePlay = async () => {
    if (!item) return;
    setPlayLoading(true);
    try {
      await playABS(item.itemId);
    } catch {
      toast("Failed to start playback", "error");
    } finally {
      setPlayLoading(false);
    }
  };

  /** Optional jump to the store catalog page for ratings / series / downloads. */
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
    <img src={item.coverUrl} alt={item.title} className="w-full rounded-xl shadow-2xl shadow-black/40" />
  ) : (
    <div className="w-full aspect-[2/3] bg-gray-800 rounded-xl flex items-center justify-center text-gray-700">
      <Headphones size={48} />
    </div>
  );

  const actions = (
    <>
      <button
        onClick={handlePlay}
        disabled={playLoading}
        className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-emerald-600 text-white hover:bg-emerald-500 transition-colors disabled:opacity-50"
      >
        {playLoading ? <Loader2 size={16} className="animate-spin" /> : <Headphones size={16} />}
        Listen
      </button>
      {ebookMatch?.chapterId ? (
        <button
          onClick={() => navigate(`/read/${ebookMatch.chapterId}`)}
          className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-amber-600 text-white hover:bg-amber-500 transition-colors"
        >
          <BookOpen size={16} />
          Read
        </button>
      ) : null}
      <button
        onClick={handleViewInStore}
        disabled={storeLoading}
        className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-gray-800 text-gray-300 border border-gray-700 hover:bg-gray-700 transition-colors disabled:opacity-50"
      >
        {storeLoading ? <Loader2 size={16} className="animate-spin" /> : <Store size={16} />}
        View in Store
      </button>
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

      {/* Mobile layout */}
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
        </div>
      </div>
    </div>
  );
}
