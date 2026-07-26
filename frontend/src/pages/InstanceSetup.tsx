import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  CheckCircle2,
  Circle,
  ArrowRight,
  ArrowLeft,
  Shield,
  AlertTriangle,
  Sparkles,
} from "lucide-react";
import api from "../api/client";
import { useToast } from "../contexts/ToastContext";

interface SetupStatus {
  complete: boolean;
  steps: Array<{
    id: string;
    label: string;
    done: boolean;
    required: boolean;
    help: string;
    abbRssOnly?: boolean;
    knabenRssOnly?: boolean;
  }>;
  defaults: {
    abbRssOnly: boolean;
    knabenRssOnly: boolean;
    abbAuthorCrawl: boolean;
    abbLiveSearch: boolean;
  };
}

interface ConfigSetting {
  key: string;
  group: string;
  label: string;
  valueType: string;
  secret: boolean;
  help: string;
  placeholder: string;
  value: string;
  hint: string;
  configured: boolean;
}

const STEP_GROUPS: Record<string, string[]> = {
  libraries: ["libraries"],
  indexers: ["indexers"],
  debrid: ["debrid"],
  pipeline: ["pipeline"],
  catalog: ["catalog"],
  scraper: ["scraper"],
  mobile: ["mobile"],
};

const FOLDER_CHECKS: Array<{ title: string; detail: string }> = [
  {
    title: "ABS ignores `.unorganized`",
    detail:
      "Audiobook staging is `/audiobooks/.unorganized/` (dot folder). ABS skips hidden dirs by default; a `.ignore` marker is also written.",
  },
  {
    title: "Kavita excludes `unorganized`",
    detail:
      "Ebook staging is `/ebooks/unorganized/` (non-dot). Add `unorganized` to Kavita’s library ignore/exclude list so quarantine drops never appear as series.",
  },
  {
    title: "M4B global encode queue",
    detail:
      "Library Site runs one M4B encode at a time (auto-forge + Quick Review share the slot). Request cards show Queued for M4B while waiting, Converting M4B when active. LIBRAFORGE_M4B_JOBS=1 is per-run workers — keep at 1 on a Pi.",
  },
  {
    title: "PUID 1000 for shared media",
    detail:
      "App and LibraForge should share UID 1000 (see PUID/PGID) so M4B / Folder Forge can write under staging and library folders.",
  },
  {
    title: "Browser extension (optional)",
    detail:
      "Load unpacked from `browser-extension/` to right-click magnets into the request queue. See browser-extension/README.md.",
  },
  {
    title: "Android APK releases",
    detail:
      "Friends install the prebuilt APK from GitHub Releases (owner/repo on the next step). Offline unlock, Downloads tab, Android Auto ±15s / idle resume.",
  },
];

