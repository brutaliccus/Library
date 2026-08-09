import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Activity,
  ClipboardList,
  Download,
  ExternalLink,
  GitBranch,
  Play,
  RefreshCw,
  RotateCcw,
  Square,
} from "lucide-react";
import api from "../../api/client";
import Modal from "../Modal";
import { useToast } from "../../contexts/ToastContext";
import { softRefreshLibraryCollectionQueries } from "../../utils/shelfQueryCache";

type DockerAction = "start" | "stop" | "restart";

type DockerStats = {
  cpuPercent?: number | null;
  memoryUsageBytes?: number | null;
  memoryLimitBytes?: number | null;
  memoryPercent?: number | null;
};

type DockerServiceInfo = {
  id: string;
  label: string;
  container: string;
  composeService: string;
  healthKey: string | null;
  isSelf: boolean;
  actions: DockerAction[];
  available: boolean;
  error?: string;
  openUrl?: string | null;
  stats?: DockerStats | null;
  state: {
    exists: boolean;
    running: boolean;
    status: string;
    startedAt?: string | null;
  };
};

type DockerServicesResponse = {
  available: boolean;
  socket?: string;
  error?: string | null;
  services: DockerServiceInfo[];
  byHealthKey: Record<string, string>;
  appServiceId: string;
};

type PendingAction = {
  id: string;
  label: string;
  description: string;
  count: number;
  href: string;
  priority: number;
};

type PendingActionsResponse = {
  total: number;
  items: PendingAction[];
};

type ServiceRowDef = {
  id: string;
  title: string;
  healthKey?: string;
  dockerId?: string;
  detail?: (probe: Record<string, unknown>) => string | null;
  /** External open URL when no docker/health openUrl */
  fallbackOpenUrl?: string | null;
};

type ServiceGroup = {
  id: string;
  label: string;
  rows: ServiceRowDef[];
};

const SERVICE_GROUPS: ServiceGroup[] = [
  {
    id: "core",
    label: "Core",
    rows: [
      { id: "app", title: "Library App", dockerId: "app" },
      {
        id: "disk",
        title: "Disk",
        healthKey: "disk",
        detail: (p) =>
          p.free_gb != null
            ? `${p.free_gb} GB free / ${p.total_gb ?? "?"} GB`
            : p.error
              ? String(p.error)
              : null,
      },
    ],
  },
  {
    id: "media",
    label: "Media stack",
    rows: [
      { id: "audiobookshelf", title: "Audiobookshelf", healthKey: "audiobookshelf", dockerId: "audiobookshelf" },
      { id: "kavita", title: "Kavita", healthKey: "kavita", dockerId: "kavita" },
      { id: "libraforge", title: "LibraForge", healthKey: "libraforge", dockerId: "libraforge" },
      { id: "abs_agg", title: "abs-agg", healthKey: "abs_agg", dockerId: "abs-agg" },

    ],
  },
  {
    id: "indexers",
    label: "Indexers",
    rows: [
      {
        id: "prowlarr",
        title: "Prowlarr",
        healthKey: "prowlarr",
        dockerId: "prowlarr",
        detail: (p) =>
          p.indexers != null
            ? `${p.indexers} indexers${p.version ? ` · v${p.version}` : ""}`
            : p.error
              ? String(p.error)
              : null,
      },
      {
        id: "jackett",
        title: "Jackett",
        healthKey: "jackett",
        dockerId: "jackett",
        detail: (p) => {
          if (p.error) return String(p.error);
          if (p.apiKey === true) return "API key set";
          if (p.apiKey === false) return "API key missing";
          return null;
        },
      },
      {
        id: "knaben",
        title: "Knaben",
        healthKey: "knaben",
        detail: (p) => (p.error ? String(p.error) : null),
      },
    ],
  },
  {
    id: "proxies",
    label: "Proxies",
    rows: [
      {
        id: "flaresolverr",
        title: "FlareSolverr",
        healthKey: "flaresolverr",
        dockerId: "flaresolverr",
        detail: (p) => (p.version ? `v${p.version}` : p.error ? String(p.error) : null),
      },
      {
        id: "mullvad_proxy",
        title: "Mullvad (gluetun)",
        healthKey: "mullvad_proxy",
        dockerId: "gluetun",
        detail: (p) => {
          if (p.error) return String(p.error);
          const parts = [p.exitIp, p.country].filter(Boolean);
          return parts.length ? parts.join(" · ") : null;
        },
      },
    ],
  },
  {
    id: "external",
    label: "External APIs",
    rows: [
      {
        id: "real_debrid",
        title: "Real-Debrid",
        healthKey: "real_debrid",
        detail: (p) => {
          if (p.error) return String(p.error);
          const bits = [p.username, p.premium ? "Premium" : null].filter(Boolean);
          return bits.length ? bits.join(" · ") : null;
        },
      },
      {
        id: "torbox",
        title: "Torbox",
        healthKey: "torbox",
        detail: (p) => {
          if (p.error) return String(p.error);
          const bits = [p.username, p.plan != null ? `Plan ${p.plan}` : null].filter(Boolean);
          return bits.length ? String(bits.join(" · ")) : null;
        },
      },
      {
        id: "nyt",
        title: "NYT Books",
        healthKey: "nyt",
        detail: (p) =>
          p.lists != null ? `${p.lists} lists` : p.error ? String(p.error) : null,
      },
      {
        id: "ol_catalog",
        title: "Open Library catalog",
        healthKey: "ol_catalog",
        detail: (p) => {
          if (p.error) return String(p.error);
          if (p.works != null) return `${Number(p.works).toLocaleString()} works`;
          return null;
        },
      },
    ],
  },
];

