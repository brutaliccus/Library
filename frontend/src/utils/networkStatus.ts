/**
 * Online / offline detection helpers.
 * Prefer these over raw navigator.onLine so UI + auth share one definition.
 */

export function isLikelyOffline(): boolean {
  return typeof navigator !== "undefined" && navigator.onLine === false;
}

/** True when an error looks like a network/unreachable failure (not an HTTP auth reject). */
export function isNetworkError(err: unknown): boolean {
  if (isLikelyOffline()) return true;
  if (!err || typeof err !== "object") return false;
  const e = err as {
    response?: unknown;
    code?: string;
    message?: string;
    name?: string;
  };
  // Axios: no response = transport failure (offline, DNS, CORS block, timeout).
  if (e.response === undefined && (e.code || e.message || e.name)) {
    const code = (e.code || "").toUpperCase();
    if (
      code === "ERR_NETWORK" ||
      code === "ECONNABORTED" ||
      code === "ETIMEDOUT" ||
      code === "ENOTFOUND"
    ) {
      return true;
    }
    const msg = (e.message || "").toLowerCase();
    if (
      msg.includes("network error") ||
      msg.includes("failed to fetch") ||
      msg.includes("timeout") ||
      msg.includes("load failed")
    ) {
      return true;
    }
    // Generic axios error without response while claiming a request was made.
    if (e.name === "AxiosError" && e.response === undefined) return true;
  }
  return false;
}

export function isAuthReject(err: unknown): boolean {
  if (!err || typeof err !== "object") return false;
  const status = (err as { response?: { status?: number } }).response?.status;
  return status === 401 || status === 403;
}

export function subscribeOnlineStatus(cb: (online: boolean) => void): () => void {
  const on = () => cb(true);
  const off = () => cb(false);
  window.addEventListener("online", on);
  window.addEventListener("offline", off);
  return () => {
    window.removeEventListener("online", on);
    window.removeEventListener("offline", off);
  };
}