export default function InstanceSetup() {
  const navigate = useNavigate();
  const { toast } = useToast();
  const qc = useQueryClient();
  const [stepIdx, setStepIdx] = useState(0);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [abbRss, setAbbRss] = useState(true);
  const [knabenRss, setKnabenRss] = useState(true);
  const [enableDeep, setEnableDeep] = useState(false);

  const { data: status, refetch: refetchStatus } = useQuery({
    queryKey: ["admin-setup-status"],
    queryFn: async () => {
      const { data } = await api.get("/admin/setup-status");
      return data as SetupStatus;
    },
  });

  const { data: config } = useQuery({
    queryKey: ["admin-config"],
    queryFn: async () => {
      const { data } = await api.get("/admin/config");
      return data as { settings: ConfigSetting[] };
    },
  });

  const steps = status?.steps || [];
  const step = steps[stepIdx];
  const groupIds = step ? STEP_GROUPS[step.id] || [] : [];
  const fields = useMemo(() => {
    const all = config?.settings || [];
    return all.filter((s) => groupIds.includes(s.group) && s.key !== "config.scraper_enabled");
  }, [config, groupIds]);

  const save = useMutation({
    mutationFn: async (updates: Record<string, string>) => {
      await api.put("/admin/config", { settings: updates });
    },
    onSuccess: async () => {
      await refetchStatus();
      void qc.invalidateQueries({ queryKey: ["admin-config"] });
      toast("Saved", "success");
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Save failed";
      toast(String(msg), "error");
    },
  });

  const applyDefaults = useMutation({
    mutationFn: async () => {
      const { data } = await api.post("/admin/setup-defaults");
      return data as SetupStatus;
    },
    onSuccess: async () => {
      await refetchStatus();
      toast("RSS-only defaults applied", "success");
    },
  });

  const saveStep = async () => {
    if (step?.id === "scraper") {
      const updates: Record<string, string> = {
        "scraper.abb_rss_only": abbRss ? "true" : "false",
        "scraper.knaben_rss_only": knabenRss ? "true" : "false",
        "config.abb_author_crawl_enabled": enableDeep && !abbRss ? "true" : "false",
        "config.abb_live_search_enabled": "false",
      };
      await save.mutateAsync(updates);
      return;
    }
    if (step?.id === "folders") {
      return;
    }
    const updates: Record<string, string> = {};
    for (const f of fields) {
      if (drafts[f.key] === undefined) continue;
      // Bools may be "false"; other fields skip empty (leave existing / env).
      if (f.valueType !== "bool" && drafts[f.key] === "") continue;
      updates[f.key] = drafts[f.key];
    }
    if (Object.keys(updates).length) {
      await save.mutateAsync(updates);
    }
  };

  const next = async () => {
    await saveStep();
    if (stepIdx < steps.length - 1) setStepIdx((i) => i + 1);
    else navigate("/admin?tab=config");
  };

  const renderFields = () => (
    <div className="space-y-3">
      {fields.map((f) => {
        const isBool = f.valueType === "bool";
        return (
          <label key={f.key} className="block space-y-1">
            <span className="text-sm text-gray-200">{f.label}</span>
            {f.help && <span className="block text-xs text-gray-500">{f.help}</span>}
            {isBool ? (
              <span className="inline-flex items-center gap-2 text-sm text-gray-300">
                <input
                  type="checkbox"
                  checked={(drafts[f.key] ?? f.value) === "true"}
                  onChange={(e) =>
                    setDrafts((d) => ({ ...d, [f.key]: e.target.checked ? "true" : "false" }))
                  }
                  className="rounded border-gray-600 bg-gray-800"
                />
                Enabled
              </span>
            ) : (
              <input
                type={f.secret ? "password" : "text"}
                placeholder={
                  f.configured && f.secret
                    ? `Configured · ${f.hint} — enter to replace`
                    : f.placeholder || ""
                }
                value={drafts[f.key] ?? ""}
                onChange={(e) => setDrafts((d) => ({ ...d, [f.key]: e.target.value }))}
                className="w-full px-3 py-2 rounded-lg bg-gray-950 border border-gray-700 text-sm text-gray-100"
              />
            )}
          </label>
        );
      })}
      {fields.length === 0 && (
        <p className="text-sm text-gray-500">No settings for this step — continue when ready.</p>
      )}
    </div>
  );

  return (
    <div className="max-w-2xl mx-auto px-4 py-10">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-gray-100 flex items-center gap-2">
          <Shield size={22} className="text-brand-400" />
          Instance setup
        </h1>
        <p className="text-sm text-gray-500 mt-2">
          Configure libraries, debrid, download pipelines, and scrapers. Change anything later in
          Admin → Config.
        </p>
      </div>

      <ol className="space-y-2 mb-8">
        {steps.map((s, i) => (
          <li key={s.id}>
            <button
              type="button"
              onClick={() => setStepIdx(i)}
              className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left text-sm ${
                i === stepIdx ? "bg-gray-800 text-gray-100" : "text-gray-400 hover:bg-gray-900"
              }`}
            >
              {s.done ? (
                <CheckCircle2 size={16} className="text-emerald-400 shrink-0" />
              ) : (
                <Circle size={16} className="text-gray-600 shrink-0" />
              )}
              <span className="flex-1">{s.label}</span>
              {s.required && (
                <span className="text-[10px] uppercase tracking-wide text-amber-500">required</span>
              )}
            </button>
          </li>
        ))}
      </ol>

      {step && (
        <div className="rounded-xl border border-gray-800 bg-gray-900/40 p-5 space-y-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-100">{step.label}</h2>
            <p className="text-sm text-gray-500 mt-1">{step.help}</p>
          </div>

          {step.id === "scraper" ? (
            <div className="space-y-4">
              <button
                type="button"
                onClick={() => applyDefaults.mutate()}
                className="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-emerald-900/30 border border-emerald-700/50 text-emerald-300 text-sm hover:bg-emerald-900/50"
              >
                <Sparkles size={14} />
                Use recommended defaults (RSS-only)
              </button>

              <label className="flex items-start gap-3 text-sm text-gray-200">
                <input
                  type="checkbox"
                  checked={abbRss}
                  onChange={(e) => {
                    setAbbRss(e.target.checked);
                    if (e.target.checked) setEnableDeep(false);
                  }}
                  className="mt-1"
                />
                <span>
                  <strong>ABB RSS-only</strong> (recommended)
                  <span className="block text-xs text-gray-500">
                    No FlareSolverr author crawl. Live Jackett search still works.
                  </span>
                </span>
              </label>

              <label className="flex items-start gap-3 text-sm text-gray-200">
                <input
                  type="checkbox"
                  checked={knabenRss}
                  onChange={(e) => setKnabenRss(e.target.checked)}
                  className="mt-1"
                />
                <span>
                  <strong>Knaben RSS-only</strong> (recommended)
                  <span className="block text-xs text-gray-500">
                    Skip full category crawl — RSS polls only.
                  </span>
                </span>
              </label>

              <label className="flex items-start gap-3 text-sm text-amber-200/90">
                <input
                  type="checkbox"
                  checked={enableDeep}
                  disabled={abbRss}
                  onChange={(e) => setEnableDeep(e.target.checked)}
                  className="mt-1"
                />
                <span>
                  <span className="inline-flex items-center gap-1 font-medium">
                    <AlertTriangle size={14} />
                    Enable ABB deep author crawl
                  </span>
                  <span className="block text-xs text-amber-500/80">
                    HIGH USAGE on a Pi — requires FlareSolverr and turns off RSS-only for ABB.
                  </span>
                </span>
              </label>
            </div>
          ) : step.id === "folders" ? (
            <ul className="space-y-3">
              {FOLDER_CHECKS.map((item) => (
                <li
                  key={item.title}
                  className="rounded-lg border border-gray-800 bg-gray-950/50 px-3 py-2.5"
                >
                  <p className="text-sm font-medium text-gray-100">{item.title}</p>
                  <p className="text-xs text-gray-500 mt-1">{item.detail}</p>
                </li>
              ))}
            </ul>
          ) : (
            renderFields()
          )}

          <div className="flex justify-between pt-2">
            <button
              type="button"
              disabled={stepIdx === 0}
              onClick={() => setStepIdx((i) => Math.max(0, i - 1))}
              className="inline-flex items-center gap-1 text-sm text-gray-400 hover:text-gray-200 disabled:opacity-30"
            >
              <ArrowLeft size={14} /> Back
            </button>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => navigate("/admin?tab=config")}
                className="px-3 py-2 text-sm text-gray-400 hover:text-gray-200"
              >
                Skip to Config
              </button>
              <button
                type="button"
                onClick={() => void next()}
                disabled={save.isPending}
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-brand-600 text-white text-sm font-medium hover:bg-brand-500 disabled:opacity-50"
              >
                {stepIdx >= steps.length - 1 ? "Finish" : "Save & continue"}
                <ArrowRight size={14} />
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
