import { useEffect, useState } from "react";
import {
  PRESET_THEMES,
  THEMES,
  normalizeCustomColors,
  readCachedCustomColors,
  type CustomThemeColors,
  type ThemeId,
} from "../theme/themes";

interface Props {
  value: ThemeId | "default" | null;
  onChange: (value: ThemeId | "default", customColors?: CustomThemeColors) => void;
  /** Include "Library default" option for personal preference. */
  allowDefault?: boolean;
  libraryDefaultLabel?: string;
  disabled?: boolean;
  /** When false, hide the Custom option (e.g. library default theme). */
  allowCustom?: boolean;
  /** Controlled custom colors; falls back to localStorage cache. */
  customColors?: CustomThemeColors;
}

export default function ThemePicker({
  value,
  onChange,
  allowDefault = false,
  libraryDefaultLabel = "Library default",
  disabled = false,
  allowCustom = true,
  customColors: controlledColors,
}: Props) {
  const selected =
    value === null || value === undefined ? (allowDefault ? "default" : "ocean") : value;
  const themes = allowCustom ? THEMES : PRESET_THEMES;

  const [draftColors, setDraftColors] = useState<CustomThemeColors>(() =>
    normalizeCustomColors(controlledColors ?? readCachedCustomColors())
  );

  useEffect(() => {
    if (controlledColors) {
      setDraftColors(normalizeCustomColors(controlledColors));
    }
  }, [controlledColors]);

  const updateColor = (key: keyof CustomThemeColors, hex: string) => {
    const next = normalizeCustomColors({ ...draftColors, [key]: hex });
    setDraftColors(next);
    onChange("custom", next);
  };

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2">
        {allowDefault && (
          <button
            type="button"
            disabled={disabled}
            onClick={() => onChange("default")}
            className={`text-left rounded-xl border p-3 transition-colors disabled:opacity-50 ${
              selected === "default"
                ? "border-brand-500 bg-brand-600/15"
                : "border-gray-700 bg-gray-800/40 hover:border-gray-600"
            }`}
          >
            <div className="flex gap-1 mb-2">
              <span className="w-4 h-4 rounded-full bg-gray-600" />
              <span className="w-4 h-4 rounded-full bg-gray-700" />
              <span className="w-4 h-4 rounded-full bg-gray-800" />
            </div>
            <p className="text-xs font-semibold text-gray-100">{libraryDefaultLabel}</p>
            <p className="text-[11px] text-gray-500 mt-0.5">Follow this library’s theme</p>
          </button>
        )}
        {themes.map((t) => {
          const swatches =
            t.id === "custom"
              ? ([draftColors.accent, draftColors.surface, draftColors.background] as [
                  string,
                  string,
                  string,
                ])
              : t.swatches;
          return (
            <button
              key={t.id}
              type="button"
              disabled={disabled}
              onClick={() =>
                onChange(t.id, t.id === "custom" ? draftColors : undefined)
              }
              className={`text-left rounded-xl border p-3 transition-colors disabled:opacity-50 ${
                selected === t.id
                  ? "border-brand-500 bg-brand-600/15"
                  : "border-gray-700 bg-gray-800/40 hover:border-gray-600"
              }`}
            >
              <div className="flex gap-1 mb-2">
                {swatches.map((c) => (
                  <span
                    key={`${t.id}-${c}`}
                    className="w-4 h-4 rounded-full border border-black/20"
                    style={{ backgroundColor: c }}
                  />
                ))}
              </div>
              <p className="text-xs font-semibold text-gray-100">{t.label}</p>
              <p className="text-[11px] text-gray-500 mt-0.5 leading-snug">{t.description}</p>
            </button>
          );
        })}
      </div>

      {allowCustom && selected === "custom" && (
        <div className="rounded-xl border border-gray-700 bg-gray-800/40 p-3 space-y-3">
          <p className="text-xs font-medium text-gray-300">Your colors</p>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            {(
              [
                ["accent", "Accent", draftColors.accent],
                ["surface", "Surface", draftColors.surface],
                ["background", "Background", draftColors.background],
              ] as const
            ).map(([key, label, hex]) => (
              <label key={key} className="block space-y-1.5">
                <span className="text-[11px] text-gray-500">{label}</span>
                <div className="flex items-center gap-2">
                  <input
                    type="color"
                    value={hex}
                    disabled={disabled}
                    onChange={(e) => updateColor(key, e.target.value)}
                    className="h-9 w-11 shrink-0 cursor-pointer rounded-lg border border-gray-600 bg-gray-900 p-0.5 disabled:opacity-50"
                    aria-label={`${label} color`}
                  />
                  <input
                    type="text"
                    value={hex}
                    disabled={disabled}
                    onChange={(e) => updateColor(key, e.target.value)}
                    spellCheck={false}
                    className="flex-1 min-w-0 px-2 py-1.5 bg-gray-900 border border-gray-700 rounded-lg text-xs text-gray-200 font-mono focus:outline-none focus:border-brand-500 disabled:opacity-50"
                    aria-label={`${label} hex`}
                  />
                </div>
              </label>
            ))}
          </div>
          <p className="text-[11px] text-gray-500 leading-relaxed">
            Accent colors buttons and highlights. Surface tints cards and panels. Background fills
            the page.
          </p>
        </div>
      )}
    </div>
  );
}
