import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Save,
  Settings2,
  AlertTriangle,
  Eye,
  EyeOff,
  Database,
  SlidersHorizontal,
  X,
  CalendarClock,
} from "lucide-react";
import api from "../../api/client";
import { useToast } from "../../contexts/ToastContext";
import NamingTemplateBuilder from "./NamingTemplateBuilder";

interface OlCatalogStatus {
  status: string;
  message?: string;
  catalog_ready?: boolean;
  catalog_size_bytes?: number;
  catalog_mtime?: number | null;
  catalog_works?: number | null;
  catalog_authors?: number | null;
  catalog_isbns?: number | null;
  catalog_error?: string;
  catalog_path?: string;
  dumps_dir?: string;
  dumps_present?: boolean;
  dumps_size_bytes?: number;
  new_dumps_available?: boolean;
  changed_dumps?: string[];
  dumps_checked_at?: number | null;
  scheduled_build_at?: string | null;
  scheduled_include_editions?: boolean;
  scheduled_force_download?: boolean;
  schedule_timezone?: string;
  warnings?: string[];
  include_editions?: boolean;
  log_tail?: string;
  log_recent?: string[];
  process_alive?: boolean;
  started_at?: number | null;
  updated_at?: number | null;
  finished_at?: number | null;
  elapsed_seconds?: number | null;
  progress_age_seconds?: number | null;
}

/** Format a Date as `YYYY-MM-DDTHH:mm` in the browser's local timezone. */
function toLocalDatetimeInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Default suggestion: tomorrow at 12:00 AM local. */
function defaultScheduleLocalValue(): string {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(0, 0, 0, 0);
  return toLocalDatetimeInput(d);
}

/** Convert datetime-local value (browser local) → ISO UTC for the API. */
function localInputToIsoUtc(localValue: string): string {
  const d = new Date(localValue);
  if (Number.isNaN(d.getTime())) {
    throw new Error("Invalid date/time");
  }
  return d.toISOString();
}

