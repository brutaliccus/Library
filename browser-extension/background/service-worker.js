/**
 * MV3 service worker: context menus + request creation.
 * Never log magnets or tokens.
 */

import { listLibraries, getLibrary, upsertLibrary } from "../lib/storage.js";
import {
  createRequest,
  ensureHostPermission,
} from "../lib/api.js";
import {
  extractMagnet,
  extractDownloadUrl,
  extractAnnasMd5,
  titleFromMagnet,
  splitTitleAuthor,
  inferMediaType,
  shortTitle,
} from "../lib/magnet.js";

const MENU_ROOT = "send-to-library-root";
const MENU_SINGLE = "send-to-library-single";
const MENU_PREFIX = "send-to-library:";
const MENU_SELECTION = "send-to-library-selection";
const MENU_SELECTION_PREFIX = "send-to-library-sel:";
const MENU_SEL_ROOT = "send-to-library-sel-root";

/** @type {Promise<void> | null} */
let rebuildMenusChain = null;

chrome.runtime.onInstalled.addListener(() => {
  scheduleRebuildMenus();
});

chrome.runtime.onStartup.addListener(() => {
  scheduleRebuildMenus();
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.library_extension_registry_v1) {
    scheduleRebuildMenus();
  }
});

// Refresh menus periodically in case SW was restarted mid-session
chrome.alarms.create("rebuild-menus", { periodInMinutes: 30 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "rebuild-menus") scheduleRebuildMenus();
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "rebuild-menus") {
    scheduleRebuildMenus()
      .then(() => sendResponse({ ok: true }))
      .catch((e) => {
        sendResponse({ ok: false, error: e.message });
      });
    return true;
  }
  if (msg?.type === "send-test") {
    handleSend(msg.libraryId, msg.payload)
      .then((r) => sendResponse({ ok: true, result: r }))
      .catch((e) => sendResponse({ ok: false, error: e.message, code: e.code }));
    return true;
  }
  return false;
});

/** Serialize rebuilds so overlapping connect/storage/SW events cannot clear menus. */
function scheduleRebuildMenus() {
  rebuildMenusChain = (rebuildMenusChain || Promise.resolve())
    .catch(() => {})
    .then(() => rebuildMenus());
  return rebuildMenusChain;
}

/**
 * Build context menus from the current library registry.
 *
 * Important: Chromium match patterns do NOT support the `magnet:` scheme.
 * Using `targetUrlPatterns: ["magnet:*", …]` makes contextMenus.create fail
 * (Invalid url pattern), so the whole "Send to …" link item never appears.
 * Show on all links/selections; the click handler filters unsupported targets.
 */
async function rebuildMenus() {
  await chrome.contextMenus.removeAll();
  const libraries = await listLibraries();

  if (libraries.length === 0) {
    await createMenu({
      id: MENU_ROOT,
      title: "Send to Library (connect a library first…)",
      contexts: ["link", "selection"],
      enabled: false,
    });
    return;
  }

  if (libraries.length === 1) {
    const lib = libraries[0];
    const name = libraryMenuTitle(lib);
    await createMenu({
      id: MENU_SINGLE,
      title: `Send to ${name}`,
      contexts: ["link"],
    });
    await createMenu({
      id: MENU_SELECTION,
      title: `Send selection to ${name}`,
      contexts: ["selection"],
    });
    await chrome.storage.session.set({ soleLibraryId: lib.id });
    return;
  }

  await createMenu({
    id: MENU_ROOT,
    title: "Send to Library",
    contexts: ["link"],
  });
  await createMenu({
    id: MENU_SEL_ROOT,
    title: "Send selection to Library",
    contexts: ["selection"],
  });

  for (const lib of libraries) {
    const name = libraryMenuTitle(lib);
    await createMenu({
      id: `${MENU_PREFIX}${lib.id}`,
      parentId: MENU_ROOT,
      title: name,
      contexts: ["link"],
    });
    await createMenu({
      id: `${MENU_SELECTION_PREFIX}${lib.id}`,
      parentId: MENU_SEL_ROOT,
      title: name,
      contexts: ["selection"],
    });
  }
}

/** @param {{ name?: string, origin?: string }} lib */
function libraryMenuTitle(lib) {
  const name = (lib?.name || "").trim();
  if (name) return name;
  try {
    return new URL(lib.origin || "").hostname || "Library";
  } catch {
    return "Library";
  }
}

/**
 * @param {chrome.contextMenus.CreateProperties} props
 * @returns {Promise<void>}
 */
function createMenu(props) {
  return new Promise((resolve) => {
    try {
      chrome.contextMenus.create(props, () => {
        const err = chrome.runtime.lastError;
        if (err) console.warn("contextMenus.create:", err.message);
        resolve();
      });
    } catch (e) {
      console.warn("contextMenus.create failed", e);
      resolve();
    }
  });
}

