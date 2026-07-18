import { useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import api from "../../api/client";
import { useToast } from "../../contexts/ToastContext";
import {
  RefreshCw,
  Play,
  Pause,
  Database,
  Link2,
  Clock,
  AlertCircle,
  CheckCircle2,
  Loader2,
  SlidersHorizontal,
  RotateCcw,
  Save,
} from "lucide-react";

export interface ScraperStatus {
  enabled: boolean;
  configEnabled: boolean;
  dbEnabled: boolean;
  status: string;
  torrentsTotal: number;
  lastRunAt: string | null;
  lastDebridRunAt: string | null;
  lastRssRunAt?: string | null;
  lastRssUpserted?: number;
  rssEveryNJobs?: number;
  lastRssIndexerResults?: Record<string, number>;
  knabenCrawl?: {
    phase?: string;
    category?: string | null;
    categoryIndex?: number;
    categoriesTotal?: number;
    shard?: string | null;
    shardLabel?: string;
    shardIndex?: number;
    shardsTotal?: number;
    offset?: number;
    pagesPerJob?: number;
    lastBatch?: Record<string, unknown>;
  };
  lastQueryIndex: number;
  queryQueueSize: number;
  currentQuery: string;
  queueProgressPercent: number;
  nextQueries: string[];
  nextRunAt: string | null;
  lastError: string | null;
  lastQuery: string | null;
  lastUpsertedCount: number;
  lastMatchesCreated: number;
  intervalSeconds: number;
  queriesPerJob: number;
  queriesPerHour?: number;
  debridIntervalHours: number;
  debridBatchSize: number;
  matchBatchSize: number;
  configuredIndexers?: { id: number; name: string; kind: string }[];
  abbConfigured?: boolean;
  abbMode?: "rss-only" | "author-crawl" | "deep";
  abbRssOnly?: boolean;
  knabenRssOnly?: boolean;
  foreignTitlePrune?: boolean;
  knabenConfigured?: boolean;
  lastJobIndexerResults?: { abb: number; knaben: number };
  stats: {
    mediaTypes: Record<string, number>;
    indexers: Record<string, number>;
    indexersByKind?: { audiobookbay: number; knaben: number; other: number };
    matchTiers: Record<string, number>;
    catalogVolumesMatched: number;
    catalogVolumesAvailable?: number;
    catalogMatchesTotal: number;
    rdCached: number;
    torboxCached: number;
    pendingDebridChecks: number;
    checkedDebridCount?: number;
    debridProvidersConfigured?: string[];
  };
  recentTorrents: {
    title: string;
    indexer: string;
    mediaType: string;
    seeders: number;
    firstSeenAt: string | null;
    rdCached: boolean;
  }[];
  debridRescan?: {
    running?: boolean;
    queued?: number;
    checked?: number;
    preloaded?: number;
    pending?: number;
    batchSize?: number;
    startedAt?: string;
    finishedAt?: string;
    error?: string;
  };
  catalogRelink?: {
    running?: boolean;
    total?: number;
    scanned?: number;
    linked?: number;
    matches?: number;
    pruned?: number;
    startedAt?: string;
    finishedAt?: string;
    error?: string;
  };
  config?: Record<string, number | string>;
}

interface ScraperSettingField {
  key: string;
  label: string;
  description: string;
  type: "int" | "text" | "bool";
  min: number | null;
  max: number | null;
}

interface ScraperSettingsPayload {
  settings: Record<string, number | string | boolean>;
  defaults: Record<string, number | string | boolean>;
  fields: ScraperSettingField[];
}

function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const diff = Date.now() - d.getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  return d.toLocaleString();
}

function timeUntil(iso: string | null): string {
  if (!iso) return "—";
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return "due now";
  const min = Math.ceil(ms / 60_000);
  if (min < 60) return `in ${min}m`;
  return `in ${Math.floor(min / 60)}h ${min % 60}m`;
}

function StatusBadge({ status, enabled }: { status: string; enabled: boolean }) {
  if (!enabled) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-gray-700 text-gray-300">
        <Pause size={12} /> Disabled
      </span>
    );
  }
  if (status === "running") {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/50 text-amber-300 border border-amber-800/50">
        <Loader2 size={12} className="animate-spin" /> Running
      </span>
    );
  }
  if (status === "error") {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-red-900/50 text-red-300 border border-red-800/50">
        <AlertCircle size={12} /> Error
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-900/50 text-emerald-300 border border-emerald-800/50">
      <CheckCircle2 size={12} /> Idle
    </span>
  );
}

function StatCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="bg-gray-800/80 border border-gray-700 rounded-xl p-3 sm:p-4 min-w-0">
      <p className="text-[10px] sm:text-xs text-gray-500 uppercase tracking-wide">{label}</p>
      <p className="text-xl sm:text-2xl font-bold text-gray-100 mt-1 tabular-nums">{value}</p>
      {sub && <p className="text-[10px] sm:text-xs text-gray-400 mt-1 break-words leading-snug">{sub}</p>}
    </div>
  );
}

