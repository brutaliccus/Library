import { useEffect } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, BookOpen } from "lucide-react";
import api from "../api/client";
import BookCarousel from "../components/BookCarousel";
import GenreSidebar from "../components/GenreSidebar";
import type { Genre } from "../components/GenreSidebar";
import type { BookSummary } from "../types/book";

function GenreChildShelf({ slug, name }: { slug: string; name: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["genre-child-shelf", slug],
    queryFn: async () => {
      const params = new URLSearchParams({
        page: "1",
        pageSize: "12",
        available_only: "false",
      });
      const { data } = await api.get(
        `/books/category/${encodeURIComponent(slug)}?${params}`
      );
      return data as { books: BookSummary[]; category?: string };
    },
    staleTime: 10 * 60 * 1000,
  });

  return (
    <BookCarousel
      title={name}
      books={data?.books || []}
      isLoading={isLoading}
      to={`/shelf/${encodeURIComponent(slug)}`}
    />
  );
}

/** Map genre hub slug → home curated shelf slug for "View all". */
const GENRE_CURATED_VIEW_ALL: Record<string, string> = {
  fantasy: "best-fantasy",
  "science-fiction": "best-scifi",
  mystery: "best-mystery",
  thriller: "best-thriller",
  romance: "best-romance",
  horror: "best-horror",
  "young-adult": "best-ya",
  "literary-fiction": "best-literary",
  "historical-fiction": "best-historical",
  nonfiction: "best-nonfiction",
};

/** Hardcover curated list for this genre (e.g. "31 Best Fantasy…"), above subgenres. */
function GenreCuratedShelf({ slug }: { slug: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["genre-curated-shelf", slug],
    queryFn: async () => {
      try {
        const { data } = await api.get(
          `/books/curated/${encodeURIComponent(slug)}?pageSize=12`
        );
        return data as {
          books: BookSummary[];
          listName?: string;
          category?: string;
          source?: string;
        };
      } catch {
        return { books: [] as BookSummary[], source: "none" };
      }
    },
    staleTime: 6 * 60 * 60 * 1000,
  });

  const books = data?.books || [];
  if (!isLoading && books.length === 0) return null;
  const title = data?.listName || data?.category || "Curated picks";
  const viewAllSlug = GENRE_CURATED_VIEW_ALL[slug] || slug;

  return (
    <BookCarousel
      title={title}
      subtitle={data?.source?.startsWith("hardcover") ? "Curated on Hardcover" : undefined}
      books={books}
      isLoading={isLoading}
      to={`/shelf/${encodeURIComponent(viewAllSlug)}`}
    />
  );
}

interface Props {
  genreMobileOpen?: boolean;
  onGenreMobileClose?: () => void;
}

export default function GenreHubPage({
  genreMobileOpen = false,
  onGenreMobileClose,
}: Props) {
  const { slug = "" } = useParams<{ slug: string }>();
  const navigate = useNavigate();
  const decoded = decodeURIComponent(slug);

  const { data: genresData, isLoading } = useQuery({
    queryKey: ["genres"],
    queryFn: async () => {
      const { data } = await api.get("/books/genres");
      return data as { genres: Genre[] };
    },
    staleTime: 60 * 60 * 1000,
  });

  const genre = (genresData?.genres || []).find((g) => g.slug === decoded);

  useEffect(() => {
    if (genre && genre.children.length === 0) {
      navigate(`/shelf/${encodeURIComponent(decoded)}`, { replace: true });
    }
  }, [genre, decoded, navigate]);

  return (
    <div className="pb-12">
      <div className="flex px-4 lg:px-6 gap-6">
        {genresData && (
          <GenreSidebar
            genres={genresData.genres}
            mode="navigate"
            activeSlugs={[decoded]}
            mobileOpen={genreMobileOpen}
            onMobileClose={onGenreMobileClose}
          />
        )}

        <div className="flex-1 min-w-0 py-8">
          <button
            type="button"
            onClick={() => navigate(-1)}
            className="inline-flex items-center gap-1.5 text-sm text-gray-400 hover:text-gray-200 mb-4"
          >
            <ArrowLeft size={16} />
            Back
          </button>

          <h1 className="text-2xl font-bold text-gray-100 mb-1">
            {genre?.name || decoded.replace(/-/g, " ")}
          </h1>
          <p className="text-sm text-gray-500 mb-8">
            Browse subcategories — tap a shelf title for the full list
          </p>

          {isLoading && (
            <p className="text-sm text-gray-500 flex items-center gap-2">
              <BookOpen size={16} className="animate-pulse" />
              Loading genres…
            </p>
          )}

          {!isLoading && !genre && (
            <p className="text-sm text-red-400">Genre not found.</p>
          )}

          {genre && genre.children.length > 0 && (
            <div className="space-y-8">
              <GenreCuratedShelf slug={genre.slug} />
              <GenreChildShelf slug={genre.slug} name={`All ${genre.name}`} />
              {genre.children.map((child) => (
                <GenreChildShelf key={child.slug} slug={child.slug} name={child.name} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
