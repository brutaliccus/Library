/** Shared playback types used by PlayerContext and its hooks. */

export interface Track {
  index: number;
  startOffset: number;
  duration: number;
  title: string;
  contentUrl: string;
  mimeType: string;
}

/** Audiobookshelf chapter markers (seconds from book start); populated for ABS playback when ABS exposes chapters. */
export interface AbsChapter {
  id: number;
  title: string;
  start: number;
  end: number | null;
}

export interface NowPlaying {
  source: "abs" | "rd";
  sessionId?: string;
  itemId?: string;
  streamHistoryId?: number;
  title: string;
  author: string;
  coverUrl: string;
  tracks: Track[];
  totalDuration: number;
  absChapters?: AbsChapter[];
}

export interface RDResumeInfo {
  /** Global progress in seconds (used when track durations are known) */
  startAt?: number;
  /** Track to resume on (authoritative when durations are unknown) */
  trackIndex?: number;
  /** Position within that track, in seconds */
  trackPositionSeconds?: number;
}

/**
 * Last known-good playback position. `key` ties the position to a specific
 * book so progress saves can't cross books when the user switches titles.
 */
export interface PlaybackPosition {
  key: string;
  time: number;
  trackIndex: number;
  trackLocal: number;
}

/** Identity of a playing book, used to pair saved positions with the right title. */
export function npKey(np: NowPlaying): string {
  return np.source === "abs"
    ? `abs:${np.sessionId ?? np.itemId ?? ""}`
    : `rd:${np.streamHistoryId ?? ""}`;
}
