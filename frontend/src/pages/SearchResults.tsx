import { useSearchParams, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useState, useEffect, FormEvent } from "react";
import api from "../api/client";
import BookGrid from "../components/BookGrid";
import CacheReleaseCard, { type CacheReleaseCardData } from "../components/CacheReleaseCard";
import GenreSidebar from "../components/GenreSidebar";
import type { Genre } from "../components/GenreSidebar";
import { Search, ChevronLeft, ChevronRight, Library, BookOpen, Headphones, HardDrive, Loader2 } from "lucide-react";
import type { BookSummary } from "../types/book";

interface LibrarySearchHit {
  title: string;
  author?: string;
  coverUrl?: string;
  source: "abs" | "kavita" | "rd";
  itemId?: string;
  chapterId?: number;
  googleVolumeId?: string;
}

function buildSearchQuery(q: string, categories: string[]): string {
  if (categories.length === 0) return q;

  if (categories.length === 1) {
    const c = categories[0];
    if (c === "all" || c === "available") return "__available__";
    if (c === "popular") return "__popular__";
    if (c === "new") return "__new__";
    return `__genre__:${c}`;
  }

  return categories.map((c) => `__genre__:${c}`).join("+");
}

function findGenreName(slug: string, genres: Genre[]): string {
  for (const g of genres) {
    if (g.slug === slug) return g.name;
    for (const c of g.children) {
      if (c.slug === slug) return c.name;
    }
  }
  if (slug === "all") return "All Books";
  if (slug === "available") return "Available to Download";
  if (slug === "popular") return "Popular Books";
  if (slug === "new") return "New Releases";
  return slug;
}

function buildHeading(q: string, categories: string[], genres: Genre[]): string {
  if (categories.length === 0) return q ? `Results for "${q}"` : "";
  if (categories.length === 1) {
    const slug = categories[0];
    if (slug === "all") return "All Books";
    if (slug === "available") return "Available to Download";
    if (slug === "popular") return "Popular Books";
    if (slug === "new") return "New Releases";
    return findGenreName(slug, genres);
  }
  return categories.map((s) => findGenreName(s, genres)).join(", ");
}

interface Props {
  genreMobileOpen: boolean;
  onGenreMobileClose: () => void;
  onActiveCountChange: (count: number) => void;
}

