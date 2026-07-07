const LEGACY_KEY = "ereader-progress";

function storageKey(): string {
  const username = localStorage.getItem("username") || "default";
  return `ereader-progress-${username}`;
}

export interface ReadingProgress {
  chapterId: number;
  page: number;
  viewportPage: number;
  totalViewportPages?: number;
  totalKavitaPages?: number;
  bookTitle: string;
  seriesName?: string;
  coverUrl: string;
  lastReadAt: number;
  /** Hidden from the Continue Reading shelf (progress preserved) */
  hidden?: boolean;
}

let _migrated = false;
function migrateLegacy() {
  if (_migrated) return;
  _migrated = true;
  try {
    const legacy = localStorage.getItem(LEGACY_KEY);
    if (!legacy) return;
    const key = storageKey();
    if (!localStorage.getItem(key)) {
      localStorage.setItem(key, legacy);
    }
    localStorage.removeItem(LEGACY_KEY);
  } catch { /* ignore */ }
}

function loadAll(): Record<string, ReadingProgress> {
  migrateLegacy();
  try {
    const s = localStorage.getItem(storageKey());
    if (s) {
      const parsed = JSON.parse(s) as Record<string, ReadingProgress>;
      return typeof parsed === "object" && parsed !== null ? parsed : {};
    }
  } catch {
    /* ignore */
  }
  return {};
}

function saveAll(data: Record<string, ReadingProgress>) {
  try {
    localStorage.setItem(storageKey(), JSON.stringify(data));
  } catch {
    /* ignore */
  }
}

export function getProgress(chapterId: number): ReadingProgress | null {
  const all = loadAll();
  return all[String(chapterId)] ?? null;
}

export function saveProgress(progress: Omit<ReadingProgress, "lastReadAt">) {
  const all = loadAll();
  all[String(progress.chapterId)] = {
    ...progress,
    lastReadAt: Date.now(),
    hidden: false, // reading again un-hides it from Continue Reading
  };
  saveAll(all);
  window.dispatchEvent(new Event("ereader-progress-updated"));
}

export function clearProgress(chapterId: number) {
  const all = loadAll();
  delete all[String(chapterId)];
  saveAll(all);
  window.dispatchEvent(new Event("ereader-progress-updated"));
}

/** Hide a book from the Continue Reading shelf without losing its progress. */
export function hideFromContinueReading(chapterId: number) {
  const all = loadAll();
  const entry = all[String(chapterId)];
  if (entry) {
    entry.hidden = true;
    saveAll(all);
    window.dispatchEvent(new Event("ereader-progress-updated"));
  }
}

export function getContinueReading(limit = 6): ReadingProgress[] {
  const all = loadAll();
  return Object.values(all)
    .filter((p) => !p.hidden)
    .sort((a, b) => b.lastReadAt - a.lastReadAt)
    .slice(0, limit);
}
