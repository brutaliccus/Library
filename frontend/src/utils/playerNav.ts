export function indexOfChapterAtTime(chapters: { start: number }[], t: number): number {
  let idx = 0;
  for (let i = 0; i < chapters.length; i++) {
    if (chapters[i].start <= t) idx = i;
    else break;
  }
  return idx;
}

/** Progress bar scope: current chapter, current track, or whole book. */
export interface PlaybackScope {
  label: string;
  /** Seconds elapsed within this scope */
  position: number;
  /** Total seconds in this scope */
  duration: number;
  /** Global book time (seconds) where this scope begins */
  scopeStart: number;
}

export function playbackScope(
  np: {
    absChapters?: { start: number; end: number | null; title: string }[];
    tracks: { startOffset: number; duration: number; title: string }[];
    totalDuration: number;
  },
  globalTime: number,
  currentTrackIndex: number
): PlaybackScope {
  const ch = np.absChapters;
  if (ch?.length) {
    const idx = indexOfChapterAtTime(ch, globalTime);
    const chapter = ch[idx];
    const start = chapter.start;
    const end =
      chapter.end != null && chapter.end > start
        ? chapter.end
        : idx < ch.length - 1
          ? ch[idx + 1].start
          : np.totalDuration;
    const duration = Math.max(0, end - start);
    return {
      label: chapter.title,
      position: Math.max(0, Math.min(globalTime - start, duration || globalTime - start)),
      duration,
      scopeStart: start,
    };
  }

  const track = np.tracks[currentTrackIndex];
  if (track) {
    const start = track.startOffset ?? 0;
    const trackDur = track.duration > 0 ? track.duration : 0;
    if (trackDur > 0) {
      const local = Math.max(0, globalTime - start);
      return {
        label: track.title || `Track ${currentTrackIndex + 1}`,
        position: Math.min(local, trackDur),
        duration: trackDur,
        scopeStart: start,
      };
    }
    // Single file, no chapter markers — scope is the whole file/book.
    if (np.tracks.length === 1 && np.totalDuration > 0) {
      return {
        label: track.title || "",
        position: globalTime,
        duration: np.totalDuration,
        scopeStart: 0,
      };
    }
  }

  return {
    label: "",
    position: globalTime,
    duration: np.totalDuration,
    scopeStart: 0,
  };
}

/** Convert a scrub position (0–1 within scope) to a global seek time. */
export function seekTimeFromScope(scope: PlaybackScope, fraction: number): number {
  const f = Math.max(0, Math.min(1, fraction));
  if (scope.duration > 0) {
    return scope.scopeStart + f * scope.duration;
  }
  return scope.scopeStart;
}

/** Whether prev/next chapter (or track) navigation should be enabled. */
export function chapterNavAvailability(
  np: {
    absChapters?: { start: number }[] | undefined;
    tracks: { length: number };
  } | null,
  currentTime: number,
  currentTrackIndex: number
): { prev: boolean; next: boolean } {
  if (!np) return { prev: false, next: false };
  const ch = np.absChapters;
  if (ch?.length) {
    const idx = indexOfChapterAtTime(ch, currentTime);
    return {
      prev: idx > 0,
      next: idx < ch.length - 1,
    };
  }
  const n = np.tracks.length;
  return {
    prev: n > 1 && currentTrackIndex > 0,
    next: n > 1 && currentTrackIndex < n - 1,
  };
}

export function currentChapterLabel(
  np: { absChapters?: { start: number; title: string }[] | undefined } | null,
  currentTime: number
): string | null {
  if (!np?.absChapters?.length) return null;
  const i = indexOfChapterAtTime(np.absChapters, currentTime);
  return np.absChapters[i]?.title ?? null;
}
