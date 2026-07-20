import { useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import { Mail, Lock } from "lucide-react";
import api from "../api/client";

export default function SetEmail() {
  const { user, isLoading, applyEmailUpdate } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [confirmEmail, setConfirmEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  if (!isLoading && !user) {
    return <Navigate to="/libraries" replace />;
  }
  if (!isLoading && user && !user.mustSetEmail) {
    return <Navigate to={user.mustChangePassword ? "/change-password" : "/"} replace />;
  }
  if (!isLoading && user?.mustChangePassword) {
    return <Navigate to="/change-password" replace />;
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    const em = email.trim().toLowerCase();
    if (!em.includes("@") || !em.includes(".")) {
      setError("Enter a valid email address");
      return;
    }
    if (em !== confirmEmail.trim().toLowerCase()) {
      setError("Emails do not match");
      return;
    }
    if (!password) {
      setError("Enter your password to confirm");
      return;
    }

    setLoading(true);
    try {
      const { data } = await api.post("/auth/set-email", {
        email: em,
        password,
      });
      applyEmailUpdate(data);
      navigate("/", { replace: true });
    } catch (err: any) {
      setError(err.response?.data?.detail || "Could not save email");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-950 p-4">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-14 h-14 bg-brand-600 text-white rounded-2xl mb-4">
            <Mail size={28} />
          </div>
          <h1 className="text-2xl font-bold text-gray-100">Add your email</h1>
          <p className="text-sm text-gray-400 mt-2 leading-relaxed">
            Hi{user?.username ? ` ${user.username}` : ""} — accounts now sign in with email.
            Add yours once; you’ll use it for all future logins. Your username still works
            until you finish this step.
          </p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="bg-gray-800 rounded-2xl shadow-lg border border-gray-700 p-6 space-y-4"
        >
          {error && (
            <div className="p-3 bg-red-900/30 text-red-400 text-sm rounded-lg">{error}</div>
          )}

          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1">Email</label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500"
              required
              autoFocus
              autoComplete="email"
              inputMode="email"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1">
              Confirm email
            </label>
            <input
              type="email"
              value={confirmEmail}
              onChange={(e) => setConfirmEmail(e.target.value)}
              className="w-full px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500"
              required
              autoComplete="email"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1">
              Current password
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500"
              required
              autoComplete="current-password"
            />
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full flex items-center justify-center gap-2 py-2.5 bg-brand-600 text-white font-medium rounded-lg hover:bg-brand-500 disabled:opacity-50 transition-colors"
          >
            <Lock size={16} />
            {loading ? "Saving…" : "Save email & continue"}
          </button>
        </form>
      </div>
    </div>
  );
}
