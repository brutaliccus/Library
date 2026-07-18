import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../hooks/useAuth";
import { useToast } from "../contexts/ToastContext";
import { useLibraryGroup } from "../hooks/useLibraryGroup";
import type { KeySource } from "../hooks/useLibraryGroup";
import api, { applyApiBaseUrl } from "../api/client";
import {
  audioCacheUsageBytes,
  audioCacheEntryCount,
  audioCacheLastError,
  clearAllAudioCache,
} from "../utils/audioCache";
import { ebookCacheUsageBytes, ebookCacheEntryCount, clearAllEbookCache } from "../utils/ebookCache";
import {
  Settings as SettingsIcon, EyeOff, Shield, Zap, HardDrive, Trash2,
  Library, Copy, RefreshCw, KeyRound, ChevronUp, ChevronDown, Globe,
} from "lucide-react";
import ServerUrlField, { commitServerUrl } from "../components/ServerUrlField";
import { getStoredInstanceUrl, isNativeApp } from "../api/instanceUrl";

interface UserSettings {
  private_mode: boolean;
  preferred_debrid: string;
  available_debrid_providers: string[];
}

const DEBRID_LABELS: Record<string, string> = {
  rd: "Real-Debrid",
  torbox: "Torbox",
};

function formatBytes(bytes: number): string {
  if (bytes <= 0) return "0 MB";
  const gb = bytes / (1024 * 1024 * 1024);
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  return `${(bytes / (1024 * 1024)).toFixed(0)} MB`;
}

function keySourceLabel(source: KeySource, name: string): string {
  if (source === "group") return `${name} key saved`;
  if (source === "server") return `${name} via server (.env)`;
  return `${name} not configured`;
}

