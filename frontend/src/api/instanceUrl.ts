/**
 * Per-device Library server URL (Android APK).
 *
 * Web/PWA keeps same-origin relative `/api`. The Capacitor app ships a bundled
 * SPA and stores the user's HTTPS library URL in localStorage.
 */
import { Capacitor } from "@capacitor/core";

export const INSTANCE_URL_KEY = "library_instance_url";
export const INSTANCE_URL_CHANGED_EVENT = "library-instance-url-changed";

export function isNativeApp(): boolean {
  try {
    return Capacitor.isNativePlatform();
  } catch {
    return false;
  }
}

/** Normalize user input to an origin like https://library.example.com (no trailing slash). */
export function normalizeInstanceUrl(input: string): string | null {
  const raw = (input || "").trim();
  if (!raw) return null;
  let candidate = raw;
  if (!/^https?:\/\//i.test(candidate)) {
    candidate = `https://${candidate}`;
  }
  try {
    const u = new URL(candidate);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    if (!u.hostname) return null;
    // Drop path/query/hash — API lives at origin/api
    return `${u.protocol}//${u.host}`;
  } catch {
    return null;
  }
}

export function getStoredInstanceUrl(): string | null {
  try {
    const raw = localStorage.getItem(INSTANCE_URL_KEY);
    return raw ? normalizeInstanceUrl(raw) : null;
  } catch {
    return null;
  }
}

/**
 * Origin used for absolute API/media URLs.
 * Native: stored library URL. Web: window.location.origin.
 */
export function getApiOrigin(): string {
  if (isNativeApp()) {
    return getStoredInstanceUrl() || "";
  }
  try {
    return window.location.origin;
  } catch {
    return "";
  }
}

/** Axios baseURL — `/api` on web, `https://host/api` on native. */
export function getApiBaseUrl(): string {
  if (!isNativeApp()) return "/api";
  const origin = getStoredInstanceUrl();
  return origin ? `${origin}/api` : "/api";
}

/** Turn `/api/...` (or any root-relative path) into an absolute URL when needed. */
export function toAbsoluteUrl(path: string): string {
  if (!path) return path;
  if (/^(https?:|blob:|data:)/i.test(path)) return path;
  const origin = getApiOrigin();
  if (!origin) return path;
  return `${origin}${path.startsWith("/") ? path : `/${path}`}`;
}

export function setInstanceUrl(input: string): string {
  const normalized = normalizeInstanceUrl(input);
  if (!normalized) {
    throw new Error("Enter a valid URL, e.g. https://library.example.com");
  }
  localStorage.setItem(INSTANCE_URL_KEY, normalized);
  try {
    window.dispatchEvent(
      new CustomEvent(INSTANCE_URL_CHANGED_EVENT, { detail: normalized })
    );
  } catch {
    // ignore
  }
  return normalized;
}

export function clearInstanceUrl(): void {
  localStorage.removeItem(INSTANCE_URL_KEY);
  try {
    window.dispatchEvent(new CustomEvent(INSTANCE_URL_CHANGED_EVENT, { detail: null }));
  } catch {
    // ignore
  }
}

/** True when the native app still needs a server URL before API calls can work. */
export function needsInstanceUrl(): boolean {
  return isNativeApp() && !getStoredInstanceUrl();
}
