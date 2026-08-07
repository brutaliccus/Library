import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Library, ImagePlus, Palette } from "lucide-react";
import api from "../../api/client";
import { useToast } from "../../contexts/ToastContext";
import { useAuth } from "../../hooks/useAuth";
import { useLibraryGroup } from "../../hooks/useLibraryGroup";
import type { KeySource } from "../../hooks/useLibraryGroup";
import CoverImage from "../CoverImage";
import ThemePicker from "../ThemePicker";
import {
  applyThemeToDocument,
  normalizePresetThemeId,
  type PresetThemeId,
} from "../../theme/themes";
import { upsertRememberedLibrary, currentOrigin, inviteFieldsFromLibraryMe } from "../../api/libraryRegistry";
import { libraryQueryKey } from "../../utils/libraryQueryKeys";

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

function DebridKeysSection() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const { data, isLoading } = useLibraryGroup();
  const lib = data?.library;

  const [rdToken, setRdToken] = useState("");
  const [torboxToken, setTorboxToken] = useState("");

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: libraryQueryKey("library-group") });
    queryClient.invalidateQueries({ queryKey: libraryQueryKey("user-settings") });
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
  if (!lib) {
    return (
      <div className="bg-gray-800 border border-gray-700 rounded-xl p-4">
        <p className="text-sm text-gray-400">
          No library group yet. Finish{" "}
          <Link to="/onboarding" className="text-brand-400 hover:text-brand-300">
            onboarding
          </Link>{" "}
          to manage debrid keys and branding.
        </p>
      </div>
    );
  }

  if (!lib.canManageKeys) {
    return (
      <div className="bg-gray-800 border border-gray-700 rounded-xl p-4">
        <div className="flex items-start gap-3">
          <div className="p-2 bg-gray-900 rounded-lg shrink-0">
            <KeyRound size={18} className="text-amber-400" />
          </div>
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-gray-100">Debrid API Keys</h3>
            <p className="text-xs text-gray-400 mt-1 leading-relaxed">
              Only the library owner can change Real-Debrid / Torbox keys for{" "}
              <span className="text-gray-200">{lib.name}</span>.
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="bg-gray-800 border border-gray-700 rounded-xl p-4">
      <div className="flex items-start gap-3">
        <div className="p-2 bg-gray-900 rounded-lg shrink-0">
          <KeyRound size={18} className="text-amber-400" />
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
                className="w-full px-3 py-2 bg-gray-900 border border-gray-700 rounded-lg text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:border-brand-500 font-mono"
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
                className="w-full px-3 py-2 bg-gray-900 border border-gray-700 rounded-lg text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:border-brand-500 font-mono"
              />
            </div>
          </div>

          <button
            type="button"
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

function LibraryBrandingSection() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const { data } = useLibraryGroup();
  const lib = data?.library;
  const [brandName, setBrandName] = useState("");
  const [savingBrand, setSavingBrand] = useState(false);
  const [uploadingCover, setUploadingCover] = useState(false);
  const [savingTheme, setSavingTheme] = useState(false);

  const refresh = () => queryClient.invalidateQueries({ queryKey: libraryQueryKey("library-group") });

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
      ...inviteFieldsFromLibraryMe(lib),
    });
  };

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

  const saveDefaultTheme = async (themeId: PresetThemeId) => {
    setSavingTheme(true);
    try {
      await api.put("/libraries/branding", { default_theme: themeId });
      applyThemeToDocument(themeId);
      await refresh();
      await queryClient.invalidateQueries({ queryKey: libraryQueryKey("user-settings") });
      toast("Library default theme updated", "success");
    } catch (e: any) {
      toast(e.response?.data?.detail || "Failed to update theme", "error");
    } finally {
      setSavingTheme(false);
    }
  };

  if (!lib) return null;
  if (lib.role !== "owner") {
    return (
      <div className="bg-gray-800 border border-gray-700 rounded-xl p-4">
        <div className="flex items-start gap-3">
          <div className="w-12 aspect-[2/3] rounded-lg overflow-hidden bg-gray-900 shrink-0 flex items-center justify-center">
            <CoverImage
              src={lib.coverUrl}
              alt={lib.name}
              className="w-full h-full object-cover"
              fallback={<Library size={16} className="text-brand-400" />}
            />
          </div>
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-gray-100">{lib.name}</h3>
            <p className="text-xs text-gray-400 mt-1">
              Only the library owner can change the name, cover, and default theme.
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="bg-gray-800 border border-gray-700 rounded-xl p-4">
      <div className="flex items-start gap-3">
        <div className="w-14 aspect-[2/3] rounded-lg overflow-hidden bg-gray-900 shrink-0 flex items-center justify-center">
          <CoverImage
            src={lib.coverUrl}
            alt={lib.name}
            className="w-full h-full object-cover"
            fallback={<Library size={20} className="text-brand-400" />}
          />
        </div>
        <div className="flex-1 min-w-0 space-y-4">
          <div>
            <h3 className="text-sm font-semibold text-gray-100 flex items-center gap-2">
              <Palette size={16} className="text-brand-400" />
              Library branding
            </h3>
            <p className="text-xs text-gray-400 mt-1">
              Name, cover art, and the default look for members who haven’t picked a personal theme.
            </p>
          </div>

          <div>
            <label className="block text-[11px] text-gray-500 mb-1">Display name</label>
            <div className="flex gap-2">
              <input
                value={brandName}
                onChange={(e) => setBrandName(e.target.value)}
                className="flex-1 px-3 py-1.5 bg-gray-900 border border-gray-700 rounded-lg text-sm text-gray-100 focus:outline-none focus:border-brand-500"
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
            <div className="flex items-center gap-2 flex-wrap">
              <label className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-gray-900 text-gray-200 text-xs rounded-lg hover:bg-gray-700 cursor-pointer border border-gray-700">
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

          <div>
            <label className="block text-[11px] text-gray-500 mb-1.5">Default theme for members</label>
            <ThemePicker
              allowCustom={false}
              value={normalizePresetThemeId(lib.defaultTheme)}
              onChange={(v) => {
                if (v === "default" || v === "custom") return;
                void saveDefaultTheme(v);
              }}
              disabled={savingTheme}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

/** Library debrid keys + branding shown at the top of Admin → Settings. */
export default function LibraryAdminSettings() {
  return (
    <div className="space-y-4 mb-6">
      <div>
        <h2 className="text-lg font-semibold text-gray-100 flex items-center gap-2">
          <Library size={18} />
          Library
        </h2>
        <p className="text-xs text-gray-500 mt-1 max-w-xl">
          Debrid API keys, library name, cover art, and the default theme for members. Invite links
          live under user Settings for owners and library admins. Member roles are managed under
          Users.
        </p>
      </div>
      <DebridKeysSection />
      <LibraryBrandingSection />
    </div>
  );
}
