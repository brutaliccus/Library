import { useState, useEffect, useCallback } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "../hooks/useAuth";
import { useToast } from "../contexts/ToastContext";
import { useLibraryGroup } from "../hooks/useLibraryGroup";
import api from "../api/client";
import {
  audioCacheUsageBytes,
  audioCacheEntryCount,
  audioCacheLastError,
  clearAllAudioCache,
} from "../utils/audioCache";
import { ebookCacheUsageBytes, ebookCacheEntryCount, clearAllEbookCache } from "../utils/ebookCache";
import ThemePicker from "../components/ThemePicker";
import { currentOrigin } from "../api/libraryRegistry";
import {
  applyThemeToDocument,
  normalizeThemeId,
  readCachedCustomColors,
  writeCachedCustomColors,
  type CustomThemeColors,
} from "../theme/themes";
import { isNativeApp } from "../api/instanceUrl";
import {
  resolveInviteShareUrl,
  shareLibraryInvite,
} from "../api/inviteLink";
import {
  Settings as SettingsIcon, EyeOff, Shield, Zap, HardDrive, Trash2,
  Copy, RefreshCw, KeyRound, Smartphone, Download, ExternalLink, Palette, Gauge,
  Share2, TabletSmartphone, BookOpen,
} from "lucide-react";
import {
  formatPlaybackSpeed,
  PLAYBACK_SPEED_STEPS,
  snapPlaybackSpeed,
} from "../utils/playbackSpeed";
import {
  getCachedDefaultPlaybackRate,
  setCachedDefaultPlaybackRate,
} from "../utils/playbackRatePrefs";
import {
  fetchAndroidAppUpdateInfo,
  getInstalledAndroidVersion,
  getLastInstalledReleaseKey,
  installAndroidAppUpdate,
  isUpdateAvailable,
  isUpdateRequired,
  type AndroidAppUpdateInfo,
} from "../utils/appUpdate";
import { ANDROID_APK_GITHUB_RELEASES_URL } from "../utils/appUpdateConfig";
import { Capacitor } from "@capacitor/core";
import OfflineUnlockModal from "../components/OfflineUnlockModal";
import {
  biometricAvailable,
  clearOfflineUnlock,
  hasOfflineUnlock,
  setBiometricEnabled,
} from "../utils/offlineUnlock";

interface UserSettings {
  private_mode: boolean;
  preferred_debrid: string;
  available_debrid_providers: string[];
  theme: string | null;
  library_default_theme: string;
  effective_theme: string;
  available_themes: string[];
  clear_theme?: boolean;
  default_playback_rate?: number;
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

/** Invite share link — owners and library admins only. */
function InviteShareSection() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const { data } = useLibraryGroup();
  const lib = data?.library;

