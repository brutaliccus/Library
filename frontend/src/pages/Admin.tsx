import { useState, type ReactNode } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import api from "../api/client";
import {
  Shield,
  Users,
  Download,
  Activity,
  Trash2,
  RefreshCw,
  Bell,
  Wrench,
  Radar,
  Settings2,
  EyeOff,
  ExternalLink,
} from "lucide-react";
import ScraperTab from "../components/admin/ScraperTab";
import ConfigTab from "../components/admin/ConfigTab";
import { usePushNotifications } from "../hooks/usePushNotifications";
import { Link, useSearchParams } from "react-router-dom";
import RequestStatusBadge from "../components/RequestStatus";
import RequestProgress from "../components/RequestProgress";
import Modal from "../components/Modal";
import { useToast } from "../contexts/ToastContext";

type Tab = "users" | "requests" | "scraper" | "health" | "config";

function resolveTab(raw: string | null): Tab {
  if (raw === "approvals") return "users";
  if (
    raw === "users" ||
    raw === "requests" ||
    raw === "scraper" ||
    raw === "health" ||
    raw === "config"
  ) {
    return raw;
  }
  return "users";
}

export default function AdminPage() {
  const [searchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState<Tab>(resolveTab(searchParams.get("tab")));
  const { state: pushState, error: pushError, subscribe: enablePush, unsubscribe: disablePush } = usePushNotifications();

  const tabs: { id: Tab; label: string; icon: typeof Shield }[] = [
    { id: "users", label: "Users", icon: Users },
    { id: "requests", label: "Requests", icon: Download },
    { id: "scraper", label: "Cache", icon: Radar },
    { id: "config", label: "Config", icon: Settings2 },
    { id: "health", label: "Health", icon: Activity },
  ];

  return (
    <div className="w-full max-w-5xl mx-auto px-4 py-8 min-w-0 overflow-x-hidden">
      <h1 className="text-2xl font-bold text-gray-100 mb-6 flex items-center gap-2">
        <Shield size={24} />
        Admin Panel
      </h1>

      {pushState !== "unsupported" && pushState !== "unavailable" && pushState !== "subscribed" && (
        <div className="mb-6 p-4 bg-gray-800/60 border border-gray-700 rounded-xl flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
          <div className="flex items-center gap-3">
            <Bell size={20} className="text-amber-400 shrink-0" />
            <div>
              <p className="text-sm font-medium text-gray-100">Admin push notifications</p>
              <p className="text-xs text-gray-500">Get notified when members join via invite, plus download status and errors</p>
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
            Push notifications enabled for admin alerts
          </div>
          <button
            onClick={disablePush}
            className="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200 border border-gray-600 rounded-lg hover:border-gray-500 transition-colors"
          >
            Disable
          </button>
        </div>
      )}

      <div className="mb-4">
        <Link
          to="/admin/setup"
          className="text-xs text-brand-400 hover:text-brand-300"
        >
          Open instance setup wizard →
        </Link>
      </div>

      <div className="grid grid-cols-5 gap-0.5 mb-6 bg-gray-900 rounded-lg p-0.5 w-full">
        {tabs.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => setActiveTab(id)}
            className={`flex items-center justify-center gap-1 px-1 sm:px-2 py-2 rounded-md text-[10px] sm:text-xs font-medium transition-colors min-w-0 ${
              activeTab === id
                ? "bg-gray-800 text-brand-400 shadow-sm"
                : "text-gray-400 hover:text-gray-200"
            }`}
          >
            <Icon size={12} className="shrink-0 hidden sm:block" />
            <span className="truncate">{label}</span>
          </button>
        ))}
      </div>

      {activeTab === "users" && <UsersTab />}
      {activeTab === "requests" && <AllRequestsTab />}
      {activeTab === "scraper" && <ScraperTab />}
      {activeTab === "config" && <ConfigTab />}
      {activeTab === "health" && <HealthTab />}
    </div>
  );
}

