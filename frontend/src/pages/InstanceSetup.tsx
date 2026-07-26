import { useEffect, useMemo, useState } from "react";
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
  Database,
  CalendarClock,
  Loader2,
} from "lucide-react";
import api from "../api/client";
import { useToast } from "../contexts/ToastContext";
import AudibleAuthPanel from "../components/admin/AudibleAuthPanel";

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
    audibleConfigured?: boolean;
    audibleReachable?: boolean;
    audibleName?: string;
  }>;
  defaults: {
    abbRssOnly: boolean;
    knabenRssOnly: boolean;
    abbAuthorCrawl: boolean;
    abbLiveSearch: boolean;
    libraforgePipelineEnabled?: boolean;
    ebookPipelineEnabled?: boolean;
  };
  presets?: Record<string, Record<string, string>>;
  stack?: {
    absConfigured?: boolean;
    kavitaConfigured?: boolean;
    libraforgeConfigured?: boolean;
    libraforgePipelineEnabled?: boolean;
    bundledMedia?: boolean;
    bundledReady?: boolean;
  };
  audible?: {
    configured?: boolean;
    reachable?: boolean;
    activeName?: string;
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

interface SetupValidateResult {
  ok: boolean;
  warnings: string[];
  probes: Record<string, { configured?: boolean; connected?: boolean; error?: string }>;
  bundledMedia?: boolean;
  bundledReady?: boolean;
}

interface OlCatalogStatus {
  status?: string;
  catalog_ready?: boolean;
  dumps_present?: boolean;
  warnings?: string[];
  scheduled_build_at?: string | null;
  new_dumps_available?: boolean;
}

const STEP_GROUPS: Record<string, string[]> = {
  stack: ["libraries", "pipeline"],
  audible: [],
  indexers: ["indexers"],
  debrid: ["debrid"],
  folders: ["storage"],
  openlibrary: [],
  catalog: ["catalog"],
  scraper: ["scraper"],
  mobile: ["mobile"],
};

/** Stack fields shown first; remaining pipeline knobs stay editable below. */
const STACK_PRIMARY_KEYS = [
  "config.abs_url",
  "config.abs_api_key",
  "config.abs_library_id",
  "config.kavita_url",
  "config.kavita_api_key",
  "config.kavita_library_id",
  "config.libraforge_url",
  "config.libraforge_internal_url",
  "config.libraforge_pipeline_enabled",
  "config.ebook_pipeline_enabled",
];

const FOLDER_CHECKS: Array<{ title: string; detail: string }> = [
  {
    title: "ABS ignores audiobook staging",
    detail:
      "Default staging is `/audiobooks/.unorganized/` (dot folder). ABS skips hidden dirs by default; a `.ignore` marker is also written. Names/paths are editable under Admin → Settings → Storage / Paths.",
  },
  {
    title: "Kavita excludes ebook staging",
    detail:
      "Default staging is `/ebooks/unorganized/` (non-dot). Add that folder name to Kavita’s library ignore/exclude list so quarantine drops never appear as series. Override under Config → Storage / Paths.",
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

function toLocalDatetimeInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function defaultScheduleLocalValue(): string {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(0, 0, 0, 0);
  return toLocalDatetimeInput(d);
}

function localInputToIsoUtc(localValue: string): string {
  const d = new Date(localValue);
  if (Number.isNaN(d.getTime())) {
    throw new Error("Invalid date/time");
  }
  return d.toISOString();
}

function detectPlatformPreset(
  status?: SetupStatus | null,
): "bundled_media" | "windows_docker" | "linux_docker" {
  if (status?.stack?.bundledMedia || status?.stack?.bundledReady) {
    return "bundled_media";
  }
  if (typeof navigator !== "undefined" && /Win/i.test(navigator.platform || navigator.userAgent)) {
    return "windows_docker";
  }
  return "linux_docker";
}

export default function InstanceSetup() {
  const navigate = useNavigate();
  const { toast } = useToast();
  const qc = useQueryClient();
  const [stepIdx, setStepIdx] = useState(0);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [abbRss, setAbbRss] = useState(true);
  const [knabenRss, setKnabenRss] = useState(true);
  const [enableDeep, setEnableDeep] = useState(false);
  const [probeWarnings, setProbeWarnings] = useState<string[]>([]);
  const [validating, setValidating] = useState(false);
  const [presetApplied, setPresetApplied] = useState(false);
  const [olChoice, setOlChoice] = useState<"skip" | "now" | "schedule">("skip");
  const [scheduleLocal, setScheduleLocal] = useState(defaultScheduleLocalValue);
  const [showStackAdvanced, setShowStackAdvanced] = useState(false);
  const [liveProbes, setLiveProbes] = useState<SetupValidateResult["probes"] | null>(null);

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

  const { data: olStatus, isLoading: olLoading } = useQuery({
    queryKey: ["admin-ol-catalog"],
    queryFn: async () => {
      const { data } = await api.get("/admin/ol-catalog");
      return data as OlCatalogStatus;
    },
    enabled: status?.steps?.[stepIdx]?.id === "openlibrary",
  });

  const steps = status?.steps || [];
  const step = steps[stepIdx];
  const groupIds = step ? STEP_GROUPS[step.id] || [] : [];
  const fields = useMemo(() => {
    const all = config?.settings || [];
    const filtered = all.filter((s) => groupIds.includes(s.group) && s.key !== "config.scraper_enabled");
    if (step?.id !== "stack") return filtered;
    const rank = (key: string) => {
      const i = STACK_PRIMARY_KEYS.indexOf(key);
      return i === -1 ? 1000 : i;
    };
    return [...filtered].sort((a, b) => rank(a.key) - rank(b.key));
  }, [config, groupIds, step?.id]);

  const bundledReady = !!status?.stack?.bundledReady;
  const bundledMedia = !!status?.stack?.bundledMedia || bundledReady;

  // Pre-fill empty stack drafts from the detected platform preset once.
  // Bundled installs already have keys in env — skip forcing host.docker.internal.
  useEffect(() => {
    if (presetApplied || !status?.presets || !config?.settings || step?.id !== "stack") return;
    const presetKey = detectPlatformPreset(status);
    const preset = status.presets[presetKey];
    if (!preset) return;
    const next: Record<string, string> = {};
    for (const [key, val] of Object.entries(preset)) {
      if (key === "label") continue;
      const existing = config.settings.find((s) => s.key === key);
      const configured = existing?.configured && existing.valueType !== "bool";
      const draftEmpty = drafts[key] === undefined || drafts[key] === "";
      if (!configured && draftEmpty && val) {
        next[key] = val;
      }
    }
    // Bool pipeline defaults when not yet overridden in drafts.
    for (const boolKey of ["config.libraforge_pipeline_enabled", "config.ebook_pipeline_enabled"]) {
      if (drafts[boolKey] !== undefined) continue;
      if (preset[boolKey]) next[boolKey] = preset[boolKey];
    }
    if (Object.keys(next).length) {
      setDrafts((d) => ({ ...next, ...d }));
    }
    setPresetApplied(true);
    // Bundled + keys present → keep advanced collapsed by default.
    if (status.stack?.bundledReady) {
      setShowStackAdvanced(false);
    }
  }, [status, config, step?.id, presetApplied, drafts]);

  // Soft probe when opening the stack step so bundled installs can show green status.
  useEffect(() => {
    if (step?.id !== "stack") return;
    let cancelled = false;
    (async () => {
      try {
        const { data } = await api.post("/admin/setup-validate");
        if (!cancelled) setLiveProbes((data as SetupValidateResult).probes || null);
      } catch {
        if (!cancelled) setLiveProbes(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [step?.id]);

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

  const olBuild = useMutation({
    mutationFn: async () => {
      const { data } = await api.post("/admin/ol-catalog/build", {
        include_editions: false,
        force_download: true,
      });
      return data;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin-ol-catalog"] });
      toast("Open Library catalog build started", "success");
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Could not start catalog build";
      toast(String(msg), "error");
    },
  });

  const olSchedule = useMutation({
    mutationFn: async (localValue: string) => {
      const { data } = await api.post("/admin/ol-catalog/schedule", {
        scheduled_at: localInputToIsoUtc(localValue),
        include_editions: false,
        force_download: true,
      });
      return data;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin-ol-catalog"] });
      toast("Catalog build scheduled", "success");
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Could not schedule catalog build";
      toast(String(msg), "error");
    },
  });

  const applyPreset = (presetKey: string) => {
    const preset = status?.presets?.[presetKey];
    if (!preset) return;
    const next: Record<string, string> = {};
    for (const [key, val] of Object.entries(preset)) {
      if (key === "label") continue;
      next[key] = val;
    }
    setDrafts((d) => ({ ...d, ...next }));
    toast(`Applied ${preset.label || presetKey} defaults`, "success");
  };

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
    if (step?.id === "folders" || step?.id === "openlibrary" || step?.id === "audible") {
      return;
    }
    const updates: Record<string, string> = {};
    for (const f of fields) {
      if (drafts[f.key] === undefined) continue;
      if (f.valueType !== "bool" && drafts[f.key] === "") continue;
      updates[f.key] = drafts[f.key];
    }
    if (Object.keys(updates).length) {
      await save.mutateAsync(updates);
    }
  };

  const runSoftValidate = async (): Promise<string[]> => {
    try {
      const { data } = await api.post("/admin/setup-validate");
      const result = data as SetupValidateResult;
      setLiveProbes(result.probes || null);
      // Bundled + keys present: only surface hard miss (neither ABS nor Kavita configured).
      if (result.bundledReady || (bundledReady && !(result.warnings || []).some((w) => w.includes("Configure at least")))) {
        const soft = (result.warnings || []).filter((w) => !w.includes("still warming"));
        return soft;
      }
      return result.warnings || [];
    } catch {
      return ["Health probe request failed — you can still continue and fix connections later."];
    }
  };

  const handleOpenLibraryContinue = async () => {
    if (olChoice === "now") {
      await olBuild.mutateAsync();
    } else if (olChoice === "schedule") {
      await olSchedule.mutateAsync(scheduleLocal);
    }
    // skip / after action — never blocks finishing
  };

  const next = async () => {
    setValidating(true);
    setProbeWarnings([]);
    try {
      await saveStep();
      if (step?.id === "stack" || step?.id === "indexers") {
        const warnings = await runSoftValidate();
        setProbeWarnings(warnings);
        if (warnings.length) {
          // Soft: bundled installs should not feel blocked by warming probes.
          const tone = bundledReady || bundledMedia ? "success" : "error";
          toast(
            bundledReady || bundledMedia
              ? "Saved — you can continue (soft connection notes below)"
              : "Saved with connection warnings — you can continue",
            tone === "success" ? "success" : "error",
          );
        }
      }
      if (step?.id === "openlibrary") {
        await handleOpenLibraryContinue();
      }
      if (stepIdx < steps.length - 1) setStepIdx((i) => i + 1);
      else navigate("/libraries");
    } finally {
      setValidating(false);
    }
  };

  const fieldValue = (f: ConfigSetting) => {
    if (drafts[f.key] !== undefined) return drafts[f.key];
    if (f.valueType === "bool") return f.value || "false";
    return "";
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
                  checked={fieldValue(f) === "true"}
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

  const probeTone = (name: string) => {
    const p = liveProbes?.[name];
    if (!p) return "text-gray-500";
    if (p.connected) return "text-emerald-400";
    if (p.configured) return "text-amber-400";
    return "text-gray-500";
  };

  const renderStack = () => (
    <div className="space-y-4">
      {(bundledReady || bundledMedia) && (
        <div className="rounded-lg border border-emerald-800/50 bg-emerald-950/25 p-3 space-y-2">
          <p className="text-sm text-emerald-200 inline-flex items-center gap-1.5 font-medium">
            <CheckCircle2 size={16} className="text-emerald-400" />
            Using bundled stack
          </p>
          <p className="text-xs text-gray-400">
            Audiobookshelf, Kavita, and LibraForge share this compose network
            (<code className="text-gray-300"> (profile bundled-media)</code>. API keys were
            bootstrapped into <code className="text-gray-300">.env</code> — no manual entry needed.
            Continue when probes look good.
          </p>
          <ul className="text-xs space-y-1 font-mono">
            <li className={probeTone("audiobookshelf")}>
              ABS {liveProbes?.audiobookshelf?.connected ? "connected" : liveProbes?.audiobookshelf?.configured ? "warming…" : "—"}
              {" · "}http://audiobookshelf:80 → host :13378
            </li>
            <li className={probeTone("kavita")}>
              Kavita {liveProbes?.kavita?.connected ? "connected" : liveProbes?.kavita?.configured ? "warming…" : "—"}
              {" · "}http://kavita:5000 → host :5000
            </li>
            <li className={probeTone("libraforge")}>
              LibraForge {liveProbes?.libraforge?.connected ? "connected" : liveProbes?.libraforge?.configured ? "warming…" : "—"}
              {" · "}http://libraforge:5056 → host :5056
            </li>
          </ul>
          {bundledReady && (
            <p className="text-[11px] text-emerald-500/90">
              Keys present — you can Continue without editing fields below.
            </p>
          )}
        </div>
      )}

      {!bundledMedia && (
        <div className="rounded-lg border border-gray-800 bg-gray-950/50 p-3 space-y-2">
          <p className="text-xs font-medium text-gray-300">Platform preset (editable after apply)</p>
          <div className="flex flex-wrap gap-2">
            {Object.entries(status?.presets || {}).map(([key, preset]) => (
              <button
                key={key}
                type="button"
                onClick={() => applyPreset(key)}
                className="px-3 py-1.5 rounded-lg border border-gray-700 text-xs text-gray-200 hover:border-brand-500 hover:text-brand-300"
              >
                {preset.label || key}
              </button>
            ))}
          </div>
          <p className="text-[11px] text-gray-500">
            Prefer <strong className="font-medium text-gray-400">Bundled media</strong> for new
            installs (<code className="text-gray-400">COMPOSE_PROFILES=bundled-media</code>). External
            Windows uses <code className="text-gray-400">host.docker.internal</code>; Linux/Pi bridge{" "}
            <code className="text-gray-400">172.17.0.1</code>. Soft warnings only — you can continue.
          </p>
        </div>
      )}

      {(bundledReady || bundledMedia) && (
        <button
          type="button"
          onClick={() => setShowStackAdvanced((v) => !v)}
          className="text-xs text-gray-400 hover:text-gray-200 underline-offset-2 hover:underline"
        >
          {showStackAdvanced ? "Hide advanced URL / key overrides" : "Advanced: override URLs / keys"}
        </button>
      )}

      {(!bundledReady && !bundledMedia) || showStackAdvanced ? (
        <>
          {bundledMedia && (
            <div className="rounded-lg border border-gray-800 bg-gray-950/50 p-3 space-y-2">
              <p className="text-xs font-medium text-gray-300">Presets</p>
              <div className="flex flex-wrap gap-2">
                {Object.entries(status?.presets || {}).map(([key, preset]) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => applyPreset(key)}
                    className="px-3 py-1.5 rounded-lg border border-gray-700 text-xs text-gray-200 hover:border-brand-500 hover:text-brand-300"
                  >
                    {preset.label || key}
                  </button>
                ))}
              </div>
            </div>
          )}
          {renderFields()}
        </>
      ) : (
        <p className="text-sm text-gray-500">
          Pipeline toggles and connection details are already set from install. Open Advanced only if
          you need to point at an external ABS/Kavita/LibraForge.
        </p>
      )}
    </div>
  );

  const renderOpenLibrary = () => (
    <div className="space-y-4">
      <div className="rounded-lg border border-amber-900/40 bg-amber-950/20 p-3 space-y-1">
        <p className="text-sm text-amber-100 inline-flex items-center gap-1.5">
          <Database size={14} />
          Optional — does not block finishing setup
        </p>
        <p className="text-xs text-gray-400">
          The shipped indexer cache seed (~36 MB compressed / ~150 MB on import) already powers
          release search. Build the full Open Library catalog only if you want local OL metadata
          (multi-GB, can take hours on a Pi).
        </p>
        {olStatus && (
          <p className="text-[11px] text-gray-500 font-mono pt-1">
            Status:{" "}
            {olLoading
              ? "…"
              : olStatus.catalog_ready
                ? `${olStatus.status || "ready"} · catalog ready`
                : olStatus.dumps_present
                  ? `${olStatus.status || "idle"} · dumps only`
                  : olStatus.status || "idle"}
            {olStatus.scheduled_build_at
              ? ` · scheduled ${new Date(olStatus.scheduled_build_at).toLocaleString()}`
              : ""}
          </p>
        )}
      </div>

      <label className="flex items-start gap-3 text-sm text-gray-200 cursor-pointer">
        <input
          type="radio"
          name="ol-choice"
          checked={olChoice === "skip"}
          onChange={() => setOlChoice("skip")}
          className="mt-1"
        />
        <span>
          <strong>Skip for now</strong>
          <span className="block text-xs text-gray-500">
            Finish onboarding; configure later under Admin → Catalog.
          </span>
        </span>
      </label>

      <label className="flex items-start gap-3 text-sm text-gray-200 cursor-pointer">
        <input
          type="radio"
          name="ol-choice"
          checked={olChoice === "now"}
          onChange={() => setOlChoice("now")}
          className="mt-1"
        />
        <span>
          <strong>Start initial catalog build now</strong>
          <span className="block text-xs text-gray-500">
            Downloads dumps and builds in the background. Keep the container running.
          </span>
        </span>
      </label>

      <label className="flex items-start gap-3 text-sm text-gray-200 cursor-pointer">
        <input
          type="radio"
          name="ol-choice"
          checked={olChoice === "schedule"}
          onChange={() => setOlChoice("schedule")}
          className="mt-1"
        />
        <span>
          <strong>Schedule for later</strong>
          <span className="block text-xs text-gray-500">
            Same off-peak schedule UI as Admin → Catalog.
          </span>
        </span>
      </label>

      {olChoice === "schedule" && (
        <label className="block text-xs text-gray-300 space-y-1.5 pl-6">
          <span className="inline-flex items-center gap-1.5 font-medium">
            <CalendarClock size={12} />
            Start download + rebuild at (local time)
          </span>
          <input
            type="datetime-local"
            value={scheduleLocal}
            onChange={(e) => setScheduleLocal(e.target.value)}
            className="w-full min-h-11 px-3 py-2 rounded-lg bg-gray-950 border border-gray-700 text-sm text-gray-100"
          />
        </label>
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
          Configure the library stack (ABS / Kavita / LibraForge), Audible metadata login,
          indexers, and optional catalog. Change anything later in Admin → Settings / Integrations.
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

          {step.id === "stack" ? (
            renderStack()
          ) : step.id === "audible" ? (
            <AudibleAuthPanel
              compact
              onStatusChange={() => {
                void refetchStatus();
              }}
            />
          ) : step.id === "openlibrary" ? (
            renderOpenLibrary()
          ) : step.id === "scraper" ? (
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

          {probeWarnings.length > 0 && (
            <div className="rounded-lg border border-amber-800/50 bg-amber-950/30 px-3 py-2.5 space-y-1">
              <p className="text-xs font-medium text-amber-200 inline-flex items-center gap-1">
                <AlertTriangle size={12} />
                Connection warnings (OK to continue)
              </p>
              <ul className="text-xs text-amber-100/80 list-disc pl-4 space-y-0.5">
                {probeWarnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </div>
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
                onClick={() => navigate("/admin?tab=settings")}
                className="px-3 py-2 text-sm text-gray-400 hover:text-gray-200"
              >
                Skip to Config
              </button>
              <button
                type="button"
                onClick={() => void next()}
                disabled={save.isPending || validating || olBuild.isPending || olSchedule.isPending}
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-brand-600 text-white text-sm font-medium hover:bg-brand-500 disabled:opacity-50"
              >
                {(save.isPending || validating || olBuild.isPending || olSchedule.isPending) && (
                  <Loader2 size={14} className="animate-spin" />
                )}
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
