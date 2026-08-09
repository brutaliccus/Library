import { useState, useEffect } from "react";
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
  Radar,
  Settings2,
  EyeOff,
  ExternalLink,
  BookOpen,
  Play,
  Ban,
  CheckCircle,
  Circle,
  FolderTree,
  Sparkles,
  Search,
  X,
  Headphones,
  KeyRound,
  Workflow,
  Menu,
  Database,
  Wand2,
  HardDrive,
  Share2,
  Clock3,
  type LucideIcon,
} from "lucide-react";
import CoverImage from "../components/CoverImage";
import LibraryAdminSettings from "../components/admin/LibraryAdminSettings";
import ScraperTab from "../components/admin/ScraperTab";
import ConfigTab from "../components/admin/ConfigTab";
import StagingFilesViewer from "../components/admin/StagingFilesViewer";
import QuickReviewWizard from "../components/admin/QuickReviewWizard";
import EbookMetadataMatcher from "../components/admin/EbookMetadataMatcher";
import LibrarySweepTab from "../components/admin/LibrarySweepTab";
import HealthTab from "../components/admin/HealthTab";
import AudibleAuthPanel from "../components/admin/AudibleAuthPanel";
import { usePushNotifications } from "../hooks/usePushNotifications";
import { Link, useSearchParams } from "react-router-dom";
import RequestStatusBadge from "../components/RequestStatus";
import RequestProgress from "../components/RequestProgress";
import Modal from "../components/Modal";
import { useToast } from "../contexts/ToastContext";
import { useAuth } from "../hooks/useAuth";
import {
  hasLiveRequests,
  requestListRefetchInterval,
} from "../utils/requestProgress";
import { libraryQueryKey } from "../utils/libraryQueryKeys";

/** Canonical Admin sections (URL ?tab=). Legacy aliases remap in resolveTab. */
type AdminTab =
  | "overview"
  | "requests"
  | "users"
  | "backups"
  | "activity"
  | "shared"
  | "discovery"
  | "catalog"
  | "library-sweep"
  | "pipelines"
  | "integrations"
  | "settings";

type NavItem = {
  id: AdminTab;
  label: string;
  icon: LucideIcon;
};

type NavGroup = { label: string; items: NavItem[] };

const ADMIN_NAV: NavGroup[] = [
  {
    label: "Operations",
    items: [
      { id: "overview", label: "Health", icon: Activity },
      { id: "requests", label: "Requests", icon: Download },
      { id: "users", label: "Users", icon: Users },
      { id: "backups", label: "Backups", icon: HardDrive },
      { id: "activity", label: "Activity", icon: Clock3 },
      { id: "shared", label: "Shared", icon: Share2 },
    ],
  },
  {
    label: "Library",
    items: [
      { id: "discovery", label: "Discovery", icon: Radar },
      { id: "catalog", label: "Catalog", icon: Database },
      { id: "library-sweep", label: "Library Sweep", icon: Wand2 },
    ],
  },
  {
    label: "System",
    items: [
      { id: "pipelines", label: "Pipelines", icon: Workflow },
      { id: "integrations", label: "Integrations", icon: KeyRound },
      { id: "settings", label: "Settings", icon: Settings2 },
    ],
  },
];

const VALID_TABS = new Set<AdminTab>(
  ADMIN_NAV.flatMap((g) => g.items.map((i) => i.id))
);

type AdminUser = {
  id: number;
  username: string;
  email: string | null;
  role: string;
  is_active: boolean;
  allow_audiobook_upload?: boolean;
  can_share_books?: boolean;
  created_at: string;
  last_seen_at: string | null;
  is_online: boolean;
  requests_total: number;
  stream_sessions: number;
  finished_streams: number;
  last_audiobook_title: string | null;
  last_audiobook_at: string | null;
  last_ebook_title: string | null;
  last_ebook_at: string | null;
};

function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const diffSec = Math.round((Date.now() - t) / 1000);
  if (diffSec < 45) return "just now";
  if (diffSec < 3600) {
    const m = Math.max(1, Math.round(diffSec / 60));
    return `${m}m ago`;
  }
  if (diffSec < 86400) {
    const h = Math.max(1, Math.round(diffSec / 3600));
    return `${h}h ago`;
  }
  const d = Math.max(1, Math.round(diffSec / 86400));
  if (d < 30) return `${d}d ago`;
  return new Date(iso).toLocaleDateString();
}

function resolveTab(raw: string | null): AdminTab {
  if (raw === "approvals") return "users";
  if (raw === "health") return "overview";
  if (raw === "scraper" || raw === "cache") return "discovery";
  if (raw === "config") return "settings";
  if (raw && VALID_TABS.has(raw as AdminTab)) return raw as AdminTab;
  return "overview";
}

function tabLabel(tab: AdminTab): string {
  for (const g of ADMIN_NAV) {
    const hit = g.items.find((i) => i.id === tab);
    if (hit) return hit.label;
  }
  return "Admin";
}

