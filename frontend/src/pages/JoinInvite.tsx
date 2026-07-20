import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { BookOpen, Loader2, LogIn, UserPlus } from "lucide-react";
import api, { applyApiBaseUrl } from "../api/client";
import { useAuth } from "../hooks/useAuth";
import { useToast } from "../contexts/ToastContext";
import {
  applyInvitePaste,
  buildAndroidIntentInviteLink,
  normalizeInviteCode,
  stashPendingInvite,
  takePendingInvite,
} from "../api/inviteLink";
import { isNativeApp } from "../api/instanceUrl";
import { currentOrigin, upsertRememberedLibrary } from "../api/libraryRegistry";

/**
 * /join/:code — shared invite link (invite-only signup).
 * Web (Android): tries to open the installed app, else shows signup.
 * App / desktop web: email + password; creates account and joins library.
 * /join — paste an invite link or code first.
 */
export default function JoinInvite() {
  const { code: pathCode } = useParams();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { user, sessionReady, acceptSession } = useAuth();
  const { toast } = useToast();

  const raw =
    pathCode ||
    params.get("invite") ||
    params.get("code") ||
    params.get("invite_code") ||
    "";
  const forceWeb = params.get("web") === "1";

  const [code, setCode] = useState(() => normalizeInviteCode(raw) || "");
  const [invitePaste, setInvitePaste] = useState("");
  const [libraryName, setLibraryName] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [joiningExisting, setJoiningExisting] = useState(false);
  const [openingApp, setOpeningApp] = useState(false);
  const appAttempted = useRef(false);
  const joinedExisting = useRef(false);

  // Stash invite + (on native) server URL from the page / deep link.
  useEffect(() => {
    if (!raw && !pathCode) return;
    const fromPage =
      typeof window !== "undefined" && pathCode
        ? window.location.href
        : raw;
    const parsed = applyInvitePaste(fromPage);
    if (parsed?.code) {
      setCode(parsed.code);
      return;
    }
    const normalized = normalizeInviteCode(raw);
    if (normalized) {
      stashPendingInvite(normalized);
      setCode(normalized);
    }
  }, [raw, pathCode]);

  // Android browser: hand off to the installed app once, then fall back to web signup.
  useEffect(() => {
    if (forceWeb || isNativeApp() || !code || appAttempted.current) return;
    if (typeof navigator === "undefined" || !/Android/i.test(navigator.userAgent)) {
      return;
    }
    const skipKey = `library_invite_app_tried_${code}`;
    try {
      if (sessionStorage.getItem(skipKey)) return;
      sessionStorage.setItem(skipKey, "1");
    } catch {
      // ignore
    }
    appAttempted.current = true;
    setOpeningApp(true);
    const origin = window.location.origin;
    const intent = buildAndroidIntentInviteLink(code, origin);
    window.location.href = intent;
    const t = window.setTimeout(() => setOpeningApp(false), 1800);
    return () => window.clearTimeout(t);
  }, [code, forceWeb]);

  // Validate invite + show library name.
  useEffect(() => {
    if (!code || !sessionReady) return;
    let cancelled = false;
    (async () => {
      applyApiBaseUrl();
      try {
        const { data } = await api.get(`/auth/invite/${encodeURIComponent(code)}`);
        if (cancelled) return;
        setLibraryName(data.library_name || data.libraryName || "Library");
        setPreviewError(null);
      } catch (e: any) {
        if (cancelled) return;
        setLibraryName(null);
        setPreviewError(e.response?.data?.detail || "Invalid or expired invite link");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [code, sessionReady]);

  // Already signed in → join this library and go home.
  useEffect(() => {
    if (!user || !sessionReady || !code || previewError || joinedExisting.current) return;
    joinedExisting.current = true;
    let cancelled = false;
    (async () => {
      setJoiningExisting(true);
      try {
        await api.post("/libraries/join", { invite_code: code });
        try {
          const lib = await api.get("/libraries/me");
          const origin = currentOrigin();
          const em =
            user?.email || localStorage.getItem("user_email") || user?.username || "";
          if (origin && em) {
            upsertRememberedLibrary({
              origin,
              name: lib.data?.library?.name || libraryName || "Library",
              coverUrl: lib.data?.library?.coverUrl || null,
              email: em,
            });
          }
        } catch {
          /* local save best-effort */
        }
        takePendingInvite();
        if (!cancelled) {
          toast("You're in!", "success");
          navigate("/", { replace: true });
        }
      } catch (e: any) {
        const detail = e.response?.data?.detail || "";
        if (String(detail).toLowerCase().includes("already")) {
          try {
            const lib = await api.get("/libraries/me");
            const origin = currentOrigin();
            const em =
              user?.email || localStorage.getItem("user_email") || user?.username || "";
            if (origin && em) {
              upsertRememberedLibrary({
                origin,
                name: lib.data?.library?.name || libraryName || "Library",
                coverUrl: lib.data?.library?.coverUrl || null,
                email: em,
              });
            }
          } catch {
            /* ignore */
          }
          takePendingInvite();
          if (!cancelled) navigate("/", { replace: true });
          return;
        }
        joinedExisting.current = false;
        if (!cancelled) {
          setError(detail || "Could not join library");
          setJoiningExisting(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [user, sessionReady, code, previewError, navigate, toast]);

  const applyPastedInvite = () => {
    setError("");
    const parsed = applyInvitePaste(invitePaste);
    if (!parsed?.code) {
      setError("Paste a valid invite link or code");
      return;
    }
    setCode(parsed.code);
    if (parsed.origin) applyApiBaseUrl();
    navigate(`/join/${parsed.code}`, { replace: true });
  };

  const handleSignup = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (!code) {
      setError("Missing invite code");
      return;
    }
    if (password !== confirm) {
      setError("Passwords do not match");
      return;
    }
    if (password.length < 6) {
      setError("Password must be at least 6 characters");
      return;
    }
    setLoading(true);
    try {
      applyApiBaseUrl();
      const { data } = await api.post("/auth/signup-with-invite", {
        invite_code: code,
        email: email.trim(),
        password,
      });
      acceptSession(data);
      takePendingInvite();
      toast(`Welcome — you're in ${libraryName || "the library"}!`, "success");
      navigate("/libraries", { replace: true });
    } catch (err: any) {
      setError(
        err.response?.data?.detail ||
          (err.code === "ERR_NETWORK"
            ? "Could not reach the library server"
            : "Could not create account")
      );
    } finally {
      setLoading(false);
    }
  };

  if (!sessionReady || joiningExisting) {
    return (
      <div className="min-h-screen flex items-center justify-center text-gray-400 text-sm">
        <Loader2 className="animate-spin mr-2" size={18} />
        {joiningExisting ? "Joining library…" : "Loading…"}
      </div>
    );
  }

  if (user && code) {
    return (
      <div className="min-h-screen flex items-center justify-center text-gray-400 text-sm">
        <Loader2 className="animate-spin mr-2" size={18} />
        Joining library…
      </div>
    );
  }

  // No code yet — ask for invite link/code (manual path from Login).
  if (!code) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-950 p-4">
        <div className="w-full max-w-sm">
          <div className="text-center mb-8">
            <div className="inline-flex items-center justify-center w-14 h-14 bg-brand-600 text-white rounded-2xl mb-4">
              <BookOpen size={28} />
            </div>
            <h1 className="text-2xl font-bold text-gray-100">Join with invite</h1>
            <p className="text-sm text-gray-400 mt-1">
              Accounts are invite-only. Paste the link or code someone shared with you.
            </p>
          </div>
          <div className="bg-gray-800 rounded-2xl shadow-lg border border-gray-700 p-6 space-y-4">
            {error && (
              <div className="p-3 bg-red-900/30 text-red-400 text-sm rounded-lg">{error}</div>
            )}
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1">
                Invite link or code
              </label>
              <input
                value={invitePaste}
                onChange={(e) => setInvitePaste(e.target.value)}
                onPaste={(e) => {
                  const text = e.clipboardData?.getData("text") || "";
                  const parsed = applyInvitePaste(text);
                  if (parsed?.code) {
                    e.preventDefault();
                    setInvitePaste(text.trim());
                    setCode(parsed.code);
                    if (parsed.origin) applyApiBaseUrl();
                    navigate(`/join/${parsed.code}`, { replace: true });
                  }
                }}
                placeholder="https://…/join/CODE or CODE"
                autoFocus
                className="w-full px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 font-mono text-sm break-all"
              />
            </div>
            <button
              type="button"
              onClick={applyPastedInvite}
              className="w-full flex items-center justify-center gap-2 py-2.5 bg-brand-600 text-white font-medium rounded-lg hover:bg-brand-500"
            >
              Continue
            </button>
          </div>
          <p className="text-center text-sm text-gray-500 mt-4">
            Already have an account?{" "}
            <Link to="/login" className="text-brand-400 hover:text-brand-300 font-medium">
              Sign in
            </Link>
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-950 p-4">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-14 h-14 bg-brand-600 text-white rounded-2xl mb-4">
            <BookOpen size={28} />
          </div>
          <h1 className="text-2xl font-bold text-gray-100">Join library</h1>
          <p className="text-sm text-gray-400 mt-1">
            {openingApp
              ? "Opening the Library app…"
              : libraryName
                ? `Create your account to join ${libraryName}`
                : "Create your email and password to join"}
          </p>
        </div>

        {previewError && (
          <div className="mb-4 p-3 bg-red-900/30 text-red-400 text-sm rounded-lg">
            {previewError}
            <p className="mt-2 text-gray-400 text-xs">
              Ask for a fresh invite link, or{" "}
              <Link to="/login" className="text-brand-400 hover:text-brand-300">
                sign in
              </Link>{" "}
              if you already have an account.
            </p>
          </div>
        )}

        {!previewError && (
          <form
            onSubmit={handleSignup}
            className="bg-gray-800 rounded-2xl shadow-lg border border-gray-700 p-6 space-y-4"
          >
            {error && (
              <div className="p-3 bg-red-900/30 text-red-400 text-sm rounded-lg">{error}</div>
            )}

            {libraryName && (
              <p className="text-xs text-center text-gray-400">
                Invited to{" "}
                <span className="text-gray-200 font-medium">{libraryName}</span>
              </p>
            )}

            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1">
                Email
              </label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
                inputMode="email"
                autoFocus
                required
                maxLength={254}
                className="w-full px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1">
                Password
              </label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="new-password"
                required
                minLength={6}
                className="w-full px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1">
                Confirm password
              </label>
              <input
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                autoComplete="new-password"
                required
                minLength={6}
                className="w-full px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              />
            </div>

            <button
              type="submit"
              disabled={loading || !code}
              className="w-full flex items-center justify-center gap-2 py-2.5 bg-brand-600 text-white font-medium rounded-lg hover:bg-brand-500 disabled:opacity-50 transition-colors"
            >
              {loading ? (
                <Loader2 size={16} className="animate-spin" />
              ) : (
                <UserPlus size={16} />
              )}
              {loading ? "Creating account…" : "Create account & join"}
            </button>
          </form>
        )}

        <p className="text-center text-sm text-gray-500 mt-4">
          Already have an account?{" "}
          <Link
            to="/login"
            className="text-brand-400 hover:text-brand-300 font-medium inline-flex items-center gap-1"
          >
            <LogIn size={14} />
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
