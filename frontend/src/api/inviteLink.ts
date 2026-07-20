/**
 * Library invite links carry both the invite code and the server origin so
 * Android APK users (and anyone pasting a share) don't need to type the URL.
 *
 * Share format (HTTPS):  https://library.example.com/join/7Q2MKX4RB3ZD
 * App deep link:         library://join/7Q2MKX4RB3ZD?origin=https://library.example.com
 * Also accepts:          7Q2MKX4RB3ZD@https://library.example.com
 *                        bare codes (legacy)
 */
import {
  getApiOrigin,
  getStoredInstanceUrl,
  isNativeApp,
  normalizeInstanceUrl,
  setInstanceUrl,
} from "./instanceUrl";
import { applyApiBaseUrl } from "./client";

export const PENDING_INVITE_KEY = "library_pending_invite_code";
export const APP_INVITE_SCHEME = "library";
export const ANDROID_PACKAGE_ID = "com.freiverse.library";

const CODE_RE = /^[A-Z0-9]{6,24}$/;

export interface ParsedInvite {
  code: string;
  /** Server origin when the paste included a URL; null for bare codes. */
  origin: string | null;
}

export function normalizeInviteCode(raw: string): string | null {
  const code = (raw || "").trim().toUpperCase().replace(/[^A-Z0-9]/g, "");
  return CODE_RE.test(code) ? code : null;
}

function currentWebOrigin(): string {
  try {
    return typeof window !== "undefined" ? window.location.origin : "";
  } catch {
    return "";
  }
}

function isShareableOrigin(origin: string): boolean {
  try {
    const host = new URL(origin).hostname.toLowerCase();
    if (!host) return false;
    if (host === "localhost" || host === "127.0.0.1") return false;
    if (host.endsWith(".local")) return false;
    return true;
  } catch {
    return false;
  }
}

/**
 * Best public origin for invite links.
 * Prefer an explicit origin, then the stored server URL (Android), then the
 * browser origin — skipping localhost so the Android WebView never produces
 * https://localhost/join/CODE (or a bare code fallback).
 */
function resolveShareOrigin(origin?: string | null): string {
  const candidates = [
    origin && normalizeInstanceUrl(origin),
    getStoredInstanceUrl(),
    !isNativeApp() ? currentWebOrigin() : null,
    getApiOrigin(),
    currentWebOrigin(),
  ];
  for (const c of candidates) {
    if (c && isShareableOrigin(c)) return c.replace(/\/+$/, "");
  }
  for (const c of candidates) {
    if (c) return c.replace(/\/+$/, "");
  }
  return "";
}

/** Build a shareable HTTPS invite URL for the current Library server. */
export function buildInviteLink(code: string, origin?: string | null): string {
  const normalized = normalizeInviteCode(code);
  if (!normalized) return (code || "").trim();
  const base = resolveShareOrigin(origin);
  if (!base) return normalized;
  return `${base}/join/${normalized}`;
}

/**
 * Prefer the server-built invite link (from APP_URL); fall back to client build.
 */
export function resolveInviteShareUrl(
  inviteLink: string | null | undefined,
  inviteCode: string | null | undefined,
  origin?: string | null
): string {
  const fromServer = (inviteLink || "").trim();
  if (fromServer && /^https?:\/\//i.test(fromServer) && /\/join\//i.test(fromServer)) {
    return fromServer.replace(/\/+$/, "");
  }
  if (inviteCode) return buildInviteLink(inviteCode, origin);
  return fromServer || "";
}

/** Custom-scheme link that opens the Android app when installed. */
export function buildAppInviteLink(code: string, origin?: string | null): string {
  const normalized = normalizeInviteCode(code);
  if (!normalized) return "";
  const base = resolveShareOrigin(origin);
  const q = base ? `?origin=${encodeURIComponent(base)}` : "";
  return `${APP_INVITE_SCHEME}://join/${normalized}${q}`;
}

/**
 * Android Chrome intent URL: opens the app if installed, otherwise falls back
 * to the HTTPS join page with ?web=1 (skips another app-open attempt).
 */
export function buildAndroidIntentInviteLink(
  code: string,
  origin?: string | null
): string {
  const normalized = normalizeInviteCode(code);
  if (!normalized) return "";
  const base = resolveShareOrigin(origin);
  const fallback = `${base.replace(/\/+$/, "")}/join/${normalized}?web=1`;
  const path = `join/${normalized}${base ? `?origin=${encodeURIComponent(base)}` : ""}`;
  return (
    `intent://${path}` +
    `#Intent;scheme=${APP_INVITE_SCHEME};package=${ANDROID_PACKAGE_ID};` +
    `S.browser_fallback_url=${encodeURIComponent(fallback)};end`
  );
}

