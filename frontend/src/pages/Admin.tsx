import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import api from "../api/client";
import {
  Shield,
  Users,
  Download,
  Activity,
  Check,
  X,
  Trash2,
  RefreshCw,
  Bell,
  Wrench,
  Radar,
} from "lucide-react";
import ScraperTab from "../components/admin/ScraperTab";
import { usePushNotifications } from "../hooks/usePushNotifications";
import { useSearchParams } from "react-router-dom";
import RequestStatusBadge from "../components/RequestStatus";
import RequestProgress from "../components/RequestProgress";
import Modal from "../components/Modal";
import { useToast } from "../contexts/ToastContext";

type Tab = "approvals" | "users" | "requests" | "health" | "scraper";

export default function AdminPage() {
  const [searchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState<Tab>(
    (searchParams.get("tab") as Tab) || "approvals"
  );
  const { state: pushState, error: pushError, subscribe: enablePush, unsubscribe: disablePush } = usePushNotifications();

  const tabs: { id: Tab; label: string; icon: typeof Shield }[] = [
    { id: "approvals", label: "Approvals", icon: Shield },
    { id: "users", label: "Users", icon: Users },
    { id: "requests", label: "All Requests", icon: Download },
    { id: "scraper", label: "Indexer Cache", icon: Radar },
    { id: "health", label: "System Health", icon: Activity },
  ];

  return (
    <div className="max-w-5xl mx-auto px-4 py-8">
      <h1 className="text-2xl font-bold text-gray-100 mb-6 flex items-center gap-2">
        <Shield size={24} />
        Admin Panel
      </h1>

      {pushState !== "unsupported" && pushState !== "unavailable" && pushState !== "subscribed" && (
        <div className="mb-6 p-4 bg-gray-800/60 border border-gray-700 rounded-xl flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <Bell size={20} className="text-amber-400 shrink-0" />
            <div>
              <p className="text-sm font-medium text-gray-100">Admin push notifications</p>
              <p className="text-xs text-gray-500">Get notified for new account requests, download status, and errors</p>
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

      <div className="flex gap-1 mb-6 bg-gray-900 rounded-lg p-1">
        {tabs.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => setActiveTab(id)}
            className={`flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium transition-colors flex-1 justify-center ${
              activeTab === id
                ? "bg-gray-800 text-brand-400 shadow-sm"
                : "text-gray-400 hover:text-gray-200"
            }`}
          >
            <Icon size={15} />
            {label}
          </button>
        ))}
      </div>

      {activeTab === "approvals" && <ApprovalsTab />}
      {activeTab === "users" && <UsersTab />}
      {activeTab === "requests" && <AllRequestsTab />}
      {activeTab === "scraper" && <ScraperTab />}
      {activeTab === "health" && <HealthTab />}
    </div>
  );
}

