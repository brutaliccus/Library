import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronUp, Plus, RotateCcw, X } from "lucide-react";
import {
  DEFAULT_NAMING_TEMPLATE,
  NAMING_TOKENS,
  SEGMENT_PRESETS,
  defaultSegments,
  humanSegmentLabel,
  parseNamingTemplate,
  previewExamplePath,
  segmentFromPreset,
  serializeNamingTemplate,
  type NamingSegment,
} from "../../utils/namingTemplate";

type Props = {
  value: string;
  onChange: (next: string) => void;
  /** Compact layout for nested settings rows. */
  dense?: boolean;
};

function moveSegment(segments: NamingSegment[], index: number, dir: -1 | 1): NamingSegment[] {
  const j = index + dir;
  if (j < 0 || j >= segments.length) return segments;
  const next = [...segments];
  const tmp = next[index];
  next[index] = next[j];
  next[j] = tmp;
  return next;
}

export default function NamingTemplateBuilder({ value, onChange, dense }: Props) {
  const parsed = useMemo(() => parseNamingTemplate(value || DEFAULT_NAMING_TEMPLATE), [value]);
  const [advancedOpen, setAdvancedOpen] = useState(() => !parsed.ok);
  const [addPreset, setAddPreset] = useState(SEGMENT_PRESETS[0]?.id ?? "author");

  useEffect(() => {
    if (!parsed.ok) setAdvancedOpen(true);
  }, [parsed.ok]);

  const segments = parsed.ok ? parsed.segments : defaultSegments();
  const template = parsed.ok
    ? serializeNamingTemplate(segments)
    : (value || DEFAULT_NAMING_TEMPLATE).trim() || DEFAULT_NAMING_TEMPLATE;
  const example = previewExamplePath(template);

  const commitSegments = (next: NamingSegment[]) => {
    onChange(serializeNamingTemplate(next));
  };

  const addSegment = () => {
    const preset = SEGMENT_PRESETS.find((p) => p.id === addPreset) || SEGMENT_PRESETS[0];
    const seg = segmentFromPreset(preset.template);
    if (!seg) return;
    commitSegments([...segments, seg]);
  };

  return (
    <div className={`space-y-2 ${dense ? "" : ""}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs text-gray-400">Folder Forge path builder</span>
        <button
          type="button"
          className="inline-flex items-center gap-1 text-[11px] text-gray-400 hover:text-gray-200"
          onClick={() => onChange(DEFAULT_NAMING_TEMPLATE)}
          title="Reset to default"
        >
          <RotateCcw size={12} />
          Default
        </button>
      </div>

      {!parsed.ok && (
        <p className="text-[11px] text-amber-400/90 rounded-lg border border-amber-900/40 bg-amber-950/30 px-2.5 py-1.5">
          Custom template couldn&apos;t be parsed into the builder ({parsed.reason}). Edit raw
          below, or reset to default.
        </p>
      )}

      {parsed.ok && (
        <div className="space-y-2">
          <ol className="space-y-1.5">
            {segments.map((seg, i) => (
              <li
                key={`${i}-${serializeNamingTemplate([seg])}`}
                className="flex items-center gap-1.5 rounded-lg border border-gray-800 bg-gray-950/60 px-2 py-1.5"
              >
                <span className="text-[10px] text-gray-600 w-4 tabular-nums shrink-0">
                  {i + 1}
                </span>
                <div className="flex flex-wrap items-center gap-1 min-w-0 flex-1">
                  {seg.parts.map((p, pi) =>
                    p.kind === "token" ? (
                      <span
                        key={pi}
                        className="inline-flex items-center px-2 py-0.5 rounded-md bg-brand-900/40 border border-brand-700/40 text-[11px] text-brand-100"
                      >
                        {humanSegmentLabel({ parts: [p] })}
                      </span>
                    ) : (
                      <span key={pi} className="text-[11px] text-gray-500 font-mono">
                        {p.text}
                      </span>
                    ),
                  )}
                  {i < segments.length - 1 && (
                    <span className="text-[10px] text-gray-600 ml-1">/</span>
                  )}
                </div>
                <div className="flex items-center gap-0.5 shrink-0">
                  <button
                    type="button"
                    className="p-1 rounded text-gray-500 hover:text-gray-200 disabled:opacity-30"
                    disabled={i === 0}
                    aria-label="Move segment up"
                    onClick={() => commitSegments(moveSegment(segments, i, -1))}
                  >
                    <ChevronUp size={14} />
                  </button>
                  <button
                    type="button"
                    className="p-1 rounded text-gray-500 hover:text-gray-200 disabled:opacity-30"
                    disabled={i === segments.length - 1}
                    aria-label="Move segment down"
                    onClick={() => commitSegments(moveSegment(segments, i, 1))}
                  >
                    <ChevronDown size={14} />
                  </button>
                  <button
                    type="button"
                    className="p-1 rounded text-gray-500 hover:text-red-400 disabled:opacity-30"
                    disabled={segments.length <= 1}
                    aria-label="Remove segment"
                    onClick={() => commitSegments(segments.filter((_, j) => j !== i))}
                  >
                    <X size={14} />
                  </button>
                </div>
              </li>
            ))}
          </ol>

          <div className="flex flex-wrap items-center gap-2">
            <select
              className="rounded-lg border border-gray-700 bg-gray-950 px-2 py-1.5 text-xs text-gray-100"
              value={addPreset}
              onChange={(e) => setAddPreset(e.target.value)}
              aria-label="Segment type to add"
            >
              {SEGMENT_PRESETS.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border border-gray-700 text-xs text-gray-200 hover:bg-gray-800"
              onClick={addSegment}
            >
              <Plus size={14} />
              Add segment
            </button>
          </div>

          <details className="text-[11px] text-gray-500">
            <summary className="cursor-pointer hover:text-gray-300">Available tokens</summary>
            <ul className="mt-1.5 grid gap-0.5 sm:grid-cols-2">
              {NAMING_TOKENS.map((t) => (
                <li key={t.id} className="font-mono text-gray-400">
                  {`{${t.id}}`} — {t.label}
                </li>
              ))}
            </ul>
          </details>
        </div>
      )}

      <div className="rounded-lg border border-gray-800 bg-gray-950/50 px-2.5 py-2 space-y-1">
        <p className="text-[10px] uppercase tracking-wider text-gray-500">Preview</p>
        <p className="text-xs font-mono text-brand-200/90 break-all">{template}</p>
        <p className="text-[11px] text-gray-400 break-all">
          e.g. <span className="text-gray-300">{example}</span>
        </p>
      </div>

      <details
        open={advancedOpen}
        onToggle={(e) => setAdvancedOpen((e.target as HTMLDetailsElement).open)}
      >
        <summary className="text-xs text-gray-400 cursor-pointer hover:text-gray-200">
          Advanced — raw template
        </summary>
        <input
          type="text"
          className="mt-1.5 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 font-mono"
          value={value}
          placeholder={DEFAULT_NAMING_TEMPLATE}
          onChange={(e) => onChange(e.target.value)}
          spellCheck={false}
          autoComplete="off"
        />
      </details>
    </div>
  );
}
