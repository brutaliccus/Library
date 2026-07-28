import api from "../api/client";
import { isNativeApp } from "../api/instanceUrl";
import { AppUpdateNative } from "../media/appUpdateNative";
import { APK_RELEASE_KEY_STORAGE, APK_VERSION_CODE_STORAGE } from "./appUpdateConfig";
import { getApkReleaseKey } from "./appUpdateAlertState";

export interface AndroidAppUpdateInfo {
  fileName: string;
  sizeBytes: number | null;
  downloadUrl: string;
  releaseUrl: string;
  githubRepo: string;
  tagName: string;
  versionName: string | null;
  versionCode: number | null;
  publishedAt: string;
  releaseKey: string;
  /** Server floor — installs below this must update. */
  minVersionCode?: number | null;
  /** When true, any newer GitHub APK is a hard (non-dismissible) update. */
  forceUpdate?: boolean;
}

export function getLastInstalledReleaseKey(): string | null {
  try {
    return localStorage.getItem(APK_RELEASE_KEY_STORAGE);
  } catch {
    return null;
  }
}

export function markApkInstalled(releaseKey: string | null) {
  if (!releaseKey) return;
  try {
    localStorage.setItem(APK_RELEASE_KEY_STORAGE, releaseKey);
  } catch {
    /* ignore */
  }
}

export function isUpdateAvailable(
  installedVersionCode: number,
  remote: Pick<AndroidAppUpdateInfo, "versionCode" | "releaseKey" | "publishedAt" | "minVersionCode">,
  lastInstalledReleaseKey: string | null
): boolean {
  const minCode = remote.minVersionCode;
  if (typeof minCode === "number" && minCode > 0 && installedVersionCode < minCode) {
    return true;
  }
  if (remote.versionCode != null && remote.versionCode > installedVersionCode) {
    return true;
  }
  const key = getApkReleaseKey(remote);
  if (!key) return false;
  if (lastInstalledReleaseKey === key) return false;
  if (lastInstalledReleaseKey == null) {
    // First check after install: only prompt when versionCode clearly newer.
    return remote.versionCode != null && remote.versionCode > installedVersionCode;
  }
  return true;
}

/** Hard gate: below min version, or forceUpdate + newer GitHub build. */
export function isUpdateRequired(
  installedVersionCode: number,
  remote: Pick<AndroidAppUpdateInfo, "versionCode" | "minVersionCode" | "forceUpdate">
): boolean {
  const minCode = remote.minVersionCode;
  if (typeof minCode === "number" && minCode > 0 && installedVersionCode < minCode) {
    return true;
  }
  if (!remote.forceUpdate) return false;
  return remote.versionCode != null && remote.versionCode > installedVersionCode;
}

export async function fetchAndroidAppUpdateInfo(force = false): Promise<AndroidAppUpdateInfo> {
  const { data } = await api.get<AndroidAppUpdateInfo>("/mobile/android-update", {
    params: force ? { force: true } : undefined,
  });
  return data;
}

export async function getInstalledAndroidVersion(): Promise<{
  versionCode: number;
  versionName: string;
}> {
  const timeoutMs = 5_000;
  return Promise.race([
    AppUpdateNative.getInstalledVersion(),
    new Promise<never>((_, reject) => {
      window.setTimeout(() => reject(new Error("Version check timed out")), timeoutMs);
    }),
  ]);
}

/**
 * Clear WebView HTTP/asset cache once per installed versionCode so bundled SPA
 * and any cached API shells are not stuck after an APK update.
 */
export async function bustWebViewCacheIfVersionChanged(): Promise<void> {
  if (!isNativeApp()) return;
  try {
    const { versionCode } = await getInstalledAndroidVersion();
    const key = String(versionCode);
    let prev: string | null = null;
    try {
      prev = localStorage.getItem(APK_VERSION_CODE_STORAGE);
    } catch {
      prev = null;
    }
    if (prev === key) return;
    try {
      await AppUpdateNative.clearWebViewCache();
    } catch {
      /* plugin missing on older APKs */
    }
    try {
      localStorage.setItem(APK_VERSION_CODE_STORAGE, key);
    } catch {
      /* ignore */
    }
  } catch {
    /* version check failed */
  }
}

export async function downloadAndInstallAndroidUpdate(
  info: AndroidAppUpdateInfo,
  onProgress?: (percent: number) => void
): Promise<void> {
  let handle: { remove: () => Promise<void> } | undefined;
  if (onProgress) {
    handle = await AppUpdateNative.addListener("downloadProgress", (e) => {
      onProgress(e.percent);
    });
  }

  try {
    await AppUpdateNative.downloadAndInstall({ url: info.downloadUrl });
    markApkInstalled(getApkReleaseKey(info));
  } finally {
    await handle?.remove();
  }
}

/** Open the APK URL in the system browser (fallback / non-native). */
export function openApkDownloadInBrowser(info: AndroidAppUpdateInfo): void {
  window.open(info.downloadUrl, "_blank", "noopener,noreferrer");
  markApkInstalled(getApkReleaseKey(info));
}

export async function installAndroidAppUpdate(
  info?: AndroidAppUpdateInfo,
  onProgress?: (percent: number) => void
): Promise<AndroidAppUpdateInfo> {
  const remote = info ?? (await fetchAndroidAppUpdateInfo(true));
  if (isNativeApp()) {
    await downloadAndInstallAndroidUpdate(remote, onProgress);
  } else {
    openApkDownloadInBrowser(remote);
  }
  return remote;
}
