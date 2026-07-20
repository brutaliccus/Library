import { useState } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import { BookOpen, LogIn } from "lucide-react";
import {
  getStoredInstanceUrl,
  isNativeApp,
  needsInstanceUrl,
  setInstanceUrl,
  normalizeInstanceUrl,
} from "../api/instanceUrl";
import { applyApiBaseUrl } from "../api/client";
import api from "../api/client";
import { listRememberedLibraries } from "../api/libraryRegistry";

export default function Login() {
  const { login, setup, setupRequired, refreshSetupRequired, user, isLoading } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const originParam = searchParams.get("origin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [serverUrl, setServerUrl] = useState(
    () => originParam || getStoredInstanceUrl() || ""
  );
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const hasLibraries = listRememberedLibraries().length > 0;

  // Native with no server and no remembered libraries → libraries picker (add via invite).
  if (!isLoading && needsInstanceUrl() && !originParam && !hasLibraries) {
    return <Navigate to="/libraries" replace />;
  }

  if (
    !isLoading &&
    user &&
    !user.mustChangePassword &&
    !user.mustSetEmail &&
    getStoredInstanceUrl()
  ) {
    return <Navigate to="/" replace />;
  }
  if (!isLoading && user?.mustChangePassword) {
    return <Navigate to="/change-password" replace />;
  }
  if (!isLoading && user?.mustSetEmail) {
    return <Navigate to="/set-email" replace />;
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      let origin = getStoredInstanceUrl();
      if (isNativeApp() || originParam) {
        const raw = serverUrl.trim() || originParam || "";
        const normalized = normalizeInstanceUrl(raw);
        if (!normalized) {
          setError("Enter your Library server URL, or join with an invite link instead");
          setLoading(false);
          return;
        }
        setInstanceUrl(normalized);
        applyApiBaseUrl();
        origin = normalized;
      }
      let doSetup = setupRequired;
      try {
        const { data } = await api.get("/auth/setup-required");
        doSetup = !!data.setup_required;
        await refreshSetupRequired();
      } catch {
        /* use context */
      }
      if (doSetup) {
        if (password.length < 6) {
          setError("Password must be at least 6 characters");
          setLoading(false);
          return;
        }
        if (password !== confirmPassword) {
          setError("Passwords do not match");
          setLoading(false);
          return;
        }
        await setup(email.trim(), password, origin || undefined);
        navigate("/onboarding?mode=create", { replace: true });
        return;
      }
      await login(email.trim(), password, origin || undefined);
      navigate("/", { replace: true });
    } catch (err: any) {
      setError(
        err.response?.data?.detail ||
          (err.code === "ERR_NETWORK"
            ? "Could not reach the library server"
            : "Sign in failed")
      );
    } finally {
      setLoading(false);
    }
  };

  const showServerField = isNativeApp() || !!originParam;

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-950 p-4">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <div className="inline-flex p-3 bg-brand-900/40 rounded-2xl mb-4">
            <BookOpen size={32} className="text-brand-400" />
          </div>
          <h1 className="text-2xl font-bold text-gray-100">
            {setupRequired ? "Create admin account" : "Sign in"}
          </h1>
          <p className="text-sm text-gray-400 mt-2">
            {setupRequired
              ? "First-time setup for this library server."
              : "Sign in to this library with your email."}
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {error && (
            <div className="p-3 bg-red-900/30 text-red-400 text-sm rounded-lg">{error}</div>
          )}

          {showServerField && (
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1">
                Library URL
              </label>
              <input
                type="url"
                value={serverUrl}
                onChange={(e) => setServerUrl(e.target.value)}
                placeholder="https://library.example.com"
                className="w-full px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500"
                autoComplete="url"
              />
              <p className="text-[11px] text-gray-500 mt-1">
                Prefer an invite link?{" "}
                <Link to="/libraries" className="text-brand-400 hover:text-brand-300">
                  Add from Libraries
                </Link>
              </p>
            </div>
          )}

          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1">
              Email or username
            </label>
            <input
              type="text"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              required
              autoFocus={!showServerField}
              autoComplete="username"
              inputMode="email"
            />
            <p className="text-[11px] text-gray-500 mt-1">
              Older accounts may still use a username. New accounts use email.
            </p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1">Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              required
              minLength={setupRequired ? 6 : undefined}
              autoComplete={setupRequired ? "new-password" : "current-password"}
            />
          </div>

          {setupRequired && (
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1">
                Confirm password
              </label>
              <input
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className="w-full px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                required
                minLength={6}
                autoComplete="new-password"
              />
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full flex items-center justify-center gap-2 py-2.5 bg-brand-600 text-white font-medium rounded-lg hover:bg-brand-500 disabled:opacity-50 transition-colors"
          >
            <LogIn size={16} />
            {loading
              ? "Please wait..."
              : setupRequired
                ? "Create admin & continue"
                : "Sign In"}
          </button>
        </form>

        <p className="text-center text-sm text-gray-500 mt-4 space-y-1">
          <span className="block">
            <Link to="/libraries" className="text-brand-400 hover:text-brand-300 font-medium">
              Your libraries
            </Link>
          </span>
          {!setupRequired && (
            <span className="block">
              New here?{" "}
              <Link to="/join" className="text-brand-400 hover:text-brand-300 font-medium">
                Join with an invite link
              </Link>
            </span>
          )}
        </p>
      </div>
    </div>
  );
}
