import {
  createContext,
  useContext,
  useRef,
  useState,
  useCallback,
  useEffect,
  type ReactNode,
} from "react";
import api from "../api/client";
import { toAbsoluteUrl } from "../api/instanceUrl";
import { clearMediaSessionPlayback } from "../media/playerMediaSession";
import { indexOfChapterAtTime } from "../utils/playerNav";
import {
  cacheBookAudio,
  clearBookCacheForTracks,
  clearAbsBookCache,
  getCachedTrackObjectUrl,
  isBookCached,
  isTrackFullyCached,
} from "../utils/audioCache";
import {
  setAudioPlaybackActive,
  setMediaDownloadPaused,
  setMediaDownloadThrottled,
} from "../utils/mediaStorage";
import {
  getAbsOfflineManifest,
  getOfflineProgress,
  isLikelyOffline,
  progressKeyForAbs,
  saveAbsOfflineManifest,
  saveRdOfflineManifest,
} from "../utils/offlinePlayback";
import { usePlayerProgressSync } from "../hooks/usePlayerProgressSync";
import { usePlayerMediaSession } from "../hooks/usePlayerMediaSession";
import {
  npKey,
  type AbsChapter,
  type NowPlaying,
  type PlaybackPosition,
  type RDResumeInfo,
  type Track,
} from "../types/player";

export type { Track, AbsChapter, NowPlaying, RDResumeInfo };

/** Capacitor WebView is https://localhost — relative /api/stream URLs must hit the library host. */
function withAbsoluteMediaUrls(np: NowPlaying): NowPlaying {
  return {
    ...np,
    coverUrl: np.coverUrl ? toAbsoluteUrl(np.coverUrl) : np.coverUrl,
    tracks: np.tracks.map((t) => ({
      ...t,
      contentUrl: toAbsoluteUrl(t.contentUrl),
    })),
  };
}

function absolutizeTracks(tracks: Track[]): Track[] {
  return tracks.map((t) => ({ ...t, contentUrl: toAbsoluteUrl(t.contentUrl) }));
}

interface PlayerState {
  nowPlaying: NowPlaying | null;
  isPlaying: boolean;
  /**
   * User/transport wants playback (play pressed, not yet deliberately paused).
   * Media session / AA sync use this so brief audio pauses don't report "paused".
   */
  wantPlaying: boolean;
  currentTime: number;
  duration: number;
  currentTrackIndex: number;
  playbackRate: number;
  volume: number;
  buffering: boolean;
  expanded: boolean;
  /** Wall-clock time (ms) when the sleep timer should pause playback, or null if off */
  sleepTimerEndAt: number | null;
  /** Seconds left until sleep timer fires; updated every second while active (for UI) */
  sleepTimerSecondsRemaining: number | null;
  /** Selected preset (minutes) while the timer is armed; drives UI highlighting */
  sleepTimerPresetMinutes: number | null;
}

interface PlayerActions {
  playABS: (itemId: string) => Promise<void>;
  playRD: (
    tracks: Track[],
    title: string,
    author?: string,
    coverUrl?: string,
    streamHistoryId?: number,
    resume?: number | RDResumeInfo,
    libraryItemId?: number
  ) => void;
  togglePlay: () => void;
  seek: (time: number) => void;
  seekRelative: (delta: number) => void;
  setPlaybackRate: (rate: number) => void;
  setVolume: (vol: number) => void;
  setExpanded: (val: boolean) => void;
  /** Close the player UI and save listening position (does not reset book progress). */
  dismissPlayer: () => void;
  jumpToTrack: (index: number) => void;
  /** Set minutes until playback pauses, or null to cancel */
  setSleepTimer: (minutes: number | null) => void;
  /** Previous chapter (ABS chapter markers) or previous file track */
  skipChapterPrev: () => void;
  /** Next chapter or next file track */
  skipChapterNext: () => void;
}

type PlayerContextType = PlayerState & PlayerActions;

const PlayerContext = createContext<PlayerContextType | null>(null);

export function usePlayer(): PlayerContextType {
  const ctx = useContext(PlayerContext);
  if (!ctx) throw new Error("usePlayer must be inside PlayerProvider");
  return ctx;
}

/** Auto-recovery attempts (audio element errors / stalled loads) per book. */
const MAX_PLAYBACK_RETRIES = 6;
/** Reload the track if buffering makes zero progress for this long. */
const STALL_WATCHDOG_MS = 25_000;

/**
 * Recalculate startOffset for every track and totalDuration from individual durations.
 * Mutates the tracks array in place for performance.
 */
function recalcOffsets(tracks: Track[]): number {
  let offset = 0;
  for (const t of tracks) {
    t.startOffset = offset;
    offset += t.duration;
  }
  return offset;
}

