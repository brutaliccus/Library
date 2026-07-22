import { ChevronDown, ChevronUp, Play, Clock, Headphones } from "lucide-react";
import { useMemo, useState } from "react";
import CoverImage from "./CoverImage";

interface SeriesBook {
  itemId: string;
  title: string;
  author: string;
  coverUrl: string;
  sequence: string;
  duration: number;
  progress: number;
}

interface Series {
  id: string;
  name: string;
  books: SeriesBook[];
  bookCount: number;
  totalDuration: number;
  coverUrl: string;
}

interface Props {
  series: Series[];
  onPlay: (itemId: string) => void;
  /** Item ids fully cached for offline play */
  cachedIds?: Set<string>;
  /** When true, uncached books are greyed out */
  offline?: boolean;
}

function formatDuration(secs: number): string {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function seqCompare(a: string, b: string): number {
  const fa = parseFloat(a || "999");
  const fb = parseFloat(b || "999");
  if (!Number.isNaN(fa) && !Number.isNaN(fb)) return fa - fb;
  return String(a).localeCompare(String(b));
}

function SeriesCard({
  series,
  isExpanded,
  onToggle,
  onPlay,
  cachedIds,
  offline,
}: {
  series: Series;
  isExpanded: boolean;
  onToggle: () => void;
  onPlay: (itemId: string) => void;
  cachedIds?: Set<string>;
  offline?: boolean;
}) {
  const books = useMemo(() => {
    const list = [...series.books];
    list.sort((a, b) => {
      const ac = cachedIds?.has(a.itemId) ? 0 : 1;
      const bc = cachedIds?.has(b.itemId) ? 0 : 1;
      if (ac !== bc) return ac - bc;
      return seqCompare(a.sequence, b.sequence);
    });
    return list;
  }, [series.books, cachedIds]);

  return (
    <div className="border border-gray-800 rounded-lg overflow-hidden bg-gray-800/30">
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-3 p-3 hover:bg-gray-800/60 transition-colors text-left"
      >
        <CoverImage
          src={series.coverUrl}
          alt=""
          className="w-10 h-14 rounded object-cover shrink-0"
          fallback={
            <div className="w-10 h-14 rounded bg-gray-700 shrink-0 flex items-center justify-center">
              <Headphones size={14} className="text-gray-500" />
            </div>
          }
        />
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-semibold text-gray-100 truncate">{series.name}</h3>
          <p className="text-xs text-gray-400">
            {series.bookCount} book{series.bookCount !== 1 ? "s" : ""}
            {series.totalDuration > 0 && <span className="ml-2 text-gray-500">{formatDuration(series.totalDuration)}</span>}
          </p>
        </div>
        {isExpanded ? (
          <ChevronUp size={16} className="text-gray-500 shrink-0" />
        ) : (
          <ChevronDown size={16} className="text-gray-500 shrink-0" />
        )}
      </button>
      {isExpanded && (
        <div className="border-t border-gray-800">
          {books.map((book) => {
            const cached = !!cachedIds?.has(book.itemId);
            const unavailable = offline && !cached;
            return (
              <button
                key={book.itemId}
                onClick={() => onPlay(book.itemId)}
                className={`w-full flex items-center gap-3 px-3 py-2 hover:bg-gray-800/60 transition-colors text-left group ${
                  unavailable ? "opacity-45 grayscale-[0.35]" : ""
                }`}
              >
                <CoverImage
                  src={book.coverUrl}
                  alt=""
                  className="w-8 h-11 rounded object-cover shrink-0"
                  fallback={<div className="w-8 h-11 rounded bg-gray-700 shrink-0" />}
                />
                <span className="text-xs text-gray-500 w-6 shrink-0 text-center font-mono">
                  {book.sequence || "—"}
                </span>
                <div className="flex-1 min-w-0">
                  <p className="text-xs text-gray-200 truncate">
                    {book.title}
                    {cached && (
                      <span className="ml-1.5 text-[9px] font-semibold text-emerald-400">Offline</span>
                    )}
                  </p>
                  {book.progress > 0 && book.progress < 1 && (
                    <div className="w-full h-0.5 bg-gray-700 rounded-full mt-1">
                      <div className="h-full bg-emerald-500 rounded-full" style={{ width: `${Math.round(book.progress * 100)}%` }} />
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  {book.duration > 0 && (
                    <span className="text-[10px] text-gray-500 flex items-center gap-0.5">
                      <Clock size={9} />
                      {formatDuration(book.duration)}
                    </span>
                  )}
                  <Play size={14} className="text-gray-600 group-hover:text-emerald-400 transition-colors" />
                </div>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function SeriesDrilldown({ series, onPlay, cachedIds, offline }: Props) {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const ordered = useMemo(() => {
    const list = [...series];
    list.sort((a, b) => {
      const ac = a.books.some((bk) => cachedIds?.has(bk.itemId)) ? 0 : 1;
      const bc = b.books.some((bk) => cachedIds?.has(bk.itemId)) ? 0 : 1;
      if (ac !== bc) return ac - bc;
      return a.name.localeCompare(b.name);
    });
    return list;
  }, [series, cachedIds]);

  if (series.length === 0) {
    return <p className="text-sm text-gray-500 py-8 text-center">No series found in your library</p>;
  }

  return (
    <div className="space-y-2">
      {ordered.map((s) => (
        <SeriesCard
          key={s.id}
          series={s}
          isExpanded={expandedId === s.id}
          onToggle={() => setExpandedId(expandedId === s.id ? null : s.id)}
          onPlay={onPlay}
          cachedIds={cachedIds}
          offline={offline}
        />
      ))}
    </div>
  );
}
