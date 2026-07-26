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
} from "lucide-react";
import api from "../../api/client";
import { useToast } from "../../contexts/ToastContext";

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
  warnings?: string[];
  include_editions?: boolean;
  log_tail?: string;
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

export default function ConfigTab() {
  const { toast } = useToast();
  const qc = useQueryClient();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [showSecrets, setShowSecrets] = useState<Record<string, boolean>>({});
  const [activeGroup, setActiveGroup] = useState<string>("libraries");
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["admin-config"],
    queryFn: async () => {
      const { data } = await api.get("/admin/config");
      return data as ConfigResponse;
    },
  });

  const olQuery = useQuery({
    queryKey: ["admin-ol-catalog"],
    queryFn: async () => {
      const { data } = await api.get("/admin/ol-catalog");
      return data as OlCatalogStatus;
    },
    refetchInterval: (q) => (q.state.data?.status === "running" ? 5000 : false),
  });

  const olBuild = useMutation({
    mutationFn: async (includeEditions: boolean) => {
      const { data } = await api.post("/admin/ol-catalog/build", {
        include_editions: includeEditions,
        skip_download: false,
      });
      return data as OlCatalogStatus;
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin-ol-catalog"] });
      toast("Open Library catalog build started", "success");
    },
    onError: (err: unknown) => {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ||
        "Failed to start catalog build";
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
    if (!mobileNavOpen) return;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMobileNavOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = "";
      window.removeEventListener("keydown", onKey);
    };
  }, [mobileNavOpen]);

  const groups = data?.groups || [];
  const settings = data?.settings || [];
  const byGroup = useMemo(() => {
    const map: Record<string, ConfigSetting[]> = {};
    for (const s of settings) {
      (map[s.group] ||= []).push(s);
    }
    return map;
  }, [settings]);

  useEffect(() => {
    if (!groups.length) return;
    if (!groups.some((g) => g.id === activeGroup)) {
      setActiveGroup(groups[0].id);
    }
  }, [groups, activeGroup]);

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
    const updates: Record<string, string> = {};
    for (const key of dirtyKeys) {
      const def = settings.find((s) => s.key === key);
      if (!def || def.group !== activeGroup) continue;
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

  const current = byGroup[activeGroup] || [];
  const activeLabel = groups.find((g) => g.id === activeGroup)?.label || "Section";

  const navList = (
    <nav className="space-y-0.5" aria-label="Config sections">
      <p className="px-3 pt-1 pb-2 text-[11px] font-semibold uppercase tracking-wider text-gray-500">
        Sections
      </p>
      {groups.map((g) => {
        const dirty = dirtyByGroup[g.id] || 0;
        const active = activeGroup === g.id;
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
            Instance configuration
          </h2>
          <p className="text-xs text-gray-500 mt-1 max-w-xl">
            All runtime settings and API keys. Values override .env when set. Secrets show only a
            hint until you enter a new value.
          </p>
        </div>
        <button
          type="button"
          onClick={saveAll}
          disabled={save.isPending || dirtyKeys.length === 0}
          className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg bg-brand-600 text-white text-sm font-medium hover:bg-brand-500 disabled:opacity-40"
        >
          <Save size={14} />
          Save all changes{dirtyKeys.length ? ` (${dirtyKeys.length})` : ""}
        </button>
      </div>

      {mobileNavOpen && (
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
        <aside className="hidden lg:block w-52 shrink-0 sticky top-[4.5rem] max-h-[calc(100vh-5rem)] overflow-y-auto pr-2 scrollbar-hide">
          {navList}
        </aside>

        <div className="flex-1 min-w-0 space-y-3">
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
              disabled={save.isPending || !(dirtyByGroup[activeGroup] > 0)}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-gray-600 text-xs text-gray-200 hover:border-gray-500 disabled:opacity-40 shrink-0"
            >
              <Save size={12} />
              Save section
            </button>
          </div>

          {(activeGroup === "storage" || activeGroup === "catalog") && (
            <OlCatalogPanel
              status={olQuery.data}
              loading={olQuery.isLoading}
              building={olBuild.isPending || olQuery.data?.status === "running"}
              onBuild={(includeEditions) => {
                const editionsNote = includeEditions
                  ? "\n\nIncluding editions makes the download and final DB much larger (often 10-20+ GB)."
                  : "";
                const ok = window.confirm(
                  "Build the local Open Library catalog?\n\n" +
                    "This downloads multi-GB dump files and can take many hours on a Pi. " +
                    "The finished database is typically several GB. " +
                    "Keep the app running until it finishes." +
                    editionsNote +
                    "\n\nContinue?"
                );
                if (ok) olBuild.mutate(includeEditions);
              }}
            />
          )}

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

function OlCatalogPanel({
  status,
  loading,
  building,
  onBuild,
}: {
  status?: OlCatalogStatus;
  loading: boolean;
  building: boolean;
  onBuild: (includeEditions: boolean) => void;
}) {
  const warnings = status?.warnings || [];
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
                ? `${status?.status || "ready"} · catalog ready`
                : status?.dumps_present
                  ? `${status?.status || "idle"} · dumps only`
                  : status?.status || "idle"}
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
        {status?.message && <p className="text-gray-500 break-words">Last: {status.message}</p>}
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={building}
          onClick={() => onBuild(false)}
          className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg bg-amber-700/80 text-white text-sm font-medium hover:bg-amber-600 disabled:opacity-40"
        >
          <Database size={14} />
          {building ? "Building…" : "Generate catalog (recommended)"}
        </button>
        <button
          type="button"
          disabled={building}
          onClick={() => onBuild(true)}
          className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-amber-800/60 text-amber-200/90 text-xs hover:border-amber-600 disabled:opacity-40"
        >
          Generate with editions (very large)
        </button>
      </div>
    </div>
  );
}