function formatBytes(bytes: unknown): string {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const unit = value === 0 ? 0 : Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** unit).toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

/** Rewrite loopback Open URLs to the page host so LAN browsers reach published ports. */
function resolveOpenUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    if (parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost") {
      parsed.hostname = window.location.hostname || parsed.hostname;
      return parsed.toString().replace(/\/$/, "");
    }
    return url.replace(/\/$/, "");
  } catch {
    return url;
  }
}

function statusMeta(probe: Record<string, unknown> | undefined, docker?: DockerServiceInfo) {
  if (probe) {
    const configured = probe.configured !== false;
    const connected = !!probe.connected;
    if (!configured) {
      return { label: "N/A", className: "bg-amber-950/50 text-amber-300 border-amber-800/50" };
    }
    if (connected) {
      return { label: "OK", className: "bg-emerald-950/50 text-emerald-300 border-emerald-800/50" };
    }
    return { label: "Down", className: "bg-red-950/50 text-red-300 border-red-800/50" };
  }
  if (docker) {
    if (!docker.state?.exists) {
      return { label: "Missing", className: "bg-amber-950/50 text-amber-300 border-amber-800/50" };
    }
    if (docker.state.running) {
      return { label: "OK", className: "bg-emerald-950/50 text-emerald-300 border-emerald-800/50" };
    }
    return { label: "Stopped", className: "bg-red-950/50 text-red-300 border-red-800/50" };
  }
  return { label: "—", className: "bg-gray-900 text-gray-500 border-gray-700" };
}


type ServerUpdateLocal = {
  sha?: string | null;
  shortSha?: string | null;
  branch?: string | null;
  message?: string | null;
  committedAt?: string | null;
  source?: string | null;
};

type ServerUpdateRemote = {
  sha?: string | null;
  shortSha?: string | null;
  branch?: string | null;
  message?: string | null;
  committedAt?: string | null;
  htmlUrl?: string | null;
};

type ServerUpdateJob = {
  phase?: string;
  running?: boolean;
  ok?: boolean | null;
  error?: string | null;
  logTail?: string;
  startedAt?: string | null;
  finishedAt?: string | null;
};

type ServerUpdateStatus = {
  local?: ServerUpdateLocal | null;
  remote?: ServerUpdateRemote | null;
  state?: string;
  branch?: string;
  repo?: string;
  compare?: { commitsBehind?: number; aheadBy?: number; status?: string } | null;
  applyAvailable?: boolean;
  applyUnavailableReason?: string | null;
  hostRoot?: string | null;
  manualCommand?: string;
  job?: ServerUpdateJob | null;
  error?: string | null;
  checkedAt?: string | null;
};

