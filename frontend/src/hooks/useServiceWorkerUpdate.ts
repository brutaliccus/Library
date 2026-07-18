import { useCallback, useEffect, useRef, useState } from "react";
import { Capacitor } from "@capacitor/core";

const UPDATE_CHECK_MS = 60 * 60 * 1000;

function markWaiting(registration: ServiceWorkerRegistration) {
  return Boolean(registration.waiting && navigator.serviceWorker.controller);
}

export function useServiceWorkerUpdate() {
  const [updateReady, setUpdateReady] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const registrationRef = useRef<ServiceWorkerRegistration | null>(null);
  const applyingRef = useRef(false);

  useEffect(() => {
    // Capacitor WebView: audio cache bypasses the SW, push uses LocalNotifications.
    // An active SW here only intercepts navigations and causes fetch errors.
    if (import.meta.env.DEV || !("serviceWorker" in navigator) || Capacitor.isNativePlatform()) {
      if (Capacitor.isNativePlatform()) {
        void navigator.serviceWorker?.getRegistrations().then((regs) => {
          for (const reg of regs) void reg.unregister();
        });
      }
      return;
    }

    let intervalId: ReturnType<typeof setInterval> | undefined;

    const onControllerChange = () => {
      if (!applyingRef.current) return;
      window.location.reload();
    };

    const checkWaiting = (reg: ServiceWorkerRegistration) => {
      if (markWaiting(reg)) setUpdateReady(true);
    };

    const watchRegistration = (reg: ServiceWorkerRegistration) => {
      registrationRef.current = reg;
      checkWaiting(reg);

      reg.addEventListener("updatefound", () => {
        const worker = reg.installing;
        if (!worker) return;
        worker.addEventListener("statechange", () => {
          if (worker.state === "installed") checkWaiting(reg);
        });
      });
    };

    const register = async () => {
      try {
        const reg = await navigator.serviceWorker.register("/sw.js", {
          updateViaCache: "none",
        });
        watchRegistration(reg);
        await reg.update();
      } catch {
        // SW optional (dev, blocked context, etc.)
      }
    };

    navigator.serviceWorker.addEventListener("controllerchange", onControllerChange);
    void register();

    intervalId = setInterval(() => {
      void registrationRef.current?.update();
    }, UPDATE_CHECK_MS);

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        void registrationRef.current?.update();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      navigator.serviceWorker.removeEventListener("controllerchange", onControllerChange);
      document.removeEventListener("visibilitychange", onVisibility);
      if (intervalId) clearInterval(intervalId);
    };
  }, []);

  const applyUpdate = useCallback(() => {
    const reg = registrationRef.current;
    const waiting = reg?.waiting;
    if (waiting) {
      applyingRef.current = true;
      waiting.postMessage({ type: "SKIP_WAITING" });
      return;
    }
    window.location.reload();
  }, []);

  const dismissUpdate = useCallback(() => setDismissed(true), []);

  return {
    showBanner: updateReady && !dismissed,
    applyUpdate,
    dismissUpdate,
  };
}
