import { useNavigate } from "react-router-dom";
import { BookOpen, Download, HardDrive } from "lucide-react";
import CoverImage from "./CoverImage";
import ShelfCardMeta from "./ShelfCardMeta";

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
      className="group text-left flex flex-col"
    >
      <div className="relative aspect-[2/3] bg-gray-900 overflow-hidden rounded-lg border border-gray-800 group-hover:border-amber-700/60 transition-all duration-200 group-hover:shadow-lg group-hover:shadow-black/20 group-hover:-translate-y-0.5">
        <CoverImage
          src={release.coverUrl}
          alt={release.title}
          className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
          loading="lazy"
          fallback={
            <div className="w-full h-full flex flex-col items-center justify-center text-gray-600 gap-1 px-2">
              <BookOpen size={20} />
              <span className="text-[8px] uppercase tracking-wide text-gray-500">No cover</span>
            </div>
          }
        />
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
      <ShelfCardMeta
        title={release.title}
        author={
          release.authors.length > 0
            ? release.authors.join(", ")
            : release.mediaType === "ebook"
              ? "Ebook release"
              : "Audiobook release"
        }
      />
    </button>
  );
}