function UsersTab() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [disableUserModal, setDisableUserModal] = useState<number | null>(null);

  const { data: users, isLoading: usersLoading } = useQuery({
    queryKey: ["admin-users"],
    queryFn: async () => {
      const { data } = await api.get("/admin/users");
      return data as any[];
    },
  });

  const deleteUser = useMutation({
    mutationFn: async (id: number) => {
      await api.delete(`/admin/users/${id}`);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin-users"] });
      setDisableUserModal(null);
      toast("User disabled.", "info");
    },
  });

  const resetPw = useMutation({
    mutationFn: async (id: number) => {
      await api.post(`/admin/users/${id}/reset-password`);
    },
    onSuccess: () => {
      toast("Password reset to \"changeme\". User will be prompted to change it on next login.", "success");
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Failed to reset password", "error");
    },
  });

  if (usersLoading) return <div className="text-gray-500">Loading...</div>;

  return (
    <div className="space-y-6 min-w-0">
      <p className="text-sm text-gray-400">
        New members join with an invite link from Settings — no approval step. You can still
        reset passwords or disable accounts here.
      </p>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Users ({users?.length ?? 0})
        </h2>
        {!users?.length ? (
          <p className="text-center py-8 text-gray-500">No users yet</p>
        ) : (
          users.map((user: any) => (
            <div
              key={user.id}
              className="bg-gray-800 border border-gray-700 rounded-xl p-4 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3"
            >
              <div>
                <p className="font-semibold text-gray-100">
                  {user.username}
                  {user.role === "admin" && (
                    <span className="ml-2 text-xs bg-brand-900/30 text-brand-400 px-2 py-0.5 rounded-full">
                      admin
                    </span>
                  )}
                  {!user.is_active && (
                    <span className="ml-2 text-xs bg-red-900/30 text-red-400 px-2 py-0.5 rounded-full">
                      disabled
                    </span>
                  )}
                </p>
                <p className="text-xs text-gray-500">
                  Joined {new Date(user.created_at).toLocaleDateString()}
                </p>
              </div>
              <div className="flex gap-2 shrink-0">
                <button
                  onClick={() => resetPw.mutate(user.id)}
                  className="flex items-center gap-1 px-3 py-1.5 bg-gray-700 text-gray-300 text-sm rounded-lg hover:bg-gray-600"
                >
                  <RefreshCw size={14} /> Reset PW
                </button>
                {user.role !== "admin" && (
                  <button
                    onClick={() => setDisableUserModal(user.id)}
                    className="flex items-center gap-1 px-3 py-1.5 bg-red-900/30 text-red-400 text-sm rounded-lg hover:bg-red-900/50"
                  >
                    <Trash2 size={14} /> Disable
                  </button>
                )}
              </div>
            </div>
          ))
        )}
      </section>

      <Modal
        title="Disable user"
        show={disableUserModal !== null}
        onClose={() => setDisableUserModal(null)}
      >
        <p className="text-sm text-gray-400 mb-4">Disable this user? They will no longer be able to log in.</p>
        <div className="flex gap-2 justify-end">
          <button
            type="button"
            onClick={() => setDisableUserModal(null)}
            className="px-3 py-1.5 text-gray-300 hover:bg-gray-700 rounded-lg"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => disableUserModal !== null && deleteUser.mutate(disableUserModal)}
            disabled={deleteUser.isPending}
            className="px-3 py-1.5 bg-red-600 text-white rounded-lg hover:bg-red-500 disabled:opacity-50"
          >
            {deleteUser.isPending ? "Disabling..." : "Disable"}
          </button>
        </div>
      </Modal>
    </div>
  );
}

