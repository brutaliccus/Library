import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  ExternalLink,
  Headphones,
  Loader2,
  AlertTriangle,
  RefreshCw,
} from "lucide-react";
import api from "../../api/client";
import { useToast } from "../../contexts/ToastContext";

export interface AudibleAuthStatus {
  configured?: boolean;
  reachable?: boolean;
  auth_ok?: boolean;
  active_name?: string;
  activation_bytes_set?: boolean;
  accounts?: Array<{
    user_id?: string;
    flavor_name?: string;
    name?: string;
    marketplace?: string;
    locale_code?: string;
    active?: boolean;
  }>;
  locales?: Record<string, string>;
  auth_file?: string;
  libraforge_accounts_url?: string;
  error?: string;
  status?: "configured" | "not_configured" | "unreachable" | string;
  note?: string;
}

function statusLabel(data?: AudibleAuthStatus | null): {
  text: string;
  className: string;
} {
  if (!data) return { text: "Checking…", className: "text-gray-500" };
  if (data.status === "unreachable" || (data.error && !data.reachable)) {
    return { text: "LibraForge unreachable", className: "text-amber-400" };
  }
  if (data.configured || data.auth_ok) {
    const name = data.active_name ? ` · ${data.active_name}` : "";
    return { text: `Configured${name}`, className: "text-emerald-400" };
  }
  return { text: "Not configured", className: "text-gray-500" };
}

