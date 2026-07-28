import { useState, FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Search, SlidersHorizontal } from "lucide-react";

interface Props {
  onGenreToggle?: () => void;
  genreActiveCount?: number;
}

const FLOAT_TOP =
  "top-[calc(3.5rem+env(safe-area-inset-top,0px))]";

export default function HeroSearch({ onGenreToggle, genreActiveCount = 0 }: Props) {
  const [value, setValue] = useState("");
  const navigate = useNavigate();

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const q = value.trim();
    if (q.length >= 2) {
      navigate(`/search?q=${encodeURIComponent(q)}`);
    }
  };

  const hasFilter = Boolean(onGenreToggle);

  const renderSearchForm = () => (
    <form onSubmit={handleSubmit} className="relative max-w-xl mx-auto">
      <Search
        size={22}
        className="absolute left-4 top-1/2 -translate-y-1/2 text-gray-500 pointer-events-none"
      />
      <input
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Search by title, author, or ISBN..."
        className={`w-full pl-12 py-4 bg-gray-800 border border-gray-700 rounded-2xl text-base text-gray-100 shadow-lg focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent placeholder:text-gray-500 ${
          hasFilter ? "pr-36" : "pr-24"
        }`}
      />
      {hasFilter && (
        <button
          type="button"
          onClick={onGenreToggle}
          className="absolute right-[5.5rem] top-1/2 -translate-y-1/2 inline-flex items-center justify-center gap-1 p-2 rounded-xl text-gray-400 hover:text-gray-100 hover:bg-gray-700/80 transition-colors"
          title="Filter genres"
          aria-label="Filter genres"
        >
          <SlidersHorizontal size={18} />
          {genreActiveCount > 0 && (
            <span className="min-w-[1.1rem] px-1 py-0.5 bg-brand-600 text-white text-[10px] font-bold rounded-full leading-none">
              {genreActiveCount}
            </span>
          )}
        </button>
      )}
      <button
        type="submit"
        className="absolute right-2 top-1/2 -translate-y-1/2 px-5 py-2 bg-brand-600 text-white text-sm font-semibold rounded-xl hover:bg-brand-500 transition-colors"
      >
        Search
      </button>
    </form>
  );

  return (
    <div className="text-center pt-2 pb-4 lg:py-12">
      {/* Mobile floating search under sticky nav */}
      <div className="h-[4.25rem] lg:hidden" aria-hidden />
      <div
        className={`lg:hidden z-40 fixed left-0 right-0 ${FLOAT_TOP} px-4 py-2 bg-gray-950/95 backdrop-blur-sm border-b border-gray-800/80`}
      >
        {renderSearchForm()}
      </div>

      <h1 className="text-4xl font-bold text-gray-100 mb-2">
        Find your next read
      </h1>
      <p className="text-gray-400 mb-8">
        Search the catalog to find books for your library
      </p>
      <p className="text-xs text-gray-500 mb-4">
        Catalog powered by Open Library. Use “Available downloads only” on search results to narrow to cached releases.
      </p>

      {/* Desktop inline search */}
      <div className="hidden lg:block">{renderSearchForm()}</div>
    </div>
  );
}
