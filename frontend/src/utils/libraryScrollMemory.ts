/** Persist My Library UI + window scroll across detail navigations (session only). */

export type LibraryTab = "abs" | "streams" | "ebooks" | "downloaded";
export type LibraryTabView = "all" | "genre" | "series" | "author";
export type LibraryMediaFilter = "all" | "audiobooks" | "ebooks";

export interface LibraryScrollMemory {
  tab: LibraryTab;
  absView: LibraryTabView;
  ebookView: LibraryTabView;
  rdView: LibraryTabView;
  mediaFilter: LibraryMediaFilter;
  filterGenre: string;
  filterSeries: string;
  filterAuthor: string;
  searchQuery: string;
  scrollY: number;
}

const STORAGE_KEY = "my-library:scroll-ui";

const TABS: LibraryTab[] = ["abs", "streams", "ebooks", "downloaded"];
const VIEWS: LibraryTabView[] = ["all", "genre", "series", "author"];
const FILTERS: LibraryMediaFilter[] = ["all", "audiobooks", "ebooks"];

function isTab(v: unknown): v is LibraryTab {
  return typeof v === "string" && (TABS as string[]).includes(v);
}
function isView(v: unknown): v is LibraryTabView {
  return typeof v === "string" && (VIEWS as string[]).includes(v);
}
function isMediaFilter(v: unknown): v is LibraryMediaFilter {
  return typeof v === "string" && (FILTERS as string[]).includes(v);
}

export function loadLibraryScrollMemory(): LibraryScrollMemory | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<LibraryScrollMemory>;
    if (!isTab(parsed.tab)) return null;
    return {
      tab: parsed.tab,
      absView: isView(parsed.absView) ? parsed.absView : "all",
      ebookView: isView(parsed.ebookView) ? parsed.ebookView : "all",
      rdView: isView(parsed.rdView) ? parsed.rdView : "all",
      mediaFilter: isMediaFilter(parsed.mediaFilter) ? parsed.mediaFilter : "all",
      filterGenre: typeof parsed.filterGenre === "string" ? parsed.filterGenre : "",
      filterSeries: typeof parsed.filterSeries === "string" ? parsed.filterSeries : "",
      filterAuthor: typeof parsed.filterAuthor === "string" ? parsed.filterAuthor : "",
      searchQuery: typeof parsed.searchQuery === "string" ? parsed.searchQuery : "",
      scrollY: typeof parsed.scrollY === "number" && parsed.scrollY > 0 ? parsed.scrollY : 0,
    };
  } catch {
    return null;
  }
}

export function saveLibraryScrollMemory(state: LibraryScrollMemory): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    /* private mode / quota */
  }
}
