import { useNavigate } from "react-router-dom";
import { BookOpen, Download, HardDrive } from "lucide-react";

export interface CacheReleaseCardData {
  id: string;
  title: string;
  authors: string[];
  coverUrl?: string;
  mediaType?: string;
  rdCached?: boolean;
  torboxCached?: boolean;
  catalogMatched?: boolean;
  availability?: { available?: boolean };
}

interface Props {
  release: CacheReleaseCardData;
}

export default function CacheReleaseCard({ release }: Props) {
  const navigate = useNavigate();

  return (
    <button
      onClick={() => navigate(`/book/${encodeURIComponent(release.id)}`)}
      className="group text-left flex flex-col bg-gray-800/50 rounded-lg overflow-hidden border border-gray-800 hover:border-amber-700/60 hover:bg-gray-800 transition-all duration-200 hover:shadow-lg hover:shadow-black/20 hover:-translate-y-0.5 h-full"
    >
      <div className="relative aspect-[2/3] bg-gray-900 overflow-hidden">
        {release.coverUrl ? (
          <img
            src={release.coverUrl}
            alt={release.title}
            className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
            loading="lazy"
          />
        ) : (
          <div className="w-full h-full flex flex-col items-center justify-center text-gray-600 gap-1 px-2">
            <BookOpen size={20} />
            <span className="text-[8px] uppercase tracking-wide text-gray-500">No cover</span>
          </div>
        )}
        <span
          className="absolute top-1 left-1 flex items-center gap-0.5 px-1 py-0.5 rounded bg-amber-950/90 text-amber-300 text-[8px] font-medium"
          title="Cached indexer release"
        >
          <HardDrive size={8} />
        </span>
        {(release.availability?.available || release.rdCached || release.torboxCached) && (
          <span
            className="absolute top-1 right-1 flex items-center gap-0.5 px-1 py-0.5 rounded bg-emerald-900/90 text-emerald-300 text-[8px] font-medium"
            title="Available to download"
          >
            <Download size={8} />
          </span>
        )}
      </div>
      <div className="p-1.5 flex flex-col gap-0.5 h-14">
        <h3 className="text-[10px] font-semibold text-gray-100 line-clamp-2 leading-tight">
          {release.title}
        </h3>
        {release.authors.length > 0 ? (
          <p className="text-[9px] text-gray-400 line-clamp-1">{release.authors.join(", ")}</p>
        ) : (
          <p className="text-[9px] text-amber-600/80 line-clamp-1">
            {release.mediaType === "ebook" ? "Ebook release" : "Audiobook release"}
          </p>
        )}
      </div>
    </button>
  );
}
