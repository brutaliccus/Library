/**
 * Pick resume position: online prefers server; offline prefers local.
 * When both exist online, prefer the newer ``updatedAt`` (ms).
 */
export function pickResumeSeconds(opts: {
  serverSeconds: number;
  serverUpdatedAtMs?: number | null;
  localSeconds?: number | null;
  localUpdatedAtMs?: number | null;
  offline?: boolean;
}): number {
  const server = Math.max(0, opts.serverSeconds || 0);
  const local = opts.localSeconds != null ? Math.max(0, opts.localSeconds) : null;
  if (local == null) return server;
  if (opts.offline) return local;

  const serverTs = opts.serverUpdatedAtMs ?? 0;
  const localTs = opts.localUpdatedAtMs ?? 0;
  if (localTs > serverTs + 5_000) return local;
  if (serverTs > localTs + 5_000) return server;
  // Same-ish timestamps: take the farther position (more progress).
  return Math.max(server, local);
}

export type TrackOffset = {
  startOffset: number;
  duration: number;
};

/** Map a book-global timestamp onto the current track list. */
export function mapGlobalToTrack(
  tracks: TrackOffset[],
  globalSeconds: number
): { trackIndex: number; trackLocal: number } {
  const t = Math.max(0, globalSeconds || 0);
  if (!tracks.length) return { trackIndex: 0, trackLocal: t };
  for (let i = 0; i < tracks.length; i++) {
    const start = tracks[i].startOffset || 0;
    const dur = tracks[i].duration || 0;
    const end = dur > 0 ? start + dur : Number.POSITIVE_INFINITY;
    if (t >= start && t < end) {
      return { trackIndex: i, trackLocal: Math.max(0, t - start) };
    }
  }
  const last = tracks.length - 1;
  const start = tracks[last].startOffset || 0;
  const dur = tracks[last].duration || 0;
  const local = Math.max(0, t - start);
  return {
    trackIndex: last,
    trackLocal: dur > 0 ? Math.min(local, dur) : local,
  };
}

/**
 * Resolve resume track + local offset against the *current* track layout.
 *
 * When the playlist collapses (multi-file → one m4b) or expands (nested m4b
 * removed), stale ``trackIndex``/``trackLocal`` must not win over the book-global
 * timeline — that jumps listeners backward into a long single file.
 */
export function resolveTrackResume(opts: {
  tracks: TrackOffset[];
  globalSeconds: number;
  trackIndex?: number | null;
  trackLocal?: number | null;
  /** Prefer stored track hints when global is unknown/zero (RD without durations). */
  preferTrackHints?: boolean;
}): { trackIndex: number; trackLocal: number; globalSeconds: number } {
  const tracks = opts.tracks;
  const global = Math.max(0, opts.globalSeconds || 0);
  if (!tracks.length) {
    return { trackIndex: 0, trackLocal: global, globalSeconds: global };
  }

  const idx = opts.trackIndex;
  const local = opts.trackLocal;
  const hintsInRange =
    idx != null &&
    local != null &&
    Number.isFinite(idx) &&
    Number.isFinite(local) &&
    idx >= 0 &&
    idx < tracks.length &&
    local >= 0;

  const durationsKnown = tracks.every((tr) => (tr.duration || 0) > 0);

  // Layout change: saved index no longer exists → always remap from global.
  if (idx != null && Number.isFinite(idx) && (idx < 0 || idx >= tracks.length)) {
    const mapped = mapGlobalToTrack(tracks, global);
    return { ...mapped, globalSeconds: global };
  }

  if (hintsInRange) {
    const t = tracks[idx!];
    const start = t.startOffset || 0;
    const dur = t.duration || 0;
    const clampedLocal = dur > 0 ? Math.min(Math.max(0, local!), dur) : Math.max(0, local!);
    const reconstructed = start + clampedLocal;

    if (durationsKnown) {
      // Hints disagree with global timeline (typical after N→1 merge).
      if (global > 0 && Math.abs(reconstructed - global) > 5) {
        const mapped = mapGlobalToTrack(tracks, global);
        return { ...mapped, globalSeconds: global };
      }
      // Single long track: never trust a mid-file local that ignores global.
      if (tracks.length === 1 && global > 0) {
        const mapped = mapGlobalToTrack(tracks, global);
        return { ...mapped, globalSeconds: global };
      }
      return {
        trackIndex: idx!,
        trackLocal: clampedLocal,
        globalSeconds: global > 0 ? global : reconstructed,
      };
    }

    // Unknown durations: track index is still useful if in range.
    if (opts.preferTrackHints || global <= 0) {
      return {
        trackIndex: idx!,
        trackLocal: Math.max(0, local!),
        globalSeconds: global > 0 ? global : reconstructed,
      };
    }
  }

  if (global > 0 && durationsKnown) {
    const mapped = mapGlobalToTrack(tracks, global);
    return { ...mapped, globalSeconds: global };
  }

  if (global > 0 && tracks.length === 1) {
    return { trackIndex: 0, trackLocal: global, globalSeconds: global };
  }

  if (hintsInRange) {
    return {
      trackIndex: idx!,
      trackLocal: Math.max(0, local!),
      globalSeconds: global,
    };
  }

  const mapped = mapGlobalToTrack(tracks, global);
  return { ...mapped, globalSeconds: global };
}
