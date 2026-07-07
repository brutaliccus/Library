import { useNavigate } from "react-router-dom";
import { BookOpen, Download } from "lucide-react";
import type { BookSummary } from "../types/book";

interface Props {
  book: BookSummary;
}

export default function BookCard({ book }: Props) {
  const navigate = useNavigate();

  return (
    <button
      onClick={() => navigate(`/book/${encodeURIComponent(book.id)}`)}
      className="group text-left flex flex-col bg-gray-800/50 rounded-lg overflow-hidden border border-gray-800 hover:border-gray-600 hover:bg-gray-800 transition-all duration-200 hover:shadow-lg hover:shadow-black/20 hover:-translate-y-0.5 h-full"
    >
      <div className="relative aspect-[2/3] bg-gray-900 overflow-hidden">
        {book.coverUrl ? (
          <img
            src={book.coverUrl}
            alt={book.title}
            className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
            loading="lazy"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-gray-700">
            <BookOpen size={20} />
          </div>
        )}
        {book.availability?.available && (
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
          {book.title}
        </h3>
        {book.authors.length > 0 && (
          <p className="text-[9px] text-gray-400 line-clamp-1">
            {book.authors.join(", ")}
          </p>
        )}
      </div>
    </button>
  );
}
