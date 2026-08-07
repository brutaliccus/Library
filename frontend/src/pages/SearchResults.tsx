import { useSearchParams, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useState, useEffect, useMemo, FormEvent } from "react";
import api from "../api/client";
import BookGrid from "../components/BookGrid";
import CacheReleaseCard, { type CacheReleaseCardData } from "../components/CacheReleaseCard";
import GenreSidebar from "../components/GenreSidebar";
import type { Genre } from "../components/GenreSidebar";
import {
  Search,
  ChevronLeft,
  ChevronRight,
  Library,
  BookOpen,
  Headphones,
  HardDrive,
  Loader2,
  SlidersHorizontal,
  Globe,
} from "lucide-react";
import type { BookSummary } from "../types/book";
import CoverImage from "../components/CoverImage";
import { usePlayer } from "../contexts/PlayerContext";
import { useToast } from "../contexts/ToastContext";
import MobileExpandableSearch from "../components/MobileExpandableSearch";
import { FLOATING_SEARCH_FILTER } from "../components/floatingSearchStyles";
import { libraryQueryKey } from "../utils/libraryQueryKeys";

interface LibrarySearchHit {
  title: string;
  author?: string;
  coverUrl?: string;
  source: "abs" | "kavita" | "rd";
  itemId?: string;
  libraryItemId?: number;
  seriesId?: number;
  chapterId?: number;
  fileKey?: string;
  fileName?: string;
  googleVolumeId?: string;
}

interface AbsShelfItem {
  itemId: string;
  title: string;
  author: string;
  coverUrl: string;
  genres?: string[];
  series?: Array<{ name: string }>;
  seriesName?: string;
  narrator?: string;
}

interface KavitaShelfItem {
  seriesId: number;
  title: string;
  author: string;
  coverUrl: string;
  chapterId: number | null;
  fileKey?: string | null;
  fileName?: string | null;
  volumeId?: number | null;
  genres?: string[];
  seriesName?: string;
  series?: Array<{ name: string }>;
}

interface RdShelfItem {
  id: number;
  googleVolumeId: string;
  title: string;
  author: string;
  coverUrl: string;
  genre?: string;
  seriesName?: string;
}

