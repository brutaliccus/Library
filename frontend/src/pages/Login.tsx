import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import { BookOpen, LogIn } from "lucide-react";
import ServerUrlField, { commitServerUrl } from "../components/ServerUrlField";
import { isNativeApp, needsInstanceUrl, getStoredInstanceUrl } from "../api/instanceUrl";
import { applyApiBaseUrl } from "../api/client";
import api from "../api/client";

export default function Login() {
  const { login, setup, setupRequired, refreshSetupRequired } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [serverUrl, setServerUrl] = useState(() => getStoredInstanceUrl() || "");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      if (isNativeApp()) {
        const saved = commitServerUrl(serverUrl);
        if (!saved) {
          setError("Enter your Library server URL, e.g. https://library.example.com");
          setLoading(false);
          return;
        }
        applyApiBaseUrl();
        await refreshSetupRequired();
      }
      let doSetup = setupRequired;
      if (isNativeApp()) {
        try {
          const { data } = await api.get("/auth/setup-required");
          doSetup = !!data.setup_required;
        } catch {
          // fall back to context flag
        }
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
        await setup(username, password);
        // First admin still needs to create the library (generates invite code).
        navigate("/onboarding?mode=create", { replace: true });
        return;
      }
      await login(username, password);
      navigate("/");
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      const msg =
        detail ||
        (err.code === "ERR_NETWORK"
          ? "Could not reach that server. Check the URL and your network."
          : "Login failed");
      setError(String(msg));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-950 p-4 pt-[calc(1rem+env(safe-area-inset-top,0px))] pb-[calc(1rem+env(safe-area-inset-bottom,0px))]">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-14 h-14 bg-brand-600 text-white rounded-2xl mb-4">
            <BookOpen size={28} />
          </div>
          <h1 className="text-2xl font-bold text-gray-100">Audiobook Library</h1>
          <p className="text-sm text-gray-400 mt-1">
            {needsInstanceUrl()
              ? "Paste an invite link (or your Library URL), then sign in or join"
              : setupRequired
                ? "Create the admin account for this Library server"
                : "Sign in to your library"}
          </p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="bg-gray-800 rounded-2xl shadow-lg border border-gray-700 p-6 space-y-4"
        >
          {error && (
            <div className="p-3 bg-red-900/30 text-red-400 text-sm rounded-lg">
              {error}
            </div>
          )}

          {setupRequired && (
            <p className="text-xs text-gray-400 leading-relaxed">
              First-time setup: create your admin username and password. Next you'll name
              the library and add debrid keys — that generates the invite link friends use
              to join.
            </p>
          )}

          {isNativeApp() && (
            <ServerUrlField
              value={serverUrl}
              onChange={setServerUrl}
              autoFocus={!serverUrl}
            />
          )}

          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1">
              Username
            </label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              required
              autoFocus={!isNativeApp() || !!serverUrl}
              autoComplete="username"
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

        {!setupRequired && (
          <p className="text-center text-sm text-gray-500 mt-4">
            New here?{" "}
            <Link
              to="/join"
              className="text-brand-400 hover:text-brand-300 font-medium"
            >
              Join with an invite link
            </Link>
          </p>
        )}
      </div>
    </div>
  );
}
