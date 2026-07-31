import { useState, useEffect, useCallback, useRef } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import axios from "axios";
import api from "../api/client";
import { getApiBaseUrl, toAbsoluteUrl } from "../api/instanceUrl";
import PdfViewer from "../components/PdfViewer";
import EpubViewer, {
  epubViewGo,
  epubViewGoTo,
  epubViewIgnoreTaps,
  type EpubLocation,
  type EpubTocItem,
} from "../components/EpubViewer";
import { cacheBookEbook, getCachedEbookObjectUrl } from "../utils/ebookCache";
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
  fontSize: number;
  fontFamily: "serif" | "sans-serif" | "monospace";
  /** Desktop spread; mobile always forces 1. */
  columnCount: 1 | 2;
}

const defaultSettings: ReaderSettings = {
  fontSize: 16,
  fontFamily: "serif",
  columnCount: 2,
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
      const cols = parsed.columnCount === 1 || parsed.columnCount === 2 ? parsed.columnCount : defaultSettings.columnCount;
      return {
        fontFamily: parsed.fontFamily || defaultSettings.fontFamily,
        fontSize: migrateFontSize(parsed.fontSize),
        columnCount: cols,
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

const FONT_FAMILIES: Record<ReaderSettings["fontFamily"], { label: string; stack: string }> = {
  serif: { label: "Serif", stack: "Georgia, 'Times New Roman', serif" },
  "sans-serif": { label: "Sans-serif", stack: "system-ui, -apple-system, sans-serif" },
  monospace: { label: "Monospace", stack: "'JetBrains Mono', 'Fira Code', monospace" },
};

/** Mobile / narrow viewports always get a single centered column. */
function useForcedSingleColumn(): boolean {
  const [narrow, setNarrow] = useState(() =>
    typeof window !== "undefined" ? window.matchMedia("(max-width: 900px)").matches : true
  );
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 900px)");
    const apply = () => setNarrow(mq.matches);
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, []);
  return narrow;
}

/** Max safe-area inset (px). Foliate margin is symmetric, so we pad for the largest edge. */
function useSafeAreaMaxInset(): number {
  const [inset, setInset] = useState(0);
  useEffect(() => {
    const el = document.createElement("div");
    el.setAttribute("aria-hidden", "true");
    el.style.cssText =
      "position:fixed;left:0;top:0;width:0;height:0;overflow:hidden;visibility:hidden;pointer-events:none;" +
      "padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px);" +
      "padding-left:env(safe-area-inset-left,0px);padding-right:env(safe-area-inset-right,0px);";
    document.body.appendChild(el);
    const measure = () => {
      const cs = getComputedStyle(el);
      const t = parseFloat(cs.paddingTop) || 0;
      const b = parseFloat(cs.paddingBottom) || 0;
      const l = parseFloat(cs.paddingLeft) || 0;
      const r = parseFloat(cs.paddingRight) || 0;
      setInset(Math.max(t, b, l, r));
    };
    measure();
    window.visualViewport?.addEventListener("resize", measure);
    window.addEventListener("resize", measure);
    return () => {
      window.visualViewport?.removeEventListener("resize", measure);
      window.removeEventListener("resize", measure);
      el.remove();
    };
  }, []);
  return inset;
}

interface BookInfo {
  bookTitle: string;
  seriesName: string;
  pages: number;
  chapterTitle?: string;
  seriesFormat?: number;
}

