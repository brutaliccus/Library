import { useEffect, useRef } from "react";
import "foliate-js/view.js";

export interface EpubTocItem {
  label: string;
  href: string;
  children?: EpubTocItem[];
}

export interface EpubLocation {
  cfi: string;
  fraction: number;
  sectionIndex: number;
  tocLabel?: string;
  locationCurrent?: number;
  locationTotal?: number;
}

interface EpubViewerProps {
  /** File/Blob or blob:/http(s) URL for the EPUB (or MOBI/AZW3). */
  source: File | Blob | string;
  initialCfi?: string | null;
  fontSize: number;
  fontFamily: string;
  /** Top/bottom margin reserved for chrome overlay (px). */
  chromeMarginPx?: number;
  onReady?: () => void;
  onError?: (message: string) => void;
  onRelocate?: (loc: EpubLocation) => void;
  onToc?: (toc: EpubTocItem[]) => void;
  onCenterTap?: () => void;
}

type FoliateView = HTMLElement & {
  book: {
    toc?: Array<{ label?: string; href?: string; subitems?: unknown[] }>;
    transformTarget?: EventTarget;
  };
  renderer: {
    setAttribute(name: string, value: string): void;
    setStyles?: (styles: string) => void;
  };
  open(book: File | Blob | string): Promise<void>;
  close(): void;
  init(opts: { lastLocation?: string; showTextStart?: boolean }): Promise<void>;
  goTo(target: string | number): Promise<unknown>;
  prev(): Promise<void>;
  next(): Promise<void>;
  goLeft(): void;
  goRight(): void;
};

function mapToc(items: unknown[] | undefined): EpubTocItem[] {
  if (!Array.isArray(items)) return [];
  const out: EpubTocItem[] = [];
  for (const raw of items) {
    const item = raw as { label?: string; href?: string; subitems?: unknown[] };
    const label = (item.label || "").trim();
    const href = (item.href || "").trim();
    if (!label && !href) continue;
    const children = mapToc(item.subitems);
    out.push({
      label: label || href || "Section",
      href,
      children: children.length ? children : undefined,
    });
  }
  return out;
}

function readerCss(fontSize: number, fontFamily: string): string {
  return `
    @namespace epub "http://www.idpf.org/2007/ops";
    html {
      color-scheme: dark;
      background: #030712 !important;
    }
    body {
      background: #030712 !important;
      color: #e5e7eb !important;
      font-size: ${fontSize}px !important;
      font-family: ${fontFamily} !important;
      line-height: 1.5 !important;
    }
    a:link { color: #93c5fd; }
    a:visited { color: #c4b5fd; }
    p, li, blockquote, dd {
      line-height: 1.55;
      text-align: justify;
      -webkit-hyphens: auto;
      hyphens: auto;
    }
    [align="left"] { text-align: left; }
    [align="right"] { text-align: right; }
    [align="center"] { text-align: center; }
    [align="justify"] { text-align: justify; }
    pre { white-space: pre-wrap !important; }
    img, svg, image { max-width: 100% !important; height: auto !important; }
    aside[epub|type~="endnote"],
    aside[epub|type~="footnote"],
    aside[epub|type~="note"],
    aside[epub|type~="rearnote"] {
      display: none;
    }
  `;
}

function toOpenable(source: File | Blob | string): File | Blob | string {
  if (typeof source === "string") return source;
  if (source instanceof File) return source;
  const type = source.type || "application/epub+zip";
  return new File([source], "book.epub", { type });
}

/**
 * Foliate-js EPUB/MOBI viewer. Owns pagination — parent supplies chrome only.
 */
