/**
 * Library Site HTTP client for the extension service worker / options page.
 * Uses fetch + optional host permissions (no CORS needed from SW with host access).
 */

import { normalizeOrigin, updateSession } from "./storage.js";

/**
 * @param {string} origin
 * @param {string} path
 * @param {RequestInit & { token?: string }} [opts]
 */
async function apiFetch(origin, path, opts = {}) {
  const base = normalizeOrigin(origin);
  if (!base) throw new Error("Invalid library origin");
  const url = `${base}${path.startsWith("/") ? path : `/${path}`}`;
  /** @type {Record<string, string>} */
  const headers = {
    Accept: "application/json",
    ...(opts.headers || {}),
  };
  if (opts.token) headers.Authorization = `Bearer ${opts.token}`;
  if (opts.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const { token: _t, ...rest } = opts;
  const res = await fetch(url, { ...rest, headers });
  return res;
}

/**
 * @param {Response} res
 */
async function readError(res) {
  try {
    const data = await res.json();
    if (typeof data?.detail === "string") return data.detail;
    if (Array.isArray(data?.detail)) {
      return data.detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
    }
    return JSON.stringify(data);
  } catch {
    return res.statusText || `HTTP ${res.status}`;
  }
}

/**
 * @param {string} origin
 * @param {{ email: string, password: string }} creds
 */
export async function login(origin, creds) {
  const res = await apiFetch(origin, "/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      email: creds.email.trim(),
      password: creds.password,
    }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/**
 * @param {string} origin
 * @param {string} refreshToken
 */
export async function refresh(origin, refreshToken) {
  const res = await apiFetch(origin, "/api/auth/refresh", {
    method: "POST",
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/**
 * @param {string} origin
 * @param {string} accessToken
 */
export async function fetchMe(origin, accessToken) {
  const res = await apiFetch(origin, "/api/auth/me", { token: accessToken });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/**
 * @param {string} origin
 * @param {string} accessToken
 */
export async function fetchLibraryInfo(origin, accessToken) {
  const res = await apiFetch(origin, "/api/libraries/me", { token: accessToken });
  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/**
 * Ensure we have a valid access token; refresh if needed.
 * @param {import("./storage.js").ConnectedLibrary} library
 * @returns {Promise<{ library: import("./storage.js").ConnectedLibrary, accessToken: string }>}
 */
export async function ensureAccessToken(library) {
  let access = library.session?.access_token;
  const refreshToken = library.session?.refresh_token;
  if (!refreshToken) {
    throw Object.assign(
      new Error("Session expired — reconnect this library in extension settings."),
      { code: "AUTH_REQUIRED" }
    );
  }

  const exp = parseJwtExpMs(access);
  const needsRefresh = !access || exp == null || exp < Date.now() + 60_000;

  if (needsRefresh) {
    try {
      const tokens = await refresh(library.origin, refreshToken);
      const session = {
        access_token: tokens.access_token,
        refresh_token: tokens.refresh_token,
        role: tokens.role,
        username: tokens.username,
        email: tokens.email ?? library.session.email,
      };
      const updated = await updateSession(library.id, session);
      return { library: updated || { ...library, session }, accessToken: session.access_token };
    } catch {
      throw Object.assign(
        new Error("Login expired — open extension settings to reconnect."),
        { code: "AUTH_REQUIRED" }
      );
    }
  }

  return { library, accessToken: access };
}

/**
 * @param {string|null|undefined} token
 * @returns {number|null}
 */
function parseJwtExpMs(token) {
  if (!token) return null;
  try {
    const payload = JSON.parse(
      atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))
    );
    return typeof payload.exp === "number" ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}

/**
 * Create a download request (same API as the web app).
 * @param {import("./storage.js").ConnectedLibrary} library
 * @param {object} body
 */
export async function createRequest(library, body) {
  let { library: lib, accessToken } = await ensureAccessToken(library);

  let res = await apiFetch(lib.origin, "/api/requests", {
    method: "POST",
    token: accessToken,
    body: JSON.stringify(body),
  });

  if (res.status === 401) {
    // Force refresh once
    try {
      const tokens = await refresh(lib.origin, lib.session.refresh_token);
      const session = {
        access_token: tokens.access_token,
        refresh_token: tokens.refresh_token,
        role: tokens.role,
        username: tokens.username,
        email: tokens.email ?? lib.session.email,
      };
      const updated = await updateSession(lib.id, session);
      lib = updated || { ...lib, session };
      accessToken = session.access_token;
      res = await apiFetch(lib.origin, "/api/requests", {
        method: "POST",
        token: accessToken,
        body: JSON.stringify(body),
      });
    } catch {
      throw Object.assign(
        new Error("Login expired — open extension settings to reconnect."),
        { code: "AUTH_REQUIRED" }
      );
    }
  }

  if (!res.ok) throw new Error(await readError(res));
  return res.json();
}

/**
 * Request host access for a library origin (MV3 optional_host_permissions).
 * @param {string} origin
 */
export async function ensureHostPermission(origin) {
  const base = normalizeOrigin(origin);
  if (!base) throw new Error("Invalid library URL");
  const pattern = `${base}/*`;
  const have = await chrome.permissions.contains({ origins: [pattern] });
  if (have) return true;
  const granted = await chrome.permissions.request({ origins: [pattern] });
  if (!granted) {
    throw new Error("Permission denied — allow access to your library URL when prompted.");
  }
  return true;
}
