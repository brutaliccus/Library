/**
 * Capacitor deep-link handling for library:// invite URLs (and HTTPS /join when
 * the OS delivers them to the app).
 */
import { applyInvitePaste, normalizeInviteCode, parseInviteInput } from "./api/inviteLink";
import { isNativeApp } from "./api/instanceUrl";

export const DEEPLINK_NAV_EVENT = "library-deeplink-navigate";

export function joinPathForInvite(code: string): string {
  const normalized = normalizeInviteCode(code);
  return normalized ? `/join/${normalized}` : "/join";
}

/** Apply invite from a deep-link URL; returns the in-app path to open. */
export function handleDeepLinkUrl(url: string): string | null {
  const parsed = applyInvitePaste(url) || parseInviteInput(url);
  if (!parsed?.code) return null;
  return joinPathForInvite(parsed.code);
}

function emitNavigate(path: string): void {
  try {
    window.dispatchEvent(new CustomEvent(DEEPLINK_NAV_EVENT, { detail: { path } }));
  } catch {
    // ignore
  }
}

/**
 * Consume cold-start launch URL before React mounts (so instance URL is set
 * before AuthProvider's first API check).
 */
export async function consumeLaunchDeepLink(): Promise<string | null> {
  if (!isNativeApp()) return null;
  try {
    const { App } = await import("@capacitor/app");
    const launch = await App.getLaunchUrl();
    if (!launch?.url) return null;
    return handleDeepLinkUrl(launch.url);
  } catch {
    return null;
  }
}

/** Listen for invites while the app is already open. */
export async function registerDeepLinkListener(): Promise<() => void> {
  if (!isNativeApp()) return () => {};
  try {
    const { App } = await import("@capacitor/app");
    const sub = await App.addListener("appUrlOpen", ({ url }) => {
      const path = handleDeepLinkUrl(url);
      if (path) emitNavigate(path);
    });
    return () => {
      void sub.remove();
    };
  } catch {
    return () => {};
  }
}
