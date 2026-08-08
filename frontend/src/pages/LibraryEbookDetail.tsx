import { useParams, Link, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import api from "../api/client";
import { resolveShareOrigin } from "../api/inviteLink";
import { useToast } from "../contexts/ToastContext";
import { useAuth } from "../hooks/useAuth";
import {
  BookOpen,
  ChevronLeft,
  Headphones,
  Loader2,
  Share2,
  Store,
  TabletSmartphone,
  Tags,
  Trash2,
} from "lucide-react";
import CoverImage from "../components/CoverImage";
import SaveOfflineButton from "../components/SaveOfflineButton";
import Modal from "../components/Modal";
import EbookMetadataMatcher from "../components/admin/EbookMetadataMatcher";
import { useOnlineStatus } from "../hooks/useOnlineStatus";
import { softRefreshLibraryCollectionQueries } from "../utils/shelfQueryCache";
import { getProgress } from "../utils/readingProgress";
import { libraryQueryKey } from "../utils/libraryQueryKeys";

interface EbookVolume {
  volumeId: number | null;
  volumeNumber: number | null;
  chapterId: number;
  title: string;
  author?: string | null;
  description?: string | null;
  coverUrl?: string | null;
  fileKey?: string | null;
  fileName?: string | null;
  seriesName?: string | null;
  sequence?: string | null;
}

interface EbookItemDetail {
  seriesId: number;
  title: string;
  author: string;
  description: string;
  genres: string[];
  series: Array<{ name: string; sequence: string }>;
  chapterId: number | null;
  coverUrl: string;
  volumes?: EbookVolume[];
  absItemId?: string | null;
}

export default function LibraryEbookDetail() {
  const { seriesId: rawId } = useParams<{ seriesId: string }>();
  const seriesId = rawId ? Number(rawId) : NaN;
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const { user } = useAuth();
  const online = useOnlineStatus();
  const isAdmin = user?.role === "admin";
  const [storeLoading, setStoreLoading] = useState(false);
  const [showDelete, setShowDelete] = useState(false);
  const [showEditMetadata, setShowEditMetadata] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [shareBusy, setShareBusy] = useState(false);
  const [ereaderBusy, setEreaderBusy] = useState(false);

  const { data: item, isLoading, error } = useQuery({
    queryKey: ["kavita-item-detail", seriesId],
    queryFn: async () => {
      const { data } = await api.get(`/library/kavita/item/${seriesId}`);
      return data as EbookItemDetail;
    },
    enabled: Number.isFinite(seriesId),
    staleTime: 5 * 60 * 1000,
  });

  const volumes = item?.volumes || [];
  const preferredChapter = Number(searchParams.get("chapter") || "");
  const preferredFile = (searchParams.get("file") || "").trim().toLowerCase();
  const activeVolume = useMemo(() => {
    if (preferredFile) {
      const byFile = volumes.find(
        (v) =>
          (v.fileKey || "").toLowerCase() === preferredFile ||
          (v.fileName || "").toLowerCase() === preferredFile ||
          (v.title || "").toLowerCase() === preferredFile,
      );
      if (byFile) return byFile;
    }
    if (Number.isFinite(preferredChapter) && preferredChapter > 0) {
      const matches = volumes.filter((v) => v.chapterId === preferredChapter);
      if (matches.length === 1) return matches[0];
      if (matches.length > 1) return matches[0];
    }
    const fallbackChapter = item?.chapterId ?? volumes[0]?.chapterId ?? null;
    return volumes.find((v) => v.chapterId === fallbackChapter) || volumes[0] || null;
  }, [item?.chapterId, preferredChapter, preferredFile, volumes]);
  const activeChapterId = activeVolume?.chapterId ?? null;
  const displayTitle = activeVolume?.title || item?.title || "";
  const displayAuthor = activeVolume?.author || item?.author || "";
  const multiVolume = volumes.length > 1;
  // Prefer the selected volume's synopsis; avoid showing volume-1/series blurb for other volumes.
  const displayDescription = multiVolume
    ? (activeVolume?.description || "").trim()
    : (activeVolume?.description || item?.description || "").trim();
  const displayCover =
    (activeVolume?.coverUrl || "").trim() ||
    (item?.coverUrl || "").trim() ||
    "";
  const seriesName =
    (activeVolume?.seriesName || "").trim() ||
    (item?.series || []).find((s) => s.name)?.name ||
    (multiVolume ? item?.title || "" : "");
  const seriesSeq =
    (activeVolume?.sequence || "").trim() ||
    (activeVolume?.volumeNumber != null && activeVolume.volumeNumber > 0
      ? String(
          Number.isInteger(activeVolume.volumeNumber)
            ? activeVolume.volumeNumber
            : activeVolume.volumeNumber,
        )
      : "") ||
    (item?.series || []).find((s) => s.name)?.sequence ||
    "";
  const seriesLine = seriesName
    ? seriesSeq
      ? `${seriesName} #${seriesSeq}`
      : seriesName
    : "";
  const readingProgress = activeChapterId != null ? getProgress(activeChapterId) : null;
  const progressLabel = (() => {
    if (!readingProgress) return null;
    const total =
      readingProgress.totalViewportPages ||
      readingProgress.totalKavitaPages ||
      0;
    const page = (readingProgress.viewportPage || 0) + 1;
    if (total > 0) return `Page ${page} of ${total}`;
    if (readingProgress.page > 0) return `Page ${readingProgress.page}`;
    return "In progress";
  })();

  const handleViewInStore = async () => {
    if (!item) return;
    setStoreLoading(true);
    try {
      const q = displayAuthor
        ? `intitle:${JSON.stringify(displayTitle)} inauthor:${displayAuthor}`
        : displayTitle;
      const { data } = await api.get(`/books/search?q=${encodeURIComponent(q)}&pageSize=5`);
      const books = (data as { books?: { id: string; title: string }[] })?.books;
      if (books?.length) {
        const titleLower = displayTitle.toLowerCase();
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
      queryClient.setQueryData(libraryQueryKey("kavita-collection"), (prev: unknown) => {
        if (!prev || typeof prev !== "object") return prev;
        const data = prev as {
          items?: Array<{ seriesId?: number }>;
          totalItems?: number;
        };
        const items = (data.items || []).filter((it) => it?.seriesId !== seriesId);
        return { ...data, items, totalItems: items.length };
      });
      void softRefreshLibraryCollectionQueries(queryClient);
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

  const canShare = user?.role === "admin" || !!user?.canShareBooks;

  const handleSendToEreader = async () => {
    if (!item || activeChapterId == null || !Number.isFinite(seriesId)) return;
    setEreaderBusy(true);
    try {
      const { data } = await api.post("/auth/ereader/shelf", {
        series_id: seriesId,
        chapter_id: activeChapterId,
        title: displayTitle,
        author: displayAuthor,
        cover_url: displayCover,
      });
      const added = (data as { added?: { downloadUrl?: string } })?.added;
      const downloadUrl = (added?.downloadUrl || "").trim();
      if (downloadUrl) {
        try {
          await navigator.clipboard.writeText(downloadUrl);
          toast("Added to ereader shelf — download link copied", "success");
        } catch {
          toast("Added to ereader shelf — open Settings → Ereader for OPDS", "success");
        }
      } else {
        toast("Added to ereader shelf — open Settings → Ereader for OPDS", "success");
      }
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Could not send to ereader";
      toast(typeof detail === "string" ? detail : "Could not send to ereader", "error");
    } finally {
      setEreaderBusy(false);
    }
  };

  const handleShare = async () => {
    if (!item || activeChapterId == null || !Number.isFinite(seriesId)) return;
    setShareBusy(true);
    try {
      const { data } = await api.post("/share", {
        media_type: "ebook",
        series_id: seriesId,
        chapter_id: activeChapterId,
        title: displayTitle,
      });
      const share = data as { path?: string; token?: string; url?: string };
      const path = share.path || (share.token ? `/share/${share.token}` : "");
      const serverUrl = (share.url || "").trim();
      const origin = resolveShareOrigin();
      const url = serverUrl.startsWith("http")
        ? serverUrl
        : origin
          ? `${origin}${path}`
          : `${window.location.origin}${path}`;
      if (navigator.share) {
        try {
          await navigator.share({ title: displayTitle || "Shared ebook", text: `Read ${displayTitle}`, url });
          toast("Share sheet opened", "success");
          return;
        } catch (err) {
          if ((err as { name?: string })?.name === "AbortError") return;
        }
      }
      await navigator.clipboard.writeText(url);
      toast("Share link copied", "success");
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || "Could not create share link";
      toast(typeof detail === "string" ? detail : "Could not create share link", "error");
    } finally {
      setShareBusy(false);
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

  const cover = displayCover ? (
    <CoverImage
      key={displayCover}
      src={displayCover}
      alt={displayTitle}
      className="w-full rounded-xl shadow-2xl shadow-black/40"
    />
  ) : (
    <div className="w-full aspect-[2/3] bg-gray-800 rounded-xl flex items-center justify-center text-gray-700">
      <BookOpen size={48} />
    </div>
  );

  const iconBtn =
    "inline-flex items-center justify-center w-11 h-11 rounded-xl bg-gray-800 text-gray-300 border border-gray-700 hover:bg-gray-700 transition-colors disabled:opacity-50";
  const iconBtnDanger =
    "inline-flex items-center justify-center w-11 h-11 rounded-xl bg-red-950/50 text-red-300 border border-red-800/60 hover:bg-red-900/60 hover:text-red-200 transition-colors";
  const mobileIconBtn =
    "inline-flex items-center justify-center flex-1 min-w-0 h-12 max-w-[4.5rem] rounded-xl bg-gray-800 text-gray-300 border border-gray-700 hover:bg-gray-700 transition-colors disabled:opacity-50";
  const mobileIconBtnDanger =
    "inline-flex items-center justify-center flex-1 min-w-0 h-12 max-w-[4.5rem] rounded-xl bg-red-950/50 text-red-300 border border-red-800/60 hover:bg-red-900/60 hover:text-red-200 transition-colors";

  const iconActions = (variant: "desktop" | "mobile") => {
    const btn = variant === "mobile" ? mobileIconBtn : iconBtn;
    const danger = variant === "mobile" ? mobileIconBtnDanger : iconBtnDanger;
    const iconSize = variant === "mobile" ? 20 : 18;
    const offlineClass =
      variant === "mobile"
        ? "!h-12 !w-auto flex-1 min-w-0 max-w-[4.5rem] !rounded-xl"
        : undefined;
    return (
      <>
        {activeChapterId != null && (
          <SaveOfflineButton
            target={{
              kind: "ebook",
              chapterId: activeChapterId,
              title: displayTitle,
              author: displayAuthor,
              coverUrl: displayCover,
            }}
            iconOnly
            className={offlineClass}
          />
        )}
        {activeChapterId != null && (
          <button
            type="button"
            onClick={() => void handleSendToEreader()}
            disabled={ereaderBusy || !online}
            title="Send to ereader"
            aria-label="Send to ereader"
            className={btn}
          >
            {ereaderBusy ? <Loader2 size={iconSize} className="animate-spin" /> : <TabletSmartphone size={iconSize} />}
          </button>
        )}
        {canShare && activeChapterId != null && (
          <button
            type="button"
            onClick={() => void handleShare()}
            disabled={shareBusy}
            title="Share"
            aria-label="Share"
            className={btn}
          >
            {shareBusy ? <Loader2 size={iconSize} className="animate-spin" /> : <Share2 size={iconSize} />}
          </button>
        )}
        {item.absItemId && (
          <button
            type="button"
            onClick={() => navigate(`/library/abs/${encodeURIComponent(item.absItemId!)}`)}
            title="Listen"
            aria-label="Listen"
            className={btn}
          >
            <Headphones size={iconSize} />
          </button>
        )}
        <button
          type="button"
          onClick={() => void handleViewInStore()}
          disabled={storeLoading || !online}
          title="View in Browse"
          aria-label="View in Browse"
          className={btn}
        >
          {storeLoading ? <Loader2 size={iconSize} className="animate-spin" /> : <Store size={iconSize} />}
        </button>
        {isAdmin && (
          <button
            type="button"
            onClick={() => setShowEditMetadata(true)}
            title="Edit metadata"
            aria-label="Edit metadata"
            className={btn}
          >
            <Tags size={iconSize} />
          </button>
        )}
        {isAdmin && (
          <button
            type="button"
            onClick={() => setShowDelete(true)}
            title="Delete"
            aria-label="Delete"
            className={danger}
          >
            <Trash2 size={iconSize} />
          </button>
        )}
      </>
    );
  };

  const metaDetails = () => (
    <>
      {displayAuthor && (
        <p className="text-gray-300 mt-1 md:mt-3 text-sm md:text-base">
          by <span className="text-gray-100 font-medium">{displayAuthor}</span>
        </p>
      )}
      {seriesLine && <p className="text-xs md:text-sm text-brand-400 mt-0.5 md:mt-1">{seriesLine}</p>}
      {!seriesLine && multiVolume && item.title && (
        <p className="text-xs md:text-sm text-brand-400 mt-0.5 md:mt-1">{item.title}</p>
      )}
      {progressLabel && (
        <p className="text-[11px] md:text-xs text-amber-400/90 mt-1.5 md:mt-2">{progressLabel}</p>
      )}
      {(item.genres || []).length > 0 && (
        <div className="flex flex-wrap gap-1 md:gap-1.5 mt-2 md:mt-3">
          {item.genres.map((g) => (
            <span
              key={g}
              className="px-1.5 md:px-2 py-0.5 text-[9px] md:text-[10px] bg-gray-800 text-gray-300 rounded-full border border-gray-700"
            >
              {g}
            </span>
          ))}
        </div>
      )}
    </>
  );

  return (
    <div className="max-w-5xl mx-auto px-4 py-8">
      <div className="mb-5">
        <button
          type="button"
          onClick={() => window.history.back()}
          title="Back"
          aria-label="Back"
          className="inline-flex items-center justify-center w-10 h-10 rounded-full bg-gradient-to-br from-gray-800 to-gray-900 text-gray-200 border border-gray-600/80 shadow-md shadow-black/30 hover:from-gray-700 hover:to-gray-800 hover:text-white hover:border-gray-500 transition-all"
        >
          <ChevronLeft size={22} strokeWidth={2.5} className="-ml-0.5" />
        </button>
      </div>

      <div className="md:hidden mb-5">
        <div className="flex gap-3 items-start">
          <div className="w-[9.5rem] shrink-0">{cover}</div>
          <div className="flex-1 min-w-0 pt-0.5">
            <h1 className="text-lg font-bold text-gray-100 leading-snug line-clamp-3">{displayTitle}</h1>
            {metaDetails()}
          </div>
        </div>
        {activeChapterId != null && (
          <button
            type="button"
            onClick={() => navigate(`/read/${activeChapterId}`)}
            className="mt-4 w-full inline-flex items-center justify-center gap-2 px-4 py-3 text-base font-semibold rounded-xl bg-amber-600 text-white hover:bg-amber-500 transition-colors"
          >
            <BookOpen size={20} />
            {readingProgress ? "Continue" : "Read"}
          </button>
        )}
        <div className="mt-3 flex w-full items-center justify-center gap-2">
          {iconActions("mobile")}
        </div>
      </div>

      <div className="flex flex-col md:flex-row gap-8">
        <div className="hidden md:block w-64 shrink-0">{cover}</div>
        <div className="flex-1 min-w-0">
          <div className="hidden md:block">
            <h1 className="text-2xl sm:text-3xl font-bold text-gray-100 leading-tight">{displayTitle}</h1>
            {metaDetails()}
          </div>

          <div className="hidden md:flex flex-wrap items-center gap-2 mt-4">
            {activeChapterId != null && (
              <button
                type="button"
                onClick={() => navigate(`/read/${activeChapterId}`)}
                title={readingProgress ? "Continue" : "Read"}
                aria-label={readingProgress ? "Continue" : "Read"}
                className="inline-flex items-center justify-center w-11 h-11 rounded-xl bg-amber-600 text-white border border-amber-500 hover:bg-amber-500 transition-colors"
              >
                <BookOpen size={18} />
              </button>
            )}
            {iconActions("desktop")}
          </div>

          {multiVolume && (
            <div className="mt-6">
              <h2 className="text-lg font-semibold text-gray-100 mb-3">
                Volumes in series ({volumes.length})
              </h2>
              <ul className="space-y-1.5">
                {volumes.map((vol) => {
                  const active =
                    (vol.fileKey && activeVolume?.fileKey
                      ? vol.fileKey === activeVolume.fileKey
                      : vol.chapterId === activeChapterId) &&
                    (!activeVolume?.fileName || !vol.fileName || vol.fileName === activeVolume.fileName);
                  const fileQs = vol.fileName
                    ? `&file=${encodeURIComponent(vol.fileName)}`
                    : vol.fileKey
                      ? `&file=${encodeURIComponent(vol.fileKey)}`
                      : "";
                  const volProgress = getProgress(vol.chapterId);
                  return (
                    <li key={`${vol.chapterId}-${vol.fileKey || vol.fileName || vol.title}`}>
                      <button
                        type="button"
                        onClick={() =>
                          navigate(
                            `/library/ebook/${seriesId}?chapter=${vol.chapterId}${fileQs}`,
                            { replace: true },
                          )
                        }
                        className={`w-full text-left px-3 py-2 rounded-lg border text-sm transition-colors ${
                          active
                            ? "bg-brand-900/40 border-brand-700 text-brand-200"
                            : "bg-gray-900/40 border-gray-800 text-gray-300 hover:bg-gray-800/60"
                        }`}
                      >
                        <span className="text-gray-500 mr-2">
                          {vol.volumeNumber != null && vol.volumeNumber > 0
                            ? `#${Number.isInteger(vol.volumeNumber) ? vol.volumeNumber : vol.volumeNumber}`
                            : "•"}
                        </span>
                        {vol.title}
                        {volProgress && (
                          <span className="ml-2 text-[10px] uppercase tracking-wide text-amber-400/80">
                            In progress
                          </span>
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}

          {displayDescription && (
            <div className="mt-6">
              <h2 className="text-lg font-semibold text-gray-100 mb-3">Synopsis</h2>
              <div
                className="text-gray-300 text-sm leading-relaxed prose prose-invert prose-sm max-w-none"
                dangerouslySetInnerHTML={{ __html: displayDescription }}
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
            {deleting ? "Deleting…" : "Delete"}
          </button>
        </div>
      </Modal>

      {isAdmin && Number.isFinite(seriesId) && (
        <EbookMetadataMatcher
          seriesId={seriesId}
          chapterId={activeChapterId}
          targetFilename={activeVolume?.fileName || activeVolume?.title || null}
          title={displayTitle || item.title}
          open={showEditMetadata}
          onClose={() => setShowEditMetadata(false)}
          onApplied={() => {
            void queryClient.invalidateQueries({ queryKey: ["kavita-item-detail", seriesId] });
            void softRefreshLibraryCollectionQueries(queryClient);
          }}
        />
      )}
    </div>
  );
}
