import { useEffect, useMemo, useRef } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ArrowLeft, BookOpen, Loader2 } from "lucide-react";
import api from "../api/client";
import BookGrid from "../components/BookGrid";
import GenreSidebar from "../components/GenreSidebar";
import type { Genre } from "../components/GenreSidebar";
import type { BookSummary } from "../types/book";

const PAGE_SIZE = 24;

const SPECIAL_TITLES: Record<string, string> = {
  all: "All Available",
  available: "Available to Download",
  popular: "Popular",
  new: "New Releases",
};

function findGenreName(slug: string, genres: Genre[]): string {
  if (SPECIAL_TITLES[slug]) return SPECIAL_TITLES[slug];
  for (const g of genres) {
    if (g.slug === slug) return g.name;
    for (const c of g.children) {
      if (c.slug === slug) return c.name;
    }
  }
  return slug.replace(/-/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function isAvailableOnlySlug(slug: string): boolean {
  return slug === "all" || slug === "available" || slug === "popular" || slug === "new";
}

function isTaxonomySlug(slug: string, genres: Genre[]): boolean {
  if (SPECIAL_TITLES[slug]) return true;
  for (const g of genres) {
    if (g.slug === slug) return true;
    for (const c of g.children) {
      if (c.slug === slug) return true;
    }
  }
  return false;
}

interface Props {
  genreMobileOpen?: boolean;
  onGenreMobileClose?: () => void;
}

export default function ShelfPage({ genreMobileOpen = false, onGenreMobileClose }: Props) {
  const { slug = "" } = useParams<{ slug: string }>();
  const navigate = useNavigate();
  const decoded = decodeURIComponent(slug);
  const sentinelRef = useRef<HTMLDivElement>(null);

  const { data: genresData } = useQuery({
    queryKey: ["genres"],
    queryFn: async () => {
      const { data } = await api.get("/books/genres");
      return data as { genres: Genre[] };
    },
    staleTime: 60 * 60 * 1000,
  });

  const { data: curatedSlugsData } = useQuery({
    queryKey: ["curated-slugs"],
    queryFn: async () => {
      const { data } = await api.get("/books/curated-slugs");
      return data as { slugs: string[] };
    },
    staleTime: 24 * 60 * 60 * 1000,
  });

  const curatedSlugs = useMemo(
    () => new Set(curatedSlugsData?.slugs || []),
    [curatedSlugsData]
  );

  const genres = genresData?.genres || [];
  // Home recommendation slugs (best-fantasy, …) are curated. Plain genre
  // taxonomy slugs (fantasy, romance, …) stay on category browse / GenreHub.
  const isCuratedShelf =
    Boolean(genresData) &&
    curatedSlugs.has(decoded) &&
    !isTaxonomySlug(decoded, genres);

  const title = findGenreName(decoded, genres);
  const availableOnly = isAvailableOnlySlug(decoded);

  const {
    data,
    isLoading,
    isError,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useInfiniteQuery({
    queryKey: ["shelf", decoded, availableOnly, isCuratedShelf],
    queryFn: async ({ pageParam }) => {
      if (isCuratedShelf) {
        if (pageParam > 1) {
          return {
            books: [] as BookSummary[],
            totalItems: 0,
            category: title,
            page: pageParam,
            pageSize: PAGE_SIZE,
          };
        }
        const params = new URLSearchParams({ pageSize: "40" });
        const { data } = await api.get(
          `/books/curated/${encodeURIComponent(decoded)}?${params}`
        );
        return data as {
          books: BookSummary[];
          totalItems: number;
          category?: string;
          listName?: string;
          page: number;
          pageSize: number;
        };
      }
      const params = new URLSearchParams({
        page: String(pageParam),
        pageSize: String(PAGE_SIZE),
        available_only: availableOnly ? "true" : "false",
      });
      const { data } = await api.get(
        `/books/category/${encodeURIComponent(decoded)}?${params}`
      );
      return data as {
        books: BookSummary[];
        totalItems: number;
        category?: string;
        page: number;
        pageSize: number;
      };
    },
    initialPageParam: 1,
    getNextPageParam: (lastPage, pages) => {
      if (isCuratedShelf) return undefined;
      if (!lastPage?.books?.length) return undefined;
      if (lastPage.books.length < PAGE_SIZE) return undefined;
      const loaded = pages.reduce((n, p) => n + (p.books?.length || 0), 0);
      if (lastPage.totalItems && loaded >= lastPage.totalItems) return undefined;
      return pages.length + 1;
    },
    enabled: Boolean(decoded) && Boolean(genresData) && curatedSlugsData !== undefined,
    staleTime: 5 * 60 * 1000,
  });

  const books = useMemo(
    () => (data?.pages || []).flatMap((p) => p.books || []),
    [data]
  );
  const heading =
    (data?.pages?.[0] as { listName?: string; category?: string } | undefined)?.listName ||
    data?.pages?.[0]?.category ||
    title;

  const pageCount = data?.pages?.length ?? 0;
  useEffect(() => {
    if (isCuratedShelf) return;
    if (pageCount === 1 && hasNextPage && !isFetchingNextPage) {
      void fetchNextPage();
    }
  }, [pageCount, hasNextPage, isFetchingNextPage, fetchNextPage, isCuratedShelf]);

  useEffect(() => {
    const el = sentinelRef.current;
    if (!el || isCuratedShelf) return;
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting && hasNextPage && !isFetchingNextPage) {
          void fetchNextPage();
        }
      },
      { rootMargin: "900px 0px", threshold: 0 }
    );
    io.observe(el);
    return () => io.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage, isCuratedShelf]);

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

          <h1 className="text-2xl font-bold text-gray-100 mb-1">{heading}</h1>
          <p className="text-sm text-gray-500 mb-6">
            {isLoading
              ? "Loading…"
              : `${books.length}${data?.pages?.[0]?.totalItems ? ` of ${data.pages[0].totalItems}` : ""} title${books.length === 1 ? "" : "s"}`}
            {isCuratedShelf ? " · Curated on Hardcover" : ""}
          </p>

          {isLoading && (
            <p className="text-sm text-gray-500 flex items-center gap-2 mb-4">
              <BookOpen size={16} className="animate-pulse" />
              Loading shelf…
            </p>
          )}
          {isError && (
            <p className="text-sm text-red-400">Could not load this shelf.</p>
          )}

          <BookGrid books={books} isLoading={isLoading && books.length === 0} />

          <div ref={sentinelRef} className="h-8" />
          {isFetchingNextPage && (
            <p className="text-sm text-gray-500 flex items-center justify-center gap-2 py-4">
              <Loader2 size={16} className="animate-spin" />
              Loading more…
            </p>
          )}
          {!hasNextPage && books.length > 0 && (
            <p className="text-center text-xs text-gray-600 py-4">End of shelf</p>
          )}
        </div>
      </div>
    </div>
  );
}
