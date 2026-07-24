import { useState } from "react";
import { Headphones, Info, BookOpen, RefreshCw } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import api from "../api/client";
import { useToast } from "../contexts/ToastContext";
import { useAuth } from "../hooks/useAuth";
import { purgeLibraryCollectionQueries } from "../utils/shelfQueryCache";
import CoverImage from "./CoverImage";
import ShelfCardMeta from "./ShelfCardMeta";

interface Props {
  itemId: string;
  title: string;
  author: string;
  coverUrl: string;
  duration: number;
  progress: number;
  /** Opens full library details (primary card action). */
  onNavigate?: (
    title: string,
    author?: string,
    target?: { ebookChapterId?: number; ebookSeriesId?: number; absItemId?: string }
  ) => void;
  hasEbook?: boolean;
  cached?: boolean;
  unavailable?: boolean;
  seriesName?: string;
  sequence?: string;
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
  onNavigate,
  hasEbook,
  cached,
  unavailable,
  seriesName,
  sequence,
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

  const openDetails = () => {
    if (onNavigate) onNavigate(title, author, { absItemId: itemId });
  };

  return (
    <button
      type="button"
      onClick={openDetails}
      className={`group text-left flex flex-col relative ${
        unavailable ? "opacity-45 grayscale-[0.35]" : ""
      }`}
    >
      <div className="relative aspect-[2/3] bg-gray-900 overflow-hidden rounded-lg border border-gray-800 group-hover:border-emerald-600/50 transition-all duration-200 group-hover:shadow-lg group-hover:shadow-emerald-900/10 group-hover:-translate-y-0.5">
        <CoverImage
          src={coverUrl}
          alt={title}
          className="absolute inset-0 w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
          loading="lazy"
          fallback={
            <div className="w-full h-full flex items-center justify-center text-gray-700">
              <Headphones size={20} />
            </div>
          }
        />
        <div className="absolute inset-0 bg-black/0 group-hover:bg-black/40 transition-colors flex items-center justify-center">
          <Info size={24} className="text-white opacity-0 group-hover:opacity-100 transition-opacity drop-shadow-lg" />
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
              <RefreshCw size={10} className={`text-emerald-400 ${rematching ? "animate-spin" : ""}`} />
            </span>
          )}
        </div>
        <div className="absolute bottom-1 right-1 flex items-center gap-0.5">
          {hasEbook && <BookOpen size={10} className="text-amber-400 drop-shadow" />}
          <Headphones size={10} className="text-emerald-400 drop-shadow" />
        </div>
      </div>
      <ShelfCardMeta
        title={title}
        author={author}
        seriesName={seriesName}
        sequence={sequence}
      >
        {duration > 0 && (
          <p className="text-[9px] text-gray-500">{formatDuration(duration)}</p>
        )}
      </ShelfCardMeta>
    </button>
  );
}
