import { useState } from "react";
import { Link } from "react-router-dom";
import { BookOpen, UserPlus, CheckCircle2 } from "lucide-react";
import axios from "axios";

export default function RequestAccount() {
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [token, setToken] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const { data } = await axios.post("/api/auth/request-account", {
        username,
        email: email || null,
        reason: reason || null,
      });
      setToken(data.token);
    } catch (err: any) {
      setError(err.response?.data?.detail || "Failed to submit request");
    } finally {
      setLoading(false);
    }
  };

  if (token) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-950 p-4">
        <div className="w-full max-w-sm text-center">
          <div className="inline-flex items-center justify-center w-14 h-14 bg-green-600 text-white rounded-2xl mb-4">
            <CheckCircle2 size={28} />
          </div>
          <h1 className="text-2xl font-bold text-gray-100 mb-2">Request Submitted</h1>
          <p className="text-gray-400 mb-6">
            Your account request has been sent for review. You'll be notified once it's approved.
          </p>
          <div className="bg-gray-800 rounded-2xl shadow-lg border border-gray-700 p-4 mb-4">
            <p className="text-sm text-gray-400 mb-1">Your status check token:</p>
            <code className="text-sm font-mono text-brand-400 bg-brand-900/30 px-3 py-1.5 rounded-lg block break-all">
              {token}
            </code>
          </div>
          <Link
            to={`/account-status?token=${token}`}
            className="text-brand-400 hover:text-brand-300 text-sm font-medium"
          >
            Check your request status
          </Link>
          <span className="mx-2 text-gray-600">|</span>
          <Link
            to="/login"
            className="text-gray-400 hover:text-gray-300 text-sm font-medium"
          >
            Back to login
          </Link>
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
          <h1 className="text-2xl font-bold text-gray-100">Request an Account</h1>
          <p className="text-sm text-gray-400 mt-1">
            Fill out the form below and an admin will review your request
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
            <label className="block text-sm font-medium text-gray-300 mb-1">
              Desired Username *
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
              Email <span className="text-gray-500">(optional)</span>
            </label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1">
              Why do you want access?{" "}
              <span className="text-gray-500">(optional)</span>
            </label>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              rows={3}
              placeholder="e.g. I'm a friend of ..."
              className="w-full px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent resize-none placeholder:text-gray-500"
            />
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full flex items-center justify-center gap-2 py-2.5 bg-brand-600 text-white font-medium rounded-lg hover:bg-brand-500 disabled:opacity-50 transition-colors"
          >
            <UserPlus size={16} />
            {loading ? "Submitting..." : "Submit Request"}
          </button>
        </form>

        <p className="text-center text-sm text-gray-500 mt-4">
          Already have an account?{" "}
          <Link
            to="/login"
            className="text-brand-400 hover:text-brand-300 font-medium"
          >
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
