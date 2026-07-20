import { useCallback, useEffect, useRef, useState } from "react";
import { Capacitor } from "@capacitor/core";
import { LocalNotifications } from "@capacitor/local-notifications";
import { isNativeApp } from "../api/instanceUrl";
import { AppUpdateNative } from "../media/appUpdateNative";
import {
  isApkUpdateAlertDismissed,
  isApkUpdateAlertShown,
  markApkUpdateAlertDismissed,
  markApkUpdateAlertShown,
} from "../utils/appUpdateAlertState";
import { checkAppUpdateRelease, type AppUpdateCheckResult } from "../utils/appUpdateCheck";
import { installAndroidAppUpdate } from "../utils/appUpdate";

const CHECK_INTERVAL_MS = 15 * 60 * 1000;
const RETRY_DELAYS_MS = [0, 3_000, 15_000] as const;

async function ensureNotificationPermission(): Promise<boolean> {
  if (!Capacitor.isNativePlatform()) return false;
  const check = await LocalNotifications.checkPermissions();
  if (check.display === "granted") return true;
  const perm = await LocalNotifications.requestPermissions();
  return perm.display === "granted";
}

async function tryShowSystemUpdateAlert(result: AppUpdateCheckResult): Promise<boolean> {
  if (isApkUpdateAlertShown(result.releaseKey)) return false;

  const ok = await ensureNotificationPermission();
  if (!ok) return false;

  await AppUpdateNative.showUpdateAvailable({
    title: "Library update available",
    body: `Version ${result.versionLabel} is ready to install.`,
    releaseKey: result.releaseKey,
    downloadUrl: result.remote.downloadUrl,
  });
  markApkUpdateAlertShown(result.releaseKey);
  return true;
}

export function useAppUpdateNotification(enabled: boolean) {
  const checkingRef = useRef(false);
  const downloadingRef = useRef(false);
  const pendingRef = useRef<AppUpdateCheckResult | null>(null);
  const [pendingUpdate, setPendingUpdate] = useState<AppUpdateCheckResult | null>(null);
  const [downloading, setDownloading] = useState(false);

  const setPending = useCallback((value: AppUpdateCheckResult | null) => {
    pendingRef.current = value;
    setPendingUpdate(value);
  }, []);

  const runCheck = useCallback(async () => {
    if (!enabled || !isNativeApp()) {
      setPending(null);
      return;
    }
    if (checkingRef.current) return;

    checkingRef.current = true;
    try {
      const result = await checkAppUpdateRelease();
      if (!result) {
        setPending(null);
        return;
      }

      if (!isApkUpdateAlertDismissed(result.releaseKey)) {
        setPending(result);
        await tryShowSystemUpdateAlert(result);
      } else {
        setPending(null);
      }
    } catch {
      /* Settings → Android update still works */
    } finally {
      checkingRef.current = false;
    }
  }, [enabled, setPending]);

  const runDownload = useCallback(async (info?: AppUpdateCheckResult) => {
    const target = info ?? pendingRef.current;
    if (!target || downloadingRef.current) return;
    downloadingRef.current = true;
    setDownloading(true);
    try {
      await installAndroidAppUpdate(target.remote);
      setPending(null);
      try {
        await AppUpdateNative.dismissUpdateNotification();
      } catch {
        /* ignore */
      }
    } finally {
      downloadingRef.current = false;
      setDownloading(false);
    }
  }, [setPending]);

  useEffect(() => {
    if (!enabled || !isNativeApp()) {
      setPending(null);
      return;
    }

    const timeouts: number[] = [];
    for (const delay of RETRY_DELAYS_MS) {
      timeouts.push(window.setTimeout(() => void runCheck(), delay));
    }

    const interval = window.setInterval(() => void runCheck(), CHECK_INTERVAL_MS);

    const onVisible = () => {
      if (document.visibilityState === "visible") void runCheck();
    };
    document.addEventListener("visibilitychange", onVisible);

    let removeRequested: { remove: () => Promise<void> } | undefined;
    let removeDismissed: { remove: () => Promise<void> } | undefined;

    void (async () => {
      try {
        removeRequested = await AppUpdateNative.addListener("appUpdateRequested", () => {
          void runDownload();
        });
        removeDismissed = await AppUpdateNative.addListener("appUpdateDismissed", (e) => {
          if (e.releaseKey) markApkUpdateAlertDismissed(e.releaseKey);
          setPending(null);
        });
      } catch {
        /* plugin missing on web */
      }
    })();

    return () => {
      for (const id of timeouts) window.clearTimeout(id);
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisible);
      void removeRequested?.remove();
      void removeDismissed?.remove();
    };
  }, [enabled, runCheck, runDownload, setPending]);

  return {
    pendingUpdate,
    downloading,
    recheckUpdate: runCheck,
    downloadUpdate: runDownload,
    dismissPending: () => {
      if (pendingRef.current) {
        markApkUpdateAlertDismissed(pendingRef.current.releaseKey);
        setPending(null);
        void AppUpdateNative.dismissUpdateNotification().catch(() => {});
      }
    },
  };
}
