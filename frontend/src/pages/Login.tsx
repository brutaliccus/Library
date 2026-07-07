import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import { BookOpen, LogIn } from "lucide-react";

export default function Login() {
  const { login, setup, setupRequired } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      if (setupRequired) {
        await setup(username, password);
      } else {
        await login(username, password);
      }
      navigate("/");
    } catch (err: any) {
      setError(err.response?.data?.detail || "Login failed");
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
            {setupRequired
              ? "Create your admin account to get started"
              : "Sign in to request audiobooks"}
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
              autoFocus
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
            />
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full flex items-center justify-center gap-2 py-2.5 bg-brand-600 text-white font-medium rounded-lg hover:bg-brand-500 disabled:opacity-50 transition-colors"
          >
            <LogIn size={16} />
            {loading
              ? "Please wait..."
              : setupRequired
              ? "Create Admin Account"
              : "Sign In"}
          </button>
        </form>

        {!setupRequired && (
          <p className="text-center text-sm text-gray-500 mt-4">
            Don't have an account?{" "}
            <Link
              to="/request-account"
              className="text-brand-400 hover:text-brand-300 font-medium"
            >
              Request one
            </Link>
          </p>
        )}
      </div>
    </div>
  );
}