export default function SearchResults({ genreMobileOpen, onGenreMobileClose, onActiveCountChange }: Props) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const q = searchParams.get("q") || "";
  const categoryParam = searchParams.get("category") || "";
  // Default: show all catalog hits (cached / in-library / not-yet-cached).
  // Pass available=1 to filter to cached downloads only.
  const availableOnlyFilter = searchParams.get("available") === "1";
  const page = parseInt(searchParams.get("page") || "1", 10);
  const pageSize = 20;

  const activeCategories = categoryParam ? categoryParam.split(",").filter(Boolean) : [];

  useEffect(() => {
    onActiveCountChange(activeCategories.length);
  }, [activeCategories.length, onActiveCountChange]);

  const [inputValue, setInputValue] = useState(q);

  useEffect(() => {
    setInputValue(q);
  }, [q]);

  const { data: genresData } = useQuery({
    queryKey: ["genres"],
    queryFn: async () => {
      const { data } = await api.get("/books/genres");
      return data as { genres: Genre[] };
    },
  });

  const genres = genresData?.genres || [];
  const searchQuery = buildSearchQuery(q, activeCategories);

  const showLibrarySection = q.trim().length >= 2 && activeCategories.length === 0;

  const { data: libraryHits, isLoading: libraryLoading } = useQuery({
    queryKey: ["library-search-store", q],
    queryFn: async () => {
      const params = new URLSearchParams({ q: q.trim(), media: "all" });
      const { data } = await api.get(`/library/search?${params}`);
      return data as { results: LibrarySearchHit[] };
    },
    enabled: showLibrarySection,
    staleTime: 60 * 1000,
  });

  const isCategoryBrowse = activeCategories.length > 0 && !q.trim();
  const isAvailableBrowse =
    activeCategories.includes("available") || activeCategories.includes("all");

  const { data, isLoading } = useQuery({
    queryKey: ["book-search", searchQuery, page, availableOnlyFilter, isCategoryBrowse, isAvailableBrowse],
    queryFn: async (): Promise<{ books: BookSummary[]; totalItems: number; page: number; source?: string } | null> => {
      if (!searchQuery) return null;

      if (searchQuery === "__available__" || isAvailableBrowse) {
        const params = new URLSearchParams({
          page: page.toString(),
          pageSize: pageSize.toString(),
        });
        const { data } = await api.get(`/books/available?${params.toString()}`);
        return data as { books: BookSummary[]; totalItems: number; page: number; source?: string };
      }

      const availableOnly = availableOnlyFilter;

      if (isCategoryBrowse || searchQuery.includes("+")) {
        const params = new URLSearchParams({
          q: searchQuery,
          page: page.toString(),
          pageSize: pageSize.toString(),
          available_only: String(availableOnly),
        });
        const { data } = await api.get(`/books/search?${params.toString()}`);
        return data as { books: BookSummary[]; totalItems: number; page: number; source?: string };
      }

      const params = new URLSearchParams({
        q: searchQuery,
        page: page.toString(),
        pageSize: pageSize.toString(),
        available_only: String(availableOnly),
      });
      const { data } = await api.get(`/books/search?${params.toString()}`);
      return data as { books: BookSummary[]; totalItems: number; page: number };
    },
    enabled: searchQuery.length >= 1,
  });

  const showCacheReleases = q.trim().length >= 2 && activeCategories.length === 0;

  const { data: cacheReleases, isLoading: cacheReleasesLoading } = useQuery({
    queryKey: ["cache-releases", q],
    queryFn: async () => {
      const params = new URLSearchParams({
        q: q.trim(),
        limit: "24",
        unmatched_only: "true",
      });
      const { data } = await api.get(`/search/cache-releases?${params}`);
      return data as { releases: CacheReleaseCardData[]; count: number };
    },
    enabled: showCacheReleases,
    staleTime: 60 * 1000,
  });

  const bookCount = Array.isArray(data?.books) ? data.books.length : 0;

  const catalogEmpty =
    showCacheReleases &&
    !isLoading &&
    Boolean(data) &&
    bookCount === 0;

  // Only hit live ABB after catalog truly returns empty — never while OL is
  // still loading (that was stacking Jackett/Flare work on top of catalog work).
  const { data: abbReleases, isLoading: abbReleasesLoading, isFetching: abbFetching } = useQuery({
    queryKey: ["abb-releases", q],
    queryFn: async () => {
      const params = new URLSearchParams({ q: q.trim(), limit: "24" });
      const { data } = await api.get(`/search/abb-releases?${params}`);
      return data as {
        releases: CacheReleaseCardData[];
        count: number;
        source?: string;
        timedOut?: boolean;
      };
    },
    enabled: catalogEmpty,
    staleTime: 60 * 1000,
  });

  const abbEnabled = catalogEmpty;
  const searchProgress: string[] = [];
  if (isLoading) searchProgress.push("Searching catalog…");
  if (cacheReleasesLoading) searchProgress.push("Checking indexer cache…");
  if (abbEnabled && (abbReleasesLoading || abbFetching)) {
    searchProgress.push("Searching AudioBookBay…");
  }

  const toggleAvailableFilter = () => {
    const params: Record<string, string> = {};
    if (q) params.q = q;
    if (categoryParam) params.category = categoryParam;
    // Default is full catalog; available=1 limits to cached downloads.
    if (!availableOnlyFilter) {
      params.available = "1";
    }
    setSearchParams(params);
  };

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = inputValue.trim();
    if (trimmed.length >= 2) {
      const params: Record<string, string> = { q: trimmed };
      if (availableOnlyFilter) params.available = "1";
      if (categoryParam) params.category = categoryParam;
      setSearchParams(params);
    }
  };

  const handleGenreSelect = (slugs: string[]) => {
    if (slugs.length === 0) {
      const params: Record<string, string> = {};
      if (q) params.q = q;
      if (availableOnlyFilter) params.available = "1";
      setSearchParams(params);
    } else {
      const params: Record<string, string> = { category: slugs.join(",") };
      if (q) params.q = q;
      if (availableOnlyFilter) params.available = "1";
      setSearchParams(params);
    }
  };

  const goToPage = (p: number) => {
    const params: Record<string, string> = { page: p.toString() };
    if (q) params.q = q;
    if (categoryParam) params.category = categoryParam;
    if (availableOnlyFilter) params.available = "1";
    setSearchParams(params);
    window.scrollTo(0, 0);
  };

  const totalPages = data ? Math.ceil(data.totalItems / pageSize) : 0;
  const heading = buildHeading(q, activeCategories, genres);

  return (
    <div className="py-8">
      <form onSubmit={handleSubmit} className="relative max-w-2xl mx-auto mb-6 px-4 lg:px-6">
        <Search
          size={20}
          className="absolute left-8 lg:left-10 top-1/2 -translate-y-1/2 text-gray-500"
        />
        <input
          type="text"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          placeholder="Search by title, author, or ISBN..."
          className="w-full pl-12 pr-24 py-3.5 bg-gray-800 border border-gray-700 rounded-xl text-base text-gray-100 shadow-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent placeholder:text-gray-500"
        />
        <button
          type="submit"
          className="absolute right-6 lg:right-8 top-1/2 -translate-y-1/2 px-5 py-2 bg-brand-600 text-white text-sm font-semibold rounded-xl hover:bg-brand-500 transition-colors"
        >
          Search
        </button>
      </form>

      <div className="flex px-4 lg:px-6 gap-6">
        {genres.length > 0 && (
          <GenreSidebar
            genres={genres}
            activeSlugs={activeCategories}
            onSelect={handleGenreSelect}
            mobileOpen={genreMobileOpen}
            onMobileClose={onGenreMobileClose}
          />
        )}

        <div className="flex-1 min-w-0">
          {heading && <h1 className="text-2xl font-bold text-gray-100 mb-6">{heading}</h1>}

          {searchProgress.length > 0 && (
            <div className="mb-4 flex items-start gap-2 rounded-lg border border-gray-800 bg-gray-900/50 px-3 py-2 text-sm text-gray-300">
              <Loader2 size={16} className="mt-0.5 shrink-0 animate-spin text-brand-400" />
              <div className="space-y-0.5">
                {searchProgress.map((line) => (
                  <p key={line}>{line}</p>
                ))}
              </div>
            </div>
          )}

          {(q || activeCategories.length > 0) && (
            <div className="flex flex-wrap items-center gap-3 mb-4 text-sm">
              {availableOnlyFilter ? (
                <span className="text-gray-400">
                  Showing books with downloads in our indexer cache.
                  {data?.books?.length === 0 && " The scraper is still building matches — check back soon."}
                </span>
              ) : (
                <span className="text-gray-400">
                  Showing catalog matches: green download = cached, green check = in library,
                  yellow ? = not yet cached.
                </span>
              )}
              {q.trim() && (
                <button
                  type="button"
                  onClick={toggleAvailableFilter}
                  className="text-brand-400 hover:text-brand-300 font-medium"
                >
                  {availableOnlyFilter ? "Show full catalog" : "Available downloads only"}
                </button>
              )}
            </div>
          )}

          {showLibrarySection && (
            <section className="mb-8">
              <div className="flex items-center gap-2 mb-3">
                <Library size={18} className="text-brand-400" />
                <h2 className="text-lg font-semibold text-gray-100">In your library</h2>
              </div>
              {libraryLoading ? (
                <p className="text-sm text-gray-500">Searching your library...</p>
              ) : libraryHits?.results?.length ? (
                <div className="space-y-1 rounded-xl border border-gray-800 bg-gray-900/40 p-1">
                  {libraryHits.results.slice(0, 8).map((r, i) => (
                    <button
                      key={`${r.source}-${r.itemId || r.chapterId || i}`}
                      type="button"
                      onClick={() => {
                        if (r.source === "kavita" && r.chapterId) {
                          navigate(`/read/${r.chapterId}`);
                        } else if (r.source === "rd" && r.googleVolumeId) {
                          navigate(`/book/${encodeURIComponent(r.googleVolumeId)}`);
                        } else if (r.source === "abs" && r.itemId) {
                          navigate(`/library/abs/${encodeURIComponent(r.itemId)}`);
                        } else {
                          navigate("/my-library");
                        }
                      }}
                      className="w-full flex items-center gap-3 p-2.5 rounded-lg hover:bg-gray-800/80 transition-colors text-left"
                    >
                      {r.coverUrl ? (
                        <img src={r.coverUrl} alt="" className="w-9 h-12 rounded object-cover shrink-0" />
                      ) : (
                        <div className="w-9 h-12 rounded bg-gray-800 shrink-0 flex items-center justify-center">
                          {r.source === "kavita" ? (
                            <BookOpen size={14} className="text-amber-400" />
                          ) : (
                            <Headphones size={14} className="text-emerald-400" />
                          )}
                        </div>
                      )}
                      <div className="flex-1 min-w-0">
                        <p className="text-sm text-gray-100 truncate">{r.title}</p>
                        {r.author && <p className="text-xs text-gray-500 truncate">{r.author}</p>}
                      </div>
                      <span className="text-[10px] uppercase tracking-wide text-gray-500 shrink-0">
                        {r.source === "kavita" ? "Ebook" : r.source === "abs" ? "Audio" : "Collection"}
                      </span>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-gray-500">No matches in Audiobookshelf, Kavita, or your collection.</p>
              )}
            </section>
          )}

          <BookGrid
            books={data?.books || []}
            isLoading={isLoading}
            skeletonCount={pageSize}
          />

          {showCacheReleases && (
            <section className="mt-10 mb-4">
              <div className="flex items-center gap-2 mb-3">
                <HardDrive size={18} className="text-amber-400" />
                <h2 className="text-lg font-semibold text-gray-100">Cached releases</h2>
              </div>
              <p className="text-sm text-gray-500 mb-4">
                Indexer torrents matching your search — including titles without an Open Library
                catalog match. Open a card to download or stream.
              </p>
              {cacheReleasesLoading ? (
                <p className="text-sm text-gray-500">Searching indexer cache...</p>
              ) : cacheReleases?.releases?.length ? (
                <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-7 lg:grid-cols-9 xl:grid-cols-11 gap-2">
                  {cacheReleases.releases.map((r) => (
                    <CacheReleaseCard key={r.id} release={r} />
                  ))}
                </div>
              ) : (
                <p className="text-sm text-gray-500">No unmatched cached releases for this search.</p>
              )}
            </section>
          )}

          {(catalogEmpty || (abbEnabled && (abbReleases?.releases?.length || abbReleasesLoading || abbFetching))) && (
            <section className="mt-10 mb-4">
              <div className="flex items-center gap-2 mb-3">
                <Headphones size={18} className="text-sky-400" />
                <h2 className="text-lg font-semibold text-gray-100">AudioBookBay results</h2>
              </div>
              <p className="text-sm text-gray-500 mb-4">
                {catalogEmpty
                  ? "No Open Library matches — searching AudioBookBay for niche titles."
                  : "Also checking AudioBookBay while the catalog search finishes…"}
              </p>
              {abbReleasesLoading || abbFetching ? (
                <p className="text-sm text-gray-500 flex items-center gap-2">
                  <Loader2 size={14} className="animate-spin" />
                  Searching AudioBookBay (usually under ~20s)…
                </p>
              ) : abbReleases?.releases?.length ? (
                <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-7 lg:grid-cols-9 xl:grid-cols-11 gap-2">
                  {abbReleases.releases.map((r) => (
                    <CacheReleaseCard key={`abb-${r.id}`} release={r} />
                  ))}
                </div>
              ) : abbReleases?.timedOut ? (
                <p className="text-sm text-amber-500/90">
                  AudioBookBay timed out — try again, or open Find Downloads from a close catalog match.
                </p>
              ) : catalogEmpty ? (
                <p className="text-sm text-gray-500">No AudioBookBay hits for this search.</p>
              ) : null}
            </section>
          )}

          {totalPages > 1 && (
            <div className="flex items-center justify-center gap-4 mt-8">
              <button
                onClick={() => goToPage(page - 1)}
                disabled={page <= 1}
                className="flex items-center gap-1 px-4 py-2 bg-gray-800 text-gray-300 rounded-lg hover:bg-gray-700 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              >
                <ChevronLeft size={16} />
                Previous
              </button>
              <span className="text-sm text-gray-400">
                Page {page} of {totalPages}
              </span>
              <button
                onClick={() => goToPage(page + 1)}
                disabled={page >= totalPages}
                className="flex items-center gap-1 px-4 py-2 bg-gray-800 text-gray-300 rounded-lg hover:bg-gray-700 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              >
                Next
                <ChevronRight size={16} />
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
