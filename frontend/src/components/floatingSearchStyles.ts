/** Shared mobile floating search-bar layout (Browse + My Library). */
export const FLOATING_SEARCH_WRAP = (liftForMini: boolean) =>
  `lg:hidden z-40 fixed left-0 right-0 px-4 pointer-events-none ${
    liftForMini
      ? "bottom-[calc(5rem+0.75rem+env(safe-area-inset-bottom,0px))]"
      : "bottom-[calc(0.75rem+env(safe-area-inset-bottom,0px))]"
  }`;

export const FLOATING_SEARCH_INNER = "pointer-events-auto relative max-w-xl mx-auto";

export const FLOATING_SEARCH_INPUT = (opts?: {
  hasFilter?: boolean;
  disabled?: boolean;
}) =>
  `w-full pl-5 py-3 bg-gray-900/90 backdrop-blur-md border border-gray-700/70 rounded-full text-sm text-gray-100 shadow-lg shadow-black/40 focus:outline-none focus:ring-2 focus:ring-brand-500/80 focus:border-brand-500/50 placeholder:text-gray-500 ${
    opts?.hasFilter ? "pr-[6.5rem]" : "pr-14"
  } ${opts?.disabled ? "disabled:opacity-50" : ""}`;

export const FLOATING_SEARCH_ACTION =
  "absolute right-1.5 top-1/2 -translate-y-1/2 p-2.5 rounded-full bg-brand-600 text-white hover:bg-brand-500 transition-colors shadow-md shadow-brand-900/30";

export const FLOATING_SEARCH_FILTER =
  "absolute right-[3.35rem] top-1/2 -translate-y-1/2 inline-flex items-center justify-center gap-0.5 p-2 rounded-full text-gray-400 hover:text-gray-100 hover:bg-gray-800/80 transition-colors";
