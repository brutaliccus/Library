import { isNativeApp } from "../api/instanceUrl";
import {
  fetchAndroidAppUpdateInfo,
  getInstalledAndroidVersion,
  getLastInstalledReleaseKey,
  isUpdateAvailable,
  type AndroidAppUpdateInfo,
} from "./appUpdate";
import { getApkReleaseKey } from "./appUpdateAlertState";

export type AppUpdateCheckResult = {
  releaseKey: string;
  remote: AndroidAppUpdateInfo;
  versionLabel: string;
};

/** Returns release info when a newer APK is on GitHub; null if up to date or unavailable. */
export async function checkAppUpdateRelease(): Promise<AppUpdateCheckResult | null> {
  if (!isNativeApp()) return null;

  const remote = await fetchAndroidAppUpdateInfo();
  const releaseKey = getApkReleaseKey(remote);
  if (!releaseKey) return null;

  const installed = await getInstalledAndroidVersion();
  const lastInstalled = getLastInstalledReleaseKey();
  const updateReady = isUpdateAvailable(installed.versionCode, remote, lastInstalled);
  if (!updateReady) return null;

  const versionLabel = remote.versionName?.trim() || remote.tagName || "new version";
  return { releaseKey, remote, versionLabel };
}