function formatScheduledLocal(isoUtc: string): string {
  const d = new Date(isoUtc);
  if (Number.isNaN(d.getTime())) return isoUtc;
  return d.toLocaleString(undefined, {
    weekday: "short",
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

interface ConfigSetting {
  key: string;
  group: string;
  label: string;
  valueType: string;
  secret: boolean;
  editable: boolean;
  restartRequired: boolean;
  highUsage: boolean;
  help: string;
  placeholder: string;
  value: string;
  configured: boolean;
  overridden: boolean;
  envConfigured: boolean;
  hint: string;
}

interface ConfigResponse {
  groups: Array<{ id: string; label: string }>;
  settings: ConfigSetting[];
}

export type ConfigTabProps = {
  /** Pin to one settings group and hide the section rail (Admin Pipelines / Catalog). */
  lockedGroup?: string;
  /** Hide groups from the section rail when they are promoted elsewhere in Admin. */
  omitGroups?: string[];
  /** Initial group when unlocked (e.g. deep-link ?section=). */
  initialGroup?: string;
  /** Override page title when locked or embedded. */
  title?: string;
  /** Override page description. */
  description?: string;
};

export default function ConfigTab({
  lockedGroup,
  omitGroups = [],
  initialGroup,
  title,
  description,
}: ConfigTabProps = {}) {
  const { toast } = useToast();
  const qc = useQueryClient();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [showSecrets, setShowSecrets] = useState<Record<string, boolean>>({});
  const [activeGroup, setActiveGroup] = useState<string>(
    lockedGroup || initialGroup || "libraries"
  );
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const locked = Boolean(lockedGroup);

  const { data, isLoading } = useQuery({
    queryKey: ["admin-config"],
    queryFn: async () => {
      const { data } = await api.get("/admin/config");
      return data as ConfigResponse;
    },
    staleTime: 60_000,
  });

  const olQuery = useQuery({
    queryKey: ["admin-ol-catalog"],
    queryFn: async () => {
      // Throttled remote dump probe when Config opens (no download).
      const { data } = await api.get("/admin/ol-catalog", { params: { check: true } });
      return data as OlCatalogStatus;
    },
    staleTime: 60_000,
    refetchInterval: (q) => {
      if (q.state.data?.status === "running") return 3000;
      if (q.state.data?.scheduled_build_at) return 30000;
      return false;
    },
  });

  const olBuild = useMutation({
    mutationFn: async (opts: { includeEditions: boolean; forceDownload?: boolean }) => {
      const { data } = await api.post("/admin/ol-catalog/build", {
        include_editions: opts.includeEditions,
        skip_download: false,
        force_download: Boolean(opts.forceDownload),
      });
      return data as OlCatalogStatus;
    },
    onSuccess: (_data, vars) => {
      void qc.invalidateQueries({ queryKey: ["admin-ol-catalog"] });
      toast(
        vars.forceDownload
          ? "Open Library catalog update started (download + rebuild)"
          : "Open Library catalog build started",
        "success"
      );
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Failed to start catalog build";
      toast(String(msg), "error");
    },
  });

  const olSchedule = useMutation({
    mutationFn: async (opts: { scheduledAtIso: string; includeEditions?: boolean }) => {
      const { data } = await api.post("/admin/ol-catalog/schedule", {
        scheduled_at: opts.scheduledAtIso,
        include_editions: Boolean(opts.includeEditions),
        force_download: true,
      });
      return data as OlCatalogStatus;
    },
    onSuccess: (data) => {
      void qc.invalidateQueries({ queryKey: ["admin-ol-catalog"] });
      const when = data.scheduled_build_at
        ? formatScheduledLocal(data.scheduled_build_at)
        : "the chosen time";
      toast(`Catalog update scheduled for ${when} (your local time)`, "success");
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Failed to schedule catalog update";
      toast(String(msg), "error");
    },
  });

  const olCancelSchedule = useMutation({
    mutationFn: async () => {
      const { data } = await api.delete("/admin/ol-catalog/schedule");
      return data as OlCatalogStatus;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin-ol-catalog"] });
      toast("Scheduled catalog update cancelled", "success");
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Failed to cancel schedule";
      toast(String(msg), "error");
    },
  });

  const save = useMutation({
    mutationFn: async (updates: Record<string, string>) => {
      const { data } = await api.put("/admin/config", { settings: updates });
      return data as ConfigResponse;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin-config"] });
      void qc.invalidateQueries({ queryKey: ["admin-setup-status"] });
      void qc.invalidateQueries({ queryKey: ["admin-integrations"] });
      setDrafts({});
      toast("Settings saved", "success");
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Failed to save settings";
      toast(String(msg), "error");
    },
  });

  useEffect(() => {
    if (!mobileNavOpen || locked) return;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMobileNavOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = "";
      window.removeEventListener("keydown", onKey);
    };
  }, [mobileNavOpen, locked]);

  useEffect(() => {
    if (lockedGroup) setActiveGroup(lockedGroup);
  }, [lockedGroup]);

  useEffect(() => {
    if (locked || !initialGroup) return;
    setActiveGroup(initialGroup);
  }, [initialGroup, locked]);

  const omitSet = useMemo(() => new Set(omitGroups), [omitGroups]);
  const groups = useMemo(
    () => (data?.groups || []).filter((g) => !omitSet.has(g.id)),
    [data?.groups, omitSet]
  );
  const settings = data?.settings || [];
  const byGroup = useMemo(() => {
    const map: Record<string, ConfigSetting[]> = {};
    for (const s of settings) {
      (map[s.group] ||= []).push(s);
    }
    return map;
  }, [settings]);

  useEffect(() => {
    if (locked) return;
    if (!groups.length) return;
    if (!groups.some((g) => g.id === activeGroup)) {
      setActiveGroup(groups[0].id);
    }
  }, [groups, activeGroup, locked]);

  const dirtyKeys = Object.keys(drafts).filter((k) => {
    const original = settings.find((s) => s.key === k);
    if (!original) return false;
    // For secrets, empty draft means "don't change"; non-empty means set
    if (original.secret) return drafts[k] !== undefined && drafts[k] !== "";
    return drafts[k] !== original.value;
  });

  const dirtyByGroup = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const key of dirtyKeys) {
      const def = settings.find((s) => s.key === key);
      if (!def) continue;
      counts[def.group] = (counts[def.group] || 0) + 1;
    }
    return counts;
  }, [dirtyKeys, settings]);

  const saveGroup = () => {
    const groupId = lockedGroup || activeGroup;
    const updates: Record<string, string> = {};
    for (const key of dirtyKeys) {
      const def = settings.find((s) => s.key === key);
      if (!def || def.group !== groupId) continue;
      updates[key] = drafts[key] ?? "";
    }
    if (!Object.keys(updates).length) {
      toast("No changes in this section", "info");
      return;
    }
    save.mutate(updates);
  };

  const saveAll = () => {
    const updates: Record<string, string> = {};
    for (const key of dirtyKeys) {
      updates[key] = drafts[key] ?? "";
    }
    if (!Object.keys(updates).length) {
      toast("No changes to save", "info");
      return;
    }
    save.mutate(updates);
  };

  const selectGroup = (id: string) => {
    setActiveGroup(id);
    setMobileNavOpen(false);
  };

  if (isLoading) {
    return <p className="text-sm text-gray-500">Loading configuration…</p>;
  }

  const effectiveGroup = lockedGroup || activeGroup;
  const current = byGroup[effectiveGroup] || [];
  const activeLabel =
    (data?.groups || []).find((g) => g.id === effectiveGroup)?.label || "Section";
  const heading = title || (locked ? activeLabel : "Instance configuration");
  const blurb =
    description ||
    (locked
      ? "Values override .env when set. Secrets show only a hint until you enter a new value."
      : "Runtime settings and paths. Catalog APIs and pipeline toggles live under Catalog and Pipelines. Secrets for NYT / OpenRouter / Mullvad are under Integrations.");

  const navList = (
    <nav className="space-y-0.5" aria-label="Config sections">
      <p className="px-3 pt-1 pb-2 text-[11px] font-semibold uppercase tracking-wider text-gray-500">
        Sections
      </p>
      {groups.map((g) => {
        const dirty = dirtyByGroup[g.id] || 0;
        const active = effectiveGroup === g.id;
        return (
          <button
            key={g.id}
            type="button"
            onClick={() => selectGroup(g.id)}
            className={`w-full text-left px-3 py-2 text-sm rounded-lg transition-colors flex items-center justify-between gap-2 ${
              active
                ? "bg-brand-600/20 text-brand-300 font-medium"
                : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
            }`}
          >
            <span className="truncate">{g.label}</span>
            {dirty > 0 && (
              <span
                className={`shrink-0 text-[10px] px-1.5 py-0.5 rounded-md ${
                  active ? "bg-brand-600/30 text-brand-200" : "bg-gray-800 text-amber-400"
                }`}
              >
                {dirty}
              </span>
            )}
          </button>
        );
      })}
    </nav>
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-100 flex items-center gap-2">
            <Settings2 size={18} />
            {heading}
          </h2>
          <p className="text-xs text-gray-500 mt-1 max-w-xl">{blurb}</p>
        </div>
        <button
          type="button"
          onClick={locked ? saveGroup : saveAll}
          disabled={
            save.isPending ||
            (locked ? !(dirtyByGroup[effectiveGroup] > 0) : dirtyKeys.length === 0)
          }
          className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg bg-brand-600 text-white text-sm font-medium hover:bg-brand-500 disabled:opacity-40"
        >
          <Save size={14} />
          {locked
            ? `Save${dirtyByGroup[effectiveGroup] ? ` (${dirtyByGroup[effectiveGroup]})` : ""}`
            : `Save all changes${dirtyKeys.length ? ` (${dirtyKeys.length})` : ""}`}
        </button>
      </div>

      {!locked && mobileNavOpen && (
        <div className="fixed inset-0 z-50 lg:hidden" role="dialog" aria-modal="true" aria-label="Config sections">
          <div className="absolute inset-0 bg-black/60" onClick={() => setMobileNavOpen(false)} />
          <div className="absolute left-0 top-0 bottom-0 w-72 max-w-[85vw] bg-gray-900 border-r border-gray-800 overflow-y-auto p-4 pt-[max(1rem,env(safe-area-inset-top,0px))] pb-[max(1rem,env(safe-area-inset-bottom,0px))] pl-[max(1rem,env(safe-area-inset-left,0px))] drawer-slide-in">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-semibold text-gray-200">Config sections</h3>
              <button
                type="button"
                onClick={() => setMobileNavOpen(false)}
                className="p-1 text-gray-500 hover:text-gray-300 transition-colors"
                aria-label="Close sections"
              >
                <X size={18} />
              </button>
            </div>
            {navList}
          </div>
        </div>
      )}

      <div className="flex gap-6 items-start min-w-0">
        {!locked && (
          <aside className="hidden lg:block w-52 shrink-0 sticky top-[4.5rem] max-h-[calc(100vh-5rem)] overflow-y-auto pr-2 scrollbar-hide">
            {navList}
          </aside>
        )}

        <div className="flex-1 min-w-0 space-y-3">
          {!locked && (
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 min-w-0">
                <button
                  type="button"
                  onClick={() => setMobileNavOpen(true)}
                  className="lg:hidden shrink-0 inline-flex items-center gap-1.5 px-2.5 py-2 rounded-lg text-sm font-medium text-gray-400 hover:bg-gray-800 hover:text-gray-200 transition-colors"
                  title="Config sections"
                  aria-label="Open config sections"
                >
                  <SlidersHorizontal size={16} />
                  {dirtyKeys.length > 0 && (
                    <span className="px-1.5 py-0.5 bg-brand-600 text-white text-[10px] font-bold rounded-full leading-none">
                      {dirtyKeys.length}
                    </span>
                  )}
                </button>
                <h3 className="text-sm font-semibold text-gray-200 truncate">{activeLabel}</h3>
              </div>
              <button
                type="button"
                onClick={saveGroup}
                disabled={save.isPending || !(dirtyByGroup[effectiveGroup] > 0)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-gray-600 text-xs text-gray-200 hover:border-gray-500 disabled:opacity-40 shrink-0"
              >
                <Save size={12} />
                Save section
              </button>
            </div>
          )}

          {(effectiveGroup === "storage" || effectiveGroup === "catalog") && (
            <OlCatalogPanel
              status={olQuery.data}
              loading={olQuery.isLoading}
              building={olBuild.isPending || olQuery.data?.status === "running"}
              scheduling={olSchedule.isPending || olCancelSchedule.isPending}
              onBuild={(includeEditions, forceDownload) => {
                const editionsNote = includeEditions
                  ? "\n\nIncluding editions makes the download and final DB much larger (often 10-20+ GB)."
                  : "";
                const updateNote = forceDownload
                  ? "\n\nThis will re-download changed Open Library dumps, then rebuild the SQLite catalog."
                  : "";
                const ok = window.confirm(
                  (forceDownload
                    ? "Update the local Open Library catalog?\n\n"
                    : "Build the local Open Library catalog?\n\n") +
                    "This downloads multi-GB dump files and can take many hours on a Pi. " +
                    "The finished database is typically several GB. " +
                    "Keep the app running until it finishes." +
                    updateNote +
                    editionsNote +
                    "\n\nContinue?"
                );
                if (ok) olBuild.mutate({ includeEditions, forceDownload });
              }}
              onSchedule={(localValue) => {
                try {
                  const iso = localInputToIsoUtc(localValue);
                  const label = formatScheduledLocal(iso);
                  const ok = window.confirm(
                    `Schedule Open Library dump download + catalog rebuild for:\n\n${label}\n\n` +
                      "(Time is in your browser's local timezone.)\n\n" +
                      "Nothing downloads until then. Continue?"
                  );
                  if (ok) olSchedule.mutate({ scheduledAtIso: iso });
                } catch {
                  toast("Pick a valid date and time", "error");
                }
              }}
              onCancelSchedule={() => {
                const ok = window.confirm("Cancel the scheduled catalog update?");
                if (ok) olCancelSchedule.mutate();
              }}
            />
          )}

          {effectiveGroup === "libraries" && <KavitaOpdsProbe />}

          {current.map((s) => {
            const draft = drafts[s.key];
            const show = showSecrets[s.key];
            const isBool = s.valueType === "bool";
            const displayValue = draft !== undefined ? draft : s.secret ? "" : s.value;

            return (
              <div
                key={s.key}
                className="p-3 rounded-xl border border-gray-800 bg-gray-900/50 space-y-2"
              >
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <p className="text-sm font-medium text-gray-100">{s.label}</p>
                    {s.help && <p className="text-xs text-gray-500 mt-0.5">{s.help}</p>}
                  </div>
                  <div className="flex flex-wrap gap-1 justify-end">
                    {s.configured && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-900/40 text-emerald-400">
                        set{s.overridden ? "" : " (env)"}
                      </span>
                    )}
                    {s.highUsage && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-900/40 text-amber-400 inline-flex items-center gap-0.5">
                        <AlertTriangle size={10} /> high usage
                      </span>
                    )}
                    {s.restartRequired && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-400">
                        may need restart
                      </span>
                    )}
                  </div>
                </div>

                {!s.editable ? (
                  <p className="text-xs text-gray-400 font-mono break-all">
                    {s.secret ? s.hint || "(not set)" : s.value || "(not set)"}
                  </p>
                ) : isBool ? (
                  <label className="inline-flex items-center gap-2 text-sm text-gray-300">
                    <input
                      type="checkbox"
                      checked={(draft ?? s.value) === "true"}
                      onChange={(e) =>
                        setDrafts((d) => ({ ...d, [s.key]: e.target.checked ? "true" : "false" }))
                      }
                      className="rounded border-gray-600 bg-gray-800"
                    />
                    Enabled
                  </label>
                ) : s.key === "config.libraforge_naming_template" ? (
                  <div className="space-y-2">
                    <NamingTemplateBuilder
                      value={displayValue}
                      onChange={(next) => setDrafts((d) => ({ ...d, [s.key]: next }))}
                      dense
                    />
                    {s.overridden && (
                      <button
                        type="button"
                        onClick={() => {
                          setDrafts((d) => ({ ...d, [s.key]: "" }));
                          save.mutate({ [s.key]: "" });
                        }}
                        className="px-2 py-1 text-xs text-gray-400 hover:text-red-400 border border-gray-700 rounded-lg"
                        title="Clear DB override (revert to env)"
                      >
                        Clear override
                      </button>
                    )}
                  </div>
                ) : (
                  <div className="flex gap-2">
                    <input
                      type={s.secret && !show ? "password" : "text"}
                      value={displayValue}
                      placeholder={
                        s.secret
                          ? s.hint
                            ? `Configured · ${s.hint} — enter new value to replace`
                            : s.placeholder || "Enter value"
                          : s.placeholder || ""
                      }
                      onChange={(e) => setDrafts((d) => ({ ...d, [s.key]: e.target.value }))}
                      className="flex-1 min-w-0 px-3 py-2 rounded-lg bg-gray-950 border border-gray-700 text-sm text-gray-100 placeholder:text-gray-600"
                    />
                    {s.secret && (
                      <button
                        type="button"
                        onClick={() => setShowSecrets((m) => ({ ...m, [s.key]: !show }))}
                        className="px-2 rounded-lg border border-gray-700 text-gray-400 hover:text-gray-200"
                        aria-label={show ? "Hide" : "Show"}
                      >
                        {show ? <EyeOff size={14} /> : <Eye size={14} />}
                      </button>
                    )}
                    {s.overridden && (
                      <button
                        type="button"
                        onClick={() => {
                          setDrafts((d) => ({ ...d, [s.key]: "" }));
                          save.mutate({ [s.key]: "" });
                        }}
                        className="px-2 text-xs text-gray-400 hover:text-red-400 border border-gray-700 rounded-lg"
                        title="Clear DB override (revert to env)"
                      >
                        Clear
                      </button>
                    )}
                  </div>
                )}
              </div>
            );
          })}

          {!current.length && (
            <p className="text-sm text-gray-500">No settings in this section.</p>
          )}
        </div>
      </div>
    </div>
  );
}

