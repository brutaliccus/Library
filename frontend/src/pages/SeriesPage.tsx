import { useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, BookOpen } from "lucide-react";
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
        <p className="text-sm text-gray-500 mb-6">
          {books.length} book{books.length === 1 ? "" : "s"}
          {data.source === "hardcover" ? " · via Hardcover" : ""}
        </p>
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
