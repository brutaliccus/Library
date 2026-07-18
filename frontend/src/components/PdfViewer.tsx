import { useCallback, useEffect, useRef, useState } from "react";
import {
  getDocument,
  GlobalWorkerOptions,
  TextLayer,
  type PDFDocumentProxy,
  type RenderTask,
} from "pdfjs-dist";
import pdfjsWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { Capacitor } from "@capacitor/core";
import { readerFileUrlForChapter } from "../utils/ebookCache";
import "./PdfViewer.css";

GlobalWorkerOptions.workerSrc = pdfjsWorker;

/** Stay under common browser canvas limits (esp. with soft-mask temp buffers). */
const MAX_CANVAS_PIXELS = 16_777_216;
const MAX_CANVAS_DIM = 8192;

/** Bundled by scripts/copy-pdfjs-assets.mjs → public/pdfjs/… */
const PDFJS_ASSETS = "/pdfjs";

interface PdfViewerProps {
  chapterId: number;
  page: number;
  onReady?: (totalPages: number) => void;
}

function preferNativeViewer(): boolean {
  // Desktop Chromium/Firefox PDF plugins composite JBIG2 soft masks correctly.
  // Capacitor WebViews often cannot embed PDFs natively — keep canvas there.
  if (Capacitor.isNativePlatform()) return false;
  try {
    const ua = navigator.userAgent || "";
    // iOS Safari iframe PDF is unreliable; use canvas (+ wasm) there.
    if (/iPhone|iPad|iPod/.test(ua)) return false;
  } catch {
    // ignore
  }
  return true;
}

/** Browser built-in PDF viewer — correct soft masks / fonts for complex scans. */
function NativePdfFrame({
  chapterId,
  page,
  pageCount,
  onReady,
}: {
  chapterId: number;
  page: number;
  pageCount: number;
  onReady?: (totalPages: number) => void;
}) {
  const url = `${readerFileUrlForChapter(chapterId, true)}#page=${page + 1}`;

  useEffect(() => {
    if (pageCount > 0) onReady?.(pageCount);
  }, [pageCount, onReady]);

  return (
    <iframe
      key={`${chapterId}-${page}`}
      title="PDF"
      src={url}
      className="w-full flex-1 min-h-0 border-0 rounded-sm bg-white shadow-lg"
    />
  );
}

