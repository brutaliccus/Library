import { useState, useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import api from "../api/client";
import SearchBar from "../components/SearchBar";
import ResultCard from "../components/ResultCard";
import { useToast } from "../contexts/ToastContext";
import { BookOpen, Headphones, BookText } from "lucide-react";

interface SearchResult {
  title: string;
  size: number;
  seeders: number;
  leechers: number;
  indexer: string;
  magnetUrl: string | null;
  downloadUrl: string | null;
  mediaType?: string;
  inLibrary?: string[];
}

type MediaFilter = "all" | "audiobook" | "ebook";

export default function SearchPage() {
  const { toast } = useToast();
  const [query, setQuery] = useState("");
  const [requestingIdx, setRequestingIdx] = useState<number | null>(null);
  const [mediaFilter, setMediaFilter] = useState<MediaFilter>("all");

  const { data, isLoading, error } = useQuery({
    queryKey: ["search", query],
    queryFn: async () => {
      if (!query) return null;
      const { data } = await api.get(`/search?q=${encodeURIComponent(query)}`);
      return data as { results: SearchResult[]; count: number };
    },
    enabled: query.length >= 2,
  });

  const handleSearch = useCallback((q: string) => setQuery(q), []);

  const filteredResults = useMemo(() => {
    if (!data?.results) return [];
    if (mediaFilter === "all") return data.results;
    return data.results.filter((r) => r.mediaType === mediaFilter);
  }, [data, mediaFilter]);

  const handleRequest = async (result: SearchResult, index: number, mediaTypeOverride: string) => {
    const link = result.magnetUrl || result.downloadUrl;
    if (!link) return;
    setRequestingIdx(index);
    try {
      const mediaType = mediaTypeOverride || result.mediaType || "unknown";
      await api.post("/requests", {
        title: result.title,
        magnet_link: result.magnetUrl || undefined,
        download_url: result.downloadUrl || undefined,
        indexer: result.indexer,
        size_bytes: result.size,
        media_type: mediaType,
      });
      const dest = mediaType === "ebook" ? "Kavita" : "Audiobookshelf";
      toast(`Requested "${result.title}". It will be added to ${dest}. Check your requests page for status.`, "success");
    } catch (err: any) {
      toast(err.response?.data?.detail || "Failed to create request", "error");
    } finally {
      setRequestingIdx(null);
    }
  };

  return (
    <div className="max-w-4xl mx-auto px-4 py-8">
      <div className="mb-8">
        <SearchBar onSearch={handleSearch} isLoading={isLoading} />
      </div>

      {!query && (
        <div className="text-center py-20 text-gray-500">
          <BookOpen size={48} className="mx-auto mb-4 opacity-50" />
          <p className="text-lg">Search for an audiobook or ebook to get started</p>
        </div>
      )}

      {error && (
        <div className="p-4 bg-red-900/30 text-red-400 rounded-xl text-sm">
          Search failed. Please try again.
        </div>
      )}

      {data && data.results.length === 0 && (
        <div className="text-center py-16 text-gray-500">
          <p>No results found for "{query}"</p>
        </div>
      )}

      {data && data.results.length > 0 && (
        <div className="space-y-3">
          <div className="flex items-center justify-between mb-4">
            <p className="text-sm text-gray-400">
              {filteredResults.length} of {data.count} result{data.count !== 1 ? "s" : ""}
            </p>
            <div className="flex gap-1 bg-gray-900 rounded-lg p-1">
              {([
                { id: "all" as MediaFilter, label: "All", icon: null },
                { id: "audiobook" as MediaFilter, label: "Audiobooks", icon: Headphones },
                { id: "ebook" as MediaFilter, label: "eBooks", icon: BookText },
              ]).map(({ id, label, icon: Icon }) => (
                <button
                  key={id}
                  onClick={() => setMediaFilter(id)}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
                    mediaFilter === id
                      ? "bg-gray-800 text-white shadow-sm"
                      : "text-gray-400 hover:text-gray-200"
                  }`}
                >
                  {Icon && <Icon size={13} />}
                  {label}
                </button>
              ))}
            </div>
          </div>
          {filteredResults.length === 0 ? (
            <div className="text-center py-12 text-gray-500">
              <p>No {mediaFilter === "audiobook" ? "audiobook" : "ebook"} results found</p>
            </div>
          ) : (
            filteredResults.map((result) => {
              const origIdx = data.results.indexOf(result);
              return (
                <ResultCard
                  key={`${result.title}-${result.indexer}-${result.size}`}
                  result={result}
                  onRequest={(r, typeOverride) => handleRequest(r, origIdx, typeOverride)}
                  requesting={requestingIdx === origIdx}
                />
              );
            })
          )}
        </div>
      )}
    </div>
  );
}
