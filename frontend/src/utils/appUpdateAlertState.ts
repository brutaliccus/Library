import type { AndroidAppUpdateInfo } from "./appUpdate";

const DISMISSED_KEY = "library-apk-update-alert-dismissed";
const SHOWN_KEY = "library-apk-update-alert-shown";

export function getApkReleaseKey(
  remote: Pick<AndroidAppUpdateInfo, "releaseKey" | "publishedAt" | "versionCode">
): string {
  if (remote.releaseKey) return remote.releaseKey;
  if (remote.publishedAt) return remote.publishedAt;
  if (remote.versionCode != null) return `vc:${remote.versionCode}`;
  return "";
}

function readKey(storageKey: string): string | null {
  try {
    const v = localStorage.getItem(storageKey);
    return v && v.length > 0 ? v : null;
  } catch {
    return null;
  }
}

function writeKey(storageKey: string, value: string | null): void {
  try {
    if (!value) localStorage.removeItem(storageKey);
    else localStorage.setItem(storageKey, value);
  } catch {
    /* ignore */
  }
}

export function isApkUpdateAlertDismissed(releaseKey: string): boolean {
  return readKey(DISMISSED_KEY) === releaseKey;
}

export function isApkUpdateAlertShown(releaseKey: string): boolean {
  return readKey(SHOWN_KEY) === releaseKey;
}

export function markApkUpdateAlertShown(releaseKey: string): void {
  writeKey(SHOWN_KEY, releaseKey);
}

export function markApkUpdateAlertDismissed(releaseKey: string): void {
  writeKey(DISMISSED_KEY, releaseKey);
}
