import type { WSMessage } from "../hooks/wsClient";

export interface DownloadRequestProgress {
  id: number;
  title: string;
  author: string | null;
  media_type?: string;
  status: string;
  status_detail: string | null;
  size_bytes: number | null;
  indexer: string | null;
  is_private?: boolean;
  google_volume_id?: string | null;
  cover_url?: string | null;
  created_at: string;
  completed_at: string | null;
  progress_percent?: number | null;
  progress_bytes?: number | null;
  progress_total_bytes?: number | null;
  progress_speed_bps?: number | null;
  staging_path?: string | null;
  quarantine_reason?: string | null;
  manual_review_url?: string | null;
  username?: string;
}

/** Truly finished — no further pipeline work expected. */
export const TERMINAL_REQUEST_STATUSES = new Set([
  "completed",
  "failed",
  "cancelled",
  "admin_rejected",
]);

/**
 * In-flight download/forge steps the user can cancel.
 * Quarantined is waiting on admin, not user cancel.
 */
export const CANCELLABLE_REQUEST_STATUSES = new Set([
  "pending",
  "sent_to_rd",
  "downloading_rd",
  "transferring",
  "organizing",
  "metadata_forge",
  "m4b_convert",
  "folder_forge",
  "finalizing",
]);

/** Statuses that need live UI updates (poll / WS). Includes quarantined. */
export function isLiveRequestStatus(status: string): boolean {
  return !!status && !TERMINAL_REQUEST_STATUSES.has(status);
}

export function hasLiveRequests(
  requests: Array<{ status: string }> | undefined
): boolean {
  return requests?.some((r) => isLiveRequestStatus(r.status)) ?? false;
}

/** Fast poll while any request may still change (incl. quarantine → continue). */
export function requestListRefetchInterval(
  requests: Array<{ status: string }> | undefined
): number | false {
  return hasLiveRequests(requests) ? 5000 : false;
}

export function applyRequestWsUpdate(
  requests: DownloadRequestProgress[] | undefined,
  msg: WSMessage
): DownloadRequestProgress[] | undefined {
  if (!requests || msg.type !== "status_update" || msg.request_id == null) {
    return requests;
  }
  return requests.map((r) => {
    if (r.id !== msg.request_id) return r;
    // Cancel is terminal — ignore late progress WS that would revive the card.
    if (
      r.status === "cancelled" &&
      msg.status != null &&
      msg.status !== "cancelled"
    ) {
      return r;
    }
    return {
      ...r,
      status: msg.status ?? r.status,
      status_detail: msg.detail ?? r.status_detail,
      progress_percent:
        msg.progress_percent !== undefined ? msg.progress_percent : r.progress_percent,
      progress_bytes:
        msg.progress_bytes !== undefined ? msg.progress_bytes : r.progress_bytes,
      progress_total_bytes:
        msg.progress_total_bytes !== undefined
          ? msg.progress_total_bytes
          : r.progress_total_bytes,
      progress_speed_bps:
        msg.progress_speed_bps !== undefined
          ? msg.progress_speed_bps
          : r.progress_speed_bps,
    };
  });
}
