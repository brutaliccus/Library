import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Save, Settings2, AlertTriangle, Eye, EyeOff } from "lucide-react";
import api from "../../api/client";
import { useToast } from "../../contexts/ToastContext";

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

  const { data, isLoading } = useQuery({
    queryKey: ["admin-config"],
    queryFn: async () => {
      const { data } = await api.get("/admin/config");
      return data as ConfigResponse;
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

  const groups = data?.groups || [];
  const settings = data?.settings || [];
  const byGroup = useMemo(() => {
    const map: Record<string, ConfigSetting[]> = {};
    for (const s of settings) {
      (map[s.group] ||= []).push(s);
    }
    return map;
  }, [settings]);

  const dirtyKeys = Object.keys(drafts).filter((k) => {
    const original = settings.find((s) => s.key === k);
    if (!original) return false;
    // For secrets, empty draft means "don't change"; non-empty means set
    if (original.secret) return drafts[k] !== undefined && drafts[k] !== "";
    return drafts[k] !== original.value;
  });

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

  if (isLoading) {
    return <p className="text-sm text-gray-500">Loading configuration…</p>;
  }

  const current = byGroup[activeGroup] || [];

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

      <div className="flex gap-2 overflow-x-auto pb-1">
        {groups.map((g) => (
          <button
            key={g.id}
            type="button"
            onClick={() => setActiveGroup(g.id)}
            className={`shrink-0 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
              activeGroup === g.id
                ? "bg-gray-800 text-brand-400 border border-brand-500/40"
                : "bg-gray-900 text-gray-400 border border-gray-800 hover:text-gray-200"
            }`}
          >
            {g.label}
          </button>
        ))}
      </div>

      <div className="space-y-3">
        {current.map((s) => {
          const draft = drafts[s.key];
          const show = showSecrets[s.key];
          const isBool = s.valueType === "bool";
          const displayValue =
            draft !== undefined
              ? draft
              : s.secret
                ? ""
                : s.value;

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
      </div>

      <div className="flex justify-end">
        <button
          type="button"
          onClick={saveGroup}
          disabled={save.isPending}
          className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-gray-600 text-sm text-gray-200 hover:border-gray-500"
        >
          <Save size={14} />
          Save this section
        </button>
      </div>
    </div>
  );
}
