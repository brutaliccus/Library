import { useState, useEffect, useCallback } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../hooks/useAuth";
import { useToast } from "../contexts/ToastContext";
import { useLibraryGroup } from "../hooks/useLibraryGroup";
import type { KeySource } from "../hooks/useLibraryGroup";
import api from "../api/client";
import {
  audioCacheUsageBytes,
  audioCacheEntryCount,
  audioCacheLastError,
  clearAllAudioCache,
} from "../utils/audioCache";
import { ebookCacheUsageBytes, ebookCacheEntryCount, clearAllEbookCache } from "../utils/ebookCache";
import {
  Settings as SettingsIcon, EyeOff, Shield, Zap, HardDrive, Trash2,
  Library, Copy, RefreshCw, KeyRound, ChevronUp, ChevronDown,
  Smartphone, Download, ExternalLink, ImagePlus,
} from "lucide-react";
import CoverImage from "../components/CoverImage";
import { upsertRememberedLibrary, currentOrigin } from "../api/libraryRegistry";
import { isNativeApp } from "../api/instanceUrl";
import { resolveInviteShareUrl } from "../api/inviteLink";
import {
  fetchAndroidAppUpdateInfo,
  getInstalledAndroidVersion,
  getLastInstalledReleaseKey,
  installAndroidAppUpdate,
  isUpdateAvailable,
  type AndroidAppUpdateInfo,
} from "../utils/appUpdate";
import { ANDROID_APK_GITHUB_RELEASES_URL } from "../utils/appUpdateConfig";
import { Capacitor } from "@capacitor/core";

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
  const { user } = useAuth();
  const { data } = useLibraryGroup();
  const lib = data?.library;
  const [brandName, setBrandName] = useState("");
  const [savingBrand, setSavingBrand] = useState(false);
  const [uploadingCover, setUploadingCover] = useState(false);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["library-group"] });

  useEffect(() => {
    if (lib?.name) setBrandName(lib.name);
  }, [lib?.name]);

  const syncRegistry = (next: { name?: string; coverUrl?: string | null }) => {
    const origin = currentOrigin();
    const email = user?.email || localStorage.getItem("user_email") || "";
    if (!origin || !email) return;
    upsertRememberedLibrary({
      origin,
      name: next.name || lib?.name || "Library",
      coverUrl: next.coverUrl !== undefined ? next.coverUrl : lib?.coverUrl || null,
      email,
    });
  };

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

  const saveBranding = async () => {
    const name = brandName.trim();
    if (!name) {
      toast("Library name is required", "error");
      return;
    }
    setSavingBrand(true);
    try {
      const { data: res } = await api.put("/libraries/branding", { name });
      syncRegistry({ name: res.library?.name || name, coverUrl: res.library?.coverUrl });
      await refresh();
      toast("Library name saved", "success");
    } catch (e: any) {
      toast(e.response?.data?.detail || "Failed to save library name", "error");
    } finally {
      setSavingBrand(false);
    }
  };

  const uploadCover = async (file: File | null) => {
    if (!file) return;
    setUploadingCover(true);
    try {
      const form = new FormData();
      form.append("cover", file);
      const { data: res } = await api.post("/libraries/branding/cover", form, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      syncRegistry({
        name: res.library?.name || lib?.name,
        coverUrl: res.library?.coverUrl,
      });
      await refresh();
      toast("Cover art updated", "success");
    } catch (e: any) {
      toast(e.response?.data?.detail || "Failed to upload cover", "error");
    } finally {
      setUploadingCover(false);
    }
  };

  if (!lib) return null;
  const isOwner = lib.role === "owner";
  const canInvite = lib.role === "owner" || lib.role === "admin";

  const inviteLink = resolveInviteShareUrl(lib.inviteLink, lib.inviteCode);
  const inviteLooksBroken =
    !!lib.inviteCode &&
    (!inviteLink ||
      inviteLink === lib.inviteCode ||
      /library\.example\.com/i.test(inviteLink) ||
      /localhost|127\.0\.0\.1/i.test(inviteLink));

  const copyInviteLink = () => {
    if (!inviteLink || inviteLink === lib.inviteCode) {
      toast(
        "Set App URL in Admin → Config (or APP_URL in .env) to your public https:// address, then copy again",
        "error"
      );
      return;
    }
    navigator.clipboard?.writeText(inviteLink).then(
      () => toast("Invite link copied", "success"),
      () => toast("Couldn't copy — long-press to copy manually", "error"),
    );
  };

  const shareInviteLink = async () => {
    if (!inviteLink || inviteLink === lib.inviteCode) {
      copyInviteLink();
      return;
    }
    try {
      if (navigator.share) {
        await navigator.share({
          title: `Join ${lib.name}`,
          text: `Join my Library — create your account with this invite:`,
          url: inviteLink,
        });
        return;
      }
    } catch {
      // user cancelled or share failed — fall through to copy
    }
    copyInviteLink();
  };

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <div className="flex items-start gap-4">
        <div className="w-14 aspect-[2/3] rounded-lg overflow-hidden bg-gray-800 shrink-0 flex items-center justify-center">
          <CoverImage
            src={lib.coverUrl}
            alt={lib.name}
            className="w-full h-full object-cover"
            fallback={<Library size={20} className="text-brand-400" />}
          />
        </div>
        <div className="flex-1 min-w-0 space-y-4">
          <div>
            <h3 className="text-sm font-semibold text-gray-100">{lib.name}</h3>
            <p className="text-xs text-gray-400 mt-1">
              Your role: <span className="text-gray-200 capitalize">{lib.role}</span>
            </p>
          </div>

          {isOwner && (
            <div className="space-y-3 rounded-lg border border-gray-800 bg-gray-950/50 p-3">
              <p className="text-xs font-medium text-gray-400">Library card (name & cover)</p>
              <div>
                <label className="block text-[11px] text-gray-500 mb-1">Display name</label>
                <div className="flex gap-2">
                  <input
                    value={brandName}
                    onChange={(e) => setBrandName(e.target.value)}
                    className="flex-1 px-3 py-1.5 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 focus:outline-none focus:border-brand-500"
                  />
                  <button
                    type="button"
                    onClick={() => void saveBranding()}
                    disabled={savingBrand || brandName.trim() === lib.name}
                    className="px-3 py-1.5 bg-brand-600 text-white text-xs font-medium rounded-lg hover:bg-brand-500 disabled:opacity-40"
                  >
                    {savingBrand ? "Saving…" : "Save"}
                  </button>
                </div>
              </div>
              <div>
                <label className="block text-[11px] text-gray-500 mb-1">Cover art</label>
                <div className="flex items-center gap-2">
                  <label className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-gray-800 text-gray-200 text-xs rounded-lg hover:bg-gray-700 cursor-pointer">
                    <ImagePlus size={14} />
                    {uploadingCover ? "Uploading…" : lib.coverUrl ? "Replace cover" : "Upload cover"}
                    <input
                      type="file"
                      accept="image/jpeg,image/png,image/webp,image/gif"
                      className="hidden"
                      disabled={uploadingCover}
                      onChange={(e) => {
                        const f = e.target.files?.[0] || null;
                        e.target.value = "";
                        void uploadCover(f);
                      }}
                    />
                  </label>
                  <span className="text-[11px] text-gray-500">JPEG, PNG, WebP, or GIF · under 8 MB</span>
                </div>
              </div>
            </div>
          )}

          {canInvite && lib.inviteCode && (
            <div className="space-y-2">
              <p className="text-xs font-medium text-gray-400">
                Invite link — share this full URL so friends can join (opens the Android app when
                installed)
              </p>
              {inviteLooksBroken && (
                <p className="text-xs text-amber-400/90 leading-relaxed">
                  Set <span className="font-medium">App URL</span> in Admin → Config (or{" "}
                  <code className="text-amber-300">APP_URL</code> in{" "}
                  <code className="text-amber-300">.env</code>) to your public address, e.g.{" "}
                  <code className="text-amber-300">https://library.yourdomain.com</code>, then
                  reload. Invite links are built from that URL.
                </p>
              )}
              <div className="flex items-start gap-2">
                <code className="flex-1 min-w-0 px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-[11px] text-brand-300 font-mono break-all select-all">
                  {inviteLink || lib.inviteCode}
                </code>
                <button
                  type="button"
                  onClick={shareInviteLink}
                  className="p-2 bg-brand-600 text-white rounded-lg hover:bg-brand-500 transition-colors shrink-0"
                  title="Share or copy invite link"
                >
                  <Copy size={15} />
                </button>
                {isOwner && (
                  <button
                    type="button"
                    onClick={() => regenInvite.mutate()}
                    disabled={regenInvite.isPending}
                    className="p-2 bg-gray-800 text-gray-300 rounded-lg hover:bg-gray-700 disabled:opacity-50 transition-colors shrink-0"
                    title="Generate a new code (old one stops working)"
                  >
                    <RefreshCw size={15} className={regenInvite.isPending ? "animate-spin" : ""} />
                  </button>
                )}
              </div>
              <p className="text-[11px] text-gray-500">
                Code inside the link:{" "}
                <span className="font-mono text-gray-400">{lib.inviteCode}</span>
              </p>
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

function formatApkBytes(bytes: number | null): string {
  if (bytes == null || !Number.isFinite(bytes)) return "—";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Native Android: check GitHub Releases for a newer APK and download/install. */
function AndroidApkSettings() {
  const { toast } = useToast();
  const nativeAndroid = isNativeApp() && Capacitor.getPlatform() === "android";
  const [installed, setInstalled] = useState<{ versionCode: number; versionName: string } | null>(
    null
  );
  const [remote, setRemote] = useState<AndroidAppUpdateInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!nativeAndroid) return;
    setLoading(true);
    setError(null);
    try {
      const [server, local] = await Promise.all([
        fetchAndroidAppUpdateInfo(true),
        getInstalledAndroidVersion().catch(() => null),
      ]);
      setRemote(server);
      setInstalled(local);
    } catch (e: unknown) {
      const detail =
        (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        (e instanceof Error ? e.message : "Update check failed");
      setError(String(detail));
      setRemote(null);
    } finally {
      setLoading(false);
    }
  }, [nativeAndroid]);

  useEffect(() => {
    if (nativeAndroid) void refresh();
  }, [nativeAndroid, refresh]);

  if (!nativeAndroid) return null;

  const lastKey = getLastInstalledReleaseKey();
  const updateReady =
    !!remote &&
    !!installed &&
    isUpdateAvailable(installed.versionCode, remote, lastKey);

  const handleDownload = async () => {
    if (!remote) return;
    setDownloading(true);
    setProgress(0);
    setError(null);
    try {
      await installAndroidAppUpdate(remote, (pct) => setProgress(pct));
      toast("Opening installer…", "success");
      void getInstalledAndroidVersion()
        .then(setInstalled)
        .catch(() => {});
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Download failed";
      setError(msg);
      toast(msg, "error");
    } finally {
      setDownloading(false);
      setProgress(null);
    }
  };

  const releaseLink = remote?.releaseUrl || ANDROID_APK_GITHUB_RELEASES_URL;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <div className="flex items-start gap-4">
        <div className="p-2 bg-gray-800 rounded-lg shrink-0">
          <Smartphone size={20} className="text-emerald-400" />
        </div>
        <div className="flex-1 min-w-0 space-y-3">
          <div>
            <h3 className="text-sm font-semibold text-gray-100">Android app update</h3>
            <p className="text-xs text-gray-400 mt-1 leading-relaxed">
              Checks GitHub Releases for a newer Library APK and installs it on this device.
            </p>
          </div>

          <dl className="text-xs space-y-1.5">
            <div className="flex justify-between gap-3">
              <dt className="text-gray-500">Installed</dt>
              <dd className="text-gray-200 text-right">
                {installed ? (
                  <>
                    v{installed.versionName}
                    <span className="text-gray-500"> (build {installed.versionCode})</span>
                  </>
                ) : (
                  "—"
                )}
              </dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-gray-500">On GitHub</dt>
              <dd className="text-gray-200 text-right">
                {remote ? (
                  <>
                    v{remote.versionName || remote.tagName}
                    {remote.versionCode != null && (
                      <span className="text-gray-500"> (build {remote.versionCode})</span>
                    )}
                    <span className="text-gray-500"> · {formatApkBytes(remote.sizeBytes)}</span>
                  </>
                ) : (
                  "—"
                )}
              </dd>
            </div>
          </dl>

          {updateReady ? (
            <p className="text-xs text-emerald-300">A newer APK is available</p>
          ) : remote && !error ? (
            <p className="text-xs text-gray-500">This device has the latest release</p>
          ) : null}
          {error && <p className="text-xs text-amber-300/90">{error}</p>}
          {progress != null && (
            <p className="text-xs text-gray-400">Downloading… {progress}%</p>
          )}

          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => void refresh()}
              disabled={loading || downloading}
              className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg bg-gray-800 text-gray-200 text-sm font-medium hover:bg-gray-700 disabled:opacity-50"
            >
              <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
              Check for updates
            </button>
            <button
              type="button"
              onClick={() => void handleDownload()}
              disabled={!remote || downloading || loading}
              className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg bg-emerald-700/80 text-white text-sm font-medium hover:bg-emerald-600 disabled:opacity-50"
            >
              <Download size={14} />
              {downloading
                ? "Downloading…"
                : updateReady
                  ? "Download & install"
                  : "Download APK"}
            </button>
            <a
              href={releaseLink}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-gray-700 text-gray-300 text-sm hover:bg-gray-800"
            >
              <ExternalLink size={14} />
              Open on GitHub
            </a>
          </div>
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
        <AndroidApkSettings />
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
                    When enabled, books you request stay visible to you (Audiobookshelf / Kavita /
                    Personal Collection) but are hidden from everyone else's library browse.
                    Others still see an “already in the library” notice so they don't re-download it.
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
                    Private mode is active. Your new downloads stay in your library and stay hidden
                    from other members' browse views.
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
