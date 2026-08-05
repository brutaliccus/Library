import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type MouseEvent,
  type ReactNode,
} from "react";
import { Search, X } from "lucide-react";
import { FLOATING_SEARCH_WRAP } from "./floatingSearchStyles";

type Props = {
  liftForMini: boolean;
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  ariaLabel: string;
  disabled?: boolean;
  /** When set, wraps controls in a form (Browse / Store search). */
  onSubmit?: (e: FormEvent) => void;
  filterSlot?: ReactNode;
};

/** Mobile-only collapsible floating search: red circle → expands left across the viewport. */
export default function MobileExpandableSearch({
  liftForMini,
  value,
  onChange,
  placeholder,
  ariaLabel,
  disabled,
  onSubmit,
  filterSlot,
}: Props) {
  const [expanded, setExpanded] = useState(() => Boolean(value.trim()));
  const inputRef = useRef<HTMLInputElement>(null);
  const prevHadValue = useRef(Boolean(value.trim()));
  const hasValue = Boolean(value.trim());

  // Auto-expand only when a query appears (not when collapsing with an existing query).
  useEffect(() => {
    if (hasValue && !prevHadValue.current) setExpanded(true);
    prevHadValue.current = hasValue;
  }, [hasValue]);

  useEffect(() => {
    if (!expanded || disabled) return;
    const id = window.setTimeout(() => inputRef.current?.focus(), 40);
    return () => window.clearTimeout(id);
  }, [expanded, disabled]);

  useEffect(() => {
    if (!expanded) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setExpanded(false);
        inputRef.current?.blur();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [expanded]);

  const collapse = () => {
    setExpanded(false);
    inputRef.current?.blur();
  };

  const open = () => {
    if (disabled) return;
    setExpanded(true);
  };

  const handleClear = () => {
    onChange("");
    inputRef.current?.focus();
  };

  const handleIconClick = (e: MouseEvent) => {
    if (!expanded) {
      e.preventDefault();
      open();
    }
  };

  const bar = (
    <div className="pointer-events-auto relative max-w-xl mx-auto flex justify-end">
      {/* Closed: no overflow-hidden/border so the FAB + filter ring aren't clipped. */}
      <div
        className={`flex items-center justify-end transition-[width,background-color,box-shadow,border-color,padding] duration-300 ease-out ${
          expanded
            ? "w-full overflow-hidden rounded-full bg-gray-900/90 backdrop-blur-md border border-gray-700/70 shadow-lg shadow-black/40"
            : "w-auto overflow-visible border-0 p-1.5"
        }`}
      >
        <input
          ref={inputRef}
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          disabled={disabled}
          tabIndex={expanded ? 0 : -1}
          className={`min-w-0 bg-transparent text-sm text-gray-100 placeholder:text-gray-500 focus:outline-none disabled:opacity-50 transition-[flex,opacity,padding] duration-300 ease-out ${
            expanded
              ? "flex-1 opacity-100 pl-5 py-3.5 pr-1"
              : "flex-none w-0 opacity-0 p-0 pointer-events-none"
          }`}
          aria-label={ariaLabel}
          aria-hidden={!expanded}
        />

        {expanded && filterSlot}

        {expanded && hasValue && (
          <button
            type="button"
            onClick={handleClear}
            className="shrink-0 p-2.5 rounded-full text-gray-400 hover:text-gray-100 hover:bg-gray-800/80 transition-colors"
            aria-label="Clear search"
          >
            <X size={20} />
          </button>
        )}

        {expanded && !hasValue && (
          <button
            type="button"
            onClick={collapse}
            className="shrink-0 p-2.5 rounded-full text-gray-400 hover:text-gray-100 hover:bg-gray-800/80 transition-colors"
            aria-label="Close search"
          >
            <X size={20} />
          </button>
        )}

        <button
          type={onSubmit && expanded ? "submit" : "button"}
          onClick={handleIconClick}
          disabled={disabled && !expanded}
          className={`relative shrink-0 inline-flex items-center justify-center w-14 h-14 rounded-full bg-brand-600 text-white hover:bg-brand-500 transition-colors shadow-md shadow-brand-900/30 disabled:opacity-50 ${
            !expanded && hasValue
              ? "ring-2 ring-brand-300/80 ring-offset-2 ring-offset-gray-950"
              : ""
          }`}
          aria-label={expanded ? (onSubmit ? "Search" : ariaLabel) : "Open search"}
          title={expanded ? (onSubmit ? "Search" : ariaLabel) : "Open search"}
        >
          <Search size={22} strokeWidth={2.25} />
        </button>
      </div>
    </div>
  );

  return (
    <>
      {expanded && (
        <button
          type="button"
          className="lg:hidden fixed inset-0 z-30 cursor-default bg-black/20"
          aria-label="Dismiss search"
          onClick={collapse}
        />
      )}
      <div className={FLOATING_SEARCH_WRAP(liftForMini)}>
        {onSubmit ? <form onSubmit={onSubmit}>{bar}</form> : bar}
      </div>
    </>
  );
}