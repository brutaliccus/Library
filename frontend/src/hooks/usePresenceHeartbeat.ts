import { useEffect } from "react";
import api from "../api/client";
import { isLikelyOffline } from "../utils/networkStatus";

const HEARTBEAT_MS = 60_000;

/**
 * Ping the server while a logged-in SPA tab is visible so admin presence
 * (users.last_seen_at) stays fresh. Skips when offline / hidden.
 */
export function usePresenceHeartbeat(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return;

    let timer: ReturnType<typeof setInterval> | undefined;

    const beat = () => {
      if (typeof document !== "undefined" && document.visibilityState !== "visible") {
        return;
      }
      if (isLikelyOffline()) return;
      void api.post("/auth/heartbeat").catch(() => {});
    };

    const start = () => {
      beat();
      if (timer) clearInterval(timer);
      timer = setInterval(beat, HEARTBEAT_MS);
    };

    const stop = () => {
      if (timer) {
        clearInterval(timer);
        timer = undefined;
      }
    };

    const onVisibility = () => {
      if (document.visibilityState === "visible") start();
      else stop();
    };

    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [enabled]);
}
