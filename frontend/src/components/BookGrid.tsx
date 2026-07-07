import type { BookSummary } from "../types/book";
import BookCard from "./BookCard";
import BookCardSkeleton from "./BookCardSkeleton";

interface Props {
  books: BookSummary[];
  isLoading?: boolean;
  skeletonCount?: number;
}

export default function BookGrid({ books, isLoading, skeletonCount = 10 }: Props) {
  if (isLoading) {
    return (
      <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-7 lg:grid-cols-9 xl:grid-cols-11 gap-2">
        {Array.from({ length: skeletonCount }).map((_, i) => (
          <BookCardSkeleton key={i} />
        ))}
      </div>
    );
  }

  if (books.length === 0) {
    return (
      <div className="text-center py-16 text-gray-500">
        <p>No books found</p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-7 lg:grid-cols-9 xl:grid-cols-11 gap-2">
      {books.map((book) => (
        <BookCard key={book.id} book={book} />
      ))}
    </div>
  );
}