  const regenInvite = useMutation({
    mutationFn: async () => (await api.post("/libraries/regenerate-invite")).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["library-group"] });
      toast("New invite code generated — the old one no longer works", "success");
    },
    onError: (e: any) => toast(e.response?.data?.detail || "Failed to regenerate code", "error"),
  });

  if (!lib) return null;
  const canInvite = lib.role === "owner" || lib.role === "admin";
  if (!canInvite || !lib.inviteCode) return null;

  const inviteLink = resolveInviteShareUrl(lib.inviteLink, lib.inviteCode);
  const inviteLooksBroken =
    !!lib.inviteCode &&
    (!inviteLink ||
      inviteLink === lib.inviteCode ||
      /library\.example\.com/i.test(inviteLink) ||
      /localhost|127\.0\.0\.1/i.test(inviteLink));

  const shareInviteLink = () =>
    void shareLibraryInvite({
      libraryName: lib.name,
      inviteCode: lib.inviteCode!,
      inviteLink: lib.inviteLink,
      toast,
    });

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <div className="flex items-start gap-4">
        <div className="p-2 bg-gray-800 rounded-lg shrink-0">
          <Share2 size={20} className="text-brand-400" />
        </div>
        <div className="flex-1 min-w-0 space-y-2">
          <div>
            <h3 className="text-sm font-semibold text-gray-100">Invite link</h3>
            <p className="text-xs text-gray-400 mt-1 leading-relaxed">
              Share this full URL so friends can join{" "}
              <span className="text-gray-200">{lib.name}</span> (opens the Android app when
              installed).
            </p>
          </div>
          {inviteLooksBroken && (
            <p className="text-xs text-amber-400/90 leading-relaxed">
              Set <span className="font-medium">App URL</span> in Admin → Settings (or{" "}
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
            {lib.role === "owner" && (
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
      </div>
    </div>
  );
}

interface EreaderInfo {
  opdsUrl: string;
  opdsUrlLegacy?: string;
  shortCode?: string;
  shelfUrl: string;
  libraryUrl: string;
  appUrlConfigured: boolean;
  shelfCount: number;
  shelf: Array<{
    id: number;
    title: string;
    author: string;
    downloadUrl: string;
  }>;
  instructions: {
    koreader: string;
    moonreader: string;
    kindle: string;
  };
}

function EreaderConnectSection() {
  const { toast } = useToast();
  const queryClient = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["ereader-settings"],
    queryFn: async () => {
      const { data: res } = await api.get("/auth/ereader");
      return res as EreaderInfo;
    },
  });

  const rotate = useMutation({
    mutationFn: async () => (await api.post("/auth/ereader/rotate-token")).data as EreaderInfo,
    onSuccess: (res) => {
      queryClient.setQueryData(["ereader-settings"], res);
      toast("OPDS URL rotated — update your ereader catalog", "success");
    },
    onError: (e: any) => toast(e.response?.data?.detail || "Could not rotate OPDS token", "error"),
  });

  const removeItem = useMutation({
    mutationFn: async (id: number) =>
      (await api.delete(`/auth/ereader/shelf/${id}`)).data as EreaderInfo,
    onSuccess: (res) => {
      queryClient.setQueryData(["ereader-settings"], res);
      toast("Removed from ereader shelf", "success");
    },
    onError: (e: any) => toast(e.response?.data?.detail || "Could not remove item", "error"),
  });

  const copyUrl = async (url: string, label: string) => {
    if (!url) {
      toast("Set App URL in Admin → Config first", "error");
      return;
    }
    try {
      await navigator.clipboard.writeText(url);
      toast(`${label} copied`, "success");
    } catch {
      toast("Could not copy", "error");
    }
  };

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <div className="flex items-start gap-4">
        <div className="p-2 bg-gray-800 rounded-lg shrink-0">
          <TabletSmartphone size={20} className="text-amber-400" />
        </div>
        <div className="flex-1 min-w-0 space-y-3">
          <div>
            <h3 className="text-sm font-semibold text-gray-100">Ereader (OPDS)</h3>
            <p className="text-xs text-gray-400 mt-1 leading-relaxed">
              Connect KOReader, Moon+ Reader, or other OPDS clients. Your personal feed lists
              books you send from ebook pages, plus the full ebook library. Kindle needs email —
              not supported here yet.
            </p>
          </div>

          {isLoading && <p className="text-xs text-gray-500">Loading…</p>}

          {data && !data.appUrlConfigured && (
            <p className="text-xs text-amber-400/90 leading-relaxed">
              Ask an admin to set <span className="font-medium">App URL</span> in Admin → Config
              so your OPDS link uses a public address ereaders can reach.
            </p>
          )}

          {data?.opdsUrl && (
            <div className="space-y-2">
              <p className="text-[11px] text-gray-500 uppercase tracking-wide">OPDS catalog URL</p>
              <div className="flex items-start gap-2">
                <code className="flex-1 min-w-0 px-3 py-2.5 bg-gray-800 border border-amber-700/40 rounded-lg text-sm text-amber-300 font-mono break-all select-all tracking-wide">
                  {data.opdsUrl}
                </code>
                <button
                  type="button"
                  onClick={() => void copyUrl(data.opdsUrl, "OPDS URL")}
                  className="p-2 bg-brand-600 text-white rounded-lg hover:bg-brand-500 transition-colors shrink-0"
                  title="Copy OPDS URL"
                >
                  <Copy size={15} />
                </button>
                <button
                  type="button"
                  onClick={() => {
                    if (
                      window.confirm(
                        "Rotate your OPDS token? Existing ereader catalogs will stop working until you paste the new URL."
                      )
                    ) {
                      rotate.mutate();
                    }
                  }}
                  disabled={rotate.isPending}
                  className="p-2 bg-gray-800 text-gray-300 rounded-lg hover:bg-gray-700 disabled:opacity-50 transition-colors shrink-0"
                  title="Rotate OPDS token"
                >
                  <RefreshCw size={15} className={rotate.isPending ? "animate-spin" : ""} />
                </button>
              </div>
              <p className="text-[11px] text-gray-500 leading-relaxed">
                Short path for typing on KOReader / Moon+. Code is in the URL ? no login needed.
              </p>
              {data.opdsUrlLegacy && data.opdsUrlLegacy !== data.opdsUrl && (
                <details className="text-[11px] text-gray-500">
                  <summary className="cursor-pointer text-gray-400 hover:text-gray-300">
                    Alternate long URL
                  </summary>
                  <div className="mt-1.5 flex items-start gap-2">
                    <code className="flex-1 min-w-0 px-2 py-1.5 bg-gray-800/80 border border-gray-700 rounded text-[10px] text-gray-400 font-mono break-all select-all">
                      {data.opdsUrlLegacy}
                    </code>
                    <button
                      type="button"
                      onClick={() => void copyUrl(data.opdsUrlLegacy!, "Long OPDS URL")}
                      className="p-1.5 bg-gray-800 text-gray-400 rounded hover:bg-gray-700 shrink-0"
                      title="Copy long OPDS URL"
                    >
                      <Copy size={13} />
                    </button>
                  </div>
                </details>
              )}
              <ul className="text-xs text-gray-500 space-y-1.5 list-disc pl-4">
                <li>{data.instructions.koreader}</li>
                <li>{data.instructions.moonreader}</li>
                <li>{data.instructions.kindle}</li>
              </ul>
            </div>
          )}

          {data && data.shelfCount > 0 && (
            <div className="space-y-2 pt-1">
              <p className="text-[11px] text-gray-500 uppercase tracking-wide">
                Send to ereader shelf ({data.shelfCount})
              </p>
              <ul className="space-y-1.5">
                {data.shelf.slice(0, 8).map((item) => (
                  <li
                    key={item.id}
                    className="flex items-center gap-2 text-xs text-gray-300 bg-gray-800/60 rounded-lg px-2.5 py-1.5"
                  >
                    <BookOpen size={12} className="text-amber-400/80 shrink-0" />
                    <span className="flex-1 min-w-0 truncate">
                      {item.title || "Ebook"}
                      {item.author ? (
                        <span className="text-gray-500"> · {item.author}</span>
                      ) : null}
                    </span>
                    <button
                      type="button"
                      onClick={() => void copyUrl(item.downloadUrl, "Download link")}
                      className="text-gray-400 hover:text-gray-200 shrink-0"
                      title="Copy download URL"
                    >
                      <Copy size={12} />
                    </button>
                    <button
                      type="button"
                      onClick={() => removeItem.mutate(item.id)}
                      disabled={removeItem.isPending}
                      className="text-gray-500 hover:text-red-400 shrink-0 disabled:opacity-50"
                      title="Remove from shelf"
                    >
                      <Trash2 size={12} />
                    </button>
                  </li>
                ))}
              </ul>
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
  const updateRequired =
    !!remote && !!installed && isUpdateRequired(installed.versionCode, remote);

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
              Required updates cannot be dismissed.
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

          {updateRequired ? (
            <p className="text-xs text-amber-300">Update required — install to continue using the app</p>
          ) : updateReady ? (
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
  const origin = currentOrigin();
  const unlockEmail = (user?.email || user?.username || "").toLowerCase();
  const [unlockSetupOpen, setUnlockSetupOpen] = useState(false);
  const [unlockEnrolled, setUnlockEnrolled] = useState(() =>
    origin && unlockEmail ? hasOfflineUnlock(origin, unlockEmail) : false
  );
  const [bioSupported, setBioSupported] = useState(false);
  const [customColors, setCustomColors] = useState<CustomThemeColors>(() =>
    readCachedCustomColors()
  );

  useEffect(() => {
    void biometricAvailable().then(setBioSupported);
  }, []);

  useEffect(() => {
    if (origin && unlockEmail) {
      setUnlockEnrolled(hasOfflineUnlock(origin, unlockEmail));
    }
  }, [origin, unlockEmail]);

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
      if (data.default_playback_rate != null) {
        setCachedDefaultPlaybackRate(data.default_playback_rate);
      }
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
        <InviteShareSection />
        <EreaderConnectSection />

        <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <div className="flex items-start gap-4">
            <div className="p-2 bg-gray-800 rounded-lg shrink-0">
              <Palette size={20} className="text-brand-400" />
            </div>
            <div className="flex-1 min-w-0 space-y-3">
              <div>
                <h3 className="text-sm font-semibold text-gray-100">Appearance</h3>
                <p className="text-xs text-gray-400 mt-1 leading-relaxed">
                  Choose your look, including a custom 3-color theme. “Library default” follows
                  what the owner set for this library.
                </p>
              </div>
              <ThemePicker
                allowDefault
                allowCustom
                libraryDefaultLabel="Library default"
                value={settings?.theme ? normalizeThemeId(settings.theme) : "default"}
                customColors={customColors}
                disabled={updateSettings.isPending}
                onChange={(v, colors) => {
                  if (v === "default") {
                    updateSettings.mutate({ clear_theme: true, theme: null });
                    const libTheme = normalizeThemeId(settings?.library_default_theme);
                    applyThemeToDocument(libTheme);
                    return;
                  }
                  if (v === "custom" && colors) {
                    setCustomColors(colors);
                    writeCachedCustomColors(colors);
                    applyThemeToDocument("custom", colors);
                    updateSettings.mutate({ theme: "custom" });
                    return;
                  }
                  applyThemeToDocument(v);
                  updateSettings.mutate({ theme: v });
                }}
              />
            </div>
          </div>
        </div>

        <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <div className="flex items-start gap-4">
            <div className="p-2 bg-gray-800 rounded-lg shrink-0">
              <Gauge size={20} className="text-brand-400" />
            </div>
            <div className="flex-1 min-w-0 space-y-3">
              <div>
                <h3 className="text-sm font-semibold text-gray-100">Default playback speed</h3>
                <p className="text-xs text-gray-400 mt-1 leading-relaxed">
                  Used for new books. Per-book speed from the player overrides this until you
                  finish the book or clear its progress.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <select
                  value={snapPlaybackSpeed(
                    settings?.default_playback_rate ?? getCachedDefaultPlaybackRate()
                  )}
                  disabled={updateSettings.isPending}
                  onChange={(e) => {
                    const rate = snapPlaybackSpeed(parseFloat(e.target.value));
                    setCachedDefaultPlaybackRate(rate);
                    updateSettings.mutate({ default_playback_rate: rate });
                  }}
                  className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500"
                  aria-label="Default playback speed"
                >
                  {PLAYBACK_SPEED_STEPS.map((s) => (
                    <option key={s} value={s}>
                      {formatPlaybackSpeed(s)}
                    </option>
                  ))}
                </select>
                <span className="text-xs text-gray-500">
                  Current default:{" "}
                  {formatPlaybackSpeed(
                    settings?.default_playback_rate ?? getCachedDefaultPlaybackRate()
                  )}
                </span>
              </div>
            </div>
          </div>
        </div>

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
                    My Collection) but are hidden from everyone else's library browse.
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
                  For streams and downloads: if a torrent is cached on exactly one service,
                  that service is used. If it&apos;s cached on both — or on neither — your
                  preferred service wins.
                </p>
                {(settings?.available_debrid_providers?.length ?? 0) < 2 && (
                  <p className="text-xs text-amber-400/90 mt-2">
                    Ask the library owner to add a Torbox key in Admin → Settings to enable both
                    services.
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
              <KeyRound size={20} className="text-amber-400" />
            </div>
            <div className="flex-1 min-w-0">
              <h3 className="text-sm font-semibold text-gray-100">Offline unlock</h3>
              <p className="text-xs text-gray-400 mt-1 leading-relaxed">
                A local PIN (and optional biometric) unlocks this library when you&apos;re offline.
                It stays on this device and is never sent to the server.
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => setUnlockSetupOpen(true)}
                  className="px-3 py-2 bg-gray-800 text-gray-200 text-xs font-medium rounded-lg hover:bg-gray-700"
                >
                  {unlockEnrolled ? "Change PIN" : "Set up PIN"}
                </button>
                {unlockEnrolled && bioSupported && (
                  <button
                    type="button"
                    onClick={() => {
                      void setBiometricEnabled(origin, unlockEmail, true).then((ok) => {
                        toast(ok ? "Biometric unlock enabled" : "Biometric not available", ok ? "success" : "error");
                      });
                    }}
                    className="px-3 py-2 bg-gray-800 text-gray-200 text-xs font-medium rounded-lg hover:bg-gray-700"
                  >
                    Enable biometric
                  </button>
                )}
                {unlockEnrolled && (
                  <button
                    type="button"
                    onClick={() => {
                      clearOfflineUnlock(origin, unlockEmail);
                      setUnlockEnrolled(false);
                      toast("Offline unlock removed", "info");
                    }}
                    className="px-3 py-2 bg-gray-800 text-red-300 text-xs font-medium rounded-lg hover:bg-red-900/40"
                  >
                    Remove
                  </button>
                )}
              </div>
              <p className="text-[11px] text-gray-500 mt-2">
                Status: {unlockEnrolled ? "Ready for offline open" : "Not set up"}
              </p>
            </div>
          </div>
        </div>

        {unlockSetupOpen && origin && unlockEmail && (
          <OfflineUnlockModal
            mode="setup"
            libraryName="This library"
            origin={origin}
            email={unlockEmail}
            onClose={() => setUnlockSetupOpen(false)}
            onUnlocked={() => {
              setUnlockSetupOpen(false);
              setUnlockEnrolled(true);
              toast("Offline unlock ready", "success");
            }}
          />
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
