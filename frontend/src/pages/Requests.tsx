import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import api from "../api/client";
import RequestStatusBadge from "../components/RequestStatus";
import RequestProgress from "../components/RequestProgress";
import { useWebSocket } from "../hooks/useWebSocket";
import { usePushNotifications } from "../hooks/usePushNotifications";
import { applyRequestWsUpdate, type DownloadRequestProgress } from "../utils/requestProgress";
import type { WSMessage } from "../hooks/wsClient";
import { List, Bell } from "lucide-react";

function formatSize(bytes: number | null): string {
  if (!bytes) return "-";
  const gb = bytes / (1024 * 1024 * 1024);
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  return `${(bytes / (1024 * 1024)).toFixed(0)} MB`;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function hasActiveDownloads(requests: DownloadRequestProgress[] | undefined): boolean {
  return (
    requests?.some((r) =>
      ["downloading_rd", "transferring", "organizing", "sent_to_rd", "pending"].includes(r.status)
    ) ?? false
  );
}

export default function RequestsPage() {
  const queryClient = useQueryClient();
  const { state: pushState, error: pushError, subscribe: enablePush, unsubscribe: disablePush } = usePushNotifications();

  const { data: requests, isLoading } = useQuery({
    queryKey: ["my-requests"],
    queryFn: async () => {
      const { data } = await api.get("/requests");
      return data as DownloadRequestProgress[];
    },
    refetchInterval: (query) => (hasActiveDownloads(query.state.data) ? 5000 : 15000),
  });

  const onWSMessage = useCallback(
    (msg: WSMessage) => {
      if (msg.type === "status_update" && msg.request_id != null) {
        queryClient.setQueryData<DownloadRequestProgress[]>(["my-requests"], (old) =>
          applyRequestWsUpdate(old, msg)
        );
        if (msg.status === "completed" || msg.status === "failed") {
          queryClient.invalidateQueries({ queryKey: ["my-requests"] });
        }
      } else {
        queryClient.invalidateQueries({ queryKey: ["my-requests"] });
      }
    },
    [queryClient]
  );

  useWebSocket(onWSMessage);

  return (
    <div className="max-w-4xl mx-auto px-4 py-8">
      <h1 className="text-2xl font-bold text-gray-100 mb-6 flex items-center gap-2">
        <List size={24} />
        My Requests
      </h1>

      {pushState !== "unsupported" && pushState !== "unavailable" && pushState !== "subscribed" && (
        <div className="mb-6 p-4 bg-gray-800/60 border border-gray-700 rounded-xl flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <Bell size={20} className="text-amber-400 shrink-0" />
            <div>
              <p className="text-sm font-medium text-gray-100">Get notified when your requests finish</p>
              <p className="text-xs text-gray-500">Enable push notifications to be alerted when books are ready</p>
              {pushError && <p className="text-xs text-red-400 mt-1">{pushError}</p>}
            </div>
          </div>
          <button
            onClick={enablePush}
            disabled={pushState === "subscribing" || pushState === "denied"}
            className="px-4 py-2 bg-amber-600 text-white text-sm font-medium rounded-lg hover:bg-amber-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {pushState === "subscribing" ? "Enabling..." : pushState === "denied" ? "Blocked" : "Enable"}
          </button>
        </div>
      )}

      {pushState === "subscribed" && (
        <div className="mb-6 p-3 bg-emerald-900/20 border border-emerald-800/50 rounded-xl flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm text-emerald-400">
            <Bell size={16} />
            Push notifications enabled — you'll be notified when your requests finish
          </div>
          <button
            onClick={disablePush}
            className="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200 border border-gray-600 rounded-lg hover:border-gray-500 transition-colors"
          >
            Disable
          </button>
        </div>
      )}

      {isLoading && (
        <div className="text-center py-12 text-gray-500">Loading...</div>
      )}

      {requests && requests.length === 0 && (
        <div className="text-center py-16 text-gray-500">
          <p>You haven't requested any audiobooks yet.</p>
        </div>
      )}

      {requests && requests.length > 0 && (
        <div className="space-y-3">
          {requests.map((req) => (
            <div
              key={req.id}
              className="bg-gray-800 border border-gray-700 rounded-xl p-4"
            >
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <h3 className="font-semibold text-gray-100 truncate">
                    {req.title}
                  </h3>
                  <div className="flex items-center gap-3 mt-1.5 text-xs text-gray-500">
                    <span>{formatDate(req.created_at)}</span>
                    <span>{formatSize(req.size_bytes)}</span>
                    {req.indexer && <span>{req.indexer}</span>}
                  </div>
                  <RequestProgress
                    status={req.status}
                    detail={req.status_detail}
                    progress_percent={req.progress_percent}
                    progress_bytes={req.progress_bytes}
                    progress_total_bytes={req.progress_total_bytes}
                    progress_speed_bps={req.progress_speed_bps}
                  />
                </div>
                <RequestStatusBadge
                  status={req.status}
                  detail={req.status_detail}
                />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
