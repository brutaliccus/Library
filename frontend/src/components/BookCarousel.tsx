import { useRef } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import type { BookSummary } from "../types/book";
import BookCard from "./BookCard";
import BookCardSkeleton from "./BookCardSkeleton";

interface Props {
  title: string;
  books: BookSummary[];
  isLoading?: boolean;
}

export default function BookCarousel({ title, books, isLoading }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);

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
        <h2 className="text-lg font-semibold text-gray-100">{title}</h2>
        <div className="flex gap-1">
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
