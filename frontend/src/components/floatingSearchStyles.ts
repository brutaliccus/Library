/** Shared mobile floating search-bar layout (Browse + My Library). */
export const FLOATING_SEARCH_WRAP = (liftForMini: boolean) =>
  `lg:hidden z-40 fixed left-0 right-0 px-4 pointer-events-none ${
    liftForMini
      ? "bottom-[calc(5rem+0.75rem+env(safe-area-inset-bottom,0px))]"
      : "bottom-[calc(0.75rem+env(safe-area-inset-bottom,0px))]"
  }`;

/** Inline filter control for expandable mobile search (flex row, not absolute). */
export const FLOATING_SEARCH_FILTER =
  "shrink-0 inline-flex items-center justify-center gap-0.5 p-2.5 rounded-full text-gray-400 hover:text-gray-100 hover:bg-gray-800/80 transition-colors";
