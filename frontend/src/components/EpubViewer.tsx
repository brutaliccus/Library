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
  source: File | Blob | string;
  initialCfi?: string | null;
  fontSize: number;
  fontFamily: string;
  columnCount?: 1 | 2;
  chromeMarginPx?: number;
  onReady?: () => void;
  onError?: (message: string) => void;
  onRelocate?: (loc: EpubLocation) => void;
  onToc?: (toc: EpubTocItem[]) => void;
  onCenterTap?: () => void;
}

type FoliateSection = {
  linear?: string;
  createDocument?: () => Promise<Document>;
};

type FoliateView = HTMLElement & {
  book: {
    toc?: Array<{ label?: string; href?: string; subitems?: unknown[] }>;
    sections?: FoliateSection[];
    transformTarget?: EventTarget;
  };
  renderer: {
    setAttribute(name: string, value: string): void;
    setStyles?: (styles: string) => void;
    render?: () => void;
    goTo(target: {
      index: number;
      anchor?: ((doc: Document) => Element | number | null) | number;
    }): Promise<void>;
  };
  history?: { pushState(state: unknown): void };
  resolveNavigation?: (target: string | number) => { index?: number } | null | undefined;
  open(book: File | Blob | string): Promise<void>;
  close(): void;
  init(opts: { lastLocation?: string; showTextStart?: boolean }): Promise<void>;
  goTo(target: string | number): Promise<unknown>;
  prev(): Promise<void>;
  next(): Promise<void>;
  goLeft(): void;
  goRight(): void;
};

type HostWithIgnore = HTMLElement & { __ignoreTapUntil?: number };

/** Module-level so ignore works whether parent passes outer wrapper or inner host. */
let ignoreTapsUntil = 0;
let ignorePeGen = 0;

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

function readThemeColors(): { bg: string; fg: string; link: string; visited: string } {
  const cs = getComputedStyle(document.documentElement);
  const tok = (name: string, fallback: string) => {
    const v = cs.getPropertyValue(name).trim();
    return v || fallback;
  };
  return {
    bg: `rgb(${tok("--gray-950", "3 7 18")})`,
    fg: `rgb(${tok("--gray-200", "229 231 235")})`,
    link: `rgb(${tok("--brand-300", "145 167 255")})`,
    visited: `rgb(${tok("--brand-400", "116 143 252")})`,
  };
}