function KeyStatusBadge({ source, label }: { source: KeySource; label: string }) {
  const styles =
    source === "group"
      ? "bg-emerald-900/40 text-emerald-300 border-emerald-700/40"
      : source === "server"
        ? "bg-sky-900/40 text-sky-300 border-sky-700/40"
        : "bg-gray-800 text-gray-400 border-gray-700";
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium border ${styles}`}>
      {label}
    </span>
  );
}

/** Prominent debrid key management — owners can add Torbox/RD any time after onboarding. */
function DebridKeysSection() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const { data, isLoading } = useLibraryGroup();
  const lib = data?.library;

  const [rdToken, setRdToken] = useState("");
  const [torboxToken, setTorboxToken] = useState("");

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["library-group"] });
    queryClient.invalidateQueries({ queryKey: ["user-settings"] });
  };

  const updateKeys = useMutation({
    mutationFn: async () =>
      (
        await api.put("/libraries/tokens", {
          real_debrid_api_token: rdToken.trim() || null,
          torbox_api_token: torboxToken.trim() || null,
        })
      ).data,
    onSuccess: () => {
      refresh();
      setRdToken("");
      setTorboxToken("");
      toast("API keys updated", "success");
    },
    onError: (e: any) => toast(e.response?.data?.detail || "Failed to update keys", "error"),
  });

  if (isLoading) return null;
  if (!lib) return null;

  if (!lib.canManageKeys) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
        <div className="flex items-start gap-4">
          <div className="p-2 bg-gray-800 rounded-lg shrink-0">
            <KeyRound size={20} className="text-amber-400" />
          </div>
          <div className="flex-1 min-w-0">
            <h3 className="text-sm font-semibold text-gray-100">Debrid API Keys</h3>
            <p className="text-xs text-gray-400 mt-1 leading-relaxed">
              You joined <span className="text-gray-200">{lib.name}</span> with an invite code, so
              streaming uses the library owner's Real-Debrid / Torbox accounts. Only the owner can
              change those keys.
            </p>
            <p className="text-xs text-gray-400 mt-2 leading-relaxed">
              Want to use your own keys instead?
            </p>
            <Link
              to="/onboarding?mode=create"
              className="inline-flex items-center gap-1.5 mt-2 px-3 py-2 bg-brand-600 text-white text-xs font-medium rounded-lg hover:bg-brand-500 transition-colors"
            >
              <KeyRound size={14} />
              Create your own library
            </Link>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <div className="flex items-start gap-4">
        <div className="p-2 bg-gray-800 rounded-lg shrink-0">
          <KeyRound size={20} className="text-amber-400" />
        </div>
        <div className="flex-1 min-w-0 space-y-3">
          <div>
            <h3 className="text-sm font-semibold text-gray-100">Debrid API Keys</h3>
            <p className="text-xs text-gray-400 mt-1 leading-relaxed">
              Keys for <span className="text-gray-200">{lib.name}</span>. Everyone in your library
              streams with these accounts. Leave a field blank to keep the current key.
            </p>
            <div className="flex flex-wrap gap-2 mt-2">
              <KeyStatusBadge
                source={lib.rdKeySource}
                label={keySourceLabel(lib.rdKeySource, "Real-Debrid")}
              />
              <KeyStatusBadge
                source={lib.torboxKeySource}
                label={keySourceLabel(lib.torboxKeySource, "Torbox")}
              />
            </div>
          </div>

          <div className="space-y-2">
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1">
                Real-Debrid API key{" "}
                <a
                  href="https://real-debrid.com/apitoken"
                  target="_blank"
                  rel="noreferrer"
                  className="text-brand-400 hover:underline"
                >
                  (get token)
                </a>
              </label>
              <input
                value={rdToken}
                onChange={(e) => setRdToken(e.target.value)}
                placeholder={
                  lib.rdKeySource === "none"
                    ? "Paste your Real-Debrid API key"
                    : "Leave blank to keep current key"
                }
                className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:border-brand-500 font-mono"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1">
                Torbox API key{" "}
                <a
                  href="https://torbox.app/settings"
                  target="_blank"
                  rel="noreferrer"
                  className="text-brand-400 hover:underline"
                >
                  (get token)
                </a>
              </label>
              <input
                value={torboxToken}
                onChange={(e) => setTorboxToken(e.target.value)}
                placeholder={
                  lib.torboxKeySource === "none"
                    ? "Paste your Torbox API key"
                    : "Leave blank to keep current key"
                }
                className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:border-brand-500 font-mono"
              />
            </div>
          </div>

          <button
            onClick={() => updateKeys.mutate()}
            disabled={updateKeys.isPending || (!rdToken.trim() && !torboxToken.trim())}
            className="flex items-center gap-1.5 px-4 py-2 bg-brand-600 text-white text-sm font-medium rounded-lg hover:bg-brand-500 disabled:opacity-50 transition-colors"
          >
            {updateKeys.isPending ? "Verifying keys..." : "Save API keys"}
          </button>
        </div>
      </div>
    </div>
  );
}

function LibraryGroupSection() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const { data } = useLibraryGroup();
  const lib = data?.library;

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["library-group"] });

  const regenInvite = useMutation({
    mutationFn: async () => (await api.post("/libraries/regenerate-invite")).data,
    onSuccess: () => {
      refresh();
      toast("New invite code generated — the old one no longer works", "success");
    },
    onError: (e: any) => toast(e.response?.data?.detail || "Failed to regenerate code", "error"),
  });

  const setRole = useMutation({
    mutationFn: async ({ id, role }: { id: number; role: string }) =>
      (await api.post(`/libraries/members/${id}/role`, { library_role: role })).data,
    onSuccess: () => refresh(),
    onError: (e: any) => toast(e.response?.data?.detail || "Failed to update member", "error"),
  });

  if (!lib) return null;
  const isOwner = lib.role === "owner";
  const canInvite = lib.role === "owner" || lib.role === "admin";

  const copyInvite = () => {
    if (!lib.inviteCode) return;
    navigator.clipboard?.writeText(lib.inviteCode).then(
      () => toast("Invite code copied", "success"),
      () => toast("Couldn't copy — long-press to copy manually", "error"),
    );
  };

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <div className="flex items-start gap-4">
        <div className="p-2 bg-gray-800 rounded-lg shrink-0">
          <Library size={20} className="text-brand-400" />
        </div>
        <div className="flex-1 min-w-0 space-y-4">
          <div>
            <h3 className="text-sm font-semibold text-gray-100">{lib.name}</h3>
            <p className="text-xs text-gray-400 mt-1">
              Your role: <span className="text-gray-200 capitalize">{lib.role}</span>
            </p>
          </div>

          {canInvite && lib.inviteCode && (
            <div>
              <p className="text-xs font-medium text-gray-400 mb-1.5">
                Invite code — share it to let others stream with this library's account
              </p>
              <div className="flex items-center gap-2">
                <code className="px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-brand-300 font-mono tracking-widest">
                  {lib.inviteCode}
                </code>
                <button
                  onClick={copyInvite}
                  className="p-2 bg-gray-800 text-gray-300 rounded-lg hover:bg-gray-700 transition-colors"
                  title="Copy invite code"
                >
                  <Copy size={15} />
                </button>
                {isOwner && (
                  <button
                    onClick={() => regenInvite.mutate()}
                    disabled={regenInvite.isPending}
                    className="p-2 bg-gray-800 text-gray-300 rounded-lg hover:bg-gray-700 disabled:opacity-50 transition-colors"
                    title="Generate a new code (old one stops working)"
                  >
                    <RefreshCw size={15} className={regenInvite.isPending ? "animate-spin" : ""} />
                  </button>
                )}
              </div>
            </div>
          )}

          {canInvite && (lib.members?.length ?? 0) > 0 && (
            <div>
              <p className="text-xs font-medium text-gray-400 mb-1.5">Members</p>
              <div className="space-y-1.5">
                {lib.members!.map((m) => (
                  <div
                    key={m.id}
                    className="flex items-center justify-between gap-3 px-3 py-2 bg-gray-800/60 rounded-lg"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="text-sm text-gray-200 truncate">{m.username}</span>
                      <span
                        className={`px-1.5 py-0.5 rounded text-[10px] font-medium uppercase ${
                          m.libraryRole === "owner"
                            ? "bg-amber-900/50 text-amber-300"
                            : m.libraryRole === "admin"
                              ? "bg-purple-900/50 text-purple-300"
                              : "bg-gray-700 text-gray-400"
                        }`}
                      >
                        {m.libraryRole}
                      </span>
                    </div>
                    {isOwner && !m.isOwner && (
                      <button
                        onClick={() =>
                          setRole.mutate({
                            id: m.id,
                            role: m.libraryRole === "admin" ? "member" : "admin",
                          })
                        }
                        disabled={setRole.isPending}
                        className="flex items-center gap-1 px-2 py-1 bg-gray-700 text-gray-300 text-xs rounded-md hover:bg-gray-600 disabled:opacity-50 transition-colors shrink-0"
                      >
                        {m.libraryRole === "admin" ? (
                          <><ChevronDown size={12} /> Demote</>
                        ) : (
                          <><ChevronUp size={12} /> Make admin</>
                        )}
                      </button>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ServerUrlSettings() {
  const { toast } = useToast();
  const { logout } = useAuth();
  const [serverUrl, setServerUrl] = useState(() => getStoredInstanceUrl() || "");

  if (!isNativeApp()) return null;

  const save = () => {
    const saved = commitServerUrl(serverUrl);
    if (!saved) {
      toast("Enter a valid Library URL (https://…)", "error");
      return;
    }
    applyApiBaseUrl();
    toast("Library server URL updated. Sign in again if requests fail.", "success");
    // Changing servers invalidates the old session — send user to login.
    logout();
  };

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <div className="flex items-start gap-4">
        <div className="p-2 bg-gray-800 rounded-lg shrink-0">
          <Globe size={20} className="text-sky-400" />
        </div>
        <div className="flex-1 min-w-0 space-y-3">
          <div>
            <h3 className="text-sm font-semibold text-gray-100">Library server</h3>
            <p className="text-xs text-gray-400 mt-1 leading-relaxed">
              Address of your self-hosted Library. Changing it signs you out so you can
              reconnect to the new server.
            </p>
          </div>
          <ServerUrlField value={serverUrl} onChange={setServerUrl} forceShow />
          <button
            type="button"
            onClick={save}
            className="px-3 py-2 rounded-lg bg-sky-700/80 text-white text-sm font-medium hover:bg-sky-600"
          >
            Save server URL
          </button>
        </div>
      </div>
    </div>
  );
}

export default function Settings() {
  const { user } = useAuth();
  const { toast } = useToast();
  const queryClient = useQueryClient();

  const { data: cacheBytes } = useQuery({
    queryKey: ["audio-cache-usage"],
    queryFn: audioCacheUsageBytes,
    staleTime: 5_000,
    refetchInterval: 15_000,
  });

  const { data: cacheEntries } = useQuery({
    queryKey: ["audio-cache-entries"],
    queryFn: audioCacheEntryCount,
    staleTime: 5_000,
    refetchInterval: 15_000,
  });

  const { data: ebookCacheBytes } = useQuery({
    queryKey: ["ebook-cache-usage"],
    queryFn: ebookCacheUsageBytes,
    staleTime: 5_000,
    refetchInterval: 15_000,
  });

  const { data: ebookCacheEntries } = useQuery({
    queryKey: ["ebook-cache-entries"],
    queryFn: ebookCacheEntryCount,
    staleTime: 5_000,
    refetchInterval: 15_000,
  });

  useEffect(() => {
    const refresh = () => {
      queryClient.invalidateQueries({ queryKey: ["audio-cache-usage"] });
      queryClient.invalidateQueries({ queryKey: ["audio-cache-entries"] });
    };
    window.addEventListener("audio-cache-updated", refresh);
    return () => window.removeEventListener("audio-cache-updated", refresh);
  }, [queryClient]);

  useEffect(() => {
    const refresh = () => {
      queryClient.invalidateQueries({ queryKey: ["ebook-cache-usage"] });
      queryClient.invalidateQueries({ queryKey: ["ebook-cache-entries"] });
    };
    window.addEventListener("ebook-cache-updated", refresh);
    return () => window.removeEventListener("ebook-cache-updated", refresh);
  }, [queryClient]);

  const clearCache = useMutation({
    mutationFn: clearAllAudioCache,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["audio-cache-usage"] });
      queryClient.invalidateQueries({ queryKey: ["audio-cache-entries"] });
      toast("Downloaded audio cleared", "success");
    },
  });

  const clearEbookCache = useMutation({
    mutationFn: clearAllEbookCache,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["ebook-cache-usage"] });
      queryClient.invalidateQueries({ queryKey: ["ebook-cache-entries"] });
      toast("Downloaded ebooks cleared", "success");
    },
  });

  const { data: settings, isLoading } = useQuery({
    queryKey: ["user-settings"],
    queryFn: async () => {
      const { data } = await api.get("/auth/settings");
      return data as UserSettings;
    },
  });

  const updateSettings = useMutation({
    mutationFn: async (body: Partial<UserSettings>) => {
      const { data } = await api.put("/auth/settings", body);
      return data as UserSettings;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(["user-settings"], data);
      toast("Settings updated", "success");
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Failed to update settings", "error");
    },
  });

  if (isLoading) {
    return (
      <div className="max-w-2xl mx-auto px-4 py-8">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-gray-800 rounded w-48" />
          <div className="h-24 bg-gray-800 rounded" />
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto px-4 py-8">
      <div className="flex items-center gap-3 mb-8">
        <SettingsIcon size={24} className="text-brand-400" />
        <div>
          <h1 className="text-2xl font-bold text-gray-100">Settings</h1>
          <p className="text-sm text-gray-500">
            Signed in as <span className="text-gray-300">{user?.username}</span>
          </p>
        </div>
      </div>

      <div className="space-y-4">
        <ServerUrlSettings />
        <DebridKeysSection />
        <LibraryGroupSection />

        <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <div className="flex items-start gap-4">
            <div className="p-2 bg-gray-800 rounded-lg shrink-0">
              <EyeOff size={20} className="text-purple-400" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <h3 className="text-sm font-semibold text-gray-100">Private Mode</h3>
                  <p className="text-xs text-gray-400 mt-1 leading-relaxed">
                    When enabled, any books you request will be hidden from other users' library views.
                    Private books will only appear in your Personal Collection. Other users will still
                    see a "book is already in the library" notice to prevent duplicate requests.
                  </p>
                </div>
                <button
                  onClick={() => updateSettings.mutate({ private_mode: !settings?.private_mode })}
                  disabled={updateSettings.isPending}
                  className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-brand-500 focus:ring-offset-2 focus:ring-offset-gray-900 disabled:opacity-50 ${
                    settings?.private_mode ? "bg-purple-600" : "bg-gray-700"
                  }`}
                  role="switch"
                  aria-checked={settings?.private_mode}
                >
                  <span
                    className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                      settings?.private_mode ? "translate-x-5" : "translate-x-0.5"
                    } mt-0.5`}
                  />
                </button>
              </div>
              {settings?.private_mode && (
                <div className="mt-3 flex items-center gap-2 px-3 py-2 bg-purple-900/20 border border-purple-800/30 rounded-lg">
                  <Shield size={14} className="text-purple-400 shrink-0" />
                  <p className="text-xs text-purple-300">
                    Private mode is active. New download requests will be hidden from other users.
                  </p>
                </div>
              )}
            </div>
          </div>
        </div>

        {(settings?.available_debrid_providers?.length ?? 0) >= 1 && (
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
            <div className="flex items-start gap-4">
              <div className="p-2 bg-gray-800 rounded-lg shrink-0">
                <Zap size={20} className="text-emerald-400" />
              </div>
              <div className="flex-1 min-w-0">
                <h3 className="text-sm font-semibold text-gray-100">Preferred Debrid Service</h3>
                <p className="text-xs text-gray-400 mt-1 leading-relaxed">
                  When you stream a book, the service that already has it cached is picked
                  automatically for instant playback. When it's cached on both (or neither),
                  your preferred service is used.
                </p>
                {(settings?.available_debrid_providers?.length ?? 0) < 2 && (
                  <p className="text-xs text-amber-400/90 mt-2">
                    Add a Torbox key in Debrid API Keys above to enable both services.
                  </p>
                )}
                <div className="mt-3 flex gap-2">
                  {settings!.available_debrid_providers.map((p) => (
                    <button
                      key={p}
                      onClick={() => updateSettings.mutate({ preferred_debrid: p })}
                      disabled={updateSettings.isPending}
                      className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 ${
                        settings?.preferred_debrid === p
                          ? "bg-emerald-600 text-white"
                          : "bg-gray-800 text-gray-300 hover:bg-gray-700"
                      }`}
                    >
                      {DEBRID_LABELS[p] || p}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}

        <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <div className="flex items-start gap-4">
            <div className="p-2 bg-gray-800 rounded-lg shrink-0">
              <HardDrive size={20} className="text-sky-400" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <h3 className="text-sm font-semibold text-gray-100">Downloaded Audio</h3>
                  <p className="text-xs text-gray-400 mt-1 leading-relaxed">
                    Books you play are downloaded to this device in the background so resuming
                    is instant. Each book is removed automatically when you finish it or clear
                    its progress. Currently{" "}
                    <span className="text-gray-200 font-medium">{formatBytes(cacheBytes ?? 0)}</span>
                    {cacheEntries ? ` (${cacheEntries} track${cacheEntries === 1 ? "" : "s"})` : ""}.
                  </p>
                  {audioCacheLastError() && (
                    <p className="text-xs text-amber-400/90 mt-1">
                      Last download issue: {audioCacheLastError()}
                    </p>
                  )}
                </div>
                <button
                  onClick={() => clearCache.mutate()}
                  disabled={clearCache.isPending || !cacheBytes}
                  className="flex items-center gap-1.5 px-3 py-2 bg-gray-800 text-gray-300 text-xs font-medium rounded-lg hover:bg-red-900/40 hover:text-red-300 disabled:opacity-50 transition-colors shrink-0"
                >
                  <Trash2 size={14} />
                  Clear all
                </button>
              </div>
            </div>
          </div>
        </div>

        <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <div className="flex items-start gap-4">
            <div className="p-2 bg-gray-800 rounded-lg shrink-0">
              <HardDrive size={20} className="text-violet-400" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <h3 className="text-sm font-semibold text-gray-100">Downloaded Ebooks</h3>
                  <p className="text-xs text-gray-400 mt-1 leading-relaxed">
                    PDF and EPUB files are saved on this device while you read so reopening
                    is instant. Currently{" "}
                    <span className="text-gray-200 font-medium">{formatBytes(ebookCacheBytes ?? 0)}</span>
                    {ebookCacheEntries ? ` (${ebookCacheEntries} file${ebookCacheEntries === 1 ? "" : "s"})` : ""}.
                  </p>
                </div>
                <button
                  onClick={() => clearEbookCache.mutate()}
                  disabled={clearEbookCache.isPending || !ebookCacheBytes}
                  className="flex items-center gap-1.5 px-3 py-2 bg-gray-800 text-gray-300 text-xs font-medium rounded-lg hover:bg-red-900/40 hover:text-red-300 disabled:opacity-50 transition-colors shrink-0"
                >
                  <Trash2 size={14} />
                  Clear all
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