interface AaSearchHit {
  title: string;
  author?: string;
  size?: number;
  mediaType?: string;
  source?: string;
  aaMd5?: string;
  fileExtension?: string;
  formatInfo?: string;
  matchTier?: string;
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

function formatSize(bytes?: number): string {
  if (!bytes || bytes <= 0) return "";
  const gb = bytes / (1024 * 1024 * 1024);
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  return `${Math.round(bytes / (1024 * 1024))} MB`;
}

function SectionLoader({ label }: { label: string }) {
  return (
    <p className="text-sm text-gray-500 flex items-center gap-2 py-1">
      <Loader2 size={14} className="animate-spin shrink-0 text-brand-400" />
      {label}
    </p>
  );
}

interface Props {
  genreMobileOpen: boolean;
  onGenreMobileClose: () => void;
  onGenreToggle?: () => void;
  genreActiveCount?: number;
  onActiveCountChange: (count: number) => void;
}

export default function SearchResults({
  genreMobileOpen,
  onGenreMobileClose,
  onGenreToggle,
  genreActiveCount = 0,
  onActiveCountChange,
}: Props) {
  const navigate = useNavigate();
  const { toast } = useToast();
  const { nowPlaying, expanded } = usePlayer();
  const liftForMini = Boolean(nowPlaying && !expanded);
  const [searchParams, setSearchParams] = useSearchParams();
  const q = searchParams.get("q") || "";
  const categoryParam = searchParams.get("category") || "";
  // Default: show all catalog hits (cached / in-library / not-yet-cached).
  // Pass available=1 to filter to cached downloads only.
  const availableOnlyFilter = searchParams.get("available") === "1";
  const page = parseInt(searchParams.get("page") || "1", 10);
  const pageSize = 20;
  const [requestingAa, setRequestingAa] = useState<string | null>(null);

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

  const showTextSections = q.trim().length >= 2 && activeCategories.length === 0;

  // Subscribe to My Library shelf caches (enabled:false — never fetch from Store search).
  type AbsCollectionCache = {
    genres: Record<string, AbsShelfItem[]>;
    ungrouped: AbsShelfItem[];
  };
  const { data: absCollection } = useQuery<AbsCollectionCache>({
    queryKey: libraryQueryKey("abs-collection"),
    queryFn: () => Promise.reject(new Error("abs-collection is owned by MyLibrary")),
    enabled: false,
  });
  const { data: kavitaCollection } = useQuery<{ items: KavitaShelfItem[] }>({
    queryKey: libraryQueryKey("kavita-collection"),
    queryFn: () => Promise.reject(new Error("kavita-collection is owned by MyLibrary")),
    enabled: false,
  });
  const { data: rdLibrary } = useQuery<{ items: RdShelfItem[] }>({
    queryKey: libraryQueryKey("streaming-library"),
    queryFn: () => Promise.reject(new Error("streaming-library is owned by MyLibrary")),
    enabled: false,
  });

  const localLibraryHits = useMemo(() => {
    if (!showTextSections) return [] as LibrarySearchHit[];
    const tokens = q
      .trim()
      .toLowerCase()
      .split(/\s+/)
      .map((t) => t.trim())
      .filter(Boolean);
    if (!tokens.length) return [] as LibrarySearchHit[];

    const match = (...parts: Array<string | undefined | null | string[]>) => {
      const blob = parts
        .flatMap((p) => (Array.isArray(p) ? p : [p || ""]))
        .join(" ")
        .toLowerCase();
      return tokens.every((t) => blob.includes(t));
    };

    const out: LibrarySearchHit[] = [];

    if (absCollection) {
      const items = [
        ...Object.values(absCollection.genres || {}).flat(),
        ...(absCollection.ungrouped || []),
      ];
      const seen = new Set<string>();
      for (const item of items) {
        if (!item.itemId || seen.has(item.itemId)) continue;
        if (
          !match(
            item.title,
            item.author,
            item.narrator,
            item.seriesName,
            item.genres,
            ...(item.series || []).map((s) => s.name)
          )
        ) {
          continue;
        }
        seen.add(item.itemId);
        out.push({
          title: item.title,
          author: item.author,
          coverUrl: item.coverUrl,
          source: "abs",
          itemId: item.itemId,
        });
      }
    }

    if (kavitaCollection?.items) {
      const seen = new Set<string>();
      for (const item of kavitaCollection.items) {
        if (item.seriesId == null) continue;
        const key = `${item.seriesId}:${item.fileKey ?? item.chapterId ?? item.volumeId ?? "s"}`;
        if (seen.has(key)) continue;
        if (
          !match(
            item.title,
            item.author,
            item.seriesName,
            item.genres,
            ...(item.series || []).map((s) => s.name)
          )
        ) {
          continue;
        }
        seen.add(key);
        out.push({
          title: item.title,
          author: item.author,
          coverUrl: item.coverUrl || "",
          source: "kavita",
          seriesId: item.seriesId,
          chapterId: item.chapterId ?? undefined,
          fileKey: item.fileKey || undefined,
          fileName: item.fileName || undefined,
        });
      }
    }

    if (rdLibrary?.items) {
      for (const item of rdLibrary.items) {
        if (!match(item.title, item.author, item.genre, item.seriesName)) continue;
        out.push({
          title: item.title || "",
          author: item.author || "",
          coverUrl: item.coverUrl || "",
          source: "rd",
          libraryItemId: item.id,
          googleVolumeId: item.googleVolumeId,
        });
      }
    }

    return out;
  }, [showTextSections, q, absCollection, kavitaCollection, rdLibrary]);

  // Server enrichment — runs in parallel; local hits already shown instantly.
  const { data: libraryHits, isLoading: libraryServerLoading } = useQuery({
    queryKey: ["library-search-store", q],
    queryFn: async () => {
      const params = new URLSearchParams({ q: q.trim(), media: "all" });
      const { data } = await api.get(`/library/search?${params}`);
      return data as { results: LibrarySearchHit[] };
    },
    enabled: showTextSections,
    staleTime: 60 * 1000,
  });

  const mergedLibraryHits = useMemo(() => {
    const byKey = new Map<string, LibrarySearchHit>();
    const keyOf = (r: LibrarySearchHit) =>
      `${r.source}:${r.itemId || r.libraryItemId || `${r.seriesId ?? ""}:${r.fileKey || r.fileName || r.chapterId || ""}` || r.title}`;
    for (const r of localLibraryHits) byKey.set(keyOf(r), r);
    for (const r of libraryHits?.results || []) {
      const k = keyOf(r);
      const existing = byKey.get(k);
      if (!existing) {
        byKey.set(k, r);
        continue;
      }
      if (!existing.coverUrl && r.coverUrl) byKey.set(k, { ...existing, ...r, coverUrl: r.coverUrl });
    }
    return Array.from(byKey.values());
  }, [localLibraryHits, libraryHits]);

  const libraryLoading = localLibraryHits.length === 0 && libraryServerLoading;

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
    enabled: showTextSections,
    staleTime: 60 * 1000,
  });

  // Concurrent with cache/AA — do not wait for catalog to finish.
  const {
    data: abbReleases,
    isLoading: abbReleasesLoading,
    isFetching: abbFetching,
  } = useQuery({
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
    enabled: showTextSections,
    staleTime: 60 * 1000,
  });

  const { data: aaResults, isLoading: aaLoading, isFetching: aaFetching } = useQuery({
    queryKey: ["aa-store-search", q],
    queryFn: async () => {
      const params = new URLSearchParams({ q: q.trim() });
      const { data } = await api.get(`/search/annas-archive?${params}`);
      return data as { results: AaSearchHit[]; count: number };
    },
    enabled: showTextSections,
    staleTime: 60 * 1000,
  });

  const bookCount = Array.isArray(data?.books) ? data.books.length : 0;
  const cacheCount = cacheReleases?.releases?.length || 0;
  const cachedMatchesLoading = isLoading || cacheReleasesLoading;
  const abbBusy = abbReleasesLoading || abbFetching;
  const aaBusy = aaLoading || aaFetching;

  const searchProgress: string[] = [];
  if (showTextSections) {
    if (libraryLoading) searchProgress.push("Searching library…");
    if (cachedMatchesLoading) searchProgress.push("Checking cached matches…");
    if (abbBusy) searchProgress.push("Searching AudioBookBay…");
    if (aaBusy) searchProgress.push("Searching Anna's Archive…");
  } else if (isLoading) {
    searchProgress.push("Searching catalog…");
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

  const openLibraryHit = (r: LibrarySearchHit) => {
    if (r.source === "kavita" && r.seriesId) {
      const params = new URLSearchParams();
      if (r.chapterId != null) params.set("chapter", String(r.chapterId));
      const file = r.fileName || r.fileKey;
      if (file) params.set("file", file);
      const q = params.toString() ? `?${params.toString()}` : "";
      navigate(`/library/ebook/${r.seriesId}${q}`);
    } else if (r.source === "kavita" && r.chapterId) {
      navigate(`/read/${r.chapterId}`);
    } else if (r.source === "rd" && r.googleVolumeId) {
      navigate(`/book/${encodeURIComponent(r.googleVolumeId)}`);
    } else if (r.source === "abs" && r.itemId) {
      navigate(`/library/abs/${encodeURIComponent(r.itemId)}`);
    } else {
      navigate("/my-library");
    }
  };

  const requestAaHit = async (r: AaSearchHit) => {
    if (!r.aaMd5) return;
    setRequestingAa(r.aaMd5);
    try {
      await api.post("/requests", {
        title: r.title,
        author: r.author || undefined,
        indexer: "Anna's Archive",
        size_bytes: r.size || 0,
        media_type: r.mediaType || "ebook",
        source: "annas_archive",
        aa_md5: r.aaMd5,
        aa_file_extension: r.fileExtension || undefined,
      });
      const dest = r.mediaType === "audiobook" ? "Audiobookshelf" : "Kavita";
      toast(`Requested "${r.title}". It will be added to ${dest}.`, "success");
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Failed to create request";
      toast(detail, "error");
    } finally {
      setRequestingAa(null);
    }
  };

  const totalPages = data ? Math.ceil(data.totalItems / pageSize) : 0;
  const heading = buildHeading(q, activeCategories, genres);
  const aaHits = (aaResults?.results || []).slice(0, 12);

  return (
    <div
      className={`pt-2 lg:py-8 ${
        liftForMini
          ? "pb-[calc(4.5rem+env(safe-area-inset-bottom,0px))]"
          : "pb-[calc(5rem+env(safe-area-inset-bottom,0px))]"
      } lg:pb-8`}
    >
      <MobileExpandableSearch
        liftForMini={liftForMini}
        value={inputValue}
        onChange={setInputValue}
        onSubmit={handleSubmit}
        placeholder="Search by title, author, or ISBN..."
        ariaLabel="Search catalog"
        filterSlot={
          onGenreToggle ? (
            <button
              type="button"
              onClick={onGenreToggle}
              className={FLOATING_SEARCH_FILTER}
              title="Filter genres"
              aria-label="Filter genres"
            >
              <SlidersHorizontal size={18} />
              {genreActiveCount > 0 && (
                <span className="min-w-[1.1rem] px-1 py-0.5 bg-brand-600 text-white text-[10px] font-bold rounded-full leading-none">
                  {genreActiveCount}
                </span>
              )}
            </button>
          ) : null
        }
      />

      {/* Desktop search */}
      <div className="hidden lg:block mb-6 px-4">
        <form onSubmit={handleSubmit} className="relative max-w-2xl mx-auto">
          <Search
            size={20}
            className="absolute left-4 top-1/2 -translate-y-1/2 text-gray-500 pointer-events-none"
          />
          <input
            type="text"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            placeholder="Search by title, author, or ISBN..."
            className={`w-full pl-12 py-3.5 bg-gray-800 border border-gray-700 rounded-xl text-base text-gray-100 shadow-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent placeholder:text-gray-500 ${
              onGenreToggle ? "pr-36" : "pr-24"
            }`}
          />
          {onGenreToggle && (
            <button
              type="button"
              onClick={onGenreToggle}
              className="absolute right-[5.5rem] top-1/2 -translate-y-1/2 inline-flex items-center justify-center gap-1 p-2 rounded-xl text-gray-400 hover:text-gray-100 hover:bg-gray-700/80 transition-colors"
              title="Filter genres"
              aria-label="Filter genres"
            >
              <SlidersHorizontal size={18} />
              {genreActiveCount > 0 && (
                <span className="min-w-[1.1rem] px-1 py-0.5 bg-brand-600 text-white text-[10px] font-bold rounded-full leading-none">
                  {genreActiveCount}
                </span>
              )}
            </button>
          )}
          <button
            type="submit"
            className="absolute right-2 top-1/2 -translate-y-1/2 px-5 py-2 bg-brand-600 text-white text-sm font-semibold rounded-xl hover:bg-brand-500 transition-colors"
          >
            Search
          </button>
        </form>
      </div>

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

          {/* 1. Library — instant local + server enrich */}
          {showTextSections && (
            <section className="mb-8">
              <div className="flex items-center gap-2 mb-3">
                <Library size={18} className="text-brand-400" />
                <h2 className="text-lg font-semibold text-gray-100">Library</h2>
              </div>
              {libraryLoading ? (
                <SectionLoader label="Searching your library…" />
              ) : mergedLibraryHits.length ? (
                <div className="space-y-1 rounded-xl border border-gray-800 bg-gray-900/40 p-1">
                  {mergedLibraryHits.slice(0, 8).map((r, i) => (
                    <button
                      key={`${r.source}-${r.itemId || r.libraryItemId || `${r.seriesId ?? ""}:${r.fileKey || r.chapterId || i}`}`}
                      type="button"
                      onClick={() => openLibraryHit(r)}
                      className="w-full flex items-center gap-3 p-2.5 rounded-lg hover:bg-gray-800/80 transition-colors text-left"
                    >
                      <CoverImage
                        src={r.coverUrl || ""}
                        alt=""
                        className="w-9 h-12 rounded object-cover shrink-0"
                        fallback={
                          <div className="w-9 h-12 rounded bg-gray-800 shrink-0 flex items-center justify-center">
                            {r.source === "kavita" ? (
                              <BookOpen size={14} className="text-amber-400" />
                            ) : (
                              <Headphones size={14} className="text-emerald-400" />
                            )}
                          </div>
                        }
                      />
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
                <p className="text-sm text-gray-500">
                  No matches in Audiobookshelf, Kavita, or your collection.
                </p>
              )}
            </section>
          )}

          {/* 2. Cached Matches — catalog + unmatched indexer cache */}
          {showTextSections ? (
            <section className="mb-8">
              <div className="flex items-center gap-2 mb-3">
                <HardDrive size={18} className="text-amber-400" />
                <h2 className="text-lg font-semibold text-gray-100">Cached Matches</h2>
              </div>
              <p className="text-sm text-gray-500 mb-4">
                Catalog and indexer-cache matches for your search. Green download = ready to
                fetch; unmatched torrents appear below the catalog grid.
              </p>
              {cachedMatchesLoading && bookCount === 0 && cacheCount === 0 ? (
                <SectionLoader label="Searching cached matches…" />
              ) : (
                <>
                  {bookCount > 0 && (
                    <BookGrid books={data?.books || []} isLoading={false} />
                  )}
                  {cacheCount > 0 && (
                    <div className={`${bookCount > 0 ? "mt-6" : ""}`}>
                      {bookCount > 0 && (
                        <p className="text-xs text-gray-500 mb-3 uppercase tracking-wide">
                          Unmatched cached releases
                        </p>
                      )}
                      <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-7 lg:grid-cols-9 xl:grid-cols-11 gap-2">
                        {cacheReleases!.releases.map((r) => (
                          <CacheReleaseCard key={r.id} release={r} />
                        ))}
                      </div>
                    </div>
                  )}
                  {!cachedMatchesLoading && bookCount === 0 && cacheCount === 0 && (
                    <p className="text-sm text-gray-500">No cached catalog or indexer matches.</p>
                  )}
                  {isLoading && bookCount > 0 && (
                    <SectionLoader label="Updating catalog…" />
                  )}
                  {cacheReleasesLoading && cacheCount === 0 && bookCount > 0 && (
                    <div className="mt-3">
                      <SectionLoader label="Checking indexer cache…" />
                    </div>
                  )}
                </>
              )}
              {totalPages > 1 && bookCount > 0 && (
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
            </section>
          ) : (
            <>
              {isLoading ? (
                <SectionLoader label="Searching catalog…" />
              ) : (
                <BookGrid books={data?.books || []} isLoading={false} />
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
            </>
          )}

          {/* 3. AudioBook Bay — concurrent */}
          {showTextSections && (
            <section className="mt-10 mb-4">
              <div className="flex items-center gap-2 mb-3">
                <Headphones size={18} className="text-sky-400" />
                <h2 className="text-lg font-semibold text-gray-100">AudioBook Bay</h2>
              </div>
              <p className="text-sm text-gray-500 mb-4">
                Live AudioBookBay results for audiobook releases.
              </p>
              {abbBusy && !(abbReleases?.releases?.length) ? (
                <SectionLoader label="Searching AudioBookBay…" />
              ) : abbReleases?.releases?.length ? (
                <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-7 lg:grid-cols-9 xl:grid-cols-11 gap-2">
                  {abbReleases.releases.map((r) => (
                    <CacheReleaseCard key={`abb-${r.id}`} release={r} />
                  ))}
                </div>
              ) : abbReleases?.timedOut ? (
                <p className="text-sm text-amber-500/90">
                  AudioBookBay timed out — try again, or open Find Downloads from a close catalog
                  match.
                </p>
              ) : (
                <p className="text-sm text-gray-500">No AudioBookBay hits for this search.</p>
              )}
            </section>
          )}

          {/* 4. Anna's Archive — concurrent */}
          {showTextSections && (
            <section className="mt-10 mb-4">
              <div className="flex items-center gap-2 mb-3">
                <Globe size={18} className="text-violet-400" />
                <h2 className="text-lg font-semibold text-gray-100">Anna&apos;s Archive</h2>
              </div>
              <p className="text-sm text-gray-500 mb-4">
                Direct ebook and file matches from Anna&apos;s Archive.
              </p>
              {aaBusy && aaHits.length === 0 ? (
                <SectionLoader label="Searching Anna's Archive…" />
              ) : aaHits.length ? (
                <div className="space-y-1 rounded-xl border border-gray-800 bg-gray-900/40 p-1">
                  {aaHits.map((r, i) => (
                    <div
                      key={`${r.aaMd5 || r.title}-${i}`}
                      className="flex items-center gap-3 p-2.5 rounded-lg hover:bg-gray-800/80 transition-colors"
                    >
                      <div className="w-9 h-12 rounded bg-gray-800 shrink-0 flex items-center justify-center">
                        <BookOpen size={14} className="text-violet-400" />
                      </div>
                      <div className="flex-1 min-w-0">
                        <p className="text-sm text-gray-100 truncate">{r.title}</p>
                        <p className="text-xs text-gray-500 truncate">
                          {[r.author, r.fileExtension?.toUpperCase(), formatSize(r.size)]
                            .filter(Boolean)
                            .join(" · ")}
                        </p>
                      </div>
                      <button
                        type="button"
                        disabled={!r.aaMd5 || requestingAa === r.aaMd5}
                        onClick={() => void requestAaHit(r)}
                        className="shrink-0 px-2.5 py-1.5 text-xs font-medium rounded-lg bg-brand-600/90 text-white hover:bg-brand-500 disabled:opacity-40 transition-colors"
                      >
                        {requestingAa === r.aaMd5 ? (
                          <Loader2 size={12} className="animate-spin" />
                        ) : (
                          "Request"
                        )}
                      </button>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-gray-500">No Anna&apos;s Archive hits for this search.</p>
              )}
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
