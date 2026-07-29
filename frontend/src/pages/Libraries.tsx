import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  BookOpen,
  Library,
  LogIn,
  LogOut,
  Trash2,
  Pencil,
  Loader2,
  X,
  WifiOff,
} from "lucide-react";
import { useAuth } from "../hooks/useAuth";
import CoverImage from "../components/CoverImage";
import OfflineUnlockModal from "../components/OfflineUnlockModal";
import ConfirmModal from "../components/ConfirmModal";
import {
  getSessionForOrigin,
  listRememberedLibraries,
  loadRegistry,
  removeRememberedLibrary,
  upsertRememberedLibrary,
  type RememberedLibrary,
} from "../api/libraryRegistry";
import {
  applyInvitePaste,
  normalizeInviteCode,
} from "../api/inviteLink";
import { applyApiBaseUrl } from "../api/client";
import api from "../api/client";
import { setInstanceUrl, normalizeInstanceUrl } from "../api/instanceUrl";
import { useToast } from "../contexts/ToastContext";
import { useOnlineStatus } from "../hooks/useOnlineStatus";
import {
  dismissOfflineUnlockPrompt,
  hasOfflineUnlock,
} from "../utils/offlineUnlock";

export default function LibrariesPage() {
  const { user, logout, enterLibrary, enterLibraryOffline, login, sessionReady, setupRequired } =
    useAuth();
  const navigate = useNavigate();
  const { toast } = useToast();
  const online = useOnlineStatus();
  const email = user?.email || loadRegistry().email || "";
  const [tick, setTick] = useState(0);
  const libraries = useMemo(
    () => listRememberedLibraries(),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [tick, user?.email]
  );

  const [busyOrigin, setBusyOrigin] = useState<string | null>(null);
  const [showAddOrSignIn, setShowAddOrSignIn] = useState(false);
  const [signInUrl, setSignInUrl] = useState("");
  const [signInEmail, setSignInEmail] = useState("");
  const [signInPassword, setSignInPassword] = useState("");
  const [signInBusy, setSignInBusy] = useState(false);
  const [signInError, setSignInError] = useState("");
  const [inviteInput, setInviteInput] = useState("");
  const [addBusy, setAddBusy] = useState(false);
  const [addError, setAddError] = useState("");
  const [editLib, setEditLib] = useState<RememberedLibrary | null>(null);
  const [editName, setEditName] = useState("");
  const [editOrigin, setEditOrigin] = useState("");
  const [loginLib, setLoginLib] = useState<RememberedLibrary | null>(null);
  const [loginEmail, setLoginEmail] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState("");
  const [unlockLib, setUnlockLib] = useState<RememberedLibrary | null>(null);
  const [unlockMode, setUnlockMode] = useState<"setup" | "unlock">("unlock");
  /** Post-login one-time prompt may be dismissed; intentional setup from Open may not. */
  const [unlockAllowSkip, setUnlockAllowSkip] = useState(false);
  const [removeLib, setRemoveLib] = useState<RememberedLibrary | null>(null);

  if (!sessionReady) {
    return (
      <div className="min-h-screen flex items-center justify-center text-gray-400 text-sm">
        <Loader2 className="animate-spin mr-2" size={18} />
        Loading…
      </div>
    );
  }

  const libraryStatus = (lib: RememberedLibrary) => {
    const session = getSessionForOrigin(lib.origin);
    const unlock = hasOfflineUnlock(lib.origin, lib.email || session?.email || "");
    if (!session) {
      return { canOpenOnline: online, canOpenOffline: false, reason: "Sign in once online" as const };
    }
    if (!unlock) {
      return {
        canOpenOnline: online,
        canOpenOffline: false,
        reason: "Set up offline unlock" as const,
      };
    }
    return { canOpenOnline: online, canOpenOffline: true, reason: null };
  };

  const openLibrary = async (lib: RememberedLibrary) => {
    setBusyOrigin(lib.origin);
    try {
      const result = await enterLibrary(lib.origin);
      if (result === "ok") {
        navigate("/my-library", { replace: true });
        return;
      }
      if (result === "need_offline_unlock") {
        setUnlockMode("unlock");
        setUnlockAllowSkip(false);
        setUnlockLib(lib);
        return;
      }
      if (result === "need_offline_setup") {
        if (online) {
          setUnlockMode("setup");
          setUnlockAllowSkip(false);
          setUnlockLib(lib);
          toast("Set a PIN to open this library offline", "info");
          return;
        }
        toast("Set up offline unlock while online first", "info");
        return;
      }
      if (result === "need_login") {
        if (!online) {
          toast("Sign in once online to save a session for offline use", "info");
          return;
        }
        setLoginLib(lib);
        setLoginEmail(lib.email || email || "");
        setLoginPassword("");
        setLoginError("");
        return;
      }
      toast("Could not open library", "error");
    } catch (e: any) {
      toast(e?.message || "Could not open library", "error");
    } finally {
      setBusyOrigin(null);
    }
  };

  const finishOfflineOpen = async (lib: RememberedLibrary) => {
    setBusyOrigin(lib.origin);
    try {
      const result = await enterLibraryOffline(lib.origin);
      if (result === "ok") {
        setUnlockLib(null);
        navigate("/my-library", { replace: true });
        return;
      }
      toast("Could not open offline library", "error");
    } finally {
      setBusyOrigin(null);
    }
  };

  const confirmLogin = async () => {
    if (!loginLib || !loginEmail.trim() || !loginPassword) return;
    setLoginBusy(true);
    setLoginError("");
    try {
      await login(loginEmail.trim(), loginPassword, loginLib.origin);
      setLoginLib(null);
      // Global OfflineUnlockSetupPrompt handles one-time PIN setup after navigate.
      navigate("/my-library", { replace: true });
    } catch (e: any) {
      setLoginError(e?.response?.data?.detail || "Sign in failed");
    } finally {
      setLoginBusy(false);
    }
  };

  const leaveLibrary = async (lib: RememberedLibrary) => {
    try {
      try {
        setInstanceUrl(lib.origin);
        applyApiBaseUrl();
        await api.post("/libraries/leave");
      } catch {
        /* host may be unreachable — still remove locally */
      }
      removeRememberedLibrary(lib.origin, lib.email);
      setTick((t) => t + 1);
      toast("Removed from this device", "success");
    } catch {
      removeRememberedLibrary(lib.origin, lib.email);
      setTick((t) => t + 1);
    }
  };

  const requestLeaveLibrary = (lib: RememberedLibrary, e: React.MouseEvent) => {
    e.stopPropagation();
    setRemoveLib(lib);
  };

  const startEdit = (lib: RememberedLibrary, e: React.MouseEvent) => {
    e.stopPropagation();
    setEditLib(lib);
    setEditName(lib.name);
    setEditOrigin(lib.origin);
  };

  const saveEdit = () => {
    if (!editLib) return;
    const origin = normalizeInstanceUrl(editOrigin);
    if (!origin) {
      toast("Enter a valid library URL", "error");
      return;
    }
    if (origin !== editLib.origin) {
      removeRememberedLibrary(editLib.origin, editLib.email);
    }
    upsertRememberedLibrary({
      origin,
      name: editName.trim() || "Library",
      coverUrl: editLib.coverUrl,
      email: editLib.email || email || "unknown",
    });
    setEditLib(null);
    setTick((t) => t + 1);
    toast("Library updated on this device", "success");
  };

  const addViaInvite = async () => {
    setAddError("");
    const parsed = applyInvitePaste(inviteInput);
    const code = parsed?.code || normalizeInviteCode(inviteInput);
    if (!code) {
      setAddError("Paste an invite link or code");
      return;
    }
    const origin = parsed?.origin;
    if (!origin) {
      setAddError("Use a full invite link (https://…/join/CODE) so the app knows which server");
      return;
    }
    if (!online) {
      setAddError("Connect to the internet to join a library");
      return;
    }
    setAddBusy(true);
    try {
      setInstanceUrl(origin);
      applyApiBaseUrl();
      navigate(`/join/${code}`, { replace: false });
      setShowAddOrSignIn(false);
      setInviteInput("");
    } catch (err: any) {
      setAddError(err?.message || "Could not open invite");
    } finally {
      setAddBusy(false);
    }
  };

  const signInExisting = async () => {
    setSignInError("");
    const origin = normalizeInstanceUrl(signInUrl);
    if (!origin) {
      setSignInError("Enter a valid library URL (https://…)");
      return;
    }
    if (!signInEmail.trim() || !signInPassword) {
      setSignInError("Enter your email or username and password");
      return;
    }
    if (!online) {
      setSignInError("Password sign-in needs a connection. Use Open offline if set up.");
      return;
    }
    setSignInBusy(true);
    try {
      await login(signInEmail.trim(), signInPassword, origin);
      setShowAddOrSignIn(false);
      // Global OfflineUnlockSetupPrompt handles one-time PIN setup after navigate.
      navigate("/my-library", { replace: true });
    } catch (e: any) {
      setSignInError(e?.response?.data?.detail || "Sign in failed");
    } finally {
      setSignInBusy(false);
    }
  };

  return (
    <div className="min-h-screen bg-gray-950 px-4 pt-[calc(2rem+env(safe-area-inset-top,0px))] pb-[calc(2rem+env(safe-area-inset-bottom,0px))] pl-[max(1rem,env(safe-area-inset-left,0px))] pr-[max(1rem,env(safe-area-inset-right,0px))]">
      <div className="max-w-3xl mx-auto">
        <div className="flex items-start justify-between gap-3 mb-8">
          <div className="min-w-0">
            <div className="inline-flex items-center gap-2 text-brand-400 mb-2">
              <Library size={22} />
              <span className="text-sm font-medium uppercase tracking-wide">Your libraries</span>
            </div>
            <h1 className="text-2xl font-bold text-gray-100">Choose a library</h1>
            <p className="text-sm text-gray-400 mt-1">
              Saved on this device. Add libraries with an invite link — each server keeps your
              account.
            </p>
            {!online && (
              <p className="mt-2 inline-flex items-center gap-1.5 text-xs text-amber-300/90">
                <WifiOff size={13} />
                Offline — open a library with offline unlock, or connect to sign in.
              </p>
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={() => {
                setAddError("");
                setSignInError("");
                setShowAddOrSignIn(true);
              }}
              disabled={!online}
              className="inline-flex items-center justify-center gap-2 px-4 h-10 rounded-full border border-gray-700 text-gray-200 hover:bg-gray-800 disabled:opacity-40 text-sm font-medium"
              title="Add or sign in to a library"
            >
              <LogIn size={16} />
              <span className="hidden sm:inline">Add or sign in</span>
              <span className="sm:hidden">Sign in</span>
            </button>
            {user ? (
              <button
                type="button"
                onClick={() => {
                  logout();
                  setTick((t) => t + 1);
                }}
                className="p-2.5 rounded-lg text-gray-400 border border-gray-700 hover:bg-gray-800 hover:text-red-400 transition-colors"
                title="Sign out"
                aria-label="Sign out"
              >
                <LogOut size={18} />
              </button>
            ) : null}
          </div>
        </div>

        {libraries.length === 0 ? (
          <div className="rounded-2xl border border-gray-800 bg-gray-900/60 p-8 text-center">
            <BookOpen size={36} className="mx-auto text-gray-600 mb-3" />
            <p className="text-gray-200 font-medium">No libraries on this device</p>
            <p className="text-sm text-gray-500 mt-1 mb-5">
              {online
                ? setupRequired
                  ? "This server has no admin yet — create the first account to continue."
                  : "Use Add or sign in above — paste an invite link to join, or sign in if you already have an account (admins don’t need an invite)."
                : "Connect once to join or sign in, then set up offline unlock for next time."}
            </p>
            {setupRequired && online ? (
              <Link
                to="/login"
                className="inline-flex items-center justify-center gap-2 px-5 h-11 rounded-full bg-brand-600 text-white hover:bg-brand-500 text-sm font-medium"
              >
                Create admin account
              </Link>
            ) : (
              <p className="text-xs text-gray-600">
                Or{" "}
                <Link to="/join" className="text-brand-400 hover:text-brand-300">
                  open join page
                </Link>
              </p>
            )}
          </div>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-4">
            {libraries.map((lib) => {
              const status = libraryStatus(lib);
              const disabledOffline = !online && !status.canOpenOffline;
              return (
                <button
                  key={`${lib.origin}:${lib.email}`}
                  type="button"
                  onClick={() => void openLibrary(lib)}
                  disabled={busyOrigin === lib.origin || disabledOffline}
                  className="group text-left rounded-xl overflow-hidden border border-gray-800 bg-gray-900/50 hover:border-brand-600/50 hover:bg-gray-900 transition-all disabled:opacity-60"
                >
                  <div className="relative aspect-[2/3] bg-gray-800">
                    <CoverImage
                      src={lib.coverUrl}
                      alt={lib.name}
                      className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
                      fallback={
                        <div className="w-full h-full flex items-center justify-center text-gray-600">
                          <BookOpen size={32} />
                        </div>
                      }
                    />
                    {busyOrigin === lib.origin && (
                      <div className="absolute inset-0 bg-black/50 flex items-center justify-center">
                        <Loader2 className="animate-spin text-white" size={24} />
                      </div>
                    )}
                    <div className="absolute top-2 right-2 flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                      <span
                        role="button"
                        tabIndex={0}
                        onClick={(e) => startEdit(lib, e)}
                        className="p-1.5 rounded-lg bg-black/60 text-gray-300 hover:text-white"
                        title="Edit"
                      >
                        <Pencil size={14} />
                      </span>
                      <span
                        role="button"
                        tabIndex={0}
                        onClick={(e) => requestLeaveLibrary(lib, e)}
                        className="p-1.5 rounded-lg bg-black/60 text-gray-300 hover:text-red-300"
                        title="Remove from this device"
                      >
                        <Trash2 size={14} />
                      </span>
                    </div>
                  </div>
                  <div className="p-3">
                    <p className="text-sm font-semibold text-gray-100 truncate">{lib.name}</p>
                    <p className="text-[11px] text-gray-500 truncate mt-0.5">
                      {lib.origin.replace(/^https?:\/\//, "")}
                    </p>
                    {!online && (
                      <p className="text-[10px] mt-1 text-amber-400/90">
                        {status.canOpenOffline
                          ? "Open offline"
                          : status.reason || "Unavailable offline"}
                      </p>
                    )}
                    {online && status.reason === "Set up offline unlock" && (
                      <p className="text-[10px] mt-1 text-gray-500">Offline unlock not set</p>
                    )}
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </div>

      {showAddOrSignIn && (
        <Modal title="Add or sign in" onClose={() => setShowAddOrSignIn(false)}>
          <div className="space-y-4">
            <section>
              <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2">
                Invite code
              </h3>
              <p className="text-xs text-gray-500 mb-2">
                Paste a full invite link. The server address is taken from the link.
              </p>
              <input
                value={inviteInput}
                onChange={(e) => setInviteInput(e.target.value)}
                placeholder="https://…/join/CODE"
                className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 mb-2"
                autoFocus
              />
              {addError && <p className="text-xs text-red-400 mb-2">{addError}</p>}
              <button
                type="button"
                disabled={addBusy || !online}
                onClick={() => void addViaInvite()}
                className="w-full py-2 bg-brand-600 text-white text-sm font-medium rounded-lg hover:bg-brand-500 disabled:opacity-50"
              >
                {addBusy ? "Opening…" : "Continue with invite"}
              </button>
            </section>

            <div className="flex items-center gap-3 text-[11px] uppercase tracking-wide text-gray-600">
              <div className="flex-1 h-px bg-gray-800" />
              or
              <div className="flex-1 h-px bg-gray-800" />
            </div>

            <section>
              <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2">
                Sign in
              </h3>
              <p className="text-xs text-gray-500 mb-2">
                For accounts that already exist on a server (including admins). No invite needed.
              </p>
              {signInError && (
                <div className="mb-2 p-2 bg-red-900/30 text-red-400 text-xs rounded-lg">
                  {signInError}
                </div>
              )}
              <label className="block text-[11px] text-gray-500 mb-1">Library URL</label>
              <input
                value={signInUrl}
                onChange={(e) => setSignInUrl(e.target.value)}
                placeholder="https://library.example.com"
                className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 mb-3"
                autoComplete="url"
              />
              <label className="block text-[11px] text-gray-500 mb-1">Email or username</label>
              <input
                type="text"
                value={signInEmail}
                onChange={(e) => setSignInEmail(e.target.value)}
                className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 mb-3"
                autoComplete="username"
              />
              <label className="block text-[11px] text-gray-500 mb-1">Password</label>
              <input
                type="password"
                value={signInPassword}
                onChange={(e) => setSignInPassword(e.target.value)}
                className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 mb-3"
                autoComplete="current-password"
              />
              <button
                type="button"
                disabled={signInBusy || !online}
                onClick={() => void signInExisting()}
                className="w-full flex items-center justify-center gap-2 py-2 bg-brand-600 text-white text-sm font-medium rounded-lg hover:bg-brand-500 disabled:opacity-50"
              >
                <LogIn size={14} />
                {signInBusy ? "Signing in…" : "Sign in"}
              </button>
            </section>
          </div>
        </Modal>
      )}

      {editLib && (
        <Modal title="Edit library" onClose={() => setEditLib(null)}>
          <label className="block text-[11px] text-gray-500 mb-1">Name</label>
          <input
            value={editName}
            onChange={(e) => setEditName(e.target.value)}
            className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 mb-3"
          />
          <label className="block text-[11px] text-gray-500 mb-1">Library URL</label>
          <input
            value={editOrigin}
            onChange={(e) => setEditOrigin(e.target.value)}
            className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 mb-3"
          />
          <button
            type="button"
            onClick={saveEdit}
            className="w-full py-2 bg-brand-600 text-white text-sm font-medium rounded-lg hover:bg-brand-500"
          >
            Save
          </button>
        </Modal>
      )}

      {loginLib && (
        <Modal title={`Sign in — ${loginLib.name}`} onClose={() => setLoginLib(null)}>
          <p className="text-xs text-gray-400 mb-3">
            Account for {loginLib.origin.replace(/^https?:\/\//, "")}
          </p>
          {loginError && (
            <div className="mb-2 p-2 bg-red-900/30 text-red-400 text-xs rounded-lg">{loginError}</div>
          )}
          <label className="block text-[11px] text-gray-500 mb-1">Email</label>
          <input
            type="email"
            value={loginEmail}
            onChange={(e) => setLoginEmail(e.target.value)}
            className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 mb-3"
            autoComplete="email"
          />
          <label className="block text-[11px] text-gray-500 mb-1">Password</label>
          <input
            type="password"
            value={loginPassword}
            onChange={(e) => setLoginPassword(e.target.value)}
            className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 mb-3"
            autoComplete="current-password"
            autoFocus
          />
          <button
            type="button"
            disabled={loginBusy || !loginEmail || !loginPassword}
            onClick={() => void confirmLogin()}
            className="w-full flex items-center justify-center gap-2 py-2 bg-brand-600 text-white text-sm font-medium rounded-lg hover:bg-brand-500 disabled:opacity-50"
          >
            <LogIn size={14} />
            {loginBusy ? "Signing in…" : "Sign in"}
          </button>
        </Modal>
      )}

      {unlockLib && (
        <OfflineUnlockModal
          mode={unlockMode}
          libraryName={unlockLib.name}
          origin={unlockLib.origin}
          email={unlockLib.email || email}
          allowSkip={unlockAllowSkip}
          onClose={() => {
            if (unlockMode === "setup" && unlockAllowSkip) {
              dismissOfflineUnlockPrompt(
                unlockLib.origin,
                unlockLib.email || email || loginEmail || signInEmail
              );
            }
            setUnlockLib(null);
          }}
          onSkip={() => {
            dismissOfflineUnlockPrompt(
              unlockLib.origin,
              unlockLib.email || email || loginEmail || signInEmail
            );
            setUnlockLib(null);
            navigate("/my-library", { replace: true });
          }}
          onUnlocked={() => {
            void (async () => {
              // After PIN setup while online, re-enter with live /auth/me.
              if (online && unlockMode === "setup") {
                setBusyOrigin(unlockLib.origin);
                try {
                  const result = await enterLibrary(unlockLib.origin);
                  setUnlockLib(null);
                  if (result === "ok") {
                    navigate("/my-library", { replace: true });
                    return;
                  }
                  if (result === "need_offline_unlock") {
                    await finishOfflineOpen(unlockLib);
                    return;
                  }
                  await finishOfflineOpen(unlockLib);
                } finally {
                  setBusyOrigin(null);
                }
                return;
              }
              await finishOfflineOpen(unlockLib);
            })();
          }}
          onSetupComplete={() => {
            setTick((t) => t + 1);
            toast("Offline unlock ready", "success");
          }}
        />
      )}

      <ConfirmModal
        show={removeLib !== null}
        title="Remove library?"
        body={
          removeLib ? (
            <>
              Remove “{removeLib.name}” from this device? You can add it again later.
            </>
          ) : null
        }
        confirmLabel="Remove"
        variant="danger"
        onCancel={() => setRemoveLib(null)}
        onConfirm={() => {
          if (!removeLib) return;
          const lib = removeLib;
          setRemoveLib(null);
          void leaveLibrary(lib);
        }}
      />
    </div>
  );
}

function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/60 p-4 pt-[max(1rem,env(safe-area-inset-top,0px))] pb-[max(1rem,env(safe-area-inset-bottom,0px))] px-[max(1rem,env(safe-area-inset-left,0px))] sm:pr-[max(1rem,env(safe-area-inset-right,0px))]">
      <div className="w-full max-w-md rounded-xl border border-gray-800 bg-gray-900 p-5 shadow-xl">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-gray-100">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            className="p-1 text-gray-500 hover:text-gray-200"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
