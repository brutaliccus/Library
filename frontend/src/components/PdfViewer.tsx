import { useCallback, useEffect, useRef, useState } from "react";
import { getDocument, GlobalWorkerOptions, TextLayer, type PDFDocumentProxy } from "pdfjs-dist";
import pdfjsWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { readerFileUrlForChapter } from "../utils/ebookCache";
import "./PdfViewer.css";

GlobalWorkerOptions.workerSrc = pdfjsWorker;

interface PdfViewerProps {
  chapterId: number;
  page: number;
  onReady?: (totalPages: number) => void;
}

export default function PdfViewer({ chapterId, page, onReady }: PdfViewerProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textLayerRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const docRef = useRef<PDFDocumentProxy | null>(null);
  const renderTokenRef = useRef(0);
  const [loading, setLoading] = useState(true);
  const [rendering, setRendering] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    docRef.current?.cleanup();
    docRef.current = null;

    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const url = readerFileUrlForChapter(chapterId, true);
        const resp = await fetch(url, { credentials: "include" });
        if (!resp.ok) throw new Error("fetch failed");
        const data = await resp.arrayBuffer();
        const doc = await getDocument({ data }).promise;
        if (cancelled) {
          void doc.cleanup();
          return;
        }
        docRef.current = doc;
        onReady?.(doc.numPages);
      } catch {
        if (!cancelled) setError("Failed to load PDF");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void load();
    return () => {
      cancelled = true;
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

    const pageNum = pageIndex + 1;
    if (pageNum < 1 || pageNum > doc.numPages) return;

    const token = ++renderTokenRef.current;
    setRendering(true);
    try {
      const pdfPage = await doc.getPage(pageNum);
      if (token !== renderTokenRef.current) return;

      const baseViewport = pdfPage.getViewport({ scale: 1 });
      const fitScale = Math.min(
        container.clientWidth / baseViewport.width,
        container.clientHeight / baseViewport.height,
      );
      const viewport = pdfPage.getViewport({ scale: fitScale });
      const outputScale = window.devicePixelRatio || 1;
      const ctx = canvas.getContext("2d");
      if (!ctx || token !== renderTokenRef.current) return;

      canvas.width = Math.floor(viewport.width * outputScale);
      canvas.height = Math.floor(viewport.height * outputScale);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      ctx.setTransform(outputScale, 0, 0, outputScale, 0, 0);

      textLayer.innerHTML = "";
      textLayer.style.width = `${viewport.width}px`;
      textLayer.style.height = `${viewport.height}px`;

      await pdfPage.render({ canvas, canvasContext: ctx, viewport }).promise;
      if (token !== renderTokenRef.current) return;

      const textContent = await pdfPage.getTextContent();
      if (token !== renderTokenRef.current) return;

      const layer = new TextLayer({
        textContentSource: textContent,
        container: textLayer,
        viewport,
      });
      await layer.render();
    } catch {
      if (token === renderTokenRef.current) setError("Failed to render page");
    } finally {
      if (token === renderTokenRef.current) setRendering(false);
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
      <div className="flex flex-1 items-center justify-center text-red-400 text-sm px-4 text-center">
        {error}
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="relative w-full max-w-4xl flex-1 min-h-0 flex items-center justify-center overflow-hidden"
    >
      {(loading || rendering) && (
        <div className="absolute inset-0 flex items-center justify-center bg-gray-950/60 z-10">
          <div className="animate-pulse text-gray-400 text-sm">Loading page…</div>
        </div>
      )}
      <div ref={wrapRef} className="pdf-viewer-wrap">
        <canvas ref={canvasRef} className="shadow-lg rounded-sm bg-white" />
        <div ref={textLayerRef} className="textLayer" aria-hidden="true" />
      </div>
    </div>
  );
}
