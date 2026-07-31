import api from "../api/client";

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
  /** EPUB CFI from foliate-js — required to resume exact position. */
  cfi?: string;
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
  } catch {
    /* ignore */
  }
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

function emitUpdated() {
  window.dispatchEvent(new Event("ereader-progress-updated"));
}

export function getProgress(chapterId: number): ReadingProgress | null {
  const all = loadAll();
  return all[String(chapterId)] ?? null;
}

function toLocalShape(item: {
  chapterId: number;
  page: number;
  viewportPage: number;
  totalViewportPages?: number | null;
  totalKavitaPages?: number | null;
  bookTitle: string;
  seriesName?: string | null;
  coverUrl: string;
  lastReadAt: number;
  hidden?: boolean;
  cfi?: string | null;
}): ReadingProgress {
  return {
    chapterId: item.chapterId,
    page: item.page,
    viewportPage: item.viewportPage,
    totalViewportPages: item.totalViewportPages ?? undefined,
    totalKavitaPages: item.totalKavitaPages ?? undefined,
    bookTitle: item.bookTitle,
    seriesName: item.seriesName ?? undefined,
    coverUrl: item.coverUrl,
    lastReadAt: item.lastReadAt,
    hidden: item.hidden,
    cfi: item.cfi ?? undefined,
  };
}

function mergeProgress(local: ReadingProgress | undefined, server: ReadingProgress): ReadingProgress {
  // Newer stamp wins, but never drop a CFI the other side still has.
  const serverNewer = !local || (server.lastReadAt || 0) >= (local.lastReadAt || 0);
  if (serverNewer) {
    return {
      ...server,
      hidden: false,
      cfi: server.cfi || local?.cfi,
    };
  }
  return {
    ...local!,
    cfi: local!.cfi || server.cfi,
  };
}

type ProgressPayload = {
  chapter_id: number;
  page: number;
  viewport_page: number;
  total_viewport_pages: number | null;
  total_kavita_pages: number | null;
  book_title: string;
  series_name: string | null;
  cover_url: string;
  cfi?: string | null;
  hidden: boolean;
  last_read_at: number;
};

function toServerPayload(p: ReadingProgress): ProgressPayload {
  return {
    chapter_id: p.chapterId,
    page: p.page,
    viewport_page: p.viewportPage,
    total_viewport_pages: p.totalViewportPages ?? null,
    total_kavita_pages: p.totalKavitaPages ?? null,
    book_title: p.bookTitle,
    series_name: p.seriesName ?? null,
    cover_url: p.coverUrl,
    cfi: p.cfi ?? null,
    hidden: false,
    last_read_at: p.lastReadAt,
  };
}

/** Pull server Continue Reading into localStorage (newer wins per chapter). */
export async function hydrateReadingProgressFromServer(): Promise<void> {
  try {
    const { data } = await api.get<{
      items: Array<{
        chapterId: number;
        page: number;
        viewportPage: number;
        totalViewportPages?: number | null;
        totalKavitaPages?: number | null;
        bookTitle: string;
        seriesName?: string | null;
        coverUrl: string;
        lastReadAt: number;
        hidden?: boolean;
        cfi?: string | null;
      }>;
    }>("/library/reading-progress", { params: { limit: 100 } });
    const items = data?.items || [];
    if (!items.length) {
      // Push any local-only rows up once so other devices can see them.
      await pushLocalReadingProgressToServer();
      return;
    }
    const all = loadAll();
    let changed = false;
    for (const raw of items) {
      const server = toLocalShape(raw);
      const key = String(server.chapterId);
      const merged = mergeProgress(all[key], server);
      if (JSON.stringify(all[key]) !== JSON.stringify(merged)) {
        all[key] = merged;
        changed = true;
      }
    }
    if (changed) {
      saveAll(all);
      emitUpdated();
    }
    await pushLocalReadingProgressToServer();
  } catch {
    /* offline / not logged in — keep local */
  }
}

/**
 * Fetch one chapter's progress from the server and merge into localStorage.
 * Used when opening the ereader so CFI is restored even after reinstall.
 */
export async function fetchChapterProgressFromServer(
  chapterId: number
): Promise<ReadingProgress | null> {
  try {
    const { data } = await api.get<{
      items: Array<{
        chapterId: number;
        page: number;
        viewportPage: number;
        totalViewportPages?: number | null;
        totalKavitaPages?: number | null;
        bookTitle: string;
        seriesName?: string | null;
        coverUrl: string;
        lastReadAt: number;
        hidden?: boolean;
        cfi?: string | null;
      }>;
    }>("/library/reading-progress", { params: { limit: 100 } });
    const raw = (data?.items || []).find((i) => i.chapterId === chapterId);
    const local = getProgress(chapterId);
    if (!raw) return local;
    const merged = mergeProgress(local ?? undefined, toLocalShape(raw));
    const all = loadAll();
    all[String(chapterId)] = merged;
    saveAll(all);
    emitUpdated();
    return merged;
  } catch {
    return getProgress(chapterId);
  }
}

async function pushLocalReadingProgressToServer(): Promise<void> {
  const all = loadAll();
  const entries = Object.values(all).filter((p) => !p.hidden);
  for (const p of entries.slice(0, 40)) {
    try {
      await api.put(`/library/reading-progress/${p.chapterId}`, toServerPayload(p));
    } catch {
      /* ignore per-row */
    }
  }
}

export function saveProgress(progress: Omit<ReadingProgress, "lastReadAt">) {
  const all = loadAll();
  const prev = all[String(progress.chapterId)];
  const row: ReadingProgress = {
    ...progress,
    // Keep prior CFI if caller omitted it (PDF saves shouldn't wipe EPUB CFI).
    cfi: progress.cfi ?? prev?.cfi,
    lastReadAt: Date.now(),
    hidden: false,
  };
  all[String(progress.chapterId)] = row;
  saveAll(all);
  emitUpdated();
  void api
    .put(`/library/reading-progress/${progress.chapterId}`, toServerPayload(row))
    .catch(() => {
      /* offline — local kept */
    });
}

export function clearProgress(chapterId: number) {
  const all = loadAll();
  delete all[String(chapterId)];
  saveAll(all);
  emitUpdated();
  void api.delete(`/library/reading-progress/${chapterId}`).catch(() => {});
}

/** Hide a book from the Continue Reading shelf without losing its progress. */
export function hideFromContinueReading(chapterId: number) {
  const all = loadAll();
  const entry = all[String(chapterId)];
  if (entry) {
    entry.hidden = true;
    saveAll(all);
    emitUpdated();
  }
  void api.post(`/library/reading-progress/${chapterId}/hide`).catch(() => {});
}

export function getContinueReading(limit = 6): ReadingProgress[] {
  const all = loadAll();
  return Object.values(all)
    .filter((p) => !p.hidden)
    .sort((a, b) => b.lastReadAt - a.lastReadAt)
    .slice(0, limit);
}