function AllRequestsTab() {
  const { data: requests, isLoading } = useQuery({
    queryKey: ["admin-downloads"],
    queryFn: async () => {
      const { data } = await api.get("/admin/download-requests");
      return data as any[];
    },
    refetchInterval: 10000,
  });

  if (isLoading) return <div className="text-gray-500">Loading...</div>;

  if (!requests?.length) {
    return (
      <div className="text-center py-12 text-gray-500">No download requests yet</div>
    );
  }

  return (
    <div className="space-y-3 min-w-0">
      {requests.map((req: any) => (
        <div
          key={req.id}
          className="bg-gray-800 border border-gray-700 rounded-xl p-4"
        >
          <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 min-w-0">
                <h3 className="font-semibold text-gray-100 truncate">
                  {req.title}
                </h3>
                {req.is_private && (
                  <span
                    className="inline-flex items-center gap-1 shrink-0 text-[10px] font-medium uppercase tracking-wide px-1.5 py-0.5 rounded bg-purple-900/40 text-purple-300 border border-purple-700/40"
                    title="Requested in private mode — hidden from other members' library browse"
                  >
                    <EyeOff size={11} />
                    Private
                  </span>
                )}
              </div>
              <div className="flex items-center gap-3 mt-1 text-xs text-gray-500">
                <span>by {req.username}</span>
                <span>{new Date(req.created_at).toLocaleString()}</span>
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
            <RequestStatusBadge status={req.status} detail={req.status_detail} />
          </div>
        </div>
      ))}
    </div>
  );
}

