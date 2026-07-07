import { useState, useEffect } from "react";
import {
  Download, HardDrive, ArrowUpCircle, ArrowDownCircle,
  Headphones, BookText, HelpCircle, ChevronDown, Library,
  Play, Loader2, CheckCircle, Clock, Globe, Zap,
} from "lucide-react";

interface SearchResult {
  title: string;
  size: number;
  seeders: number;
  leechers: number;
  indexer: string;
  magnetUrl: string | null;
  downloadUrl: string | null;
  mediaType?: string;
  inLibrary?: string[];
  source?: string;
  aaMd5?: string;
  author?: string;
  fileExtension?: string;
  formatInfo?: string;
  rdCached?: boolean;
  torboxCached?: boolean;
  matchTier?: "exact" | "likely" | "weak";
}

interface StreamHistoryEntry {
  id: number;
  status: string;
  hasProgress: boolean;
}

interface Props {
  result: SearchResult;
  onRequest: (result: SearchResult, mediaTypeOverride: string) => void;
  onStream?: (result: SearchResult) => void;
  requesting?: boolean;
  streamProgress?: { detail: string; progress: number } | null;
  streaming?: boolean;
  streamHistory?: StreamHistoryEntry | null;
}

function formatSize(bytes: number): string {
  if (bytes === 0) return "Unknown";
  const gb = bytes / (1024 * 1024 * 1024);
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  const mb = bytes / (1024 * 1024);
  return `${mb.toFixed(0)} MB`;
}

const MEDIA_OPTIONS = [
  { value: "audiobook", label: "Audiobook", color: "bg-purple-900/40 text-purple-300", icon: Headphones },
  { value: "ebook", label: "eBook", color: "bg-emerald-900/40 text-emerald-300", icon: BookText },
  { value: "unknown", label: "Unknown", color: "bg-gray-700 text-gray-400", icon: HelpCircle },
];

const LIBRARY_BADGES: Record<string, { label: string; color: string }> = {
  audiobookshelf: { label: "In Audiobookshelf", color: "bg-amber-900/40 text-amber-300" },
  kavita: { label: "In Kavita", color: "bg-sky-900/40 text-sky-300" },
};