function readerCss(fontSize: number, fontFamily: string, theme = readThemeColors()): string {
  return `
    @namespace epub "http://www.idpf.org/2007/ops";
    html {
      color-scheme: dark;
      background: ${theme.bg} !important;
    }
    body {
      background: ${theme.bg} !important;
      color: ${theme.fg} !important;
      font-size: ${fontSize}px !important;
      font-family: ${fontFamily} !important;
      line-height: 1.5 !important;
    }
    a:link { color: ${theme.link}; }
    a:visited { color: ${theme.visited}; }
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
    aside[epub|type~="endnotes"],
    aside[epub|type~="footnotes"],
    aside[epub|type~="notes"],
    aside[epub|type~="rearnotes"] {
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

function applyLayout(
  view: FoliateView,
  columnCount: 1 | 2,
  chromeMarginPx: number,
  fontSize: number,
  fontFamily: string
) {
  const cols = columnCount === 2 ? "2" : "1";
  view.renderer.setAttribute("flow", "paginated");
  view.renderer.setAttribute("margin", `${Math.max(24, chromeMarginPx)}px`);
  view.renderer.setAttribute("gap", "5%");
  view.renderer.setAttribute("max-inline-size", cols === "1" ? "680px" : "720px");
  view.renderer.setAttribute("max-column-count", cols);
  view.renderer.setStyles?.(readerCss(fontSize, fontFamily));
  try {
    view.renderer.render?.();
  } catch {
    /* ignore */
  }
}

function tapsBlocked(host?: HTMLElement | null): boolean {
  if (Date.now() < ignoreTapsUntil) return true;
  const until = (host as HostWithIgnore | null | undefined)?.__ignoreTapUntil || 0;
  return Date.now() < until;
}

/**
 * Foliate paginates by expanding the iframe across every column and scrolling
 * the container. clientX / innerWidth therefore spans the whole chapter, so a
 * "right third of the screen" click is usually the middle of the iframe.
 * Fold into the visible page using documentElement width (one page column).
 */
function turnFromVisiblePageClick(
  view: FoliateView | null | undefined,
  doc: Document,
  clientX: number,
  onCenterTap?: () => void
): void {
  const pageW = Math.max(1, doc.documentElement.clientWidth || 1);
  let x = clientX % pageW;
  if (x < 0) x += pageW;
  if (x < pageW / 3) void view?.goLeft();
  else if (x > (2 * pageW) / 3) void view?.goRight();
  else onCenterTap?.();
}

const TAP_MOVE_PX = 14;

/**
 * Foliate calls preventDefault on touchmove, which often suppresses the synthetic
 * click on mobile. Handle a stationary pointer/touch end as a page-turn tap, then
 * ignore the follow-up click so desktop/mouse paths do not double-turn.
 */
function attachTapPager(
  target: EventTarget,
  host: () => HTMLElement | null,
  onTap: (clientX: number, ev: Event) => void
): () => void {
  let startX = 0;
  let startY = 0;
  let active = false;
  let ignoreClickUntil = 0;

  const onStart = (ev: Event) => {
    if (tapsBlocked(host())) return;
    // Duck-type: iframe events fail `instanceof PointerEvent` across realms.
    const pe = ev as PointerEvent;
    if ("pointerType" in ev && typeof pe.clientX === "number") {
      if (pe.pointerType === "mouse" && pe.button !== 0) return;
      startX = pe.clientX;
      startY = pe.clientY;
      active = true;
      return;
    }
    const te = ev as TouchEvent;
    if (!te.changedTouches?.length) return;
    startX = te.changedTouches[0].clientX;
    startY = te.changedTouches[0].clientY;
    active = true;
  };

  const onEnd = (ev: Event) => {
    if (!active) return;
    active = false;
    if (tapsBlocked(host())) return;
    let x = startX;
    let y = startY;
    const pe = ev as PointerEvent;
    if ("pointerType" in ev && typeof pe.clientX === "number") {
      x = pe.clientX;
      y = pe.clientY;
    } else {
      const te = ev as TouchEvent;
      if (!te.changedTouches?.length) return;
      x = te.changedTouches[0].clientX;
      y = te.changedTouches[0].clientY;
    }
    if (Math.hypot(x - startX, y - startY) > TAP_MOVE_PX) return;
    const t = (ev as Event & { target?: EventTarget | null }).target as Element | null;
    if (t?.closest?.("a[href]")) return;
    ignoreClickUntil = Date.now() + 450;
    onTap(x, ev);
  };

  const onClick = (ev: Event) => {
    if (Date.now() < ignoreClickUntil || tapsBlocked(host())) {
      ev.preventDefault();
      ev.stopPropagation();
      return;
    }
    const t = (ev as Event & { target?: EventTarget | null }).target as Element | null;
    if (t?.closest?.("a[href]")) return;
    onTap((ev as MouseEvent).clientX, ev);
  };

  const opts: AddEventListenerOptions = { passive: true };
  target.addEventListener("pointerdown", onStart as EventListener, opts);
  target.addEventListener("pointerup", onEnd as EventListener, opts);
  target.addEventListener("touchstart", onStart as EventListener, opts);
  target.addEventListener("touchend", onEnd as EventListener, opts);
  target.addEventListener("click", onClick as EventListener);

  return () => {
    target.removeEventListener("pointerdown", onStart as EventListener, opts);
    target.removeEventListener("pointerup", onEnd as EventListener, opts);
    target.removeEventListener("touchstart", onStart as EventListener, opts);
    target.removeEventListener("touchend", onEnd as EventListener, opts);
    target.removeEventListener("click", onClick as EventListener);
  };
}

export default function EpubViewer({
  source,
  initialCfi,
  fontSize,
  fontFamily,
  columnCount = 1,
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
  const layoutRef = useRef({ columnCount, chromeMarginPx, fontSize, fontFamily });
  layoutRef.current = { columnCount, chromeMarginPx, fontSize, fontFamily };

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

        // Side/center taps on foliate chrome margins (outside the iframe).
        const detachViewTaps = attachTapPager(view, () => hostRef.current, (clientX) => {
          const rect = view!.getBoundingClientRect();
          const w = Math.max(1, rect.width);
          const x = clientX - rect.left;
          if (x < w / 3) void view?.goLeft();
          else if (x > (2 * w) / 3) void view?.goRight();
          else callbacks.current.onCenterTap?.();
        });

        const docCleanups: Array<() => void> = [];
        view.addEventListener("load", ((e: Event) => {
          const doc = (e as CustomEvent).detail?.doc as Document | undefined;
          if (!doc) return;
          const detach = attachTapPager(doc, () => hostRef.current, (clientX) => {
            turnFromVisiblePageClick(view, doc, clientX, () =>
              callbacks.current.onCenterTap?.()
            );
          });
          docCleanups.push(detach);
        }) as EventListener);
        (view as FoliateView & { __tapCleanups?: Array<() => void> }).__tapCleanups = [
          detachViewTaps,
          () => {
            for (const c of docCleanups) c();
            docCleanups.length = 0;
          },
        ];

        await view.open(toOpenable(source));
        if (cancelled) return;

        view.book.transformTarget?.addEventListener("data", ((ev: Event) => {
          const detail = (ev as CustomEvent).detail;
          if (!detail) return;
          detail.data = Promise.resolve(detail.data).catch(() => "");
        }) as EventListener);

        callbacks.current.onToc?.(mapToc(view.book.toc));

        const layout = layoutRef.current;
        applyLayout(
          view,
          layout.columnCount,
          layout.chromeMarginPx,
          layout.fontSize,
          layout.fontFamily
        );

        const cfi = (initialCfiRef.current || "").trim();
        if (cfi) await view.init({ lastLocation: cfi });
        else await view.init({ showTextStart: true });
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
        const cleanups = (view as FoliateView & { __tapCleanups?: Array<() => void> } | null)?.__tapCleanups;
        if (cleanups) for (const c of cleanups) c();
      } catch {
        /* ignore */
      }
      try {
        view?.close();
      } catch {
        /* ignore */
      }
      viewRef.current = null;
      host.replaceChildren();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view?.renderer) return;
    applyLayout(view, columnCount, chromeMarginPx, fontSize, fontFamily);
  }, [fontSize, fontFamily, chromeMarginPx, columnCount]);

  // Keep injected EPUB styles in sync with data-theme on <html>.
  useEffect(() => {
    const view = viewRef.current;
    if (!view?.renderer) return;
    const apply = () => {
      applyLayout(view, columnCount, chromeMarginPx, fontSize, fontFamily);
    };
    apply();
    const obs = new MutationObserver(apply);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => obs.disconnect();
  }, [fontSize, fontFamily, chromeMarginPx, columnCount]);

  return (
    <div
      ref={hostRef}
      data-epub-root
      className="w-full h-full min-h-0 bg-gray-950 [&_foliate-view]:w-full [&_foliate-view]:h-full"
      style={{ backgroundColor: "rgb(var(--gray-950))" }}
    />
  );
}

function getView(host: HTMLElement | null): FoliateView | null {
  return (host?.querySelector("foliate-view") as FoliateView | null) || null;
}

function markIgnore(el: HTMLElement | null | undefined, until: number): void {
  if (!el) return;
  (el as HostWithIgnore).__ignoreTapUntil = until;
}

export function epubViewTapsBlocked(host: HTMLElement | null = null): boolean {
  return tapsBlocked(host);
}

export function epubViewIgnoreTaps(host: HTMLElement | null, ms = 600): void {
  if (!host) return;
  const until = Date.now() + ms;
  ignoreTapsUntil = until;
  markIgnore(host, until);
  markIgnore(host.querySelector("[data-epub-root]") as HTMLElement | null, until);
  if (host.hasAttribute?.("data-epub-root")) markIgnore(host, until);

  // Soften pointer-events blocking: host tap zones sit above the iframe and honor
  // tapsBlocked. Only dim the inner foliate root so TOC/chrome stay interactive.
  const root =
    (host.matches?.("[data-epub-root]") ? host : null) ||
    (host.querySelector("[data-epub-root]") as HTMLElement | null) ||
    host;
  root.style.pointerEvents = "none";
  const gen = ++ignorePeGen;
  window.setTimeout(() => {
    if (gen !== ignorePeGen) return;
    if (!root.isConnected) return;
    root.style.pointerEvents = "";
    if (host !== root && host.style.pointerEvents === "none") {
      host.style.pointerEvents = "";
    }
  }, ms + 50);
}

export function epubViewGo(host: HTMLElement | null, dir: "prev" | "next"): void {
  // Footer buttons should always work; ghost protection is for content taps only.
  const view = getView(host);
  if (!view) return;
  if (dir === "prev") void view.goLeft();
  else void view.goRight();
}

const ONES = [
  "",
  "one",
  "two",
  "three",
  "four",
  "five",
  "six",
  "seven",
  "eight",
  "nine",
  "ten",
  "eleven",
  "twelve",
  "thirteen",
  "fourteen",
  "fifteen",
  "sixteen",
  "seventeen",
  "eighteen",
  "nineteen",
];
const TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"];

function normalizeText(s: string): string {
  return s.replace(/\s+/g, " ").trim().toLowerCase();
}

function chapterNumberWords(n: number): string[] {
  if (n <= 0 || n > 199) return [String(n)];
  const out = new Set<string>([String(n)]);
  if (n < 20) {
    if (ONES[n]) out.add(ONES[n]);
    return [...out];
  }
  const hundred = Math.floor(n / 100);
  const rest = n % 100;
  const head = hundred ? `${ONES[hundred]} hundred` : "";
  let tail = "";
  if (rest === 0) tail = "";
  else if (rest < 20) tail = ONES[rest];
  else {
    const t = TENS[Math.floor(rest / 10)];
    const o = ONES[rest % 10];
    tail = o ? `${t}-${o}` : t;
    out.add(o ? `${t} ${o}` : t);
  }
  const phrase = [head, tail].filter(Boolean).join(" ");
  if (phrase) {
    out.add(phrase);
    out.add(phrase.replace(/-/g, " "));
    out.add(phrase.replace(/ /g, "-"));
  }
  return [...out];
}

function parseChapterNumber(label: string): number | null {
  const m = normalizeText(label).match(/^(?:chapter|ch\.?|chap\.?)\s*(\d+)$/i);
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isFinite(n) && n > 0 ? n : null;
}

function isTocLinkElement(el: Element): boolean {
  return Boolean(el.closest("a[href]"));
}

function elementPlainText(el: Element): string {
  return normalizeText(el.textContent || "");
}

/** Find an in-document heading for a TOC label (skips TOC link text). */
function findTocElement(doc: Document, label: string): Element | null {
  const want = normalizeText(label);
  if (!want) return null;

  const blocks = Array.from(doc.body?.querySelectorAll("h1,h2,h3,h4,h5,h6,p,div,span,section") ?? []);

  for (const el of blocks) {
    if (isTocLinkElement(el)) continue;
    if (elementPlainText(el) === want) return el;
  }

  const chapNum = parseChapterNumber(label);
  if (chapNum != null) {
    const words = new Set(chapterNumberWords(chapNum).map(normalizeText));
    for (let i = 0; i < blocks.length; i++) {
      const el = blocks[i];
      if (isTocLinkElement(el)) continue;
      const text = elementPlainText(el);
      if (text === `chapter ${chapNum}` || text === `ch ${chapNum}` || text === `chap ${chapNum}`) {
        return el;
      }
      if (text === "chapter" || text === "ch" || text === "chap") {
        // Calibre-style: <p>CHAPTER</p><p>ONE</p>
        for (let j = i + 1; j < Math.min(i + 4, blocks.length); j++) {
          const next = blocks[j];
          if (isTocLinkElement(next)) continue;
          const nt = elementPlainText(next);
          if (!nt) continue;
          if (words.has(nt) || nt === String(chapNum)) return el;
          // Stop if we hit another real paragraph of prose.
          if (nt.length > 24) break;
        }
      }
    }
  }

  return null;
}

function flattenTocHrefs(
  items?: Array<{ label?: string; href?: string; subitems?: unknown[] }>,
): string[] {
  if (!Array.isArray(items)) return [];
  const out: string[] = [];
  for (const raw of items) {
    const item = raw as { label?: string; href?: string; subitems?: unknown[] };
    if (item.href) out.push(item.href);
    out.push(...flattenTocHrefs(item.subitems as typeof items));
  }
  return out;
}

/**
 * Broken NCX/nav often points every chapter at the same spine file with no (or
 * identical) fragment — goTo then lands on the section's title plate. Detect that
 * so we can resolve by chapter label instead.
 */
function tocHrefNeedsLabelRefine(view: FoliateView, href: string): boolean {
  const hash = href.includes("#") ? href.slice(href.indexOf("#") + 1) : "";
  if (!hash) return true;

  const hrefs = flattenTocHrefs(view.book?.toc);
  if (!hrefs.length) return true;

  const sameExact = hrefs.filter((h) => h === href).length;
  if (sameExact > 1) return true;

  const path = href.split("#")[0];
  const samePath = hrefs.filter((h) => h.split("#")[0] === path);
  if (samePath.length > 1) {
    const hashes = new Set(samePath.map((h) => (h.includes("#") ? h.slice(h.indexOf("#") + 1) : "")));
    if (hashes.size <= 1) return true;
  }
  return false;
}

async function findSectionIndexForLabel(
  book: FoliateView["book"],
  label: string,
  preferIndex = 0,
): Promise<number | null> {
  const sections = book.sections;
  if (!sections?.length) return null;

  const order: number[] = [];
  for (let i = preferIndex; i < sections.length; i++) order.push(i);
  for (let i = 0; i < preferIndex; i++) order.push(i);

  for (const index of order) {
    const section = sections[index];
    if (!section || section.linear === "no" || !section.createDocument) continue;
    try {
      const doc = await section.createDocument();
      if (findTocElement(doc, label)) return index;
    } catch {
      // ignore unloadable sections
    }
  }
  return null;
}

export async function epubViewGoTo(
  host: HTMLElement | null,
  href: string,
  label?: string,
): Promise<void> {
  const view = getView(host);
  if (!view || !href) return;
  epubViewIgnoreTaps(host, 500);
  try {
    const trimmedLabel = (label || "").trim();
    if (trimmedLabel && tocHrefNeedsLabelRefine(view, href)) {
      const startIndex = view.resolveNavigation?.(href)?.index ?? 0;
      const index = await findSectionIndexForLabel(view.book, trimmedLabel, startIndex);
      if (index != null) {
        let landed: Element | null = null;
        await view.renderer.goTo({
          index,
          anchor: (doc) => {
            landed = findTocElement(doc, trimmedLabel);
            return landed ?? 0;
          },
        });
        if (landed) {
          view.history?.pushState?.(href);
          return;
        }
      }
    }
    await view.goTo(href);
  } finally {
    epubViewIgnoreTaps(host, 350);
  }
}

