/**
 * AudioBookBay file-list helpers for the extension.
 * scrapeAbbFileListFromDocument is injected into the page via chrome.scripting.
 */

/**
 * @param {string} url
 */
export function isAudiobookBayUrl(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host.includes("audiobookbay");
  } catch {
    return false;
  }
}

/**
 * Pure parser for table cell rows (testable outside a browser).
 * @param {string[][]} rows
 * @returns {{ path: string, size_text?: string }[]}
 */
export function parseAbbFileListCells(rows) {
  /** @type {{ path: string, size_text?: string }[]} */
  const out = [];
  const sizeRe = /^([\d.]+)\s*(TB|GB|MB|KB)s?$/i;
  for (const cells of rows || []) {
    const parts = (cells || []).map((c) => String(c || "").trim()).filter(Boolean);
    if (parts.length < 2) continue;
    let sizeText = "";
    let pathParts = parts;
    if (sizeRe.test(parts[parts.length - 1])) {
      sizeText = parts[parts.length - 1];
      pathParts = parts.slice(0, -1);
    } else if (
      parts.length >= 2 &&
      sizeRe.test(`${parts[parts.length - 2]} ${parts[parts.length - 1]}`)
    ) {
      sizeText = `${parts[parts.length - 2]} ${parts[parts.length - 1]}`;
      pathParts = parts.slice(0, -2);
    }
    if (!pathParts.length) continue;
    const joined = pathParts.join(" ").toLowerCase();
    if (
      joined.startsWith("tracker:") ||
      joined.startsWith("announce url") ||
      joined.includes("info hash") ||
      joined.startsWith("creation date")
    ) {
      continue;
    }
    if (pathParts[0].toLowerCase() === "filename" || pathParts[0].toLowerCase() === "file") {
      continue;
    }
    const path = pathParts.length === 1 ? pathParts[0] : pathParts.join("/");
    const name = path.split("/").pop() || "";
    if (!name.includes(".")) continue;
    /** @type {{ path: string, size_text?: string }} */
    const row = { path };
    if (sizeText) row.size_text = sizeText;
    out.push(row);
  }
  return out;
}

/**
 * Runs in the ABB page context (no imports / closures).
 * @returns {{ path: string, size_text?: string }[]}
 */
export function scrapeAbbFileListFromDocument() {
  const sizeRe = /^([\d.]+)\s*(TB|GB|MB|KB)s?$/i;
  /** @type {string[][]} */
  const cellRows = [];
  const tables = Array.from(document.querySelectorAll("table"));
  for (const table of tables) {
    const rows = Array.from(table.querySelectorAll("tr"));
    for (const tr of rows) {
      const cells = Array.from(tr.querySelectorAll("td, th")).map((c) =>
        (c.textContent || "").replace(/\s+/g, " ").trim()
      );
      if (cells.length >= 2) cellRows.push(cells);
    }
  }
  /** @type {{ path: string, size_text?: string }[]} */
  const out = [];
  for (const parts of cellRows) {
    let sizeText = "";
    let pathParts = parts.filter(Boolean);
    if (pathParts.length < 2) continue;
    if (sizeRe.test(pathParts[pathParts.length - 1])) {
      sizeText = pathParts[pathParts.length - 1];
      pathParts = pathParts.slice(0, -1);
    } else if (
      pathParts.length >= 2 &&
      sizeRe.test(`${pathParts[pathParts.length - 2]} ${pathParts[pathParts.length - 1]}`)
    ) {
      sizeText = `${pathParts[pathParts.length - 2]} ${pathParts[pathParts.length - 1]}`;
      pathParts = pathParts.slice(0, -2);
    }
    if (!pathParts.length) continue;
    const joined = pathParts.join(" ").toLowerCase();
    if (
      joined.startsWith("tracker:") ||
      joined.startsWith("announce url") ||
      joined.includes("info hash") ||
      joined.startsWith("creation date")
    ) {
      continue;
    }
    const head = pathParts[0].toLowerCase();
    if (head === "filename" || head === "file" || head === "name") continue;
    const path = pathParts.length === 1 ? pathParts[0] : pathParts.join("/");
    const name = path.split("/").pop() || "";
    if (!name.includes(".")) continue;
    /** @type {{ path: string, size_text?: string }} */
    const row = { path };
    if (sizeText) row.size_text = sizeText;
    out.push(row);
  }
  return out;
}