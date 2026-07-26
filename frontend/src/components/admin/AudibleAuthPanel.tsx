import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  Copy,
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

/** Device OAuth URLs must keep the full query string — Amazon 404s if params are truncated. */
export function isValidAudibleOauthUrl(url: string): boolean {
  const u = (url || "").trim();
  if (!/^https:\/\/www\.(amazon|audible)\./i.test(u)) return false;
  if (!u.includes("/ap/signin?")) return false;
  // Real PKCE URLs have many &-separated openid params; a lone ?param is a broken open.
  return (u.match(/&/g) || []).length >= 5 && u.includes("openid.oa2.code_challenge=");
}

export function redirectHasAuthCode(url: string): boolean {
  const raw = (url || "").trim();
  if (!raw) return false;
  try {
    const parsed = new URL(raw);
    if (parsed.searchParams.get("openid.oa2.authorization_code")) return true;
    // Rare: some browsers put OAuth params in the hash.
    if (parsed.hash && /openid\.oa2\.authorization_code=/.test(parsed.hash)) return true;
  } catch {
    /* fall through — pasted strings are sometimes messy */
  }
  return /[?#&]openid\.oa2\.authorization_code=/.test(raw);
}

/** True when the paste is the OAuth *start* URL (/ap/signin + code_challenge), not the redirect. */
export function looksLikeAudibleOauthStartUrl(url: string): boolean {
  const raw = (url || "").trim();
  if (!raw || redirectHasAuthCode(raw)) return false;
  if (/\/ap\/signin/i.test(raw)) return true;
  if (/openid\.oa2\.code_challenge=/.test(raw)) return true;
  return (
    /openid\.oa2\.response_type=code/.test(raw) &&
    /openid\.return_to=/.test(raw) &&
    !/openid\.oa2\.authorization_code=/.test(raw)
  );
}

/**
 * Human-readable reason the paste cannot complete login, or null if it looks usable.
 * Distinguishes “pasted the login link” from “dog page without auth code”.
 */
export function diagnoseAudibleRedirectPaste(url: string): string | null {
  const raw = (url || "").trim();
  if (!raw) {
    return "Paste the address-bar URL from the Amazon dog / Page Not Found page after you finish signing in.";
  }
  if (redirectHasAuthCode(raw)) return null;
  if (looksLikeAudibleOauthStartUrl(raw)) {
    return (
      "That's the Amazon login page URL (it has code_challenge /ap/signin). " +
      "Open it, finish signing in, then copy the address bar from the dog/Page Not Found page — " +
      "it must include openid.oa2.authorization_code."
    );
  }
  if (/\/ap\/ext\/oauth\/2/i.test(raw)) {
    return (
      "That looks like Amazon's OAuth endpoint path, not the post-login redirect. " +
      "On the dog/Page Not Found page, click the address bar, Select all (Ctrl+A), Copy — " +
      "the URL should be …/ap/maplanding?…&openid.oa2.authorization_code=…"
    );
  }
  if (/\/ap\/maplanding/i.test(raw)) {
    return (
      "That maplanding URL has no openid.oa2.authorization_code. " +
      "Click the address bar and copy the entire URL (Ctrl+L, Ctrl+A, Ctrl+C). " +
      "If there is still no authorization_code, retry Sign in in a private/incognito window."
    );
  }
  return (
    "That URL is missing openid.oa2.authorization_code. After Amazon sign-in, copy the entire " +
    "address bar from the dog/Page Not Found page (usually …/ap/maplanding?…), not the login link " +
    "and not just the page title."
  );
}

/** Open via <a> so the full query string (including &) is preserved. */
function openExternalUrl(url: string): void {
  const a = document.createElement("a");
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  document.body.appendChild(a);
  a.click();
  a.remove();
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
      const url = (body.oauth_url || "").trim();
      setOauthUrl(url);
      if (!url) {
        toast("No OAuth URL returned from LibraForge", "error");
        return;
      }
      if (!isValidAudibleOauthUrl(url)) {
        toast(
          "LibraForge returned a malformed Amazon login URL (query string missing). Retry Sign in, or use Open LibraForge Accounts.",
          "error",
        );
        return;
      }
      openExternalUrl(url);
      toast("Amazon sign-in opened — after login, paste the full address-bar URL below", "success");
    },
    onError: (err) => toast(apiError(err), "error"),
  });

  const completeLogin = useMutation({
    mutationFn: async () => {
      const redirect = redirectUrl.trim();
      const diagnose = diagnoseAudibleRedirectPaste(redirect);
      if (diagnose) throw new Error(diagnose);
      const { data: body } = await api.post("/admin/audible-auth/login/complete", {
        redirect_url: redirect,
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

  const copyOauthUrl = async () => {
    if (!oauthUrl) return;
    try {
      await navigator.clipboard.writeText(oauthUrl);
      toast("Login URL copied", "success");
    } catch {
      toast("Could not copy — select the URL manually", "error");
    }
  };

  const chip = statusLabel(data);
  const locales = data?.locales || { us: "United States" };
  const accountsUrl = data?.libraforge_accounts_url || "";
  const redirectHint = redirectUrl.trim()
    ? diagnoseAudibleRedirectPaste(redirectUrl)
    : null;

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
          <ol className="text-xs text-gray-400 list-decimal list-inside space-y-1">
            <li>Nickname + marketplace → <span className="text-gray-300">Sign in to Audible</span></li>
            <li>
              Sign in on Amazon. When you see the dog /{" "}
              <span className="text-gray-300">Page Not Found</span> screen, that is expected.
            </li>
            <li>
              Copy the <span className="text-gray-300">dog-page address bar</span> (must contain{" "}
              <code className="text-[11px] text-gray-300">openid.oa2.authorization_code</code>) —{" "}
              <span className="text-amber-200/90">not</span> the login link from step 1.
            </li>
          </ol>
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
            <div className="space-y-2 rounded-lg border border-gray-800 bg-gray-900/50 p-2.5">
              <p className="text-xs text-amber-100/90 inline-flex gap-2 items-start">
                <AlertTriangle size={14} className="shrink-0 mt-0.5 text-amber-400" />
                <span>
                  This box is only the <span className="text-amber-50">login link to open</span> — do{" "}
                  <span className="text-amber-50">not</span> paste it into Complete below. After
                  Amazon finishes you should land on{" "}
                  <code className="text-[11px] text-gray-300">/ap/maplanding</code> (dog / Page Not
                  Found). Copy <span className="text-amber-50">that</span> address bar (Ctrl+L,
                  Ctrl+A, Ctrl+C). Example shape:{" "}
                  <code className="text-[11px] text-gray-300 break-all">
                    https://www.amazon.com/ap/maplanding?…&amp;openid.oa2.authorization_code=REDACTED
                  </code>
                  . If you never see a login form, retry in a private window.
                </span>
              </p>
              <label className="block space-y-1">
                <span className="text-xs text-gray-500">
                  Login link only — open in browser (do not paste into Complete)
                </span>
                <textarea
                  readOnly
                  value={oauthUrl}
                  rows={compact ? 2 : 3}
                  className="w-full px-3 py-1.5 bg-gray-950 border border-gray-700 rounded-lg text-gray-300 text-[11px] font-mono focus:outline-none"
                />
              </label>
              <div className="flex flex-wrap gap-2">
                <a
                  href={oauthUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs text-brand-300 hover:text-brand-200 border border-brand-900/50 rounded-lg"
                >
                  <ExternalLink size={12} /> Open login link
                </a>
                <button
                  type="button"
                  onClick={() => void copyOauthUrl()}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs text-gray-300 hover:text-white border border-gray-700 rounded-lg"
                >
                  <Copy size={12} /> Copy login link
                </button>
              </div>
            </div>
          )}
          <label className="block space-y-1">
            <span className="text-xs text-gray-500">
              Complete — paste dog-page address bar (must include authorization_code)
            </span>
            <textarea
              value={redirectUrl}
              onChange={(e) => setRedirectUrl(e.target.value)}
              rows={compact ? 2 : 3}
              placeholder="https://www.amazon.com/ap/maplanding?…&openid.oa2.authorization_code=…"
              className={`w-full px-3 py-1.5 bg-gray-900 border rounded-lg text-gray-100 text-sm font-mono focus:outline-none ${
                redirectHint ? "border-amber-700 focus:border-amber-500" : "border-gray-700 focus:border-gray-500"
              }`}
            />
          </label>
          {redirectHint && (
            <p className="text-xs text-amber-200/90 inline-flex gap-2 items-start">
              <AlertTriangle size={14} className="shrink-0 mt-0.5 text-amber-400" />
              <span>{redirectHint}</span>
            </p>
          )}
          <button
            type="button"
            onClick={() => completeLogin.mutate()}
            disabled={completeLogin.isPending || !redirectUrl.trim() || Boolean(redirectHint)}
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
