import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import api from "../api/client";
import { usePlayer } from "../contexts/PlayerContext";
import { useToast } from "../contexts/ToastContext";
import HeroSearch from "../components/HeroSearch";
import BookCarousel from "../components/BookCarousel";
import GenreSidebar from "../components/GenreSidebar";
import type { Genre } from "../components/GenreSidebar";
import ContinueItemMenu, { type ContinueMenuTarget } from "../components/ContinueItemMenu";
import { useLongPress } from "../hooks/useLongPress";
import { Headphones, Radio, BookOpen } from "lucide-react";
import {
  getContinueReading,
  clearProgress as clearReadingProgress,
  hideFromContinueReading,
} from "../utils/readingProgress";
import { clearBookCache, clearAbsBookCache } from "../utils/audioCache";
import type { BookSummary } from "../types/book";

/** Cover tile that supports tap-to-open plus long-press / right-click for the context menu. */
function ContinueTile({
  onClick,
  onLongPress,
  coverUrl,
  alt,
  ringClass,
  fallbackIcon,
}: {
  onClick: () => void;
  onLongPress: (point: { x: number; y: number }) => void;
  coverUrl: string;
  alt: string;
  ringClass: string;
  fallbackIcon: ReactNode;
}) {
  const longPressProps = useLongPress(onLongPress);
  return (
    <button
      onClick={onClick}
      {...longPressProps}
      className={`aspect-[2/3] rounded-lg overflow-hidden bg-gray-800/60 hover:ring-2 ${ringClass} transition-all group select-none`}
      style={{ WebkitTouchCallout: "none" }}
    >
      {coverUrl ? (
        <img
          src={coverUrl}
          alt={alt}
          draggable={false}
          className="w-full h-full object-cover group-hover:scale-105 transition-transform"
        />
      ) : (
        <div className="w-full h-full flex items-center justify-center">{fallbackIcon}</div>
      )}
    </button>
  );
}

function buildCategoryUrl(slugs: string[]): string {
  if (slugs.length === 0) return "/search";
  return `/search?category=${slugs.join(",")}`;
}

const HOME_CAROUSELS = [
  { slug: "fantasy", name: "Fantasy" },
  { slug: "science-fiction", name: "Science Fiction" },
  { slug: "mystery", name: "Mystery" },
  { slug: "thriller", name: "Thriller" },
  { slug: "romance", name: "Romance" },
  { slug: "horror", name: "Horror" },
];

interface InProgressItem {
  itemId: string;
  title: string;
  author: string;
  coverUrl: string;
  progress: number;
  currentTime: number;
  duration: number;
  isFinished: boolean;
}

interface RDHistoryItem {
  id: number;
  title: string;
  author: string;
  coverUrl: string;
  progressSeconds: number;
  totalSeconds: number;
  currentTrackIndex: number;
  trackPositionSeconds: number;
  status: string;
  tracks: Array<{
    index: number; title: string; contentUrl: string; mimeType: string;
    startOffset: number; duration: number;
  }>;
}

interface Props {
  genreMobileOpen: boolean;
  onGenreMobileClose: () => void;
  onActiveCountChange: (count: number) => void;
}

