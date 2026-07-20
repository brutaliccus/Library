import { THEMES, type ThemeId } from "../theme/themes";

interface Props {
  value: ThemeId | "default" | null;
  onChange: (value: ThemeId | "default") => void;
  /** Include "Library default" option for personal preference. */
  allowDefault?: boolean;
  libraryDefaultLabel?: string;
  disabled?: boolean;
}

export default function ThemePicker({
  value,
  onChange,
  allowDefault = false,
  libraryDefaultLabel = "Library default",
  disabled = false,
}: Props) {
  const selected = value === null || value === undefined ? (allowDefault ? "default" : "ocean") : value;

  return (
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
      {THEMES.map((t) => (
        <button
          key={t.id}
          type="button"
          disabled={disabled}
          onClick={() => onChange(t.id)}
          className={`text-left rounded-xl border p-3 transition-colors disabled:opacity-50 ${
            selected === t.id
              ? "border-brand-500 bg-brand-600/15"
              : "border-gray-700 bg-gray-800/40 hover:border-gray-600"
          }`}
        >
          <div className="flex gap-1 mb-2">
            {t.swatches.map((c) => (
              <span
                key={c}
                className="w-4 h-4 rounded-full border border-black/20"
                style={{ backgroundColor: c }}
              />
            ))}
          </div>
          <p className="text-xs font-semibold text-gray-100">{t.label}</p>
          <p className="text-[11px] text-gray-500 mt-0.5 leading-snug">{t.description}</p>
        </button>
      ))}
    </div>
  );
}
