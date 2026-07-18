import { useState, useCallback, useEffect } from "react";
import { Capacitor } from "@capacitor/core";
import { LocalNotifications } from "@capacitor/local-notifications";
import api from "../api/client";

export type PushState = "unsupported" | "unavailable" | "prompt" | "subscribing" | "subscribed" | "denied" | "error";

const NATIVE_PUSH_PREF_KEY = "native_push_notifications_enabled";
export const NATIVE_PUSH_PREF_EVENT = "native-push-pref-change";

function readNativePushPref(): boolean {
  try {
    return localStorage.getItem(NATIVE_PUSH_PREF_KEY) === "true";
  } catch {
    return false;
  }
}

function writeNativePushPref(enabled: boolean): void {
  try {
    localStorage.setItem(NATIVE_PUSH_PREF_KEY, enabled ? "true" : "false");
    window.dispatchEvent(
      new CustomEvent(NATIVE_PUSH_PREF_EVENT, { detail: enabled })
    );
  } catch {
    /* WebView storage unavailable */
  }
}

/** Whether the user opted in to native (APK) alerts on this device. */
export function isNativePushOptedIn(): boolean {
  if (!Capacitor.isNativePlatform()) return false;
  return readNativePushPref();
}

async function resolveNativePushState(): Promise<PushState> {
  const perm = await LocalNotifications.checkPermissions();
  if (perm.display === "denied") return "denied";
  if (perm.display === "granted" && readNativePushPref()) return "subscribed";
  return "prompt";
}

async function resolveWebPushState(): Promise<PushState> {
  if (typeof window === "undefined" || !("serviceWorker" in navigator) || !("PushManager" in window)) {
    return "unsupported";
  }
  if (!("Notification" in window)) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) return "subscribed";
  } catch {
    /* ignore */
  }
  if (Notification.permission === "granted") return "prompt";
  return "prompt";
}

function initialPushState(): PushState {
  if (Capacitor.isNativePlatform()) {
    // Sync read for first paint; useEffect reconciles with OS permission.
    return readNativePushPref() ? "subscribed" : "prompt";
  }
  if (typeof window === "undefined" || !("serviceWorker" in navigator) || !("PushManager" in window)) {
    return "unsupported";
  }
  if (!("Notification" in window)) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  return "prompt";
}

export function usePushNotifications() {
  const [state, setState] = useState<PushState>(initialPushState);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const resolved = Capacitor.isNativePlatform()
        ? await resolveNativePushState()
        : await resolveWebPushState();
      if (!cancelled) setState(resolved);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const subscribe = useCallback(async () => {
    if (state === "unsupported" || state === "unavailable") return;
    setError(null);
    setState("subscribing");

    try {
      if (Capacitor.isNativePlatform()) {
        const perm = await LocalNotifications.requestPermissions();
        if (perm.display !== "granted") {
          writeNativePushPref(false);
          setState("denied");
          return;
        }
        writeNativePushPref(true);
        setState("subscribed");
        return;
      }

      const { data } = await api.get<{ publicKey?: string; public_key?: string }>("/push/vapid-public");
      const vapidKey = data?.publicKey ?? data?.public_key;
      if (!vapidKey || typeof vapidKey !== "string") {
        throw new Error("Server did not return a valid VAPID public key");
      }

      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidKey) as BufferSource,
      });

      const subscription = sub.toJSON();
      await api.post("/push/subscribe", {
        endpoint: subscription.endpoint,
        keys: subscription.keys,
      });

      setState("subscribed");
    } catch (e: unknown) {
      const err = e as { response?: { status?: number } };
      if (err?.response?.status === 503) {
        setState("unavailable");
        setError("Push notifications are not configured");
        return;
      }
      const msg = e instanceof Error ? e.message : "Failed to enable notifications";
      setError(msg);
      if (!Capacitor.isNativePlatform() && Notification.permission === "denied") {
        setState("denied");
      } else {
        setState("error");
      }
    }
  }, [state]);

  const requestAndSubscribe = useCallback(async () => {
    if (Capacitor.isNativePlatform()) {
      await subscribe();
      return;
    }
    if (!("Notification" in window)) {
      setState("unsupported");
      return;
    }
    if (Notification.permission === "granted") {
      await subscribe();
      return;
    }
    const perm = await Notification.requestPermission();
    if (perm === "denied") {
      setState("denied");
      return;
    }
    if (perm === "granted") {
      await subscribe();
    }
  }, [subscribe]);

  const unsubscribe = useCallback(async () => {
    if (state !== "subscribed") return;
    setError(null);
    try {
      if (Capacitor.isNativePlatform()) {
        writeNativePushPref(false);
        setState("prompt");
        return;
      }
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        await api.delete("/push/subscribe", { params: { endpoint: sub.endpoint } });
        await sub.unsubscribe();
      }
      setState(Notification.permission === "granted" ? "prompt" : "denied");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to disable");
    }
  }, [state]);

  return { state, error, subscribe: requestAndSubscribe, unsubscribe };
}

function urlBase64ToUint8Array(base64: string): Uint8Array {
  if (!base64 || typeof base64 !== "string") {
    throw new Error("Invalid VAPID key: expected a base64 string");
  }
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  const arr = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return arr;
}