export default function AdminPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState<AdminTab>(() =>
    resolveTab(searchParams.get("tab"))
  );
  const [visitedTabs, setVisitedTabs] = useState<Set<AdminTab>>(
    () => new Set([resolveTab(searchParams.get("tab"))])
  );
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const settingsSection = searchParams.get("section") || undefined;
  const { state: pushState, error: pushError, subscribe: enablePush, unsubscribe: disablePush } =
    usePushNotifications();

  useEffect(() => {
    const tab = resolveTab(searchParams.get("tab"));
    setActiveTab(tab);
    setVisitedTabs((prev) => {
      if (prev.has(tab)) return prev;
      const next = new Set(prev);
      next.add(tab);
      return next;
    });
  }, [searchParams]);

  useEffect(() => {
    if (!mobileNavOpen) return;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMobileNavOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = "";
      window.removeEventListener("keydown", onKey);
    };
  }, [mobileNavOpen]);

  const selectTab = (id: AdminTab) => {
    setActiveTab(id);
    setMobileNavOpen(false);
    const next = new URLSearchParams(searchParams);
    next.set("tab", id);
    if (id !== "settings") next.delete("section");
    if (id !== "library-sweep") {
      next.delete("sweep");
      next.delete("queue");
    }
    setSearchParams(next, { replace: true });
  };

  const navList = (
    <nav className="space-y-4" aria-label="Admin sections">
      {ADMIN_NAV.map((group) => (
        <div key={group.label}>
          <p className="px-3 pb-1.5 text-[11px] font-semibold uppercase tracking-wider text-gray-500">
            {group.label}
          </p>
          <div className="space-y-0.5">
            {group.items.map(({ id, label, icon: Icon }) => {
              const active = activeTab === id;
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => selectTab(id)}
                  className={`w-full text-left px-3 py-2 text-sm rounded-lg transition-colors flex items-center gap-2.5 ${
                    active
                      ? "bg-brand-600/20 text-brand-300 font-medium"
                      : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
                  }`}
                >
                  <Icon size={16} className="shrink-0 opacity-80" />
                  <span className="truncate">{label}</span>
                </button>
              );
            })}
          </div>
        </div>
      ))}
    </nav>
  );

  return (
    <div className="w-full max-w-6xl mx-auto min-w-0 overflow-x-hidden pt-8 pb-[calc(2rem+env(safe-area-inset-bottom,0px))] pl-[max(1rem,env(safe-area-inset-left,0px))] pr-[max(1rem,env(safe-area-inset-right,0px))]">
      <div className="flex items-start justify-between gap-3 mb-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold text-gray-100 flex items-center gap-2">
            <Shield size={24} className="shrink-0" />
            Admin
          </h1>
          <p className="text-xs text-gray-500 mt-1 lg:hidden">{tabLabel(activeTab)}</p>
        </div>
        <button
          type="button"
          onClick={() => setMobileNavOpen(true)}
          className="lg:hidden shrink-0 p-2 -mr-1 text-gray-400 hover:text-gray-100 transition-colors"
          aria-label="Open admin menu"
        >
          <Menu size={22} strokeWidth={2} />
        </button>
      </div>

      {pushState !== "unsupported" && pushState !== "unavailable" && pushState !== "subscribed" && (
        <div className="mb-4 p-4 bg-gray-800/60 border border-gray-700 rounded-xl flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
          <div className="flex items-center gap-3">
            <Bell size={20} className="text-amber-400 shrink-0" />
            <div>
              <p className="text-sm font-medium text-gray-100">Admin push notifications</p>
              <p className="text-xs text-gray-500">
                Get notified when members join via invite, plus download status and errors
              </p>
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
        <div className="mb-4 p-3 bg-emerald-900/20 border border-emerald-800/50 rounded-xl flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-sm text-emerald-400">
              <Bell size={16} />
              Push notifications enabled for admin alerts
            </div>
            <p className="text-xs text-gray-500 mt-1">
              Browser only (not the Android APK). If quarantine alerts stop, Disable then Enable to
              refresh the subscription.
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => void enablePush()}
              className="px-3 py-1.5 text-xs text-emerald-300 hover:text-emerald-100 border border-emerald-800/60 rounded-lg hover:border-emerald-600 transition-colors"
            >
              Refresh
            </button>
            <button
              onClick={disablePush}
              className="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200 border border-gray-600 rounded-lg hover:border-gray-500 transition-colors"
            >
              Disable
            </button>
          </div>
        </div>
      )}

      <div className="mb-4">
        <Link to="/admin/setup" className="text-xs text-brand-400 hover:text-brand-300">
          Open instance setup wizard →
        </Link>
      </div>

      {mobileNavOpen && (
        <div
          className="fixed inset-0 z-50 lg:hidden"
          role="dialog"
          aria-modal="true"
          aria-label="Admin menu"
        >
          <div className="absolute inset-0 bg-black/60" onClick={() => setMobileNavOpen(false)} />
          <div className="absolute left-0 top-0 bottom-0 w-72 max-w-[85vw] bg-gray-900 border-r border-gray-800 overflow-y-auto p-4 pt-[max(1rem,env(safe-area-inset-top,0px))] pb-[max(1rem,env(safe-area-inset-bottom,0px))] pl-[max(1rem,env(safe-area-inset-left,0px))] drawer-slide-in">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-semibold text-gray-200">Admin</h3>
              <button
                type="button"
                onClick={() => setMobileNavOpen(false)}
                className="p-1 text-gray-500 hover:text-gray-300 transition-colors"
                aria-label="Close menu"
              >
                <X size={18} />
              </button>
            </div>
            {navList}
          </div>
        </div>
      )}

      <div className="flex gap-6 items-start min-w-0">
        <aside className="hidden lg:block w-52 shrink-0 sticky top-[4.5rem] max-h-[calc(100vh-5rem)] overflow-y-auto pr-2 scrollbar-hide">
          {navList}
        </aside>

        <div className="flex-1 min-w-0">
          {/* Keep visited tabs mounted so switching back is instant (React Query cache + no remount). */}
          {visitedTabs.has("overview") && (
            <div className={activeTab === "overview" ? "" : "hidden"}><HealthTab /></div>
          )}
          {visitedTabs.has("requests") && (
            <div className={activeTab === "requests" ? "" : "hidden"}><AllRequestsTab /></div>
          )}
          {visitedTabs.has("users") && (
            <div className={activeTab === "users" ? "" : "hidden"}><UsersTab /></div>
          )}
          {visitedTabs.has("backups") && (
            <div className={activeTab === "backups" ? "" : "hidden"}><BackupsTab /></div>
          )}
          {visitedTabs.has("activity") && (
            <div className={activeTab === "activity" ? "" : "hidden"}><ActivityTab /></div>
          )}
          {visitedTabs.has("shared") && (
            <div className={activeTab === "shared" ? "" : "hidden"}><SharedTab /></div>
          )}
          {visitedTabs.has("discovery") && (
            <div className={activeTab === "discovery" ? "" : "hidden"}><ScraperTab /></div>
          )}
          {visitedTabs.has("catalog") && (
            <div className={activeTab === "catalog" ? "" : "hidden"}>
              <ConfigTab
                lockedGroup="catalog"
                title="Catalog"
                description="Catalog API keys (Hardcover, NYT, ISBNdb, OpenRouter, Google Books) plus the local Open Library catalog build, update, and schedule controls."
              />
            </div>
          )}
          {visitedTabs.has("library-sweep") && (
            <div className={activeTab === "library-sweep" ? "" : "hidden"}><LibrarySweepTab /></div>
          )}
          {visitedTabs.has("pipelines") && (
            <div className={activeTab === "pipelines" ? "" : "hidden"}>
              <ConfigTab
                lockedGroup="pipeline"
                title="Pipelines"
                description="LibraForge and ebook pipeline toggles, scores, and M4B-related settings. Monitor LibraForge status under Health; review quarantines under Requests."
              />
            </div>
          )}
          {visitedTabs.has("integrations") && (
            <div className={activeTab === "integrations" ? "" : "hidden"}><IntegrationsPanel /></div>
          )}
          {visitedTabs.has("settings") && (
            <div className={activeTab === "settings" ? "space-y-2 min-w-0" : "hidden"}>
              <LibraryAdminSettings />
              <ConfigTab
                omitGroups={["pipeline", "catalog"]}
                initialGroup={settingsSection}
                title="Instance settings"
                description="Core instance settings, libraries paths, indexers, debrid defaults, VPN, notifications, Android, discovery flags, and storage. Pipeline and Catalog APIs are under their own Admin sections."
              />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function UsersTab() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const { user: me } = useAuth();
  const [userSearch, setUserSearch] = useState("");
  const [disableUserModal, setDisableUserModal] = useState<{ id: number; username: string } | null>(null);
  const [deleteUserModal, setDeleteUserModal] = useState<{ id: number; username: string } | null>(null);

  const { data: users, isLoading: usersLoading } = useQuery({
    queryKey: ["admin-users"],
    queryFn: async () => {
      const { data } = await api.get("/admin/users");
      return data as AdminUser[];
    },
    staleTime: 60_000,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });

  const searchQ = userSearch.trim().toLowerCase();
  const filteredUsers = !users
    ? []
    : !searchQ
      ? users
      : users.filter((u) => {
          const hay = [u.username, u.email].filter(Boolean).join(" ").toLowerCase();
          return hay.includes(searchQ);
        });

  const setActive = useMutation({
    mutationFn: async (body: {
      id: number;
      is_active?: boolean;
      role?: string;
      allow_audiobook_upload?: boolean;
      can_share_books?: boolean;
    }) => {
      const { id, ...patch } = body;
      const { data } = await api.patch(`/admin/users/${id}`, patch);
      return data as {
        message: string;
        is_active: boolean;
        role?: string;
        allow_audiobook_upload?: boolean;
        can_share_books?: boolean;
      };
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["admin-users"] });
      setDisableUserModal(null);
      toast(data.message, "success");
      if (data.role === "admin" && (data as any).libraforge_whitelist_added) {
        toast("Their IP was added to the LibraForge whitelist file.", "info");
      }
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Failed to update user", "error");
    },
  });

  const deleteUser = useMutation({
    mutationFn: async (id: number) => {
      const { data } = await api.delete(`/admin/users/${id}`);
      return data as { message: string };
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["admin-users"] });
      setDeleteUserModal(null);
      toast(data.message || "User deleted.", "info");
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Failed to delete user", "error");
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
        New members join with an invite link from Settings — no approval step. You can reset
        passwords, promote/demote admins, toggle per-user audiobook upload and book sharing,
        disable accounts, or permanently delete them here.
      </p>

      <div className="relative">
        <Search size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
        <input
          type="search"
          value={userSearch}
          onChange={(e) => setUserSearch(e.target.value)}
          placeholder="Search users by name or email..."
          className="w-full pl-10 pr-10 py-2.5 bg-gray-800 border border-gray-700 rounded-xl text-sm text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent placeholder:text-gray-500"
        />
        {userSearch && (
          <button
            type="button"
            onClick={() => setUserSearch("")}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
            aria-label="Clear search"
          >
            <X size={16} />
          </button>
        )}
      </div>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Users ({filteredUsers.length}
          {searchQ ? ` of ${users?.length ?? 0}` : ""})
        </h2>
        {!users?.length ? (
          <p className="text-center py-8 text-gray-500">No users yet</p>
        ) : !filteredUsers.length ? (
          <p className="text-center py-8 text-gray-500">No users match “{userSearch.trim()}”</p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {filteredUsers.map((user) => {
              const isSelf = me?.username === user.username;
              const presenceLabel = user.is_online
                ? "Online"
                : user.last_seen_at
                  ? `Last seen ${formatRelativeTime(user.last_seen_at)}`
                  : "Offline";
              return (
                <div
                  key={user.id}
                  className="bg-gray-800 border border-gray-700 rounded-xl p-4 flex flex-col gap-3 min-w-0"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="font-semibold text-gray-100 truncate">
                        {user.username}
                        {isSelf && (
                          <span className="ml-2 text-xs bg-gray-700 text-gray-400 px-2 py-0.5 rounded-full align-middle">
                            you
                          </span>
                        )}
                      </p>
                      {user.email && (
                        <p className="mt-0.5 text-xs text-gray-500 truncate" title={user.email}>
                          {user.email}
                        </p>
                      )}
                      <p className="mt-1 flex items-center gap-1.5 text-xs text-gray-400">
                        <Circle
                          size={8}
                          className={
                            user.is_online
                              ? "fill-emerald-400 text-emerald-400"
                              : "fill-gray-600 text-gray-600"
                          }
                        />
                        <span className={user.is_online ? "text-emerald-400" : ""}>
                          {presenceLabel}
                        </span>
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-1.5 justify-end shrink-0">
                      <span
                        className={`text-xs px-2 py-0.5 rounded-full ${
                          user.role === "admin"
                            ? "bg-brand-900/30 text-brand-400"
                            : "bg-gray-700/80 text-gray-300"
                        }`}
                      >
                        {user.role}
                      </span>
                      <span
                        className={`text-xs px-2 py-0.5 rounded-full ${
                          user.is_active
                            ? "bg-emerald-900/30 text-emerald-400"
                            : "bg-red-900/30 text-red-400"
                        }`}
                      >
                        {user.is_active ? "active" : "disabled"}
                      </span>
                    </div>
                  </div>

                  <dl className="grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
                    <div>
                      <dt className="text-gray-500">Joined</dt>
                      <dd className="text-gray-200">
                        {user.created_at
                          ? new Date(user.created_at).toLocaleDateString()
                          : "—"}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-gray-500">Requests</dt>
                      <dd className="text-gray-200">{user.requests_total}</dd>
                    </div>
                    <div>
                      <dt className="text-gray-500">Finished streams</dt>
                      <dd className="text-gray-200">{user.finished_streams}</dd>
                    </div>
                    <div>
                      <dt className="text-gray-500">Sessions</dt>
                      <dd className="text-gray-200">{user.stream_sessions}</dd>
                    </div>
                    <div className="col-span-2 space-y-1.5">
                      <dt className="text-gray-500">Last Book</dt>
                      <dd className="space-y-1 text-gray-200">
                        <p className="flex items-start gap-1.5 min-w-0">
                          <Headphones size={12} className="mt-0.5 shrink-0 text-gray-500" />
                          <span className="min-w-0">
                            <span className="truncate block" title={user.last_audiobook_title || undefined}>
                              {user.last_audiobook_title || "—"}
                            </span>
                            {user.last_audiobook_at && (
                              <span className="text-gray-500">
                                {formatRelativeTime(user.last_audiobook_at)}
                              </span>
                            )}
                          </span>
                        </p>
                        <p className="flex items-start gap-1.5 min-w-0">
                          <BookOpen size={12} className="mt-0.5 shrink-0 text-gray-500" />
                          <span className="min-w-0">
                            <span className="truncate block" title={user.last_ebook_title || undefined}>
                              {user.last_ebook_title || "—"}
                            </span>
                            {user.last_ebook_at && (
                              <span className="text-gray-500">
                                {formatRelativeTime(user.last_ebook_at)}
                              </span>
                            )}
                          </span>
                        </p>
                      </dd>
                    </div>
                  </dl>

                  <div className="flex flex-wrap gap-2 pt-1 border-t border-gray-700/80">
                    <label className="inline-flex items-center gap-1.5 px-2 py-1.5 text-xs text-gray-300">
                      <input
                        type="checkbox"
                        className="rounded border-gray-600 bg-gray-900"
                        checked={!!user.allow_audiobook_upload || user.role === "admin"}
                        disabled={user.role === "admin" || setActive.isPending}
                        onChange={(e) =>
                          setActive.mutate({
                            id: user.id,
                            allow_audiobook_upload: e.target.checked,
                          })
                        }
                      />
                      Can upload books
                    </label>
                    <label className="inline-flex items-center gap-1.5 px-2 py-1.5 text-xs text-gray-300">
                      <input
                        type="checkbox"
                        className="rounded border-gray-600 bg-gray-900"
                        checked={!!user.can_share_books || user.role === "admin"}
                        disabled={user.role === "admin" || setActive.isPending}
                        onChange={(e) =>
                          setActive.mutate({
                            id: user.id,
                            can_share_books: e.target.checked,
                          })
                        }
                      />
                      Can share books
                    </label>
                    {!isSelf && user.role !== "admin" && (
                      <button
                        type="button"
                        onClick={() => setActive.mutate({ id: user.id, role: "admin" })}
                        disabled={setActive.isPending}
                        className="flex items-center gap-1 px-3 py-1.5 bg-brand-900/30 text-brand-300 text-sm rounded-lg hover:bg-brand-900/50 disabled:opacity-50"
                      >
                        <Shield size={14} /> Promote to admin
                      </button>
                    )}
                    {!isSelf && user.role === "admin" && (
                      <button
                        type="button"
                        onClick={() => setActive.mutate({ id: user.id, role: "user" })}
                        disabled={setActive.isPending}
                        className="flex items-center gap-1 px-3 py-1.5 bg-gray-700 text-gray-300 text-sm rounded-lg hover:bg-gray-600 disabled:opacity-50"
                      >
                        Demote
                      </button>
                    )}
                    <button
                      onClick={() => resetPw.mutate(user.id)}
                      className="flex items-center gap-1 px-3 py-1.5 bg-gray-700 text-gray-300 text-sm rounded-lg hover:bg-gray-600"
                    >
                      <RefreshCw size={14} /> Reset PW
                    </button>
                    {!isSelf && (
                      user.is_active ? (
                        <button
                          onClick={() => setDisableUserModal({ id: user.id, username: user.username })}
                          className="flex items-center gap-1 px-3 py-1.5 bg-amber-900/30 text-amber-400 text-sm rounded-lg hover:bg-amber-900/50"
                        >
                          <Ban size={14} /> Disable
                        </button>
                      ) : (
                        <button
                          onClick={() => setActive.mutate({ id: user.id, is_active: true })}
                          disabled={setActive.isPending}
                          className="flex items-center gap-1 px-3 py-1.5 bg-emerald-900/30 text-emerald-400 text-sm rounded-lg hover:bg-emerald-900/50 disabled:opacity-50"
                        >
                          <CheckCircle size={14} /> Enable
                        </button>
                      )
                    )}
                    {!isSelf && (
                      <button
                        onClick={() => setDeleteUserModal({ id: user.id, username: user.username })}
                        className="flex items-center gap-1 px-3 py-1.5 bg-red-900/30 text-red-400 text-sm rounded-lg hover:bg-red-900/50"
                      >
                        <Trash2 size={14} /> Delete
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <Modal
        title="Disable user"
        show={disableUserModal !== null}
        onClose={() => setDisableUserModal(null)}
      >
        <p className="text-sm text-gray-400 mb-4">
          Disable <span className="text-gray-200">{disableUserModal?.username}</span>? They will no
          longer be able to log in. You can re-enable them later.
        </p>
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
            onClick={() =>
              disableUserModal !== null &&
              setActive.mutate({ id: disableUserModal.id, is_active: false })
            }
            disabled={setActive.isPending}
            className="px-3 py-1.5 bg-amber-600 text-white rounded-lg hover:bg-amber-500 disabled:opacity-50"
          >
            {setActive.isPending ? "Disabling..." : "Disable"}
          </button>
        </div>
      </Modal>

      <Modal
        title="Delete account"
        show={deleteUserModal !== null}
        onClose={() => setDeleteUserModal(null)}
      >
        <p className="text-sm text-gray-400 mb-4">
          Permanently delete <span className="text-gray-200">{deleteUserModal?.username}</span>?
          Their requests, play history, and related data will be removed. This cannot be undone.
        </p>
        <div className="flex gap-2 justify-end">
          <button
            type="button"
            onClick={() => setDeleteUserModal(null)}
            className="px-3 py-1.5 text-gray-300 hover:bg-gray-700 rounded-lg"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => deleteUserModal !== null && deleteUser.mutate(deleteUserModal.id)}
            disabled={deleteUser.isPending}
            className="px-3 py-1.5 bg-red-600 text-white rounded-lg hover:bg-red-500 disabled:opacity-50"
          >
            {deleteUser.isPending ? "Deleting..." : "Delete"}
          </button>
        </div>
      </Modal>
    </div>
  );
}

function catalogBookPath(volumeId: string | null | undefined, title: string): string {
  if (volumeId && !String(volumeId).startsWith("rd:")) {
    return `/book/${encodeURIComponent(volumeId)}`;
  }
  return `/search?q=${encodeURIComponent(title || "")}`;
}

function AllRequestsTab() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [stagingViewer, setStagingViewer] = useState<{
    id: number;
    title: string;
  } | null>(null);
  const [quickReview, setQuickReview] = useState<{
    id: number;
    title: string;
    manual_review_url?: string | null;
  } | null>(null);
  const [ebookReview, setEbookReview] = useState<{
    id: number;
    title: string;
  } | null>(null);
  const { data: requests, isLoading } = useQuery({
    queryKey: ["admin-downloads"],
    queryFn: async () => {
      const { data } = await api.get("/admin/download-requests");
      return data as any[];
    },
    // Include quarantined: admin continue → forge steps must stay live.
    staleTime: 15_000,
    refetchInterval: (query) =>
      requestListRefetchInterval(query.state.data as Array<{ status: string }> | undefined),
    refetchOnWindowFocus: true,
    refetchOnReconnect: true,
  });

  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      if (!hasLiveRequests(requests as Array<{ status: string }> | undefined)) return;
      void queryClient.invalidateQueries({ queryKey: ["admin-downloads"] });
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
    };
  }, [queryClient, requests]);

  const rejectMutation = useMutation({
    mutationFn: (id: number) =>
      api.post(`/admin/download-requests/${id}/reject`, {
        reason: "Rejected by admin",
        delete_files: true,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["admin-downloads"] });
      toast("Request rejected", "info");
    },
    onError: (err: any) => toast(err.response?.data?.detail || "Reject failed", "error"),
  });

  const continueMutation = useMutation({
    mutationFn: (id: number) => api.post(`/admin/download-requests/${id}/continue-forge`),
    onSuccess: (res: any) => {
      queryClient.invalidateQueries({ queryKey: ["admin-downloads"] });
      toast(res?.data?.message || "Continuing pipeline", "success");
    },
    onError: (err: any) => toast(err.response?.data?.detail || "Continue failed", "error"),
  });

  const rerunMutation = useMutation({
    mutationFn: (id: number) =>
      api.post(`/admin/download-requests/${id}/rerun-pipeline`, null, { timeout: 180_000 }),
    onSuccess: (res: any) => {
      queryClient.invalidateQueries({ queryKey: ["admin-downloads"] });
      toast(res?.data?.message || "Re-staged for Quick Review", "success");
      const id = res?.data?.id as number | undefined;
      const title = requests?.find((r: any) => r.id === id)?.title || "Request";
      if (id) {
        setQuickReview({
          id,
          title,
          manual_review_url: res?.data?.manual_review_url,
        });
      }
    },
    onError: (err: any) => toast(err.response?.data?.detail || "Re-run failed", "error"),
  });

  if (isLoading) return <div className="text-gray-500">Loading...</div>;

  if (!requests?.length) {
    return (
      <div className="text-center py-12 text-gray-500">No download requests yet</div>
    );
  }

  return (
    <div className="space-y-3 min-w-0">
      {requests.map((req: any) => {
        const quarantined = req.status === "quarantined";
        const hasStaging = Boolean(req.staging_path);
        const showStagingBrowser =
          hasStaging &&
          (quarantined ||
            [
              "metadata_forge",
              "m4b_convert",
              "chapter_forge",
              "folder_forge",
              "finalizing",
              "organizing",
            ].includes(req.status));
        return (
          <div
            key={req.id}
            className={`bg-gray-800 border rounded-xl p-3 sm:p-4 ${
              quarantined ? "border-amber-700/60" : "border-gray-700"
            }`}
          >
            <div className="flex gap-3 sm:gap-4">
              <Link
                to={catalogBookPath(req.google_volume_id, req.title)}
                className="w-16 sm:w-20 h-24 sm:h-28 rounded-lg overflow-hidden bg-gray-900 shrink-0 border border-gray-700 hover:border-gray-500 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-teal-500/60"
                title="Open store page"
                aria-label={`Open store page for ${req.title || "request"}`}
              >
                {req.cover_url ? (
                  <CoverImage src={req.cover_url} alt="" className="w-full h-full object-cover" />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-gray-600">
                    <BookOpen size={22} />
                  </div>
                )}
              </Link>
              <div className="flex-1 min-w-0 flex flex-col">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 min-w-0">
                      <h3 className="font-semibold text-gray-100 truncate text-base max-w-full">
                        {req.title}
                      </h3>
                      {req.username && (
                        <span className="text-xs text-gray-500 shrink-0">
                          Requested by {req.username}
                        </span>
                      )}
                      {req.is_private && (
                        <span
                          className="inline-flex items-center gap-1 shrink-0 text-[10px] font-medium uppercase tracking-wide px-1.5 py-0.5 rounded bg-purple-900/40 text-purple-300 border border-purple-700/40"
                          title="Requested in private mode"
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
                  <RequestStatusBadge status={req.status} detail={null} plainLanguage={false} />
                </div>
                <div className="flex flex-wrap items-center gap-3 mt-1.5 text-xs text-gray-500">
                  <span>{new Date(req.created_at).toLocaleString()}</span>
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
                  media_type={req.media_type}
                />
                {(quarantined ||
                  showStagingBrowser ||
                  (req.status === "completed" && req.media_type !== "ebook")) && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    {quarantined && req.media_type !== "ebook" && (
                      <button
                        type="button"
                        onClick={() =>
                          setQuickReview({
                            id: req.id,
                            title: req.title || "Request",
                            manual_review_url: req.manual_review_url,
                          })
                        }
                        className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-lg bg-teal-700/80 text-white hover:bg-teal-600"
                      >
                        <Sparkles size={12} />
                        Quick review
                      </button>
                    )}
                    {quarantined && req.media_type === "ebook" && (
                      <button
                        type="button"
                        onClick={() =>
                          setEbookReview({
                            id: req.id,
                            title: req.title || "Request",
                          })
                        }
                        className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-lg bg-teal-700/80 text-white hover:bg-teal-600"
                      >
                        <Sparkles size={12} />
                        Match metadata
                      </button>
                    )}
                    {showStagingBrowser && (
                      <button
                        type="button"
                        onClick={() =>
                          setStagingViewer({ id: req.id, title: req.title || "Request" })
                        }
                        className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-lg border border-gray-600 text-gray-300 hover:bg-gray-700/50"
                      >
                        <FolderTree size={12} />
                        Staging files
                      </button>
                    )}
                    {quarantined && req.manual_review_url && req.media_type !== "ebook" && (
                      <a
                        href={req.manual_review_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-lg border border-amber-700/50 text-amber-300 hover:bg-amber-900/30"
                      >
                        <ExternalLink size={12} />
                        LibraForge
                      </a>
                    )}
                    {quarantined && (
                      <button
                        type="button"
                        onClick={() => continueMutation.mutate(req.id)}
                        disabled={continueMutation.isPending}
                        className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-lg border border-teal-700/50 text-teal-300 hover:bg-teal-900/30 disabled:opacity-50"
                      >
                        <Play size={12} />
                        Continue pipeline
                      </button>
                    )}
                    {quarantined && (
                      <button
                        type="button"
                        onClick={() => rejectMutation.mutate(req.id)}
                        disabled={rejectMutation.isPending}
                        className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-lg border border-red-700/50 text-red-300 hover:bg-red-900/30 disabled:opacity-50"
                      >
                        <Ban size={12} />
                        Reject / delete
                      </button>
                    )}
                    {req.status === "completed" && req.media_type !== "ebook" && (
                      <button
                        type="button"
                        onClick={() => rerunMutation.mutate(req.id)}
                        disabled={rerunMutation.isPending}
                        className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-lg border border-sky-700/50 text-sky-300 hover:bg-sky-900/30 disabled:opacity-50"
                        title="Copy library folder back to staging and open Quick Review"
                      >
                        <RefreshCw size={12} />
                        Re-run pipeline
                      </button>
                    )}
                    {req.staging_path && (
                      <span
                        className="text-[10px] text-gray-500 truncate max-w-full"
                        title={req.staging_path}
                      >
                        {req.staging_path}
                      </span>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        );
      })}
      {stagingViewer && (
        <StagingFilesViewer
          requestId={stagingViewer.id}
          title={stagingViewer.title}
          open={!!stagingViewer}
          onClose={() => setStagingViewer(null)}
        />
      )}
      {quickReview && (
        <QuickReviewWizard
          requestId={quickReview.id}
          title={quickReview.title}
          open={!!quickReview}
          onClose={() => setQuickReview(null)}
          manualReviewUrl={quickReview.manual_review_url}
        />
      )}
      {ebookReview && (
        <EbookMetadataMatcher
          requestId={ebookReview.id}
          title={ebookReview.title}
          open={!!ebookReview}
          onClose={() => setEbookReview(null)}
        />
      )}
    </div>
  );
}

function BackupsTab() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useQuery({
    queryKey: ["admin-backups"],
    queryFn: async () => {
      const response = await api.get("/admin/backups");
      return response.data as any;
    },
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
  const targets = Array.isArray(data) ? data : data?.targets || data?.backups || [];
  const backupNow = useMutation({
    mutationFn: async (target: any) => {
      const endpoint = target.backup_url || target.action_url || `/admin/backups/${encodeURIComponent(target.id || target.name)}`;
      return api.post(endpoint);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["admin-backups"] });
      toast("Backup started. Status will refresh shortly.", "success");
    },
    onError: (err: any) => toast(err.response?.data?.detail || "Failed to start backup", "error"),
  });

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-gray-100 flex items-center gap-2"><HardDrive size={18} /> Backups</h2>
        <p className="text-xs text-gray-500 mt-1">Backup targets and their most recent completed snapshot.</p>
      </div>
      {isLoading ? <p className="text-gray-500">Loading backups…</p> : isError ? (
        <p className="rounded-xl border border-amber-800/60 bg-amber-900/20 p-4 text-sm text-amber-300">
          Backup status is unavailable. Configure the admin backups endpoint to enable this panel.
        </p>
      ) : !targets.length ? <p className="text-center py-10 text-gray-500">No backup targets configured</p> : (
        <div className="grid gap-3">
          {targets.map((target: any, index: number) => {
            const status = String(target.status || (target.ok ? "ready" : "unknown"));
            const healthy = ["ok", "ready", "complete", "completed", "success"].includes(status.toLowerCase());
            return (
              <div key={target.id || target.name || index} className="bg-gray-800 border border-gray-700 rounded-xl p-4 flex flex-col sm:flex-row gap-4 sm:items-center">
                <div className={`w-2.5 h-2.5 rounded-full shrink-0 ${healthy ? "bg-emerald-400" : "bg-amber-400"}`} />
                <div className="flex-1 min-w-0">
                  <p className="font-medium text-gray-100">{target.name || target.label || `Backup target ${index + 1}`}</p>
                  <p className="text-xs text-gray-500 mt-1">
                    {status} · Last backed up {formatRelativeTime(target.last_backed_up_at || target.last_backup_at || target.updated_at)}
                    {target.size_bytes != null || target.file_size != null ? ` · ${formatFileSize(target.size_bytes ?? target.file_size)}` : ""}
                  </p>
                  {target.error && <p className="text-xs text-red-400 mt-1">{target.error}</p>}
                </div>
                <button type="button" onClick={() => backupNow.mutate(target)} disabled={backupNow.isPending}
                  className="inline-flex items-center justify-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-brand-600 text-white hover:bg-brand-500 disabled:opacity-50">
                  <HardDrive size={14} /> {backupNow.isPending ? "Starting…" : "Backup now"}
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ActivityTab() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["admin-activity"],
    queryFn: async () => (await api.get("/admin/activity")).data as any,
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
  const events = Array.isArray(data) ? data : data?.events || data?.activity || [];
  const iconFor = (kind: string) => {
    const key = kind.toLowerCase();
    if (key.includes("share")) return Share2;
    if (key.includes("stream") || key.includes("listen") || key.includes("read")) return Headphones;
    if (key.includes("user") || key.includes("invite")) return Users;
    return key.includes("request") || key.includes("download") ? Download : Activity;
  };

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-gray-100 flex items-center gap-2"><Clock3 size={18} /> Activity</h2>
        <p className="text-xs text-gray-500 mt-1">Recent requests, membership events, streams, and shares.</p>
      </div>
      {isLoading ? <p className="text-gray-500">Loading activity…</p> : isError ? (
        <p className="rounded-xl border border-amber-800/60 bg-amber-900/20 p-4 text-sm text-amber-300">Activity is unavailable.</p>
      ) : !events.length ? <p className="text-center py-10 text-gray-500">No activity recorded yet</p> : (
        <ol className="space-y-2">
          {events.map((event: any, index: number) => {
            const Icon = iconFor(String(event.kind || event.type || ""));
            const timestamp = event.created_at || event.at || event.timestamp;
            return <li key={event.id || `${timestamp}-${index}`} className="bg-gray-800 border border-gray-700 rounded-xl p-3 flex gap-3">
              <div className="p-2 rounded-lg bg-gray-900 text-brand-400 h-fit"><Icon size={16} /></div>
              <div className="min-w-0 flex-1">
                <p className="text-sm text-gray-200">{event.message || event.summary || event.title || event.kind || "Activity"}</p>
                <p className="text-xs text-gray-500 mt-1">{event.user || event.username ? `${event.user || event.username} · ` : ""}{formatRelativeTime(timestamp)}</p>
              </div>
            </li>;
          })}
        </ol>
      )}
    </div>
  );
}

function SharedTab() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [confirmAll, setConfirmAll] = useState(false);
  const { data, isLoading, isError } = useQuery({
    queryKey: ["admin-shares"],
    queryFn: async () => (await api.get("/admin/shares")).data as any,
    staleTime: 60_000,
  });
  const shares = Array.isArray(data) ? data : data?.shares || [];
  const revoke = useMutation({
    mutationFn: async (share: any) => api.delete(`/admin/shares/${encodeURIComponent(share.id || share.token)}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["admin-shares"] });
      toast("Share link revoked", "success");
    },
    onError: (err: any) => toast(err.response?.data?.detail || "Failed to revoke share", "error"),
  });
  const revokeAll = useMutation({
    mutationFn: async () => api.post("/admin/shares/revoke-all"),
    onSuccess: () => {
      setConfirmAll(false);
      void queryClient.invalidateQueries({ queryKey: ["admin-shares"] });
      toast("All share links revoked", "success");
    },
    onError: (err: any) => toast(err.response?.data?.detail || "Failed to revoke all shares", "error"),
  });

  return <div className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 className="text-lg font-semibold text-gray-100 flex items-center gap-2"><Share2 size={18} /> Shared</h2>
        <p className="text-xs text-gray-500 mt-1">Public links can be revoked at any time.</p>
      </div>
      <button type="button" onClick={() => setConfirmAll(true)} disabled={!shares.length}
        className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg border border-red-800/60 text-red-300 hover:bg-red-900/30 disabled:opacity-50">
        <Trash2 size={14} /> Revoke all
      </button>
    </div>
    <p className="text-xs text-gray-500">Invite code auto-rotation is configured in Settings as “Invite code auto-rotation (minutes)” (60–43200).</p>
    {isLoading ? <p className="text-gray-500">Loading shared links…</p> : isError ? (
      <p className="rounded-xl border border-amber-800/60 bg-amber-900/20 p-4 text-sm text-amber-300">Share management is unavailable.</p>
    ) : !shares.length ? <p className="text-center py-10 text-gray-500">No active shares</p> : (
      <div className="space-y-2">
        {shares.map((share: any, index: number) => (
          <div key={share.id || share.token || index} className="bg-gray-800 border border-gray-700 rounded-xl p-4 flex flex-col sm:flex-row gap-3 sm:items-center">
            <div className="flex-1 min-w-0">
              <p className="font-medium text-gray-100 truncate">{share.title || "Shared item"} <span className="text-xs font-normal text-gray-500 capitalize">· {share.media_type || share.type || "audiobook"}</span></p>
              <p className="text-xs text-gray-500 mt-1">Created {formatRelativeTime(share.created_at)}{share.created_by || share.username ? ` by ${share.created_by || share.username}` : ""}</p>
              {(share.url || share.path) && <p className="text-xs text-brand-400 truncate mt-1">{share.url || share.path}</p>}
            </div>
            <button type="button" onClick={() => revoke.mutate(share)} disabled={revoke.isPending}
              className="inline-flex items-center justify-center gap-1.5 px-3 py-1.5 text-sm rounded-lg border border-red-800/60 text-red-300 hover:bg-red-900/30 disabled:opacity-50">
              <Trash2 size={14} /> Revoke
            </button>
          </div>
        ))}
      </div>
    )}
    <Modal title="Revoke all shares" show={confirmAll} onClose={() => !revokeAll.isPending && setConfirmAll(false)}>
      <p className="text-sm text-gray-400 mb-4">Revoke every active public share link? This cannot be undone.</p>
      <div className="flex justify-end gap-2">
        <button type="button" onClick={() => setConfirmAll(false)} disabled={revokeAll.isPending} className="px-3 py-1.5 text-gray-300 hover:bg-gray-700 rounded-lg">Cancel</button>
        <button type="button" onClick={() => revokeAll.mutate()} disabled={revokeAll.isPending} className="px-3 py-1.5 bg-red-600 text-white rounded-lg hover:bg-red-500 disabled:opacity-50">{revokeAll.isPending ? "Revoking…" : "Revoke all"}</button>
      </div>
    </Modal>
  </div>;
}

function formatFileSize(bytes: unknown): string {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const unit = value === 0 ? 0 : Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** unit).toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

type OpenRouterUsage = {
  label?: string;
  usage?: number | null;
  usageDaily?: number | null;
  usageWeekly?: number | null;
  usageMonthly?: number | null;
  limit?: number | null;
  limitRemaining?: number | null;
  limitReset?: string | null;
  isFreeTier?: boolean | null;
  error?: string | null;
  creditsExhausted?: boolean;
};

function formatCredits(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

function OpenRouterUsageBlock({
  usage,
  onRefresh,
}: {
  usage?: OpenRouterUsage | null;
  onRefresh: () => void;
}) {
  const { toast } = useToast();
  const refresh = useMutation({
    mutationFn: async () => {
      const { data } = await api.get("/admin/integrations/openrouter-usage");
      return (data as { usage: OpenRouterUsage }).usage;
    },
    onSuccess: () => {
      onRefresh();
      toast("OpenRouter usage refreshed", "success");
    },
    onError: () => toast("Failed to refresh usage", "error"),
  });

  const u = refresh.data || usage;

  return (
    <div className="rounded-lg border border-gray-700/80 bg-gray-900/40 p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-gray-400">
          OpenRouter credits (per-key · GET /api/v1/key)
        </span>
        <button
          type="button"
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          className="text-xs px-2 py-1 rounded border border-gray-600 text-gray-300 hover:bg-gray-800 disabled:opacity-50"
        >
          {refresh.isPending ? "Refreshing…" : "Refresh"}
        </button>
      </div>
      {u?.error ? (
        <p className="text-xs text-amber-400/90">{u.error}</p>
      ) : (
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
          {u?.creditsExhausted ||
          (u?.limitRemaining != null && u.limitRemaining <= 0) ? (
            <>
              <dt className="text-gray-500 col-span-2">LLM assist</dt>
              <dd className="text-amber-300/90 col-span-2 text-left">
                Credits exhausted — assist skipped (same as toggle off); pipeline continues.
              </dd>
            </>
          ) : null}
          <dt className="text-gray-500">Limit remaining</dt>
          <dd className="text-gray-200 text-right tabular-nums">
            {u?.limitRemaining == null && u?.limit == null
              ? "Unlimited / none set"
              : formatCredits(u?.limitRemaining)}
          </dd>
          <dt className="text-gray-500">Key limit</dt>
          <dd className="text-gray-200 text-right tabular-nums">
            {u?.limit == null ? "—" : formatCredits(u.limit)}
            {u?.limitReset ? ` · reset ${u.limitReset}` : ""}
          </dd>
          <dt className="text-gray-500">Usage (all time)</dt>
          <dd className="text-gray-200 text-right tabular-nums">{formatCredits(u?.usage)}</dd>
          <dt className="text-gray-500">Usage (month)</dt>
          <dd className="text-gray-200 text-right tabular-nums">
            {formatCredits(u?.usageMonthly)}
          </dd>
          <dt className="text-gray-500">Usage (week / day)</dt>
          <dd className="text-gray-200 text-right tabular-nums">
            {formatCredits(u?.usageWeekly)} / {formatCredits(u?.usageDaily)}
          </dd>
          {u?.label ? (
            <>
              <dt className="text-gray-500">Key label</dt>
              <dd className="text-gray-400 text-right font-mono truncate" title={u.label}>
                {u.label}
              </dd>
            </>
          ) : null}
        </dl>
      )}
    </div>
  );
}

interface IntegrationsResponse {
  nyt?: { configured: boolean; overridden: boolean; hint: string };
  isbndb?: { configured: boolean; overridden: boolean; hint: string };
  hardcover?: { configured: boolean; overridden: boolean; hint: string };
  openrouter?: {
    enabled: boolean;
    configured: boolean;
    overridden: boolean;
    hint: string;
    model: string;
    confidenceThreshold: number;
    note?: string;
    usage?: OpenRouterUsage | null;
  };
  mullvad?: {
    configured: boolean;
    overridden: boolean;
    hint: string;
    note?: string;
    wireguardReady?: boolean;
    wireguardHint?: string;
    vpnEnable?: { ok?: boolean; error?: string; log?: string };
  };
  audible?: {
    configured?: boolean;
    reachable?: boolean;
    auth_ok?: boolean;
    active_name?: string;
    status?: string;
    note?: string;
    libraforge_accounts_url?: string;
    error?: string;
  };
}

function IntegrationsPanel() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [nytKey, setNytKey] = useState("");
  const [isbndbKey, setIsbndbKey] = useState("");
  const [hardcoverKey, setHardcoverKey] = useState("");
  const [openrouterKey, setOpenrouterKey] = useState("");
  const [openrouterModel, setOpenrouterModel] = useState("");
  const [openrouterThreshold, setOpenrouterThreshold] = useState("");
  const [mullvadAcct, setMullvadAcct] = useState("");

  const { data } = useQuery<IntegrationsResponse>({
    queryKey: ["admin-integrations"],
    queryFn: async () => {
      const { data } = await api.get("/admin/integrations");
      return data as IntegrationsResponse;
    },
    staleTime: 60_000,
  });

  const saveNyt = useMutation({
    mutationFn: async (value: string) => {
      const { data } = await api.put("/admin/integrations", { nyt_api_key: value });
      return data as IntegrationsResponse;
    },
    onSuccess: () => {
      setNytKey("");
      queryClient.invalidateQueries({ queryKey: ["admin-integrations"] });
      queryClient.invalidateQueries({ queryKey: libraryQueryKey("trending-books") });
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

  const saveOpenrouter = useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      const { data } = await api.put("/admin/integrations", payload);
      return data as IntegrationsResponse;
    },
    onSuccess: () => {
      setOpenrouterKey("");
      setOpenrouterModel("");
      setOpenrouterThreshold("");
      queryClient.invalidateQueries({ queryKey: ["admin-integrations"] });
      toast("OpenRouter settings saved", "success");
    },
    onError: () => toast("Failed to save OpenRouter settings", "error"),
  });

  const saveMullvad = useMutation({
    mutationFn: async (value: string) => {
      const { data } = await api.put(
        "/admin/integrations",
        { mullvad_account_number: value },
        { timeout: 420_000 },
      );
      return data as IntegrationsResponse;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(["admin-integrations"], data);
      setMullvadAcct("");
      void queryClient.invalidateQueries({ queryKey: ["admin-docker-services"] });
      void queryClient.invalidateQueries({ queryKey: ["admin-health"] });
      const vpn = data?.mullvad?.vpnEnable;
      if (vpn && vpn.ok === false) {
        toast(
          vpn.error || "Account saved but gluetun enable failed — try Enable VPN below",
          "error",
        );
      } else if (!data?.mullvad?.configured) {
        toast("Mullvad account cleared", "success");
      } else {
        toast(
          data?.mullvad?.wireguardReady
            ? "Mullvad saved — WireGuard registered and gluetun enabled"
            : "Mullvad account saved",
          "success",
        );
      }
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Failed to save Mullvad account";
      toast(String(msg), "error");
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
      void queryClient.invalidateQueries({ queryKey: ["admin-integrations"] });
      void queryClient.invalidateQueries({ queryKey: ["admin-docker-services"] });
      void queryClient.invalidateQueries({ queryKey: ["admin-health"] });
      toast(
        data.ok ? "gluetun started (vpn profile enabled)" : data.error || "VPN enable failed",
        data.ok ? "success" : "error",
      );
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "VPN enable failed";
      toast(String(msg), "error");
    },
  });

  const nyt = data?.nyt;
  const isbndb = data?.isbndb;
  const hardcover = data?.hardcover;
  const openrouter = data?.openrouter;
  const mullvad = data?.mullvad;

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-gray-100 flex items-center gap-2">
          <KeyRound size={18} />
          Integrations
        </h2>
        <p className="text-xs text-gray-500 mt-1 max-w-xl">
          Audible metadata login, catalog/assist API keys, and OpenRouter usage. Real-Debrid /
          TorBox / indexer URLs are under Settings → Debrid and Indexers. Open Library dump
          scheduling is under Catalog.
        </p>
      </div>
    <div className="bg-gray-800 border border-gray-700 rounded-xl p-4 space-y-5">

      <div className="space-y-2 text-sm border-b border-gray-700 pb-4">
        <AudibleAuthPanel />
      </div>

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
        <div className="flex items-center justify-between gap-3">
          <span className="text-gray-300">OpenRouter LLM assist</span>
          <label className="flex items-center gap-2 text-sm text-gray-400 cursor-pointer">
            <input
              type="checkbox"
              checked={Boolean(openrouter?.enabled)}
              onChange={(e) =>
                saveOpenrouter.mutate({ openrouter_enabled: e.target.checked })
              }
              disabled={saveOpenrouter.isPending}
              className="rounded border-gray-600 bg-gray-900 text-emerald-500 focus:ring-emerald-700"
            />
            {openrouter?.enabled ? "On" : "Off"}
          </label>
        </div>
        <div className="flex items-center justify-between text-xs">
          <span className="text-gray-500">API key</span>
          <span className={openrouter?.configured ? "text-emerald-400" : "text-gray-500"}>
            {openrouter?.configured
              ? `Configured${openrouter.overridden ? "" : " (env)"}${openrouter.hint ? ` · ${openrouter.hint}` : ""}`
              : "Not set"}
          </span>
        </div>
        <div className="flex flex-col sm:flex-row gap-2">
          <input
            type="password"
            value={openrouterKey}
            onChange={(e) => setOpenrouterKey(e.target.value)}
            placeholder="sk-or-… OpenRouter API key"
            autoComplete="off"
            className="flex-1 min-w-0 px-3 py-1.5 bg-gray-900 border border-gray-700 rounded-lg text-gray-100 text-sm focus:outline-none focus:border-gray-500"
          />
          <button
            type="button"
            onClick={() =>
              saveOpenrouter.mutate({ openrouter_api_key: openrouterKey.trim() })
            }
            disabled={saveOpenrouter.isPending || !openrouterKey.trim()}
            className="px-3 py-1.5 bg-emerald-900/50 text-emerald-300 text-sm rounded-lg hover:bg-emerald-900/70 border border-emerald-800/50 disabled:opacity-50"
          >
            {saveOpenrouter.isPending ? "Saving…" : "Save key"}
          </button>
          {openrouter?.overridden && (
            <button
              type="button"
              onClick={() => saveOpenrouter.mutate({ openrouter_api_key: "" })}
              disabled={saveOpenrouter.isPending}
              className="px-3 py-1.5 bg-gray-900 text-gray-400 text-sm rounded-lg hover:text-gray-200 border border-gray-700 disabled:opacity-50"
            >
              Clear
            </button>
          )}
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          <div className="space-y-1">
            <label className="text-xs text-gray-500">Model</label>
            <input
              type="text"
              value={openrouterModel}
              onChange={(e) => setOpenrouterModel(e.target.value)}
              placeholder={openrouter?.model || "openai/gpt-4o-mini"}
              autoComplete="off"
              className="w-full px-3 py-1.5 bg-gray-900 border border-gray-700 rounded-lg text-gray-100 text-sm focus:outline-none focus:border-gray-500"
            />
          </div>
          <div className="space-y-1">
            <label className="text-xs text-gray-500">Confidence threshold (0–1)</label>
            <input
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={openrouterThreshold}
              onChange={(e) => setOpenrouterThreshold(e.target.value)}
              placeholder={
                openrouter?.confidenceThreshold != null
                  ? String(openrouter.confidenceThreshold)
                  : "0.85"
              }
              className="w-full px-3 py-1.5 bg-gray-900 border border-gray-700 rounded-lg text-gray-100 text-sm focus:outline-none focus:border-gray-500"
            />
          </div>
        </div>
        <button
          type="button"
          onClick={() => {
            const payload: Record<string, unknown> = {};
            if (openrouterModel.trim()) {
              payload.openrouter_model = openrouterModel.trim();
            }
            if (openrouterThreshold.trim()) {
              const n = Number(openrouterThreshold);
              if (!Number.isNaN(n)) payload.openrouter_confidence_threshold = n;
            }
            if (Object.keys(payload).length === 0) {
              toast("Enter a model and/or threshold to save", "error");
              return;
            }
            saveOpenrouter.mutate(payload);
          }}
          disabled={saveOpenrouter.isPending}
          className="px-3 py-1.5 bg-gray-900 text-gray-300 text-sm rounded-lg hover:text-gray-100 border border-gray-700 disabled:opacity-50"
        >
          Save model / threshold
        </button>
        <p className="text-xs text-gray-500">
          {openrouter?.note ||
            "LLM assist (off by default): Metadata Forge / ebook identify retry, multi-book split, file prune, ASIN recovery. No calls without a key."}
          {" "}
          Current model: {openrouter?.model || "openai/gpt-4o-mini"}; threshold{" "}
          {openrouter?.confidenceThreshold ?? 0.85}.
        </p>
        {openrouter?.configured && (
          <OpenRouterUsageBlock
            usage={openrouter.usage}
            onRefresh={() =>
              queryClient.invalidateQueries({ queryKey: ["admin-integrations"] })
            }
          />
        )}
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
          {mullvad?.wireguardReady && (
            <button
              type="button"
              onClick={() => enableVpn.mutate()}
              disabled={enableVpn.isPending || saveMullvad.isPending}
              className="px-3 py-1.5 bg-sky-900/40 text-sky-200 text-sm rounded-lg hover:bg-sky-900/60 border border-sky-800/50 disabled:opacity-50"
            >
              {enableVpn.isPending ? "Starting…" : "Enable / restart gluetun"}
            </button>
          )}
        </div>
        {mullvad?.wireguardReady && (
          <p className="text-xs text-emerald-500/90">
            WireGuard ready{mullvad.wireguardHint ? ` · ${mullvad.wireguardHint}` : ""}
          </p>
        )}
        <p className="text-xs text-gray-500">
          {mullvad?.note ||
            "Only AudioBook Bay Flare/RSS/search egress via Mullvad. The rest of the stack stays on your LAN. Saving an account registers keys and starts gluetun — no CLI."}
        </p>
      </div>
    </div>
    </div>
  );
}
