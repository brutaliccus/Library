import { useState, useEffect, useCallback, useRef } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import axios from "axios";
import api from "../api/client";
import { getApiBaseUrl, toAbsoluteUrl } from "../api/instanceUrl";
import PdfViewer from "../components/PdfViewer";
import { cacheBookEbook } from "../utils/ebookCache";
import {
  cacheEbookPageHtml,
  getCachedEbookPage,
} from "../utils/ebookPageCache";
import {
  getEbookOfflineManifest,
  isEbookOfflineReady,
  saveEbookOfflineManifest,
} from "../utils/offlinePlayback";
import { isLikelyOffline, isNetworkError } from "../utils/networkStatus";
import { getProgress, saveProgress } from "../utils/readingProgress";
import {
  ChevronLeft,
  ChevronRight,
  Menu,
  Minus,
  Plus,
  X,
  Maximize2,
  Minimize2,
  Settings,
} from "lucide-react";

/** Kavita MangaFormat: Pdf = 4 */
const KAVITA_PDF_FORMAT = 4;

const READER_SETTINGS_KEY = "ereader-settings";
const FONT_SIZE_MIN = 10;
const FONT_SIZE_MAX = 32;
const FONT_SIZE_STEP = 2;

interface ReaderSettings {
  /** Point size (10–32). Legacy small/medium/large values are migrated on load. */
  fontSize: number;
  fontFamily: "serif" | "sans-serif" | "monospace";
}

const defaultSettings: ReaderSettings = {
  fontSize: 16,
  fontFamily: "serif",
};

function clampFontSize(n: number): number {
  const stepped = Math.round(n / FONT_SIZE_STEP) * FONT_SIZE_STEP;
  return Math.min(FONT_SIZE_MAX, Math.max(FONT_SIZE_MIN, stepped));
}

function migrateFontSize(raw: unknown): number {
  if (typeof raw === "number" && Number.isFinite(raw)) return clampFontSize(raw);
  if (raw === "small") return 12;
  if (raw === "large") return 20;
  if (raw === "medium") return 16;
  return defaultSettings.fontSize;
}

function loadSettings(): ReaderSettings {
  try {
    const s = localStorage.getItem(READER_SETTINGS_KEY);
    if (s) {
      const parsed = JSON.parse(s) as Partial<ReaderSettings> & { fontSize?: unknown };
      return {
        fontFamily: parsed.fontFamily || defaultSettings.fontFamily,
        fontSize: migrateFontSize(parsed.fontSize),
      };
    }
  } catch {
    /* ignore */
  }
  return defaultSettings;
}

function saveSettings(s: ReaderSettings) {
  try {
    localStorage.setItem(READER_SETTINGS_KEY, JSON.stringify(s));
  } catch {
    /* ignore */
  }
}

const FONT_FAMILIES = {
  serif: "Georgia, 'Times New Roman', serif",
  "sans-serif": "system-ui, -apple-system, sans-serif",
  monospace: "'JetBrains Mono', 'Fira Code', monospace",
};

interface BookInfo {
  bookTitle: string;
  seriesName: string;
  pages: number;
  chapterTitle?: string;
  seriesFormat?: number;
}

interface ChapterItem {
  title?: string;
  part?: string;
  page: number;
  children?: ChapterItem[];
}

const SWIPE_THRESHOLD = 60;

