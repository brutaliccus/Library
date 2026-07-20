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
}

export function applyRequestWsUpdate(
  requests: DownloadRequestProgress[] | undefined,
  msg: WSMessage
): DownloadRequestProgress[] | undefined {
  if (!requests || msg.type !== "status_update" || msg.request_id == null) {
    return requests;
  }
  return requests.map((r) =>
    r.id === msg.request_id
      ? {
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
        }
      : r
  );
}
