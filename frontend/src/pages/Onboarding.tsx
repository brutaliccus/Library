import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import api from "../api/client";
import { useToast } from "../contexts/ToastContext";
import { Library, KeyRound, Users, Loader2, ArrowRight } from "lucide-react";

type Mode = "choose" | "create" | "join";

export default function Onboarding() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [searchParams] = useSearchParams();
  const initialMode = searchParams.get("mode") === "create" ? "create" : "choose";

  const [mode, setMode] = useState<Mode>(initialMode);
  const [busy, setBusy] = useState(false);

  const [name, setName] = useState("");
  const [rdToken, setRdToken] = useState("");
  const [torboxToken, setTorboxToken] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [error, setError] = useState<string | null>(null);

  const finish = async () => {
    await queryClient.invalidateQueries({ queryKey: ["library-group"] });
    await queryClient.invalidateQueries({ queryKey: ["user-settings"] });
    navigate("/", { replace: true });
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
      await api.post("/libraries/create", {
        name: name.trim(),
        real_debrid_api_token: rdToken.trim(),
        torbox_api_token: torboxToken.trim(),
      });
      toast("Library created!", "success");
      await finish();
    } catch (e: any) {
      setError(e.response?.data?.detail || "Failed to create library");
    } finally {
      setBusy(false);
    }
  };

  const handleJoin = async () => {
    setError(null);
    if (!inviteCode.trim()) {
      setError("Enter your invite code");
      return;
    }
    setBusy(true);
    try {
      const { data } = await api.post("/libraries/join", {
        invite_code: inviteCode.trim(),
      });
      toast(`Joined "${data.library?.name || "library"}"!`, "success");
      await finish();
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
            All downloaded books are shared with everyone. This choice only decides whose
            debrid account powers <span className="text-gray-300">your</span> streaming.
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
                  code to share with friends.
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
                <h3 className="font-semibold text-gray-100">Join with an invite code</h3>
                <p className="text-xs text-gray-400 mt-1">
                  Someone shared a code with you? You'll stream using their debrid
                  account — no API keys needed.
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
              <label className="block text-xs font-medium text-gray-400 mb-1.5">Invite code</label>
              <input
                value={inviteCode}
                onChange={(e) => setInviteCode(e.target.value.toUpperCase())}
                placeholder="e.g. 7Q2MKX4RB3ZD"
                className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:border-brand-500 font-mono tracking-widest text-center"
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
