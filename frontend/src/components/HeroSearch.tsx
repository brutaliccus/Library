import { useState, FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Search } from "lucide-react";

export default function HeroSearch() {
  const [value, setValue] = useState("");
  const navigate = useNavigate();

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const q = value.trim();
    if (q.length >= 2) {
      navigate(`/search?q=${encodeURIComponent(q)}`);
    }
  };

  return (
    <div className="text-center py-12">
      <h1 className="text-4xl font-bold text-gray-100 mb-2">
        Find your next read
      </h1>
      <p className="text-gray-400 mb-8">
        Search books you can download — matched against our indexer cache
      </p>
      <p className="text-xs text-gray-500 mb-4">
        Catalog powered by Open Library. Only books with cached torrents show by default.
      </p>
      <form onSubmit={handleSubmit} className="relative max-w-xl mx-auto">
        <Search
          size={22}
          className="absolute left-4 top-1/2 -translate-y-1/2 text-gray-500"
        />
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Search by title, author, or ISBN..."
          className="w-full pl-12 pr-24 py-4 bg-gray-800 border border-gray-700 rounded-2xl text-base text-gray-100 shadow-lg focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent placeholder:text-gray-500"
        />
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
