import { useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Activity,
  ClipboardList,
  Copy,
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
    refetchInterval: 30_000,
  });

  const { data: pending, refetch: refetchPending } = useQuery({
    queryKey: ["admin-health-pending"],
    queryFn: async () => {
      const { data } = await api.get("/admin/health/pending-actions");
      return data as PendingActionsResponse;
    },
    refetchInterval: 20_000,
  });

  const { data: dockerInfo, refetch: refetchDocker } = useQuery({
    queryKey: ["admin-docker-services"],
    queryFn: async () => {
      const { data } = await api.get("/admin/docker/services");
      return data as DockerServicesResponse;
    },
    retry: 1,
    refetchInterval: 15_000,
  });

  const dockerById = useMemo(() => {
    const map = new Map<string, DockerServiceInfo>();
    for (const s of dockerInfo?.services || []) map.set(s.id, s);
    return map;
  }, [dockerInfo]);

  const refreshHealth = async () => {
    await Promise.all([refetch(), refetchDocker(), refetchPending()]);
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

      <div className="space-y-4">
        {SERVICE_GROUPS.map((group) => (
          <ServiceGroupTable
            key={group.id}
            group={group}
            health={health}
            dockerById={dockerById}
            busyId={busyId}
            onDockerAction={runDockerAction}
          />
        ))}
      </div>

            <div className="rounded-xl border border-gray-800 bg-gray-900/40 overflow-hidden">
        <div className="px-3 py-2 border-b border-gray-800 flex items-center gap-2">
          <GitBranch size={14} className="text-gray-500 shrink-0" />
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-gray-100">Update from Git</h3>
            <p className="text-[11px] text-gray-500 mt-0.5">
              Run on the host install root (SSH). Keeps .env, media, and NPM; rebuilds the app from origin/main.
            </p>
          </div>
        </div>
        <div className="px-3 py-3 space-y-2">
          <pre className="text-[11px] text-gray-300 bg-gray-950/60 border border-gray-800 rounded-lg px-3 py-2 overflow-x-auto whitespace-pre-wrap break-all">
{`cd /opt/library && bash scripts/update_library.sh`}
          </pre>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => {
                const cmd = "cd /opt/library && bash scripts/update_library.sh";
                void navigator.clipboard.writeText(cmd).then(
                  () => toast("Copied update command", "success"),
                  () => toast("Could not copy — select the command manually", "error"),
                );
              }}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-gray-800 text-gray-300 text-sm rounded-lg hover:bg-gray-700 border border-gray-700"
            >
              <Copy size={14} /> Copy command
            </button>
            <a
              href="https://github.com/brutaliccus/Library/blob/main/docs/ubuntu-server-install.md#updating"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-teal-300/90 hover:text-teal-200"
            >
              <ExternalLink size={14} /> Docs
            </a>
          </div>
          <p className="text-[11px] text-gray-600">
            Dirty tree? Use <code className="text-gray-400">--force</code> only when you intend to discard local tracked edits.
            Windows: <code className="text-gray-400">.\scripts\update_library.ps1</code>
          </p>
        </div>
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
}: {
  group: ServiceGroup;
  health: Record<string, Record<string, unknown>>;
  dockerById: Map<string, DockerServiceInfo>;
  busyId: string | null;
  onDockerAction: (svc: DockerServiceInfo, action: DockerAction) => void;
}) {
  // CPU/RAM only for Docker-backed rows. External APIs / Disk / Knaben omit the columns.
  const showResources = group.rows.some((r) => !!r.dockerId);
  const gridCols = showResources
    ? "md:grid-cols-[minmax(0,1.4fr)_4.5rem_4.5rem_5.5rem_minmax(0,1fr)_auto]"
    : "md:grid-cols-[minmax(0,1.4fr)_5.5rem_minmax(0,1fr)_auto]";

  return (
    <section className="rounded-xl border border-gray-700 bg-gray-800/60 overflow-hidden">
      <div className="px-3 py-2 border-b border-gray-700/80">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500">
          {group.label}
        </h3>
      </div>
      {/* Desktop header */}
      <div className={`hidden md:grid ${gridCols} gap-2 px-3 py-1.5 text-[10px] uppercase tracking-wide text-gray-600 border-b border-gray-800`}>
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
              className={`px-3 py-2 md:grid ${gridCols} md:gap-2 md:items-center`}
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
}: {
  docker?: DockerServiceInfo;
  openUrl: string | null;
  busy?: boolean;
  onAction: (svc: DockerServiceInfo, action: DockerAction) => void;
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

    if (can("start")) {
      controls.push(
        <button
          key="start"
          type="button"
          title={!socketOk ? docker.error || "Docker unavailable" : "Start"}
          disabled={disabled || running}
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
