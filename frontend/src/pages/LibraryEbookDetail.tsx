import { useParams, Link, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import api from "../api/client";
import { useToast } from "../contexts/ToastContext";
import { useAuth } from "../hooks/useAuth";
import {
  ArrowLeft, BookOpen, Headphones, Loader2, Store, Trash2,
} from "lucide-react";
import CoverImage from "../components/CoverImage";
import SaveOfflineButton from "../components/SaveOfflineButton";
import Modal from "../components/Modal";
import { useOnlineStatus } from "../hooks/useOnlineStatus";
import { purgeLibraryCollectionQueries } from "../utils/shelfQueryCache";

interface EbookItemDetail {
  seriesId: number;
  title: string;
  author: string;
  description: string;
  genres: string[];
  series: Array<{ name: string; sequence: string }>;
  chapterId: number | null;
  coverUrl: string;
  absItemId?: string | null;
}

export default function LibraryEbookDetail() {
  const { seriesId: rawId } = useParams<{ seriesId: string }>();
  const seriesId = rawId ? Number(rawId) : NaN;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const { user } = useAuth();
  const online = useOnlineStatus();
  const isAdmin = user?.role === "admin";
  const [storeLoading, setStoreLoading] = useState(false);
  const [showDelete, setShowDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const { data: item, isLoading, error } = useQuery({
    queryKey: ["kavita-item-detail", seriesId],
    queryFn: async () => {
      const { data } = await api.get(`/library/kavita/item/${seriesId}`);
      return data as EbookItemDetail;
    },
    enabled: Number.isFinite(seriesId),
    staleTime: 5 * 60 * 1000,
  });

  const handleViewInStore = async () => {
    if (!item) return;
    setStoreLoading(true);
    try {
      const q = item.author
        ? `intitle:${JSON.stringify(item.title)} inauthor:${item.author}`
        : item.title;
      const { data } = await api.get(`/books/search?q=${encodeURIComponent(q)}&pageSize=5`);
      const books = (data as { books?: { id: string; title: string }[] })?.books;
      if (books?.length) {
        const titleLower = item.title.toLowerCase();
        const match =
          books.find((b) => {
            const bt = b.title.toLowerCase();
            return bt === titleLower || bt.includes(titleLower) || titleLower.includes(bt);
          }) || books[0];
        navigate(`/book/${encodeURIComponent(match.id)}`);
      } else {
        toast("No store page found for this book", "info");
      }
    } catch {
      toast("Couldn't reach the store catalog", "error");
    } finally {
      setStoreLoading(false);
    }
  };

  const handleDelete = async () => {
    if (!Number.isFinite(seriesId)) return;
    setDeleting(true);
    try {
      await api.delete(`/admin/library/ebook/${seriesId}`);
      await purgeLibraryCollectionQueries(queryClient, { refetch: true });
      toast("Ebook deleted from library", "success");
      setShowDelete(false);
      navigate("/my-library", { replace: true });
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Failed to delete ebook";
      toast(typeof detail === "string" ? detail : "Failed to delete ebook", "error");
    } finally {
      setDeleting(false);
    }
  };

  if (isLoading) {
    return (
      <div className="max-w-5xl mx-auto px-4 py-8">
        <div className="animate-pulse">
          <div className="h-6 w-24 bg-gray-800 rounded mb-8" />
          <div className="flex flex-col md:flex-row gap-8">
            <div className="w-28 md:w-64 shrink-0 aspect-[2/3] bg-gray-800 rounded-xl" />
            <div className="flex-1 space-y-4">
              <div className="h-8 bg-gray-800 rounded w-3/4" />
              <div className="h-5 bg-gray-800 rounded w-1/2" />
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (error || !item) {
    return (
      <div className="max-w-5xl mx-auto px-4 py-16 text-center">
        <p className="text-gray-400">Ebook not found in your library</p>
        <Link to="/my-library" className="text-brand-400 hover:text-brand-300 mt-4 inline-block">
          Back to My Library
        </Link>
      </div>
    );
  }

  const seriesLine = (item.series || [])
    .filter((s) => s.name)
    .map((s) => (s.sequence ? `${s.name} #${s.sequence}` : s.name))
    .join(" · ");

  const cover = item.coverUrl ? (
    <CoverImage src={item.coverUrl} alt={item.title} className="w-full rounded-xl shadow-2xl shadow-black/40" />
  ) : (
    <div className="w-full aspect-[2/3] bg-gray-800 rounded-xl flex items-center justify-center text-gray-700">
      <BookOpen size={48} />
    </div>
  );

  const actions = (
    <>
      {item.chapterId != null && (
        <button
          type="button"
          onClick={() => navigate(`/read/${item.chapterId}`)}
          className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-amber-600 text-white hover:bg-amber-500 transition-colors"
        >
          <BookOpen size={16} />
          Read
        </button>
      )}
      {item.absItemId && (
        <button
          type="button"
          onClick={() => navigate(`/library/abs/${encodeURIComponent(item.absItemId!)}`)}
          className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-emerald-600 text-white hover:bg-emerald-500 transition-colors"
        >
          <Headphones size={16} />
          Listen
        </button>
      )}
      {item.chapterId != null && (
        <SaveOfflineButton
          target={{
            kind: "ebook",
            chapterId: item.chapterId,
            title: item.title,
            author: item.author,
            coverUrl: item.coverUrl,
            isPdf: true,
          }}
        />
      )}
      <button
        type="button"
        onClick={handleViewInStore}
        disabled={storeLoading || !online}
        className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-gray-800 text-gray-300 border border-gray-700 hover:bg-gray-700 transition-colors disabled:opacity-50"
      >
        {storeLoading ? <Loader2 size={16} className="animate-spin" /> : <Store size={16} />}
        View in Store
      </button>
      {isAdmin && (
        <button
          type="button"
          onClick={() => setShowDelete(true)}
          className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-red-900/40 text-red-300 border border-red-800/60 hover:bg-red-900/60 transition-colors"
        >
          <Trash2 size={16} />
          Delete
        </button>
      )}
    </>
  );

  return (
    <div className="max-w-5xl mx-auto px-4 py-8">
      <Link
        to={-1 as any}
        className="inline-flex items-center gap-1 text-sm text-gray-400 hover:text-gray-200 transition-colors mb-6"
        onClick={(e) => {
          e.preventDefault();
          window.history.back();
        }}
      >
        <ArrowLeft size={16} />
        Back
      </Link>

      <div className="flex gap-4 mb-5 md:hidden">
        <div className="w-[7.5rem] shrink-0">{cover}</div>
        <div className="flex flex-col gap-2 flex-1 content-start self-start">{actions}</div>
      </div>

      <div className="flex flex-col md:flex-row gap-8">
        <div className="hidden md:block w-64 shrink-0">{cover}</div>
        <div className="flex-1 min-w-0">
          <h1 className="text-2xl sm:text-3xl font-bold text-gray-100 leading-tight">{item.title}</h1>
          {item.author && (
            <p className="text-gray-300 mt-2 sm:mt-3">
              by <span className="text-gray-100 font-medium">{item.author}</span>
            </p>
          )}
          {seriesLine && <p className="text-sm text-brand-400 mt-1">{seriesLine}</p>}

          {(item.genres || []).length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-3">
              {item.genres.map((g) => (
                <span key={g} className="px-2 py-0.5 text-[10px] bg-gray-800 text-gray-300 rounded-full border border-gray-700">
                  {g}
                </span>
              ))}
            </div>
          )}

          <div className="hidden md:flex flex-wrap items-center gap-2 mt-4">{actions}</div>

          {item.description && (
            <div className="mt-6">
              <h2 className="text-lg font-semibold text-gray-100 mb-3">Synopsis</h2>
              <div
                className="text-gray-300 text-sm leading-relaxed prose prose-invert prose-sm max-w-none"
                dangerouslySetInnerHTML={{ __html: item.description }}
              />
            </div>
          )}
        </div>
      </div>

      <Modal title="Delete ebook" show={showDelete} onClose={() => !deleting && setShowDelete(false)}>
        <p className="text-sm text-gray-400 mb-4">
          Permanently delete <span className="text-gray-200">{item.title}</span> from the library?
          Ebook files on disk and the Kavita entry will be removed. This cannot be undone.
        </p>
        <div className="flex gap-2 justify-end">
          <button
            type="button"
            onClick={() => setShowDelete(false)}
            disabled={deleting}
            className="px-3 py-1.5 text-gray-300 hover:bg-gray-700 rounded-lg disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void handleDelete()}
            disabled={deleting}
            className="px-3 py-1.5 bg-red-600 text-white rounded-lg hover:bg-red-500 disabled:opacity-50"
          >
            {deleting ? "Deleting..." : "Delete"}
          </button>
        </div>
      </Modal>
    </div>
  );
}
