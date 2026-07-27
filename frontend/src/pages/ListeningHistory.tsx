import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Clock, Headphones, History } from "lucide-react";
import api from "../api/client";
import CoverImage from "../components/CoverImage";

interface HistoryItem {
  id: string;
  source: "abs" | "rd";
  title: string;
  author: string;
  coverUrl: string;
  progressSeconds: number;
  totalSeconds: number;
  progress: number;
  status: string;
  updatedAt: string;
  itemId?: string;
  streamHistoryId?: number;
}

function formatDate(iso: string): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

function formatPct(p: number): string {
  if (!p || !isFinite(p)) return "0%";
  return `${Math.min(100, Math.round(p * 100))}%`;
}

export default function ListeningHistory() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["listening-history"],
    queryFn: async () => {
      const { data } = await api.get("/stream/history");
      return data as { items: HistoryItem[] };
    },
    staleTime: 60 * 1000,
  });

  const items = data?.items || [];

  return (
    <div className="max-w-3xl mx-auto px-4 py-8 pb-24">
      <Link
        to="/my-library"
        className="inline-flex items-center gap-1.5 text-sm text-gray-400 hover:text-gray-200 mb-6"
      >
        <ArrowLeft size={16} />
        My Library
      </Link>

      <div className="flex items-center gap-3 mb-6">
        <History size={22} className="text-brand-400" />
        <div>
          <h1 className="text-2xl font-bold text-gray-100">Listening history</h1>
          <p className="text-sm text-gray-500">Recent titles you’ve played</p>
        </div>
      </div>

      {isLoading && (
        <p className="text-sm text-gray-500 flex items-center gap-2">
          <Clock size={16} className="animate-pulse" />
          Loading history…
        </p>
      )}
      {isError && (
        <p className="text-sm text-red-400">Could not load listening history.</p>
      )}
      {!isLoading && !isError && items.length === 0 && (
        <div className="text-center py-16 text-gray-500">
          <Headphones size={36} className="mx-auto mb-3 opacity-40" />
          <p className="text-sm">No listens yet. Start something from My Library or Browse.</p>
          <Link to="/" className="text-brand-400 hover:text-brand-300 text-sm mt-3 inline-block">
            Browse catalog
          </Link>
        </div>
      )}

      <ul className="space-y-3">
        {items.map((item) => {
          const detailTo =
            item.source === "abs" && item.itemId
              ? `/library/book/${encodeURIComponent(item.itemId)}`
              : "/";
          const pct =
            item.progress > 0
              ? item.progress
              : item.totalSeconds > 0
                ? item.progressSeconds / item.totalSeconds
                : 0;

          return (
            <li key={`${item.source}-${item.id}`}>
              <Link
                to={detailTo}
                className="flex gap-3 p-3 rounded-xl border border-gray-800 bg-gray-900/60 hover:border-gray-700 hover:bg-gray-900 transition-colors"
              >
                <CoverImage
                  src={item.coverUrl}
                  alt=""
                  className="w-14 h-[5.25rem] rounded-md object-cover shrink-0"
                  fallback={
                    <div className="w-14 h-[5.25rem] rounded-md bg-gray-800 flex items-center justify-center">
                      <Headphones size={18} className="text-gray-600" />
                    </div>
                  }
                />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-gray-100 truncate">{item.title}</p>
                  {item.author && (
                    <p className="text-xs text-gray-500 truncate mt-0.5">{item.author}</p>
                  )}
                  <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-gray-500">
                    <span>{formatPct(pct)} listened</span>
                    {item.updatedAt && <span>{formatDate(item.updatedAt)}</span>}
                    <span className="uppercase tracking-wide text-gray-600">
                      {item.source === "abs" ? "Library" : "Stream"}
                    </span>
                  </div>
                  {pct > 0 && (
                    <div className="mt-2 h-1 rounded-full bg-gray-800 overflow-hidden">
                      <div
                        className="h-full bg-brand-500"
                        style={{ width: `${Math.min(100, Math.round(pct * 100))}%` }}
                      />
                    </div>
                  )}
                </div>
              </Link>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