chrome.contextMenus.onClicked.addListener(async (info) => {
  try {
    const libraryId = await resolveLibraryId(info.menuItemId);
    if (!libraryId) {
      await notify("Connect a library", "Open extension settings to add your Library Site.", true);
      chrome.runtime.openOptionsPage();
      return;
    }

    const payload = buildPayloadFromClick(info);
    if (!payload) {
      await notify(
        "Nothing to send",
        "Use a magnet link, .torrent URL, or Anna’s Archive /md5/… link (or select a magnet URL).",
        true
      );
      return;
    }

    await handleSend(libraryId, payload);
  } catch (e) {
    await notify("Send failed", e.message || "Unknown error", true);
    if (e.code === "AUTH_REQUIRED") {
      chrome.runtime.openOptionsPage();
    }
  }
});

/**
 * @param {string} menuItemId
 */
async function resolveLibraryId(menuItemId) {
  if (menuItemId === MENU_SINGLE || menuItemId === MENU_SELECTION) {
    const { soleLibraryId } = await chrome.storage.session.get("soleLibraryId");
    if (soleLibraryId) return soleLibraryId;
    const libs = await listLibraries();
    return libs[0]?.id || null;
  }
  if (menuItemId.startsWith(MENU_PREFIX)) {
    return menuItemId.slice(MENU_PREFIX.length);
  }
  if (menuItemId.startsWith(MENU_SELECTION_PREFIX)) {
    return menuItemId.slice(MENU_SELECTION_PREFIX.length);
  }
  return null;
}

/**
 * @param {chrome.contextMenus.OnClickData} info
 */
function buildPayloadFromClick(info) {
  const linkUrl = info.linkUrl || "";
  const selection = info.selectionText || "";
  const pageUrl = info.pageUrl || "";

  const magnet = extractMagnet(linkUrl) || extractMagnet(selection);
  if (magnet) {
    const rawTitle = titleFromMagnet(magnet);
    const { title, author } = splitTitleAuthor(rawTitle);
    return {
      title,
      author,
      magnet_link: magnet,
      media_type: inferMediaType(rawTitle, pageUrl),
      indexer: "Browser Extension",
      source: "browser_extension",
    };
  }

  const aaMd5 = extractAnnasMd5(linkUrl) || extractAnnasMd5(pageUrl);
  if (aaMd5) {
    const rawTitle = (selection || documentTitleHint(pageUrl) || `Anna's Archive ${aaMd5.slice(0, 8)}`).trim();
    const { title, author } = splitTitleAuthor(rawTitle);
    return {
      title,
      author,
      source: "annas_archive",
      aa_md5: aaMd5,
      media_type: "ebook",
      indexer: "Anna's Archive",
    };
  }

  const downloadUrl = extractDownloadUrl(linkUrl);
  if (downloadUrl) {
    const rawTitle = (selection || documentTitleHint(downloadUrl) || "Torrent request").trim();
    const { title, author } = splitTitleAuthor(rawTitle);
    return {
      title,
      author,
      download_url: downloadUrl,
      media_type: inferMediaType(rawTitle, pageUrl || downloadUrl),
      indexer: "Browser Extension",
      source: "browser_extension",
    };
  }

  return null;
}

/** @param {string} url */
function documentTitleHint(url) {
  try {
    const u = new URL(url);
    const last = u.pathname.split("/").filter(Boolean).pop() || "";
    return decodeURIComponent(last).replace(/[-_]+/g, " ").trim() || null;
  } catch {
    return null;
  }
}

/**
 * @param {string} libraryId
 * @param {object} payload
 */
async function handleSend(libraryId, payload) {
  const library = await getLibrary(libraryId);
  if (!library) {
    throw Object.assign(new Error("Library not found — reconnect in settings."), {
      code: "AUTH_REQUIRED",
    });
  }

  await ensureHostPermission(library.origin);

  const body = {
    title: payload.title,
    author: payload.author || undefined,
    magnet_link: payload.magnet_link || undefined,
    download_url: payload.download_url || undefined,
    indexer: payload.indexer || "Browser Extension",
    media_type: payload.media_type || "audiobook",
    source: payload.source || "browser_extension",
    aa_md5: payload.aa_md5 || undefined,
    aa_file_extension: payload.aa_file_extension || undefined,
  };

  const result = await createRequest(library, body);

  // Refresh local copy (tokens may have rotated) and bump lastUsedAt
  const fresh = await getLibrary(libraryId);
  if (fresh) {
    await upsertLibrary({
      id: fresh.id,
      origin: fresh.origin,
      name: fresh.name,
      email: fresh.email,
      session: fresh.session,
    });
  }

  await notify(
    `Sent to ${library.name}`,
    shortTitle(result.title || body.title),
    false
  );
  return result;
}

/**
 * @param {string} title
 * @param {string} message
 * @param {boolean} isError
 */
async function notify(title, message, isError) {
  try {
    await chrome.notifications.create({
      type: "basic",
      iconUrl: chrome.runtime.getURL("icons/icon128.png"),
      title,
      message,
      priority: isError ? 2 : 0,
    });
  } catch {
    // Notifications can fail if permission revoked; ignore
  }
}

// Initial build when SW loads (also covers connect without requiring reload)
scheduleRebuildMenus();
