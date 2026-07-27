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
  url?: string;
  alert_type?: string;
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

    if (msg.type === "status_update" && msg.request_id != null && msg.status) {
      const status = msg.status;
      if (status === "completed") {
        const key = `dl-${msg.request_id}`;
        if (seen.current.has(key)) return;
        seen.current.add(key);
        void showNativeNotification(
          "Download ready",
          msg.detail || "Your book is available in the library",
          { url: "/my-library" }
        );
        return;
      }
      if (status === "quarantined") {
        // status_update is sent to the requesting user only; admins get
        // admin_alert from notify_admins (see push.py).
        const key = `q-${msg.request_id}`;
        if (seen.current.has(key)) return;
        seen.current.add(key);
        void showNativeNotification(
          "Waiting for review",
          msg.detail || "An admin will review your request",
          { url: "/requests" }
        );
        return;
      }
      if (status === "failed" || status === "admin_rejected") {
        const key = `fail-${msg.request_id}-${status}`;
        if (seen.current.has(key)) return;
        seen.current.add(key);
        const title =
          status === "admin_rejected" ? "Request rejected" : "Download failed";
        void showNativeNotification(
          title,
          msg.detail || "Open Downloads for details",
          { url: "/requests" }
        );
        return;
      }
    }

    if (msg.type === "download_complete") {
      const key = `dl-${msg.request_id ?? msg.title ?? ""}`;
      if (seen.current.has(key)) return;
      seen.current.add(key);

      const title = msg.title ? `${msg.title} is ready` : "Download ready";
      const body = msg.detail || "Available in your library";
      void showNativeNotification(title, body, { url: "/my-library" });
      return;
    }

    if (
      msg.type === "invite_signup" ||
      msg.type === "admin_alert" ||
      msg.type === "download_quarantined"
    ) {
      const key = `admin-${msg.type}-${msg.alert_type ?? ""}-${msg.request_id ?? msg.title ?? Date.now()}`;
      if (seen.current.has(key)) return;
      seen.current.add(key);
      const url =
        msg.url ||
        (msg.type === "download_quarantined" || msg.alert_type === "download_quarantined"
          ? "/admin?tab=requests"
          : "/admin");
      void showNativeNotification(
        msg.title || "Library",
        msg.detail || "You have a new notification",
        { url }
      );
    }
  }, []);

  useWebSocket(active ? onMessage : undefined);

  useEffect(() => {
    if (!active || !Capacitor.isNativePlatform()) return;
    void ensureNotificationPermission();
  }, [active]);
}