function formatBytes(n?: number): string {
  if (!n || n <= 0) return "—";
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  return `${Math.round(n / 1024)} KB`;
}

function formatElapsed(seconds?: number | null): string {
  if (seconds == null || seconds < 0 || Number.isNaN(seconds)) return "—";
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${r}s`;
  return `${r}s`;
}

function formatProgressAge(seconds?: number | null): string {
  if (seconds == null) return "—";
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  return `${Math.floor(seconds / 60)}m ago`;
}

function OlCatalogPanel({
  status,
  loading,
  building,
  scheduling,
  onBuild,
  onSchedule,
  onCancelSchedule,
}: {
  status?: OlCatalogStatus;
  loading: boolean;
  building: boolean;
  scheduling: boolean;
  onBuild: (includeEditions: boolean, forceDownload?: boolean) => void;
  onSchedule: (localDatetimeValue: string) => void;
  onCancelSchedule: () => void;
}) {
  const warnings = status?.warnings || [];
  const newDumps = Boolean(status?.new_dumps_available);
  const changed = (status?.changed_dumps || []).join(", ");
  const scheduledAt = status?.scheduled_build_at || null;
  const [pickerOpen, setPickerOpen] = useState(false);
  const [scheduleLocal, setScheduleLocal] = useState(defaultScheduleLocalValue);

  useEffect(() => {
    if (scheduledAt) {
      setScheduleLocal(toLocalDatetimeInput(new Date(scheduledAt)));
      setPickerOpen(false);
    }
  }, [scheduledAt]);

  const busy = building || scheduling;
  const isRunning = status?.status === "running";
  const isInterrupted = status?.status === "interrupted";
  const progressAge = status?.progress_age_seconds;
  const quietTooLong = isRunning && progressAge != null && progressAge >= 120;
  const logLines =
    status?.log_recent && status.log_recent.length
      ? status.log_recent
      : status?.log_tail
        ? [status.log_tail]
        : [];

  return (
    <div className="p-4 rounded-xl border border-amber-900/50 bg-amber-950/20 space-y-3">
      <div className="flex items-start gap-2">
        <Database size={18} className="text-amber-400 mt-0.5 shrink-0" />
        <div>
          <h3 className="text-sm font-semibold text-gray-100">Open Library catalog</h3>
          <p className="text-xs text-gray-400 mt-1">
            Optional local metadata DB used for matching and store search. Not required to run the
            app — the indexer cache seed already ships with the install. Build this only if you want
            the full local Open Library catalog.
          </p>
        </div>
      </div>

      {(isRunning || isInterrupted) && (
        <div
          className={`rounded-lg border p-3 space-y-2 ${
            isInterrupted
              ? "border-rose-700/50 bg-rose-950/30"
              : "border-emerald-700/50 bg-emerald-950/30"
          }`}
        >
          <p
            className={`text-sm font-semibold inline-flex items-center gap-1.5 ${
              isInterrupted ? "text-rose-200" : "text-emerald-200"
            }`}
          >
            <Database size={14} className={isRunning ? "animate-pulse" : undefined} />
            {isInterrupted
              ? "Build interrupted"
              : status?.process_alive === false
                ? "Build starting…"
                : "Build in progress"}
          </p>
          <p className="text-xs text-gray-200 break-words">
            {status?.message || (isInterrupted ? "Previous build did not finish." : "Working…")}
          </p>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px] font-mono text-gray-400">
            <p>
              Elapsed:{" "}
              <span className="text-gray-200">{formatElapsed(status?.elapsed_seconds)}</span>
            </p>
            <p>
              Last log:{" "}
              <span className={quietTooLong ? "text-amber-300" : "text-gray-200"}>
                {formatProgressAge(progressAge)}
              </span>
            </p>
            <p>
              Dumps: <span className="text-gray-200">{formatBytes(status?.dumps_size_bytes)}</span>
            </p>
            <p>
              DB: <span className="text-gray-200">{formatBytes(status?.catalog_size_bytes)}</span>
            </p>
          </div>
          {quietTooLong && (
            <p className="text-[11px] text-amber-200/90">
              No new log lines for {formatElapsed(progressAge)}. Downloads report every ~10s;
              import/index phases can stay quiet longer — check container logs for{" "}
              <span className="font-mono">[ol-import]</span> if this stays stuck.
            </p>
          )}
          {isInterrupted && (
            <p className="text-[11px] text-rose-100/80">
              The import process is no longer running (often after an app restart). Start Generate
              catalog again to continue; dumps already on disk are reused.
            </p>
          )}
          {logLines.length > 0 && (
            <div className="rounded-md border border-white/10 bg-black/40 px-2.5 py-2 max-h-36 overflow-y-auto">
              <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">Recent log</p>
              <ul className="space-y-0.5 font-mono text-[11px] text-gray-300">
                {logLines.map((line, i) => (
                  <li key={`${i}-${line.slice(0, 24)}`} className="break-words">
                    {line}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {newDumps && (
        <div className="rounded-lg border border-sky-700/50 bg-sky-950/40 p-3 space-y-3">
          <p className="text-sm font-semibold text-sky-200 inline-flex items-center gap-1.5">
            <AlertTriangle size={14} />
            New Open Library dumps available
          </p>
          <p className="text-xs text-sky-100/80">
            Remote monthly dumps differ from the copies on disk
            {changed ? ` (${changed})` : ""}. Download and rebuild only start when you click Update
            catalog or confirm a schedule — a daily check never auto-downloads.
          </p>

          {scheduledAt && (
            <div className="rounded-md border border-sky-600/40 bg-sky-900/40 px-3 py-2 space-y-2">
              <p className="text-xs text-sky-100">
                Scheduled for{" "}
                <span className="font-semibold text-white">{formatScheduledLocal(scheduledAt)}</span>
                <span className="text-sky-200/70"> (your local time)</span>
              </p>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setScheduleLocal(toLocalDatetimeInput(new Date(scheduledAt)));
                    setPickerOpen(true);
                  }}
                  className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-sky-600/70 text-sky-100 text-sm hover:border-sky-400 disabled:opacity-40 min-h-10"
                >
                  <CalendarClock size={14} />
                  Reschedule
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={onCancelSchedule}
                  className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-rose-700/60 text-rose-200 text-sm hover:border-rose-500 disabled:opacity-40 min-h-10"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => onBuild(false, true)}
              className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg bg-sky-700 text-white text-sm font-medium hover:bg-sky-600 disabled:opacity-40 min-h-10"
            >
              <Database size={14} />
              {building ? "Updating…" : "Update catalog"}
            </button>
            {!scheduledAt && (
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setScheduleLocal(defaultScheduleLocalValue());
                  setPickerOpen((v) => !v);
                }}
                className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-sky-600/70 text-sky-100 text-sm font-medium hover:border-sky-400 disabled:opacity-40 min-h-10"
              >
                <CalendarClock size={14} />
                Schedule
              </button>
            )}
          </div>

          {pickerOpen && (
            <div className="rounded-md border border-sky-700/50 bg-black/30 p-3 space-y-2">
              <label className="block text-xs text-sky-100/90 space-y-1.5">
                <span className="font-medium">Start download + rebuild at</span>
                <input
                  type="datetime-local"
                  value={scheduleLocal}
                  onChange={(e) => setScheduleLocal(e.target.value)}
                  className="w-full min-h-11 px-3 py-2 rounded-lg bg-gray-950 border border-sky-800/60 text-sky-50 text-sm"
                />
              </label>
              <p className="text-[11px] text-sky-200/60">
                Uses your browser&apos;s local timezone. The server stores the time in UTC and starts
                the same force-download path as Update catalog.
              </p>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={busy || !scheduleLocal}
                  onClick={() => onSchedule(scheduleLocal)}
                  className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg bg-sky-600 text-white text-sm font-medium hover:bg-sky-500 disabled:opacity-40 min-h-10"
                >
                  Confirm schedule
                </button>
                <button
                  type="button"
                  disabled={scheduling}
                  onClick={() => setPickerOpen(false)}
                  className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-gray-600 text-gray-200 text-sm hover:border-gray-500 disabled:opacity-40 min-h-10"
                >
                  Dismiss
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {!newDumps && scheduledAt && (
        <div className="rounded-lg border border-sky-700/50 bg-sky-950/40 p-3 space-y-2">
          <p className="text-xs text-sky-100">
            Catalog update scheduled for{" "}
            <span className="font-semibold text-white">{formatScheduledLocal(scheduledAt)}</span>
            <span className="text-sky-200/70"> (your local time)</span>
          </p>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setScheduleLocal(toLocalDatetimeInput(new Date(scheduledAt)));
                setPickerOpen(true);
              }}
              className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-sky-600/70 text-sky-100 text-sm hover:border-sky-400 disabled:opacity-40 min-h-10"
            >
              <CalendarClock size={14} />
              Reschedule
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={onCancelSchedule}
              className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-rose-700/60 text-rose-200 text-sm hover:border-rose-500 disabled:opacity-40 min-h-10"
            >
              Cancel
            </button>
          </div>
          {pickerOpen && (
            <div className="rounded-md border border-sky-700/50 bg-black/30 p-3 space-y-2">
              <label className="block text-xs text-sky-100/90 space-y-1.5">
                <span className="font-medium">New start time</span>
                <input
                  type="datetime-local"
                  value={scheduleLocal}
                  onChange={(e) => setScheduleLocal(e.target.value)}
                  className="w-full min-h-11 px-3 py-2 rounded-lg bg-gray-950 border border-sky-800/60 text-sky-50 text-sm"
                />
              </label>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={busy || !scheduleLocal}
                  onClick={() => onSchedule(scheduleLocal)}
                  className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg bg-sky-600 text-white text-sm font-medium hover:bg-sky-500 disabled:opacity-40 min-h-10"
                >
                  Confirm schedule
                </button>
                <button
                  type="button"
                  onClick={() => setPickerOpen(false)}
                  className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-gray-600 text-gray-200 text-sm min-h-10"
                >
                  Dismiss
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      <div className="rounded-lg border border-amber-800/40 bg-black/20 p-3 space-y-1.5">
        <p className="text-xs font-medium text-amber-300 inline-flex items-center gap-1">
          <AlertTriangle size={12} /> Before you start
        </p>
        <ul className="text-xs text-gray-400 list-disc pl-4 space-y-1">
          {warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
          {!warnings.length && (
            <>
              <li>Downloads multi-GB dumps; final DB is typically several GB (much more with editions).</li>
              <li>Can take many hours on a Raspberry Pi — leave the container running.</li>
            </>
          )}
        </ul>
      </div>

      <div className="text-xs text-gray-400 space-y-0.5 font-mono">
        <p>
          Status:{" "}
          <span className="text-gray-200">
            {loading
              ? "…"
              : status?.catalog_ready
                ? `${status?.status || "ready"} · catalog ready${
                    newDumps ? " · update available" : ""
                  }${scheduledAt ? " · scheduled" : ""}`
                : status?.dumps_present
                  ? `${status?.status || "idle"} · dumps only${
                      newDumps ? " · update available" : ""
                    }${scheduledAt ? " · scheduled" : ""}`
                  : `${status?.status || "idle"}${scheduledAt ? " · scheduled" : ""}`}
          </span>
        </p>
        <p>DB size: {formatBytes(status?.catalog_size_bytes)}</p>
        {(status?.dumps_size_bytes != null || status?.dumps_dir) && (
          <p>Dumps size: {formatBytes(status?.dumps_size_bytes)}</p>
        )}
        {status?.catalog_ready &&
          (status.catalog_works != null ||
            status.catalog_authors != null ||
            status.catalog_isbns != null) && (
            <p>
              Rows:{" "}
              {[
                status.catalog_works != null
                  ? `${status.catalog_works.toLocaleString()} works`
                  : null,
                status.catalog_authors != null
                  ? `${status.catalog_authors.toLocaleString()} authors`
                  : null,
                status.catalog_isbns != null
                  ? `${status.catalog_isbns.toLocaleString()} isbns`
                  : null,
              ]
                .filter(Boolean)
                .join(" · ")}
            </p>
          )}
        {status?.catalog_path && <p className="break-all">DB: {status.catalog_path}</p>}
        {status?.dumps_dir && <p className="break-all">Dumps: {status.dumps_dir}</p>}
        {status?.catalog_error && !status?.catalog_ready && (
          <p className="text-amber-400/90 break-words">DB error: {status.catalog_error}</p>
        )}
        {status?.message && !isRunning && !isInterrupted && (
          <p className="text-gray-500 break-words">Last: {status.message}</p>
        )}
      </div>

      <div className="flex flex-wrap gap-2">
        {!newDumps && status?.catalog_ready && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onBuild(false, true)}
            className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-sky-800/60 text-sky-200/90 text-xs hover:border-sky-600 disabled:opacity-40 min-h-10"
            title="Force re-download dumps and rebuild"
          >
            Download &amp; rebuild
          </button>
        )}
        <button
          type="button"
          disabled={busy}
          onClick={() => onBuild(false)}
          className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg bg-amber-700/80 text-white text-sm font-medium hover:bg-amber-600 disabled:opacity-40 min-h-10"
        >
          <Database size={14} />
          {building ? "Building…" : "Generate catalog (recommended)"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => onBuild(true)}
          className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-amber-800/60 text-amber-200/90 text-xs hover:border-amber-600 disabled:opacity-40 min-h-10"
        >
          Generate with editions (very large)
        </button>
      </div>
    </div>
  );
}

function KavitaOpdsProbe() {
  const { toast } = useToast();
  const [status, setStatus] = useState<{
    configured?: boolean;
    ok?: boolean;
    error?: string | null;
    note?: string;
  } | null>(null);
  const [loading, setLoading] = useState(false);

  const runProbe = async () => {
    setLoading(true);
    try {
      const { data } = await api.get("/admin/kavita/opds-status");
      setStatus(data);
      if (data?.ok) toast("Kavita OPDS responded OK", "success");
      else toast(data?.error || "Kavita OPDS not reachable", "error");
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "OPDS probe failed";
      toast(typeof detail === "string" ? detail : "OPDS probe failed", "error");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="p-3 rounded-xl border border-sky-900/50 bg-sky-950/20 space-y-2">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-gray-100">Kavita OPDS</p>
          <p className="text-xs text-gray-500 mt-0.5 leading-relaxed">
            Kavita’s OPDS feed uses the same API key as above. Members connect via Library Site’s
            proxied feed (Settings → Ereader) so the key stays private.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void runProbe()}
          disabled={loading}
          className="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-sky-800/60 text-xs text-sky-200 hover:border-sky-600 disabled:opacity-40"
        >
          {loading ? "Testing…" : "Test OPDS"}
        </button>
      </div>
      {status && (
        <p
          className={`text-xs ${
            status.ok ? "text-emerald-400" : status.configured ? "text-amber-400" : "text-gray-500"
          }`}
        >
          {status.ok
            ? "OPDS feed reachable with the configured key."
            : status.error || "Not configured"}
        </p>
      )}
    </div>
  );
}
