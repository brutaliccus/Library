import api from "../api/client";
import { isNativeApp } from "../api/instanceUrl";
import { AppUpdateNative } from "../media/appUpdateNative";
import { APK_RELEASE_KEY_STORAGE } from "./appUpdateConfig";
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
  remote: Pick<AndroidAppUpdateInfo, "versionCode" | "releaseKey" | "publishedAt">,
  lastInstalledReleaseKey: string | null
): boolean {
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
