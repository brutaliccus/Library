import { useCallback, useEffect, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, Headphones, Radio } from "lucide-react";
import api from "../api/client";
import { usePlayer } from "../contexts/PlayerContext";
import { useToast } from "../contexts/ToastContext";
import ContinueItemMenu, { type ContinueMenuTarget } from "./ContinueItemMenu";
import { useLongPress } from "../hooks/useLongPress";
import CoverImage from "./CoverImage";
import {
  getContinueReading,
  clearProgress as clearReadingProgress,
  hideFromContinueReading,
  hydrateReadingProgressFromServer,
} from "../utils/readingProgress";
import { clearBookCache, clearAbsBookCache } from "../utils/audioCache";
import {
  clearOfflineProgress,
  progressKeyForAbs,
  progressKeyForRd,
  removeAbsOfflineManifest,
  removeRdOfflineManifest,
} from "../utils/offlinePlayback";
import { clearEbookCache } from "../utils/ebookCache";
import { clearAaResumeIfMatching } from "../media/aaResumeSnapshot";

interface InProgressItem {
  itemId: string;
  title: string;
  author: string;
  coverUrl: string;
  progress: number;
  currentTime: number;
  duration: number;
  isFinished: boolean;
  playbackRate?: number | null;
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
  playbackRate?: number | null;
  tracks: Array<{
    index: number;
    title: string;
    contentUrl: string;
    mimeType: string;
    startOffset: number;
    duration: number;
  }>;
}

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
      <CoverImage
        src={coverUrl}
        alt={alt}
        draggable={false}
        className="w-full h-full object-cover group-hover:scale-105 transition-transform"
        fallback={
          <div className="w-full h-full flex items-center justify-center">{fallbackIcon}</div>
        }
      />
    </button>
  );
}

/** Continue Reading / Continue Listening shelves for My Library. */
export default function ContinueShelves() {
  const navigate = useNavigate();
  const { playABS, playRD } = usePlayer();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [continueReading, setContinueReading] = useState(() => getContinueReading(6));
  const [menuTarget, setMenuTarget] = useState<ContinueMenuTarget | null>(null);

  useEffect(() => {
    void hydrateReadingProgressFromServer().then(() => {
      setContinueReading(getContinueReading(6));
    });
  }, []);

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
          const id = String(target.id);
          await api.post(`/stream/abs/${encodeURIComponent(id)}/clear-progress`);
          clearOfflineProgress(progressKeyForAbs(id));
          removeAbsOfflineManifest(id);
          clearAaResumeIfMatching({ itemId: id });
          void clearAbsBookCache(id);
          queryClient.invalidateQueries({ queryKey: ["in-progress"] });
        } else if (target.kind === "rd") {
          const histId = Number(target.id);
          await api.post(`/stream/rd/history/${histId}/clear-progress`);
          clearOfflineProgress(progressKeyForRd({ streamHistoryId: histId }) || "");
          removeRdOfflineManifest({ streamHistoryId: histId });
          clearAaResumeIfMatching({ streamHistoryId: histId });
          void clearBookCache("h", histId);
          queryClient.invalidateQueries({ queryKey: ["rd-in-progress"] });
        } else {
          const chapterId = Number(target.id);
          clearReadingProgress(chapterId);
          void clearEbookCache(chapterId);
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
    staleTime: 60 * 1000,
    refetchOnWindowFocus: false,
  });

  const { data: rdInProgressData } = useQuery({
    queryKey: ["rd-in-progress"],
    queryFn: async () => {
      const { data } = await api.get("/stream/rd/history/in-progress");
      return data as { items: RDHistoryItem[] };
    },
    staleTime: 60 * 1000,
    refetchOnWindowFocus: false,
  });

  const listeningItems = inProgressData?.items?.filter((i) => !i.isFinished) || [];
  const rdListeningItems = rdInProgressData?.items || [];
  const hasListening = listeningItems.length > 0 || rdListeningItems.length > 0;

  if (continueReading.length === 0 && !hasListening) return null;

  return (
    <div className="mb-2 space-y-4">
      <ContinueItemMenu
        target={menuTarget}
        onClose={() => setMenuTarget(null)}
        onClearProgress={handleMenuClearProgress}
        onHide={handleMenuHide}
      />

      {continueReading.length > 0 && (
        <section>
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

      {hasListening && (
        <section>
          <h2 className="flex items-center gap-2 text-lg font-semibold text-gray-100 mb-3">
            <Headphones size={18} className="text-emerald-400" />
            Continue Listening
          </h2>
          <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-6 lg:grid-cols-8 xl:grid-cols-10 gap-2">
            {listeningItems.slice(0, 6).map((item) => (
              <ContinueTile
                key={`abs-${item.itemId}`}
                onClick={() => {
                  void playABS(item.itemId).catch((err) => {
                    const msg =
                      err instanceof Error && err.message.startsWith("Offline")
                        ? err.message
                        : "Failed to start playback";
                    toast(msg, "error");
                  });
                }}
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
                      },
                      undefined,
                      item.playbackRate
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
                ringClass="hover:ring-brand-500/60"
                fallbackIcon={<Radio size={24} className="text-gray-500" />}
              />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
