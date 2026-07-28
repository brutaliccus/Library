/**
 * Offline EPUB HTML pages + embedded resources (Cache API).
 *
 * PDF offline uses the source file blob (ebookCache). EPUB still renders via
 * Kavita-style HTML pages, so we mirror book-page + resources into a separate
 * cache that the reader reads directly (Capacitor WebView often bypasses SW).
 *
 * Keys keep ?page= / ?file= — do not use cacheStorageKey (strips query).
 */

import api from "../api/client";
import { toAbsoluteUrl } from "../api/instanceUrl";
import { hasStorageRoom } from "./mediaStorage";

const PAGES_CACHE = "ebook-pages-v1";
/** Capture absolute or path-only resource URLs embedded in page HTML. */
const RESOURCE_URL_RE =
  /(?:https?:\/\/[^"'/\s]+)?(\/api\/library\/reader\/\d+\/resources\?file=[^"'&\s]+)/gi;

function cacheSupported(): boolean {
  return typeof caches !== "undefined";
}

function pageUrl(chapterId: number, page: number): string {
  return toAbsoluteUrl(`/api/library/reader/${chapterId}/book-page?page=${page}`);
}

function completeMarkerUrl(chapterId: number): string {
  return toAbsoluteUrl(`/api/library/reader/${chapterId}/offline-pages-complete`);
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  try {
    const token = localStorage.getItem("access_token");
    if (token) headers.Authorization = `Bearer ${token}`;
  } catch {
    /* ignore */
  }
  return headers;
}

/** Unique absolute resource URLs referenced by this page HTML. */
function extractResourceUrls(html: string): string[] {
  const urls = new Set<string>();
  RESOURCE_URL_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = RESOURCE_URL_RE.exec(html)) !== null) {
    const pathOrAbs = m[1];
    urls.add(pathOrAbs.startsWith("http") ? pathOrAbs : toAbsoluteUrl(pathOrAbs));
  }
  return [...urls];
}

async function putText(cache: Cache, url: string, body: string, contentType: string): Promise<void> {
  await cache.put(
    url,
    new Response(body, {
      status: 200,
      headers: {
        "Content-Type": contentType,
        "Content-Length": String(new Blob([body]).size),
      },
    })
  );
}

async function cacheResourceByUrl(cache: Cache, url: string): Promise<boolean> {
  if (await cache.match(url)) return true;
  try {
    const resp = await fetch(url, { headers: authHeaders(), credentials: "include" });
    if (!resp.ok) return false;
    const buf = await resp.arrayBuffer();
    const type = resp.headers.get("content-type") || "application/octet-stream";
    await cache.put(
      url,
      new Response(buf, {
        status: 200,
        headers: {
          "Content-Type": type,
          "Content-Length": String(buf.byteLength),
        },
      })
    );
    return true;
  } catch {
    return false;
  }
}

/** Persist one Kavita HTML page (+ its resources) for offline open. */
export async function cacheEbookPageHtml(
  chapterId: number,
  page: number,
  html: string
): Promise<void> {
  if (!cacheSupported() || !html) return;
  try {
    const cache = await caches.open(PAGES_CACHE);
    await putText(cache, pageUrl(chapterId, page), html, "text/html; charset=utf-8");
    for (const url of extractResourceUrls(html)) {
      if (!(await hasStorageRoom())) break;
      await cacheResourceByUrl(cache, url);
    }
  } catch {
    /* best-effort */
  }
}

/**
 * Download every EPUB page (and resources) for offline reading.
 * Returns false if any page fetch fails.
 */
export async function cacheAllEbookPages(
  chapterId: number,
  pageCount: number,
  opts?: { onProgress?: (done: number, total: number) => void }
): Promise<boolean> {
  if (!cacheSupported() || pageCount <= 0) return false;
  const cache = await caches.open(PAGES_CACHE);
  let ok = true;
  for (let p = 0; p < pageCount; p++) {
    if (!(await hasStorageRoom())) {
      ok = false;
      break;
    }
    const url = pageUrl(chapterId, p);
    if (!(await cache.match(url))) {
      try {
        const { data } = await api.get(`/library/reader/${chapterId}/book-page`, {
          params: { page: p },
          responseType: "text",
        });
        const html = typeof data === "string" ? data : String(data ?? "");
        if (!html) {
          ok = false;
          break;
        }
        await putText(cache, url, html, "text/html; charset=utf-8");
        for (const resUrl of extractResourceUrls(html)) {
          if (!(await hasStorageRoom())) break;
          await cacheResourceByUrl(cache, resUrl);
        }
      } catch {
        ok = false;
        break;
      }
    }
    opts?.onProgress?.(p + 1, pageCount);
  }
  if (ok) {
    await putText(cache, completeMarkerUrl(chapterId), "1", "text/plain");
  }
  return ok;
}

export async function areEbookPagesCached(chapterId: number, pageCount?: number): Promise<boolean> {
  if (!cacheSupported()) return false;
  try {
    const cache = await caches.open(PAGES_CACHE);
    if (await cache.match(completeMarkerUrl(chapterId))) return true;
    if (pageCount == null || pageCount <= 0) return false;
    for (let p = 0; p < pageCount; p++) {
      if (!(await cache.match(pageUrl(chapterId, p)))) return false;
    }
    return true;
  } catch {
    return false;
  }
}

/**
 * Load a cached EPUB page. Rewrites resource URLs to blob: Object URLs so
 * Capacitor (SW-bypass) can still show images/fonts offline.
 * Caller should revoke returned objectUrls when done with the page.
 */
export async function getCachedEbookPage(
  chapterId: number,
  page: number
): Promise<{ html: string; objectUrls: string[] } | null> {
  if (!cacheSupported()) return null;
  try {
    const cache = await caches.open(PAGES_CACHE);
    const resp = await cache.match(pageUrl(chapterId, page));
    if (!resp) return null;
    let html = await resp.text();
    if (!html) return null;

    const objectUrls: string[] = [];
    for (const absUrl of extractResourceUrls(html)) {
      const r = await cache.match(absUrl);
      if (!r) continue;
      const blob = await r.blob();
      if (!blob.size) continue;
      const obj = URL.createObjectURL(blob);
      objectUrls.push(obj);
      try {
        const pathOnly = new URL(absUrl).pathname + new URL(absUrl).search;
        html = html.split(absUrl).join(obj);
        html = html.split(pathOnly).join(obj);
      } catch {
        html = html.split(absUrl).join(obj);
      }
    }
    return { html, objectUrls };
  } catch {
    return null;
  }
}

export async function clearEbookPageCache(chapterId: number): Promise<void> {
  if (!cacheSupported()) return;
  try {
    const cache = await caches.open(PAGES_CACHE);
    const keys = await cache.keys();
    const needle = `/api/library/reader/${chapterId}/`;
    await Promise.all(
      keys
        .filter((req) => req.url.includes(needle))
        .map((req) => cache.delete(req))
    );
  } catch {
    /* best-effort */
  }
}
