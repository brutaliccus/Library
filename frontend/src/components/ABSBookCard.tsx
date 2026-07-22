import { useState } from "react";
import { Headphones, Play, BookOpen, Info, RefreshCw } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import api from "../api/client";
import { useToast } from "../contexts/ToastContext";
import { useAuth } from "../hooks/useAuth";
import { purgeLibraryCollectionQueries } from "../utils/shelfQueryCache";
import CoverImage from "./CoverImage";

interface Props {
  itemId: string;
  title: string;
  author: string;
  coverUrl: string;
  duration: number;
  progress: number;
  onPlay: (itemId: string) => void;
  onNavigate?: (
    title: string,
    author?: string,
    target?: { ebookChapterId?: number; absItemId?: string }
  ) => void;
  hasEbook?: boolean;
  /** Fully cached for offline play */
  cached?: boolean;
  /** Greyed / non-playable (e.g. offline + not cached) */
  unavailable?: boolean;
}

function formatDuration(secs: number): string {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export default function ABSBookCard({
  itemId,
  title,
  author,
  coverUrl,
  duration,
  progress,
  onPlay,
  onNavigate,
  hasEbook,
  cached,
  unavailable,
}: Props) {
  const { toast } = useToast();
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const [rematching, setRematching] = useState(false);

  const handleRematch = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setRematching(true);
    try {
      await api.post(`/admin/abs/rematch/${itemId}`);
      await purgeLibraryCollectionQueries(queryClient, { refetch: true });
      toast("Book re-matched. Metadata updated.", "success");
    } catch {
      toast("Failed to re-match book", "error");
    } finally {
      setRematching(false);
    }
  };

  const handleInfoClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (onNavigate) onNavigate(title, author, { absItemId: itemId });
  };

  return (
    <button
      onClick={() => onPlay(itemId)}
      className={`group text-left flex flex-col bg-gray-800/50 rounded-lg overflow-hidden border border-gray-800 hover:border-emerald-600/50 hover:bg-gray-800 transition-all duration-200 hover:shadow-lg hover:shadow-emerald-900/10 hover:-translate-y-0.5 h-full relative ${
        unavailable ? "opacity-45 grayscale-[0.35]" : ""
      }`}
    >
      <div className="relative aspect-[2/3] bg-gray-900 overflow-hidden">
        <CoverImage
          src={coverUrl}
          alt={title}
          className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
          loading="lazy"
          fallback={
            <div className="w-full h-full flex items-center justify-center text-gray-700">
              <Headphones size={20} />
            </div>
          }
        />
        <div className="absolute inset-0 bg-black/0 group-hover:bg-black/40 transition-colors flex items-center justify-center">
          <Play size={24} className="text-white opacity-0 group-hover:opacity-100 transition-opacity drop-shadow-lg" />
        </div>
        {cached && (
          <span className="absolute top-1 left-1 px-1 py-0.5 rounded bg-black/65 text-[8px] font-semibold text-emerald-300">
            Offline
          </span>
        )}
        {progress > 0 && progress < 1 && (
          <div className="absolute bottom-0 left-0 right-0 h-1 bg-gray-700/80">
            <div className="h-full bg-emerald-500" style={{ width: `${Math.round(progress * 100)}%` }} />
          </div>
        )}
        <div className="absolute top-0.5 right-0.5 flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
          {user?.role === "admin" && (
            <span
              onClick={handleRematch}
              className="p-1 bg-black/60 rounded hover:bg-black/80 transition-colors cursor-pointer"
              title="Re-match metadata"
            >
              <RefreshCw size={10} className={`text-purple-400 ${rematching ? "animate-spin" : ""}`} />
            </span>
          )}
          {onNavigate && (
            <span
              onClick={handleInfoClick}
              className="p-1 bg-black/60 rounded hover:bg-black/80 transition-colors cursor-pointer"
              title="View book details"
            >
              <Info size={10} className="text-blue-400" />
            </span>
          )}
        </div>
        <div className="absolute bottom-1 right-1 flex items-center gap-0.5">
          {hasEbook && <BookOpen size={10} className="text-amber-400 drop-shadow" />}
          <Headphones size={10} className="text-emerald-400 drop-shadow" />
        </div>
      </div>
      <div className="p-1.5 flex flex-col gap-0.5 h-14">
        <h3 className="text-[10px] font-semibold text-gray-100 line-clamp-2 leading-tight">{title}</h3>
        {author && <p className="text-[9px] text-gray-400 line-clamp-1">{author}</p>}
        {duration > 0 && (
          <p className="text-[8px] text-gray-500 mt-auto">{formatDuration(duration)}</p>
        )}
      </div>
    </button>
  );
}