function BreakdownBar({
  items,
  colors,
}: {
  items: Record<string, number>;
  colors?: Record<string, string>;
}) {
  const total = Object.values(items).reduce((a, b) => a + b, 0);
  if (total === 0) return <p className="text-sm text-gray-500">No data yet</p>;

  const defaultColors: Record<string, string> = {
    audiobook: "bg-brand-500",
    ebook: "bg-violet-500",
    unknown: "bg-gray-600",
    exact: "bg-emerald-500",
    likely: "bg-sky-500",
    weak: "bg-gray-500",
  };
  const palette = { ...defaultColors, ...colors };

  return (
    <div className="space-y-2">
      <div className="flex h-2.5 rounded-full overflow-hidden bg-gray-900">
        {Object.entries(items).map(([key, count]) => (
          <div
            key={key}
            className={`${palette[key] || "bg-gray-600"} transition-all`}
            style={{ width: `${(count / total) * 100}%` }}
            title={`${key}: ${count}`}
          />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {Object.entries(items).map(([key, count]) => (
          <span key={key} className="text-gray-400">
            <span className={`inline-block w-2 h-2 rounded-full mr-1 ${palette[key] || "bg-gray-600"}`} />
            {key}: <span className="text-gray-200 font-medium">{count}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

function ScraperTuningCard() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [draft, setDraft] = useState<Record<string, string>>({});

  const { data, isLoading } = useQuery({
    queryKey: ["admin-scraper-settings"],
    queryFn: async () => {
      const { data: res } = await api.get("/admin/scraper-settings");
      return res as ScraperSettingsPayload;
    },
  });

  useEffect(() => {
    if (!data) return;
    const next: Record<string, string> = {};
    for (const f of data.fields) next[f.key] = String(data.settings[f.key] ?? "");
    setDraft(next);
  }, [data]);

  const dirty = useMemo(() => {
    if (!data) return false;
    return data.fields.some(
      (f) => String(data.settings[f.key] ?? "") !== (draft[f.key] ?? "")
    );
  }, [data, draft]);

  const save = useMutation({
    mutationFn: async () => {
      if (!data) return null;
      const updates: Record<string, number | string | boolean> = {};
      for (const f of data.fields) {
        const raw = draft[f.key] ?? "";
        if (String(data.settings[f.key] ?? "") === raw) continue;
        if (f.type === "int") {
          const n = Number(raw);
          if (!Number.isFinite(n)) throw new Error(`"${f.label}" must be a number`);
          updates[f.key] = Math.round(n);
        } else if (f.type === "bool") {
          updates[f.key] = raw === "true";
        } else {
          updates[f.key] = raw;
        }
      }
      const { data: res } = await api.put("/admin/scraper-settings", { updates });
      return res as ScraperSettingsPayload;
    },
    onSuccess: (res) => {
      if (!res) return;
      queryClient.setQueryData(["admin-scraper-settings"], res);
      queryClient.invalidateQueries({ queryKey: ["admin-scraper-status"] });
      toast("Scraper settings saved — they apply from the next job", "success");
    },
    onError: (err: any) => {
      toast(err.message || err.response?.data?.detail || "Failed to save settings", "error");
    },
  });

  const reset = useMutation({
    mutationFn: async () => {
      const { data: res } = await api.post("/admin/scraper-settings/reset");
      return res as ScraperSettingsPayload;
    },
    onSuccess: (res) => {
      queryClient.setQueryData(["admin-scraper-settings"], res);
      queryClient.invalidateQueries({ queryKey: ["admin-scraper-status"] });
      toast("Scraper settings reset to defaults", "success");
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Failed to reset settings", "error");
    },
  });

  if (isLoading || !data) {
    return (
      <div className="bg-gray-800 border border-gray-700 rounded-xl p-4 text-sm text-gray-500">
        Loading tuning settings…
      </div>
    );
  }

  const boolFields = data.fields.filter((f) => f.type === "bool");
  const numberFields = data.fields.filter((f) => f.type === "int");
  const textFields = data.fields.filter((f) => f.type === "text");

  return (
    <div className="bg-gray-800 border border-gray-700 rounded-xl p-4 space-y-4">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <h3 className="text-sm font-semibold text-gray-100 flex items-center gap-2">
          <SlidersHorizontal size={16} className="text-emerald-400" />
          Tuning
        </h3>
        <div className="flex gap-2">
          <button
            onClick={() => reset.mutate()}
            disabled={reset.isPending}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-900 text-gray-400 text-xs rounded-lg hover:bg-gray-700 border border-gray-700 disabled:opacity-50"
          >
            <RotateCcw size={12} />
            Reset to defaults
          </button>
          <button
            onClick={() => save.mutate()}
            disabled={save.isPending || !dirty}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-brand-600 text-white text-xs rounded-lg hover:bg-brand-500 disabled:opacity-50"
          >
            {save.isPending ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
            Save changes
          </button>
        </div>
      </div>
      <p className="text-xs text-gray-500">
        Overrides are stored in the database and take effect on the next scrape job — no restart
        needed. Tune these down if the Pi struggles, up if it&apos;s coasting.
      </p>

      {boolFields.length > 0 && (
        <div className="space-y-3 border border-gray-700/80 rounded-lg p-3 bg-gray-900/40">
          {boolFields.map((f) => {
            const on = (draft[f.key] ?? "") === "true";
            return (
              <label key={f.key} className="flex items-start gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={on}
                  onChange={(e) =>
                    setDraft((d) => ({ ...d, [f.key]: e.target.checked ? "true" : "false" }))
                  }
                  className="mt-0.5 rounded border-gray-600 bg-gray-900 text-brand-500 focus:ring-brand-500"
                />
                <span className="min-w-0">
                  <span className="block text-xs font-medium text-gray-200">{f.label}</span>
                  <span className="block text-[11px] text-gray-500 mt-0.5 leading-snug">
                    {f.description} Default: {String(data.defaults[f.key])}
                  </span>
                </span>
              </label>
            );
          })}
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {numberFields.map((f) => (
          <label key={f.key} className="block">
            <span className="text-xs font-medium text-gray-300">{f.label}</span>
            <input
              type="number"
              min={f.min ?? undefined}
              max={f.max ?? undefined}
              value={draft[f.key] ?? ""}
              onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
              className="mt-1 w-full px-2.5 py-1.5 bg-gray-900 border border-gray-700 rounded-lg text-sm text-gray-200 focus:outline-none focus:border-brand-500"
            />
            <span className="block text-[11px] text-gray-500 mt-1 leading-snug">
              {f.description} Default: {String(data.defaults[f.key])}
              {f.min != null && f.max != null ? ` (${f.min}–${f.max})` : ""}
            </span>
          </label>
        ))}
      </div>

      {textFields.map((f) => (
        <label key={f.key} className="block">
          <span className="text-xs font-medium text-gray-300">{f.label}</span>
          <textarea
            rows={4}
            value={draft[f.key] ?? ""}
            onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
            placeholder={"joe abercrombie\nproject hail mary\nlitrpg audiobook"}
            className="mt-1 w-full px-2.5 py-1.5 bg-gray-900 border border-gray-700 rounded-lg text-sm text-gray-200 font-mono focus:outline-none focus:border-brand-500"
          />
          <span className="block text-[11px] text-gray-500 mt-1">{f.description}</span>
        </label>
      ))}
    </div>
  );
}

export default function ScraperTab() {
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const { data, isLoading, isFetching, refetch, dataUpdatedAt } = useQuery({
    queryKey: ["admin-scraper-status"],
    queryFn: async () => {
      const { data: res } = await api.get("/admin/scraper-status");
      return res as ScraperStatus;
    },
    refetchInterval: (query) => {
      const s = query.state.data?.status;
      const rescan = query.state.data?.debridRescan?.running;
      const relink = query.state.data?.catalogRelink?.running;
      if (query.state.fetchFailureCount > 0) return 30_000;
      return s === "running" || rescan || relink ? 12_000 : 20_000;
    },
    retry: 1,
  });

  const toggle = useMutation({
    mutationFn: async (enabled: boolean) => {
      const { data: res } = await api.post("/admin/scraper-enabled", { enabled });
      return res as ScraperStatus;
    },
    onSuccess: (res) => {
      queryClient.setQueryData(["admin-scraper-status"], res);
      toast(res.enabled ? "Indexer scraper enabled" : "Indexer scraper paused", "success");
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Failed to update scraper", "error");
    },
  });

  const clearError = useMutation({
    mutationFn: async () => {
      const { data: res } = await api.post("/admin/scraper-clear-error");
      return res as ScraperStatus;
    },
    onSuccess: (res) => {
      queryClient.setQueryData(["admin-scraper-status"], res);
      toast("Error cleared", "success");
    },
  });

  const clearJobErrors = useMutation({
    mutationFn: async () => {
      const { data: res } = await api.post("/admin/scraper-clear-job-errors?force_stop=true");
      return res as { status?: ScraperStatus };
    },
    onSuccess: (res) => {
      if (res.status) {
        queryClient.setQueryData(["admin-scraper-status"], res.status);
      } else {
        queryClient.invalidateQueries({ queryKey: ["admin-scraper-status"] });
      }
      toast("Job errors cleared — you can re-run rescan / re-link", "success");
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Failed to clear job errors", "error");
    },
  });

  const runNow = useMutation({
    mutationFn: async () => {
      const { data: res } = await api.post("/admin/scraper-run-now");
      return res as ScraperStatus;
    },
    onSuccess: (res) => {
      queryClient.setQueryData(["admin-scraper-status"], res);
      toast(`Scrape complete — ${res.lastUpsertedCount} torrents from "${res.lastQuery}"`, "success");
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Failed to run scraper", "error");
    },
  });

  const rescanAllDebrid = useMutation({
    mutationFn: async () => {
      const { data: res } = await api.post("/admin/scraper-rescan-all-debrid");
      return res as {
        ok: boolean;
        queued?: number;
        error?: string;
        progress?: ScraperStatus["debridRescan"];
      };
    },
    onSuccess: (res) => {
      if (!res.ok) {
        toast(res.error || "Debrid rescan already running", "info");
        return;
      }
      queryClient.invalidateQueries({ queryKey: ["admin-scraper-status"] });
      toast(
        `Queued ${res.queued ?? 0} torrents for debrid cache rescan. This runs in the background.`,
        "success",
      );
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Failed to start debrid rescan", "error");
    },
  });

  const relinkCatalog = useMutation({
    mutationFn: async () => {
      const { data: res } = await api.post("/admin/scraper-relink-catalog", {
        prune_unmatched: true,
      });
      return res as {
        ok: boolean;
        error?: string;
        progress?: ScraperStatus["catalogRelink"];
      };
    },
    onSuccess: (res) => {
      if (!res.ok) {
        toast(res.error || "Catalog re-link already running", "info");
        return;
      }
      queryClient.invalidateQueries({ queryKey: ["admin-scraper-status"] });
      toast(
        "Re-linking cached torrents against the local catalog (prunes non-matches). Runs in the background.",
        "success",
      );
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Failed to start catalog re-link", "error");
    },
  });

  const queuePosition = useMemo(() => {
    if (!data?.queryQueueSize) return "—";
    return `${(data.lastQueryIndex % data.queryQueueSize) + 1} / ${data.queryQueueSize}`;
  }, [data]);

  if (isLoading) {
    return <div className="text-gray-500 py-12 text-center">Loading scraper status…</div>;
  }
  if (!data) return null;

  const isRunning = data.status === "running";
  const debridRescan = data.debridRescan;
  const debridRescanRunning = Boolean(debridRescan?.running);
  const catalogRelink = data.catalogRelink;
  const catalogRelinkRunning = Boolean(catalogRelink?.running);

  return (
    <div className="space-y-6 min-w-0 max-w-full overflow-x-hidden">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <div className="flex items-center gap-3 flex-wrap">
            <h2 className="text-lg font-semibold text-gray-100">Indexer Cache Scraper</h2>
            <StatusBadge status={data.status} enabled={data.enabled} />
          </div>
          <p className="text-sm text-gray-400 mt-1">
            Background crawl of trusted Prowlarr indexers (ABB + Knaben). Powers fast store search and cached downloads.
          </p>
          <p className="text-xs text-gray-500 mt-1">
            Updated {formatWhen(new Date(dataUpdatedAt).toISOString())}
            {isFetching && !isLoading && " · refreshing…"}
          </p>
        </div>
        <div className="flex flex-wrap gap-2 shrink-0">
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            className="flex items-center gap-1.5 px-3 py-2 bg-gray-800 text-gray-300 text-sm rounded-lg hover:bg-gray-700 border border-gray-700 disabled:opacity-50"
          >
            <RefreshCw size={14} className={isFetching ? "animate-spin" : ""} />
            Refresh
          </button>
          <button
            onClick={() => runNow.mutate()}
            disabled={runNow.isPending || isRunning || !data.enabled}
            className="flex items-center gap-1.5 px-3 py-2 bg-brand-600 text-white text-sm rounded-lg hover:bg-brand-500 disabled:opacity-50"
          >
            {runNow.isPending || isRunning ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <Play size={14} />
            )}
            Run now
          </button>
          <button
            onClick={() => rescanAllDebrid.mutate()}
            disabled={rescanAllDebrid.isPending || debridRescanRunning}
            title="Re-check every cached torrent against RD/Torbox and submit catalog matches for caching"
            className="flex items-center gap-1.5 px-3 py-2 bg-violet-900/40 text-violet-200 text-sm rounded-lg hover:bg-violet-900/60 border border-violet-800/50 disabled:opacity-50"
          >
            {rescanAllDebrid.isPending || debridRescanRunning ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <Database size={14} />
            )}
            Rescan debrid
          </button>
          <button
            onClick={() => relinkCatalog.mutate()}
            disabled={relinkCatalog.isPending || catalogRelinkRunning}
            title="Re-match every cached torrent against the local Open Library catalog and prune entries that match nothing (non-book noise)"
            className="flex items-center gap-1.5 px-3 py-2 bg-sky-900/40 text-sky-200 text-sm rounded-lg hover:bg-sky-900/60 border border-sky-800/50 disabled:opacity-50"
          >
            {relinkCatalog.isPending || catalogRelinkRunning ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <Link2 size={14} />
            )}
            Re-link catalog
          </button>
          <button
            onClick={() => toggle.mutate(!data.enabled)}
            disabled={toggle.isPending || !data.configEnabled}
            className={`flex items-center gap-1.5 px-3 py-2 text-sm rounded-lg border disabled:opacity-50 ${
              data.enabled
                ? "bg-amber-900/30 text-amber-300 border-amber-800/50 hover:bg-amber-900/50"
                : "bg-emerald-900/30 text-emerald-300 border-emerald-800/50 hover:bg-emerald-900/50"
            }`}
          >
            {data.enabled ? <Pause size={14} /> : <Play size={14} />}
            {data.enabled ? "Pause" : "Enable"}
          </button>
        </div>
      </div>

      {debridRescanRunning && debridRescan && (
        <div className="p-3 bg-violet-900/20 border border-violet-800/40 rounded-xl text-sm text-violet-200">
          <p className="font-medium flex items-center gap-2">
            <Loader2 size={14} className="animate-spin shrink-0" />
            Full debrid rescan in progress
          </p>
          <p className="text-xs text-violet-300/90 mt-1">
            Checked {debridRescan.checked ?? 0} of {debridRescan.queued ?? data.torrentsTotal}
            {debridRescan.preloaded != null && debridRescan.preloaded > 0
              ? ` · ${debridRescan.preloaded} preloaded to debrid`
              : ""}
            {debridRescan.pending != null ? ` · ${debridRescan.pending} remaining` : ""}
          </p>
        </div>
      )}

      {catalogRelinkRunning && catalogRelink && (
        <div className="p-3 bg-sky-900/20 border border-sky-800/40 rounded-xl text-sm text-sky-200">
          <p className="font-medium flex items-center gap-2">
            <Loader2 size={14} className="animate-spin shrink-0" />
            Catalog re-link in progress
          </p>
          <p className="text-xs text-sky-300/90 mt-1">
            Scanned {catalogRelink.scanned ?? 0} of {catalogRelink.total ?? data.torrentsTotal}
            {catalogRelink.linked != null ? ` · ${catalogRelink.linked} linked` : ""}
            {catalogRelink.pruned != null && catalogRelink.pruned > 0
              ? ` · ${catalogRelink.pruned} pruned`
              : ""}
          </p>
        </div>
      )}

      {catalogRelink?.error && !catalogRelinkRunning && (
        <div className="p-3 bg-red-900/20 border border-red-800/40 rounded-xl text-sm text-red-300 flex gap-2 justify-between">
          <div className="min-w-0">
            <p className="font-medium">Catalog re-link failed</p>
            <p className="text-red-400/90 mt-0.5 break-words">{catalogRelink.error}</p>
            <p className="text-xs text-red-400/60 mt-1">
              Dismiss stale errors after a deploy, then re-run Catalog re-link.
            </p>
          </div>
          <button
            type="button"
            onClick={() => clearJobErrors.mutate()}
            disabled={clearJobErrors.isPending}
            className="shrink-0 px-2 py-1 text-xs bg-red-900/40 hover:bg-red-900/60 rounded border border-red-800/50 h-fit"
          >
            Dismiss
          </button>
        </div>
      )}

      {debridRescan?.error && !debridRescanRunning && (
        <div className="p-3 bg-red-900/20 border border-red-800/40 rounded-xl text-sm text-red-300 flex gap-2 justify-between">
          <div className="min-w-0">
            <p className="font-medium">Debrid rescan failed</p>
            <p className="text-red-400/90 mt-0.5 break-words">{debridRescan.error}</p>
            <p className="text-xs text-red-400/60 mt-1">
              Dismiss stale errors after a deploy, then re-run Rescan debrid.
            </p>
          </div>
          <button
            type="button"
            onClick={() => clearJobErrors.mutate()}
            disabled={clearJobErrors.isPending}
            className="shrink-0 px-2 py-1 text-xs bg-red-900/40 hover:bg-red-900/60 rounded border border-red-800/50 h-fit"
          >
            Dismiss
          </button>
        </div>
      )}

      {!data.configEnabled && (
        <div className="p-3 bg-amber-900/20 border border-amber-800/40 rounded-xl text-sm text-amber-300">
          Scraper is disabled in server config (<code className="text-amber-200">SCRAPER_ENABLED=false</code>).
        </div>
      )}

      {data.lastError && (
        <div className="p-3 bg-red-900/20 border border-red-800/40 rounded-xl text-sm text-red-300 flex gap-2 justify-between">
          <div className="flex gap-2 min-w-0">
            <AlertCircle size={16} className="shrink-0 mt-0.5" />
            <div className="min-w-0">
              <p className="font-medium">Last error</p>
              <p className="text-red-400/90 mt-0.5 break-words">{data.lastError}</p>
              <p className="text-xs text-red-400/60 mt-1">
                Transient Prowlarr timeouts retry automatically. Persistent errors often mean Prowlarr is unreachable.
              </p>
            </div>
          </div>
          {data.status === "error" && (
            <button
              type="button"
              onClick={() => clearError.mutate()}
              disabled={clearError.isPending}
              className="shrink-0 px-2 py-1 text-xs bg-red-900/40 hover:bg-red-900/60 rounded border border-red-800/50"
            >
              Dismiss
            </button>
          )}
        </div>
      )}

      {data.knabenCrawl && !data.knabenRssOnly && (
        <div className="bg-gray-800 border border-sky-800/40 rounded-xl p-4 space-y-2">
          <h3 className="text-sm font-semibold text-gray-100">Knaben full category crawl</h3>
          <p className="text-xs text-gray-500">
            Sweeps Knaben&apos;s Audiobook then EBook categories (API caps at 10k per query, so title
            slices fill the long tail). After both finish, RSS polling keeps new uploads flowing.
          </p>
          {data.knabenCrawl.phase === "maintenance" ? (
            <p className="text-sm text-sky-300 flex items-center gap-2">
              <CheckCircle2 size={16} className="shrink-0" />
              Full sweep complete — RSS maintenance active each scrape job
            </p>
          ) : (
            <div className="text-sm text-gray-300 space-y-1">
              <p>
                <span className="text-gray-400">Category:</span>{" "}
                {data.knabenCrawl.category ?? "—"}
                {data.knabenCrawl.categoriesTotal != null &&
                  data.knabenCrawl.categoryIndex != null &&
                  ` (${data.knabenCrawl.categoryIndex + 1}/${data.knabenCrawl.categoriesTotal})`}
              </p>
              <p>
                <span className="text-gray-400">Slice:</span>{" "}
                {data.knabenCrawl.shardLabel ?? data.knabenCrawl.shard ?? "—"}
                {data.knabenCrawl.shardsTotal != null &&
                  data.knabenCrawl.shardIndex != null &&
                  ` · ${data.knabenCrawl.shardIndex + 1}/${data.knabenCrawl.shardsTotal}`}
              </p>
              <p>
                <span className="text-gray-400">Offset:</span>{" "}
                {(data.knabenCrawl.offset ?? 0).toLocaleString()}
                {data.knabenCrawl.pagesPerJob
                  ? ` · ${data.knabenCrawl.pagesPerJob} pages/job (~${(data.knabenCrawl.pagesPerJob * 100).toLocaleString()} torrents)`
                  : ""}
              </p>
            </div>
          )}
        </div>
      )}

      {data.configuredIndexers && data.configuredIndexers.length > 0 && (
        <div className="bg-gray-800 border border-gray-700 rounded-xl p-4 space-y-3">
          <h3 className="text-sm font-semibold text-gray-100">Prowlarr indexers (detected)</h3>
          <p className="text-xs text-gray-500">
            Cached &quot;By indexer&quot; counts reflect what is stored in the database. The scraper now queries ABB and Knaben separately so Knaben cannot fill the shared result cap.
          </p>
          <div className="flex flex-wrap gap-2">
            {data.configuredIndexers.map((idx) => {
              const kindLabel =
                idx.kind === "audiobookbay" ? "ABB" : idx.kind === "knaben" ? "Knaben" : "Other";
              const kindColor =
                idx.kind === "audiobookbay"
                  ? "border-brand-600/50 text-brand-300 bg-brand-900/20"
                  : idx.kind === "knaben"
                    ? "border-sky-700/50 text-sky-300 bg-sky-900/20"
                    : "border-gray-600 text-gray-400 bg-gray-900/40";
              return (
                <span
                  key={idx.id}
                  className={`px-2.5 py-1 text-xs rounded-lg border ${kindColor}`}
                  title={`Prowlarr id ${idx.id}`}
                >
                  {kindLabel}: {idx.name}
                </span>
              );
            })}
          </div>
          {data.abbConfigured === false && (
            <p className="text-xs text-amber-400/90">
              AudioBook Bay was not detected by name in Prowlarr. Add it or set{" "}
              <code className="text-amber-200">PROWLARR_TRUSTED_INDEXER_NAMES</code> to match your indexer name.
            </p>
          )}
          {data.abbConfigured !== false && data.abbMode && (
            <p className="text-xs text-gray-400">
              ABB ingest mode:{" "}
              <span className="text-gray-200">
                {data.abbMode === "rss-only"
                  ? "RSS only (recent posts via Mullvad Flare — Jackett stays on LAN)"
                  : data.abbMode === "author-crawl"
                    ? "Author / A–Z deep crawl"
                    : "Direct deep scrape"}
              </span>
              {data.abbMode === "rss-only" && data.rssEveryNJobs
                ? ` · polled every ${data.rssEveryNJobs} job(s)`
                : null}
              {" · "}
              live download search uses Flare→Mullvad when VPN is configured
            </p>
          )}
          {data.knabenRssOnly && (
            <p className="text-xs text-gray-400">
              Knaben ingest mode:{" "}
              <span className="text-gray-200">RSS only</span> (full category crawl paused)
            </p>
          )}
          {data.foreignTitlePrune !== false && (
            <p className="text-xs text-gray-400">
              Foreign-script titles:{" "}
              <span className="text-gray-200">pruning on</span> (≥50% non-Latin letters)
            </p>
          )}
          {data.lastJobIndexerResults && (
            <p className="text-xs text-gray-400">
              Last job raw results: ABB {data.lastJobIndexerResults.abb}, Knaben{" "}
              {data.lastJobIndexerResults.knaben}
            </p>
          )}
        </div>
      )}

      {/* Key stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard label="Cached torrents" value={data.torrentsTotal} sub="active in database" />
        <StatCard
          label="Catalog matches"
          value={data.stats.catalogMatchesTotal}
          sub={`${data.stats.catalogVolumesAvailable ?? data.stats.catalogVolumesMatched} downloadable · ${data.stats.catalogVolumesMatched} linked total`}
        />
        <StatCard
          label="RD instant"
          value={data.stats.rdCached}
          sub={
            (data.stats.debridProvidersConfigured?.length ?? 0) === 0
              ? "No debrid API keys configured"
              : `${data.stats.torboxCached} on Torbox · ${data.stats.checkedDebridCount ?? 0} checked`
          }
        />
        <StatCard
          label="Queue position"
          value={queuePosition}
          sub={`${data.queueProgressPercent}% through rotation`}
        />
      </div>

      {/* Progress & schedule */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-gray-800 border border-gray-700 rounded-xl p-4 space-y-4">
          <h3 className="text-sm font-semibold text-gray-100 flex items-center gap-2">
            <Clock size={16} className="text-brand-400" />
            Schedule & progress
          </h3>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between gap-4">
              <span className="text-gray-400 shrink-0">Scrape interval</span>
              <span className="text-gray-200 text-right break-words min-w-0">
                {data.intervalSeconds}s · {data.queriesPerJob} queries/job
                {data.queriesPerHour != null && ` (~${data.queriesPerHour}/hr)`}
              </span>
            </div>
            <div className="flex justify-between gap-4">
              <span className="text-gray-400">Last scrape</span>
              <span className="text-gray-200">{formatWhen(data.lastRunAt)}</span>
            </div>
            <div className="flex justify-between gap-4">
              <span className="text-gray-400">Next scrape</span>
              <span className="text-gray-200">{timeUntil(data.nextRunAt)}</span>
            </div>
            <div className="flex justify-between gap-4">
              <span className="text-gray-400">Last debrid batch</span>
              <span className="text-gray-200">{formatWhen(data.lastDebridRunAt)}</span>
            </div>
            <div className="flex justify-between gap-4">
              <span className="text-gray-400">Last RSS ingest</span>
              <span className="text-gray-200">
                {(data.rssEveryNJobs ?? 0) > 0
                  ? `${formatWhen(data.lastRssRunAt ?? null)} · ${data.lastRssUpserted ?? 0} upserted`
                  : data.knabenCrawl?.phase === "maintenance"
                    ? `Knaben RSS each job · ${formatWhen(data.lastRssRunAt ?? null)}`
                    : "Prowlarr off · Knaben RSS after full crawl"}
              </span>
            </div>
            <div className="flex justify-between gap-4">
              <span className="text-gray-400">Debrid interval</span>
              <span className="text-gray-200">every {data.debridIntervalHours}h · batch {data.debridBatchSize}</span>
            </div>
            <div className="flex justify-between gap-4">
              <span className="text-gray-400">Pending debrid checks</span>
              <span className="text-gray-200">{data.stats.pendingDebridChecks}</span>
            </div>
          </div>

          <div>
            <p className="text-xs text-gray-500 mb-2">Queue rotation</p>
            <div className="h-2 bg-gray-900 rounded-full overflow-hidden">
              <div
                className="h-full bg-brand-500 transition-all duration-500"
                style={{ width: `${data.queueProgressPercent}%` }}
              />
            </div>
            <p className="text-xs text-gray-500 mt-1.5">
              Current: <span className="text-gray-300 font-medium">&quot;{data.currentQuery}&quot;</span>
            </p>
          </div>
        </div>

        <div className="bg-gray-800 border border-gray-700 rounded-xl p-4 space-y-3">
          <h3 className="text-sm font-semibold text-gray-100 flex items-center gap-2">
            <Database size={16} className="text-sky-400" />
            Last job
          </h3>
          {data.lastQuery ? (
            <div className="space-y-2 text-sm">
              <div className="flex justify-between gap-4">
                <span className="text-gray-400">Query</span>
                <span className="text-gray-200 font-mono">&quot;{data.lastQuery}&quot;</span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-gray-400">Torrents upserted</span>
                <span className="text-gray-200">{data.lastUpsertedCount}</span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-gray-400">Catalog links created</span>
                <span className="text-gray-200">{data.lastMatchesCreated}</span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-gray-400">Match batch size</span>
                <span className="text-gray-200">{data.matchBatchSize} torrents</span>
              </div>
            </div>
          ) : (
            <p className="text-sm text-gray-500">No scrape jobs completed yet.</p>
          )}

          {data.nextQueries.length > 0 && (
            <div className="pt-2 border-t border-gray-700">
              <p className="text-xs text-gray-500 mb-2">Up next in queue</p>
              <div className="flex flex-wrap gap-1.5">
                {data.nextQueries.map((q) => (
                  <span
                    key={q}
                    className="px-2 py-0.5 bg-gray-900 text-gray-300 text-xs rounded-md font-mono border border-gray-700"
                  >
                    {q}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Live-tunable settings */}
      <ScraperTuningCard />

      {/* Breakdowns */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="bg-gray-800 border border-gray-700 rounded-xl p-4">
          <h3 className="text-sm font-semibold text-gray-100 mb-3">By media type</h3>
          <BreakdownBar items={data.stats.mediaTypes} />
        </div>
        <div className="bg-gray-800 border border-gray-700 rounded-xl p-4">
          <h3 className="text-sm font-semibold text-gray-100 mb-3">Match tiers</h3>
          <BreakdownBar items={data.stats.matchTiers} />
        </div>
        <div className="bg-gray-800 border border-gray-700 rounded-xl p-4">
          <h3 className="text-sm font-semibold text-gray-100 mb-3">By indexer (cached)</h3>
          {data.stats.indexersByKind ? (
            <div className="space-y-2 mb-3 text-sm">
              <div className="flex justify-between gap-2">
                <span className="text-brand-300">AudioBook Bay</span>
                <span className="text-gray-200 font-medium tabular-nums">
                  {data.stats.indexersByKind.audiobookbay}
                </span>
              </div>
              <div className="flex justify-between gap-2">
                <span className="text-sky-300">Knaben</span>
                <span className="text-gray-200 font-medium tabular-nums">
                  {data.stats.indexersByKind.knaben}
                </span>
              </div>
              {data.stats.indexersByKind.other > 0 && (
                <div className="flex justify-between gap-2">
                  <span className="text-gray-400">Other</span>
                  <span className="text-gray-200 font-medium tabular-nums">
                    {data.stats.indexersByKind.other}
                  </span>
                </div>
              )}
            </div>
          ) : null}
          {Object.keys(data.stats.indexers).length === 0 ? (
            <p className="text-sm text-gray-500">No data yet</p>
          ) : (
            <div className="space-y-1.5 text-sm border-t border-gray-700/60 pt-2">
              <p className="text-xs text-gray-500 mb-1">Prowlarr indexer names</p>
              {Object.entries(data.stats.indexers).map(([name, count]) => (
                <div key={name} className="flex justify-between gap-2">
                  <span className="text-gray-400 truncate">{name}</span>
                  <span className="text-gray-200 font-medium tabular-nums shrink-0">{count}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Recent ingestions */}
      <div className="bg-gray-800 border border-gray-700 rounded-xl p-4">
        <h3 className="text-sm font-semibold text-gray-100 mb-3 flex items-center gap-2">
          <Link2 size={16} className="text-violet-400" />
          Recently cached torrents
        </h3>
        {data.recentTorrents.length === 0 ? (
          <p className="text-sm text-gray-500 py-4 text-center">No torrents cached yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 border-b border-gray-700">
                  <th className="pb-2 pr-3 font-medium">Title</th>
                  <th className="pb-2 pr-3 font-medium hidden sm:table-cell">Indexer</th>
                  <th className="pb-2 pr-3 font-medium">Type</th>
                  <th className="pb-2 pr-3 font-medium hidden md:table-cell">Seeders</th>
                  <th className="pb-2 pr-3 font-medium hidden lg:table-cell">RD</th>
                  <th className="pb-2 font-medium">Seen</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-700/60">
                {data.recentTorrents.map((t, i) => (
                  <tr key={`${t.title}-${i}`} className="text-gray-300">
                    <td className="py-2 pr-3 max-w-[200px] sm:max-w-xs truncate" title={t.title}>
                      {t.title}
                    </td>
                    <td className="py-2 pr-3 text-gray-400 hidden sm:table-cell truncate max-w-[100px]">
                      {t.indexer}
                    </td>
                    <td className="py-2 pr-3">
                      <span className="text-xs px-1.5 py-0.5 rounded bg-gray-900 text-gray-400">
                        {t.mediaType}
                      </span>
                    </td>
                    <td className="py-2 pr-3 tabular-nums hidden md:table-cell">{t.seeders}</td>
                    <td className="py-2 pr-3 hidden lg:table-cell">
                      {t.rdCached ? (
                        <span className="text-emerald-400 text-xs">cached</span>
                      ) : (
                        <span className="text-gray-600 text-xs">—</span>
                      )}
                    </td>
                    <td className="py-2 text-gray-500 text-xs whitespace-nowrap">
                      {formatWhen(t.firstSeenAt)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
