import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { LucideIcon } from "lucide-react";
import {
  Play,
  Pause,
  Square,
  ChevronLeft,
  ChevronRight,
  Check,
  Wand2,
  Loader2,
  RefreshCw,
  SkipForward,
  RotateCcw,
  X,
  Settings,
  Headphones,
  BookOpen,
  Trash2,
  Search,
} from "lucide-react";
import api from "../../api/client";
import { useToast } from "../../contexts/ToastContext";
import CoverImage from "../CoverImage";
import ConfirmModal from "../ConfirmModal";
import Modal from "../Modal";
import QuickReviewWizard from "./QuickReviewWizard";
import EbookMetadataMatcher from "./EbookMetadataMatcher";
import NamingTemplateBuilder from "./NamingTemplateBuilder";

// --- Shared types (audiobook + ebook sweep share the same job/response shapes) ---

type SweepCurrent = {
  request_id: number | null;
  title: string | null;
  author: string | null;
  cover_url: string | null;
  status: string | null;
  abs_item_id?: string | null;
};

type SweepUpNext = {
  request_id?: number | null;
  title: string | null;
  author: string | null;
  cover_url: string | null;
  abs_item_id?: string | null;
};

type UnprocessedCounts = {
  cancelled: number;
  failed: number;
  skipped: number;
  admin_rejected: number;
  total: number;
};

type SweepStatus = {
  id: number;
  status: string;
  started_at: string | null;
  updated_at: string | null;
  total: number;
  scanned: number;
  auto_applied: number;
  needs_review: number;
  failed: number;
  m4b_queued: number;
  review_cursor_request_id: number | null;
  error: string | null;
  started_by_user_id: number | null;
  current?: SweepCurrent | null;
  up_next?: SweepUpNext | null;
  processed_total?: number;
  unprocessed?: UnprocessedCounts;
};

type NeedsReviewItem = {
  id: number;
  title: string | null;
  author: string | null;
  status: string;
  status_detail: string | null;
  quarantine_reason: string | null;
  abs_item_id?: string | null;
  staging_path: string | null;
  cover_url: string | null;
  created_at: string | null;
};

type NeedsReviewResponse = {
  items: NeedsReviewItem[];
  review_cursor_request_id: number | null;
  count: number;
};

type UnprocessedResponse = {
  items: NeedsReviewItem[];
  count: number;
  counts: UnprocessedCounts;
};

type ProcessedItem = {
  id: number;
  title: string | null;
  author: string | null;
  status: string;
  status_detail: string | null;
  abs_item_id?: string | null;
  cover_url: string | null;
  created_at: string | null;
  completed_at: string | null;
};

type ProcessedResponse = {
  items: ProcessedItem[];
  total: number;
  limit: number;
  offset: number;
  count: number;
};

type QueueTab = "needs-review" | "unprocessed" | "processed";

type SweepConfirm =
  | { kind: "start" }
  | { kind: "cancel" }
  | { kind: "dismiss"; id: number; title: string };

type SweepSetting = {
  key: string;
  label: string;
  value: string;
  help?: string;
  placeholder?: string;
  valueType?: string;
};

const PROCESSED_PAGE_SIZE = 40;

function iconBtnClass(active?: boolean) {
  return `inline-flex items-center justify-center p-2 rounded-lg border transition-colors disabled:opacity-40 ${
    active
      ? "border-brand-600/60 bg-brand-600/20 text-brand-300"
      : "border-gray-700 text-gray-300 hover:bg-gray-800 hover:text-gray-100"
  }`;
}

function statusBadge(status: string | null | undefined) {
  const s = (status || "").toLowerCase();
  if (s === "failed" || s === "admin_rejected") return "text-red-400";
  if (s === "cancelled" || s === "skipped") return "text-amber-400";
  if (s === "quarantined") return "text-amber-300";
  if (s.includes("forge") || s === "m4b_convert" || s === "scanning") return "text-brand-300";
  return "text-gray-400";
}

function formatBytes(n: number): string {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  const decimals = i === 0 || v >= 10 ? 0 : 1;
  return `${v.toFixed(decimals)} ${units[i]}`;
}

// --- Shared /admin/config helpers (both sections read/write the same settings list) ---

function useAdminConfigSettings() {
  return useQuery({
    queryKey: ["admin-config"],
    queryFn: async () => {
      const { data } = await api.get("/admin/config");
      return (data?.settings || []) as SweepSetting[];
    },
  });
}

function useSaveConfigKeys() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (updates: Record<string, string>) => {
      const { data } = await api.put("/admin/config", { settings: updates });
      return data as { settings?: SweepSetting[] };
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["admin-config"] });
      toast("Setting saved", "success");
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      toast(err?.response?.data?.detail || "Failed to save setting", "error");
    },
  });
}

function InlineBoolToggle({
  label,
  help,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  help?: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="flex items-start gap-2 rounded-lg border border-gray-800 bg-gray-950/40 px-3 py-2.5">
      <input
        type="checkbox"
        className="mt-0.5 rounded border-gray-600 bg-gray-900"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="min-w-0">
        <span className="block text-xs font-medium text-gray-200">{label}</span>
        {help ? <span className="block text-[11px] text-gray-500 mt-0.5">{help}</span> : null}
      </span>
    </label>
  );
}

// ============================================================================
// MediumSweepSection - shared status/queue UI for both audiobook + ebook sweep
// ============================================================================

type MediumSweepSectionProps = {
  medium: "audiobook" | "ebook";
  basePath: string;
  icon: LucideIcon;
  title: string;
  description: string;
  startConfirmText: string;
  showM4bCounter: boolean;
  toggles: React.ReactNode;
  renderReviewModal: (args: {
    item: NeedsReviewItem;
    open: boolean;
    onClose: () => void;
  }) => React.ReactNode;
};

