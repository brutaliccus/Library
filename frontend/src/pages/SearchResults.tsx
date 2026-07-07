import { useSearchParams, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useState, useEffect, FormEvent } from "react";
import api from "../api/client";
import BookGrid from "../components/BookGrid";
import GenreSidebar from "../components/GenreSidebar";
import type { Genre } from "../components/GenreSidebar";
import { Search, ChevronLeft, ChevronRight, Library, BookOpen, Headphones } from "lucide-react";
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
    if (c === "all") return "subject:fiction";
    if (c === "available") return "__available__";
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
  const advancedSearch = searchParams.get("advanced") === "1";
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
  const isAvailableBrowse = activeCategories.includes("available");
  const isBrowseAll =
    activeCategories.includes("all") ||
    activeCategories.includes("popular") ||
    activeCategories.includes("new");

  const { data, isLoading } = useQuery({
    queryKey: ["book-search", searchQuery, page, advancedSearch, isCategoryBrowse, isBrowseAll, isAvailableBrowse],
    queryFn: async () => {
      if (!searchQuery) return null;

      if (searchQuery === "__available__" || isAvailableBrowse) {
        const params = new URLSearchParams({
          page: page.toString(),
          pageSize: pageSize.toString(),
        });
        const { data } = await api.get(`/books/available?${params.toString()}`);
        return data as { books: BookSummary[]; totalItems: number; page: number; source?: string };
      }

      // Category shelves named "All" / Popular / New → full Google catalog
      if (isBrowseAll || advancedSearch) {
        const params = new URLSearchParams({
          q: searchQuery,
          page: page.toString(),
          pageSize: pageSize.toString(),
          available_only: "false",
        });
        const { data } = await api.get(`/books/search?${params.toString()}`);
        return data as { books: BookSummary[]; totalItems: number; page: number; source?: string };
      }

      // Genre/category browse → try available filter (backend falls back to full shelf if empty)
      if (isCategoryBrowse) {
        const params = new URLSearchParams({
          q: searchQuery,
          page: page.toString(),
          pageSize: pageSize.toString(),
          available_only: "true",
        });
        const { data } = await api.get(`/books/search?${params.toString()}`);
        return data as { books: BookSummary[]; totalItems: number; page: number; source?: string };
      }

      // Free-text search → default to available-only unless advanced
      const availableOnly = !advancedSearch;

      if (searchQuery.includes("+")) {
        const parts = searchQuery.split("+").filter(Boolean);
        const slugs = parts.map((p) => p.replace("__genre__:", ""));
        const firstSlug = slugs[0];
        const params = new URLSearchParams({
          q: `__genre__:${firstSlug}`,
          page: page.toString(),
          pageSize: pageSize.toString(),
          available_only: String(availableOnly),
        });
        const { data } = await api.get(`/books/search?${params.toString()}`);
        return data as { books: BookSummary[]; totalItems: number; page: number };
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

  const toggleAdvancedSearch = () => {
    const params: Record<string, string> = {};
    if (q) params.q = q;
    if (categoryParam) params.category = categoryParam;
    if (!advancedSearch) params.advanced = "1";
    setSearchParams(params);
  };

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = inputValue.trim();
    if (trimmed.length >= 2) {
      setSearchParams({ q: trimmed });
    }
  };

  const handleGenreSelect = (slugs: string[]) => {
    if (slugs.length === 0) {
      setSearchParams(q ? { q } : {});
    } else {
      setSearchParams({ category: slugs.join(",") });
    }
  };

  const goToPage = (p: number) => {
    const params: Record<string, string> = { page: p.toString() };
    if (q) params.q = q;
    if (categoryParam) params.category = categoryParam;
    setSearchParams(params);
    window.scrollTo(0, 0);
  };

  const totalPages = data ? Math.ceil(Math.min(data.totalItems, 200) / pageSize) : 0;
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

          {(q || activeCategories.length > 0) && (
            <div className="flex flex-wrap items-center gap-3 mb-4 text-sm">
              {isBrowseAll || advancedSearch ? (
                <span className="text-gray-400">
                  Showing full catalog from Google Books.
                </span>
              ) : isAvailableBrowse ? (
                <span className="text-gray-400">
                  Books with downloads ready in our indexer cache.
                </span>
              ) : isCategoryBrowse ? (
                <span className="text-gray-400">
                  Showing books with downloads in our indexer cache.
                  {data?.books?.length === 0 && " The scraper is still building — try All or Advanced search."}
                </span>
              ) : (
                <span className="text-gray-400">
                  Showing books available to download from our indexers.
                </span>
              )}
              {!isBrowseAll && (
                <button
                  type="button"
                  onClick={toggleAdvancedSearch}
                  className="text-brand-400 hover:text-brand-300 font-medium"
                >
                  {advancedSearch ? "Show available only" : "Advanced search (full catalog)"}
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