function HealthTab() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const { data: health, isLoading, refetch } = useQuery({
    queryKey: ["admin-health"],
    queryFn: async () => {
      const { data } = await api.get("/admin/health");
      return data;
    },
  });

  const fixMetadata = useMutation({
    mutationFn: async () => {
      const { data } = await api.post("/admin/abs/fix-metadata");
      return data as {
        fixed: { itemId: string; oldTitle: string; newTitle: string }[];
        count: number;
        scan_ran: boolean;
        orphan_cleanup_ok: boolean;
        items_examined: number;
        fetch_error?: string | null;
      };
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["abs-collection"] });
      queryClient.invalidateQueries({ queryKey: ["abs-series"] });
      const fixed = Array.isArray(data.fixed) ? data.fixed : [];
      const bits: string[] = [];
      if (data.scan_ran) {
        bits.push(
          data.orphan_cleanup_ok
            ? "Library scan finished; Audiobookshelf removed entries whose files are missing."
            : "Library scan finished.",
        );
      } else {
        bits.push("Library scan did not complete; check ABS connectivity and logs.");
      }
      if (data.items_examined > 0) {
        bits.push(`Checked ${data.items_examined} item(s) for title vs. folder name mismatches.`);
      }
      if (data.count > 0) {
        const sample = fixed
          .slice(0, 6)
          .map((f) => `"${f.oldTitle}" → "${f.newTitle}"`)
          .join("; ");
        bits.push(
          `Updated ${data.count} title(s)${fixed.length > 6 ? " (showing first 6)" : ""}: ${sample}${fixed.length > 6 ? " …" : ""}`,
        );
        toast(bits.join(" "), "success");
      } else {
        bits.push(
          "No title/folder mismatches left. Orphaned rows from an old folder layout are removed when their files are missing after a scan.",
        );
        toast(bits.join(" "), data.scan_ran ? "success" : "info");
      }
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Failed to fix metadata", "error");
    },
  });

  if (isLoading) return <div className="text-gray-500">Loading...</div>;
  if (!health) return null;

  const h = health as Record<string, any>;
  const svc = (key: string) => h[key] || {};

  return (
    <div className="space-y-4 min-w-0">
      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => refetch()}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-800 text-gray-300 text-sm rounded-lg hover:bg-gray-700 border border-gray-700"
        >
          <RefreshCw size={14} /> Refresh
        </button>
        <button
          type="button"
          title="Runs a full Audiobookshelf library scan, removes library rows whose files are missing (e.g. after a folder restructure), then fixes titles that still differ from the folder name."
          onClick={() => fixMetadata.mutate()}
          disabled={fixMetadata.isPending}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-purple-900/50 text-purple-300 text-sm rounded-lg hover:bg-purple-900/70 border border-purple-800/50 disabled:opacity-50"
        >
          <Wrench size={14} className={fixMetadata.isPending ? "animate-spin" : ""} />
          {fixMetadata.isPending ? "Scanning…" : "Scan ABS & fix metadata"}
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <HealthCard
          title="Real-Debrid"
          configured={svc("real_debrid").configured !== false}
          connected={!!svc("real_debrid").connected}
          items={[
            { label: "User", value: svc("real_debrid").username || "N/A" },
            { label: "Premium", value: svc("real_debrid").premium ? "Yes" : "No" },
            { label: "Points", value: String(svc("real_debrid").points ?? "N/A") },
            ...(svc("real_debrid").error
              ? [{ label: "Error", value: String(svc("real_debrid").error) }]
              : []),
          ]}
        />
        <HealthCard
          title="Torbox"
          configured={!!svc("torbox").configured}
          connected={!!svc("torbox").connected}
          items={[
            { label: "User", value: svc("torbox").username || "N/A" },
            { label: "Plan", value: String(svc("torbox").plan ?? "N/A") },
            ...(svc("torbox").error
              ? [{ label: "Error", value: String(svc("torbox").error) }]
              : []),
          ]}
        />
        <HealthCard
          title="Audiobookshelf"
          configured={svc("audiobookshelf").configured !== false}
          connected={!!svc("audiobookshelf").connected}
          items={[
            { label: "URL", value: svc("audiobookshelf").url || "N/A" },
          ]}
        />
        <HealthCard
          title="Kavita"
          configured={svc("kavita").configured !== false}
          connected={!!svc("kavita").connected}
          items={[
            { label: "URL", value: svc("kavita").url || "N/A" },
          ]}
        />
        <HealthCard
          title="Prowlarr"
          configured={!!svc("prowlarr").configured}
          connected={!!svc("prowlarr").connected}
          items={[
            { label: "URL", value: svc("prowlarr").url || "N/A" },
            { label: "Version", value: String(svc("prowlarr").version ?? "N/A") },
            { label: "Indexers", value: String(svc("prowlarr").indexers ?? "N/A") },
            ...(svc("prowlarr").error
              ? [{ label: "Error", value: String(svc("prowlarr").error) }]
              : []),
          ]}
        />
        <HealthCard
          title="Jackett"
          configured={!!svc("jackett").configured}
          connected={!!svc("jackett").connected}
          items={[
            { label: "URL", value: svc("jackett").url || "N/A" },
            { label: "API key", value: svc("jackett").apiKey ? "Set" : "Missing" },
            ...(svc("jackett").error
              ? [{ label: "Error", value: String(svc("jackett").error) }]
              : []),
          ]}
        />
        <HealthCard
          title="FlareSolverr"
          configured={!!svc("flaresolverr").configured}
          connected={!!svc("flaresolverr").connected}
          items={[
            { label: "URL", value: svc("flaresolverr").url || "N/A" },
            { label: "Version", value: String(svc("flaresolverr").version ?? "N/A") },
            ...(svc("flaresolverr").error
              ? [{ label: "Error", value: String(svc("flaresolverr").error) }]
              : []),
          ]}
        />
        <HealthCard
          title="Mullvad (ABB proxy)"
          configured={!!svc("mullvad_proxy").configured}
          connected={!!svc("mullvad_proxy").connected}
          items={[
            { label: "Proxy", value: svc("mullvad_proxy").proxy || "N/A" },
            { label: "Exit IP", value: svc("mullvad_proxy").exitIp || "N/A" },
            {
              label: "Mullvad exit",
              value:
                svc("mullvad_proxy").mullvadExit == null
                  ? "N/A"
                  : svc("mullvad_proxy").mullvadExit
                    ? "Yes"
                    : "No",
            },
            { label: "Location", value: svc("mullvad_proxy").country || "N/A" },
            ...(svc("mullvad_proxy").error
              ? [{ label: "Error", value: String(svc("mullvad_proxy").error) }]
              : []),
          ]}
        />
        <HealthCard
          title="Knaben"
          configured={svc("knaben").configured !== false}
          connected={!!svc("knaben").connected}
          items={[
            { label: "RSS", value: svc("knaben").url || "N/A" },
            ...(svc("knaben").error
              ? [{ label: "Error", value: String(svc("knaben").error) }]
              : []),
          ]}
        />
        <HealthCard
          title="Open Library catalog"
          configured={!!svc("ol_catalog").configured}
          connected={!!svc("ol_catalog").connected}
          items={[
            {
              label: "Works",
              value:
                svc("ol_catalog").works != null
                  ? Number(svc("ol_catalog").works).toLocaleString()
                  : "N/A",
            },
            { label: "Path", value: svc("ol_catalog").path || "N/A" },
            ...(svc("ol_catalog").error
              ? [{ label: "Error", value: String(svc("ol_catalog").error) }]
              : []),
          ]}
        />
        <HealthCard
          title="NYT Books API"
          configured={!!svc("nyt").configured}
          connected={!!svc("nyt").connected}
          items={[
            { label: "Lists", value: String(svc("nyt").lists ?? "N/A") },
            ...(svc("nyt").error
              ? [{ label: "Error", value: String(svc("nyt").error) }]
              : []),
          ]}
        />
        <KavitaEbookDebug />
        <IntegrationsCard />
        <HealthCard
          title="LibraForge"
          configured={svc("libraforge").configured !== false}
          connected={!!svc("libraforge").connected}
          items={[
            { label: "URL", value: svc("libraforge").url || "N/A" },
            {
              label: "Workflow",
              value: "Dry-run → backup → apply → Scan ABS",
            },
            ...(svc("libraforge").error
              ? [{ label: "Error", value: String(svc("libraforge").error) }]
              : []),
          ]}
          action={
            svc("libraforge").url ? (
              <a
                href={String(svc("libraforge").url)}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 px-3 py-1.5 mt-3 text-sm rounded-lg bg-teal-900/40 text-teal-200 border border-teal-800/50 hover:bg-teal-900/60"
              >
                <ExternalLink size={14} /> Open LibraForge
              </a>
            ) : null
          }
        />
        <HealthCard
          title="Disk Space"
          configured={svc("disk").configured !== false}
          connected={svc("disk").connected !== false}
          items={[
            { label: "Total", value: `${svc("disk").total_gb ?? "?"} GB` },
            { label: "Used", value: `${svc("disk").used_gb ?? "?"} GB` },
            { label: "Free", value: `${svc("disk").free_gb ?? "?"} GB` },
            { label: "Path", value: svc("disk").path || "N/A" },
          ]}
        />
      </div>
    </div>
  );
}

