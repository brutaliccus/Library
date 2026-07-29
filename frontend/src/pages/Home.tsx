import { useEffect, useMemo, useRef } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import api from "../api/client";
import HeroSearch from "../components/HeroSearch";
import BookCarousel from "../components/BookCarousel";
import BadgeLegend from "../components/BadgeLegend";
import ContinueShelves from "../components/ContinueShelves";
import GenreSidebar from "../components/GenreSidebar";
import type { Genre } from "../components/GenreSidebar";
import { Loader2 } from "lucide-react";
import type { BookSummary } from "../types/book";

interface HomeShelf {
  slug: string;
  title: string;
  genre?: string;
  listName?: string;
  source?: string;
  books: BookSummary[];
}

interface PersonalizedShelf {
  id: string;
  title: string;
  subtitle?: string;
  books: BookSummary[];
}

interface Props {
  genreMobileOpen: boolean;
  onGenreMobileClose: () => void;
  onGenreToggle?: () => void;
  genreActiveCount?: number;
  onActiveCountChange: (count: number) => void;
}

export default function Home({
  genreMobileOpen,
  onGenreMobileClose,
  onGenreToggle,
  genreActiveCount = 0,
  onActiveCountChange,
}: Props) {
  useEffect(() => { onActiveCountChange(0); }, [onActiveCountChange]);

  const { data: genresData } = useQuery({
    queryKey: ["genres"],
    queryFn: async () => {
      const { data } = await api.get("/books/genres");
      return data as { genres: Genre[] };
    },
    staleTime: 24 * 60 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    refetchOnWindowFocus: false,
  });

  const shelfSentinelRef = useRef<HTMLDivElement>(null);

  const { data: settingsData } = useQuery({
    queryKey: ["user-settings"],
    queryFn: async () => {
      const { data } = await api.get("/auth/settings");
      return data as { private_mode?: boolean };
    },
    staleTime: 5 * 60 * 1000,
  });
  const privateMode = !!settingsData?.private_mode;

  const { data: personalizedData, isLoading: personalizedLoading } = useQuery({
    queryKey: ["personalized-shelves"],
    queryFn: async () => {
      try {
        const { data } = await api.get("/books/personalized-shelves");
        return data as { shelves: PersonalizedShelf[]; disabled?: boolean };
      } catch {
        return { shelves: [] as PersonalizedShelf[] };
      }
    },
    enabled: !privateMode,
    staleTime: 15 * 60 * 1000,
    refetchOnWindowFocus: false,
    retry: 1,
  });

  const { data: trendingData, isLoading: trendingLoading } = useQuery({
    queryKey: ["trending-books"],
    queryFn: async () => {
      try {
        const { data } = await api.get("/books/trending");
        return data as { books: BookSummary[]; refreshedAt?: string };
      } catch {
        return { books: [] as BookSummary[] };
      }
    },
    staleTime: 15 * 60 * 1000,
    gcTime: 48 * 60 * 60 * 1000,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    retry: 1,
  });

  const { data: newReleasesData, isLoading: newReleasesLoading } = useQuery({
    queryKey: ["new-releases"],
    queryFn: async () => {
      try {
        const { data } = await api.get("/books/new-releases");
        return data as { books: BookSummary[]; refreshedAt?: string };
      } catch {
        return { books: [] as BookSummary[] };
      }
    },
    staleTime: 15 * 60 * 1000,
    gcTime: 48 * 60 * 60 * 1000,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    retry: 1,
  });

  const {
    data: homeShelvesPages,
    isLoading: homeShelvesLoading,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useInfiniteQuery({
    queryKey: ["home-shelves", new Date().toISOString().slice(0, 10)],
    queryFn: async ({ pageParam }) => {
      try {
        const params = new URLSearchParams({
          page: String(pageParam),
          pageSize: "6",
          booksPerShelf: "12",
        });
        const { data } = await api.get(`/books/home-shelves?${params}`);
        return data as {
          shelves: HomeShelf[];
          hasMore?: boolean;
          totalShelves?: number;
          page: number;
          rotationDay?: string;
        };
      } catch {
        return { shelves: [] as HomeShelf[], hasMore: false, page: pageParam as number };
      }
    },
    initialPageParam: 1,
    getNextPageParam: (last) => (last?.hasMore ? (last.page || 1) + 1 : undefined),
    staleTime: 6 * 60 * 60 * 1000,
    gcTime: 24 * 60 * 60 * 1000,
    refetchOnWindowFocus: false,
    retry: 1,
  });

  const carouselQueries = useMemo(() => {
    const shelves = (homeShelvesPages?.pages || []).flatMap((p) => p.shelves || []);
    return shelves.map((shelf) => ({
      slug: shelf.slug,
      name: shelf.listName || shelf.title || shelf.genre || shelf.slug,
      subtitle: shelf.source?.startsWith("hardcover") ? "Curated on Hardcover" : undefined,
      books: shelf.books || [],
      isLoading: homeShelvesLoading && shelves.length === 0,
    }));
  }, [homeShelvesPages, homeShelvesLoading]);

  useEffect(() => {
    const el = shelfSentinelRef.current;
    if (!el) return;
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting && hasNextPage && !isFetchingNextPage) {
          void fetchNextPage();
        }
      },
      { rootMargin: "800px 0px", threshold: 0 }
    );
    io.observe(el);
    return () => io.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  const personalizedShelves = personalizedData?.shelves || [];

  return (
    <div className="pb-12">
      <div className="flex px-4 lg:px-6 gap-6">
        {genresData && (
          <GenreSidebar
            genres={genresData.genres}
            mode="navigate"
            mobileOpen={genreMobileOpen}
            onMobileClose={onGenreMobileClose}
          />
        )}

        <div className="flex-1 min-w-0">
          <ContinueShelves />

          <div className="max-w-3xl mx-auto">
            <HeroSearch onGenreToggle={onGenreToggle} genreActiveCount={genreActiveCount} />
            <BadgeLegend />
          </div>

          {!privateMode && personalizedShelves.length > 0 && (
            <div className="mb-8 space-y-8">
              {personalizedShelves.map((shelf) => (
                <BookCarousel
                  key={shelf.id}
                  title={shelf.title}
                  subtitle={shelf.subtitle}
                  books={shelf.books || []}
                  isLoading={personalizedLoading}
                />
              ))}
            </div>
          )}

          <div className="mb-8">
            <BookCarousel
              title="Trending"
              books={trendingData?.books || []}
              isLoading={trendingLoading}
              to="/shelf/popular"
            />
          </div>

          <div className="mb-8">
            <BookCarousel
              title="New Releases"
              books={newReleasesData?.books || []}
              isLoading={newReleasesLoading}
              to="/shelf/new"
            />
          </div>

          <div className="space-y-8">
            {carouselQueries.map((cat) => (
              <BookCarousel
                key={cat.slug}
                title={cat.name}
                subtitle={cat.subtitle}
                books={cat.books}
                isLoading={cat.isLoading}
                to={`/shelf/${encodeURIComponent(cat.slug)}`}
              />
            ))}
          </div>
          <div ref={shelfSentinelRef} className="h-8" />
          {isFetchingNextPage && (
            <p className="text-sm text-gray-500 flex items-center justify-center gap-2 py-4">
              <Loader2 size={16} className="animate-spin" />
              Loading more lists…
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