export function PlayerProvider({ children }: { children: ReactNode }) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const probeAbortRef = useRef<AbortController | null>(null);
  /**
   * The position every progress save reads from. Set to the INTENDED position
   * the moment a track loads/seeks, then refined by real playback ticks.
   * Never derived from a still-buffering audio element (which sits at 0:00 and
   * used to wipe saved progress when the player closed mid-load).
   * `key` ties the position to a specific book so saves can't cross books
   * when the user switches titles mid-play.
   */
  const lastPosRef = useRef<PlaybackPosition | null>(null);
  /** Seek target (track-local seconds) we're still waiting for the audio element to reach. */
  const pendingSeekRef = useRef<number | null>(null);
  /** Ticks observed below the pending seek target while audibly playing. */
  const staleTickCountRef = useRef(0);
  /** Monotonic id so a slow cache lookup can't set the src of a newer load. */
  const loadSeqRef = useRef(0);
  /** Object URL of the currently playing cached track (revoked on replace). */
  const trackObjectUrlRef = useRef<string | null>(null);
  /**
   * True while the user wants playback (pressed play and hasn't paused).
   * Gates auto-recovery so an error retry or stall reload never resumes
   * a book the user deliberately paused.
   */
  const playIntentRef = useRef(false);
  /** Auto-recovery budget; reset when playback actually produces sound. */
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** Last playback position + wall time — used by the stall watchdog. */
  const stallWatchRef = useRef<{ time: number; at: number } | null>(null);
  const loadTrackRef = useRef<(np: NowPlaying, trackIndex: number, startTime?: number) => void>(
    () => {}
  );
  /** Ignore pause events fired while we reset the audio element between tracks. */
  const suppressPauseIntentRef = useRef(false);
  /** Resume after a transient system pause while play intent is still true. */
  const resumeAfterTransientPauseRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [state, setState] = useState<PlayerState>({
    nowPlaying: null,
    isPlaying: false,
    wantPlaying: false,
    currentTime: 0,
    duration: 0,
    currentTrackIndex: 0,
    playbackRate: 1,
    volume: 1,
    buffering: false,
    expanded: false,
    sleepTimerEndAt: null,
    sleepTimerSecondsRemaining: null,
    sleepTimerPresetMinutes: null,
  });

  const setPlayIntent = useCallback((want: boolean) => {
    playIntentRef.current = want;
    setState((s) => (s.wantPlaying === want ? s : { ...s, wantPlaying: want }));
  }, []);

  const stateRef = useRef(state);
  stateRef.current = state;

  const getNowPlaying = useCallback(() => stateRef.current.nowPlaying, []);
  const getPosition = useCallback(() => lastPosRef.current, []);
  const { syncProgress, persistPlaybackProgress } = usePlayerProgressSync({
    getNowPlaying,
    getPosition,
  });

  const getAudio = useCallback(() => {
    if (!audioRef.current) {
      audioRef.current = new Audio();
      audioRef.current.preload = "auto";
    }
    return audioRef.current;
  }, []);

  const globalTime = useCallback(
    (trackIndex: number, localTime: number, tracks: Track[]) => {
      if (!tracks.length) return localTime;
      const track = tracks[trackIndex];
      return (track?.startOffset ?? 0) + localTime;
    },
    []
  );

  /**
   * Update a single track's duration and recalculate offsets/totalDuration.
   * Only applies if the track still has duration === 0 (not yet known).
   */
  const applyTrackDuration = useCallback((trackIndex: number, dur: number) => {
    setState((s) => {
      const np = s.nowPlaying;
      if (!np || !np.tracks[trackIndex] || np.tracks[trackIndex].duration > 0) return s;

      const updatedTracks = np.tracks.map((t, i) =>
        i === trackIndex ? { ...t, duration: dur } : { ...t }
      );
      const totalDuration = recalcOffsets(updatedTracks);

      return {
        ...s,
        nowPlaying: { ...np, tracks: updatedTracks, totalDuration },
        duration: totalDuration,
      };
    });
  }, []);

  const loadTrack = useCallback(
    (np: NowPlaying, trackIndex: number, startTime = 0) => {
      const audio = getAudio();
      const track = np.tracks[trackIndex];
      if (!track) return;

      if (retryTimerRef.current) {
        clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }

      // Record the intended position immediately so a progress save that fires
      // while the file is still loading persists where the user SHOULD be, not 0:00.
      lastPosRef.current = {
        key: npKey(np),
        time: (track.startOffset ?? 0) + startTime,
        trackIndex,
        trackLocal: startTime,
      };
      pendingSeekRef.current = startTime > 0 ? startTime : null;
      staleTickCountRef.current = 0;
      playIntentRef.current = true;
      setState((s) => (s.wantPlaying ? s : { ...s, wantPlaying: true }));
      stallWatchRef.current = { time: startTime, at: Date.now() };

      suppressPauseIntentRef.current = true;
      audio.pause();
      const seq = ++loadSeqRef.current;

      if (trackObjectUrlRef.current) {
        URL.revokeObjectURL(trackObjectUrlRef.current);
        trackObjectUrlRef.current = null;
      }

      const beginPlayback = (src: string, objectUrl: string | null) => {
        if (seq !== loadSeqRef.current) {
          if (objectUrl) URL.revokeObjectURL(objectUrl);
          return;
        }
        if (objectUrl) trackObjectUrlRef.current = objectUrl;

        audio.src = src;
        audio.playbackRate = stateRef.current.playbackRate;
        audio.volume = stateRef.current.volume;
        audio.load();

        const tryPlay = () => {
          if (seq !== loadSeqRef.current || !playIntentRef.current) return;
          if (startTime > 0) {
            try {
              audio.currentTime = startTime;
            } catch {
              /* ignore */
            }
          }
          audio.play().catch(() => {});
        };

        const onReady = () => {
          if (audio.duration && isFinite(audio.duration) && audio.duration > 0) {
            applyTrackDuration(trackIndex, audio.duration);
          }
          tryPlay();
        };

        audio.addEventListener("loadedmetadata", onReady, { once: true });
        audio.addEventListener("canplay", tryPlay, { once: true });
      };

      const streamUrl = toAbsoluteUrl(track.contentUrl);

      // Cached books: play from blob (offline). Uncached: stream immediately.
      void (async () => {
        try {
          if (await isTrackFullyCached(streamUrl)) {
            const objectUrl = await getCachedTrackObjectUrl(streamUrl);
            if (objectUrl && seq === loadSeqRef.current) {
              beginPlayback(objectUrl, objectUrl);
              return;
            }
          }
        } catch {
          /* fall through to stream */
        }
        if (seq === loadSeqRef.current) {
          beginPlayback(streamUrl, null);
        }
      })();

      setState((s) => ({
        ...s,
        currentTrackIndex: trackIndex,
        currentTime: (track.startOffset ?? 0) + startTime,
        isPlaying: false,
        buffering: true,
      }));
    },
    [getAudio, applyTrackDuration]
  );

  loadTrackRef.current = loadTrack;

  const schedulePlaybackRetry = useCallback(() => {
    if (!playIntentRef.current) return;
    if (retryCountRef.current >= MAX_PLAYBACK_RETRIES) return;
    const np = stateRef.current.nowPlaying;
    if (!np) return;

    retryCountRef.current += 1;
    const ti = stateRef.current.currentTrackIndex;
    const local = lastPosRef.current?.trackLocal ?? getAudio().currentTime;

    if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
    retryTimerRef.current = setTimeout(() => {
      if (!playIntentRef.current || !stateRef.current.nowPlaying) return;
      loadTrackRef.current(np, ti, Math.max(0, local));
    }, Math.min(8_000, 1_000 * retryCountRef.current));
  }, [getAudio]);

  /**
   * Probe tracks in the background using temporary Audio elements to discover
   * durations. Limited to 2 at a time (probing every track at once opened a
   * connection per file through the debrid proxy and starved playback), and
   * the track that's actively loading is skipped — the main audio element
   * reports its duration itself.
   */
  const probeAllTracks = useCallback(
    (tracks: Track[], excludeIndex = -1) => {
      probeAbortRef.current?.abort();
      const controller = new AbortController();
      probeAbortRef.current = controller;

      const pending = tracks
        .map((t, idx) => ({ t, idx }))
        .filter(({ t, idx }) => t.duration <= 0 && idx !== excludeIndex);
      if (pending.length === 0) return;

      const probeOne = ({ t, idx }: { t: Track; idx: number }) =>
        new Promise<void>((resolve) => {
          const probe = new Audio();
          probe.preload = "metadata";
          let settled = false;

          const cleanup = () => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            probe.removeEventListener("loadedmetadata", onMeta);
            probe.removeEventListener("error", onError);
            probe.src = "";
            resolve();
          };
          const onMeta = () => {
            if (!controller.signal.aborted && probe.duration && isFinite(probe.duration)) {
              applyTrackDuration(idx, probe.duration);
            }
            cleanup();
          };
          const onError = () => cleanup();
          const timer = setTimeout(cleanup, 20_000);

          controller.signal.addEventListener("abort", cleanup, { once: true });
          probe.addEventListener("loadedmetadata", onMeta, { once: true });
          probe.addEventListener("error", onError, { once: true });
          probe.src = toAbsoluteUrl(t.contentUrl);
        });

      let cursor = 0;
      const worker = async () => {
        // Wait until playback has had a long head start — probes compete for bandwidth.
        await new Promise((r) => setTimeout(r, 90_000));
        while (cursor < pending.length && !controller.signal.aborted) {
          await probeOne(pending[cursor++]);
        }
      };
      void worker();
      void worker();
    },
    [applyTrackDuration]
  );

  useEffect(() => {
    const audio = getAudio();

    const onTimeUpdate = () => {
      const np = stateRef.current.nowPlaying;
      const ti = stateRef.current.currentTrackIndex;
      if (!np) return;
      // Ignore ticks from an element that hasn't reached its resume target yet
      // (it reports 0:00 while loading, which would clobber the saved position).
      const pending = pendingSeekRef.current;
      if (pending != null) {
        if (audio.currentTime + 3 < pending) {
          // If audio is audibly playing well below the target, the resume seek
          // was lost (rare). After ~3s of real playback accept the element's
          // position instead of showing a frozen timeline forever.
          if (!audio.paused && ++staleTickCountRef.current >= 12) {
            pendingSeekRef.current = null;
            staleTickCountRef.current = 0;
          } else {
            return;
          }
        } else {
          pendingSeekRef.current = null;
          staleTickCountRef.current = 0;
        }
      }
      const gt = globalTime(ti, audio.currentTime, np.tracks);
      lastPosRef.current = { key: npKey(np), time: gt, trackIndex: ti, trackLocal: audio.currentTime };
      stallWatchRef.current = { time: audio.currentTime, at: Date.now() };
      retryCountRef.current = 0;
      setState((s) => ({ ...s, currentTime: gt }));
    };

    const onPlaying = () => {
      retryCountRef.current = 0;
      suppressPauseIntentRef.current = false;
      if (resumeAfterTransientPauseRef.current) {
        clearTimeout(resumeAfterTransientPauseRef.current);
        resumeAfterTransientPauseRef.current = null;
      }
      setState((s) => ({
        ...s,
        isPlaying: true,
        wantPlaying: true,
        buffering: false,
      }));
    };
    const onPause = () => {
      setState((s) => ({ ...s, isPlaying: false }));
      // Persist on every pause so a later force-close can't lose the position.
      const np = stateRef.current.nowPlaying;
      const pos = lastPosRef.current;
      if (np && pos?.key === npKey(np)) {
        void syncProgress(np, pos.time, pos.trackIndex, pos.trackLocal);
      }
      // Track swaps intentionally pause the element — don't clear play intent.
      if (suppressPauseIntentRef.current) return;
      // Deliberate pause() already cleared intent. Transient system pauses
      // (focus blip / WebView wake) must keep intent and resume, or lock-screen
      // / AA play starts then stops ~0.5s later.
      if (playIntentRef.current) {
        setState((s) => ({ ...s, wantPlaying: true, buffering: true }));
        if (resumeAfterTransientPauseRef.current) {
          clearTimeout(resumeAfterTransientPauseRef.current);
        }
        resumeAfterTransientPauseRef.current = setTimeout(() => {
          resumeAfterTransientPauseRef.current = null;
          if (!playIntentRef.current) return;
          const a = audioRef.current;
          if (a && a.paused && a.src) {
            a.play().catch(() => {});
          }
        }, 250);
        return;
      }
      setState((s) => (s.wantPlaying ? { ...s, wantPlaying: false } : s));
    };
    const onWaiting = () => setState((s) => ({ ...s, buffering: true }));
    const onCanPlay = () => setState((s) => ({ ...s, buffering: false }));
    const onError = () => {
      if (retryCountRef.current >= MAX_PLAYBACK_RETRIES - 1) {
        console.warn("[player] audio error — giving up after retries");
      }
      schedulePlaybackRetry();
    };
    const onStalled = () => {
      setState((s) => ({ ...s, buffering: true }));
    };

    const onEnded = () => {
      const np = stateRef.current.nowPlaying;
      const ti = stateRef.current.currentTrackIndex;
      if (!np) return;
      if (ti < np.tracks.length - 1) {
        playIntentRef.current = true;
        setState((s) => (s.wantPlaying ? s : { ...s, wantPlaying: true }));
        suppressPauseIntentRef.current = true;
        loadTrack(np, ti + 1, 0);
      } else {
        playIntentRef.current = false;
        setState((s) => ({ ...s, isPlaying: false, wantPlaying: false }));
        if (np.source === "abs" && np.sessionId) {
          api
            .post(`/stream/abs/${np.sessionId}/close`, {
              currentTime: np.totalDuration,
              duration: np.totalDuration,
            })
            .catch(() => {});
        }
        // Book finished — the locally downloaded copy is no longer needed.
        if (np.source === "rd") {
          void clearBookCacheForTracks(np.tracks);
        } else if (np.source === "abs" && np.itemId) {
          void clearAbsBookCache(np.itemId);
        }
      }
    };

    audio.addEventListener("timeupdate", onTimeUpdate);
    audio.addEventListener("playing", onPlaying);
    audio.addEventListener("pause", onPause);
    audio.addEventListener("waiting", onWaiting);
    audio.addEventListener("canplay", onCanPlay);
    audio.addEventListener("error", onError);
    audio.addEventListener("stalled", onStalled);
    audio.addEventListener("ended", onEnded);

    return () => {
      audio.removeEventListener("timeupdate", onTimeUpdate);
      audio.removeEventListener("playing", onPlaying);
      audio.removeEventListener("pause", onPause);
      audio.removeEventListener("waiting", onWaiting);
      audio.removeEventListener("canplay", onCanPlay);
      audio.removeEventListener("error", onError);
      audio.removeEventListener("stalled", onStalled);
      audio.removeEventListener("ended", onEnded);
    };
  }, [getAudio, globalTime, loadTrack, syncProgress, schedulePlaybackRetry]);

  // Defer cache only while audio is actively loading or playing — not when paused.
  useEffect(() => {
    setAudioPlaybackActive(state.isPlaying || state.buffering);
  }, [state.isPlaying, state.buffering]);

  useEffect(() => {
    setMediaDownloadPaused(state.buffering);
    if (!state.buffering) {
      setMediaDownloadThrottled(false);
      return;
    }
    const timer = setTimeout(() => setMediaDownloadThrottled(true), 6_000);
    return () => clearTimeout(timer);
  }, [state.buffering]);

  // Reload the track when buffering makes zero progress for too long.
  useEffect(() => {
    if (!state.buffering || !state.nowPlaying) return;

    const id = setInterval(() => {
      if (!playIntentRef.current || !stateRef.current.buffering) return;
      const stall = stallWatchRef.current;
      if (!stall) return;
      if (Date.now() - stall.at < STALL_WATCHDOG_MS) return;
      console.warn("[player] stall watchdog — reloading track");
      schedulePlaybackRetry();
    }, 5_000);

    return () => clearInterval(id);
  }, [state.buffering, state.nowPlaying, schedulePlaybackRetry]);

  useEffect(() => {
    if (state.sleepTimerEndAt == null) return;

    const tick = () => {
      const endAt = stateRef.current.sleepTimerEndAt;
      if (endAt == null) return;
      const now = Date.now();
      if (now >= endAt) {
        playIntentRef.current = false;
        setState((s) => ({
          ...s,
          wantPlaying: false,
          sleepTimerEndAt: null,
          sleepTimerSecondsRemaining: null,
          sleepTimerPresetMinutes: null,
        }));
        getAudio().pause();
        return;
      }
      setState((s) => ({
        ...s,
        sleepTimerSecondsRemaining: Math.ceil((endAt - now) / 1000),
      }));
    };

    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [state.sleepTimerEndAt, getAudio]);

  const playABS = useCallback(
    async (itemId: string) => {
      probeAbortRef.current?.abort();
      retryCountRef.current = 0;
      setPlayIntent(true);

      const startFromManifest = async () => {
        const manifest = getAbsOfflineManifest(itemId);
        if (!manifest?.tracks.length) return false;
        if (!(await isBookCached(manifest.tracks))) return false;

        const local = getOfflineProgress(progressKeyForAbs(itemId));
        const np = withAbsoluteMediaUrls({
          source: "abs",
          // No live ABS session offline — progress stays local until next online play.
          itemId,
          title: manifest.title,
          author: manifest.author,
          coverUrl: manifest.coverUrl,
          tracks: manifest.tracks,
          totalDuration: manifest.totalDuration,
          absChapters: manifest.absChapters,
        });

        const startOffset = local?.time ?? 0;
        let trackIdx = local?.trackIndex ?? 0;
        let localStart = local?.trackLocal ?? 0;
        if (local == null && startOffset > 0) {
          for (let i = 0; i < np.tracks.length; i++) {
            const t = np.tracks[i];
            if (startOffset >= t.startOffset && startOffset < t.startOffset + t.duration) {
              trackIdx = i;
              localStart = startOffset - t.startOffset;
              break;
            }
          }
        } else if (local != null) {
          trackIdx = Math.min(Math.max(0, local.trackIndex), np.tracks.length - 1);
          localStart = Math.max(0, local.trackLocal);
        }

        setState((s) => ({
          ...s,
          nowPlaying: np,
          currentTime: (np.tracks[trackIdx]?.startOffset ?? 0) + localStart,
          duration: np.totalDuration,
          currentTrackIndex: trackIdx,
          expanded: false,
        }));
        loadTrack(np, trackIdx, localStart);
        return true;
      };

      // Prefer cached metadata when already offline — skip a doomed /play round-trip.
      if (isLikelyOffline()) {
        if (await startFromManifest()) return;
        throw new Error("Offline playback unavailable — download this book while online first");
      }

      // Wake storage / ABS ahead of the play handshake (fire-and-forget).
      void api.post(`/stream/abs/${itemId}/warmup`).catch(() => {});

      try {
        const { data } = await api.post(`/stream/abs/${itemId}/play`);

        const np = withAbsoluteMediaUrls({
          source: "abs",
          sessionId: data.sessionId,
          itemId,
          title: data.title,
          author: data.author,
          coverUrl: data.coverUrl,
          tracks: data.tracks,
          totalDuration: data.duration,
        });

        // Prefer local offline progress when it's newer than ABS (listened offline).
        const local = getOfflineProgress(progressKeyForAbs(itemId));
        let startOffset = data.startOffset || 0;
        if (local && local.updatedAt > Date.now() - 7 * 24 * 60 * 60 * 1000 && local.time > startOffset + 5) {
          startOffset = local.time;
        }

        setState((s) => ({
          ...s,
          nowPlaying: np,
          currentTime: startOffset,
          duration: data.duration,
          currentTrackIndex: 0,
          expanded: false,
        }));

        let trackIdx = 0;
        let localStart = startOffset;
        if (local && Math.abs(local.time - startOffset) < 2) {
          trackIdx = Math.min(Math.max(0, local.trackIndex), np.tracks.length - 1);
          localStart = Math.max(0, local.trackLocal);
        } else {
          for (let i = 0; i < np.tracks.length; i++) {
            const t = np.tracks[i];
            if (startOffset >= t.startOffset && startOffset < t.startOffset + t.duration) {
              trackIdx = i;
              localStart = startOffset - t.startOffset;
              break;
            }
          }
        }
        loadTrack(np, trackIdx, localStart);

        saveAbsOfflineManifest({
          itemId,
          title: data.title,
          author: data.author || "",
          coverUrl: data.coverUrl || "",
          tracks: data.tracks,
          totalDuration: data.duration || 0,
        });

        // Download tracks locally while they stream (ABS + debrid)
        void cacheBookAudio(np.tracks);

        // Chapters are optional for playback — load after audio starts so play feels faster.
        void api
          .get<{ chapters: AbsChapter[] }>(`/stream/abs/${itemId}/chapters`)
          .then((chaptersResp) => {
            const rawCh = chaptersResp.data?.chapters;
            if (!Array.isArray(rawCh) || rawCh.length === 0) return;
            const absChapters = rawCh.map((c: unknown, i: number) => {
              const o = c as Record<string, unknown>;
              const endRaw = o.end;
              return {
                id: typeof o.id === "number" ? o.id : i,
                title: String(o.title ?? `Chapter ${i + 1}`),
                start: Number(o.start) || 0,
                end: endRaw != null && endRaw !== "" ? Number(endRaw) : null,
              } satisfies AbsChapter;
            });
            saveAbsOfflineManifest({
              itemId,
              title: data.title,
              author: data.author || "",
              coverUrl: data.coverUrl || "",
              tracks: data.tracks,
              totalDuration: data.duration || 0,
              absChapters,
            });
            setState((s) => {
              if (s.nowPlaying?.source !== "abs" || s.nowPlaying.itemId !== itemId) return s;
              return { ...s, nowPlaying: { ...s.nowPlaying, absChapters } };
            });
          })
          .catch(() => {});
      } catch (err) {
        if (await startFromManifest()) return;
        throw err;
      }
    },
    [loadTrack, setPlayIntent]
  );

  const playRD = useCallback(
    (
      tracks: Track[],
      title: string,
      author?: string,
      coverUrl?: string,
      streamHistoryId?: number,
      resume: number | RDResumeInfo = 0,
      libraryItemId?: number
    ) => {
      probeAbortRef.current?.abort();
      retryCountRef.current = 0;
      setPlayIntent(true);
      const tracksCopy = absolutizeTracks(tracks.map((t) => ({ ...t })));
      const totalDuration = recalcOffsets(tracksCopy);
      const np = withAbsoluteMediaUrls({
        source: "rd",
        streamHistoryId,
        libraryItemId,
        title,
        author: author || "",
        coverUrl: coverUrl || "",
        tracks: tracksCopy,
        totalDuration,
      });

      const info: RDResumeInfo =
        typeof resume === "number" ? { startAt: resume } : resume;
      const startAt = info.startAt ?? 0;
      const durationsKnown = tracksCopy.every((t) => t.duration > 0);

      let trackIdx = 0;
      let localStart = 0;
      if (
        info.trackIndex != null &&
        info.trackIndex >= 0 &&
        info.trackIndex < tracksCopy.length &&
        (!durationsKnown || info.trackPositionSeconds != null)
      ) {
        // Track-based resume: reliable even when track durations are unknown
        // (a global offset alone would land in the wrong file).
        trackIdx = info.trackIndex;
        localStart = Math.max(0, info.trackPositionSeconds ?? 0);
      } else if (startAt > 0 && durationsKnown) {
        for (let i = 0; i < tracksCopy.length; i++) {
          const t = tracksCopy[i];
          if (startAt >= t.startOffset && startAt < t.startOffset + t.duration) {
            trackIdx = i;
            localStart = startAt - t.startOffset;
            break;
          }
        }
      } else if (startAt > 0 && tracksCopy.length === 1) {
        localStart = startAt;
      }

      const initialGlobalTime = durationsKnown
        ? (tracksCopy[trackIdx]?.startOffset ?? 0) + localStart
        : startAt;

      setState((s) => ({
        ...s,
        nowPlaying: np,
        currentTime: initialGlobalTime,
        duration: totalDuration,
        currentTrackIndex: trackIdx,
        expanded: false,
      }));
      loadTrack(np, trackIdx, localStart);
      // With unknown track durations the loadTrack-computed global time is off;
      // keep the saved global progress until real playback ticks refine it.
      lastPosRef.current = {
        key: npKey(np),
        time: initialGlobalTime,
        trackIndex: trackIdx,
        trackLocal: localStart,
      };

      saveRdOfflineManifest({
        libraryItemId,
        streamHistoryId,
        title,
        author: author || "",
        coverUrl: coverUrl || "",
        tracks: tracksCopy,
        totalDuration,
      });

      if (!isLikelyOffline()) {
        // Probe remaining tracks in the background to discover durations
        probeAllTracks(tracksCopy, trackIdx);

        // Download the whole book to local storage while it streams, so
        // pausing and resuming later never has to touch the debrid service.
        void cacheBookAudio(tracksCopy);
      }
    },
    [loadTrack, probeAllTracks, setPlayIntent]
  );

  const togglePlay = useCallback(() => {
    const audio = getAudio();
    if (audio.paused) {
      setPlayIntent(true);
      audio.play().catch(() => {});
    } else {
      setPlayIntent(false);
      audio.pause();
    }
  }, [getAudio, setPlayIntent]);

  // Explicit play/pause for external controllers (Android Auto, lock screen).
  // Toggle semantics there are dangerous: if native and web state disagree,
  // "pause" would start playback.
  const play = useCallback(() => {
    setPlayIntent(true);
    const audio = getAudio();
    const np = stateRef.current.nowPlaying;

    // After car power-cycle the WebView may have no in-memory book — restore
    // from the Android Auto resume snapshot and start that title.
    if (!np) {
      void (async () => {
        try {
          const { loadAaResumeSnapshot } = await import("../media/aaResumeSnapshot");
          const { LibraryAuto } = await import("../media/libraryAuto");
          const snap = loadAaResumeSnapshot();
          if (!snap) return;
          try {
            await LibraryAuto.bringToForeground();
          } catch {
            /* ignore */
          }
          if (snap.source === "abs" && snap.itemId) {
            await playABS(snap.itemId);
          } else if (snap.source === "rd" && snap.streamHistoryId) {
            const { data } = await api.get("/stream/rd/history/in-progress");
            const item = (data?.items ?? []).find(
              (i: { id: number }) => i.id === snap.streamHistoryId
            );
            if (item?.tracks?.length) {
              playRD(
                item.tracks,
                item.title || snap.title,
                item.author || snap.author,
                item.coverUrl || snap.coverUrl,
                item.id,
                {
                  startAt: snap.position,
                  trackIndex: snap.trackIndex,
                  trackPositionSeconds: snap.trackLocal,
                }
              );
            }
          }
        } catch (e) {
          console.warn("[player] AA resume snapshot failed", e);
        }
      })();
      return;
    }

    const reloadAtPosition = () => {
      const ti = stateRef.current.currentTrackIndex;
      const local = lastPosRef.current?.trackLocal ?? audio.currentTime ?? 0;
      suppressPauseIntentRef.current = true;
      loadTrackRef.current(np, ti, Math.max(0, local));
    };

    if (!audio.src || audio.error) {
      reloadAtPosition();
      return;
    }

    const attemptPlay = (retriesLeft: number) => {
      audio.play().catch((err) => {
        const name = err instanceof Error ? err.name : "";
        // Locked / frozen WebView — wake and retry (Android Auto after a call).
        if (name === "NotAllowedError" || retriesLeft > 0) {
          void import("../media/libraryAuto")
            .then(({ LibraryAuto }) => LibraryAuto.bringToForeground())
            .catch(() => {})
            .finally(() => {
              if (retriesLeft > 0) {
                setTimeout(() => attemptPlay(retriesLeft - 1), 450);
              } else {
                reloadAtPosition();
              }
            });
          return;
        }
        reloadAtPosition();
      });
    };
    attemptPlay(5);
  }, [getAudio, playABS, playRD, setPlayIntent]);

  const pause = useCallback(() => {
    if (resumeAfterTransientPauseRef.current) {
      clearTimeout(resumeAfterTransientPauseRef.current);
      resumeAfterTransientPauseRef.current = null;
    }
    setPlayIntent(false);
    getAudio().pause();
  }, [getAudio, setPlayIntent]);

  const seek = useCallback(
    (time: number) => {
      const np = stateRef.current.nowPlaying;
      if (!np) return;
      let trackIdx = 0;
      let localTime = time;
      for (let i = 0; i < np.tracks.length; i++) {
        const t = np.tracks[i];
        if (time >= t.startOffset && time < t.startOffset + t.duration) {
          trackIdx = i;
          localTime = time - t.startOffset;
          break;
        }
      }
      if (trackIdx === stateRef.current.currentTrackIndex) {
        pendingSeekRef.current = null;
        getAudio().currentTime = localTime;
        lastPosRef.current = { key: npKey(np), time, trackIndex: trackIdx, trackLocal: localTime };
      } else {
        loadTrack(np, trackIdx, localTime);
      }
      setState((s) => ({ ...s, currentTime: time }));
    },
    [getAudio, loadTrack]
  );

  const seekRelative = useCallback(
    (delta: number) => {
      const newTime = Math.max(0, stateRef.current.currentTime + delta);
      seek(
        stateRef.current.nowPlaying
          ? Math.min(newTime, stateRef.current.nowPlaying.totalDuration || Infinity)
          : newTime
      );
    },
    [seek]
  );

  const setPlaybackRate = useCallback(
    (rate: number) => {
      getAudio().playbackRate = rate;
      setState((s) => ({ ...s, playbackRate: rate }));
    },
    [getAudio]
  );

  const setVolume = useCallback(
    (vol: number) => {
      getAudio().volume = vol;
      setState((s) => ({ ...s, volume: vol }));
    },
    [getAudio]
  );

  const setExpanded = useCallback((val: boolean) => {
    setState((s) => ({ ...s, expanded: val }));
  }, []);

  const dismissPlayer = useCallback(() => {
    probeAbortRef.current?.abort();
    if (resumeAfterTransientPauseRef.current) {
      clearTimeout(resumeAfterTransientPauseRef.current);
      resumeAfterTransientPauseRef.current = null;
    }
    setPlayIntent(false);
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    const np = stateRef.current.nowPlaying;
    // Snapshot the last known-good position BEFORE tearing anything down.
    // Never read the live audio element here: mid-buffer it reports 0:00.
    const pos = lastPosRef.current;
    getAudio().pause(); // stop sound immediately; teardown finishes after the save

    const teardown = () => {
      const audio = getAudio();
      audio.pause();
      audio.src = "";
      loadSeqRef.current++; // cancel any in-flight cache lookup from loadTrack
      if (trackObjectUrlRef.current) {
        URL.revokeObjectURL(trackObjectUrlRef.current);
        trackObjectUrlRef.current = null;
      }
      lastPosRef.current = null;
      pendingSeekRef.current = null;
      setState((s) => ({
        ...s,
        nowPlaying: null,
        isPlaying: false,
        wantPlaying: false,
        currentTime: 0,
        duration: 0,
        currentTrackIndex: 0,
        expanded: false,
        sleepTimerEndAt: null,
        sleepTimerSecondsRemaining: null,
        sleepTimerPresetMinutes: null,
      }));
      clearMediaSessionPlayback();
    };

    if (!np || pos?.key !== npKey(np)) {
      teardown();
      return;
    }

    void persistPlaybackProgress(np, pos.time, pos.trackIndex, pos.trackLocal).finally(teardown);
  }, [getAudio, persistPlaybackProgress, setPlayIntent]);

  const setSleepTimer = useCallback((minutes: number | null) => {
    if (minutes == null) {
      setState((s) => ({
        ...s,
        sleepTimerEndAt: null,
        sleepTimerSecondsRemaining: null,
        sleepTimerPresetMinutes: null,
      }));
      return;
    }
    const sec = minutes * 60;
    const endAt = Date.now() + sec * 1000;
    setState((s) => ({
      ...s,
      sleepTimerEndAt: endAt,
      sleepTimerSecondsRemaining: sec,
      sleepTimerPresetMinutes: minutes,
    }));
  }, []);

  const jumpToTrack = useCallback(
    (index: number) => {
      const np = stateRef.current.nowPlaying;
      if (!np || !np.tracks[index]) return;
      loadTrack(np, index, 0);
    },
    [loadTrack]
  );

  const skipChapterPrev = useCallback(() => {
    const np = stateRef.current.nowPlaying;
    if (!np) return;
    const t = stateRef.current.currentTime;
    const ch = np.absChapters;
    if (ch?.length) {
      const idx = indexOfChapterAtTime(ch, t);
      seek(idx > 0 ? ch[idx - 1].start : 0);
      return;
    }
    const ti = stateRef.current.currentTrackIndex;
    if (np.tracks.length > 1 && ti > 0) jumpToTrack(ti - 1);
  }, [seek, jumpToTrack]);

  const skipChapterNext = useCallback(() => {
    const np = stateRef.current.nowPlaying;
    if (!np) return;
    const t = stateRef.current.currentTime;
    const ch = np.absChapters;
    if (ch?.length) {
      const idx = indexOfChapterAtTime(ch, t);
      if (idx < ch.length - 1) {
        seek(ch[idx + 1].start);
        return;
      }
      // Last chapter marker but more audio files remain — advance track.
      const ti = stateRef.current.currentTrackIndex;
      if (np.tracks.length > 1 && ti < np.tracks.length - 1) {
        setPlayIntent(true);
        suppressPauseIntentRef.current = true;
        jumpToTrack(ti + 1);
      }
      return;
    }
    const ti = stateRef.current.currentTrackIndex;
    if (np.tracks.length > 1 && ti < np.tracks.length - 1) jumpToTrack(ti + 1);
  }, [seek, jumpToTrack, setPlayIntent]);

  // Lock screen / browser / Android Auto controls and metadata.
  usePlayerMediaSession(
    {
      nowPlaying: state.nowPlaying,
      isPlaying: state.isPlaying,
      wantPlaying: state.wantPlaying,
      buffering: state.buffering,
      currentTime: state.currentTime,
      currentTrackIndex: state.currentTrackIndex,
      playbackRate: state.playbackRate,
    },
    {
      togglePlay,
      play,
      pause,
      seek,
      seekRelative,
      skipChapterPrev,
      skipChapterNext,
      dismissPlayer,
    },
    { playABS, playRD }
  );

  const value: PlayerContextType = {
    ...state,
    playABS,
    playRD,
    togglePlay,
    seek,
    seekRelative,
    setPlaybackRate,
    setVolume,
    setExpanded,
    dismissPlayer,
    jumpToTrack,
    setSleepTimer,
    skipChapterPrev,
    skipChapterNext,
  };

  return (
    <PlayerContext.Provider value={value}>{children}</PlayerContext.Provider>
  );
}