interface IntegrationsResponse {
  nyt?: { configured: boolean; overridden: boolean; hint: string };
  isbndb?: { configured: boolean; overridden: boolean; hint: string };
  hardcover?: { configured: boolean; overridden: boolean; hint: string };
  mullvad?: {
    configured: boolean;
    overridden: boolean;
    hint: string;
    note?: string;
  };
}

function IntegrationsCard() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [nytKey, setNytKey] = useState("");
  const [isbndbKey, setIsbndbKey] = useState("");
  const [hardcoverKey, setHardcoverKey] = useState("");
  const [mullvadAcct, setMullvadAcct] = useState("");

  const { data } = useQuery<IntegrationsResponse>({
    queryKey: ["admin-integrations"],
    queryFn: async () => {
      const { data } = await api.get("/admin/integrations");
      return data as IntegrationsResponse;
    },
  });

  const saveNyt = useMutation({
    mutationFn: async (value: string) => {
      const { data } = await api.put("/admin/integrations", { nyt_api_key: value });
      return data as IntegrationsResponse;
    },
    onSuccess: () => {
      setNytKey("");
      queryClient.invalidateQueries({ queryKey: ["admin-integrations"] });
      queryClient.invalidateQueries({ queryKey: ["trending-books"] });
      toast("NYT API key saved", "success");
    },
    onError: () => toast("Failed to save NYT key", "error"),
  });

  const saveIsbndb = useMutation({
    mutationFn: async (value: string) => {
      const { data } = await api.put("/admin/integrations", { isbndb_api_key: value });
      return data as IntegrationsResponse;
    },
    onSuccess: () => {
      setIsbndbKey("");
      queryClient.invalidateQueries({ queryKey: ["admin-integrations"] });
      toast("ISBNdb API key saved", "success");
    },
    onError: () => toast("Failed to save ISBNdb key", "error"),
  });

  const saveHardcover = useMutation({
    mutationFn: async (value: string) => {
      const { data } = await api.put("/admin/integrations", { hardcover_api_key: value });
      return data as IntegrationsResponse;
    },
    onSuccess: () => {
      setHardcoverKey("");
      queryClient.invalidateQueries({ queryKey: ["admin-integrations"] });
      queryClient.invalidateQueries({ queryKey: ["curated-carousel"] });
      toast("Hardcover API key saved", "success");
    },
    onError: () => toast("Failed to save Hardcover key", "error"),
  });

  const saveMullvad = useMutation({
    mutationFn: async (value: string) => {
      const { data } = await api.put("/admin/integrations", {
        mullvad_account_number: value,
      });
      return data as IntegrationsResponse;
    },
    onSuccess: () => {
      setMullvadAcct("");
      queryClient.invalidateQueries({ queryKey: ["admin-integrations"] });
      toast(
        "Mullvad account saved — restart gluetun on the Pi to apply (docker compose restart gluetun jackett)",
        "success"
      );
    },
    onError: () => toast("Failed to save Mullvad account", "error"),
  });

  const nyt = data?.nyt;
  const isbndb = data?.isbndb;
  const hardcover = data?.hardcover;
  const mullvad = data?.mullvad;

  return (
    <div className="md:col-span-2 bg-gray-800 border border-gray-700 rounded-xl p-4 space-y-5">
      <h3 className="font-semibold text-gray-100">Integrations</h3>

      <div className="space-y-2 text-sm">
        <div className="flex items-center justify-between">
          <span className="text-gray-300">NYT Books API (trending)</span>
          <span className={nyt?.configured ? "text-emerald-400" : "text-gray-500"}>
            {nyt?.configured
              ? `Configured${nyt.overridden ? "" : " (env)"}${nyt.hint ? ` · ${nyt.hint}` : ""}`
              : "Not set"}
          </span>
        </div>
        <div className="flex flex-col sm:flex-row gap-2">
          <input
            type="password"
            value={nytKey}
            onChange={(e) => setNytKey(e.target.value)}
            placeholder="Enter NYT API key"
            autoComplete="off"
            className="flex-1 min-w-0 px-3 py-1.5 bg-gray-900 border border-gray-700 rounded-lg text-gray-100 text-sm focus:outline-none focus:border-gray-500"
          />
          <button
            type="button"
            onClick={() => saveNyt.mutate(nytKey.trim())}
            disabled={saveNyt.isPending || !nytKey.trim()}
            className="px-3 py-1.5 bg-emerald-900/50 text-emerald-300 text-sm rounded-lg hover:bg-emerald-900/70 border border-emerald-800/50 disabled:opacity-50"
          >
            {saveNyt.isPending ? "Saving…" : "Save"}
          </button>
          {nyt?.overridden && (
            <button
              type="button"
              onClick={() => saveNyt.mutate("")}
              disabled={saveNyt.isPending}
              className="px-3 py-1.5 bg-gray-900 text-gray-400 text-sm rounded-lg hover:text-gray-200 border border-gray-700 disabled:opacity-50"
            >
              Clear
            </button>
          )}
        </div>
        <p className="text-xs text-gray-500">
          Free key from developer.nytimes.com. Powers real bestseller matching on the
          Trending shelf.
        </p>
      </div>

      <div className="space-y-2 text-sm border-t border-gray-700 pt-4">
        <div className="flex items-center justify-between">
          <span className="text-gray-300">ISBNdb API (catalog)</span>
          <span className={isbndb?.configured ? "text-emerald-400" : "text-gray-500"}>
            {isbndb?.configured
              ? `Configured${isbndb.overridden ? "" : " (env)"}${isbndb.hint ? ` · ${isbndb.hint}` : ""}`
              : "Not set"}
          </span>
        </div>
        <div className="flex flex-col sm:flex-row gap-2">
          <input
            type="password"
            value={isbndbKey}
            onChange={(e) => setIsbndbKey(e.target.value)}
            placeholder="Enter ISBNdb REST key"
            autoComplete="off"
            className="flex-1 min-w-0 px-3 py-1.5 bg-gray-900 border border-gray-700 rounded-lg text-gray-100 text-sm focus:outline-none focus:border-gray-500"
          />
          <button
            type="button"
            onClick={() => saveIsbndb.mutate(isbndbKey.trim())}
            disabled={saveIsbndb.isPending || !isbndbKey.trim()}
            className="px-3 py-1.5 bg-emerald-900/50 text-emerald-300 text-sm rounded-lg hover:bg-emerald-900/70 border border-emerald-800/50 disabled:opacity-50"
          >
            {saveIsbndb.isPending ? "Saving…" : "Save"}
          </button>
          {isbndb?.overridden && (
            <button
              type="button"
              onClick={() => saveIsbndb.mutate("")}
              disabled={saveIsbndb.isPending}
              className="px-3 py-1.5 bg-gray-900 text-gray-400 text-sm rounded-lg hover:text-gray-200 border border-gray-700 disabled:opacity-50"
            >
              Clear
            </button>
          )}
        </div>
        <p className="text-xs text-gray-500">
          Key from isbndb.com — fills catalog gaps beyond the local Open Library dump
          (~100M+ titles). Used for store search fallback and torrent matching.
        </p>
      </div>

      <div className="space-y-2 text-sm border-t border-gray-700 pt-4">
        <div className="flex items-center justify-between">
          <span className="text-gray-300">Hardcover API (ratings / series / lists)</span>
          <span className={hardcover?.configured ? "text-emerald-400" : "text-gray-500"}>
            {hardcover?.configured
              ? `Configured${hardcover.overridden ? "" : " (env)"}${hardcover.hint ? ` · ${hardcover.hint}` : ""}`
              : "Not set"}
          </span>
        </div>
        <div className="flex flex-col sm:flex-row gap-2">
          <input
            type="password"
            value={hardcoverKey}
            onChange={(e) => setHardcoverKey(e.target.value)}
            placeholder="Bearer token or raw key"
            autoComplete="off"
            className="flex-1 min-w-0 px-3 py-1.5 bg-gray-900 border border-gray-700 rounded-lg text-gray-100 text-sm focus:outline-none focus:border-gray-500"
          />
          <button
            type="button"
            onClick={() => saveHardcover.mutate(hardcoverKey.trim())}
            disabled={saveHardcover.isPending || !hardcoverKey.trim()}
            className="px-3 py-1.5 bg-emerald-900/50 text-emerald-300 text-sm rounded-lg hover:bg-emerald-900/70 border border-emerald-800/50 disabled:opacity-50"
          >
            {saveHardcover.isPending ? "Saving…" : "Save"}
          </button>
          {hardcover?.overridden && (
            <button
              type="button"
              onClick={() => saveHardcover.mutate("")}
              disabled={saveHardcover.isPending}
              className="px-3 py-1.5 bg-gray-900 text-gray-400 text-sm rounded-lg hover:text-gray-200 border border-gray-700 disabled:opacity-50"
            >
              Clear
            </button>
          )}
        </div>
        <p className="text-xs text-gray-500">
          Token from hardcover.app/account/api — used only for public book ratings, series
          graphs, and curated lists. Does not sync your Hardcover account or library.
        </p>
      </div>

      <div className="space-y-2 text-sm border-t border-gray-700 pt-4">
        <div className="flex items-center justify-between">
          <span className="text-gray-300">Mullvad (ABB only via VPN)</span>
          <span className={mullvad?.configured ? "text-emerald-400" : "text-gray-500"}>
            {mullvad?.configured
              ? `Configured${mullvad.overridden ? "" : " (env)"}${mullvad.hint ? ` · ${mullvad.hint}` : ""}`
              : "Not set"}
          </span>
        </div>
        <div className="flex flex-col sm:flex-row gap-2">
          <input
            type="password"
            value={mullvadAcct}
            onChange={(e) => setMullvadAcct(e.target.value)}
            placeholder="16-digit Mullvad account number"
            autoComplete="off"
            className="flex-1 min-w-0 px-3 py-1.5 bg-gray-900 border border-gray-700 rounded-lg text-gray-100 text-sm focus:outline-none focus:border-gray-500"
          />
          <button
            type="button"
            onClick={() => saveMullvad.mutate(mullvadAcct.trim())}
            disabled={saveMullvad.isPending || !mullvadAcct.trim()}
            className="px-3 py-1.5 bg-emerald-900/50 text-emerald-300 text-sm rounded-lg hover:bg-emerald-900/70 border border-emerald-800/50 disabled:opacity-50"
          >
            {saveMullvad.isPending ? "Saving…" : "Save"}
          </button>
          {mullvad?.overridden && (
            <button
              type="button"
              onClick={() => saveMullvad.mutate("")}
              disabled={saveMullvad.isPending}
              className="px-3 py-1.5 bg-gray-900 text-gray-400 text-sm rounded-lg hover:text-gray-200 border border-gray-700 disabled:opacity-50"
            >
              Clear
            </button>
          )}
        </div>
        <p className="text-xs text-gray-500">
          {mullvad?.note ||
            "Only AudioBook Bay Flare/RSS/search egress via Mullvad. The rest of the stack stays on your LAN."}
        </p>
      </div>
    </div>
  );
}