function MediumSweepSection({
  medium,
  basePath,
  icon: Icon,
  title,
  description,
  startConfirmText,
  showM4bCounter,
  toggles,
  renderReviewModal,
}: MediumSweepSectionProps) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [cursorRestored, setCursorRestored] = useState(false);
  const [queueTab, setQueueTab] = useState<QueueTab>("needs-review");
  const [processedOffset, setProcessedOffset] = useState(0);
  const [confirmAction, setConfirmAction] = useState<SweepConfirm | null>(null);

  const STATUS_KEY = useMemo(() => ["admin-library-sweep-status", medium] as const, [medium]);
  const REVIEW_KEY = useMemo(
    () => ["admin-library-sweep-needs-review", medium] as const,
    [medium],
  );
  const UNPROCESSED_KEY = useMemo(
    () => ["admin-library-sweep-unprocessed", medium] as const,
    [medium],
  );
  const PROCESSED_KEY = useMemo(
    () => ["admin-library-sweep-processed", medium] as const,
    [medium],
  );

  const { data: status, isLoading: statusLoading, refetch: refetchStatus } = useQuery({
    queryKey: STATUS_KEY,
    queryFn: async () => {
      const { data } = await api.get(`${basePath}/status`);
      return data as SweepStatus;
    },
    refetchInterval: (q) => (q.state.data?.status === "running" ? 2500 : 15_000),
  });

  const { data: review, isLoading: reviewLoading, refetch: refetchReview } = useQuery({
    queryKey: REVIEW_KEY,
    queryFn: async () => {
      const { data } = await api.get(`${basePath}/needs-review`);
      return data as NeedsReviewResponse;
    },
    refetchInterval: () => (status?.status === "running" ? 5000 : 20_000),
  });

  const {
    data: unprocessed,
    isLoading: unprocessedLoading,
    refetch: refetchUnprocessed,
  } = useQuery({
    queryKey: UNPROCESSED_KEY,
    queryFn: async () => {
      const { data } = await api.get(`${basePath}/unprocessed`);
      return data as UnprocessedResponse;
    },
    refetchInterval: () => (status?.status === "running" ? 8000 : 30_000),
  });

  const {
    data: processed,
    isLoading: processedLoading,
    refetch: refetchProcessed,
  } = useQuery({
    queryKey: [...PROCESSED_KEY, processedOffset],
    queryFn: async () => {
      const { data } = await api.get(`${basePath}/processed`, {
        params: { limit: PROCESSED_PAGE_SIZE, offset: processedOffset },
      });
      return data as ProcessedResponse;
    },
    enabled: queueTab === "processed",
    refetchInterval: () => (status?.status === "running" ? 10_000 : 45_000),
  });

  const items = review?.items ?? [];
  const unprocessedItems = unprocessed?.items ?? [];
  const processedItems = processed?.items ?? [];
  const processedTotal = processed?.total ?? status?.processed_total ?? 0;
  const selectedIndex = useMemo(() => {
    if (selectedId == null) return -1;
    return items.findIndex((i) => i.id === selectedId);
  }, [items, selectedId]);
  const selectedItem = selectedIndex >= 0 ? items[selectedIndex] : null;

  const setCursor = useMutation({
    mutationFn: async (requestId: number | null) => {
      const { data } = await api.put(`${basePath}/review-cursor`, {
        request_id: requestId,
      });
      return data as SweepStatus;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(STATUS_KEY, data);
      void queryClient.invalidateQueries({ queryKey: REVIEW_KEY });
    },
  });

  const selectItem = useCallback(
    (id: number | null, { openWizard = false }: { openWizard?: boolean } = {}) => {
      setSelectedId(id);
      if (openWizard && id != null) setWizardOpen(true);
      if (id !== status?.review_cursor_request_id) {
        setCursor.mutate(id);
      }
    },
    [setCursor, status?.review_cursor_request_id],
  );

  useEffect(() => {
    if (cursorRestored || reviewLoading) return;
    const cursor = review?.review_cursor_request_id ?? status?.review_cursor_request_id ?? null;
    if (cursor != null && items.some((i) => i.id === cursor)) {
      setSelectedId(cursor);
      setCursorRestored(true);
      return;
    }
    if (items.length > 0 && selectedId == null) {
      setSelectedId(items[0].id);
      setCursorRestored(true);
      return;
    }
    if (!reviewLoading && status) setCursorRestored(true);
  }, [
    cursorRestored,
    review?.review_cursor_request_id,
    status?.review_cursor_request_id,
    status,
    items,
    reviewLoading,
    selectedId,
  ]);

  useEffect(() => {
    if (selectedId == null || reviewLoading) return;
    if (items.length === 0) {
      setSelectedId(null);
      setWizardOpen(false);
      return;
    }
    if (!items.some((i) => i.id === selectedId)) {
      const next = items[Math.min(selectedIndex < 0 ? 0 : selectedIndex, items.length - 1)];
      setSelectedId(next.id);
      if (wizardOpen) setWizardOpen(true);
    }
  }, [items, selectedId, selectedIndex, reviewLoading, wizardOpen]);

  const invalidateAll = () => {
    void refetchStatus();
    void refetchReview();
    void refetchUnprocessed();
    void refetchProcessed();
  };

  const startMutation = useMutation({
    mutationFn: async () => {
      const { data } = await api.post(`${basePath}/start`);
      return data as SweepStatus & { message?: string };
    },
    onSuccess: (data) => {
      queryClient.setQueryData(STATUS_KEY, data);
      invalidateAll();
      toast(data.message || `${title} sweep started`, "success");
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      toast(err?.response?.data?.detail || "Failed to start sweep", "error");
    },
  });

  const pauseMutation = useMutation({
    mutationFn: async () => {
      const { data } = await api.post(`${basePath}/pause`);
      return data as SweepStatus;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(STATUS_KEY, data);
      toast(`${title} sweep paused`, "info");
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      toast(err?.response?.data?.detail || "Failed to pause sweep", "error");
    },
  });

  const cancelMutation = useMutation({
    mutationFn: async () => {
      const { data } = await api.post(`${basePath}/cancel`);
      return data as SweepStatus;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(STATUS_KEY, data);
      invalidateAll();
      toast(`${title} sweep cancelled`, "info");
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      toast(err?.response?.data?.detail || "Failed to cancel sweep", "error");
    },
  });

  const skipMutation = useMutation({
    mutationFn: async (requestId?: number | null) => {
      const { data } = await api.post(`${basePath}/skip`, {
        request_id: requestId ?? null,
      });
      return data as SweepStatus & { message?: string; skipped_id?: number | null };
    },
    onSuccess: (data) => {
      queryClient.setQueryData(STATUS_KEY, data);
      invalidateAll();
      toast(data.message || "Book skipped", "info");
      setQueueTab("unprocessed");
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      toast(err?.response?.data?.detail || "Failed to skip book", "error");
    },
  });

  const reprocessMutation = useMutation({
    mutationFn: async (requestId: number) => {
      const { data } = await api.post(`${basePath}/reprocess/${requestId}`);
      return data as {
        ok?: boolean;
        result?: { id?: number; status?: string; reason?: string };
        status?: SweepStatus;
      };
    },
    onSuccess: (data) => {
      if (data.status) queryClient.setQueryData(STATUS_KEY, data.status);
      invalidateAll();
      if (data.ok === false) {
        const reason = data.result?.reason;
        toast(
          reason === "staging_missing" || reason === "missing_staging"
            ? "Cannot reprocess - staging folder missing and restage failed"
            : reason
              ? `Reprocess failed: ${reason}`
              : "Reprocess failed",
          "error",
        );
        return;
      }
      toast("Reprocess started for this book", "success");
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      toast(err?.response?.data?.detail || "Failed to reprocess", "error");
    },
  });

  const dismissMutation = useMutation({
    mutationFn: async (requestId: number) => {
      const { data } = await api.post(`${basePath}/dismiss/${requestId}`);
      return data as { ok?: boolean; dismissed?: number[]; status?: SweepStatus };
    },
    onSuccess: (data) => {
      if (data.status) queryClient.setQueryData(STATUS_KEY, data.status);
      invalidateAll();
      toast("Removed from Unprocessed", "info");
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      toast(err?.response?.data?.detail || "Failed to remove", "error");
    },
  });

  const busy =
    startMutation.isPending ||
    pauseMutation.isPending ||
    cancelMutation.isPending ||
    skipMutation.isPending;
  const running = status?.status === "running";
  const paused = status?.status === "paused";
  const current = status?.current;
  const upNext = status?.up_next;
  const unprocessedTotal =
    status?.unprocessed?.total ?? unprocessed?.counts?.total ?? unprocessedItems.length;

  const goPrev = () => {
    if (selectedIndex <= 0) return;
    const prev = items[selectedIndex - 1];
    selectItem(prev.id, { openWizard: wizardOpen });
  };

  const goNext = (openWizard = wizardOpen) => {
    if (selectedIndex < 0 || selectedIndex >= items.length - 1) {
      setWizardOpen(false);
      return;
    }
    const next = items[selectedIndex + 1];
    selectItem(next.id, { openWizard });
  };

  const saveAndNext = () => {
    if (selectedIndex < 0) return;
    if (selectedIndex >= items.length - 1) {
      setWizardOpen(false);
      toast("End of needs-review queue", "info");
      return;
    }
    goNext(true);
  };

  const counters: { label: string; value: number | string }[] = [
    { label: "Status", value: status?.status ?? "-" },
    { label: "Total", value: status?.total ?? 0 },
    { label: "Scanned", value: status?.scanned ?? 0 },
    { label: "Auto-applied", value: status?.auto_applied ?? 0 },
    { label: "Needs review", value: status?.needs_review ?? 0 },
    { label: "Failed", value: status?.failed ?? 0 },
    ...(showM4bCounter ? [{ label: "M4B queued", value: status?.m4b_queued ?? 0 }] : []),
  ];

  return (
    <section className="rounded-xl border border-gray-800 bg-gray-900/50 p-4 space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-base font-semibold text-gray-100 flex items-center gap-2">
            <Icon size={18} className="text-brand-400 shrink-0" />
            {title}
          </h3>
          <p className="text-xs text-gray-500 mt-1 max-w-xl">{description}</p>
        </div>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            className={iconBtnClass(running)}
            title={paused ? "Resume sweep" : "Start sweep"}
            aria-label={paused ? "Resume sweep" : "Start sweep"}
            disabled={busy || running}
            onClick={() => setConfirmAction({ kind: "start" })}
          >
            {startMutation.isPending ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <Play size={16} />
            )}
          </button>
          <button
            type="button"
            className={iconBtnClass(paused)}
            title="Pause sweep"
            aria-label="Pause sweep"
            disabled={busy || !running}
            onClick={() => pauseMutation.mutate()}
          >
            {pauseMutation.isPending ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <Pause size={16} />
            )}
          </button>
          <button
            type="button"
            className={iconBtnClass()}
            title="Skip current book"
            aria-label="Skip current book"
            disabled={busy || (!running && !current?.request_id && !current?.title)}
            onClick={() => skipMutation.mutate(current?.request_id ?? null)}
          >
            {skipMutation.isPending ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <SkipForward size={16} />
            )}
          </button>
          <button
            type="button"
            className={iconBtnClass()}
            title="Cancel sweep"
            aria-label="Cancel sweep"
            disabled={busy || (!running && !paused)}
            onClick={() => setConfirmAction({ kind: "cancel" })}
          >
            {cancelMutation.isPending ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <Square size={16} />
            )}
          </button>
          <button
            type="button"
            className={iconBtnClass()}
            title="Refresh status"
            aria-label="Refresh status"
            onClick={invalidateAll}
          >
            <RefreshCw size={16} />
          </button>
        </div>
      </div>

      {toggles}

      {statusLoading && !status ? (
        <p className="text-sm text-gray-500">Loading sweep status...</p>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
          {counters.map((c) => (
            <div
              key={c.label}
              className="rounded-xl border border-gray-800 bg-gray-950/40 px-3 py-2.5"
            >
              <p className="text-[10px] uppercase tracking-wider text-gray-500">{c.label}</p>
              <p className="text-sm font-semibold text-gray-100 mt-0.5 truncate capitalize">
                {c.value}
              </p>
            </div>
          ))}
        </div>
      )}

      {(running || paused || current) && (
        <div className="rounded-xl border border-gray-800 bg-gray-950/60 p-3 flex gap-3 items-center">
          <div className="w-14 h-20 shrink-0 rounded overflow-hidden bg-gray-800">
            {current?.cover_url ? (
              <CoverImage src={current.cover_url} alt="" className="w-full h-full object-cover" />
            ) : (
              <div className="w-full h-full flex items-center justify-center text-gray-600 text-xs">
                -
              </div>
            )}
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-[10px] uppercase tracking-wider text-gray-500">Processing now</p>
            <p className="text-sm font-semibold text-gray-100 truncate mt-0.5">
              {current?.title || (running ? "Scanning library..." : "-")}
            </p>
            {current?.author && (
              <p className="text-xs text-gray-400 truncate mt-0.5">{current.author}</p>
            )}
            {current?.status && (
              <p className={`text-[11px] mt-1 capitalize ${statusBadge(current.status)}`}>
                {current.status.replace(/_/g, " ")}
                {current.request_id != null ? ` - #${current.request_id}` : ""}
              </p>
            )}
          </div>
          <button
            type="button"
            className={iconBtnClass()}
            title="Skip this book"
            aria-label="Skip this book"
            disabled={busy || (!current?.request_id && !current?.title)}
            onClick={() => skipMutation.mutate(current?.request_id ?? null)}
          >
            <SkipForward size={16} />
          </button>
        </div>
      )}

      {(running || paused) && upNext && (
        <div className="rounded-xl border border-gray-800/80 bg-gray-950/30 p-3 flex gap-3 items-center">
          <div className="w-12 h-[4.25rem] shrink-0 rounded overflow-hidden bg-gray-800">
            {upNext.cover_url ? (
              <CoverImage src={upNext.cover_url} alt="" className="w-full h-full object-cover" />
            ) : (
              <div className="w-full h-full flex items-center justify-center text-gray-600 text-xs">
                -
              </div>
            )}
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-[10px] uppercase tracking-wider text-gray-500">Up next</p>
            <p className="text-sm font-medium text-gray-200 truncate mt-0.5">
              {upNext.title || "-"}
            </p>
            {upNext.author && (
              <p className="text-xs text-gray-400 truncate mt-0.5">{upNext.author}</p>
            )}
          </div>
        </div>
      )}

      {(running || paused) && !upNext && (
        <div className="rounded-xl border border-dashed border-gray-800 bg-gray-950/20 px-3 py-2">
          <p className="text-[10px] uppercase tracking-wider text-gray-500">Up next</p>
          <p className="text-xs text-gray-500 mt-0.5">
            {running ? "Looking ahead..." : "Nothing queued after the current book"}
          </p>
        </div>
      )}

      {status?.error && (
        <p className="text-sm text-red-400 border border-red-900/50 bg-red-950/30 rounded-lg px-3 py-2">
          {status.error}
        </p>
      )}

      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-1 rounded-lg border border-gray-800 p-0.5">
            <button
              type="button"
              className={`px-3 py-1.5 text-xs rounded-md transition-colors ${
                queueTab === "needs-review"
                  ? "bg-brand-600/20 text-brand-200"
                  : "text-gray-400 hover:text-gray-200"
              }`}
              onClick={() => setQueueTab("needs-review")}
            >
              Needs review ({items.length})
            </button>
            <button
              type="button"
              className={`px-3 py-1.5 text-xs rounded-md transition-colors ${
                queueTab === "unprocessed"
                  ? "bg-brand-600/20 text-brand-200"
                  : "text-gray-400 hover:text-gray-200"
              }`}
              onClick={() => setQueueTab("unprocessed")}
            >
              Unprocessed ({unprocessedTotal})
            </button>
            <button
              type="button"
              className={`px-3 py-1.5 text-xs rounded-md transition-colors ${
                queueTab === "processed"
                  ? "bg-brand-600/20 text-brand-200"
                  : "text-gray-400 hover:text-gray-200"
              }`}
              onClick={() => {
                setQueueTab("processed");
                setProcessedOffset(0);
              }}
            >
              Processed ({status?.processed_total ?? processedTotal})
            </button>
          </div>
          {queueTab === "needs-review" && items.length > 0 && selectedIndex >= 0 && (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-gray-500 tabular-nums mr-1">
                {selectedIndex + 1} / {items.length}
              </span>
              <button
                type="button"
                className={iconBtnClass()}
                title="Previous"
                aria-label="Previous needs-review item"
                disabled={selectedIndex <= 0 || setCursor.isPending}
                onClick={goPrev}
              >
                <ChevronLeft size={16} />
              </button>
              <button
                type="button"
                className={iconBtnClass()}
                title="Save & next"
                aria-label="Save and next"
                disabled={selectedIndex < 0 || setCursor.isPending}
                onClick={saveAndNext}
              >
                <Check size={14} />
                <ChevronRight size={14} className="-ml-0.5" />
              </button>
              <button
                type="button"
                className={iconBtnClass()}
                title="Skip / next"
                aria-label="Skip to next"
                disabled={
                  selectedIndex < 0 || selectedIndex >= items.length - 1 || setCursor.isPending
                }
                onClick={() => goNext(wizardOpen)}
              >
                <ChevronRight size={16} />
              </button>
            </div>
          )}
        </div>

        {queueTab === "needs-review" ? (
          reviewLoading && items.length === 0 ? (
            <p className="text-sm text-gray-500">Loading queue...</p>
          ) : items.length === 0 ? (
            <p className="text-sm text-gray-500">No books waiting for review.</p>
          ) : (
            <ul className="space-y-2">
              {items.map((item) => {
                const active = item.id === selectedId;
                return (
                  <li key={item.id}>
                    <button
                      type="button"
                      onClick={() => selectItem(item.id, { openWizard: true })}
                      className={`w-full text-left flex gap-3 p-3 rounded-xl border transition-colors ${
                        active
                          ? "border-brand-600/50 bg-brand-950/30"
                          : "border-gray-800 bg-gray-950/40 hover:border-gray-700"
                      }`}
                    >
                      <div className="w-10 h-14 shrink-0 rounded overflow-hidden bg-gray-800">
                        {item.cover_url ? (
                          <CoverImage
                            src={item.cover_url}
                            alt=""
                            className="w-full h-full object-cover"
                          />
                        ) : null}
                      </div>
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-medium text-gray-100 truncate">
                          {item.title || `Request #${item.id}`}
                        </p>
                        {item.author && (
                          <p className="text-xs text-gray-400 truncate mt-0.5">{item.author}</p>
                        )}
                        {item.quarantine_reason && (
                          <p className="text-[11px] text-amber-400/90 mt-1 line-clamp-2">
                            {item.quarantine_reason}
                          </p>
                        )}
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )
        ) : queueTab === "unprocessed" ? (
          unprocessedLoading && unprocessedItems.length === 0 ? (
            <p className="text-sm text-gray-500">Loading unprocessed...</p>
          ) : unprocessedItems.length === 0 ? (
            <p className="text-sm text-gray-500">No cancelled, failed, or skipped sweep books.</p>
          ) : (
            <ul className="space-y-2">
              {unprocessedItems.map((item) => (
                <li
                  key={item.id}
                  className="flex gap-3 p-3 rounded-xl border border-gray-800 bg-gray-950/40"
                >
                  <div className="w-10 h-14 shrink-0 rounded overflow-hidden bg-gray-800">
                    {item.cover_url ? (
                      <CoverImage
                        src={item.cover_url}
                        alt=""
                        className="w-full h-full object-cover"
                      />
                    ) : null}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-gray-100 truncate">
                      {item.title || `Request #${item.id}`}
                    </p>
                    {item.author && (
                      <p className="text-xs text-gray-400 truncate mt-0.5">{item.author}</p>
                    )}
                    <p className={`text-[11px] mt-1 capitalize ${statusBadge(item.status)}`}>
                      {item.status}
                      {item.status_detail ? ` - ${item.status_detail}` : ""}
                    </p>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <button
                      type="button"
                      className={iconBtnClass()}
                      title="Reprocess (forge only)"
                      aria-label={`Reprocess ${item.title || item.id}`}
                      disabled={reprocessMutation.isPending || dismissMutation.isPending}
                      onClick={() => reprocessMutation.mutate(item.id)}
                    >
                      {reprocessMutation.isPending && reprocessMutation.variables === item.id ? (
                        <Loader2 size={16} className="animate-spin" />
                      ) : (
                        <RotateCcw size={16} />
                      )}
                    </button>
                    <button
                      type="button"
                      className={iconBtnClass()}
                      title="Remove from Unprocessed"
                      aria-label={`Remove ${item.title || item.id} from Unprocessed`}
                      disabled={reprocessMutation.isPending || dismissMutation.isPending}
                      onClick={() =>
                        setConfirmAction({
                          kind: "dismiss",
                          id: item.id,
                          title: item.title || `Request #${item.id}`,
                        })
                      }
                    >
                      {dismissMutation.isPending && dismissMutation.variables === item.id ? (
                        <Loader2 size={16} className="animate-spin" />
                      ) : (
                        <X size={16} />
                      )}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )
        ) : processedLoading && processedItems.length === 0 ? (
          <p className="text-sm text-gray-500">Loading processed...</p>
        ) : processedItems.length === 0 ? (
          <p className="text-sm text-gray-500">No successfully completed sweep books yet.</p>
        ) : (
          <div className="space-y-3">
            <ul className="space-y-2 max-h-[28rem] overflow-y-auto pr-0.5">
              {processedItems.map((item) => (
                <li
                  key={item.id}
                  className="flex gap-3 p-3 rounded-xl border border-gray-800 bg-gray-950/40"
                >
                  <div className="w-10 h-14 shrink-0 rounded overflow-hidden bg-gray-800">
                    {item.cover_url ? (
                      <CoverImage
                        src={item.cover_url}
                        alt=""
                        className="w-full h-full object-cover"
                      />
                    ) : null}
                  </div>
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-gray-100 truncate">
                      {item.title || `Request #${item.id}`}
                    </p>
                    {item.author && (
                      <p className="text-xs text-gray-400 truncate mt-0.5">{item.author}</p>
                    )}
                    <p className="text-[11px] text-emerald-400/90 mt-1">
                      Completed
                      {item.completed_at
                        ? ` - ${new Date(item.completed_at).toLocaleString()}`
                        : item.id
                          ? ` - #${item.id}`
                          : ""}
                    </p>
                  </div>
                </li>
              ))}
            </ul>
            {processedTotal > PROCESSED_PAGE_SIZE && (
              <div className="flex items-center justify-between gap-2">
                <button
                  type="button"
                  className={iconBtnClass()}
                  disabled={processedOffset <= 0 || processedLoading}
                  onClick={() => setProcessedOffset((o) => Math.max(0, o - PROCESSED_PAGE_SIZE))}
                  aria-label="Previous processed page"
                >
                  <ChevronLeft size={16} />
                </button>
                <span className="text-xs text-gray-500 tabular-nums">
                  {processedOffset + 1}-
                  {Math.min(processedOffset + processedItems.length, processedTotal)} of{" "}
                  {processedTotal}
                </span>
                <button
                  type="button"
                  className={iconBtnClass()}
                  disabled={
                    processedLoading || processedOffset + PROCESSED_PAGE_SIZE >= processedTotal
                  }
                  onClick={() => setProcessedOffset((o) => o + PROCESSED_PAGE_SIZE)}
                  aria-label="Next processed page"
                >
                  <ChevronRight size={16} />
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {selectedItem &&
        queueTab === "needs-review" &&
        renderReviewModal({
          item: selectedItem,
          open: wizardOpen,
          onClose: () => {
            setWizardOpen(false);
            invalidateAll();
          },
        })}

      <ConfirmModal
        show={confirmAction?.kind === "start"}
        title={`Start ${title} sweep?`}
        body={startConfirmText}
        confirmLabel="Start sweep"
        variant="warning"
        busy={startMutation.isPending}
        onCancel={() => setConfirmAction(null)}
        onConfirm={() => {
          setConfirmAction(null);
          startMutation.mutate();
        }}
      />
      <ConfirmModal
        show={confirmAction?.kind === "cancel"}
        title={`Cancel ${title} sweep?`}
        body={`Stop the current ${title} sweep? In-progress books may be left incomplete.`}
        confirmLabel="Cancel sweep"
        variant="danger"
        busy={cancelMutation.isPending}
        onCancel={() => setConfirmAction(null)}
        onConfirm={() => {
          setConfirmAction(null);
          cancelMutation.mutate();
        }}
      />
      <ConfirmModal
        show={confirmAction?.kind === "dismiss"}
        title="Remove from Unprocessed?"
        body={
          confirmAction?.kind === "dismiss" ? (
            <>Remove "{confirmAction.title}" from Unprocessed? Library files are kept.</>
          ) : null
        }
        confirmLabel="Remove"
        variant="danger"
        busy={dismissMutation.isPending}
        onCancel={() => setConfirmAction(null)}
        onConfirm={() => {
          if (confirmAction?.kind !== "dismiss") return;
          const id = confirmAction.id;
          setConfirmAction(null);
          dismissMutation.mutate(id);
        }}
      />
    </section>
  );
}

// ============================================================================
// Audiobook section
// ============================================================================

const AUDIOBOOK_BOOL_KEYS = [
  "config.library_sweep_skip_m4b",
  "config.library_sweep_force_metadata_forge",
  "config.library_sweep_force_chapter_forge",
  "config.library_sweep_force_folder_forge",
] as const;

const AUDIOBOOK_BOOL_LABELS: Record<
  (typeof AUDIOBOOK_BOOL_KEYS)[number],
  { label: string; help: string }
> = {
  "config.library_sweep_skip_m4b": {
    label: "Skip M4B",
    help: "Checked disables M4B conversion during Sweep. Leave unchecked for normal M4B runs.",
  },
  "config.library_sweep_force_metadata_forge": {
    label: "Force metadata forge",
    help: "Unchecked skips Metadata Forge when applied markers already exist.",
  },
  "config.library_sweep_force_chapter_forge": {
    label: "Force chapter forge",
    help: "Unchecked skips Chapter Forge when the .m4b already has chapter markers.",
  },
  "config.library_sweep_force_folder_forge": {
    label: "Force folder forge",
    help: "Unchecked skips Folder Forge when staging is already hardlinked into the library.",
  },
};

const AUDIOBOOK_MODAL_KEYS = [
  "config.libraforge_naming_template",
  "config.libraforge_metadata_provider",
  "config.library_sweep_abs_scan_every",
] as const;

const METADATA_PROVIDERS = [
  { value: "audible", label: "Audible" },
  { value: "graphicaudio", label: "Graphic Audio" },
  { value: "soundbooththeater", label: "Soundbooth Theater" },
] as const;

const AUDIOBOOK_START_CONFIRM =
  "Library Sweep will rewrite audiobook metadata and may queue M4B conversion for matching library books. Continue?";

function AudiobookAdvancedSettingsModal({
  show,
  onClose,
  allSettings,
}: {
  show: boolean;
  onClose: () => void;
  allSettings?: SweepSetting[];
}) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!show) return;
    const next: Record<string, string> = {};
    for (const key of AUDIOBOOK_MODAL_KEYS) {
      next[key] = allSettings?.find((s) => s.key === key)?.value ?? "";
    }
    setDraft(next);
    // Only reset when the modal opens - editing shouldn't be clobbered by refetches.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [show]);

  const dirty = AUDIOBOOK_MODAL_KEYS.some((key) => {
    const d = draft[key];
    if (d === undefined) return false;
    const original = allSettings?.find((s) => s.key === key)?.value ?? "";
    return d !== original;
  });

  const saveMutation = useMutation({
    mutationFn: async () => {
      const updates: Record<string, string> = {};
      for (const key of AUDIOBOOK_MODAL_KEYS) {
        const cur = draft[key];
        if (cur === undefined) continue;
        const original = allSettings?.find((s) => s.key === key)?.value ?? "";
        if (cur !== original) updates[key] = cur;
      }
      if (Object.keys(updates).length === 0) return null;
      const { data } = await api.put("/admin/config", { settings: updates });
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["admin-config"] });
      toast("Sweep settings saved", "success");
      onClose();
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      toast(err?.response?.data?.detail || "Failed to save settings", "error");
    },
  });

  return (
    <Modal title="Audiobook sweep settings" show={show} onClose={onClose} size="lg">
      <p className="text-xs text-gray-500 mb-3">
        Folder Forge naming template, default metadata provider, and ABS scan cadence.
      </p>
      <div className="flex justify-end mb-3">
        <button
          type="button"
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-brand-600/50 bg-brand-600/20 text-brand-200 text-xs font-medium disabled:opacity-40"
          disabled={!dirty || saveMutation.isPending}
          onClick={() => saveMutation.mutate()}
        >
          {saveMutation.isPending ? (
            <Loader2 size={14} className="animate-spin" />
          ) : (
            <Check size={14} />
          )}
          Save
        </button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="block space-y-1 sm:col-span-2">
          <span className="text-xs text-gray-400">Folder Forge naming template</span>
          <NamingTemplateBuilder
            value={
              draft["config.libraforge_naming_template"] ??
              "{author}/{series} [{edition}]/{title}/{filename}"
            }
            onChange={(next) =>
              setDraft((d) => ({ ...d, "config.libraforge_naming_template": next }))
            }
          />
        </div>
        <label className="block space-y-1">
          <span className="text-xs text-gray-400">Default metadata provider</span>
          <select
            className="w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100"
            value={draft["config.libraforge_metadata_provider"] || "audible"}
            onChange={(e) =>
              setDraft((d) => ({ ...d, "config.libraforge_metadata_provider": e.target.value }))
            }
          >
            {METADATA_PROVIDERS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
          <span className="text-[11px] text-gray-500">
            On a miss: Graphic Audio - Soundbooth Theater
          </span>
        </label>
        <label className="block space-y-1">
          <span className="text-xs text-gray-400">ABS scan every N completed books</span>
          <input
            type="number"
            min={1}
            className="w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100"
            value={draft["config.library_sweep_abs_scan_every"] ?? "25"}
            onChange={(e) =>
              setDraft((d) => ({ ...d, "config.library_sweep_abs_scan_every": e.target.value }))
            }
          />
          <span className="text-[11px] text-gray-500">
            Also scans when Sweep completes, pauses, cancels, or stops
          </span>
        </label>
      </div>
    </Modal>
  );
}

function AudiobookSection() {
  const { data: allSettings } = useAdminConfigSettings();
  const saveConfig = useSaveConfigKeys();
  const [settingsOpen, setSettingsOpen] = useState(false);

  const boolValues = useMemo(() => {
    const out: Record<string, boolean> = {};
    for (const key of AUDIOBOOK_BOOL_KEYS) {
      const found = allSettings?.find((s) => s.key === key)?.value;
      out[key] = (found ?? "false") === "true";
    }
    return out;
  }, [allSettings]);

  const toggles = (
    <div className="grid gap-2 sm:grid-cols-2">
      {AUDIOBOOK_BOOL_KEYS.map((key) => (
        <InlineBoolToggle
          key={key}
          label={AUDIOBOOK_BOOL_LABELS[key].label}
          help={AUDIOBOOK_BOOL_LABELS[key].help}
          checked={boolValues[key]}
          disabled={saveConfig.isPending}
          onChange={(checked) => saveConfig.mutate({ [key]: checked ? "true" : "false" })}
        />
      ))}
      <button
        type="button"
        onClick={() => setSettingsOpen(true)}
        className="sm:col-span-2 inline-flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg border border-gray-700 text-xs text-gray-300 hover:bg-gray-800 hover:text-gray-100"
      >
        <Settings size={14} />
        Naming template, metadata provider &amp; ABS scan cadence...
      </button>
      <AudiobookAdvancedSettingsModal
        show={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        allSettings={allSettings}
      />
    </div>
  );

  return (
    <MediumSweepSection
      medium="audiobook"
      basePath="/admin/library-sweep"
      icon={Headphones}
      title="Audiobooks"
      description="Walk existing ABS audiobooks through the forge pipeline (no re-download). M4B encodes queue globally (one at a time) while the sweep keeps scanning. Books that need judgment land in Needs review; cancelled / failed / skipped land in Unprocessed."
      startConfirmText={AUDIOBOOK_START_CONFIRM}
      showM4bCounter
      toggles={toggles}
      renderReviewModal={({ item, open, onClose }) => (
        <QuickReviewWizard
          key={item.id}
          requestId={item.id}
          title={item.title || `Request #${item.id}`}
          open={open}
          onClose={onClose}
        />
      )}
    />
  );
}

// ============================================================================
// Ebook section
// ============================================================================

const EBOOK_BOOL_KEYS = [
  "config.ebook_sweep_convert_all_to_epub",
  "config.ebook_sweep_force_metadata",
] as const;

const EBOOK_BOOL_LABELS: Record<(typeof EBOOK_BOOL_KEYS)[number], { label: string; help: string }> =
  {
    "config.ebook_sweep_convert_all_to_epub": {
      label: "Convert all to EPUB",
      help: "Converts PDF/MOBI/AZW/FB2/TXT to EPUB via Calibre. Comic archives (CBZ/CBR) are left as-is. Default on.",
    },
    "config.ebook_sweep_force_metadata": {
      label: "Force metadata (Hardcover to OL)",
      help: "Re-identify every ebook via Hardcover then Open Library, even if already organized. Default on.",
    },
  };

const EBOOK_KAVITA_SCAN_KEY = "config.ebook_sweep_kavita_scan_every";

const EBOOK_START_CONFIRM =
  "Ebook Sweep will organize/convert ebooks in place and may rewrite metadata for matching library books. Continue?";

function EbookSection() {
  const { data: allSettings } = useAdminConfigSettings();
  const saveConfig = useSaveConfigKeys();
  const [scanEveryDraft, setScanEveryDraft] = useState<string | null>(null);

  const boolValues = useMemo(() => {
    const out: Record<string, boolean> = {};
    for (const key of EBOOK_BOOL_KEYS) {
      const found = allSettings?.find((s) => s.key === key)?.value;
      out[key] = (found ?? "true") === "true";
    }
    return out;
  }, [allSettings]);

  const savedScanEvery = allSettings?.find((s) => s.key === EBOOK_KAVITA_SCAN_KEY)?.value ?? "25";
  const scanEveryValue = scanEveryDraft ?? savedScanEvery;

  const toggles = (
    <div className="grid gap-2 sm:grid-cols-2">
      {EBOOK_BOOL_KEYS.map((key) => (
        <InlineBoolToggle
          key={key}
          label={EBOOK_BOOL_LABELS[key].label}
          help={EBOOK_BOOL_LABELS[key].help}
          checked={boolValues[key]}
          disabled={saveConfig.isPending}
          onChange={(checked) => saveConfig.mutate({ [key]: checked ? "true" : "false" })}
        />
      ))}
      <label className="flex items-center gap-2 rounded-lg border border-gray-800 bg-gray-950/40 px-3 py-2.5 sm:col-span-2">
        <span className="min-w-0 flex-1">
          <span className="block text-xs font-medium text-gray-200">
            Kavita scan every N books
          </span>
          <span className="block text-[11px] text-gray-500 mt-0.5">
            Full Kavita library scan after this many completed Ebook Sweep books (also on
            complete / pause / cancel).
          </span>
        </span>
        <input
          type="number"
          min={1}
          className="w-20 shrink-0 rounded-lg border border-gray-700 bg-gray-950 px-2 py-1.5 text-sm text-gray-100"
          value={scanEveryValue}
          disabled={saveConfig.isPending}
          onChange={(e) => setScanEveryDraft(e.target.value)}
          onBlur={() => {
            if (scanEveryDraft == null) return;
            const trimmed = scanEveryDraft.trim();
            if (trimmed && trimmed !== savedScanEvery) {
              saveConfig.mutate({ [EBOOK_KAVITA_SCAN_KEY]: trimmed });
            }
            setScanEveryDraft(null);
          }}
        />
      </label>
    </div>
  );

  return (
    <MediumSweepSection
      medium="ebook"
      basePath="/admin/library-sweep/ebook"
      icon={BookOpen}
      title="Ebooks"
      description="Walk the existing ebook library through the DIY organizer (identify, convert/embed, organize, Kavita scan) - no re-download. Books that need judgment land in Needs review; cancelled / failed / skipped land in Unprocessed."
      startConfirmText={EBOOK_START_CONFIRM}
      showM4bCounter={false}
      toggles={toggles}
      renderReviewModal={({ item, open, onClose }) => (
        <EbookMetadataMatcher
          key={item.id}
          requestId={item.id}
          title={item.title || `Request #${item.id}`}
          open={open}
          onClose={onClose}
        />
      )}
    />
  );
}

// ============================================================================
// Folder cleanup section
// ============================================================================

type CleanupCandidate = {
  path: string;
  kind: string;
  reason: string;
  size_bytes: number;
  scope: "audiobook" | "ebook";
};

type CleanupPreview = {
  token: string;
  scopes: string[];
  protected_roots: string[];
  count: number;
  total_bytes: number;
  candidates: CleanupCandidate[];
  canonical?: Record<string, unknown>;
  expires_in_seconds: number;
};

type CleanupApplyResult = {
  ok: boolean;
  deleted: string[];
  deleted_count: number;
  errors: { path: string; error: string }[];
};

function CleanupSection() {
  const { toast } = useToast();
  const [scopeAudiobook, setScopeAudiobook] = useState(true);
  const [scopeEbook, setScopeEbook] = useState(true);
  const [preview, setPreview] = useState<CleanupPreview | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmApply, setConfirmApply] = useState(false);

  const previewMutation = useMutation({
    mutationFn: async () => {
      const scopes = [
        ...(scopeAudiobook ? ["audiobook"] : []),
        ...(scopeEbook ? ["ebook"] : []),
      ];
      const { data } = await api.post("/admin/library-sweep/cleanup/preview", { scopes });
      return data as CleanupPreview;
    },
    onSuccess: (data) => {
      setPreview(data);
      setSelected(new Set(data.candidates.map((c) => c.path)));
      setConfirmApply(false);
      if (data.count === 0) toast("No orphaned files or folders found", "success");
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      toast(err?.response?.data?.detail || "Cleanup preview failed", "error");
    },
  });

  const applyMutation = useMutation({
    mutationFn: async () => {
      if (!preview) throw new Error("No preview to apply");
      const allSelected = selected.size === preview.candidates.length;
      const { data } = await api.post("/admin/library-sweep/cleanup/apply", {
        token: preview.token,
        ...(allSelected ? {} : { paths: Array.from(selected) }),
      });
      return data as CleanupApplyResult;
    },
    onSuccess: (data) => {
      toast(
        data.errors.length
          ? `Deleted ${data.deleted_count} item(s), ${data.errors.length} error(s)`
          : `Deleted ${data.deleted_count} item(s)`,
        data.errors.length ? "error" : "success",
      );
      setPreview(null);
      setSelected(new Set());
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      toast(err?.response?.data?.detail || "Cleanup apply failed", "error");
    },
  });

  const toggleSelected = (path: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const candidates = preview?.candidates ?? [];
  const noScopeSelected = !scopeAudiobook && !scopeEbook;

  return (
    <section className="rounded-xl border border-gray-800 bg-gray-900/50 p-4 space-y-4">
      <div>
        <h3 className="text-base font-semibold text-gray-100 flex items-center gap-2">
          <Trash2 size={18} className="text-brand-400" />
          Folder cleanup
        </h3>
        <p className="text-xs text-gray-500 mt-1 max-w-2xl">
          Dry-run scan for non-canonical leftovers under the library roots: duplicate .m4b files,
          multipart audio left after conversion, numbered ebook duplicates, junk files, and empty
          folders. Canonical layout is{" "}
          <code className="text-gray-400">{"{author}/{series} [{edition}]/{title}/"}</code> under
          each library root; staging folders are always protected. Nothing is deleted without an
          explicit confirmation below.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-4">
        <label className="inline-flex items-center gap-2 text-sm text-gray-300">
          <input
            type="checkbox"
            className="rounded border-gray-600 bg-gray-900"
            checked={scopeAudiobook}
            onChange={(e) => setScopeAudiobook(e.target.checked)}
          />
          Audiobook library
        </label>
        <label className="inline-flex items-center gap-2 text-sm text-gray-300">
          <input
            type="checkbox"
            className="rounded border-gray-600 bg-gray-900"
            checked={scopeEbook}
            onChange={(e) => setScopeEbook(e.target.checked)}
          />
          Ebook library
        </label>
        <button
          type="button"
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-brand-600/50 bg-brand-600/20 text-brand-200 text-xs font-medium disabled:opacity-40"
          disabled={noScopeSelected || previewMutation.isPending}
          onClick={() => previewMutation.mutate()}
        >
          {previewMutation.isPending ? (
            <Loader2 size={14} className="animate-spin" />
          ) : (
            <Search size={14} />
          )}
          Preview cleanup
        </button>
        {preview && (
          <button
            type="button"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-red-600/50 bg-red-600/20 text-red-200 text-xs font-medium disabled:opacity-40"
            disabled={selected.size === 0 || applyMutation.isPending}
            onClick={() => setConfirmApply(true)}
          >
            <Trash2 size={14} />
            Delete {selected.size} item{selected.size === 1 ? "" : "s"}
          </button>
        )}
      </div>

      {preview && (
        <div className="space-y-2">
          <p className="text-xs text-gray-500">
            {preview.count} candidate{preview.count === 1 ? "" : "s"} -{" "}
            {formatBytes(preview.total_bytes)} total
            {candidates.length > 0 && (
              <>
                {" "}
                -{" "}
                <button
                  type="button"
                  className="underline hover:text-gray-300"
                  onClick={() => setSelected(new Set(candidates.map((c) => c.path)))}
                >
                  Select all
                </button>{" "}
                <button
                  type="button"
                  className="underline hover:text-gray-300"
                  onClick={() => setSelected(new Set())}
                >
                  Select none
                </button>
              </>
            )}
          </p>
          {candidates.length === 0 ? (
            <p className="text-sm text-gray-500">No orphaned files or folders found.</p>
          ) : (
            <ul className="space-y-1.5 max-h-96 overflow-y-auto pr-0.5">
              {candidates.map((c) => (
                <li
                  key={c.path}
                  className="flex items-start gap-2.5 rounded-lg border border-gray-800 bg-gray-950/40 px-3 py-2"
                >
                  <input
                    type="checkbox"
                    className="mt-0.5 rounded border-gray-600 bg-gray-900"
                    checked={selected.has(c.path)}
                    onChange={() => toggleSelected(c.path)}
                  />
                  <div className="min-w-0 flex-1">
                    <p className="text-xs text-gray-300 font-mono truncate" title={c.path}>
                      {c.path}
                    </p>
                    <p className="text-[11px] text-gray-500 mt-0.5">
                      <span className="uppercase tracking-wide text-gray-600">
                        {c.kind.replace(/_/g, " ")}
                      </span>
                      {" - "}
                      {c.reason}
                      {c.size_bytes > 0 && <> - {formatBytes(c.size_bytes)}</>}
                    </p>
                  </div>
                  <span
                    className={`text-[10px] px-1.5 py-0.5 rounded border shrink-0 ${
                      c.scope === "audiobook"
                        ? "border-sky-800/50 bg-sky-950/40 text-sky-300"
                        : "border-amber-800/40 bg-amber-950/30 text-amber-300"
                    }`}
                  >
                    {c.scope}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <ConfirmModal
        show={confirmApply}
        title="Delete selected items?"
        body={`Permanently delete ${selected.size} item(s) from the library? This cannot be undone.`}
        confirmLabel={`Delete ${selected.size}`}
        variant="danger"
        busy={applyMutation.isPending}
        onCancel={() => setConfirmApply(false)}
        onConfirm={() => {
          setConfirmApply(false);
          applyMutation.mutate();
        }}
      />
    </section>
  );
}

// ============================================================================
// Page
// ============================================================================

export default function LibrarySweepTab() {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold text-gray-100 flex items-center gap-2">
          <Wand2 size={20} className="text-brand-400 shrink-0" />
          Library Sweep
        </h2>
        <p className="text-xs text-gray-500 mt-1 max-w-2xl">
          Backfill existing audiobooks and ebooks through the forge / DIY organizer pipelines
          in place (no re-download), then clean up non-canonical leftovers once everything is
          organized.
        </p>
      </div>

      <AudiobookSection />
      <EbookSection />
      <CleanupSection />
    </div>
  );
}
