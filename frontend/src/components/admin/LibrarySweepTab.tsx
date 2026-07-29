import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
} from "lucide-react";
import api from "../../api/client";
import { useToast } from "../../contexts/ToastContext";
import CoverImage from "../CoverImage";
import ConfirmModal from "../ConfirmModal";
import Modal from "../Modal";
import QuickReviewWizard from "./QuickReviewWizard";
import NamingTemplateBuilder from "./NamingTemplateBuilder";

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
  abs_item_id: string | null;
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
  abs_item_id: string | null;
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

const STATUS_KEY = ["admin-library-sweep-status"] as const;
const REVIEW_KEY = ["admin-library-sweep-needs-review"] as const;
const UNPROCESSED_KEY = ["admin-library-sweep-unprocessed"] as const;
const PROCESSED_KEY = ["admin-library-sweep-processed"] as const;
const PROCESSED_PAGE_SIZE = 40;
const SWEEP_SETTINGS_KEYS = [
  "config.libraforge_naming_template",
  "config.libraforge_metadata_provider",
  "config.library_sweep_abs_scan_every",
  "config.library_sweep_skip_m4b",
  "config.library_sweep_force_metadata_forge",
  "config.library_sweep_force_chapter_forge",
  "config.library_sweep_force_folder_forge",
] as const;

const SWEEP_BOOL_KEYS = [
  "config.library_sweep_skip_m4b",
  "config.library_sweep_force_metadata_forge",
  "config.library_sweep_force_chapter_forge",
  "config.library_sweep_force_folder_forge",
] as const;

const SWEEP_BOOL_LABELS: Record<(typeof SWEEP_BOOL_KEYS)[number], { label: string; help: string }> = {
  "config.library_sweep_skip_m4b": {
    label: "Skip M4B processing",
    help: "Checked disables M4B conversion during Sweep. Leave unchecked for normal M4B runs.",
  },
  "config.library_sweep_force_metadata_forge": {
    label: "Force metadata forging",
    help: "Unchecked skips Metadata Forge when applied markers already exist.",
  },
  "config.library_sweep_force_chapter_forge": {
    label: "Force chapter forging",
    help: "Unchecked skips Chapter Forge when the .m4b already has chapter markers.",
  },
  "config.library_sweep_force_folder_forge": {
    label: "Force folder forging",
    help: "Unchecked skips Folder Forge when staging is already hardlinked into the library.",
  },
};

type SweepConfirm =
  | { kind: "start" }
  | { kind: "cancel" }
  | { kind: "dismiss"; id: number; title: string };

const METADATA_PROVIDERS = [
  { value: "audible", label: "Audible" },
  { value: "graphicaudio", label: "Graphic Audio" },
  { value: "soundbooththeater", label: "Soundbooth Theater" },
] as const;

const START_CONFIRM =
  "Library Sweep will rewrite audiobook metadata and may queue M4B conversion for matching library books. Continue?";

type SweepSetting = {
  key: string;
  label: string;
  value: string;
  help?: string;
  placeholder?: string;
  valueType?: string;
};

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

