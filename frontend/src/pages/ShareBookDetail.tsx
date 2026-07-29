import { useParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import axios from "axios";
import { getApiBaseUrl, toAbsoluteUrl } from "../api/instanceUrl";
import { usePlayer } from "../contexts/PlayerContext";
import { useToast } from "../contexts/ToastContext";
import {
  Headphones, Loader2, Mic, Clock,
} from "lucide-react";
import CoverImage from "../components/CoverImage";
import SaveOfflineButton from "../components/SaveOfflineButton";
import { useOnlineStatus } from "../hooks/useOnlineStatus";
import {
  getOfflineProgress,
  progressKeyForAbs,
} from "../utils/offlinePlayback";
import type { AbsChapter } from "../types/player";

interface ShareBookDetail {
  token: string;
  itemId: string;
  title: string;
  subtitle: string;
  author: string;
  narrator: string;
  description: string;
  publisher: string;
  publishedYear: string;
  genres: string[];
  series: Array<{ id: string; name: string; sequence: string }>;
  duration: number;
  numTracks: number;
  coverUrl: string;
}

function formatDuration(secs: number): string {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function formatChapterTime(s: number): string {
  if (!s || !isFinite(s)) return "0:00";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  const pad = (n: number) => n.toString().padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

export default function ShareBookDetail() {
  const { token: rawToken } = useParams<{ token: string }>();
  const token = rawToken ? decodeURIComponent(rawToken) : undefined;
  const { playABS } = usePlayer();
  const { toast } = useToast();
  const online = useOnlineStatus();
  const [playLoading, setPlayLoading] = useState(false);

  const { data: item, isLoading, error } = useQuery({
    queryKey: ["share-book", token],
    queryFn: async () => {
      const { data } = await axios.get(
        `${getApiBaseUrl()}/share/${encodeURIComponent(token!)}`
      );
      return data as ShareBookDetail;
    },
    enabled: !!token,
    staleTime: 5 * 60 * 1000,
    retry: 1,
  });

  const { data: chaptersData } = useQuery({
    queryKey: ["share-chapters", token],
    queryFn: async () => {
      const { data } = await axios.get(
        `${getApiBaseUrl()}/share/${encodeURIComponent(token!)}/chapters`
      );
      return data as { chapters: AbsChapter[] };
    },
    enabled: !!token && online,
    staleTime: 10 * 60 * 1000,
    retry: 1,
  });

  const hasLocalProgress = useMemo(() => {
    if (!item?.itemId) return false;
    const p = getOfflineProgress(progressKeyForAbs(item.itemId));
    return !!p && (p.time > 5 || p.trackIndex > 0 || p.trackLocal > 5);
  }, [item?.itemId, playLoading]);

  const handlePlay = async (startAt?: number) => {
    if (!item || !token) return;
    setPlayLoading(true);
    try {
      await playABS(item.itemId, {
        ...(startAt != null ? { startAt } : {}),
        shareToken: token,
      });
    } catch (err) {
      const msg =
        err instanceof Error && err.message.startsWith("Offline")
          ? err.message
          : "Failed to start playback";
      toast(msg, "error");
    } finally {
      setPlayLoading(false);
    }
  };

  if (isLoading) {
    return (
      <div className="max-w-5xl mx-auto px-4 py-8">
        <div className="animate-pulse">
          <div className="flex flex-col md:flex-row gap-8">
            <div className="w-28 md:w-64 shrink-0 aspect-[2/3] bg-gray-800 rounded-xl" />
            <div className="flex-1 space-y-4">
              <div className="h-8 bg-gray-800 rounded w-3/4" />
              <div className="h-5 bg-gray-800 rounded w-1/2" />
              <div className="h-32 bg-gray-800 rounded w-full mt-6" />
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (error || !item) {
    return (
      <div className="max-w-5xl mx-auto px-4 py-16 text-center">
        <p className="text-gray-400">This shared book link is invalid or no longer available.</p>
        <Link to="/libraries" className="text-brand-400 hover:text-brand-300 mt-4 inline-block">
          Go to Libraries
        </Link>
      </div>
    );
  }

  const seriesLine = (item.series || [])
    .filter((s) => s.name)
    .map((s) => (s.sequence ? `${s.name} #${s.sequence}` : s.name))
    .join(" · ");

  const coverSrc = item.coverUrl ? toAbsoluteUrl(item.coverUrl) : "";
  const cover = coverSrc ? (
    <CoverImage src={coverSrc} alt={item.title} className="w-full rounded-xl shadow-2xl shadow-black/40" />
  ) : (
    <div className="w-full aspect-[2/3] bg-gray-800 rounded-xl flex items-center justify-center text-gray-700">
      <Headphones size={48} />
    </div>
  );

  const listenLabel = hasLocalProgress ? "Resume" : "Listen";

  const actions = (
    <>
      <button
        type="button"
        onClick={() => void handlePlay()}
        disabled={playLoading}
        className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-emerald-600 text-white hover:bg-emerald-500 transition-colors disabled:opacity-50"
      >
        {playLoading ? <Loader2 size={16} className="animate-spin" /> : <Headphones size={16} />}
        {listenLabel}
      </button>
      {item.itemId && (
        <SaveOfflineButton
          target={{ kind: "abs", itemId: item.itemId }}
          shareToken={token}
        />
      )}
    </>
  );

  return (
    <div className="max-w-5xl mx-auto px-4 py-8">
      <p className="text-xs text-gray-500 mb-6">Shared audiobook · progress stays on this device</p>

      <div className="flex gap-4 mb-5 md:hidden">
        <div className="w-[7.5rem] shrink-0">{cover}</div>
        <div className="flex flex-col gap-2 flex-1 content-start self-start">{actions}</div>
      </div>

      <div className="flex flex-col md:flex-row gap-8">
        <div className="hidden md:block w-64 shrink-0">{cover}</div>
        <div className="flex-1 min-w-0">
          <h1 className="text-2xl sm:text-3xl font-bold text-gray-100 leading-tight">{item.title}</h1>
          {item.subtitle && <p className="text-base sm:text-lg text-gray-400 mt-1">{item.subtitle}</p>}
          {item.author && (
            <p className="text-gray-300 mt-2 sm:mt-3">
              by <span className="text-gray-100 font-medium">{item.author}</span>
            </p>
          )}
          {seriesLine && <p className="text-sm text-brand-400 mt-1">{seriesLine}</p>}

          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-3 text-xs text-gray-400">
            {item.narrator && (
              <span className="inline-flex items-center gap-1">
                <Mic size={12} /> {item.narrator}
              </span>
            )}
            {item.duration > 0 && (
              <span className="inline-flex items-center gap-1">
                <Clock size={12} /> {formatDuration(item.duration)}
              </span>
            )}
            {item.publishedYear && <span>{item.publishedYear}</span>}
          </div>

          {(item.genres?.length ?? 0) > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-3">
              {item.genres.map((g) => (
                <span key={g} className="px-2 py-0.5 text-[10px] bg-gray-800 text-gray-300 rounded-full border border-gray-700">
                  {g}
                </span>
              ))}
            </div>
          )}

          <div className="hidden md:flex flex-wrap items-center gap-2 mt-4">{actions}</div>

          {item.description && (
            <div className="mt-6">
              <h2 className="text-lg font-semibold text-gray-100 mb-3">Synopsis</h2>
              <div
                className="text-gray-300 text-sm leading-relaxed prose prose-invert prose-sm max-w-none"
                dangerouslySetInnerHTML={{ __html: item.description }}
              />
            </div>
          )}

          {(chaptersData?.chapters?.length ?? 0) > 0 && (
            <div className="mt-8">
              <h2 className="text-lg font-semibold text-gray-100 mb-3">Chapters</h2>
              <ul className="space-y-1 max-h-80 overflow-y-auto pr-1">
                {chaptersData!.chapters.map((ch) => (
                  <li key={`${ch.id}-${ch.start}`}>
                    <button
                      type="button"
                      onClick={() => void handlePlay(ch.start)}
                      disabled={playLoading}
                      className="w-full text-left flex items-start gap-3 px-3 py-2 rounded-lg text-gray-300 hover:bg-gray-800 hover:text-white transition-colors disabled:opacity-50"
                    >
                      <span className="text-xs tabular-nums text-gray-500 shrink-0 pt-0.5">
                        {formatChapterTime(ch.start)}
                      </span>
                      <span className="text-sm flex-1 leading-snug">{ch.title}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
