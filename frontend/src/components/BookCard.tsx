import { useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { BookOpen, Check, Download, HelpCircle } from "lucide-react";
import type { BookSummary } from "../types/book";
import CoverImage from "./CoverImage";
import ShelfCardMeta from "./ShelfCardMeta";

interface Props {
  book: BookSummary;
}

export default function BookCard({ book }: Props) {
  const navigate = useNavigate();
  const [coverFailed, setCoverFailed] = useState(false);
  const coverUrl = book.coverUrl || "";
  const showCover = Boolean(coverUrl) && !coverFailed;
  const avail = book.availability;
  const inLibrary = Boolean(avail?.inLibrary);
  const cached = Boolean(avail?.available);
  const catalogOnly = Boolean(avail?.catalogOnly) || (!cached && !inLibrary);

  useEffect(() => {
    setCoverFailed(false);
  }, [coverUrl, book.id]);

  return (
    <button
      onClick={() => navigate(`/book/${encodeURIComponent(book.id)}`)}
      className="group text-left flex flex-col rounded-lg border border-gray-800 bg-gray-800/50 hover:border-gray-600 hover:bg-gray-800 transition-all duration-200 hover:shadow-lg hover:shadow-black/20 hover:-translate-y-0.5"
    >
      <div className="relative aspect-[2/3] bg-gray-900 overflow-hidden rounded-t-lg">
        {showCover ? (
          <CoverImage
            src={coverUrl}
            alt={book.title}
            className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
            loading="lazy"
            referrerPolicy="no-referrer"
            onError={() => setCoverFailed(true)}
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-gray-700">
            <BookOpen size={20} />
          </div>
        )}
        {inLibrary ? (
          <span
            className="absolute top-1 right-1 flex items-center gap-0.5 px-1 py-0.5 rounded bg-emerald-900/90 text-emerald-300 text-[8px] font-medium"
            title="Already in library"
          >
            <Check size={8} strokeWidth={3} />
          </span>
        ) : cached ? (
          <span
            className="absolute top-1 right-1 flex items-center gap-0.5 px-1 py-0.5 rounded bg-emerald-900/90 text-emerald-300 text-[8px] font-medium"
            title="Cached — available to download"
          >
            <Download size={8} />
          </span>
        ) : catalogOnly ? (
          <span
            className="absolute top-1 right-1 flex items-center gap-0.5 px-1 py-0.5 rounded bg-amber-950/90 text-amber-300 text-[8px] font-medium"
            title="In catalog — not yet cached"
          >
            <HelpCircle size={8} />
          </span>
        ) : null}
      </div>
      <ShelfCardMeta
        title={book.title}
        author={book.authors.length > 0 ? book.authors.join(", ") : undefined}
        seriesName={book.seriesName}
        sequence={book.seriesBookNumber}
      />
    </button>
  );
}