function ApprovalsTab() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [denyModal, setDenyModal] = useState<{ requestId: number } | null>(null);
  const [denyReason, setDenyReason] = useState("");

  const { data: requests, isLoading } = useQuery({
    queryKey: ["admin-account-requests"],
    queryFn: async () => {
      const { data } = await api.get("/admin/account-requests?status_filter=pending");
      return data as any[];
    },
  });

  const approve = useMutation({
    mutationFn: async (id: number) => {
      await api.post(`/admin/account-requests/${id}/approve`, {});
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin-account-requests"] });
      toast("Account approved. They can log in with password \"changeme\" and will be prompted to change it.", "success");
    },
  });

  const deny = useMutation({
    mutationFn: async ({ id, reason }: { id: number; reason: string | null }) => {
      await api.post(`/admin/account-requests/${id}/deny`, { reason: reason || undefined });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin-account-requests"] });
      setDenyModal(null);
      setDenyReason("");
      toast("Account request denied.", "info");
    },
  });

  const submitDeny = () => {
    if (denyModal) {
      deny.mutate({ id: denyModal.requestId, reason: denyReason.trim() || null });
    }
  };

  if (isLoading) return <div className="text-gray-500">Loading...</div>;

  if (!requests?.length) {
    return (
      <div className="text-center py-12 text-gray-500">
        No pending account requests
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {requests.map((req: any) => (
        <div
          key={req.id}
          className="bg-gray-800 border border-gray-700 rounded-xl p-4 flex items-center justify-between gap-4"
        >
          <div>
            <p className="font-semibold text-gray-100">{req.username}</p>
            {req.email && (
              <p className="text-sm text-gray-400">{req.email}</p>
            )}
            {req.reason && (
              <p className="text-sm text-gray-400 mt-1 italic">"{req.reason}"</p>
            )}
            <p className="text-xs text-gray-500 mt-1">
              {new Date(req.created_at).toLocaleString()}
            </p>
          </div>
          <div className="flex gap-2 shrink-0">
            <button
              onClick={() => approve.mutate(req.id)}
              disabled={approve.isPending}
              className="flex items-center gap-1 px-3 py-1.5 bg-green-600 text-white text-sm rounded-lg hover:bg-green-500 disabled:opacity-50"
            >
              <Check size={14} /> Approve
            </button>
            <button
              onClick={() => setDenyModal({ requestId: req.id })}
              disabled={deny.isPending}
              className="flex items-center gap-1 px-3 py-1.5 bg-red-600 text-white text-sm rounded-lg hover:bg-red-500 disabled:opacity-50"
            >
              <X size={14} /> Deny
            </button>
          </div>
        </div>
      ))}

      <Modal
        title="Deny account request"
        show={denyModal !== null}
        onClose={() => { setDenyModal(null); setDenyReason(""); }}
      >
        <p className="text-sm text-gray-400 mb-3">Optionally provide a reason to show the user.</p>
        <textarea
          value={denyReason}
          onChange={(e) => setDenyReason(e.target.value)}
          placeholder="Reason (optional)"
          rows={3}
          className="w-full px-3 py-2 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 placeholder:text-gray-500 mb-4"
        />
        <div className="flex gap-2 justify-end">
          <button
            type="button"
            onClick={() => { setDenyModal(null); setDenyReason(""); }}
            className="px-3 py-1.5 text-gray-300 hover:bg-gray-700 rounded-lg"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={submitDeny}
            disabled={deny.isPending}
            className="px-3 py-1.5 bg-red-600 text-white rounded-lg hover:bg-red-500 disabled:opacity-50"
          >
            {deny.isPending ? "Denying..." : "Deny request"}
          </button>
        </div>
      </Modal>
    </div>
  );
}

function UsersTab() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [disableUserModal, setDisableUserModal] = useState<number | null>(null);

  const { data: users, isLoading } = useQuery({
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

  if (isLoading) return <div className="text-gray-500">Loading...</div>;

  return (
    <div className="space-y-3">
      {users?.map((user: any) => (
        <div
          key={user.id}
          className="bg-gray-800 border border-gray-700 rounded-xl p-4 flex items-center justify-between gap-4"
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
      ))}

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
    <div className="space-y-3">
      {requests.map((req: any) => (
        <div
          key={req.id}
          className="bg-gray-800 border border-gray-700 rounded-xl p-4"
        >
          <div className="flex items-start justify-between gap-4">
            <div className="flex-1 min-w-0">
              <h3 className="font-semibold text-gray-100 truncate">
                {req.title}
              </h3>
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

  return (
    <div className="space-y-4">
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
          connected={health.real_debrid?.connected}
          items={[
            { label: "User", value: health.real_debrid?.username || "N/A" },
            { label: "Premium", value: health.real_debrid?.premium ? "Yes" : "No" },
            { label: "Points", value: String(health.real_debrid?.points ?? "N/A") },
          ]}
        />
        <HealthCard
          title="Audiobookshelf"
          connected={health.audiobookshelf?.connected}
          items={[
            { label: "URL", value: health.audiobookshelf?.url || "N/A" },
          ]}
        />
        <HealthCard
          title="Kavita"
          connected={health.kavita?.connected}
          items={[
            { label: "URL", value: health.kavita?.url || "N/A" },
          ]}
        />
        <KavitaEbookDebug />
        <HealthCard
          title="Disk Space"
          connected={true}
          items={[
            { label: "Total", value: `${health.disk?.total_gb} GB` },
            { label: "Used", value: `${health.disk?.used_gb} GB` },
            { label: "Free", value: `${health.disk?.free_gb} GB` },
          ]}
        />
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
  items,
}: {
  title: string;
  connected: boolean;
  items: { label: string; value: string }[];
}) {
  return (
    <div className="bg-gray-800 border border-gray-700 rounded-xl p-4">
      <div className="flex items-center gap-2 mb-3">
        <div
          className={`w-2 h-2 rounded-full ${
            connected ? "bg-green-500" : "bg-red-500"
          }`}
        />
        <h3 className="font-semibold text-gray-100">{title}</h3>
      </div>
      <div className="space-y-1.5">
        {items.map(({ label, value }) => (
          <div key={label} className="flex justify-between text-sm">
            <span className="text-gray-400">{label}</span>
            <span className="text-gray-200 font-medium">{value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