export default function Ereader() {
  const { chapterId } = useParams<{ chapterId: string }>();
  const [searchParams] = useSearchParams();
  const shareToken = (searchParams.get("share") || "").trim() || null;
  const navigate = useNavigate();
  const [bookInfo, setBookInfo] = useState<BookInfo | null>(null);
  const [chapters, setChapters] = useState<ChapterItem[]>([]);
  const [page, setPage] = useState(0);
  const [content, setContent] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tocOpen, setTocOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settings, setSettingsState] = useState<ReaderSettings>(loadSettings);
  const [fullscreen, setFullscreen] = useState(false);
  /** In fullscreen, chrome (header/footer) is hidden until center-tap reveals it. */
  const [chromeHidden, setChromeHidden] = useState(false);
  const [viewportPage, setViewportPage] = useState(0);
  const [totalViewportPages, setTotalViewportPages] = useState(1);
  const [pageHeight, setPageHeight] = useState(400);
  const [pageOffsets, setPageOffsets] = useState<number[]>([0]);
  const [contentHeight, setContentHeight] = useState(0);
  const [pdfPageCount, setPdfPageCount] = useState(0);
  const pageCache = useRef<Map<number, string>>(new Map());
  const pageObjectUrls = useRef<string[]>([]);

  const contentRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const touchStartX = useRef<number>(0);
  const touchStartY = useRef<number>(0);
  const pendingRestore = useRef<{ page: number; viewportPage: number } | null>(null);
  /** Fraction through content (0–1) to restore after font-size / remasure. */
  const positionAnchorRef = useRef<number | null>(null);
  const showChrome = !fullscreen || !chromeHidden;

  const cid = chapterId ? parseInt(chapterId, 10) : NaN;

  const readerGet = useCallback(
    async <T,>(path: string, opts?: { responseType?: "text" | "json"; params?: Record<string, unknown> }) => {
      if (shareToken) {
        const base = getApiBaseUrl();
        const url = `${base}/share/${encodeURIComponent(shareToken)}/ebook/${path}`;
        const { data } = await axios.get(url, {
          params: opts?.params,
          responseType: opts?.responseType === "text" ? "text" : "json",
        });
        return data as T;
      }
      const { data } = await api.get(`/library/reader/${cid}/${path}`, {
        params: opts?.params,
        responseType: opts?.responseType === "text" ? "text" : undefined,
      });
      return data as T;
    },
    [shareToken, cid]
  );

  const revokePageObjectUrls = useCallback(() => {
    for (const u of pageObjectUrls.current) {
      try {
        URL.revokeObjectURL(u);
      } catch {
        /* ignore */
      }
    }
    pageObjectUrls.current = [];
  }, []);

  const setSettings = useCallback((s: ReaderSettings | ((prev: ReaderSettings) => ReaderSettings)) => {
    setSettingsState((prev) => {
      const next = typeof s === "function" ? s(prev) : s;
      saveSettings(next);
      return next;
    });
  }, []);

  const handlePdfReady = useCallback((total: number) => {
    setPdfPageCount(total);
  }, []);

  const applyProgressRestore = useCallback((chapter: number) => {
    const prog = getProgress(chapter);
    if (prog) {
      setPage(prog.page);
      setViewportPage(prog.viewportPage);
      pendingRestore.current = { page: prog.page, viewportPage: prog.viewportPage };
    } else {
      setPage(0);
      setViewportPage(0);
      pendingRestore.current = null;
    }
  }, []);

  const loadBookInfo = useCallback(async () => {
    if (!cid || isNaN(cid)) return;
    try {
      const data = await readerGet<BookInfo>("book-info");
      setBookInfo(data);
      applyProgressRestore(cid);
      setError(null);
    } catch (e: unknown) {
      // Offline / unreachable: hydrate from Save-offline manifest (same idea as audio).
      if (shareToken) {
        setError("Failed to load shared book");
        return;
      }
      const manifest = getEbookOfflineManifest(cid);
      const ready = manifest ? await isEbookOfflineReady(cid) : false;
      if (manifest && ready && (isLikelyOffline() || isNetworkError(e))) {
        setBookInfo({
          bookTitle: manifest.title,
          seriesName: manifest.title,
          pages: manifest.pages || (manifest.isPdf ? 0 : 1),
          seriesFormat: manifest.isPdf ? KAVITA_PDF_FORMAT : 3,
        });
        applyProgressRestore(cid);
        setError(null);
        return;
      }
      setError(
        ready
          ? "Failed to load book"
          : "Failed to load book — save this ebook while online to read offline"
      );
    }
  }, [cid, applyProgressRestore, readerGet, shareToken]);

  const loadChapters = useCallback(async () => {
    if (!cid || isNaN(cid)) return;
    try {
      const data = await readerGet<ChapterItem[] | { chapters?: ChapterItem[] }>("chapters");
      const list = Array.isArray(data) ? data : data?.chapters || [];
      setChapters(list);
    } catch {
      setChapters([]);
    }
  }, [cid, readerGet]);

  const loadPage = useCallback(
    async (p: number) => {
      if (!cid || isNaN(cid)) return;
      const cached = pageCache.current.get(p);
      if (cached) {
        revokePageObjectUrls();
        setContent(cached);
        setViewportPage(0);
        setLoading(false);
        return;
      }
      setLoading(true);
      try {
        const data = await readerGet<string>("book-page", {
          params: { page: p },
          responseType: "text",
        });
        pageCache.current.set(p, data);
        revokePageObjectUrls();
        setContent(data);
        setViewportPage(0);
        if (!shareToken) void cacheEbookPageHtml(cid, p, data);
      } catch (e: unknown) {
        if (shareToken) {
          setError("Failed to load page");
          setLoading(false);
          return;
        }
        const offlinePage = await getCachedEbookPage(cid, p);
        if (offlinePage) {
          pageCache.current.set(p, offlinePage.html);
          revokePageObjectUrls();
          pageObjectUrls.current = offlinePage.objectUrls;
          setContent(offlinePage.html);
          setViewportPage(0);
          setError(null);
        } else {
          setError(
            isLikelyOffline() || isNetworkError(e)
              ? "Page not available offline — re-save this ebook while online"
              : "Failed to load page"
          );
        }
      } finally {
        setLoading(false);
      }
    },
    [cid, revokePageObjectUrls, readerGet, shareToken]
  );

  // Prefetch next pages in background
  const prefetchPages = useCallback(
    (current: number) => {
      if (!cid || isNaN(cid) || !bookInfo || bookInfo.seriesFormat === KAVITA_PDF_FORMAT) return;
      const total = bookInfo.pages ?? 0;
      for (let i = 1; i <= 2; i++) {
        const next = current + i;
        if (next < total && !pageCache.current.has(next)) {
          readerGet<string>("book-page", { params: { page: next }, responseType: "text" })
            .then((data) => {
              pageCache.current.set(next, data);
              if (!shareToken) void cacheEbookPageHtml(cid, next, data);
            })
            .catch(() => {});
        }
      }
    },
    [cid, bookInfo, readerGet, shareToken]
  );

  useEffect(() => {
    if (!cid || isNaN(cid)) {
      setError("Invalid chapter");
      setLoading(false);
      return;
    }
    setError(null);
    setLoading(true);
    Promise.all([loadBookInfo(), loadChapters()]).finally(() => setLoading(false));
  }, [cid, loadBookInfo, loadChapters]);

  useEffect(() => {
    if (cid && !isNaN(cid)) {
      pageCache.current.clear();
      setPdfPageCount(0);
      revokePageObjectUrls();
    }
  }, [cid, revokePageObjectUrls]);

  useEffect(() => () => revokePageObjectUrls(), [revokePageObjectUrls]);

  useEffect(() => {
    if (!bookInfo || !cid || isNaN(cid)) return;
    if (isLikelyOffline()) return;
    const isPdf = bookInfo.seriesFormat === KAVITA_PDF_FORMAT;
    saveEbookOfflineManifest({
      chapterId: cid,
      title: bookInfo.bookTitle || bookInfo.seriesName || "Ebook",
      author: "",
      coverUrl: toAbsoluteUrl(`/api/library/reader/cover/chapter/${cid}`),
      isPdf,
      pages: bookInfo.pages,
      pagesCached: isPdf ? undefined : getEbookOfflineManifest(cid)?.pagesCached,
    });
    void cacheBookEbook(cid, isPdf);
  }, [bookInfo, cid]);

  useEffect(() => {
    if (bookInfo?.seriesFormat === KAVITA_PDF_FORMAT) {
      setError(null);
      setLoading(false);
      return;
    }
    if (bookInfo) {
      loadPage(page);
      prefetchPages(page);
    }
  }, [bookInfo, page, loadPage, prefetchPages]);

  useEffect(() => {
    if (bookInfo?.seriesFormat === KAVITA_PDF_FORMAT || loading) return;
    if (bookInfo) {
      prefetchPages(page);
    }
  }, [bookInfo, page, loading, prefetchPages]);

  useEffect(() => {
    if (content) setPageOffsets([0]);
  }, [content]);

  // Restore viewportPage after content and measure (for resume)
  // Don't restore when totalViewportPages is still 1 (initial) — measure hasn't run yet and we'd clamp to 0
  useEffect(() => {
    const pending = pendingRestore.current;
    if (!pending || loading || !content || totalViewportPages <= 0) return;
    if (page !== pending.page) {
      pendingRestore.current = null;
      return;
    }
    if (totalViewportPages === 1 && pending.viewportPage > 0) return; // wait for measure
    const target = Math.min(pending.viewportPage, totalViewportPages - 1);
    setViewportPage(target);
    pendingRestore.current = null;
  }, [content, loading, page, totalViewportPages]);

  // Save progress when page/viewportPage changes (debounced)
  const saveProgressRef = useRef<ReturnType<typeof setTimeout>>();
  const saveProgressImmediate = useCallback(() => {
    if (!cid || !bookInfo) return;
    saveProgress({
      chapterId: cid,
      page,
      viewportPage,
      totalViewportPages,
      totalKavitaPages: bookInfo.pages,
      bookTitle: bookInfo.bookTitle,
      seriesName: bookInfo.seriesName,
      coverUrl: toAbsoluteUrl(`/api/library/reader/cover/chapter/${cid}`),
    });
  }, [cid, bookInfo, page, viewportPage, totalViewportPages]);

  useEffect(() => {
    if (!cid || !bookInfo || loading) return;
    saveProgressRef.current = setTimeout(saveProgressImmediate, 500);
    return () => clearTimeout(saveProgressRef.current);
  }, [cid, bookInfo, page, viewportPage, totalViewportPages, loading, saveProgressImmediate]);

  // Save immediately when user leaves (tab close, navigate away) so we don't lose progress
  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.visibilityState === "hidden") saveProgressImmediate();
    };
    const onPageHide = () => saveProgressImmediate();
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("pagehide", onPageHide);
    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("pagehide", onPageHide);
    };
  }, [saveProgressImmediate]);

  // Measure viewport and split content into discrete pages at line boundaries (book-like).
  // Chrome is an overlay — do not remasure when showChrome toggles (that was shifting pages).
  useEffect(() => {
    if (loading || !content) return;
    const restoreFromAnchor = (offsets: number[], scrollH: number) => {
      const anchor = positionAnchorRef.current;
      if (anchor == null || scrollH <= 0 || offsets.length === 0) {
        setViewportPage((prev) => Math.min(prev, offsets.length - 1));
        return;
      }
      const targetY = anchor * scrollH;
      let best = 0;
      for (let i = 0; i < offsets.length; i++) {
        if (offsets[i] <= targetY + 0.5) best = i;
        else break;
      }
      setViewportPage(best);
      positionAnchorRef.current = null;
    };
    const applyFixedPages = (scrollH: number, effectiveH: number) => {
      setContentHeight(scrollH);
      const total = Math.max(1, Math.ceil(scrollH / effectiveH));
      const offsets = Array.from({ length: total }, (_, i) => i * effectiveH);
      setPageOffsets(offsets);
      setTotalViewportPages(total);
      restoreFromAnchor(offsets, scrollH);
    };
    const measure = () => {
      const containerEl = containerRef.current;
      const contentEl = contentRef.current;
      if (!containerEl || !contentEl) return;
      const h = containerEl.clientHeight;
      if (h <= 0) return;
      const effectiveH = Math.max(1, h);
      setPageHeight(effectiveH);

      // Get line rects via Range API for book-like pagination (no mid-line cuts)
      const readerEl = contentEl.querySelector(".reader-content") ?? contentEl.firstElementChild;
      if (!readerEl || !readerEl.childNodes.length) {
        applyFixedPages(contentEl.scrollHeight, effectiveH);
        return;
      }

      try {
        const range = document.createRange();
        range.selectNodeContents(readerEl);
        const rects = range.getClientRects();
        range.detach();

        if (rects.length === 0) {
          applyFixedPages(contentEl.scrollHeight, effectiveH);
          return;
        }

        const contentTop = contentEl.getBoundingClientRect().top;
        const lines: { top: number; bottom: number }[] = [];
        for (let i = 0; i < rects.length; i++) {
          const r = rects[i];
          if (r.width > 0 && r.height > 0) {
            lines.push({ top: r.top - contentTop, bottom: r.bottom - contentTop });
          }
        }

        if (lines.length === 0) {
          setContentHeight(contentEl.scrollHeight);
          setPageOffsets([0]);
          setTotalViewportPages(1);
          return;
        }

        const EPS = 0.5;
        const offsets: number[] = [0];
        let pageStart = 0;
        let pageBottom = effectiveH;

        for (let i = 0; i < lines.length; i++) {
          const line = lines[i];
          if (line.bottom <= pageBottom + EPS) continue;

          // Oversized line at page start: keep it, break after it
          if (line.top <= pageStart + EPS) {
            if (i + 1 < lines.length) {
              const nextStart = lines[i + 1].top;
              if (nextStart > pageStart + EPS) {
                offsets.push(nextStart);
                pageStart = nextStart;
                pageBottom = pageStart + effectiveH;
              }
            }
            continue;
          }

          // Line does not fully fit — start next page here (no overlap with prior page)
          offsets.push(line.top);
          pageStart = line.top;
          pageBottom = pageStart + effectiveH;
        }

        const scrollH = contentEl.scrollHeight;
        setContentHeight(scrollH);
        setPageOffsets(offsets);
        setTotalViewportPages(offsets.length);
        restoreFromAnchor(offsets, scrollH);
      } catch {
        applyFixedPages(contentEl.scrollHeight, effectiveH);
      }
    };
    const timer = requestAnimationFrame(() => requestAnimationFrame(measure));
    const ro = new ResizeObserver(measure);
    const el = containerRef.current;
    if (el) ro.observe(el);
    return () => {
      cancelAnimationFrame(timer);
      ro.disconnect();
    };
  }, [content, loading, settings.fontSize, settings.fontFamily, fullscreen]);

  const bumpFontSize = useCallback((delta: number) => {
    setSettings((s) => {
      const y = pageOffsets[viewportPage] ?? 0;
      const h = contentHeight || 1;
      positionAnchorRef.current = Math.min(1, Math.max(0, y / h));
      return { ...s, fontSize: clampFontSize(s.fontSize + delta) };
    });
  }, [pageOffsets, viewportPage, contentHeight, setSettings]);

  const flattenChapters = (items: ChapterItem[]): { title: string; page: number }[] => {
    const out: { title: string; page: number }[] = [];
    for (const c of items) {
      out.push({ title: c.title || `Page ${c.page}`, page: c.page });
      if (c.children?.length) {
        out.push(...flattenChapters(c.children));
      }
    }
    return out;
  };

  const flatToc = flattenChapters(chapters);
  const isPdf = bookInfo?.seriesFormat === KAVITA_PDF_FORMAT;
  const totalKavitaPages = isPdf
    ? pdfPageCount || bookInfo?.pages || 0
    : bookInfo?.pages ?? 0;
  const canPrevKavita = page > 0;
  const canNextKavita = page < totalKavitaPages - 1;

  const scrollToViewport = useCallback((idx: number) => {
    setViewportPage(idx);
  }, []);

  const goPrev = useCallback(() => {
    if (isPdf) {
      if (page > 0) setPage((p) => p - 1);
      return;
    }
    if (viewportPage > 0) {
      scrollToViewport(viewportPage - 1);
    } else if (canPrevKavita) {
      setPage((p) => p - 1);
    }
  }, [isPdf, page, viewportPage, canPrevKavita, scrollToViewport]);

  const goNext = useCallback(() => {
    if (isPdf) {
      if (page < totalKavitaPages - 1) setPage((p) => p + 1);
      return;
    }
    if (viewportPage < totalViewportPages - 1) {
      scrollToViewport(viewportPage + 1);
    } else if (canNextKavita) {
      setPage((p) => p + 1);
    }
  }, [isPdf, page, totalKavitaPages, viewportPage, totalViewportPages, canNextKavita, scrollToViewport]);

  const canPrev = isPdf ? page > 0 : viewportPage > 0 || canPrevKavita;
  const canNext = isPdf ? page < totalKavitaPages - 1 : viewportPage < totalViewportPages - 1 || canNextKavita;

  // Tap/click zones: left = prev, right = next, center = toggle chrome in fullscreen
  const handleContentClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (tocOpen || settingsOpen) return;
      const target = e.currentTarget;
      const rect = target.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const third = rect.width / 3;
      if (x < third) goPrev();
      else if (x > third * 2) goNext();
      else if (fullscreen) setChromeHidden((h) => !h);
    },
    [goPrev, goNext, tocOpen, settingsOpen, fullscreen]
  );

  // Swipe gestures
  const handleTouchStart = useCallback((e: React.TouchEvent) => {
    touchStartX.current = e.touches[0].clientX;
    touchStartY.current = e.touches[0].clientY;
  }, []);

  const handleTouchEnd = useCallback(
    (e: React.TouchEvent) => {
      const endX = e.changedTouches[0].clientX;
      const endY = e.changedTouches[0].clientY;
      const diffX = endX - touchStartX.current;
      const diffY = endY - touchStartY.current;
      // Prefer horizontal swipe; ignore mostly-vertical moves
      if (Math.abs(diffX) < SWIPE_THRESHOLD || Math.abs(diffX) < Math.abs(diffY)) return;
      if (diffX > 0) goPrev();
      else goNext();
    },
    [goPrev, goNext]
  );

  // Fullscreen / locked reading mode — hide chrome so only text fills the viewport
  const toggleFullscreen = useCallback(async () => {
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
        setFullscreen(false);
        setChromeHidden(false);
      } else {
        await document.documentElement.requestFullscreen();
        setFullscreen(true);
        setChromeHidden(true);
      }
    } catch {
      // Some WebViews block Fullscreen API — still enter locked chrome-hidden mode
      setFullscreen((prev) => {
        const next = !prev;
        setChromeHidden(next);
        return next;
      });
    }
  }, []);

  useEffect(() => {
    const onFullscreenChange = () => {
      const on = !!document.fullscreenElement;
      setFullscreen(on);
      if (!on) setChromeHidden(false);
      else setChromeHidden(true);
    };
    document.addEventListener("fullscreenchange", onFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", onFullscreenChange);
  }, []);

  // Lock orientation to portrait on mobile (respects user preference to prevent unwanted rotation)
  useEffect(() => {
    let unlocked = false;
    const orient = screen.orientation as ScreenOrientation & { lock?: (mode: string) => Promise<void>; unlock?: () => void };
    const doLock = async () => {
      try {
        if (typeof orient?.lock === "function") {
          await orient.lock("portrait");
          unlocked = true;
        }
      } catch {
        /* lock requires fullscreen on some browsers */
      }
    };
    doLock();
    return () => {
      if (unlocked && typeof orient?.unlock === "function") {
        try {
          orient.unlock();
        } catch {
          /* ignore */
        }
      }
    };
  }, []);

  if (error) {
    return (
      <div className="min-h-screen bg-gray-950 flex items-center justify-center">
        <div className="text-center">
          <p className="text-red-400 mb-4">{error}</p>
          <button
            onClick={() => navigate("/my-library")}
            className="px-4 py-2 bg-gray-700 text-gray-200 rounded-lg hover:bg-gray-600"
          >
            Back to Library
          </button>
        </div>
      </div>
    );
  }

  const containerClass = fullscreen
    ? "fixed inset-0 z-[9999] bg-gray-950 flex flex-col"
    : "h-screen bg-gray-950 flex flex-col overflow-hidden";

  const pageStart = pageOffsets[viewportPage] ?? viewportPage * pageHeight;
  const pageEnd =
    viewportPage + 1 < pageOffsets.length
      ? pageOffsets[viewportPage + 1]
      : contentHeight > pageStart
        ? contentHeight
        : pageStart + pageHeight;
  const pageClipH = Math.max(1, Math.min(pageHeight, pageEnd - pageStart));

  const backTarget = shareToken ? `/share/${encodeURIComponent(shareToken)}` : "/my-library";

  return (
    <div className={`${containerClass} relative`}>
      {/* Header overlays content (does not reflow pagination). */}
      {showChrome && (
        <header className="absolute top-0 left-0 right-0 z-20 flex items-center justify-between px-4 py-2 pt-[calc(0.5rem+env(safe-area-inset-top,0px))] pb-2 bg-gray-900/95 border-b border-gray-800">
          <button
            onClick={() => navigate(backTarget)}
            className="p-2 text-gray-400 hover:text-white rounded-lg hover:bg-gray-800 transition-colors"
            title="Back"
          >
            <ChevronLeft size={20} />
          </button>
          <h1 className="text-sm font-medium text-gray-200 truncate max-w-[40%]">
            {bookInfo?.bookTitle || bookInfo?.seriesName || "Loading..."}
          </h1>
          <div className="flex items-center gap-1">
            <button
              onClick={toggleFullscreen}
              className="p-2 text-gray-400 hover:text-white rounded-lg hover:bg-gray-800 transition-colors"
              title={fullscreen ? "Exit fullscreen" : "Fullscreen"}
            >
              {fullscreen ? <Minimize2 size={20} /> : <Maximize2 size={20} />}
            </button>
            <button
              onClick={() => setSettingsOpen((o) => !o)}
              className={`p-2 rounded-lg transition-colors ${
                settingsOpen ? "text-amber-400 bg-gray-800" : "text-gray-400 hover:text-white hover:bg-gray-800"
              }`}
              title="Reader settings"
            >
              <Settings size={20} />
            </button>
            <button
              onClick={() => setTocOpen((o) => !o)}
              className={`p-2 rounded-lg transition-colors ${
                tocOpen ? "text-amber-400 bg-gray-800" : "text-gray-400 hover:text-white hover:bg-gray-800"
              }`}
              title="Table of Contents"
            >
              {tocOpen ? <X size={20} /> : <Menu size={20} />}
            </button>
          </div>
        </header>
      )}

      <div className="flex flex-1 overflow-hidden min-h-0 relative">
        {/* Settings panel (overlay) */}
        {showChrome && settingsOpen && (
          <aside className="absolute top-0 bottom-0 left-0 z-20 w-64 border-r border-gray-800 bg-gray-900/95 overflow-y-auto pt-14">
            <div className="p-4 space-y-4">
              <h2 className="text-xs font-semibold text-gray-500 uppercase">Reader settings</h2>
              <div>
                <label className="block text-xs text-gray-400 mb-2">Font size</label>
                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    onClick={() => bumpFontSize(-FONT_SIZE_STEP)}
                    disabled={settings.fontSize <= FONT_SIZE_MIN}
                    className="p-2 rounded-lg bg-gray-800 text-gray-200 hover:bg-gray-700 disabled:opacity-40"
                    title="Smaller"
                  >
                    <Minus size={16} />
                  </button>
                  <span className="min-w-[3rem] text-center text-sm font-medium text-gray-100 tabular-nums">
                    {settings.fontSize}
                  </span>
                  <button
                    type="button"
                    onClick={() => bumpFontSize(FONT_SIZE_STEP)}
                    disabled={settings.fontSize >= FONT_SIZE_MAX}
                    className="p-2 rounded-lg bg-gray-800 text-gray-200 hover:bg-gray-700 disabled:opacity-40"
                    title="Larger"
                  >
                    <Plus size={16} />
                  </button>
                </div>
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-2">Font</label>
                <div className="flex flex-col gap-1">
                  {(["serif", "sans-serif", "monospace"] as const).map((fam) => (
                    <button
                      key={fam}
                      onClick={() => {
                        const y = pageOffsets[viewportPage] ?? 0;
                        const h = contentHeight || 1;
                        positionAnchorRef.current = Math.min(1, Math.max(0, y / h));
                        setSettings((s) => ({ ...s, fontFamily: fam }));
                      }}
                      className={`px-3 py-2 rounded text-sm text-left ${
                        settings.fontFamily === fam ? "bg-amber-600 text-white" : "bg-gray-800 text-gray-300"
                      }`}
                    >
                      {fam === "serif" ? "Serif" : fam === "sans-serif" ? "Sans-serif" : "Monospace"}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </aside>
        )}

        {/* TOC sidebar (overlay) */}
        {showChrome && tocOpen && !settingsOpen && (
          <aside className="absolute top-0 bottom-0 left-0 z-20 w-64 border-r border-gray-800 bg-gray-900/95 overflow-y-auto pt-14">
            <div className="p-3">
              <h2 className="text-xs font-semibold text-gray-500 uppercase mb-2">Contents</h2>
              <ul className="space-y-1">
                {flatToc.map((c, i) => (
                  <li key={i}>
                    <button
                      onClick={() => {
                        setPage(c.page);
                        setTocOpen(false);
                      }}
                      className={`w-full text-left px-2 py-1.5 rounded text-sm truncate transition-colors ${
                        c.page === page ? "bg-amber-600/30 text-amber-400" : "text-gray-300 hover:bg-gray-800"
                      }`}
                    >
                      {c.title}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          </aside>
        )}

        {/* Content fills the viewport; chrome overlays top/bottom lines when visible. */}
        <main
          onClick={handleContentClick}
          onTouchStart={handleTouchStart}
          onTouchEnd={handleTouchEnd}
          className="flex-1 overflow-hidden flex flex-col items-center min-h-0 p-0 px-4 md:px-8"
        >
          {isPdf ? (
            <PdfViewer chapterId={cid} page={page} onReady={handlePdfReady} />
          ) : (
          <div
            ref={containerRef}
            className="relative w-full max-w-2xl flex-1 min-h-0 py-0"
          >
            {loading ? (
              <div className="flex justify-center py-12 h-full items-center">
                <div className="animate-pulse text-gray-500">Loading...</div>
              </div>
            ) : (
              <div
                className="overflow-hidden w-full"
                style={{ height: pageClipH }}
              >
                <div
                  ref={contentRef}
                  className="transition-transform duration-150 ease-out"
                  style={{
                    fontFamily: FONT_FAMILIES[settings.fontFamily],
                    fontSize: `${settings.fontSize}px`,
                    transform: `translateY(-${pageStart}px)`,
                  }}
                >
                  <div
                    className="reader-content max-w-2xl w-full text-gray-100 leading-relaxed prose prose-invert prose-p:my-0 max-w-none select-none [&_img]:max-w-full [&_img]:h-auto bg-transparent rounded-none px-1 py-2"
                    dangerouslySetInnerHTML={{ __html: content }}
                    style={{ paddingBottom: "0.5rem" }}
                  />
                </div>
              </div>
            )}
          </div>
          )}
        </main>
      </div>

      {/* Footer overlays content */}
      {showChrome && (
        <footer className="absolute bottom-0 left-0 right-0 z-20 flex items-center justify-between px-4 py-2 pb-[calc(0.5rem+env(safe-area-inset-bottom,0px))] bg-gray-900/95 border-t border-gray-800">
          <button
            onClick={goPrev}
            disabled={!canPrev}
            className="flex items-center gap-1 px-3 py-2 rounded-lg bg-gray-800 text-gray-300 hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            <ChevronLeft size={18} /> Previous
          </button>
          <span className="text-sm text-gray-500">
            {isPdf ? (
              <>Page {page + 1}{totalKavitaPages > 0 ? ` of ${totalKavitaPages}` : ""}</>
            ) : (
              <>
                Page {viewportPage + 1} of {totalViewportPages}
                {totalKavitaPages > 1 && (
                  <span className="text-gray-600 ml-1">· Ch. {page + 1}/{totalKavitaPages}</span>
                )}
              </>
            )}
          </span>
          <button
            onClick={goNext}
            disabled={!canNext}
            className="flex items-center gap-1 px-3 py-2 rounded-lg bg-gray-800 text-gray-300 hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            Next <ChevronRight size={18} />
          </button>
        </footer>
      )}
    </div>
  );
}
