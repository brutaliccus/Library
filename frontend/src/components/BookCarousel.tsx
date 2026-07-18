import { useRef } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronLeft, ChevronRight } from "lucide-react";
import type { BookSummary } from "../types/book";
import BookCard from "./BookCard";
import BookCardSkeleton from "./BookCardSkeleton";

interface Props {
  title: string;
  subtitle?: string;
  books: BookSummary[];
  isLoading?: boolean;
  /** When set, the shelf title navigates to the full list page. */
  to?: string;
}

export default function BookCarousel({ title, subtitle, books, isLoading, to }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();

  const scroll = (dir: "left" | "right") => {
    if (!scrollRef.current) return;
    const amount = scrollRef.current.clientWidth * 0.75;
    scrollRef.current.scrollBy({
      left: dir === "left" ? -amount : amount,
      behavior: "smooth",
    });
  };

  return (
    <section>
      <div className="flex items-center justify-between mb-3">
        <div className="min-w-0">
          {to ? (
            <button
              type="button"
              onClick={() => navigate(to)}
              className="text-lg font-semibold text-gray-100 truncate hover:text-brand-300 transition-colors text-left"
            >
              {title}
              <span className="ml-2 text-sm font-normal text-gray-500">View all</span>
            </button>
          ) : (
            <h2 className="text-lg font-semibold text-gray-100 truncate">{title}</h2>
          )}
          {subtitle && (
            <p className="text-xs text-gray-500 mt-0.5">{subtitle}</p>
          )}
        </div>
        <div className="flex gap-1 shrink-0">
          <button
            onClick={() => scroll("left")}
            className="p-1.5 rounded-lg text-gray-400 hover:bg-gray-800 hover:text-gray-200 transition-colors"
          >
            <ChevronLeft size={18} />
          </button>
          <button
            onClick={() => scroll("right")}
            className="p-1.5 rounded-lg text-gray-400 hover:bg-gray-800 hover:text-gray-200 transition-colors"
          >
            <ChevronRight size={18} />
          </button>
        </div>
      </div>
      <div
        ref={scrollRef}
        className="grid grid-flow-col auto-cols-[20%] sm:auto-cols-[14%] md:auto-cols-[10%] lg:auto-cols-[8%] xl:auto-cols-[6.5%] gap-2 overflow-x-auto pb-2 scroll-smooth scrollbar-hide"
      >
        {isLoading
          ? Array.from({ length: 8 }).map((_, i) => (
              <BookCardSkeleton key={i} />
            ))
          : books.map((book) => (
              <BookCard key={book.id} book={book} />
            ))}
      </div>
    </section>
  );
}
