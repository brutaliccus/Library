/**
 * Magnet / torrent URL helpers. Do not log full magnets (may contain trackers/PII).
 */

const MAGNET_RE = /^magnet:\?/i;
const INFO_HASH_RE = /[?&]xt=urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})/i;
const AA_MD5_RE = /(?:^|\/)md5\/([a-fA-F0-9]{32})(?:\/|$|\?|#)/i;

/**
 * @param {string|null|undefined} text
 * @returns {string|null}
 */
export function extractMagnet(text) {
  if (!text || typeof text !== "string") return null;
  const trimmed = text.trim();
  if (MAGNET_RE.test(trimmed)) return trimmed;

  // Selection may include surrounding whitespace or quotes
  const m = trimmed.match(/magnet:\?[^\s"'<>]+/i);
  return m ? m[0] : null;
}

/**
 * ABB detail pages and direct .torrent URLs are accepted by the backend as download_url.
 * @param {string|null|undefined} url
 * @returns {string|null}
 */
export function extractDownloadUrl(url) {
  if (!url || typeof url !== "string") return null;
  const trimmed = url.trim();
  try {
    const u = new URL(trimmed);
    if (!/^https?:$/i.test(u.protocol)) return null;
    const host = u.hostname.toLowerCase();
    const path = u.pathname.toLowerCase();
    if (host.includes("audiobookbay") || host.includes("abyss.to")) return trimmed;
    if (path.endsWith(".torrent")) return trimmed;
    return null;
  } catch {
    return null;
  }
}

/**
 * Anna's Archive md5 page → aa_md5 for the annas_archive request path.
 * @param {string|null|undefined} url
 * @returns {string|null}
 */
export function extractAnnasMd5(url) {
  if (!url || typeof url !== "string") return null;
  const m = url.trim().match(AA_MD5_RE);
  return m ? m[1].toLowerCase() : null;
}

/**
 * @param {string} magnet
 * @returns {string}
 */
export function titleFromMagnet(magnet) {
  try {
    const u = new URL(magnet);
    const dn = u.searchParams.get("dn");
    if (dn) {
      try {
        return decodeURIComponent(dn.replace(/\+/g, " ")).trim() || "Magnet request";
      } catch {
        return dn.replace(/\+/g, " ").trim() || "Magnet request";
      }
    }
  } catch {
    // magnet: URLs sometimes fail URL() in older engines; fall back to regex
    const m = magnet.match(/[?&]dn=([^&]+)/i);
    if (m) {
      try {
        return decodeURIComponent(m[1].replace(/\+/g, " ")).trim() || "Magnet request";
      } catch {
        return m[1].replace(/\+/g, " ").trim() || "Magnet request";
      }
    }
  }
  const hash = magnet.match(INFO_HASH_RE);
  if (hash) return `Magnet ${hash[1].slice(0, 8)}…`;
  return "Magnet request";
}

/**
 * Best-effort "Author - Title" / "Title - Author" split for release names.
 * @param {string} title
 * @returns {{ title: string, author: string|null }}
 */
export function splitTitleAuthor(title) {
  const t = (title || "").trim();
  if (!t) return { title: "Untitled", author: null };

  // Common: "Author - Title [reader]" or "Title - Author"
  const parts = t.split(/\s+-\s+/);
  if (parts.length >= 2) {
    const left = parts[0].trim();
    const right = parts.slice(1).join(" - ").replace(/\s*[\[(].*$/, "").trim();
    // Prefer shorter side as author when both look like names
    if (left.length > 0 && right.length > 0) {
      if (left.split(/\s+/).length <= 4 && left.length < right.length) {
        return { title: right, author: left };
      }
      return { title: left, author: right.length < 80 ? right : null };
    }
  }
  return { title: t.replace(/\s*[\[(].*$/, "").trim() || t, author: null };
}

/**
 * @param {string} title
 * @param {string|null} pageUrl
 * @returns {"audiobook"|"ebook"}
 */
export function inferMediaType(title, pageUrl = null) {
  const hay = `${title || ""} ${pageUrl || ""}`.toLowerCase();
  if (/\b(epub|pdf|mobi|azw3?|fb2|djvu)\b/.test(hay)) return "ebook";
  if (pageUrl && /annas-archive|annasarchive/i.test(pageUrl)) return "ebook";
  return "audiobook";
}

/**
 * Short label for notifications (never the full magnet).
 * @param {string} title
 */
export function shortTitle(title) {
  const t = (title || "Request").trim();
  return t.length > 60 ? `${t.slice(0, 57)}…` : t;
}
