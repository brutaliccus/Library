/**
 * Multi-library registry stored in chrome.storage.local.
 * Mirrors the web app's libraryRegistry pattern (origin + JWT session).
 */

export const STORAGE_KEY = "library_extension_registry_v1";

/**
 * @typedef {Object} LibrarySession
 * @property {string} access_token
 * @property {string} refresh_token
 * @property {string} role
 * @property {string} username
 * @property {string|null} email
 */

/**
 * @typedef {Object} ConnectedLibrary
 * @property {string} id
 * @property {string} origin
 * @property {string} name
 * @property {string} email
 * @property {number} lastUsedAt
 * @property {LibrarySession} session
 */

/**
 * @typedef {Object} Registry
 * @property {ConnectedLibrary[]} libraries
 */

/** @returns {Registry} */
function emptyRegistry() {
  return { libraries: [] };
}

/** @returns {Promise<Registry>} */
export async function loadRegistry() {
  const data = await chrome.storage.local.get(STORAGE_KEY);
  const reg = data[STORAGE_KEY];
  if (!reg || !Array.isArray(reg.libraries)) return emptyRegistry();
  return { libraries: reg.libraries };
}

/** @param {Registry} reg */
export async function saveRegistry(reg) {
  await chrome.storage.local.set({ [STORAGE_KEY]: reg });
}

/** @param {string} origin */
export function normalizeOrigin(origin) {
  let o = (origin || "").trim();
  if (!o) return "";
  if (!/^https?:\/\//i.test(o)) o = `https://${o}`;
  try {
    const u = new URL(o);
    return `${u.protocol}//${u.host}`.replace(/\/+$/, "");
  } catch {
    return "";
  }
}

/**
 * @param {Omit<ConnectedLibrary, "id" | "lastUsedAt"> & { id?: string }} entry
 * @returns {Promise<ConnectedLibrary>}
 */
export async function upsertLibrary(entry) {
  const origin = normalizeOrigin(entry.origin);
  if (!origin) throw new Error("Invalid library URL");
  const reg = await loadRegistry();
  const email = (entry.email || "").trim().toLowerCase();
  const existingIdx = reg.libraries.findIndex(
    (l) => l.origin === origin && l.email === email
  );
  /** @type {ConnectedLibrary} */
  const row = {
    id: entry.id || (existingIdx >= 0 ? reg.libraries[existingIdx].id : crypto.randomUUID()),
    origin,
    name: entry.name || "Library",
    email,
    lastUsedAt: Date.now(),
    session: entry.session,
  };
  if (existingIdx >= 0) reg.libraries[existingIdx] = row;
  else reg.libraries.push(row);
  await saveRegistry(reg);
  return row;
}

/** @param {string} id */
export async function removeLibrary(id) {
  const reg = await loadRegistry();
  reg.libraries = reg.libraries.filter((l) => l.id !== id);
  await saveRegistry(reg);
}

/** @param {string} id */
export async function getLibrary(id) {
  const reg = await loadRegistry();
  return reg.libraries.find((l) => l.id === id) || null;
}

/** @returns {Promise<ConnectedLibrary[]>} */
export async function listLibraries() {
  const reg = await loadRegistry();
  return [...reg.libraries].sort((a, b) => b.lastUsedAt - a.lastUsedAt);
}

/**
 * @param {string} id
 * @param {LibrarySession} session
 */
export async function updateSession(id, session) {
  const reg = await loadRegistry();
  const lib = reg.libraries.find((l) => l.id === id);
  if (!lib) return null;
  lib.session = session;
  lib.lastUsedAt = Date.now();
  await saveRegistry(reg);
  return lib;
}
