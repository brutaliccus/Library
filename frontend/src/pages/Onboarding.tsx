import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import api, { applyApiBaseUrl } from "../api/client";
import { useToast } from "../contexts/ToastContext";
import { useAuth } from "../hooks/useAuth";
import { Library, KeyRound, Users, Loader2, ArrowRight, ImagePlus } from "lucide-react";
import {
  applyInvitePaste,
  normalizeInviteCode,
  peekPendingInvite,
  resolveInviteShareUrl,
  takePendingInvite,
} from "../api/inviteLink";
import { isNativeApp } from "../api/instanceUrl";
import { upsertRememberedLibrary, currentOrigin } from "../api/libraryRegistry";
import ThemePicker from "../components/ThemePicker";
import { applyThemeToDocument, DEFAULT_THEME, type ThemeId } from "../theme/themes";

type Mode = "choose" | "create" | "join";

export default function Onboarding() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const { user } = useAuth();
  const [searchParams] = useSearchParams();
  const initialMode =
    searchParams.get("mode") === "create"
      ? "create"
      : searchParams.get("mode") === "join"
        ? "join"
        : "choose";

  const [mode, setMode] = useState<Mode>(initialMode);
  const [busy, setBusy] = useState(false);

  const [name, setName] = useState("");
  const [theme, setTheme] = useState<ThemeId>(DEFAULT_THEME);
  const [coverFile, setCoverFile] = useState<File | null>(null);
  const [coverPreview, setCoverPreview] = useState<string | null>(null);
  const [rdToken, setRdToken] = useState("");
  const [torboxToken, setTorboxToken] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const pending = peekPendingInvite();
    if (pending) {
      setInviteCode(pending);
      if (searchParams.get("mode") !== "create") {
        setMode("join");
      }
      return;
    }
    // First admin (or explicit ?mode=create): go straight to creating the library / invite.
    if (
      searchParams.get("mode") === "create" ||
      (user?.role === "admin" && !searchParams.get("mode"))
    ) {
      setMode("create");
    }
  }, [searchParams, user?.role]);

  const rememberLibrary = (
    lib: { name?: string; coverUrl?: string | null } | null | undefined
  ) => {
    const origin = currentOrigin();
    const email = user?.email || localStorage.getItem("user_email") || "";
    if (!origin || !email) return;
    upsertRememberedLibrary({
      origin,
      name: lib?.name || name.trim() || "Library",
      coverUrl: lib?.coverUrl || null,
      email,
    });
  };

  const finish = async (lib?: { name?: string; coverUrl?: string | null } | null) => {
    rememberLibrary(lib);
    await queryClient.invalidateQueries({ queryKey: ["library-group"] });
    await queryClient.invalidateQueries({ queryKey: ["user-settings"] });
    navigate("/libraries", { replace: true });
  };

  const onCoverPicked = (file: File | null) => {
    if (coverPreview) URL.revokeObjectURL(coverPreview);
    setCoverFile(file);
    setCoverPreview(file ? URL.createObjectURL(file) : null);
  };

  const handleCreate = async () => {
    setError(null);
    if (!name.trim()) {
      setError("Give your library a name");
      return;
    }
    if (!rdToken.trim() && !torboxToken.trim()) {
      setError("Enter at least one API key (Real-Debrid or Torbox)");
      return;
    }
    setBusy(true);
    try {
      const { data } = await api.post("/libraries/create", {
        name: name.trim(),
        real_debrid_api_token: rdToken.trim(),
        torbox_api_token: torboxToken.trim(),
        default_theme: theme,
      });
      let library = data.library;
      if (coverFile) {
        try {
          const form = new FormData();
          form.append("cover", coverFile);
          const uploaded = await api.post("/libraries/branding/cover", form, {
            headers: { "Content-Type": "multipart/form-data" },
          });
          library = uploaded.data.library || library;
        } catch {
          toast("Library created, but cover upload failed — you can add it later in Settings.", "error");
        }
      }
      const link = resolveInviteShareUrl(
        library?.inviteLink,
        library?.inviteCode
      );
      if (link && link !== library?.inviteCode) {
        try {
          await navigator.clipboard?.writeText(link);
          toast("Library created — invite link copied. Share it from Settings anytime.", "success");
        } catch {
          toast("Library created! Copy your invite link from Settings.", "success");
        }
      } else {
        toast(
          "Library created! Set App URL in Admin → Config, then copy the invite link from Settings.",
          "success"
        );
      }
      await finish(library);
    } catch (e: any) {
      setError(e.response?.data?.detail || "Failed to create library");
    } finally {
      setBusy(false);
    }
  };

  const handleJoin = async () => {
    setError(null);
    const parsed = applyInvitePaste(inviteCode);
    const code = parsed?.code || normalizeInviteCode(inviteCode);
    if (!code) {
      setError("Enter your invite code or paste an invite link");
      return;
    }
    if (parsed?.origin) {
      applyApiBaseUrl();
    }
    setBusy(true);
    try {
      const { data } = await api.post("/libraries/join", {
        invite_code: code,
      });
      takePendingInvite();
      toast(`Joined "${data.library?.name || "library"}"!`, "success");
      await finish(data.library);
    } catch (e: any) {
      setError(e.response?.data?.detail || "Failed to join library");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center px-4 py-10">
      <div className="w-full max-w-lg">
        <div className="text-center mb-8">
          <div className="inline-flex p-3 bg-brand-900/40 rounded-2xl mb-4">
            <Library size={32} className="text-brand-400" />
          </div>
          <h1 className="text-2xl font-bold text-gray-100">Set up your library</h1>
          <p className="text-sm text-gray-400 mt-2">
            {mode === "create" || initialMode === "create"
              ? "Name your library, optionally add cover art, and add debrid keys. You'll get an invite link to share — that's how friends create accounts."
              : "All downloaded books are shared. This choice only decides whose debrid account powers your streaming."}
          </p>
        </div>

        {mode === "choose" && (
          <div className="space-y-3">
            <button
              onClick={() => setMode("create")}
              className="w-full flex items-center gap-4 bg-gray-900 border border-gray-800 hover:border-brand-600/60 rounded-xl p-5 text-left transition-colors"
            >
              <div className="p-2.5 bg-gray-800 rounded-lg shrink-0">
                <KeyRound size={22} className="text-emerald-400" />
              </div>
              <div className="flex-1">
                <h3 className="font-semibold text-gray-100">Create my own library</h3>
                <p className="text-xs text-gray-400 mt-1">
                  Use your own Real-Debrid and/or Torbox API keys. You'll get an invite
                  link to share with friends.
                </p>
              </div>
              <ArrowRight size={18} className="text-gray-500 shrink-0" />
            </button>

            <button
              onClick={() => setMode("join")}
              className="w-full flex items-center gap-4 bg-gray-900 border border-gray-800 hover:border-brand-600/60 rounded-xl p-5 text-left transition-colors"
            >
              <div className="p-2.5 bg-gray-800 rounded-lg shrink-0">
                <Users size={22} className="text-sky-400" />
              </div>
              <div className="flex-1">
                <h3 className="font-semibold text-gray-100">Join with an invite</h3>
                <p className="text-xs text-gray-400 mt-1">
                  Paste an invite link or code. Links include the server URL for the
                  Android app — no API keys needed.
                </p>
              </div>
              <ArrowRight size={18} className="text-gray-500 shrink-0" />
            </button>
          </div>
        )}

        {mode === "create" && (
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 space-y-4">
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1.5">Library name</label>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Jared's Library"
                className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:border-brand-500"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1.5">
                Default theme
              </label>
              <p className="text-[11px] text-gray-500 mb-2">
                Members see this look unless they pick their own in Settings.
              </p>
              <ThemePicker
                value={theme}
                onChange={(v) => {
                  if (v === "default") return;
                  setTheme(v);
                  applyThemeToDocument(v);
                }}
                disabled={busy}
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1.5">
                Cover art <span className="text-gray-500 font-normal">(optional — add later in Settings)</span>
              </label>
              <div className="flex items-center gap-3">
                <div className="w-16 aspect-[2/3] rounded-lg overflow-hidden bg-gray-800 border border-gray-700 shrink-0 flex items-center justify-center">
                  {coverPreview ? (
                    <img src={coverPreview} alt="" className="w-full h-full object-cover" />
                  ) : (
                    <ImagePlus size={18} className="text-gray-600" />
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <input
                    type="file"
                    accept="image/jpeg,image/png,image/webp,image/gif"
                    onChange={(e) => onCoverPicked(e.target.files?.[0] || null)}
                    className="block w-full text-xs text-gray-400 file:mr-3 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:text-xs file:font-medium file:bg-gray-800 file:text-gray-200 hover:file:bg-gray-700"
                  />
                  {coverFile && (
                    <button
                      type="button"
                      onClick={() => onCoverPicked(null)}
                      className="mt-1.5 text-[11px] text-gray-500 hover:text-gray-300"
                    >
                      Remove
                    </button>
                  )}
                </div>
              </div>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1.5">
                Real-Debrid API key{" "}
                <a
                  href="https://real-debrid.com/apitoken"
                  target="_blank"
                  rel="noreferrer"
                  className="text-brand-400 hover:underline"
                >
                  (get it here)
                </a>
              </label>
              <input
                value={rdToken}
                onChange={(e) => setRdToken(e.target.value)}
                placeholder="Optional if you provide a Torbox key"
                className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:border-brand-500 font-mono"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1.5">
                Torbox API key{" "}
                <a
                  href="https://torbox.app/settings"
                  target="_blank"
                  rel="noreferrer"
                  className="text-brand-400 hover:underline"
                >
                  (get it here)
                </a>
              </label>
              <input
                value={torboxToken}
                onChange={(e) => setTorboxToken(e.target.value)}
                placeholder="Optional if you provide a Real-Debrid key"
                className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:border-brand-500 font-mono"
              />
            </div>
            {error && <p className="text-xs text-red-400">{error}</p>}
            <div className="flex gap-2 pt-1">
              <button
                onClick={() => { setMode("choose"); setError(null); }}
                className="px-4 py-2 bg-gray-800 text-gray-300 text-sm font-medium rounded-lg hover:bg-gray-700 transition-colors"
              >
                Back
              </button>
              <button
                onClick={handleCreate}
                disabled={busy}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-2 bg-brand-600 text-white text-sm font-medium rounded-lg hover:bg-brand-500 disabled:opacity-50 transition-colors"
              >
                {busy && <Loader2 size={15} className="animate-spin" />}
                {busy ? "Verifying keys..." : "Create library"}
              </button>
            </div>
          </div>
        )}

        {mode === "join" && (
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 space-y-4">
            <div>
              <label className="block text-xs font-medium text-gray-400 mb-1.5">
                Invite link or code
              </label>
              <input
                value={inviteCode}
                onChange={(e) => {
                  const raw = e.target.value;
                  const parsed = applyInvitePaste(raw);
                  if (parsed) {
                    setInviteCode(parsed.code);
                    if (parsed.origin) applyApiBaseUrl();
                    return;
                  }
                  setInviteCode(raw.toUpperCase());
                }}
                onPaste={(e) => {
                  const text = e.clipboardData?.getData("text") || "";
                  const parsed = applyInvitePaste(text);
                  if (parsed) {
                    e.preventDefault();
                    setInviteCode(parsed.code);
                    if (parsed.origin) applyApiBaseUrl();
                    if (isNativeApp() && parsed.origin) {
                      toast("Server URL set from invite link", "success");
                    }
                  }
                }}
                placeholder="Paste invite link or code"
                className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:border-brand-500 font-mono break-all"
              />
              <p className="text-[11px] text-gray-500 mt-1.5">
                Example: https://library.example.com/join/7Q2MKX4RB3ZD
              </p>
            </div>
            {error && <p className="text-xs text-red-400">{error}</p>}
            <div className="flex gap-2 pt-1">
              <button
                onClick={() => { setMode("choose"); setError(null); }}
                className="px-4 py-2 bg-gray-800 text-gray-300 text-sm font-medium rounded-lg hover:bg-gray-700 transition-colors"
              >
                Back
              </button>
              <button
                onClick={handleJoin}
                disabled={busy}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-2 bg-brand-600 text-white text-sm font-medium rounded-lg hover:bg-brand-500 disabled:opacity-50 transition-colors"
              >
                {busy && <Loader2 size={15} className="animate-spin" />}
                {busy ? "Joining..." : "Join library"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