function apiError(err: unknown): string {
  return (
    (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
    (err as Error)?.message ||
    "Request failed"
  );
}

type Props = {
  /** Compact layout for setup wizard */
  compact?: boolean;
  onStatusChange?: (configured: boolean) => void;
};

export default function AudibleAuthPanel({ compact = false, onStatusChange }: Props) {
  const { toast } = useToast();
  const qc = useQueryClient();
  const [locale, setLocale] = useState("us");
  const [flavorName, setFlavorName] = useState("Metadata");
  const [oauthUrl, setOauthUrl] = useState("");
  const [redirectUrl, setRedirectUrl] = useState("");

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["admin-audible-auth"],
    queryFn: async () => {
      const { data: body } = await api.get("/admin/audible-auth");
      return body as AudibleAuthStatus;
    },
    refetchInterval: (query) => (query.state.data?.configured ? false : 15_000),
  });

  useEffect(() => {
    if (data?.locales && !data.locales[locale]) {
      const first = Object.keys(data.locales)[0];
      if (first) setLocale(first);
    }
  }, [data?.locales, locale]);

  useEffect(() => {
    onStatusChange?.(Boolean(data?.configured || data?.auth_ok));
  }, [data?.configured, data?.auth_ok, onStatusChange]);

  const startLogin = useMutation({
    mutationFn: async () => {
      const { data: body } = await api.post("/admin/audible-auth/login/start", {
        locale,
        flavor_name: flavorName.trim() || "Metadata",
      });
      return body as { oauth_url: string };
    },
    onSuccess: (body) => {
      const url = body.oauth_url || "";
      setOauthUrl(url);
      if (url) {
        window.open(url, "_blank", "noopener,noreferrer");
        toast("Amazon sign-in opened — paste the final redirect URL below", "success");
      } else {
        toast("No OAuth URL returned from LibraForge", "error");
      }
    },
    onError: (err) => toast(apiError(err), "error"),
  });

  const completeLogin = useMutation({
    mutationFn: async () => {
      const { data: body } = await api.post("/admin/audible-auth/login/complete", {
        redirect_url: redirectUrl.trim(),
      });
      return body as AudibleAuthStatus & { ok?: boolean };
    },
    onSuccess: async () => {
      setRedirectUrl("");
      setOauthUrl("");
      await qc.invalidateQueries({ queryKey: ["admin-audible-auth"] });
      await qc.invalidateQueries({ queryKey: ["admin-integrations"] });
      await qc.invalidateQueries({ queryKey: ["admin-setup-status"] });
      toast("Audible account connected", "success");
    },
    onError: (err) => toast(apiError(err), "error"),
  });

  const disconnect = useMutation({
    mutationFn: async (force: boolean) => {
      const { data: body } = await api.post("/admin/audible-auth/disconnect", { force });
      return body as AudibleAuthStatus;
    },
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["admin-audible-auth"] });
      await qc.invalidateQueries({ queryKey: ["admin-integrations"] });
      await qc.invalidateQueries({ queryKey: ["admin-setup-status"] });
      toast("Audible account disconnected", "success");
    },
    onError: (err) => {
      const msg = apiError(err);
      if (msg.toLowerCase().includes("deregister")) {
        toast(`${msg} — retry with force if the device is already gone`, "error");
      } else {
        toast(msg, "error");
      }
    },
  });

  const chip = statusLabel(data);
  const locales = data?.locales || { us: "United States" };
  const accountsUrl = data?.libraforge_accounts_url || "";

  return (
    <div className={compact ? "space-y-4" : "space-y-2 text-sm"}>
      <div className="flex items-center justify-between gap-3">
        <span className="text-gray-300 inline-flex items-center gap-2">
          <Headphones size={16} className="text-brand-400 shrink-0" />
          Audible (metadata lookup)
        </span>
        <span className={`inline-flex items-center gap-1.5 ${chip.className}`}>
          {(isLoading || isFetching) && <Loader2 size={12} className="animate-spin" />}
          {chip.text}
        </span>
      </div>

      <p className="text-xs text-gray-500">
        {data?.note ||
          "Signs in through LibraForge and writes an unencrypted auth file at /auth/audible-metadata.json. Prefer a dedicated Audible account for metadata lookups."}
      </p>

      {data?.error && !data.reachable && (
        <div className="rounded-lg border border-amber-800/50 bg-amber-950/30 px-3 py-2 text-xs text-amber-100/90 inline-flex gap-2 items-start">
          <AlertTriangle size={14} className="shrink-0 mt-0.5" />
          <span>
            LibraForge is unreachable — finish the Stack step (or start the libraforge container)
            first. {data.error}
          </span>
        </div>
      )}

      {(data?.configured || data?.auth_ok) && (
        <div className="rounded-lg border border-emerald-800/40 bg-emerald-950/20 px-3 py-2 text-xs text-emerald-100/90 space-y-1">
          <p className="inline-flex items-center gap-1.5 font-medium">
            <CheckCircle2 size={14} className="text-emerald-400" />
            Ready for Metadata Forge / Chapter Forge
          </p>
          {data.active_name && (
            <p className="text-emerald-200/80">Active account: {data.active_name}</p>
          )}
          {(data.accounts || []).length > 1 && (
            <p className="text-gray-400">
              {(data.accounts || []).length} saved accounts — switch in LibraForge Settings →
              Accounts if needed.
            </p>
          )}
        </div>
      )}

      {data?.reachable && !(data.configured || data.auth_ok) && (
        <div className="space-y-3 rounded-lg border border-gray-800 bg-gray-950/40 p-3">
          <p className="text-xs text-gray-400">
            1) Name this account and pick a marketplace → 2) Sign in at Amazon → 3) Paste the
            final redirect URL (address bar after login succeeds).
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            <label className="block space-y-1">
              <span className="text-xs text-gray-500">Account nickname</span>
              <input
                type="text"
                value={flavorName}
                onChange={(e) => setFlavorName(e.target.value)}
                placeholder="Metadata"
                maxLength={80}
                className="w-full px-3 py-1.5 bg-gray-900 border border-gray-700 rounded-lg text-gray-100 text-sm focus:outline-none focus:border-gray-500"
              />
            </label>
            <label className="block space-y-1">
              <span className="text-xs text-gray-500">Country / marketplace</span>
              <select
                value={locale}
                onChange={(e) => setLocale(e.target.value)}
                className="w-full px-3 py-1.5 bg-gray-900 border border-gray-700 rounded-lg text-gray-100 text-sm focus:outline-none focus:border-gray-500"
              >
                {Object.entries(locales).map(([code, label]) => (
                  <option key={code} value={code}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <button
            type="button"
            onClick={() => startLogin.mutate()}
            disabled={startLogin.isPending || !flavorName.trim()}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-emerald-900/50 text-emerald-300 text-sm rounded-lg hover:bg-emerald-900/70 border border-emerald-800/50 disabled:opacity-50"
          >
            {startLogin.isPending ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <ExternalLink size={14} />
            )}
            Sign in to Audible
          </button>
          {oauthUrl && (
            <p className="text-[11px] text-gray-500 break-all">
              If the window was blocked, open{" "}
              <a
                href={oauthUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-brand-400 hover:underline"
              >
                this Amazon login URL
              </a>
              .
            </p>
          )}
          <label className="block space-y-1">
            <span className="text-xs text-gray-500">Paste final Amazon redirect URL</span>
            <textarea
              value={redirectUrl}
              onChange={(e) => setRedirectUrl(e.target.value)}
              rows={compact ? 2 : 3}
              placeholder="https://www.amazon.com/ap/maplanding?…"
              className="w-full px-3 py-1.5 bg-gray-900 border border-gray-700 rounded-lg text-gray-100 text-sm font-mono focus:outline-none focus:border-gray-500"
            />
          </label>
          <button
            type="button"
            onClick={() => completeLogin.mutate()}
            disabled={completeLogin.isPending || !redirectUrl.trim()}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-brand-600 text-white text-sm rounded-lg hover:bg-brand-500 disabled:opacity-50"
          >
            {completeLogin.isPending && <Loader2 size={14} className="animate-spin" />}
            Complete sign-in
          </button>
        </div>
      )}

      <div className="flex flex-wrap gap-2 pt-1">
        <button
          type="button"
          onClick={() => void refetch()}
          className="inline-flex items-center gap-1 px-2.5 py-1 text-xs text-gray-400 hover:text-gray-200 border border-gray-700 rounded-lg"
        >
          <RefreshCw size={12} /> Refresh status
        </button>
        {accountsUrl && (
          <a
            href={accountsUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 px-2.5 py-1 text-xs text-gray-400 hover:text-brand-300 border border-gray-700 rounded-lg"
          >
            <ExternalLink size={12} /> Open LibraForge Accounts
          </a>
        )}
        {(data?.configured || data?.auth_ok) && (
          <>
            <button
              type="button"
              onClick={() => {
                if (window.confirm("Disconnect the active Audible account from LibraForge?")) {
                  disconnect.mutate(false);
                }
              }}
              disabled={disconnect.isPending}
              className="px-2.5 py-1 text-xs text-amber-300/90 hover:text-amber-200 border border-amber-900/50 rounded-lg disabled:opacity-50"
            >
              Disconnect
            </button>
            <button
              type="button"
              onClick={() => {
                if (
                  window.confirm(
                    "Force-remove the local auth file without deregistering the device at Audible?",
                  )
                ) {
                  disconnect.mutate(true);
                }
              }}
              disabled={disconnect.isPending}
              className="px-2.5 py-1 text-xs text-gray-500 hover:text-gray-300 border border-gray-800 rounded-lg disabled:opacity-50"
            >
              Force remove
            </button>
          </>
        )}
      </div>
    </div>
  );
}