function KavitaEbookDebug() {
  const { data: debug, isLoading, refetch } = useQuery({
    queryKey: ["kavita-debug"],
    queryFn: async () => {
      const { data } = await api.get("/admin/kavita-debug");
      return data;
    },
  });

  if (isLoading) return <div className="text-gray-500 text-sm">Loading ebook diagnostic...</div>;
  if (!debug) return null;

  const ok = !debug.error && debug.series_api_ok;
  return (
    <div className="md:col-span-2 bg-gray-800 border border-gray-700 rounded-xl p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-semibold text-gray-100">Kavita Ebooks Diagnostic</h3>
        <button
          onClick={() => refetch()}
          className="flex items-center gap-1 px-2 py-1 text-xs text-gray-400 hover:text-gray-200 rounded"
        >
          <RefreshCw size={12} /> Refresh
        </button>
      </div>
      <div className="space-y-1.5 text-sm">
        <div className="flex justify-between">
          <span className="text-gray-400">API key set</span>
          <span className={debug.api_key_set ? "text-emerald-400" : "text-red-400"}>
            {debug.api_key_set ? "Yes" : "No"}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-400">Series API</span>
          <span className={debug.series_api_ok ? "text-emerald-400" : "text-red-400"}>
            {debug.series_api_ok ? "OK" : "Failed"}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-400">Total series</span>
          <span className="text-gray-200">{debug.series_count ?? "—"}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-400">Ebooks (EPUB/PDF)</span>
          <span className="text-gray-200">{debug.ebook_count ?? "—"}</span>
        </div>
        {debug.error && (
          <p className="text-red-400 text-xs mt-2 p-2 bg-red-900/20 rounded">{debug.error}</p>
        )}
      </div>
    </div>
  );
}

function HealthCard({
  title,
  connected,
  configured = true,
  items,
  action,
}: {
  title: string;
  connected: boolean;
  configured?: boolean;
  items: { label: string; value: string }[];
  action?: ReactNode;
}) {
  const dot =
    !configured ? "bg-amber-500" : connected ? "bg-green-500" : "bg-red-500";
  const status = !configured ? "Not configured" : connected ? "OK" : "Down";

  return (
    <div className="bg-gray-800 border border-gray-700 rounded-xl p-4">
      <div className="flex items-center gap-2 mb-3">
        <div className={`w-2 h-2 rounded-full ${dot}`} title={status} />
        <h3 className="font-semibold text-gray-100">{title}</h3>
        <span className="ml-auto text-xs text-gray-500">{status}</span>
      </div>
      <div className="space-y-1.5">
        {items.map(({ label, value }) => (
          <div key={label} className="flex justify-between gap-3 text-sm">
            <span className="text-gray-400 shrink-0">{label}</span>
            <span className="text-gray-200 font-medium text-right break-all min-w-0">{value}</span>
          </div>
        ))}
      </div>
      {action}
    </div>
  );
}