export default function Home({ genreMobileOpen, onGenreMobileClose, onActiveCountChange }: Props) {
  const navigate = useNavigate();
  const { playABS, playRD } = usePlayer();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [continueReading, setContinueReading] = useState(() => getContinueReading(6));
  const [menuTarget, setMenuTarget] = useState<ContinueMenuTarget | null>(null);

  const openMenu = useCallback(
    (base: Omit<ContinueMenuTarget, "anchorX" | "anchorY">, point: { x: number; y: number }) => {
      setMenuTarget({ ...base, anchorX: point.x, anchorY: point.y });
    },
    []
  );

  const handleMenuClearProgress = useCallback(
    async (target: ContinueMenuTarget) => {
      setMenuTarget(null);
      try {
        if (target.kind === "abs") {
          await api.post(`/stream/abs/${encodeURIComponent(String(target.id))}/clear-progress`);
          void clearAbsBookCache(String(target.id));
          queryClient.invalidateQueries({ queryKey: ["in-progress"] });
        } else if (target.kind === "rd") {
          await api.post(`/stream/rd/history/${target.id}/clear-progress`);
          void clearBookCache("h", Number(target.id));
          queryClient.invalidateQueries({ queryKey: ["rd-in-progress"] });
        } else {
          clearReadingProgress(Number(target.id));
          setContinueReading(getContinueReading(6));
        }
        toast(`Progress cleared for "${target.title}"`, "success");
      } catch (err: unknown) {
        const msg =
          (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
          "Failed to clear progress";
        toast(msg, "error");
      }
    },
    [queryClient, toast]
  );

  const handleMenuHide = useCallback(
    async (target: ContinueMenuTarget) => {
      setMenuTarget(null);
      try {
        if (target.kind === "abs") {
          await api.post(`/stream/abs/${encodeURIComponent(String(target.id))}/hide`);
          queryClient.invalidateQueries({ queryKey: ["in-progress"] });
        } else if (target.kind === "rd") {
          await api.post(`/stream/rd/history/${target.id}/hide`);
          queryClient.invalidateQueries({ queryKey: ["rd-in-progress"] });
        } else {
          hideFromContinueReading(Number(target.id));
          setContinueReading(getContinueReading(6));
        }
        toast(`"${target.title}" hidden — progress kept`, "info");
      } catch {
        toast("Failed to hide item", "error");
      }
    },
    [queryClient, toast]
  );

  useEffect(() => { onActiveCountChange(0); }, [onActiveCountChange]);

  useEffect(() => {
    const refresh = () => setContinueReading(getContinueReading(6));
    window.addEventListener("storage", refresh);
    window.addEventListener("ereader-progress-updated", refresh);
    return () => {
      window.removeEventListener("storage", refresh);
      window.removeEventListener("ereader-progress-updated", refresh);
    };
  }, []);

  const { data: inProgressData } = useQuery({
    queryKey: ["in-progress"],
    queryFn: async () => {
      const { data } = await api.get("/stream/abs/in-progress");
      return data as { items: InProgressItem[] };
    },
  });

  const { data: rdInProgressData } = useQuery({
    queryKey: ["rd-in-progress"],
    queryFn: async () => {
      const { data } = await api.get("/stream/rd/history/in-progress");
      return data as { items: RDHistoryItem[] };
    },
  });

  const listeningItems = inProgressData?.items?.filter((i) => !i.isFinished) || [];
  const rdListeningItems = rdInProgressData?.items || [];

  const { data: genresData } = useQuery({
    queryKey: ["genres"],
    queryFn: async () => {
      const { data } = await api.get("/books/genres");
      return data as { genres: Genre[] };
    },
  });

  const { data: trendingData, isLoading: trendingLoading } = useQuery({
    queryKey: ["trending-books"],
    queryFn: async () => {
      const { data } = await api.get("/books/trending");
      return data as { books: BookSummary[] };
    },
    staleTime: 30 * 60 * 1000,
    gcTime: 60 * 60 * 1000,
    refetchOnWindowFocus: false,
  });

  const { data: newReleasesData, isLoading: newReleasesLoading } = useQuery({
    queryKey: ["new-releases"],
    queryFn: async () => {
      const { data } = await api.get("/books/new-releases");
      return data as { books: BookSummary[] };
    },
    staleTime: 30 * 60 * 1000,
    gcTime: 60 * 60 * 1000,
    refetchOnWindowFocus: false,
  });

  const carouselQueries = HOME_CAROUSELS.map((cat) => {
    const { data, isLoading } = useQuery({
      queryKey: ["category-carousel", cat.slug],
      queryFn: async () => {
        const { data } = await api.get(`/books/category/${cat.slug}?pageSize=20`);
        return data as { books: BookSummary[] };
      },
      staleTime: 30 * 60 * 1000,
      gcTime: 60 * 60 * 1000,
      refetchOnWindowFocus: false,
    });
    return { ...cat, books: data?.books || [], isLoading };
  });

  return (
    <div className="pb-12">
      <ContinueItemMenu
        target={menuTarget}
        onClose={() => setMenuTarget(null)}
        onClearProgress={handleMenuClearProgress}
        onHide={handleMenuHide}
      />
      <div className="max-w-3xl mx-auto px-4 lg:px-6">
        <HeroSearch />
      </div>

      <div className="flex px-4 lg:px-6 gap-6">
        {genresData && (
          <GenreSidebar
            genres={genresData.genres}
            onSelect={(slugs) => navigate(buildCategoryUrl(slugs))}
            mobileOpen={genreMobileOpen}
            onMobileClose={onGenreMobileClose}
          />
        )}

        <div className="flex-1 min-w-0">
          {continueReading.length > 0 && (
            <section className="mb-8">
              <h2 className="flex items-center gap-2 text-lg font-semibold text-gray-100 mb-3">
                <BookOpen size={18} className="text-amber-400" />
                Continue Reading
              </h2>
              <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-6 lg:grid-cols-8 xl:grid-cols-10 gap-2">
                {continueReading.map((item) => (
                  <ContinueTile
                    key={`ebook-${item.chapterId}`}
                    onClick={() => navigate(`/read/${item.chapterId}`)}
                    onLongPress={(point) =>
                      openMenu(
                        {
                          kind: "ebook",
                          id: item.chapterId,
                          title: item.bookTitle || item.seriesName || "Book",
                          coverUrl: item.coverUrl,
                        },
                        point
                      )
                    }
                    coverUrl={item.coverUrl}
                    alt={item.bookTitle}
                    ringClass="hover:ring-amber-500/60"
                    fallbackIcon={<BookOpen size={24} className="text-gray-500" />}
                  />
                ))}
              </div>
            </section>
          )}

          {(listeningItems.length > 0 || rdListeningItems.length > 0) && (
            <section className="mb-8">
              <h2 className="flex items-center gap-2 text-lg font-semibold text-gray-100 mb-3">
                <Headphones size={18} className="text-emerald-400" />
                Continue Listening
              </h2>
              <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-6 lg:grid-cols-8 xl:grid-cols-10 gap-2">
                {listeningItems.slice(0, 6).map((item) => (
                  <ContinueTile
                    key={`abs-${item.itemId}`}
                    onClick={() => playABS(item.itemId)}
                    onLongPress={(point) =>
                      openMenu(
                        {
                          kind: "abs",
                          id: item.itemId,
                          title: item.title,
                          coverUrl: item.coverUrl,
                        },
                        point
                      )
                    }
                    coverUrl={item.coverUrl}
                    alt={item.title}
                    ringClass="hover:ring-emerald-500/60"
                    fallbackIcon={<Headphones size={24} className="text-gray-500" />}
                  />
                ))}
                {rdListeningItems.slice(0, 6).map((item) => (
                  <ContinueTile
                    key={`rd-${item.id}`}
                    onClick={() => {
                      if (item.tracks?.length > 0) {
                        playRD(
                          item.tracks,
                          item.title,
                          item.author,
                          item.coverUrl,
                          item.id,
                          {
                            startAt: item.progressSeconds,
                            trackIndex: item.currentTrackIndex,
                            trackPositionSeconds: item.trackPositionSeconds,
                          }
                        );
                      }
                    }}
                    onLongPress={(point) =>
                      openMenu(
                        {
                          kind: "rd",
                          id: item.id,
                          title: item.title,
                          coverUrl: item.coverUrl,
                        },
                        point
                      )
                    }
                    coverUrl={item.coverUrl}
                    alt={item.title}
                    ringClass="hover:ring-purple-500/60"
                    fallbackIcon={<Radio size={24} className="text-gray-500" />}
                  />
                ))}
              </div>
            </section>
          )}

          <div className="mb-8">
            <BookCarousel
              title="Trending"
              books={trendingData?.books || []}
              isLoading={trendingLoading}
            />
          </div>

          <div className="mb-8">
            <BookCarousel
              title="New Releases"
              books={newReleasesData?.books || []}
              isLoading={newReleasesLoading}
            />
          </div>

          <div className="space-y-8">
            {carouselQueries.map((cat) => (
              <BookCarousel
                key={cat.slug}
                title={cat.name}
                books={cat.books}
                isLoading={cat.isLoading}
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