export default function LibrarySweepTab() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [cursorRestored, setCursorRestored] = useState(false);
  const [queueTab, setQueueTab] = useState<QueueTab>("needs-review");
  const [settingsDraft, setSettingsDraft] = useState<Record<string, string>>({});
  const [processedOffset, setProcessedOffset] = useState(0);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [confirmAction, setConfirmAction] = useState<SweepConfirm | null>(null);

  const { data: status, isLoading: statusLoading, refetch: refetchStatus } = useQuery({
    queryKey: STATUS_KEY,
    queryFn: async () => {
      const { data } = await api.get("/admin/library-sweep/status");
      return data as SweepStatus;
    },
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "running" ? 2500 : 15_000;
    },
  });

  const { data: sweepSettings, isLoading: settingsLoading } = useQuery({
    queryKey: ["admin-library-sweep-settings"],
    queryFn: async () => {
      const { data } = await api.get("/admin/config");
      const all = (data?.settings || []) as SweepSetting[];
      return all.filter((s) =>
        (SWEEP_SETTINGS_KEYS as readonly string[]).includes(s.key),
      );
    },
  });

  useEffect(() => {
    if (!sweepSettings) return;
    const next: Record<string, string> = {};
    for (const s of sweepSettings) {
      next[s.key] = s.value ?? "";
    }
    // Defaults for bools when not yet in DB (all are deviations → default false)
    for (const key of SWEEP_BOOL_KEYS) {
      if (next[key] === undefined || next[key] === "") {
        next[key] = "false";
      }
    }
    setSettingsDraft((prev) => {
      const merged = { ...next };
      for (const k of Object.keys(prev)) {
        if (prev[k] !== undefined && next[k] !== undefined && prev[k] !== next[k]) {
          const loaded = sweepSettings.find((s) => s.key === k)?.value ?? next[k] ?? "";
          if (prev[k] !== loaded) merged[k] = prev[k];
        }
      }
      return merged;
    });
  }, [sweepSettings]);

  const saveSettings = useMutation({
    mutationFn: async () => {
      const updates: Record<string, string> = {};
      for (const key of SWEEP_SETTINGS_KEYS) {
        const cur = settingsDraft[key];
        if (cur === undefined) continue;
        const original = sweepSettings?.find((s) => s.key === key)?.value ?? "";
        if (cur !== original) updates[key] = cur;
      }
      if (Object.keys(updates).length === 0) return { settings: sweepSettings };
      const { data } = await api.put("/admin/config", { settings: updates });
      return data as { settings?: SweepSetting[] };
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["admin-library-sweep-settings"] });
      void queryClient.invalidateQueries({ queryKey: ["admin-config"] });
      toast("Sweep settings saved", "success");
      setSettingsOpen(false);
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      toast(err?.response?.data?.detail || "Failed to save settings", "error");
    },
  });

  const settingsDirty = useMemo(() => {
    if (!sweepSettings) return false;
    return SWEEP_SETTINGS_KEYS.some((key) => {
      const draft = settingsDraft[key];
      if (draft === undefined) return false;
      const original = sweepSettings.find((s) => s.key === key)?.value ?? "";
      return draft !== original;
    });
  }, [settingsDraft, sweepSettings]);

  const { data: review, isLoading: reviewLoading, refetch: refetchReview } = useQuery({
    queryKey: REVIEW_KEY,
    queryFn: async () => {
      const { data } = await api.get("/admin/library-sweep/needs-review");
      return data as NeedsReviewResponse;
    },
    refetchInterval: (q) => {
      const s = status?.status;
      return s === "running" ? 5000 : 20_000;
    },
  });

  const {
    data: unprocessed,
    isLoading: unprocessedLoading,
    refetch: refetchUnprocessed,
  } = useQuery({
    queryKey: UNPROCESSED_KEY,
    queryFn: async () => {
      const { data } = await api.get("/admin/library-sweep/unprocessed");
      return data as UnprocessedResponse;
    },
    refetchInterval: (q) => {
      const s = status?.status;
      return s === "running" ? 8000 : 30_000;
    },
  });

  const {
    data: processed,
    isLoading: processedLoading,
    refetch: refetchProcessed,
  } = useQuery({
    queryKey: [...PROCESSED_KEY, processedOffset],
    queryFn: async () => {
      const { data } = await api.get("/admin/library-sweep/processed", {
        params: { limit: PROCESSED_PAGE_SIZE, offset: processedOffset },
      });
      return data as ProcessedResponse;
    },
    enabled: queueTab === "processed",
    refetchInterval: (q) => {
      const s = status?.status;
      return s === "running" ? 10_000 : 45_000;
    },
  });

  const items = review?.items ?? [];
  const unprocessedItems = unprocessed?.items ?? [];
  const processedItems = processed?.items ?? [];
  const processedTotal =
    processed?.total ?? status?.processed_total ?? 0;
  const selectedIndex = useMemo(() => {
    if (selectedId == null) return -1;
    return items.findIndex((i) => i.id === selectedId);
  }, [items, selectedId]);
  const selectedItem = selectedIndex >= 0 ? items[selectedIndex] : null;

  const setCursor = useMutation({
    mutationFn: async (requestId: number | null) => {
      const { data } = await api.put("/admin/library-sweep/review-cursor", {
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
    const cursor =
      review?.review_cursor_request_id ?? status?.review_cursor_request_id ?? null;
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
      const { data } = await api.post("/admin/library-sweep/start");
      return data as SweepStatus & { message?: string };
    },
    onSuccess: (data) => {
      queryClient.setQueryData(STATUS_KEY, data);
      invalidateAll();
      toast(data.message || "Library Sweep started", "success");
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      toast(err?.response?.data?.detail || "Failed to start sweep", "error");
    },
  });

  const pauseMutation = useMutation({
    mutationFn: async () => {
      const { data } = await api.post("/admin/library-sweep/pause");
      return data as SweepStatus;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(STATUS_KEY, data);
      toast("Library Sweep paused", "info");
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      toast(err?.response?.data?.detail || "Failed to pause sweep", "error");
    },
  });

  const cancelMutation = useMutation({
    mutationFn: async () => {
      const { data } = await api.post("/admin/library-sweep/cancel");
      return data as SweepStatus;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(STATUS_KEY, data);
      invalidateAll();
      toast("Library Sweep cancelled", "info");
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      toast(err?.response?.data?.detail || "Failed to cancel sweep", "error");
    },
  });

  const skipMutation = useMutation({
    mutationFn: async (requestId?: number | null) => {
      const { data } = await api.post("/admin/library-sweep/skip", {
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
      const { data } = await api.post(`/admin/library-sweep/reprocess/${requestId}`);
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
            ? "Cannot reprocess — staging folder missing and ABS restage failed"
            : reason
              ? `Reprocess failed: ${reason}`
              : "Reprocess failed",
          "error",
        );
        return;
      }
      // Stay on Unprocessed — do not open Needs Review / set review cursor.
      toast("Reprocess started for this book", "success");
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      toast(err?.response?.data?.detail || "Failed to reprocess", "error");
    },
  });

  const dismissMutation = useMutation({
    mutationFn: async (requestId: number) => {
      const { data } = await api.post(`/admin/library-sweep/dismiss/${requestId}`);
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
    { label: "Status", value: status?.status ?? "—" },
    { label: "Total", value: status?.total ?? 0 },
    { label: "Scanned", value: status?.scanned ?? 0 },
    { label: "Auto-applied", value: status?.auto_applied ?? 0 },
    { label: "Needs review", value: status?.needs_review ?? 0 },
    { label: "Failed", value: status?.failed ?? 0 },
    { label: "M4B queued", value: status?.m4b_queued ?? 0 },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-lg font-semibold text-gray-100 flex items-center gap-2">
            <Wand2 size={20} className="text-brand-400 shrink-0" />
            Library Sweep
          </h2>
          <p className="text-xs text-gray-500 mt-1 max-w-xl">
            Walk existing ABS audiobooks through the forge pipeline (no re-download). M4B encodes
            queue globally (one at a time) while the sweep keeps scanning. Books that need judgment
            land in Needs review; cancelled / failed / skipped land in Unprocessed.
          </p>
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
          <button
            type="button"
            className={iconBtnClass(settingsOpen)}
            title="Sweep settings"
            aria-label="Sweep settings"
            onClick={() => setSettingsOpen(true)}
          >
            <Settings size={16} />
          </button>
        </div>
      </div>

      {statusLoading && !status ? (
        <p className="text-sm text-gray-500">Loading sweep status…</p>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-2">
          {counters.map((c) => (
            <div
              key={c.label}
              className="rounded-xl border border-gray-800 bg-gray-900/50 px-3 py-2.5"
            >
              <p className="text-[10px] uppercase tracking-wider text-gray-500">{c.label}</p>
              <p className="text-sm font-semibold text-gray-100 mt-0.5 truncate capitalize">
                {c.value}
              </p>
            </div>
          ))}
        </div>
      )}

      <Modal
        title="Sweep settings"
        show={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        size="lg"
      >
        <p className="text-xs text-gray-500 mb-3">
          Folder Forge naming, metadata provider, forge force toggles, and ABS scan cadence.
        </p>
        <div className="flex justify-end mb-3">
          <button
            type="button"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-brand-600/50 bg-brand-600/20 text-brand-200 text-xs font-medium disabled:opacity-40"
            disabled={!settingsDirty || saveSettings.isPending || settingsLoading}
            onClick={() => saveSettings.mutate()}
          >
            {saveSettings.isPending ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <Check size={14} />
            )}
            Save
          </button>
        </div>
        {settingsLoading && !sweepSettings ? (
          <p className="text-xs text-gray-500">Loading settings…</p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="sm:col-span-2 grid gap-2 sm:grid-cols-2">
              {SWEEP_BOOL_KEYS.map((key) => {
                const meta = SWEEP_BOOL_LABELS[key];
                const checked = (settingsDraft[key] ?? "false") === "true";
                return (
                  <label
                    key={key}
                    className="flex items-start gap-2 rounded-lg border border-gray-800 bg-gray-950/40 px-3 py-2.5"
                  >
                    <input
                      type="checkbox"
                      className="mt-0.5 rounded border-gray-600 bg-gray-900"
                      checked={checked}
                      onChange={(e) =>
                        setSettingsDraft((d) => ({
                          ...d,
                          [key]: e.target.checked ? "true" : "false",
                        }))
                      }
                    />
                    <span className="min-w-0">
                      <span className="block text-xs font-medium text-gray-200">{meta.label}</span>
                      <span className="block text-[11px] text-gray-500 mt-0.5">{meta.help}</span>
                    </span>
                  </label>
                );
              })}
            </div>
            <div className="block space-y-1 sm:col-span-2">
              <span className="text-xs text-gray-400">Folder Forge naming template</span>
              <NamingTemplateBuilder
                value={
                  settingsDraft["config.libraforge_naming_template"] ??
                  "{author}/{series} [{edition}]/{title}/{filename}"
                }
                onChange={(next) =>
                  setSettingsDraft((d) => ({
                    ...d,
                    "config.libraforge_naming_template": next,
                  }))
                }
              />
            </div>
            <label className="block space-y-1">
              <span className="text-xs text-gray-400">Default metadata provider</span>
              <select
                className="w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100"
                value={settingsDraft["config.libraforge_metadata_provider"] || "audible"}
                onChange={(e) =>
                  setSettingsDraft((d) => ({
                    ...d,
                    "config.libraforge_metadata_provider": e.target.value,
                  }))
                }
              >
                {METADATA_PROVIDERS.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
              <span className="text-[11px] text-gray-500">
                On a miss: Graphic Audio → Soundbooth Theater
              </span>
            </label>
            <label className="block space-y-1">
              <span className="text-xs text-gray-400">ABS scan every N completed books</span>
              <input
                type="number"
                min={1}
                className="w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100"
                value={settingsDraft["config.library_sweep_abs_scan_every"] ?? "25"}
                onChange={(e) =>
                  setSettingsDraft((d) => ({
                    ...d,
                    "config.library_sweep_abs_scan_every": e.target.value,
                  }))
                }
              />
              <span className="text-[11px] text-gray-500">
                Also scans when Sweep completes, pauses, cancels, or stops
              </span>
            </label>
          </div>
        )}
      </Modal>

      {(running || paused || current) && (
        <div className="rounded-xl border border-gray-800 bg-gray-900/60 p-3 flex gap-3 items-center">
          <div className="w-14 h-20 shrink-0 rounded overflow-hidden bg-gray-800">
            {current?.cover_url ? (
              <CoverImage
                src={current.cover_url}
                alt=""
                className="w-full h-full object-cover"
              />
            ) : (
              <div className="w-full h-full flex items-center justify-center text-gray-600 text-xs">
                —
              </div>
            )}
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-[10px] uppercase tracking-wider text-gray-500">Processing now</p>
            <p className="text-sm font-semibold text-gray-100 truncate mt-0.5">
              {current?.title || (running ? "Scanning library…" : "—")}
            </p>
            {current?.author && (
              <p className="text-xs text-gray-400 truncate mt-0.5">{current.author}</p>
            )}
            {current?.status && (
              <p className={`text-[11px] mt-1 capitalize ${statusBadge(current.status)}`}>
                {current.status.replace(/_/g, " ")}
                {current.request_id != null ? ` · #${current.request_id}` : ""}
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
        <div className="rounded-xl border border-gray-800/80 bg-gray-900/40 p-3 flex gap-3 items-center">
          <div className="w-12 h-[4.25rem] shrink-0 rounded overflow-hidden bg-gray-800">
            {upNext.cover_url ? (
              <CoverImage
                src={upNext.cover_url}
                alt=""
                className="w-full h-full object-cover"
              />
            ) : (
              <div className="w-full h-full flex items-center justify-center text-gray-600 text-xs">
                —
              </div>
            )}
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-[10px] uppercase tracking-wider text-gray-500">Up next</p>
            <p className="text-sm font-medium text-gray-200 truncate mt-0.5">
              {upNext.title || "—"}
            </p>
            {upNext.author && (
              <p className="text-xs text-gray-400 truncate mt-0.5">{upNext.author}</p>
            )}
          </div>
        </div>
      )}

      {(running || paused) && !upNext && (
        <div className="rounded-xl border border-dashed border-gray-800 bg-gray-900/20 px-3 py-2">
          <p className="text-[10px] uppercase tracking-wider text-gray-500">Up next</p>
          <p className="text-xs text-gray-500 mt-0.5">
            {running ? "Looking ahead…" : "Nothing queued after the current book"}
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
                disabled={selectedIndex < 0 || selectedIndex >= items.length - 1 || setCursor.isPending}
                onClick={() => goNext(wizardOpen)}
              >
                <ChevronRight size={16} />
              </button>
            </div>
          )}
        </div>

        {queueTab === "needs-review" ? (
          reviewLoading && items.length === 0 ? (
            <p className="text-sm text-gray-500">Loading queue…</p>
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
                          : "border-gray-800 bg-gray-900/40 hover:border-gray-700"
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
            <p className="text-sm text-gray-500">Loading unprocessed…</p>
          ) : unprocessedItems.length === 0 ? (
            <p className="text-sm text-gray-500">No cancelled, failed, or skipped sweep books.</p>
          ) : (
            <ul className="space-y-2">
              {unprocessedItems.map((item) => (
                <li
                  key={item.id}
                  className="flex gap-3 p-3 rounded-xl border border-gray-800 bg-gray-900/40"
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
                      {item.status_detail ? ` · ${item.status_detail}` : ""}
                    </p>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <button
                      type="button"
                      className={iconBtnClass()}
                      title="Reprocess (forge only)"
                      aria-label={`Reprocess ${item.title || item.id}`}
                      disabled={
                        reprocessMutation.isPending || dismissMutation.isPending
                      }
                      onClick={() => reprocessMutation.mutate(item.id)}
                    >
                      {reprocessMutation.isPending &&
                      reprocessMutation.variables === item.id ? (
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
                      disabled={
                        reprocessMutation.isPending || dismissMutation.isPending
                      }
                      onClick={() =>
                        setConfirmAction({
                          kind: "dismiss",
                          id: item.id,
                          title: item.title || `Request #${item.id}`,
                        })
                      }
                    >
                      {dismissMutation.isPending &&
                      dismissMutation.variables === item.id ? (
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
          <p className="text-sm text-gray-500">Loading processed…</p>
        ) : processedItems.length === 0 ? (
          <p className="text-sm text-gray-500">No successfully completed sweep books yet.</p>
        ) : (
          <div className="space-y-3">
            <ul className="space-y-2 max-h-[28rem] overflow-y-auto pr-0.5">
              {processedItems.map((item) => (
                <li
                  key={item.id}
                  className="flex gap-3 p-3 rounded-xl border border-gray-800 bg-gray-900/40"
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
                        ? ` · ${new Date(item.completed_at).toLocaleString()}`
                        : item.id
                          ? ` · #${item.id}`
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
                  onClick={() =>
                    setProcessedOffset((o) => Math.max(0, o - PROCESSED_PAGE_SIZE))
                  }
                  aria-label="Previous processed page"
                >
                  <ChevronLeft size={16} />
                </button>
                <span className="text-xs text-gray-500 tabular-nums">
                  {processedOffset + 1}–
                  {Math.min(processedOffset + processedItems.length, processedTotal)} of{" "}
                  {processedTotal}
                </span>
                <button
                  type="button"
                  className={iconBtnClass()}
                  disabled={
                    processedLoading ||
                    processedOffset + PROCESSED_PAGE_SIZE >= processedTotal
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

      {selectedItem && queueTab === "needs-review" && (
        <QuickReviewWizard
          requestId={selectedItem.id}
          title={selectedItem.title || `Request #${selectedItem.id}`}
          open={wizardOpen}
          onClose={() => {
            setWizardOpen(false);
            invalidateAll();
          }}
        />
      )}

      <ConfirmModal
        show={confirmAction?.kind === "start"}
        title="Start Library Sweep?"
        body={START_CONFIRM}
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
        title="Cancel Library Sweep?"
        body="Stop the current Library Sweep? In-progress books may be left incomplete."
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
            <>
              Remove “{confirmAction.title}” from Unprocessed? Library files are kept.
            </>
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
    </div>
  );
}
