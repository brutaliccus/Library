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

/**
 * Recalculate startOffset for every track from individual durations.
 * Mutates the tracks array in place. Returns total duration.
 */
export function recalcTrackOffsets<T extends TrackOffset>(tracks: T[]): number {
  let offset = 0;
  for (const t of tracks) {
    t.startOffset = offset;
    offset += t.duration || 0;
  }
  return offset;
}

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
    // Skip unprobed tracks (dur==0) when later tracks have known bounds —
    // otherwise the first zero-duration file swallows every global seek.
    if (dur <= 0) continue;
    const end = start + dur;
    if (t >= start && t < end) {
      return { trackIndex: i, trackLocal: Math.max(0, t - start) };
    }
  }
  // Fallback: last track with a known duration, else last track.
  let last = tracks.length - 1;
  for (let i = tracks.length - 1; i >= 0; i--) {
    if ((tracks[i].duration || 0) > 0) {
      last = i;
      break;
    }
  }
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
    // Clamped local (saved local > current track duration) means the layout
    // expanded (1→N nested m4b split). Never trust the truncated value —
    // remap from book-global when we have it.
    const localOverflow = dur > 0 && local! > dur + 1;
    if (localOverflow && global > 0 && durationsKnown) {
      const mapped = mapGlobalToTrack(tracks, global);
      return { ...mapped, globalSeconds: global };
    }
    // Overflow without a usable global: keep the raw local on the hinted
    // index (Exo/HTML5 will clamp) rather than silently jumping to track end.
    const clampedLocal = dur > 0 ? Math.min(Math.max(0, local!), dur) : Math.max(0, local!);
    const reconstructed = start + (localOverflow ? Math.max(0, local!) : clampedLocal);

    if (durationsKnown) {
      // Hints disagree with global timeline (typical after N→1 merge).
      if (global > 0 && Math.abs(start + clampedLocal - global) > 5) {
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
        trackLocal: localOverflow ? Math.max(0, local!) : clampedLocal,
        globalSeconds: global > 0 ? global : reconstructed,
      };
    }

    // Unknown durations: track index is still useful if in range.
    if (opts.preferTrackHints || global <= 0) {
      return {
        trackIndex: idx!,
        trackLocal: Math.max(0, local!),
        globalSeconds: global > 0 ? global : start + Math.max(0, local!),
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

  // Only map from global when durations are known — otherwise the first
  // zero-duration track would swallow the seek (legacy mapGlobalToTrack).
  if (global > 0 && durationsKnown) {
    const mapped = mapGlobalToTrack(tracks, global);
    return { ...mapped, globalSeconds: global };
  }

  return { trackIndex: 0, trackLocal: global, globalSeconds: global };
}