export default function Ereader() {
  const { chapterId } = useParams<{ chapterId: string }>();
  const [searchParams] = useSearchParams();
  const shareToken = (searchParams.get("share") || "").trim() || null;
  const navigate = useNavigate();
  const cid = chapterId ? parseInt(chapterId, 10) : NaN;

  const [bookInfo, setBookInfo] = useState<BookInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tocOpen, setTocOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settings, setSettingsState] = useState<ReaderSettings>(loadSettings);
  const [fullscreen, setFullscreen] = useState(false);
  const [chromeHidden, setChromeHidden] = useState(false);
  const [epubSource, setEpubSource] = useState<File | Blob | string | null>(null);
  const [epubToc, setEpubToc] = useState<EpubTocItem[]>([]);
  const [locationLabel, setLocationLabel] = useState("");
  const [pdfPage, setPdfPage] = useState(0);
  const [pdfPageCount, setPdfPageCount] = useState(0);

  const epubHostRef = useRef<HTMLDivElement>(null);
  const lastLocRef = useRef<EpubLocation | null>(null);
  const objectUrlRef = useRef<string | null>(null);
  const saveTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const initialCfiRef = useRef<string | null>(null);

  const showChrome = !fullscreen || !chromeHidden;
  const isPdf = bookInfo?.seriesFormat === KAVITA_PDF_FORMAT;
  const forceSingleColumn = useForcedSingleColumn();
  const effectiveColumns: 1 | 2 = forceSingleColumn ? 1 : settings.columnCount;
  const safeInset = useSafeAreaMaxInset();
  // Foliate's margin is one symmetric top/bottom value. Include notch/home insets
  // so immersive (chrome-hidden) text is not clipped under the status bar.
  const chromeMarginPx = showChrome
    ? Math.max(72, 52 + Math.ceil(safeInset))
    : Math.max(36, 20 + Math.ceil(safeInset));

  const readerGet = useCallback(
    async <T,>(path: string, config?: { params?: Record<string, unknown>; responseType?: "text" | "blob" | "json" }) => {
      if (shareToken) {
        const { data } = await axios.get<T>(
          `${getApiBaseUrl()}/share/${encodeURIComponent(shareToken)}/ebook/${path}`,
          config
        );
        return data;
      }
      const { data } = await api.get<T>(`/library/reader/${cid}/${path}`, config);
      return data;
    },
    [cid, shareToken]
  );

  const setSettings = useCallback((s: ReaderSettings | ((prev: ReaderSettings) => ReaderSettings)) => {
    setSettingsState((prev) => {
      const next = typeof s === "function" ? s(prev) : s;
      saveSettings(next);
      return next;
    });
  }, []);

  const revokeObjectUrl = useCallback(() => {
    if (objectUrlRef.current) {
      try {
        URL.revokeObjectURL(objectUrlRef.current);
      } catch {
        /* ignore */
      }
      objectUrlRef.current = null;
    }
  }, []);

  const persistEpubProgress = useCallback(() => {
    if (!cid || !bookInfo || shareToken) return;
    const loc = lastLocRef.current;
    if (!loc?.cfi) return;
    saveProgress({
      chapterId: cid,
      page: Math.max(0, Math.round((loc.fraction || 0) * 10000)),
      viewportPage: Math.max(0, loc.sectionIndex || 0),
      totalViewportPages: loc.locationTotal,
      totalKavitaPages: bookInfo.pages,
      bookTitle: bookInfo.bookTitle,
      seriesName: bookInfo.seriesName,
      coverUrl: toAbsoluteUrl(`/api/library/reader/cover/chapter/${cid}`),
      cfi: loc.cfi,
    });
  }, [cid, bookInfo, shareToken]);

  const persistPdfProgress = useCallback(() => {
    if (!cid || !bookInfo || shareToken) return;
    saveProgress({
      chapterId: cid,
      page: pdfPage,
      viewportPage: 0,
      totalViewportPages: 1,
      totalKavitaPages: pdfPageCount || bookInfo.pages,
      bookTitle: bookInfo.bookTitle,
      seriesName: bookInfo.seriesName,
      coverUrl: toAbsoluteUrl(`/api/library/reader/cover/chapter/${cid}`),
    });
  }, [cid, bookInfo, shareToken, pdfPage, pdfPageCount]);

  useEffect(() => {
    if (!cid || isNaN(cid)) return;
    let cancelled = false;
    (async () => {
      try {
        const data = await readerGet<BookInfo>("book-info");
        if (cancelled) return;
        setBookInfo(data);
        setError(null);
        const prog = getProgress(cid);
        if (data.seriesFormat === KAVITA_PDF_FORMAT) {
          setPdfPage(prog?.page ?? 0);
        } else {
          initialCfiRef.current = prog?.cfi || null;
        }
      } catch (e: unknown) {
        if (cancelled) return;
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
          const prog = getProgress(cid);
          if (manifest.isPdf) setPdfPage(prog?.page ?? 0);
          else initialCfiRef.current = prog?.cfi || null;
          setError(null);
          return;
        }
        setError(
          ready
            ? "Failed to load book"
            : "Failed to load book — save this ebook while online to read offline"
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [cid, readerGet, shareToken]);

  // Load EPUB bytes (cache → network).
  useEffect(() => {
    if (!bookInfo || isPdf || !cid || isNaN(cid)) return;
    let cancelled = false;
    setLoading(true);
    setEpubSource(null);
    revokeObjectUrl();

    (async () => {
      try {
        if (!shareToken) {
          const cached = await getCachedEbookObjectUrl(cid, false);
          if (cancelled) {
            if (cached) URL.revokeObjectURL(cached);
            return;
          }
          if (cached) {
            objectUrlRef.current = cached;
            setEpubSource(cached);
            setLoading(false);
            return;
          }
        }

        const blob = await readerGet<Blob>("file", { responseType: "blob" });
        if (cancelled) return;
        if (!(blob instanceof Blob) || blob.size < 100) {
          throw new Error("Empty ebook file");
        }
        setEpubSource(blob);
        setLoading(false);
      } catch (e: unknown) {
        if (cancelled) return;
        setError(
          isLikelyOffline() || isNetworkError(e)
            ? "Ebook not available offline — re-save while online"
            : "Failed to load ebook file"
        );
        setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [bookInfo, isPdf, cid, shareToken, readerGet, revokeObjectUrl]);

  useEffect(() => {
    if (!bookInfo || !cid || isNaN(cid) || shareToken) return;
    if (isLikelyOffline()) return;
    saveEbookOfflineManifest({
      chapterId: cid,
      title: bookInfo.bookTitle || bookInfo.seriesName || "Ebook",
      author: "",
      coverUrl: toAbsoluteUrl(`/api/library/reader/cover/chapter/${cid}`),
      isPdf: !!isPdf,
      pages: bookInfo.pages,
    });
    void cacheBookEbook(cid, !!isPdf);
  }, [bookInfo, cid, shareToken, isPdf]);

  useEffect(() => {
    if (!cid || !bookInfo || loading || shareToken) return;
    const save = isPdf ? persistPdfProgress : persistEpubProgress;
    saveTimerRef.current = setTimeout(save, 500);
    return () => clearTimeout(saveTimerRef.current);
  }, [
    cid,
    bookInfo,
    loading,
    shareToken,
    isPdf,
    pdfPage,
    pdfPageCount,
    locationLabel,
    persistPdfProgress,
    persistEpubProgress,
  ]);

  useEffect(() => {
    const flush = () => {
      if (isPdf) persistPdfProgress();
      else persistEpubProgress();
    };
    const onVis = () => {
      if (document.visibilityState === "hidden") flush();
    };
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("pagehide", flush);
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      window.removeEventListener("pagehide", flush);
    };
  }, [isPdf, persistPdfProgress, persistEpubProgress]);

  useEffect(() => () => revokeObjectUrl(), [revokeObjectUrl]);

  const readerRootRef = useRef<HTMLDivElement>(null);

  const nudgeLayout = useCallback(() => {
    // Foliate/paginator sizes from the container; fullscreen changes need a remasure.
    requestAnimationFrame(() => {
      window.dispatchEvent(new Event("resize"));
    });
  }, []);

  const toggleFullscreen = useCallback(async () => {
    const root = readerRootRef.current;
    try {
      if (document.fullscreenElement) {
        await document.exitFullscreen();
        setFullscreen(false);
        setChromeHidden(false);
      } else {
        // Fullscreen the reader shell (not <html>) so layout stays viewport-bound.
        if (root && typeof root.requestFullscreen === "function") {
          await root.requestFullscreen();
        }
        setFullscreen(true);
        setChromeHidden(true);
      }
    } catch {
      // Capacitor / iOS: immersive chrome-hide without Fullscreen API.
      setFullscreen((f) => {
        const next = !f;
        setChromeHidden(next);
        return next;
      });
    }
    nudgeLayout();
  }, [nudgeLayout]);

  useEffect(() => {
    const onFs = () => {
      const active = Boolean(document.fullscreenElement);
      setFullscreen(active);
      if (!active) setChromeHidden(false);
      else setChromeHidden(true);
      nudgeLayout();
    };
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, [nudgeLayout]);

  const goPrev = useCallback(() => {
    if (isPdf) {
      setPdfPage((p) => Math.max(0, p - 1));
      return;
    }
    epubViewGo(epubHostRef.current, "prev");
  }, [isPdf]);

  const goNext = useCallback(() => {
    if (isPdf) {
      const max = Math.max(0, (pdfPageCount || bookInfo?.pages || 1) - 1);
      setPdfPage((p) => Math.min(max, p + 1));
      return;
    }
    epubViewGo(epubHostRef.current, "next");
  }, [isPdf, pdfPageCount, bookInfo?.pages]);

  const handleRelocate = useCallback((loc: EpubLocation) => {
    lastLocRef.current = loc;
    const pct = Math.round((loc.fraction || 0) * 100);
    const label = loc.tocLabel || (loc.locationTotal ? `${loc.locationCurrent ?? "?"} / ${loc.locationTotal}` : "");
    setLocationLabel(label ? `${label} · ${pct}%` : `${pct}%`);
  }, []);

  const jumpToc = useCallback(async (
    href: string,
    label: string,
    e?: React.SyntheticEvent,
  ) => {
    e?.preventDefault();
    e?.stopPropagation();
    // Block right-third ghost taps BEFORE the TOC overlay unmounts.
    // Keep the window short enough that mobile chapter jumps still feel snappy.
    epubViewIgnoreTaps(epubHostRef.current, 450);
    setTocOpen(false);
    setSettingsOpen(false);
    await epubViewGoTo(epubHostRef.current, href, label);
  }, []);

  const renderToc = (items: EpubTocItem[], depth = 0) =>
    items.map((item, i) => (
      <div key={`${item.href}-${i}`}>
        <button
          type="button"
          onClick={(ev) => void jumpToc(item.href, item.label, ev)}
          // Desktop only: prevent mousedown→focus from synthesizing a click that
          // lands on the book after the overlay closes. Touch must keep defaults.
          onPointerDown={(ev) => {
            if (ev.pointerType === "mouse") ev.preventDefault();
          }}
          className="w-full text-left px-3 py-2 text-sm text-gray-200 hover:bg-gray-800 rounded-lg touch-manipulation"
          style={{ paddingLeft: 12 + depth * 12 }}
        >
          {item.label}
        </button>
        {item.children?.length ? renderToc(item.children, depth + 1) : null}
      </div>
    ));

  if (error) {
    return (
      <div className="min-h-screen bg-gray-950 flex items-center justify-center" style={{ backgroundColor: "rgb(var(--gray-950))" }}>
        <div className="text-center">
          <p className="text-red-400 mb-4">{error}</p>
          <button
            onClick={() => navigate(shareToken ? `/share/${encodeURIComponent(shareToken)}` : "/my-library")}
            className="px-4 py-2 bg-gray-700 text-gray-200 rounded-lg hover:bg-gray-600"
          >
            Back
          </button>
        </div>
      </div>
    );
  }

  // Always pin to the visual viewport (100dvh). Immersive mode only hides chrome /
  // uses Fullscreen API on this root — never documentElement (that shoved content off-screen).
  const containerClass =
    "fixed inset-0 z-[9999] flex flex-col overflow-hidden bg-gray-950 " +
    "h-[100dvh] max-h-[100dvh] w-full max-w-[100vw]";
  const backTarget = shareToken ? `/share/${encodeURIComponent(shareToken)}` : "/my-library";
  const stopChrome = (e: React.SyntheticEvent) => e.stopPropagation();

  return (
    <div ref={readerRootRef} className={`${containerClass} relative`} style={{ backgroundColor: "rgb(var(--gray-950))" }}>
      {showChrome && (
        <header
          data-reader-chrome
          onClick={stopChrome}
          onTouchStart={stopChrome}
          onTouchEnd={stopChrome}
          className="absolute top-0 left-0 right-0 z-20 flex items-center justify-between px-4 py-2 pt-[calc(0.5rem+env(safe-area-inset-top,0px))] pb-2 bg-gray-900/95 border-b border-gray-800"
        >
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
              onClick={() => void toggleFullscreen()}
              className="p-2 text-gray-400 hover:text-white rounded-lg hover:bg-gray-800 transition-colors"
              title={fullscreen ? "Exit fullscreen" : "Fullscreen"}
            >
              {fullscreen ? <Minimize2 size={20} /> : <Maximize2 size={20} />}
            </button>
            {!isPdf && (
              <button
                onClick={() => setSettingsOpen((o) => !o)}
                className={`p-2 rounded-lg transition-colors ${
                  settingsOpen ? "text-amber-400 bg-gray-800" : "text-gray-400 hover:text-white hover:bg-gray-800"
                }`}
                title="Reader settings"
              >
                <Settings size={20} />
              </button>
            )}
            {!isPdf && (
              <button
                onClick={() => setTocOpen((o) => !o)}
                className={`p-2 rounded-lg transition-colors ${
                  tocOpen ? "text-amber-400 bg-gray-800" : "text-gray-400 hover:text-white hover:bg-gray-800"
                }`}
                title="Table of Contents"
              >
                {tocOpen ? <X size={20} /> : <Menu size={20} />}
              </button>
            )}
          </div>
        </header>
      )}

      <div className="flex flex-1 overflow-hidden min-h-0 relative">
        {showChrome && settingsOpen && !isPdf && (
          <aside
            data-reader-chrome
            onClick={stopChrome}
            className="absolute top-0 bottom-0 left-0 z-20 w-64 border-r border-gray-800 bg-gray-900/95 overflow-y-auto pt-14"
          >
            <div className="p-4 space-y-4">
              <h2 className="text-xs font-semibold text-gray-500 uppercase">Reader settings</h2>
              <div>
                <div className="text-xs text-gray-400 mb-2">Font size</div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    className="p-2 rounded-lg bg-gray-800 text-gray-200"
                    onClick={() => setSettings((s) => ({ ...s, fontSize: clampFontSize(s.fontSize - FONT_SIZE_STEP) }))}
                  >
                    <Minus size={16} />
                  </button>
                  <span className="text-sm text-gray-200 w-10 text-center">{settings.fontSize}</span>
                  <button
                    type="button"
                    className="p-2 rounded-lg bg-gray-800 text-gray-200"
                    onClick={() => setSettings((s) => ({ ...s, fontSize: clampFontSize(s.fontSize + FONT_SIZE_STEP) }))}
                  >
                    <Plus size={16} />
                  </button>
                </div>
              </div>
              <div>
                <div className="text-xs text-gray-400 mb-2">Font</div>
                <div className="space-y-1">
                  {(Object.keys(FONT_FAMILIES) as Array<keyof typeof FONT_FAMILIES>).map((key) => (
                    <button
                      key={key}
                      type="button"
                      onClick={() => setSettings((s) => ({ ...s, fontFamily: key }))}
                      className={`w-full text-left px-3 py-2 rounded-lg text-sm ${
                        settings.fontFamily === key ? "bg-amber-500/20 text-amber-300" : "text-gray-300 hover:bg-gray-800"
                      }`}
                      style={{ fontFamily: FONT_FAMILIES[key].stack }}
                    >
                      {FONT_FAMILIES[key].label}
                    </button>
                  ))}
                </div>
              </div>
              {!forceSingleColumn && (
                <div>
                  <div className="text-xs text-gray-400 mb-2">Columns</div>
                  <div className="flex gap-2">
                    {([1, 2] as const).map((n) => (
                      <button
                        key={n}
                        type="button"
                        onClick={() => setSettings((s) => ({ ...s, columnCount: n }))}
                        className={`flex-1 px-3 py-2 rounded-lg text-sm ${
                          settings.columnCount === n
                            ? "bg-amber-500/20 text-amber-300"
                            : "text-gray-300 hover:bg-gray-800 bg-gray-800/50"
                        }`}
                      >
                        {n === 1 ? "1 column" : "2 columns"}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </aside>
        )}

        {showChrome && tocOpen && !isPdf && (
          <aside
            data-reader-chrome
            onClick={stopChrome}
            className="absolute top-0 bottom-0 right-0 z-20 w-72 border-l border-gray-800 bg-gray-900/95 overflow-y-auto pt-14"
          >
            <div className="p-3">
              <h2 className="text-xs font-semibold text-gray-500 uppercase px-2 mb-2">Contents</h2>
              {epubToc.length ? renderToc(epubToc) : (
                <p className="text-sm text-gray-500 px-2">No table of contents</p>
              )}
            </div>
          </aside>
        )}

        <div className="flex-1 min-w-0 min-h-0 relative">
          {isPdf && bookInfo ? (
            <PdfViewer
              chapterId={cid}
              page={pdfPage}
              onReady={(n) => {
                setPdfPageCount(n);
                setLoading(false);
              }}
            />
          ) : epubSource ? (
            <div
              ref={epubHostRef}
              className={`absolute inset-0 ${tocOpen || settingsOpen ? "pointer-events-none" : ""}`}
            >
              <EpubViewer
                source={epubSource}
                initialCfi={initialCfiRef.current}
                fontSize={settings.fontSize}
                fontFamily={FONT_FAMILIES[settings.fontFamily].stack}
                columnCount={effectiveColumns}
                chromeMarginPx={chromeMarginPx}
                onReady={() => setLoading(false)}
                onError={(msg) => setError(msg)}
                onRelocate={handleRelocate}
                onToc={setEpubToc}
                onCenterTap={() => {
                  if (fullscreen) setChromeHidden((h) => !h);
                }}
              />
            </div>
          ) : (
            <div className="absolute inset-0 flex items-center justify-center text-gray-500 text-sm">
              {loading ? "Loading…" : "Preparing reader…"}
            </div>
          )}
        </div>
      </div>

      {showChrome && (
        <footer
          data-reader-chrome
          onClick={stopChrome}
          className="absolute bottom-0 left-0 right-0 z-20 flex items-center justify-between px-4 py-2 pb-[calc(0.5rem+env(safe-area-inset-bottom,0px))] bg-gray-900/95 border-t border-gray-800"
        >
          <button
            type="button"
            onClick={goPrev}
            className="p-2 text-gray-400 hover:text-white rounded-lg hover:bg-gray-800"
            title="Previous"
          >
            <ChevronLeft size={20} />
          </button>
          <div className="text-xs text-gray-400 truncate max-w-[60%] text-center">
            {isPdf
              ? `Page ${pdfPage + 1} of ${pdfPageCount || bookInfo?.pages || "?"}`
              : locationLabel || "—"}
          </div>
          <button
            type="button"
            onClick={goNext}
            className="p-2 text-gray-400 hover:text-white rounded-lg hover:bg-gray-800"
            title="Next"
          >
            <ChevronRight size={20} />
          </button>
        </footer>
      )}
    </div>
  );
}