export default function ResultCard({
  result, onRequest, onStream, requesting, streaming, streamProgress, streamHistory,
}: Props) {
  const isAA = result.source === "annas_archive";
  const hasLink = isAA ? !!result.aaMd5 : !!(result.magnetUrl || result.downloadUrl);
  const [selectedType, setSelectedType] = useState(result.mediaType || "unknown");
  const [showDropdown, setShowDropdown] = useState(false);

  useEffect(() => {
    setSelectedType(result.mediaType || "unknown");
  }, [result.mediaType, result.title, result.size]);

  const current = MEDIA_OPTIONS.find((o) => o.value === selectedType) || MEDIA_OPTIONS[2];
  const BadgeIcon = current.icon;

  const isAudiobook = selectedType === "audiobook";
  const canStream = isAudiobook && !isAA && !!(result.magnetUrl || result.downloadUrl);

  return (
    <div className="bg-gray-800 border border-gray-700 rounded-xl p-4 hover:border-gray-600 transition-colors">
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1 flex-wrap">
            <div className="relative">
              <button
                onClick={() => setShowDropdown(!showDropdown)}
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${current.color} hover:opacity-80 transition-opacity`}
                title="Click to change type"
              >
                <BadgeIcon size={12} />
                {current.label}
                <ChevronDown size={10} />
              </button>
              {showDropdown && (
                <div className="absolute top-full left-0 mt-1 bg-gray-700 border border-gray-600 rounded-lg shadow-xl z-10 py-1 min-w-[120px]">
                  {MEDIA_OPTIONS.filter((o) => o.value !== "unknown").map((opt) => {
                    const Icon = opt.icon;
                    return (
                      <button
                        key={opt.value}
                        onClick={() => {
                          setSelectedType(opt.value);
                          setShowDropdown(false);
                        }}
                        className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left hover:bg-gray-600 transition-colors ${
                          selectedType === opt.value ? "text-white font-medium" : "text-gray-300"
                        }`}
                      >
                        <Icon size={12} />
                        {opt.label}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            {!isAA && result.matchTier === "exact" && (
              <span
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-brand-900/50 text-brand-200 border border-brand-600/40"
                title="Matches this book in the series"
              >
                Best match
              </span>
            )}

            {!isAA && result.rdCached && (
              <span
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-emerald-900/50 text-emerald-300 border border-emerald-700/50"
                title="Already cached on Real-Debrid — instant download after adding"
              >
                <Zap size={12} className="fill-emerald-400/80" />
                Instant RD
              </span>
            )}

            {!isAA && result.torboxCached && (
              <span
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-orange-900/50 text-orange-300 border border-orange-700/50"
                title="Already cached on Torbox — instant download after adding"
              >
                <Zap size={12} className="fill-orange-400/80" />
                Instant Torbox
              </span>
            )}

            {(result.inLibrary?.length ?? 0) > 0 && result.inLibrary!.map((lib) => {
              const badge = LIBRARY_BADGES[lib];
              if (!badge) return null;
              return (
                <span
                  key={lib}
                  className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${badge.color}`}
                >
                  <Library size={12} />
                  {badge.label}
                </span>
              );
            })}

            {streamHistory && (
              <span
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${
                  streamHistory.hasProgress
                    ? "bg-purple-900/40 text-purple-300"
                    : "bg-blue-900/40 text-blue-300"
                }`}
                title={streamHistory.hasProgress ? "You've started listening to this" : "Previously streamed via RD"}
              >
                {streamHistory.hasProgress ? <Clock size={11} /> : <CheckCircle size={11} />}
                {streamHistory.hasProgress ? "In Progress" : "Previously Streamed"}
              </span>
            )}
          </div>
          <h3 className="font-semibold text-gray-100 truncate">{result.title}</h3>
          {result.author && isAA && (
            <p className="text-xs text-gray-400 truncate mt-0.5">{result.author}</p>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-3 text-sm text-gray-400">
            {result.size > 0 && (
              <span className="flex items-center gap-1">
                <HardDrive size={14} />
                {formatSize(result.size)}
              </span>
            )}
            {!isAA && (
              <>
                <span className="flex items-center gap-1 text-green-400">
                  <ArrowUpCircle size={14} />
                  {result.seeders}
                </span>
                <span className="flex items-center gap-1 text-red-400">
                  <ArrowDownCircle size={14} />
                  {result.leechers}
                </span>
              </>
            )}
            {isAA && result.fileExtension && (
              <span className="px-2 py-0.5 bg-indigo-900/40 rounded text-xs font-medium text-indigo-300 uppercase">
                {result.fileExtension}
              </span>
            )}
            <span className={`flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${
              isAA ? "bg-teal-900/40 text-teal-300" : "bg-gray-700 text-gray-300"
            }`}>
              {isAA && <Globe size={11} />}
              {result.indexer}
            </span>
            {isAA && (
              <span className="px-2 py-0.5 bg-green-900/40 rounded text-xs font-medium text-green-300">
                Direct Download
              </span>
            )}
          </div>
        </div>

        <div className="flex flex-col gap-2 shrink-0">
          {canStream && onStream && (
            <div className="flex flex-col gap-1">
              <button
                onClick={() => onStream(result)}
                disabled={streaming}
                className="flex items-center gap-1.5 px-4 py-2 bg-emerald-600 text-white text-sm font-medium rounded-lg hover:bg-emerald-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {streaming ? (
                  <Loader2 size={15} className="animate-spin" />
                ) : (
                  <Play size={15} />
                )}
                {streaming
                  ? streamProgress
                    ? `${streamProgress.progress}%`
                    : "Loading..."
                  : "Stream"}
              </button>
              {streaming && streamProgress && (
                <div className="w-full">
                  <div className="h-1 bg-gray-700 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-emerald-500 transition-all duration-500"
                      style={{ width: `${Math.min(streamProgress.progress, 100)}%` }}
                    />
                  </div>
                  <p className="text-[10px] text-gray-400 mt-0.5 truncate max-w-[160px]">{streamProgress.detail}</p>
                </div>
              )}
            </div>
          )}
          <button
            onClick={() => onRequest(result, selectedType)}
            disabled={!hasLink || requesting}
            className="flex items-center gap-1.5 px-4 py-2 bg-brand-600 text-white text-sm font-medium rounded-lg hover:bg-brand-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <Download size={15} />
            {requesting ? "Requesting..." : "Request"}
          </button>
        </div>
      </div>
    </div>
  );
}
