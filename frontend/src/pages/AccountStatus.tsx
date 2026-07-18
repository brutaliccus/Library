import { useState, useEffect } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { BookOpen, Clock, CheckCircle2, XCircle, Search } from "lucide-react";
import api, { applyApiBaseUrl } from "../api/client";
import ServerUrlField, { commitServerUrl } from "../components/ServerUrlField";
import { getStoredInstanceUrl, isNativeApp } from "../api/instanceUrl";

interface StatusData {
  status: string;
  username: string;
  deny_reason?: string | null;
  temp_password?: string | null;
}

export default function AccountStatus() {
  const [searchParams] = useSearchParams();
  const [token, setToken] = useState(searchParams.get("token") || "");
  const [serverUrl, setServerUrl] = useState(() => getStoredInstanceUrl() || "");
  const [data, setData] = useState<StatusData | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const check = async (t: string) => {
    if (!t.trim()) return;
    setError("");
    setLoading(true);
    try {
      if (isNativeApp()) {
        const saved = commitServerUrl(serverUrl);
        if (!saved) {
          setError("Enter your Library server URL first.");
          setLoading(false);
          return;
        }
        applyApiBaseUrl();
      }
      const { data } = await api.get(`/auth/account-status/${t.trim()}`);
      setData(data);
    } catch {
      setError("Request not found. Check your token and try again.");
      setData(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const t = searchParams.get("token");
    if (t) check(t);
  }, [searchParams]);

  const statusDisplay = data && {
    pending: {
      icon: Clock,
      color: "text-yellow-400",
      bg: "bg-yellow-900/30",
      title: "Pending Review",
      message: "Your request is waiting to be reviewed by an admin. Check back later!",
    },
    approved: {
      icon: CheckCircle2,
      color: "text-green-400",
      bg: "bg-green-900/30",
      title: "Approved!",
      message: "Your account has been created. You can now log in.",
    },
    denied: {
      icon: XCircle,
      color: "text-red-400",
      bg: "bg-red-900/30",
      title: "Denied",
      message: data.deny_reason || "Your request was not approved.",
    },
  }[data.status];

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-950 p-4">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-14 h-14 bg-brand-600 text-white rounded-2xl mb-4">
            <BookOpen size={28} />
          </div>
          <h1 className="text-2xl font-bold text-gray-100">Account Request Status</h1>
        </div>

        <div className="bg-gray-800 rounded-2xl shadow-lg border border-gray-700 p-6 space-y-4">
          {isNativeApp() && (
            <ServerUrlField value={serverUrl} onChange={setServerUrl} />
          )}
          <div className="flex gap-2">
            <input
              type="text"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="Paste your token here"
              className="flex-1 px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent text-sm font-mono"
              onKeyDown={(e) => e.key === "Enter" && check(token)}
            />
            <button
              onClick={() => check(token)}
              disabled={loading}
              className="px-4 py-2.5 bg-brand-600 text-white rounded-lg hover:bg-brand-500 disabled:opacity-50 transition-colors"
            >
              <Search size={16} />
            </button>
          </div>

          {error && (
            <div className="p-3 bg-red-900/30 text-red-400 text-sm rounded-lg">{error}</div>
          )}

          {data && statusDisplay && (
            <div className={`p-4 rounded-xl ${statusDisplay.bg}`}>
              <div className="flex items-center gap-2 mb-2">
                <statusDisplay.icon size={20} className={statusDisplay.color} />
                <h3 className={`font-semibold ${statusDisplay.color}`}>
                  {statusDisplay.title}
                </h3>
              </div>
              <p className="text-sm text-gray-300">{statusDisplay.message}</p>

              {data.status === "approved" && (
                <div className="mt-3 p-3 bg-gray-900 rounded-lg border border-green-800">
                  <p className="text-xs text-gray-400 mb-1">Your default password:</p>
                  <code className="text-sm font-mono text-brand-400">changeme</code>
                  <p className="text-xs text-gray-500 mt-2">
                    You'll be prompted to change this on your first login.
                  </p>
                </div>
              )}
            </div>
          )}
        </div>

        <p className="text-center text-sm text-gray-500 mt-4">
          <Link
            to="/login"
            className="text-brand-400 hover:text-brand-300 font-medium"
          >
            Back to login
          </Link>
        </p>
      </div>
    </div>
  );
}