function formatCommitWhen(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function ServerUpdateCard() {
  const { toast } = useToast();
  const [info, setInfo] = useState<ServerUpdateStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      const { data } = await api.get<ServerUpdateStatus>("/admin/server-update/status");
      setInfo(data);
      setError(null);
    } catch (e: unknown) {
      const detail =
        (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        (e instanceof Error ? e.message : "Could not load server update status");
      setError(String(detail));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  const jobRunning = Boolean(info?.job?.running || info?.job?.phase === "updating");

  useEffect(() => {
    if (!jobRunning) return;
    const id = window.setInterval(() => {
      void (async () => {
        try {
          const { data: job } = await api.get<ServerUpdateJob>("/admin/server-update/job");
          setInfo((prev) => (prev ? { ...prev, job, state: job.running ? "updating" : prev.state } : prev));
          if (!job.running) {
            await loadStatus();
            if (job.ok) toast("Server update finished", "success");
            else if (job.error) toast(job.error, "error");
          }
        } catch {
          /* app may be restarting mid-update */
        }
      })();
    }, 2500);
    return () => window.clearInterval(id);
  }, [jobRunning, loadStatus, toast]);

  const check = async () => {
    setChecking(true);
    setError(null);
    try {
      const { data } = await api.post<ServerUpdateStatus>("/admin/server-update/check");
      setInfo(data);
      if (data.state === "up_to_date") toast("Stack is up to date", "success");
      else if (data.state === "update_available") toast("Update available", "info");
      else if (data.state === "check_failed") toast(data.error || "Check failed", "error");
    } catch (e: unknown) {
      const detail =
        (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        (e instanceof Error ? e.message : "Update check failed");
      setError(String(detail));
      toast(String(detail), "error");
    } finally {
      setChecking(false);
    }
  };

  const apply = async () => {
    setConfirmOpen(false);
    setError(null);
    try {
      const { data } = await api.post<{ message?: string; job?: ServerUpdateJob }>(
        "/admin/server-update/apply",
        null,
        { timeout: 60_000 },
      );
      setInfo((prev) => ({
        ...(prev || {}),
        state: "updating",
        job: data.job || { phase: "updating", running: true, logTail: "" },
      }));
      toast(data.message || "Server update started", "info");
    } catch (e: unknown) {
      const detail =
        (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        (e instanceof Error ? e.message : "Could not start update");
      setError(String(detail));
      toast(String(detail), "error");
      // Replace stale job/log from a previous failure with the latest apply attempt.
      await loadStatus();
    }
  };

  const local = info?.local;
  const remote = info?.remote;
  const state = jobRunning ? "updating" : info?.state || "unknown";
  const behind =
    info?.compare?.commitsBehind ??
    info?.compare?.aheadBy ??
    null;
  const updateReady = state === "update_available";
  const githubUrl = `https://github.com/${info?.repo || "brutaliccus/Library"}`;

  let statusLine: ReactNode = null;
  if (state === "updating") {
    statusLine = <p className="text-xs text-amber-300">Updating entire stack… app may restart briefly</p>;
  } else if (state === "update_available") {
    statusLine = (
      <p className="text-xs text-emerald-300">
        Update available
        {behind != null && behind > 0 ? ` — ${behind} commit${behind === 1 ? "" : "s"} behind` : ""}
      </p>
    );
  } else if (state === "up_to_date") {
    statusLine = <p className="text-xs text-gray-500">This server has the latest {info?.branch || "main"}</p>;
  } else if (state === "check_failed") {
    statusLine = <p className="text-xs text-amber-300/90">{info?.error || error || "Check failed"}</p>;
  } else if (!loading) {
    statusLine = (
      <p className="text-xs text-gray-500">Check for updates to compare with origin/{info?.branch || "main"}</p>
    );
  }

  return (
    <>
      <div className="rounded-xl border border-gray-800 bg-gray-900/40 overflow-hidden">
        <div className="px-3 py-2 border-b border-gray-800 flex items-start gap-2">
          <GitBranch size={14} className="text-emerald-400 shrink-0 mt-0.5" />
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold text-gray-100">Server stack update</h3>
            <p className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">
              Compare this install to origin/{info?.branch || "main"} and update the entire Docker stack
              (same as <code className="text-gray-400">scripts/update_library.sh</code>).
            </p>
          </div>
        </div>
        <div className="px-3 py-3 space-y-3">
          <dl className="text-xs space-y-1.5">
            <div className="flex justify-between gap-3">
              <dt className="text-gray-500">Installed</dt>
              <dd className="text-gray-200 text-right min-w-0">
                {loading ? (
                  "…"
                ) : local?.shortSha || local?.sha ? (
                  <>
                    <span className="font-mono">{local.shortSha || local.sha?.slice(0, 7)}</span>
                    {local.branch ? <span className="text-gray-500"> · {local.branch}</span> : null}
                    {local.committedAt ? (
                      <span className="block text-gray-500 font-normal">{formatCommitWhen(local.committedAt)}</span>
                    ) : null}
                    {local.message ? (
                      <span className="block text-gray-500 truncate max-w-[16rem] ml-auto">{local.message}</span>
                    ) : null}
                  </>
                ) : (
                  <span className="text-gray-500">Unknown — run an update once to record revision</span>
                )}
              </dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-gray-500">On GitHub</dt>
              <dd className="text-gray-200 text-right min-w-0">
                {remote?.shortSha || remote?.sha ? (
                  <>
                    <span className="font-mono">{remote.shortSha || remote.sha?.slice(0, 7)}</span>
                    {remote.branch ? <span className="text-gray-500"> · {remote.branch}</span> : null}
                    {remote.committedAt ? (
                      <span className="block text-gray-500 font-normal">{formatCommitWhen(remote.committedAt)}</span>
                    ) : null}
                    {remote.message ? (
                      <span className="block text-gray-500 truncate max-w-[16rem] ml-auto">{remote.message}</span>
                    ) : (
                      <span className="block text-gray-600">Check for updates to load</span>
                    )}
                  </>
                ) : (
                  "—"
                )}
              </dd>
            </div>
          </dl>

          {statusLine}
          {error && state !== "check_failed" && (
            <p className="text-xs text-amber-300/90">{error}</p>
          )}
          {!info?.applyAvailable && info?.applyUnavailableReason && (
            <p className="text-[11px] text-gray-500">
              Apply unavailable: {info.applyUnavailableReason}. You can still update over SSH with{" "}
              <code className="text-gray-400">{info.manualCommand || "bash scripts/update_library.sh"}</code>.
            </p>
          )}

          {(jobRunning || info?.job?.logTail) && (
            <pre className="text-[10px] text-gray-400 bg-gray-950/60 border border-gray-800 rounded-lg px-2 py-2 max-h-36 overflow-auto whitespace-pre-wrap break-all">
              {info?.job?.logTail || "Waiting for update logs…"}
            </pre>
          )}

          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => void check()}
              disabled={loading || checking || jobRunning}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-800 text-gray-200 text-sm font-medium hover:bg-gray-700 disabled:opacity-50 border border-gray-700"
            >
              <RefreshCw size={14} className={checking ? "animate-spin" : ""} />
              Check for updates
            </button>
            <button
              type="button"
              onClick={() => setConfirmOpen(true)}
              disabled={!updateReady || jobRunning || !info?.applyAvailable || checking}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-700/80 text-white text-sm font-medium hover:bg-emerald-600 disabled:opacity-50"
            >
              <Download size={14} />
              {jobRunning ? "Updating…" : "Update"}
            </button>
            <a
              href={githubUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-teal-300/90 hover:text-teal-200"
            >
              <ExternalLink size={14} /> GitHub
            </a>
          </div>
        </div>
      </div>

      <Modal
        title="Update entire stack?"
        show={confirmOpen}
        onClose={() => setConfirmOpen(false)}
      >
        <p className="text-sm text-gray-400 mb-4">
          This runs <code className="text-gray-300">git reset --hard origin/{info?.branch || "main"}</code> on
          the host install
          {info?.hostRoot ? (
            <>
              {" "}
              (<code className="text-gray-300">{info.hostRoot}</code>)
            </>
          ) : null}
          , then rebuilds and recreates containers. Local <strong className="text-gray-300 font-medium">tracked</strong>{" "}
          file edits are discarded. <code className="text-gray-300">.env</code>, media, data, and NPM config are kept.
        </p>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={() => setConfirmOpen(false)}
            className="px-3 py-1.5 text-sm rounded-lg bg-gray-800 text-gray-300 border border-gray-700 hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void apply()}
            className="px-3 py-1.5 text-sm rounded-lg bg-emerald-700/80 text-white hover:bg-emerald-600"
          >
            Update stack
          </button>
        </div>
      </Modal>
    </>
  );
}


export default function HealthTab() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [stopConfirm, setStopConfirm] = useState<DockerServiceInfo | null>(null);
  const [showKavitaDebug, setShowKavitaDebug] = useState(false);

  const { data: health, isLoading, refetch } = useQuery({
    queryKey: ["admin-health"],
    queryFn: async () => {
      const { data } = await api.get("/admin/health");
      return data as Record<string, Record<string, unknown>>;
    },
    staleTime: 25_000,
    refetchInterval: 30_000,
  });

  const { data: pending, refetch: refetchPending } = useQuery({
    queryKey: ["admin-health-pending"],
    queryFn: async () => {
      const { data } = await api.get("/admin/health/pending-actions");
      return data as PendingActionsResponse;
    },
    staleTime: 15_000,
    refetchInterval: 20_000,
  });

  const { data: dockerInfo, refetch: refetchDocker } = useQuery({
    queryKey: ["admin-docker-services"],
    queryFn: async () => {
      const { data } = await api.get("/admin/docker/services");
      return data as DockerServicesResponse;
    },
    retry: 1,
    staleTime: 12_000,
    refetchInterval: 15_000,
  });

  const dockerById = useMemo(() => {
    const map = new Map<string, DockerServiceInfo>();
    for (const s of dockerInfo?.services || []) map.set(s.id, s);
    return map;
  }, [dockerInfo]);

  const refreshHealth = async () => {
    // force=true bypasses short server-side caches used for fast tab loads
    const [{ data: healthData }, { data: dockerData }, { data: pendingData }] = await Promise.all([
      api.get("/admin/health", { params: { force: true } }),
      api.get("/admin/docker/services", { params: { force: true } }),
      api.get("/admin/health/pending-actions"),
    ]);
    queryClient.setQueryData(["admin-health"], healthData);
    queryClient.setQueryData(["admin-docker-services"], dockerData);
    queryClient.setQueryData(["admin-health-pending"], pendingData);
  };

  const dockerAction = useMutation({
    mutationFn: async ({ serviceId, action }: { serviceId: string; action: DockerAction }) => {
      const { data } = await api.post(
        `/admin/docker/services/${serviceId}/${action}`,
        null,
        { timeout: 90_000 },
      );
      return data as {
        ok: boolean;
        message?: string;
        deferred?: boolean;
        serviceId: string;
        action: string;
      };
    },
    onSuccess: async (data) => {
      toast(data.message || "Container action completed", data.deferred ? "info" : "success");
      setStopConfirm(null);
      const delays = data.deferred ? [2500, 6000, 12000] : [800, 2500, 5000];
      for (const delay of delays) {
        await new Promise((r) => setTimeout(r, delay));
        await refreshHealth();
        if (data.serviceId === "app") {
          await queryClient.invalidateQueries({ queryKey: ["uptime-check"] });
        }
      }
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Container action failed", "error");
    },
  });

  const enableVpn = useMutation({
    mutationFn: async () => {
      const { data } = await api.post(
        "/admin/setup/enable-vpn",
        { account: "" },
        { timeout: 420_000 },
      );
      return data as { ok?: boolean; error?: string };
    },
    onSuccess: async (data) => {
      toast(
        data.ok ? "gluetun enabled (vpn profile)" : data.error || "VPN enable failed",
        data.ok ? "success" : "error",
      );
      await refreshHealth();
      void queryClient.invalidateQueries({ queryKey: ["admin-integrations"] });
    },
    onError: (err: any) => {
      const detail = err.response?.data?.detail || "VPN enable failed";
      toast(
        String(detail).includes("No WireGuard")
          ? `${detail} — save a Mullvad account under Integrations first.`
          : String(detail),
        "error",
      );
    },
  });

  const runDockerAction = (svc: DockerServiceInfo, action: DockerAction) => {
    if (action === "stop") {
      setStopConfirm(svc);
      return;
    }
    dockerAction.mutate({ serviceId: svc.id, action });
  };

  const libraryRefresh = useMutation({
    mutationFn: async () => {
      const { data } = await api.post("/admin/library/refresh", null, { timeout: 90_000 });
      return data as {
        ok: boolean;
        message: string;
        abs: {
          ok: boolean;
          deferred?: boolean;
          already_running?: boolean;
        };
      };
    },
    onSuccess: async (data) => {
      void (async () => {
        for (let i = 0; i < 36; i++) {
          await new Promise((r) => setTimeout(r, 10_000));
          try {
            const { data: st } = await api.get("/admin/library/refresh/status", {
              timeout: 15_000,
            });
            if ((st as { phase?: string })?.phase === "idle") break;
          } catch {
            // keep polling
          }
        }
        await softRefreshLibraryCollectionQueries(queryClient, { bustMs: 5_000 });
      })();

      const absDeferred = Boolean(data.abs?.deferred || data.abs?.already_running);
      toast(
        data.message || "Library refresh kicked",
        data.ok ? (absDeferred ? "info" : "success") : "error",
      );
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Library refresh failed", "error");
    },
  });

  if (isLoading) return <div className="text-gray-500">Loading...</div>;
  if (!health) return null;

  const busyId = dockerAction.isPending
    ? (dockerAction.variables?.serviceId ?? null)
    : null;

  const pendingItems = (pending?.items || []).filter((i) => (i.count || 0) > 0);
  const pendingTotal = pending?.total ?? pendingItems.reduce((n, i) => n + (i.count || 0), 0);

  return (
    <div className="space-y-5 min-w-0">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-100 flex items-center gap-2">
            <Activity size={18} />
            Health
          </h2>
          <p className="text-xs text-gray-500 mt-1 max-w-xl">
            Pending review queues and condensed service status. API keys live under Integrations.
          </p>
          {dockerInfo && !dockerInfo.available && (
            <p className="text-xs text-amber-400/90 mt-2 max-w-xl">
              Container controls unavailable
              {dockerInfo.error ? `: ${dockerInfo.error}` : ""}. Mount docker.sock and set{" "}
              <code className="text-amber-300">DOCKER_GID</code>, then recreate the app container.
            </p>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => void refreshHealth()}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-800 text-gray-300 text-sm rounded-lg hover:bg-gray-700 border border-gray-700"
          >
            <RefreshCw size={14} /> Refresh
          </button>
          <button
            type="button"
            title="Safely rescan Audiobookshelf and Kavita"
            onClick={() => libraryRefresh.mutate()}
            disabled={libraryRefresh.isPending}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-brand-900/40 text-brand-300 text-sm rounded-lg hover:bg-brand-900/60 border border-brand-800/50 disabled:opacity-50"
          >
            <RefreshCw size={14} className={libraryRefresh.isPending ? "animate-spin" : ""} />
            {libraryRefresh.isPending ? "Refreshing…" : "Library Refresh"}
          </button>
        </div>
      </div>

      <PendingActionsSection items={pendingItems} total={pendingTotal} loading={!pending} />

      <ServerUpdateCard />

      <div className="space-y-4">
        {SERVICE_GROUPS.map((group) => (
          <ServiceGroupTable
            key={group.id}
            group={group}
            health={health}
            dockerById={dockerById}
            busyId={busyId || (enableVpn.isPending ? "gluetun" : null)}
            onDockerAction={runDockerAction}
            onEnableVpn={() => enableVpn.mutate()}
            enableVpnBusy={enableVpn.isPending}
          />
        ))}
      </div>

      <div className="border border-gray-800 rounded-xl bg-gray-900/40">
        <button
          type="button"
          onClick={() => setShowKavitaDebug((v) => !v)}
          className="w-full flex items-center justify-between px-3 py-2 text-left text-sm text-gray-400 hover:text-gray-200"
        >
          <span>Kavita ebooks diagnostic</span>
          <span className="text-xs text-gray-600">{showKavitaDebug ? "Hide" : "Show"}</span>
        </button>
        {showKavitaDebug && (
          <div className="px-3 pb-3 border-t border-gray-800">
            <KavitaEbookDebug />
          </div>
        )}
      </div>

      <Modal
        title="Stop container?"
        show={stopConfirm !== null}
        onClose={() => !dockerAction.isPending && setStopConfirm(null)}
      >
        <p className="text-sm text-gray-400 mb-4">
          Stop{" "}
          <span className="text-gray-200">{stopConfirm?.label}</span> (
          <code className="text-xs text-gray-300">{stopConfirm?.container}</code>)? Dependent
          features may fail until you start it again.
        </p>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            disabled={dockerAction.isPending}
            onClick={() => setStopConfirm(null)}
            className="px-3 py-1.5 text-sm rounded-lg bg-gray-800 text-gray-300 border border-gray-700 hover:bg-gray-700 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={dockerAction.isPending || !stopConfirm}
            onClick={() =>
              stopConfirm &&
              dockerAction.mutate({ serviceId: stopConfirm.id, action: "stop" })
            }
            className="px-3 py-1.5 text-sm rounded-lg bg-red-900/50 text-red-200 border border-red-800/60 hover:bg-red-900/70 disabled:opacity-50"
          >
            {dockerAction.isPending ? "Stopping…" : "Stop"}
          </button>
        </div>
      </Modal>
    </div>
  );
}

function PendingActionsSection({
  items,
  total,
  loading,
}: {
  items: PendingAction[];
  total: number;
  loading: boolean;
}) {
  return (
    <section className="rounded-xl border border-gray-700 bg-gray-800/80 overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2.5 border-b border-gray-700/80">
        <ClipboardList size={16} className="text-amber-400 shrink-0" />
        <h3 className="text-sm font-semibold text-gray-100">Pending actions</h3>
        <span className="ml-auto text-xs text-gray-500">
          {loading ? "…" : total > 0 ? `${total} item${total === 1 ? "" : "s"}` : "All clear"}
        </span>
      </div>
      {loading ? (
        <p className="px-3 py-3 text-sm text-gray-500">Loading queues…</p>
      ) : items.length === 0 ? (
        <p className="px-3 py-3 text-sm text-gray-500">
          No books waiting for admin review.
        </p>
      ) : (
        <ul className="divide-y divide-gray-700/70">
          {items.map((item) => (
            <li
              key={item.id}
              className="flex flex-wrap items-center gap-2 px-3 py-2.5 hover:bg-gray-800/80"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="text-sm text-gray-100 font-medium truncate">{item.label}</span>
                  <span className="shrink-0 text-xs px-1.5 py-0.5 rounded border border-amber-800/50 bg-amber-950/40 text-amber-200">
                    {item.count}
                  </span>
                </div>
                <p className="text-[11px] text-gray-500 mt-0.5 line-clamp-1">{item.description}</p>
              </div>
              <Link
                to={item.href}
                className="shrink-0 inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded-lg bg-teal-900/40 text-teal-200 border border-teal-800/50 hover:bg-teal-900/60"
              >
                Review
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ServiceGroupTable({
  group,
  health,
  dockerById,
  busyId,
  onDockerAction,
  onEnableVpn,
  enableVpnBusy,
}: {
  group: ServiceGroup;
  health: Record<string, Record<string, unknown>>;
  dockerById: Map<string, DockerServiceInfo>;
  busyId: string | null;
  onDockerAction: (svc: DockerServiceInfo, action: DockerAction) => void;
  onEnableVpn?: () => void;
  enableVpnBusy?: boolean;
}) {
  // CPU/RAM only for Docker-backed rows. External APIs / Disk / Knaben omit the columns.
  // Shared template on header + rows. Actions is fixed (not auto): each row is its
  // own grid, so auto would size from local content and shift the other columns.
  const showResources = group.rows.some((r) => !!r.dockerId);
  const gridTemplateColumns = showResources
    ? "minmax(0,1.2fr) 4.5rem 4.5rem 5.5rem minmax(0,1.4fr) 15.5rem"
    : "minmax(0,1.2fr) 5.5rem minmax(0,1.4fr) 15.5rem";
  const gridStyle = { gridTemplateColumns } as const;

  return (
    <section className="rounded-xl border border-gray-700 bg-gray-800/60 overflow-hidden">
      <div className="px-3 py-2 border-b border-gray-700/80">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500">
          {group.label}
        </h3>
      </div>
      {/* Desktop header */}
      <div
        className="hidden md:grid gap-2 px-3 py-1.5 text-[10px] uppercase tracking-wide text-gray-600 border-b border-gray-800"
        style={gridStyle}
      >
        <span>Service</span>
        {showResources && (
          <>
            <span className="text-right">CPU</span>
            <span className="text-right">RAM</span>
          </>
        )}
        <span>Status</span>
        <span>Detail</span>
        <span className="text-right">Actions</span>
      </div>
      <ul className="divide-y divide-gray-800">
        {group.rows.map((row) => {
          const probe = row.healthKey ? health[row.healthKey] : undefined;
          const docker = row.dockerId ? dockerById.get(row.dockerId) : undefined;
          const status = statusMeta(probe, docker);
          const stats = docker?.stats;
          const rowHasResources = showResources && !!row.dockerId;
          const cpu =
            rowHasResources && stats?.cpuPercent != null && Number.isFinite(stats.cpuPercent)
              ? `${stats.cpuPercent}%`
              : rowHasResources
                ? "—"
                : "";
          const ram =
            rowHasResources && stats?.memoryUsageBytes != null
              ? formatBytes(stats.memoryUsageBytes)
              : rowHasResources
                ? "—"
                : "";
          const detail = probe && row.detail ? row.detail(probe) : null;
          const openUrl = resolveOpenUrl(
            docker?.openUrl || (probe?.openUrl as string | undefined) || row.fallbackOpenUrl,
          );
          const uptimeExtra =
            row.id === "app" ? <AppLatencyHint /> : null;

          return (
            <li
              key={row.id}
              className="px-3 py-2 md:grid md:gap-2 md:items-center"
              style={gridStyle}
            >
              <div className="min-w-0 flex items-center gap-2">
                <span className="text-sm text-gray-100 font-medium truncate">{row.title}</span>
                {uptimeExtra}
              </div>
              {showResources && (
                <>
                  <div className="hidden md:block text-right text-xs text-gray-300 tabular-nums">
                    {cpu}
                  </div>
                  <div className="hidden md:block text-right text-xs text-gray-300 tabular-nums" title={
                    rowHasResources && stats?.memoryLimitBytes
                      ? `${formatBytes(stats.memoryUsageBytes)} / ${formatBytes(stats.memoryLimitBytes)}`
                      : undefined
                  }>
                    {ram}
                  </div>
                </>
              )}
              <div className="mt-1 md:mt-0">
                <span className={`inline-flex text-[11px] px-1.5 py-0.5 rounded border ${status.className}`}>
                  {status.label}
                </span>
              </div>
              <div className="mt-1 md:mt-0 text-[11px] text-gray-500 truncate min-w-0" title={detail || undefined}>
                {rowHasResources && (
                  <span className="md:hidden text-gray-600 mr-2">
                    CPU {cpu} · RAM {ram}
                  </span>
                )}
                {detail || (docker?.state?.status && docker.state.status !== "unknown"
                  ? docker.state.status.replace(/_/g, " ")
                  : "")}
              </div>
              <div className="mt-2 md:mt-0 flex flex-wrap items-center justify-start md:justify-end gap-1">
                <RowControls
                  docker={docker}
                  openUrl={openUrl}
                  busy={busyId === docker?.id}
                  onAction={onDockerAction}
                  onEnableVpn={row.dockerId === "gluetun" ? onEnableVpn : undefined}
                  enableVpnBusy={row.dockerId === "gluetun" ? enableVpnBusy : undefined}
                />
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function AppLatencyHint() {
  const { data, isError } = useQuery({
    queryKey: ["uptime-check"],
    queryFn: async () => {
      const started = performance.now();
      await api.get("/health");
      return { latency: Math.round(performance.now() - started) };
    },
    retry: 1,
    refetchInterval: 30_000,
  });
  if (isError) return <span className="text-[10px] text-red-400">liveness down</span>;
  if (!data) return null;
  return <span className="text-[10px] text-gray-500 tabular-nums">{data.latency} ms</span>;
}

function RowControls({
  docker,
  openUrl,
  busy,
  onAction,
  onEnableVpn,
  enableVpnBusy,
}: {
  docker?: DockerServiceInfo;
  openUrl: string | null;
  busy?: boolean;
  onAction: (svc: DockerServiceInfo, action: DockerAction) => void;
  onEnableVpn?: () => void;
  enableVpnBusy?: boolean;
}) {
  const btn =
    "inline-flex items-center justify-center gap-0.5 px-1.5 py-1 text-[11px] rounded border disabled:opacity-40 disabled:cursor-not-allowed";

  const controls: ReactNode[] = [];

  if (docker) {
    const running = !!docker.state?.running;
    const exists = !!docker.state?.exists;
    const can = (action: DockerAction) => docker.actions.includes(action);
    const socketOk = docker.available !== false;
    const disabled = busy || !socketOk;

    // gluetun lives behind compose profile vpn — Start fails until the profile
    // creates the container. Offer Enable VPN (host sidecar) when missing.
    if (docker.id === "gluetun" && onEnableVpn && !exists) {
      controls.push(
        <button
          key="enable-vpn"
          type="button"
          title="Register compose vpn profile and start gluetun (needs Mullvad keys in Integrations)"
          disabled={!!enableVpnBusy || !socketOk}
          onClick={() => onEnableVpn()}
          className={`${btn} bg-sky-950/50 text-sky-200 border-sky-800/50 hover:bg-sky-900/50`}
        >
          <Play size={11} /> {enableVpnBusy ? "…" : "Enable VPN"}
        </button>,
      );
    }

    if (can("start")) {
      controls.push(
        <button
          key="start"
          type="button"
          title={!socketOk ? docker.error || "Docker unavailable" : "Start"}
          disabled={disabled || running || (docker.id === "gluetun" && !exists)}
          onClick={() => onAction(docker, "start")}
          className={`${btn} bg-emerald-950/40 text-emerald-300 border-emerald-800/50 hover:bg-emerald-900/50`}
        >
          <Play size={11} /> Start
        </button>,
      );
    }
    if (can("stop")) {
      controls.push(
        <button
          key="stop"
          type="button"
          title={!socketOk ? docker.error || "Docker unavailable" : "Stop"}
          disabled={disabled || !running}
          onClick={() => onAction(docker, "stop")}
          className={`${btn} bg-red-950/40 text-red-300 border-red-800/50 hover:bg-red-900/50`}
        >
          <Square size={11} /> Stop
        </button>,
      );
    }
    if (can("restart")) {
      controls.push(
        <button
          key="restart"
          type="button"
          title={
            docker.isSelf
              ? "Restart Library app (brief outage)"
              : !socketOk
                ? docker.error || "Docker unavailable"
                : "Restart"
          }
          disabled={disabled || (!running && !docker.isSelf)}
          onClick={() => onAction(docker, "restart")}
          className={`${btn} bg-gray-900 text-gray-300 border-gray-700 hover:bg-gray-700`}
        >
          <RotateCcw size={11} className={busy ? "animate-spin" : ""} />{" "}
          {busy ? "…" : "Restart"}
        </button>,
      );
    }
  }

  if (openUrl) {
    controls.push(
      <a
        key="open"
        href={openUrl}
        target="_blank"
        rel="noopener noreferrer"
        title={`Open ${openUrl}`}
        className={`${btn} bg-teal-950/40 text-teal-200 border-teal-800/50 hover:bg-teal-900/50`}
      >
        <ExternalLink size={11} /> Open
      </a>,
    );
  }

  if (!controls.length) {
    return <span className="text-[11px] text-gray-600">—</span>;
  }

  return <>{controls}</>;
}

function KavitaEbookDebug() {
  const { data: debug, isLoading, refetch } = useQuery({
    queryKey: ["kavita-debug"],
    queryFn: async () => {
      const { data } = await api.get("/admin/kavita-debug");
      return data as {
        api_key_set?: boolean;
        series_api_ok?: boolean;
        series_count?: number;
        ebook_series_count?: number;
        ebook_count?: number;
        error?: string | null;
      };
    },
  });

  if (isLoading) return <p className="text-gray-500 text-xs pt-2">Loading…</p>;
  if (!debug) return null;

  return (
    <div className="pt-2 space-y-1 text-xs">
      <div className="flex justify-between gap-3">
        <span className="text-gray-500">API key</span>
        <span className={debug.api_key_set ? "text-emerald-400" : "text-red-400"}>
          {debug.api_key_set ? "Set" : "Missing"}
        </span>
      </div>
      <div className="flex justify-between gap-3">
        <span className="text-gray-500">Series API</span>
        <span className={debug.series_api_ok ? "text-emerald-400" : "text-red-400"}>
          {debug.series_api_ok ? "OK" : "Failed"}
        </span>
      </div>
      <div className="flex justify-between gap-3">
        <span className="text-gray-500">Ebook series</span>
        <span className="text-gray-300">{debug.ebook_series_count ?? "—"}</span>
      </div>
      <div className="flex justify-between gap-3">
        <span className="text-gray-500">Shelf ebooks</span>
        <span className="text-gray-300" title="Same volume/file expansion as My Library after refresh">
          {debug.ebook_count ?? "—"}
        </span>
      </div>
      <div className="flex justify-between gap-3">
        <span className="text-gray-500">All series</span>
        <span className="text-gray-300">{debug.series_count ?? "—"}</span>
      </div>
      {debug.error && (
        <p className="text-red-400 text-[11px] mt-1 p-2 bg-red-900/20 rounded">{debug.error}</p>
      )}
      <button
        type="button"
        onClick={() => void refetch()}
        className="mt-1 inline-flex items-center gap-1 text-[11px] text-gray-500 hover:text-gray-300"
      >
        <RefreshCw size={11} /> Refresh diagnostic
      </button>
    </div>

  );
}
