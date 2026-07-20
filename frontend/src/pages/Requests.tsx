import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import api from "../api/client";
import RequestStatusBadge from "../components/RequestStatus";
import RequestProgress from "../components/RequestProgress";
import CoverImage from "../components/CoverImage";
import { useWebSocket } from "../hooks/useWebSocket";
import { usePushNotifications } from "../hooks/usePushNotifications";
import { useToast } from "../contexts/ToastContext";
import { applyRequestWsUpdate, type DownloadRequestProgress } from "../utils/requestProgress";
import type { WSMessage } from "../hooks/wsClient";
import { List, Bell, EyeOff, BookOpen, RotateCcw, X } from "lucide-react";

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

const ACTIVE = new Set(["pending", "sent_to_rd", "downloading_rd", "transferring", "organizing"]);
const RETRYABLE = new Set(["failed", "cancelled"]);

export default function RequestsPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { state: pushState, error: pushError, subscribe: enablePush, unsubscribe: disablePush } = usePushNotifications();

  const { data: requests, isLoading } = useQuery({
    queryKey: ["my-requests"],
    queryFn: async () => {
      const { data } = await api.get("/requests");
      return data as DownloadRequestProgress[];
    },
    refetchInterval: (query) => (hasActiveDownloads(query.state.data) ? 5000 : 15000),
  });

  const cancelMutation = useMutation({
    mutationFn: (id: number) => api.post(`/requests/${id}/cancel`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["my-requests"] });
      toast("Request cancelled", "info");
    },
    onError: (err: any) => toast(err.response?.data?.detail || "Cancel failed", "error"),
  });

  const retryMutation = useMutation({
    mutationFn: (id: number) => api.post(`/requests/${id}/retry`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["my-requests"] });
      toast("Retry started", "success");
    },
    onError: (err: any) => toast(err.response?.data?.detail || "Retry failed", "error"),
  });

  const onWSMessage = useCallback(
    (msg: WSMessage) => {
      if (msg.type === "status_update" && msg.request_id != null) {
        queryClient.setQueryData<DownloadRequestProgress[]>(["my-requests"], (old) =>
          applyRequestWsUpdate(old, msg)
        );
        if (msg.status === "completed" || msg.status === "failed" || msg.status === "cancelled") {
          queryClient.invalidateQueries({ queryKey: ["my-requests"] });
        }
      } else {
        queryClient.invalidateQueries({ queryKey: ["my-requests"] });
      }
    },
    [queryClient]
  );

  useWebSocket(onWSMessage);

  const openRequest = useCallback(
    async (req: DownloadRequestProgress) => {
      if (req.status === "failed" || req.status === "cancelled") {
        if (req.google_volume_id && !req.google_volume_id.startsWith("rd:")) {
          navigate(`/book/${encodeURIComponent(req.google_volume_id)}`);
          return;
        }
        navigate(`/search?q=${encodeURIComponent(req.title)}`);
        return;
      }

      if (req.status === "completed") {
        try {
          const media = req.media_type === "ebook" ? "ebooks" : req.media_type === "audiobook" ? "audiobooks" : "all";
          const { data } = await api.get(
            `/library/search?q=${encodeURIComponent(req.title)}&media=${media}`
          );
          const results = (data as { results?: Array<{
            source: string;
            itemId?: string;
            chapterId?: number;
            title?: string;
          }> })?.results || [];
          const titleLower = req.title.toLowerCase();
          const match =
            results.find((r) => (r.title || "").toLowerCase() === titleLower) ||
            results.find((r) => {
              const t = (r.title || "").toLowerCase();
              return t.includes(titleLower) || titleLower.includes(t);
            }) ||
            results[0];
          if (match?.source === "abs" && match.itemId) {
            navigate(`/library/abs/${encodeURIComponent(match.itemId)}`);
            return;
          }
          if (match?.source === "kavita" && match.chapterId != null) {
            navigate(`/read/${match.chapterId}`);
            return;
          }
        } catch {
          /* fall through */
        }
        if (req.google_volume_id && !req.google_volume_id.startsWith("rd:")) {
          navigate(`/book/${encodeURIComponent(req.google_volume_id)}`);
          return;
        }
        navigate(`/library?q=${encodeURIComponent(req.title)}`);
        return;
      }

      // In-progress: still allow jumping to catalog / search
      if (req.google_volume_id && !req.google_volume_id.startsWith("rd:")) {
        navigate(`/book/${encodeURIComponent(req.google_volume_id)}`);
      }
    },
    [navigate]
  );

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
          <p>You haven't requested any books yet.</p>
        </div>
      )}

      {requests && requests.length > 0 && (
        <div className="space-y-3">
          {requests.map((req) => {
            const canCancel = ACTIVE.has(req.status);
            const canRetry = RETRYABLE.has(req.status);
            const clickable = req.status === "completed" || req.status === "failed" || req.status === "cancelled" || !!req.google_volume_id;
            return (
              <div
                key={req.id}
                className={`bg-gray-800 border border-gray-700 rounded-xl p-3 sm:p-4 ${
                  clickable ? "hover:border-gray-500 cursor-pointer" : ""
                }`}
                onClick={() => clickable && openRequest(req)}
                role={clickable ? "button" : undefined}
                tabIndex={clickable ? 0 : undefined}
                onKeyDown={(e) => {
                  if (clickable && (e.key === "Enter" || e.key === " ")) {
                    e.preventDefault();
                    openRequest(req);
                  }
                }}
              >
                <div className="flex gap-3 sm:gap-4">
                  <div className="w-16 sm:w-20 h-24 sm:h-28 rounded-lg overflow-hidden bg-gray-900 shrink-0 border border-gray-700">
                    {req.cover_url ? (
                      <CoverImage
                        src={req.cover_url}
                        alt=""
                        className="w-full h-full object-cover"
                      />
                    ) : (
                      <div className="w-full h-full flex items-center justify-center text-gray-600">
                        <BookOpen size={22} />
                      </div>
                    )}
                  </div>
                  <div className="flex-1 min-w-0 flex flex-col">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 min-w-0">
                          <h3 className="font-semibold text-gray-100 truncate text-base">
                            {req.title}
                          </h3>
                          {req.is_private && (
                            <span
                              className="inline-flex items-center gap-1 shrink-0 text-[10px] font-medium uppercase tracking-wide px-1.5 py-0.5 rounded bg-purple-900/40 text-purple-300 border border-purple-700/40"
                              title="Hidden from other members' library browse"
                            >
                              <EyeOff size={11} />
                              Private
                            </span>
                          )}
                        </div>
                        {req.author && (
                          <p className="text-sm text-gray-400 truncate mt-0.5">{req.author}</p>
                        )}
                      </div>
                      <RequestStatusBadge
                        status={req.status}
                        detail={req.status_detail}
                      />
                    </div>
                    <div className="flex items-center gap-3 mt-1.5 text-xs text-gray-500">
                      <span>{formatDate(req.created_at)}</span>
                      <span>{formatSize(req.size_bytes)}</span>
                      {req.indexer && <span className="truncate">{req.indexer}</span>}
                      {req.media_type && req.media_type !== "unknown" && (
                        <span className="capitalize">{req.media_type}</span>
                      )}
                    </div>
                    <RequestProgress
                      status={req.status}
                      detail={req.status_detail}
                      progress_percent={req.progress_percent}
                      progress_bytes={req.progress_bytes}
                      progress_total_bytes={req.progress_total_bytes}
                      progress_speed_bps={req.progress_speed_bps}
                    />
                    {(canCancel || canRetry) && (
                      <div className="flex gap-2 mt-2" onClick={(e) => e.stopPropagation()}>
                        {canCancel && (
                          <button
                            type="button"
                            onClick={() => cancelMutation.mutate(req.id)}
                            disabled={cancelMutation.isPending}
                            className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-lg border border-gray-600 text-gray-300 hover:border-red-500/60 hover:text-red-300 disabled:opacity-50"
                          >
                            <X size={12} />
                            Cancel
                          </button>
                        )}
                        {canRetry && (
                          <button
                            type="button"
                            onClick={() => retryMutation.mutate(req.id)}
                            disabled={retryMutation.isPending}
                            className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-lg border border-amber-700/50 text-amber-300 hover:bg-amber-900/30 disabled:opacity-50"
                          >
                            <RotateCcw size={12} />
                            Retry
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
