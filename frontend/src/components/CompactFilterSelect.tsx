import { useEffect, useId, useRef, useState } from "react";
import { Check, ChevronDown, X } from "lucide-react";

interface Props {
  label: string;
  value: string;
  options: string[];
  allLabel: string;
  onChange: (value: string) => void;
  /** Fixed trigger width — does not grow to widest option */
  className?: string;
}

/**
 * Site-styled filter picker (no native &lt;select&gt; / system UI).
 * Fixed-width trigger; options open in a bottom sheet (mobile) or panel (sm+).
 */
export default function CompactFilterSelect({
  label,
  value,
  options,
  allLabel,
  onChange,
  className = "",
}: Props) {
  const [open, setOpen] = useState(false);
  const titleId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const display = value || allLabel;

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`${label}: ${display}`}
        className={`flex items-center gap-1 px-2.5 py-1.5 bg-gray-800 border border-gray-700 rounded-lg text-xs text-gray-200 hover:border-gray-600 transition-colors ${className}`}
      >
        <span className="truncate min-w-0 flex-1 text-left">{display}</span>
        <ChevronDown size={12} className="text-gray-500 shrink-0" />
      </button>

      {open && (
        <div
          className="fixed inset-0 z-[200] flex items-end sm:items-center justify-center p-2 sm:p-4 pt-[max(0.5rem,env(safe-area-inset-top,0px))] pb-[max(0.5rem,env(safe-area-inset-bottom,0px))] px-[max(0.5rem,env(safe-area-inset-left,0px))] sm:pr-[max(1rem,env(safe-area-inset-right,0px))]"
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
        >
          <div
            className="absolute inset-0 bg-black/60"
            onClick={() => setOpen(false)}
            aria-hidden="true"
          />
          <div className="relative w-full max-w-sm rounded-xl border border-gray-700 bg-gray-800 shadow-xl flex flex-col max-h-[min(70vh,calc(100dvh-env(safe-area-inset-top,0px)-env(safe-area-inset-bottom,0px)-2rem))]">
            <div className="flex items-center justify-between px-4 py-3 border-b border-gray-700 shrink-0">
              <h2 id={titleId} className="text-sm font-semibold text-gray-100">
                {label}
              </h2>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="p-1 rounded-lg text-gray-400 hover:bg-gray-700 hover:text-gray-200"
                aria-label="Close"
              >
                <X size={18} />
              </button>
            </div>
            <ul
              role="listbox"
              aria-label={label}
              className="overflow-y-auto min-h-0 flex-1 py-1"
            >
              <li role="option" aria-selected={!value}>
                <button
                  type="button"
                  onClick={() => {
                    onChange("");
                    setOpen(false);
                  }}
                  className={`w-full flex items-center gap-2 px-4 py-2.5 text-sm text-left transition-colors ${
                    !value
                      ? "bg-brand-600/20 text-brand-300"
                      : "text-gray-300 hover:bg-gray-700/80"
                  }`}
                >
                  <span className="flex-1 truncate">{allLabel}</span>
                  {!value && <Check size={14} className="text-brand-400 shrink-0" />}
                </button>
              </li>
              {options.map((opt) => {
                const selected = value === opt;
                return (
                  <li key={opt} role="option" aria-selected={selected}>
                    <button
                      type="button"
                      onClick={() => {
                        onChange(opt);
                        setOpen(false);
                      }}
                      className={`w-full flex items-center gap-2 px-4 py-2.5 text-sm text-left transition-colors ${
                        selected
                          ? "bg-brand-600/20 text-brand-300"
                          : "text-gray-300 hover:bg-gray-700/80"
                      }`}
                    >
                      <span className="flex-1 truncate">{opt}</span>
                      {selected && <Check size={14} className="text-brand-400 shrink-0" />}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        </div>
      )}
    </>
  );
}