export default function EpubViewer({
  source,
  initialCfi,
  fontSize,
  fontFamily,
  chromeMarginPx = 56,
  onReady,
  onError,
  onRelocate,
  onToc,
  onCenterTap,
}: EpubViewerProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<FoliateView | null>(null);
  const callbacks = useRef({ onReady, onError, onRelocate, onToc, onCenterTap });
  callbacks.current = { onReady, onError, onRelocate, onToc, onCenterTap };
  const initialCfiRef = useRef(initialCfi);
  initialCfiRef.current = initialCfi;

  // Open book once per source identity.
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let cancelled = false;
    let view: FoliateView | null = null;

    const run = async () => {
      try {
        host.replaceChildren();
        view = document.createElement("foliate-view") as FoliateView;
        view.style.display = "block";
        view.style.width = "100%";
        view.style.height = "100%";
        host.append(view);
        viewRef.current = view;

        view.addEventListener("relocate", ((e: Event) => {
          const detail = (e as CustomEvent).detail || {};
          const cfi = typeof detail.cfi === "string" ? detail.cfi : "";
          if (!cfi) return;
          const sectionIndex =
            typeof detail.section?.current === "number" ? detail.section.current : 0;
          callbacks.current.onRelocate?.({
            cfi,
            fraction: typeof detail.fraction === "number" ? detail.fraction : 0,
            sectionIndex,
            tocLabel: detail.tocItem?.label,
            locationCurrent: detail.location?.current,
            locationTotal: detail.location?.total,
          });
        }) as EventListener);

        view.addEventListener("load", ((e: Event) => {
          const doc = (e as CustomEvent).detail?.doc as Document | undefined;
          if (!doc) return;
          doc.addEventListener("click", (ev) => {
            const t = ev.target as Element | null;
            if (t?.closest?.("a[href]")) return;
            const w = doc.defaultView?.innerWidth || 1;
            const x = (ev as MouseEvent).clientX;
            if (x < w / 3) void view?.goLeft();
            else if (x > (2 * w) / 3) void view?.goRight();
            else callbacks.current.onCenterTap?.();
          });
        }) as EventListener);

        const openable = toOpenable(source);
        await view.open(openable);
        if (cancelled) return;

        view.book.transformTarget?.addEventListener("data", ((ev: Event) => {
          const detail = (ev as CustomEvent).detail;
          if (!detail) return;
          detail.data = Promise.resolve(detail.data).catch(() => "");
        }) as EventListener);

        const toc = mapToc(view.book.toc);
        callbacks.current.onToc?.(toc);

        view.renderer.setAttribute("flow", "paginated");
        view.renderer.setAttribute("margin", `${Math.max(24, chromeMarginPx)}px`);
        view.renderer.setAttribute("gap", "5%");
        view.renderer.setAttribute("max-inline-size", "720px");
        view.renderer.setStyles?.(readerCss(fontSize, fontFamily));

        const cfi = (initialCfiRef.current || "").trim();
        if (cfi) {
          await view.init({ lastLocation: cfi });
        } else {
          await view.init({ showTextStart: true });
        }
        if (cancelled) return;
        callbacks.current.onReady?.();
      } catch (err) {
        if (cancelled) return;
        const msg = err instanceof Error ? err.message : "Failed to open ebook";
        callbacks.current.onError?.(msg);
      }
    };

    void run();
    return () => {
      cancelled = true;
      try {
        view?.close();
      } catch {
        /* ignore */
      }
      viewRef.current = null;
      host.replaceChildren();
    };
    // Re-open only when the source object/url changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- font/chrome applied in separate effect
  }, [source]);

  // Live style updates without reloading the book.
  useEffect(() => {
    const view = viewRef.current;
    if (!view?.renderer) return;
    view.renderer.setAttribute("margin", `${Math.max(24, chromeMarginPx)}px`);
    view.renderer.setStyles?.(readerCss(fontSize, fontFamily));
  }, [fontSize, fontFamily, chromeMarginPx]);

  return (
    <div
      ref={hostRef}
      className="w-full h-full min-h-0 bg-gray-950 [&_foliate-view]:w-full [&_foliate-view]:h-full"
    />
  );
}

/** Imperative helpers for parent chrome buttons. */
export function epubViewGo(host: HTMLElement | null, dir: "prev" | "next"): void {
  const view = host?.querySelector("foliate-view") as FoliateView | null;
  if (!view) return;
  if (dir === "prev") void view.goLeft();
  else void view.goRight();
}

export function epubViewGoTo(host: HTMLElement | null, href: string): void {
  const view = host?.querySelector("foliate-view") as FoliateView | null;
  if (!view || !href) return;
  void view.goTo(href);
}
