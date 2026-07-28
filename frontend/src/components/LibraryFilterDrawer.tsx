import { useEffect, useState, type ReactNode } from "react";
import { ChevronRight, X } from "lucide-react";

export type BrowseByView = "all" | "genre" | "series" | "author";

export interface LibraryFilterOptions {
  genres: string[];
  series: string[];
  authors: string[];
}

interface Props {
  open: boolean;
  onClose: () => void;
  options: LibraryFilterOptions;
  filterGenre: string;
  filterSeries: string;
  filterAuthor: string;
  onFilterGenre: (value: string) => void;
  onFilterSeries: (value: string) => void;
  onFilterAuthor: (value: string) => void;
  browseBy: BrowseByView;
  onBrowseBy: (value: BrowseByView) => void;
  onClearAll: () => void;
}

type AccordionKey = "browse" | "genre" | "series" | "author";

const BROWSE_OPTIONS: { value: BrowseByView; label: string }[] = [
  { value: "all", label: "All" },
  { value: "genre", label: "By Genre" },
  { value: "series", label: "By Series" },
  { value: "author", label: "By Author" },
];

function AccordionSection({
  id,
  title,
  open,
  onToggle,
  activeHint,
  children,
}: {
  id: AccordionKey;
  title: string;
  open: boolean;
  onToggle: (id: AccordionKey) => void;
  activeHint?: string;
  children: ReactNode;
}) {
  return (
    <div className="border-b border-gray-800 last:border-b-0">
      <button
        type="button"
        onClick={() => onToggle(id)}
        className="w-full flex items-center justify-between gap-2 px-1 py-3 text-left"
        aria-expanded={open}
      >
        <span className="min-w-0">
          <span className="text-sm font-medium text-gray-200">{title}</span>
          {activeHint ? (
            <span className="ml-2 text-xs text-brand-400 truncate">{activeHint}</span>
          ) : null}
        </span>
        <ChevronRight
          size={16}
          className={`shrink-0 text-gray-500 transition-transform duration-150 ${
            open ? "rotate-90" : ""
          }`}
        />
      </button>
      {open && <div className="pb-3 space-y-0.5">{children}</div>}
    </div>
  );
}

function OptionButton({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-full text-left px-3 py-2 text-sm rounded-lg transition-colors ${
        active
          ? "bg-brand-600/20 text-brand-300 font-medium"
          : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
      }`}
    >
      {label}
    </button>
  );
}

export default function LibraryFilterDrawer({
  open,
  onClose,
  options,
  filterGenre,
  filterSeries,
  filterAuthor,
  onFilterGenre,
  onFilterSeries,
  onFilterAuthor,
  browseBy,
  onBrowseBy,
  onClearAll,
}: Props) {
  const [expanded, setExpanded] = useState<Set<AccordionKey>>(() => {
    const initial = new Set<AccordionKey>();
    if (browseBy !== "all") initial.add("browse");
    if (filterGenre) initial.add("genre");
    if (filterSeries) initial.add("series");
    if (filterAuthor) initial.add("author");
    if (initial.size === 0) initial.add("browse");
    return initial;
  });

  useEffect(() => {
    if (open) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    setExpanded((prev) => {
      const next = new Set(prev);
      if (browseBy !== "all") next.add("browse");
      if (filterGenre) next.add("genre");
      if (filterSeries) next.add("series");
      if (filterAuthor) next.add("author");
      return next;
    });
  }, [open, browseBy, filterGenre, filterSeries, filterAuthor]);

  const toggle = (id: AccordionKey) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const hasFilters = Boolean(filterGenre || filterSeries || filterAuthor || browseBy !== "all");
  const browseLabel = BROWSE_OPTIONS.find((o) => o.value === browseBy)?.label;

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[60]">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} aria-hidden />
      <div
        className="absolute left-0 top-0 bottom-0 w-72 max-w-[85vw] bg-gray-900 border-r border-gray-800 overflow-y-auto p-4 pt-[max(1rem,env(safe-area-inset-top,0px))] pb-[max(1rem,env(safe-area-inset-bottom,0px))] pl-[max(1rem,env(safe-area-inset-left,0px))] drawer-slide-in"
        role="dialog"
        aria-modal="true"
        aria-label="Library filters"
      >
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-semibold text-gray-200">Filters</h3>
          <button
            type="button"
            onClick={onClose}
            className="p-1 text-gray-500 hover:text-gray-300 transition-colors"
            aria-label="Close filters"
          >
            <X size={18} />
          </button>
        </div>

        {hasFilters && (
          <button
            type="button"
            onClick={() => {
              onClearAll();
            }}
            className="mb-3 w-full px-3 py-2 text-xs font-medium rounded-lg border border-gray-700 text-gray-300 hover:bg-gray-800 hover:text-gray-100 transition-colors"
          >
            Clear all
          </button>
        )}

        <AccordionSection
          id="browse"
          title="Browse by"
          open={expanded.has("browse")}
          onToggle={toggle}
          activeHint={browseBy !== "all" ? browseLabel : undefined}
        >
          {BROWSE_OPTIONS.map((opt) => (
            <OptionButton
              key={opt.value}
              label={opt.label}
              active={browseBy === opt.value}
              onClick={() => onBrowseBy(opt.value)}
            />
          ))}
        </AccordionSection>

        <AccordionSection
          id="genre"
          title="Genre"
          open={expanded.has("genre")}
          onToggle={toggle}
          activeHint={filterGenre || undefined}
        >
          <OptionButton
            label="All genres"
            active={!filterGenre}
            onClick={() => onFilterGenre("")}
          />
          {options.genres.map((g) => (
            <OptionButton
              key={g}
              label={g}
              active={filterGenre === g}
              onClick={() => onFilterGenre(filterGenre === g ? "" : g)}
            />
          ))}
          {options.genres.length === 0 && (
            <p className="px-3 py-2 text-xs text-gray-500">No genres yet</p>
          )}
        </AccordionSection>

        <AccordionSection
          id="series"
          title="Series"
          open={expanded.has("series")}
          onToggle={toggle}
          activeHint={filterSeries || undefined}
        >
          <OptionButton
            label="All series"
            active={!filterSeries}
            onClick={() => onFilterSeries("")}
          />
          {options.series.map((s) => (
            <OptionButton
              key={s}
              label={s}
              active={filterSeries === s}
              onClick={() => onFilterSeries(filterSeries === s ? "" : s)}
            />
          ))}
          {options.series.length === 0 && (
            <p className="px-3 py-2 text-xs text-gray-500">No series yet</p>
          )}
        </AccordionSection>

        <AccordionSection
          id="author"
          title="Author"
          open={expanded.has("author")}
          onToggle={toggle}
          activeHint={filterAuthor || undefined}
        >
          <OptionButton
            label="All authors"
            active={!filterAuthor}
            onClick={() => onFilterAuthor("")}
          />
          {options.authors.map((a) => (
            <OptionButton
              key={a}
              label={a}
              active={filterAuthor === a}
              onClick={() => onFilterAuthor(filterAuthor === a ? "" : a)}
            />
          ))}
          {options.authors.length === 0 && (
            <p className="px-3 py-2 text-xs text-gray-500">No authors yet</p>
          )}
        </AccordionSection>
      </div>
    </div>
  );
}