function parseCustomSchemeInvite(text: string): ParsedInvite | null {
  // library://join/CODE?origin=…  or  com.freiverse.library://join/CODE?origin=…
  const m = text.match(
    /^(?:library|com\.freiverse\.library):\/\/(?:join\/|invite\/)?([A-Za-z0-9]{6,24})\/?(?:\?([^#]*))?$/i
  );
  if (m) {
    const code = normalizeInviteCode(m[1]);
    if (!code) return null;
    const params = new URLSearchParams(m[2] || "");
    const origin = normalizeInstanceUrl(params.get("origin") || "");
    return { code, origin };
  }

  try {
    const u = new URL(text);
    if (u.protocol !== "library:" && u.protocol !== "com.freiverse.library:") {
      return null;
    }
    const host = (u.hostname || "").toLowerCase();
    const pathPart = (u.pathname || "").replace(/^\/+/, "");
    let codeRaw = "";
    if (host === "join" || host === "invite") {
      codeRaw = pathPart.split("/")[0] || "";
    } else if (CODE_RE.test(host.toUpperCase())) {
      codeRaw = host;
    } else {
      codeRaw = pathPart.split("/").filter(Boolean).pop() || "";
    }
    const code = normalizeInviteCode(codeRaw);
    if (!code) return null;
    const origin = normalizeInstanceUrl(u.searchParams.get("origin") || "");
    return { code, origin };
  } catch {
    return null;
  }
}

/**
 * Parse a pasted invite (full link, app scheme, code@url, or bare code).
 * Returns null if nothing invite-like was found.
 */
export function parseInviteInput(raw: string): ParsedInvite | null {
  const text = (raw || "").trim();
  if (!text) return null;

  if (/^(library|com\.freiverse\.library):/i.test(text)) {
    return parseCustomSchemeInvite(text);
  }

  // code@https://host
  const at = text.match(/^([A-Za-z0-9]{6,24})@(https?:\/\/\S+)$/i);
  if (at) {
    const code = normalizeInviteCode(at[1]);
    const origin = normalizeInstanceUrl(at[2]);
    if (code && origin) return { code, origin };
  }

  // Full URL with /join/CODE or ?invite= / ?code=
  try {
    const withProto = /^https?:\/\//i.test(text) ? text : `https://${text}`;
    const u = new URL(withProto);
    const pathMatch = u.pathname.match(/\/join\/([A-Za-z0-9]{6,24})\/?$/i);
    const q =
      u.searchParams.get("invite") ||
      u.searchParams.get("code") ||
      u.searchParams.get("invite_code");
    const code = normalizeInviteCode(pathMatch?.[1] || q || "");
    if (code) {
      return { code, origin: `${u.protocol}//${u.host}` };
    }
  } catch {
    // not a URL
  }

  const bare = normalizeInviteCode(text);
  if (bare) return { code: bare, origin: null };

  return null;
}

export function stashPendingInvite(code: string): void {
  const normalized = normalizeInviteCode(code);
  if (!normalized) return;
  try {
    localStorage.setItem(PENDING_INVITE_KEY, normalized);
  } catch {
    // ignore
  }
}

export function peekPendingInvite(): string | null {
  try {
    return normalizeInviteCode(localStorage.getItem(PENDING_INVITE_KEY) || "");
  } catch {
    return null;
  }
}

/** Read and clear a pending invite (e.g. after successful signup/join). */
export function takePendingInvite(): string | null {
  const code = peekPendingInvite();
  try {
    localStorage.removeItem(PENDING_INVITE_KEY);
  } catch {
    // ignore
  }
  return code;
}

function isUsableInviteOrigin(origin: string): boolean {
  try {
    const host = new URL(origin).hostname.toLowerCase();
    if (!host || host === "localhost" || host === "127.0.0.1") return false;
    if (host.endsWith(".local")) return false;
    return true;
  } catch {
    return false;
  }
}

/**
 * Apply a pasted invite: set server URL (native), stash code, refresh API base.
 * Returns the parsed invite or null.
 */
export function applyInvitePaste(raw: string): ParsedInvite | null {
  const parsed = parseInviteInput(raw);
  if (!parsed) return null;
  stashPendingInvite(parsed.code);
  if (parsed.origin && isNativeApp() && isUsableInviteOrigin(parsed.origin)) {
    try {
      setInstanceUrl(parsed.origin);
      applyApiBaseUrl();
    } catch {
      // invalid origin — still keep the code
    }
  }
  return parsed;
}
