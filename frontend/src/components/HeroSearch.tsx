import { useState, FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Search, SlidersHorizontal } from "lucide-react";
import { usePlayer } from "../contexts/PlayerContext";
import {
  FLOATING_SEARCH_ACTION,
  FLOATING_SEARCH_FILTER,
  FLOATING_SEARCH_INNER,
  FLOATING_SEARCH_INPUT,
  FLOATING_SEARCH_WRAP,
} from "./floatingSearchStyles";

interface Props {
  onGenreToggle?: () => void;
  genreActiveCount?: number;
}

export default function HeroSearch({ onGenreToggle, genreActiveCount = 0 }: Props) {
  const [value, setValue] = useState("");
  const navigate = useNavigate();
  const { nowPlaying, expanded } = usePlayer();
  const liftForMini = Boolean(nowPlaying && !expanded);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const q = value.trim();
    if (q.length >= 2) {
      navigate(`/search?q=${encodeURIComponent(q)}`);
    }
  };

  const hasFilter = Boolean(onGenreToggle);

  return (
    <div className="text-center pt-1 pb-3 lg:pt-4 lg:pb-12">
      {/* Mobile: bottom floating pill — no outer tray */}
      <div className={FLOATING_SEARCH_WRAP(liftForMini)}>
        <form onSubmit={handleSubmit} className={FLOATING_SEARCH_INNER}>
          <input
            type="text"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="Search by title, author, or ISBN..."
            className={FLOATING_SEARCH_INPUT({ hasFilter })}
            aria-label="Search catalog"
          />
          {hasFilter && (
            <button
              type="button"
              onClick={onGenreToggle}
              className={FLOATING_SEARCH_FILTER}
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
            className={FLOATING_SEARCH_ACTION}
            aria-label="Search"
            title="Search"
          >
            <Search size={18} strokeWidth={2.25} />
          </button>
        </form>
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
      <form onSubmit={handleSubmit} className="relative max-w-xl mx-auto hidden lg:block">
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
    </div>
  );
}
