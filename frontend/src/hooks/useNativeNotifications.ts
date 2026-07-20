import { useCallback, useEffect, useRef, useState } from "react";
import { Capacitor } from "@capacitor/core";
import { LocalNotifications } from "@capacitor/local-notifications";
import { isNativePushOptedIn, NATIVE_PUSH_PREF_EVENT } from "./usePushNotifications";
import { useWebSocket } from "./useWebSocket";

let permissionRequested = false;
let nextId = 1;

async function ensureNotificationPermission(): Promise<boolean> {
  if (!Capacitor.isNativePlatform()) return false;
  if (permissionRequested) {
    const check = await LocalNotifications.checkPermissions();
    return check.display === "granted";
  }
  permissionRequested = true;
  const perm = await LocalNotifications.requestPermissions();
  return perm.display === "granted";
}

/** Show a native Android/iOS notification (in-app websocket events + playback). */
export async function showNativeNotification(
  title: string,
  body: string,
  extra?: { url?: string }
): Promise<void> {
  if (!Capacitor.isNativePlatform() || !isNativePushOptedIn()) return;
  const ok = await ensureNotificationPermission();
  if (!ok) return;

  const id = nextId++;
  await LocalNotifications.schedule({
    notifications: [
      {
        id,
        title,
        body,
        sound: undefined,
        smallIcon: "ic_stat_notification",
        extra: extra ?? {},
      },
    ],
  });
}

interface WSMessage {
  type: string;
  request_id?: number;
  status?: string;
  detail?: string;
  title?: string;
}

/**
 * On native Android/iOS: listen for server websocket events and surface them
 * as system notifications (web push does not work inside Capacitor WebView).
 */
export function useNativeNotifications(enabled: boolean) {
  const seen = useRef(new Set<string>());
  const [pushOptedIn, setPushOptedIn] = useState(isNativePushOptedIn);

  useEffect(() => {
    const onPref = () => setPushOptedIn(isNativePushOptedIn());
    window.addEventListener(NATIVE_PUSH_PREF_EVENT, onPref);
    return () => window.removeEventListener(NATIVE_PUSH_PREF_EVENT, onPref);
  }, []);

  const active = enabled && pushOptedIn;

  const onMessage = useCallback((msg: WSMessage) => {
    if (!Capacitor.isNativePlatform()) return;

    if (
      msg.type === "status_update" &&
      msg.status === "completed"
    ) {
      const key = `dl-${msg.request_id ?? ""}`;
      if (seen.current.has(key)) return;
      seen.current.add(key);
      void showNativeNotification(
        "Download ready",
        msg.detail || "Your book is available in the library",
        { url: "/my-library" }
      );
    }

    if (msg.type === "download_complete") {
      const key = `dl-${msg.request_id ?? msg.title ?? ""}`;
      if (seen.current.has(key)) return;
      seen.current.add(key);

      const title = msg.title ? `${msg.title} is ready` : "Download ready";
      const body = msg.detail || "Available in your library";
      void showNativeNotification(title, body, { url: "/my-library" });
    }

    if (msg.type === "invite_signup" || msg.type === "admin_alert") {
      const key = `admin-${msg.type}-${msg.request_id ?? Date.now()}`;
      if (seen.current.has(key)) return;
      seen.current.add(key);
      void showNativeNotification(
        msg.title || "Library",
        msg.detail || "You have a new notification",
        { url: "/admin" }
      );
    }
  }, []);

  useWebSocket(active ? onMessage : undefined);

  useEffect(() => {
    if (!active || !Capacitor.isNativePlatform()) return;
    void ensureNotificationPermission();
  }, [active]);
}
