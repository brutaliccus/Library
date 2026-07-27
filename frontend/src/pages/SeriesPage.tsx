import { useNavigate, useParams, Link } from "react-router-dom";
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, BookOpen, Headphones } from "lucide-react";
import api from "../api/client";
import BookGrid from "../components/BookGrid";
import type { BookSummary } from "../types/book";

interface SeriesBook {
  id: string;
  title: string;
  subtitle?: string;
  coverUrl?: string;
  authors?: string[];
  sequence?: string;
  publishedDate?: string;
  availability?: BookSummary["availability"];
}

interface ABSItem {
  itemId: string;
  title: string;
  author: string;
  progress: number;
  isFinished: boolean;
}

export default function SeriesPage() {
  const params = useParams();
  const rawVolumeId = params["*"] ?? params.volumeId ?? "";
  const navigate = useNavigate();
  let decoded = rawVolumeId;
  try {
    decoded = decodeURIComponent(rawVolumeId);
  } catch {
    /* keep raw */
  }

  const { data, isLoading, isError } = useQuery({
    queryKey: ["book-series", decoded],
    queryFn: async () => {
      const { data } = await api.get(`/books/series/${encodeURIComponent(decoded)}`);
      return data as {
        seriesName: string | null;
        books: SeriesBook[];
        currentBookIndex: number;
        source?: string;
      };
    },
    enabled: Boolean(decoded),
    staleTime: 10 * 60 * 1000,
  });

  const { data: absCollection } = useQuery({
    queryKey: ["abs-collection"],
    queryFn: async () => {
      const { data } = await api.get("/library/abs/collection");
      return data as { genres: Record<string, ABSItem[]>; ungrouped: ABSItem[] };
    },
    staleTime: 30 * 60 * 1000,
  });

  const books: BookSummary[] = (data?.books || []).map((b) => ({
    id: b.id,
    title: b.title,
    subtitle: b.subtitle || "",
    authors: b.authors || [],
    publisher: "",
    publishedDate: b.publishedDate || "",
    description: "",
    pageCount: 0,
    categories: [],
    mainCategory: "",
    averageRating: 0,
    ratingsCount: 0,
    language: "",
    coverUrl: b.coverUrl || "",
    isbn10: "",
    isbn13: "",
    previewLink: "",
    infoLink: "",
    availability: b.availability,
  }));

  const listenStats = useMemo(() => {
    if (!absCollection || !data?.books?.length) return null;
    const owned = [...Object.values(absCollection.genres).flat(), ...absCollection.ungrouped];
    const byTitle = new Map(
      owned.map((i) => [i.title.trim().toLowerCase(), i] as const)
    );
    let ownedCount = 0;
    let listened = 0;
    let nextOwned: ABSItem | null = null;
    for (const b of data.books) {
      const hit = byTitle.get(b.title.trim().toLowerCase());
      if (!hit) continue;
      ownedCount += 1;
      if (hit.isFinished || (hit.progress || 0) >= 0.95) listened += 1;
      else if (!nextOwned && (hit.progress || 0) < 0.95) nextOwned = hit;
    }
    if (ownedCount === 0) return null;
    return { ownedCount, listened, total: data.books.length, nextOwned };
  }, [absCollection, data]);

  return (
    <div className="py-8 px-4 lg:px-6 max-w-6xl mx-auto">
      <button
        type="button"
        onClick={() => navigate(-1)}
        className="inline-flex items-center gap-1.5 text-sm text-gray-400 hover:text-gray-200 mb-4"
      >
        <ArrowLeft size={16} />
        Back
      </button>

      <h1 className="text-2xl font-bold text-gray-100 mb-1">
        {data?.seriesName ? `More in ${data.seriesName}` : "Series"}
      </h1>
      {data?.seriesName && (
        <p className="text-sm text-gray-500 mb-2">
          {books.length} book{books.length === 1 ? "" : "s"}
          {data.source === "hardcover" ? " · via Hardcover" : ""}
        </p>
      )}
      {listenStats && (
        <div className="mb-6 flex flex-wrap items-center gap-3">
          <p className="text-sm text-emerald-400/90 flex items-center gap-1.5">
            <Headphones size={14} />
            {listenStats.listened} of {listenStats.ownedCount} owned listened
            {listenStats.total > listenStats.ownedCount
              ? ` · ${listenStats.total} in series`
              : ""}
          </p>
          {listenStats.nextOwned && (
            <Link
              to={`/library/book/${encodeURIComponent(listenStats.nextOwned.itemId)}`}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg bg-emerald-600 text-white hover:bg-emerald-500 transition-colors"
            >
              Next: {listenStats.nextOwned.title}
            </Link>
          )}
        </div>
      )}

      {isLoading && (
        <p className="text-sm text-gray-500 flex items-center gap-2">
          <BookOpen size={16} className="animate-pulse" />
          Loading series…
        </p>
      )}
      {isError && (
        <p className="text-sm text-red-400">Could not load series for this book.</p>
      )}
      {!isLoading && !isError && books.length === 0 && (
        <p className="text-sm text-gray-500">No other books found in this series.</p>
      )}
      {books.length > 0 && <BookGrid books={books} />}
    </div>
  );
}
