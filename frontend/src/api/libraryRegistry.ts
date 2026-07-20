/**
 * Device-local remembered Library servers + per-origin sessions.
 * Cleared if the app/cache is wiped — rejoin via invite to restore.
 */
import { getApiOrigin, getStoredInstanceUrl, setInstanceUrl, isNativeApp } from "./instanceUrl";
import { applyApiBaseUrl } from "./client";

export const LIBRARY_REGISTRY_KEY = "library_account_registry_v1";
export const ACTIVE_LIBRARY_ORIGIN_KEY = "library_active_origin";

export interface RememberedLibrary {
  origin: string;
  name: string;
  coverUrl: string | null;
  email: string;
  lastUsedAt: number;
}

export interface LibrarySession {
  access_token: string;
  refresh_token: string;
  role: string;
  username: string;
  email: string | null;
  must_change_password: boolean;
  must_set_email?: boolean;
}

export interface LibraryRegistry {
  email: string | null;
  libraries: RememberedLibrary[];
  sessions: Record<string, LibrarySession>;
}

function emptyRegistry(): LibraryRegistry {
  return { email: null, libraries: [], sessions: {} };
}

export function loadRegistry(): LibraryRegistry {
  try {
    const raw = localStorage.getItem(LIBRARY_REGISTRY_KEY);
    if (!raw) return emptyRegistry();
    const parsed = JSON.parse(raw) as LibraryRegistry;
    if (!parsed || !Array.isArray(parsed.libraries)) return emptyRegistry();
    return {
      email: parsed.email ?? null,
      libraries: parsed.libraries,
      sessions: parsed.sessions && typeof parsed.sessions === "object" ? parsed.sessions : {},
    };
  } catch {
    return emptyRegistry();
  }
}

function saveRegistry(reg: LibraryRegistry): void {
  localStorage.setItem(LIBRARY_REGISTRY_KEY, JSON.stringify(reg));
}

export function listRememberedLibraries(email?: string | null): RememberedLibrary[] {
  const reg = loadRegistry();
  // Show all device-saved libraries unless a specific email filter is passed.
  const em = (email ?? "").toLowerCase();
  const list = em
    ? reg.libraries.filter((l) => l.email.toLowerCase() === em)
    : reg.libraries;
  return [...list].sort((a, b) => b.lastUsedAt - a.lastUsedAt);
}

export function upsertRememberedLibrary(entry: {
  origin: string;
  name: string;
  coverUrl?: string | null;
  email: string;
}): RememberedLibrary {
  const origin = entry.origin.replace(/\/+$/, "");
  const email = entry.email.trim().toLowerCase();
  const reg = loadRegistry();
  reg.email = email;
  const cover =
    entry.coverUrl && entry.coverUrl.startsWith("/")
      ? `${origin}${entry.coverUrl}`
      : entry.coverUrl || null;
  const existing = reg.libraries.findIndex((l) => l.origin === origin && l.email === email);
  const row: RememberedLibrary = {
    origin,
    name: entry.name || "Library",
    coverUrl: cover,
    email,
    lastUsedAt: Date.now(),
  };
  if (existing >= 0) reg.libraries[existing] = row;
  else reg.libraries.push(row);
  saveRegistry(reg);
  return row;
}

export function saveSessionForOrigin(origin: string, session: LibrarySession): void {
  const key = origin.replace(/\/+$/, "");
  const reg = loadRegistry();
  reg.sessions[key] = session;
  if (session.email) reg.email = session.email.toLowerCase();
  saveRegistry(reg);
  localStorage.setItem("access_token", session.access_token);
  localStorage.setItem("refresh_token", session.refresh_token);
  localStorage.setItem("user_role", session.role);
  localStorage.setItem("username", session.username);
  if (session.email) localStorage.setItem("user_email", session.email);
  else localStorage.removeItem("user_email");
  localStorage.setItem("must_change_password", String(session.must_change_password));
  localStorage.setItem("must_set_email", String(!!session.must_set_email));
  localStorage.setItem(ACTIVE_LIBRARY_ORIGIN_KEY, key);
}

export function clearActiveSession(): void {
  const origin = (getStoredInstanceUrl() || getApiOrigin() || "").replace(/\/+$/, "");
  const reg = loadRegistry();
  if (origin && reg.sessions[origin]) {
    delete reg.sessions[origin];
    saveRegistry(reg);
  }
  localStorage.removeItem("access_token");
  localStorage.removeItem("refresh_token");
  localStorage.removeItem("user_role");
  localStorage.removeItem("username");
  localStorage.removeItem("user_email");
  localStorage.removeItem("must_change_password");
  localStorage.removeItem("must_set_email");
}

export function switchToLibrary(origin: string): LibrarySession | null {
  const key = origin.replace(/\/+$/, "");
  setInstanceUrl(key);
  applyApiBaseUrl();
  localStorage.setItem(ACTIVE_LIBRARY_ORIGIN_KEY, key);
  const reg = loadRegistry();
  const session = reg.sessions[key] || null;
  if (session) {
    localStorage.setItem("access_token", session.access_token);
    localStorage.setItem("refresh_token", session.refresh_token);
    localStorage.setItem("user_role", session.role);
    localStorage.setItem("username", session.username);
    if (session.email) localStorage.setItem("user_email", session.email);
    else localStorage.removeItem("user_email");
    localStorage.setItem("must_change_password", String(session.must_change_password));
    localStorage.setItem("must_set_email", String(!!session.must_set_email));
  } else {
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    localStorage.removeItem("user_role");
    localStorage.removeItem("username");
    localStorage.removeItem("user_email");
    localStorage.removeItem("must_change_password");
    localStorage.removeItem("must_set_email");
  }
  return session;
}

export function removeRememberedLibrary(origin: string, email?: string): void {
  const key = origin.replace(/\/+$/, "");
  const reg = loadRegistry();
  reg.libraries = reg.libraries.filter(
    (l) => !(l.origin === key && (!email || l.email === email.toLowerCase()))
  );
  delete reg.sessions[key];
  saveRegistry(reg);
}

export function currentOrigin(): string {
  if (isNativeApp()) {
    return (getStoredInstanceUrl() || "").replace(/\/+$/, "");
  }
  try {
    const active = localStorage.getItem(ACTIVE_LIBRARY_ORIGIN_KEY);
    if (active) return active.replace(/\/+$/, "");
    return window.location.origin.replace(/\/+$/, "");
  } catch {
    return "";
  }
}

export function getSessionForOrigin(origin: string): LibrarySession | null {
  const key = origin.replace(/\/+$/, "");
  return loadRegistry().sessions[key] || null;
}