function CanvasPdfViewer({ chapterId, page, onReady }: PdfViewerProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textLayerRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const docRef = useRef<PDFDocumentProxy | null>(null);
  const renderTaskRef = useRef<RenderTask | null>(null);
  const renderTokenRef = useRef(0);
  const [loading, setLoading] = useState(true);
  const [rendering, setRendering] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let loadingTask: ReturnType<typeof getDocument> | null = null;
    void docRef.current?.cleanup();
    docRef.current = null;

    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const url = readerFileUrlForChapter(chapterId, true);
        // Dinotopia-style pages: base JPEG + overlay with JBIG2 soft mask.
        // Without wasmUrl, JBIG2Decode fails and only the base image paints.
        loadingTask = getDocument({
          url,
          withCredentials: true,
          disableRange: false,
          disableStream: false,
          isOffscreenCanvasSupported: false,
          wasmUrl: `${PDFJS_ASSETS}/wasm/`,
          cMapUrl: `${PDFJS_ASSETS}/cmaps/`,
          cMapPacked: true,
          standardFontDataUrl: `${PDFJS_ASSETS}/standard_fonts/`,
          iccUrl: `${PDFJS_ASSETS}/iccs/`,
        });
        const doc = await loadingTask.promise;
        if (cancelled) {
          void doc.cleanup();
          return;
        }
        docRef.current = doc;
        onReady?.(doc.numPages);
      } catch (e) {
        if (!cancelled) {
          const msg = e instanceof Error ? e.message : String(e);
          setError(msg ? `Failed to load PDF: ${msg}` : "Failed to load PDF");
          console.error("PdfViewer load error", e);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void load();
    return () => {
      cancelled = true;
      try {
        loadingTask?.destroy();
      } catch {
        // ignore
      }
      try {
        renderTaskRef.current?.cancel();
      } catch {
        // ignore
      }
      renderTaskRef.current = null;
      void docRef.current?.cleanup();
      docRef.current = null;
    };
  }, [chapterId, onReady]);

  const renderPage = useCallback(async (pageIndex: number) => {
    const doc = docRef.current;
    const canvas = canvasRef.current;
    const textLayer = textLayerRef.current;
    const container = containerRef.current;
    if (!doc || !canvas || !textLayer || !container) return;
    if (container.clientWidth < 32 || container.clientHeight < 32) return;

    const pageNum = pageIndex + 1;
    if (pageNum < 1 || pageNum > doc.numPages) return;

    const token = ++renderTokenRef.current;
    setRendering(true);

    try {
      renderTaskRef.current?.cancel();
    } catch {
      // ignore
    }
    renderTaskRef.current = null;

    try {
      const pdfPage = await doc.getPage(pageNum);
      if (token !== renderTokenRef.current) return;

      const baseViewport = pdfPage.getViewport({ scale: 1 });
      let fitScale = Math.min(
        container.clientWidth / baseViewport.width,
        container.clientHeight / baseViewport.height
      );
      if (!Number.isFinite(fitScale) || fitScale <= 0) return;

      let outputScale = Math.min(window.devicePixelRatio || 1, 2);
      let scale = fitScale;
      for (let i = 0; i < 12; i++) {
        const w = Math.floor(baseViewport.width * scale * outputScale);
        const h = Math.floor(baseViewport.height * scale * outputScale);
        if (w * h <= MAX_CANVAS_PIXELS && w <= MAX_CANVAS_DIM && h <= MAX_CANVAS_DIM) break;
        if (outputScale > 1) outputScale = Math.max(1, outputScale * 0.75);
        else scale *= 0.85;
      }

      const viewport = pdfPage.getViewport({ scale });
      const cssWidth = viewport.width;
      const cssHeight = viewport.height;

      canvas.width = Math.floor(cssWidth * outputScale);
      canvas.height = Math.floor(cssHeight * outputScale);
      canvas.style.width = `${cssWidth}px`;
      canvas.style.height = `${cssHeight}px`;

      const transform =
        outputScale !== 1 ? ([outputScale, 0, 0, outputScale, 0, 0] as const) : undefined;

      const renderTask = pdfPage.render({
        canvas,
        viewport,
        transform: transform ? [...transform] : undefined,
        background: "rgb(255,255,255)",
        intent: "display",
      });
      renderTaskRef.current = renderTask;
      await renderTask.promise;
      if (token !== renderTokenRef.current) return;

      textLayer.innerHTML = "";
      textLayer.style.width = `${cssWidth}px`;
      textLayer.style.height = `${cssHeight}px`;

      try {
        const textContent = await pdfPage.getTextContent();
        if (token !== renderTokenRef.current) return;
        if ((textContent.items?.length ?? 0) > 0) {
          const layer = new TextLayer({
            textContentSource: textContent,
            container: textLayer,
            viewport,
          });
          await layer.render();
        }
      } catch (e) {
        console.warn("PdfViewer text layer skipped", e);
      }
    } catch (e) {
      const name = e instanceof Error ? e.name : "";
      if (name === "RenderingCancelledException") return;
      if (token === renderTokenRef.current) {
        const msg = e instanceof Error ? e.message : String(e);
        setError(`Failed to render page: ${msg}`);
        console.error("PdfViewer render error", e);
      }
    } finally {
      if (token === renderTokenRef.current) {
        renderTaskRef.current = null;
        setRendering(false);
      }
    }
  }, []);

  useEffect(() => {
    if (loading) return;
    void renderPage(page);
  }, [page, loading, renderPage]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || loading) return;

    let debounce: ReturnType<typeof setTimeout> | null = null;
    const ro = new ResizeObserver(() => {
      if (debounce) clearTimeout(debounce);
      debounce = setTimeout(() => void renderPage(page), 150);
    });
    ro.observe(container);
    return () => {
      if (debounce) clearTimeout(debounce);
      ro.disconnect();
    };
  }, [page, loading, renderPage]);

  if (error) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 text-red-400 text-sm px-4 text-center">
        <p>{error}</p>
        <a
          href={readerFileUrlForChapter(chapterId, true)}
          target="_blank"
          rel="noreferrer"
          className="text-brand-400 hover:text-brand-300 underline"
        >
          Open PDF in a new tab
        </a>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="relative w-full max-w-4xl flex-1 min-h-0 flex items-center justify-center overflow-hidden"
    >
      {(loading || rendering) && (
        <div className="absolute inset-0 flex items-center justify-center bg-gray-950/60 z-10 pointer-events-none">
          <div className="animate-pulse text-gray-400 text-sm">Loading page…</div>
        </div>
      )}
      <div className="pdf-viewer-wrap">
        <canvas ref={canvasRef} className="shadow-lg rounded-sm bg-white" />
        <div ref={textLayerRef} className="textLayer" aria-hidden="true" />
      </div>
    </div>
  );
}

export default function PdfViewer({ chapterId, page, onReady }: PdfViewerProps) {
  const [native, setNative] = useState(preferNativeViewer);
  const [pageCount, setPageCount] = useState(0);

  // Lightweight page-count probe so footer nav works with the native viewer.
  useEffect(() => {
    if (!native) return;
    let cancelled = false;
    const task = getDocument({
      url: readerFileUrlForChapter(chapterId, true),
      withCredentials: true,
      wasmUrl: `${PDFJS_ASSETS}/wasm/`,
      cMapUrl: `${PDFJS_ASSETS}/cmaps/`,
      cMapPacked: true,
      standardFontDataUrl: `${PDFJS_ASSETS}/standard_fonts/`,
      iccUrl: `${PDFJS_ASSETS}/iccs/`,
      isOffscreenCanvasSupported: false,
    });
    void task.promise
      .then((doc) => {
        if (!cancelled) {
          setPageCount(doc.numPages);
          onReady?.(doc.numPages);
          void doc.cleanup();
        } else {
          void doc.cleanup();
        }
      })
      .catch(() => {
        // Native iframe can still show the file; page count stays 0.
      });
    return () => {
      cancelled = true;
      try {
        task.destroy();
      } catch {
        // ignore
      }
    };
  }, [chapterId, native, onReady]);

  return (
    <div className="relative w-full max-w-5xl flex-1 min-h-0 flex flex-col items-stretch gap-2">
      <div className="flex justify-end gap-2 shrink-0 px-1">
        <button
          type="button"
          onClick={() => setNative((v) => !v)}
          className="text-[11px] text-gray-500 hover:text-gray-300"
        >
          {native ? "Use canvas viewer" : "Use browser PDF viewer"}
        </button>
        <a
          href={readerFileUrlForChapter(chapterId, true)}
          target="_blank"
          rel="noreferrer"
          className="text-[11px] text-gray-500 hover:text-gray-300"
        >
          Open in new tab
        </a>
      </div>
      {native ? (
        <NativePdfFrame
          chapterId={chapterId}
          page={page}
          pageCount={pageCount}
          onReady={onReady}
        />
      ) : (
        <CanvasPdfViewer chapterId={chapterId} page={page} onReady={onReady} />
      )}
    </div>
  );
}
