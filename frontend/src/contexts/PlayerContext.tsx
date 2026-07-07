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
import { useToast } from "./ToastContext";
import {
  clearMediaSessionPlayback,
  MEDIA_SKIP_SECONDS,
  toAbsoluteArtworkUrl,
} from "../media/playerMediaSession";
import { indexOfChapterAtTime, playbackScope } from "../utils/playerNav";
import {
  cacheBookAudio,
  clearBookCacheForTracks,
  clearAbsBookCache,
  getCachedTrackObjectUrl,
} from "../utils/audioCache";
import { setAudioPlaybackActive, setMediaDownloadThrottled } from "../utils/mediaStorage";
import {
  registerNativeMediaHandlers,
  syncNativeMediaSession,
} from "../media/capacitorMediaSession";

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

interface PlayerState {
  nowPlaying: NowPlaying | null;
  isPlaying: boolean;
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

export interface RDResumeInfo {
  /** Global progress in seconds (used when track durations are known) */
  startAt?: number;
  /** Track to resume on (authoritative when durations are unknown) */
  trackIndex?: number;
  /** Position within that track, in seconds */
  trackPositionSeconds?: number;
}

interface PlayerActions {
  playABS: (itemId: string) => Promise<void>;
  playRD: (
    tracks: Track[],
    title: string,
    author?: string,
    coverUrl?: string,
    streamHistoryId?: number,
    resume?: number | RDResumeInfo
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

const SYNC_INTERVAL = 30_000;

/** Identity of a playing book, used to pair saved positions with the right title. */
function npKey(np: NowPlaying): string {
  return np.source === "abs"
    ? `abs:${np.sessionId ?? np.itemId ?? ""}`
    : `rd:${np.streamHistoryId ?? ""}`;
}

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
  const { toast } = useToast();
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const syncTimer = useRef<ReturnType<typeof setInterval>>();
  const probeAbortRef = useRef<AbortController | null>(null);
  /**
   * The position every progress save reads from. Set to the INTENDED position
   * the moment a track loads/seeks, then refined by real playback ticks.
   * Never derived from a still-buffering audio element (which sits at 0:00 and
   * used to wipe saved progress when the player closed mid-load).
   * `key` ties the position to a specific book so saves can't cross books
   * when the user switches titles mid-play.
   */
  const lastPosRef = useRef<{
    key: string;
    time: number;
    trackIndex: number;
    trackLocal: number;
  } | null>(null);
  /** Seek target (track-local seconds) we're still waiting for the audio element to reach. */
  const pendingSeekRef = useRef<number | null>(null);
  /** Ticks observed below the pending seek target while audibly playing. */
  const staleTickCountRef = useRef(0);
  /** Monotonic id so a slow cache lookup can't set the src of a newer load. */
  const loadSeqRef = useRef(0);
  /** Object URL of the currently playing cached track (revoked on replace). */
  const trackObjectUrlRef = useRef<string | null>(null);
  const [state, setState] = useState<PlayerState>({
    nowPlaying: null,
    isPlaying: false,
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

  /** Returns true when the progress save succeeded (or there was nothing to save). */
  const syncProgress = useCallback(
    async (
      np: NowPlaying,
      time: number,
      trackIndex: number,
      trackLocalTime?: number
    ): Promise<boolean> => {
      if (np.source === "abs" && np.sessionId) {
        try {
          await api.post(`/stream/abs/${np.sessionId}/sync`, {
            currentTime: time,
            duration: np.totalDuration,
          });
          return true;
        } catch {
          return false;
        }
      } else if (np.source === "rd" && np.streamHistoryId) {
        try {
          const allDurationsKnown = np.tracks.every((t) => t.duration > 0);
          await api.post("/stream/rd/history/sync", {
            stream_history_id: np.streamHistoryId,
            progress_seconds: time,
            total_seconds: np.totalDuration || 0,
            current_track_index: trackIndex,
            track_position_seconds:
              trackLocalTime ?? Math.max(0, time - (np.tracks[trackIndex]?.startOffset ?? 0)),
            track_durations: allDurationsKnown ? np.tracks.map((t) => t.duration) : undefined,
          });
          return true;
        } catch {
          return false;
        }
      }
      return true;
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

      audio.pause();
      const seq = ++loadSeqRef.current;

      // Prefer the locally downloaded copy. The service worker can't serve
      // <audio> requests on Android WebView, so read the cache directly.
      void (async () => {
        let src = track.contentUrl;
        let objectUrl: string | null = null;
        try {
          objectUrl = await getCachedTrackObjectUrl(track.contentUrl);
        } catch {
          /* fall back to streaming */
        }
        if (seq !== loadSeqRef.current) {
          if (objectUrl) URL.revokeObjectURL(objectUrl);
          return;
        }
        if (trackObjectUrlRef.current) {
          URL.revokeObjectURL(trackObjectUrlRef.current);
          trackObjectUrlRef.current = null;
        }
        if (objectUrl) {
          src = objectUrl;
          trackObjectUrlRef.current = objectUrl;
        }

        audio.src = src;
        audio.playbackRate = stateRef.current.playbackRate;
        audio.volume = stateRef.current.volume;

        const onLoadedEnough = () => {
          audio.removeEventListener("loadedmetadata", onLoadedEnough);
          audio.removeEventListener("canplay", onLoadedEnough);

          // Capture duration from the audio element for this track
          if (audio.duration && isFinite(audio.duration) && audio.duration > 0) {
            applyTrackDuration(trackIndex, audio.duration);
          }

          if (startTime > 0) {
            try { audio.currentTime = startTime; } catch { /* ignore */ }
          }
          audio.play().catch(() => {});
        };
        audio.addEventListener("loadedmetadata", onLoadedEnough, { once: true });
        if (startTime === 0) {
          audio.play().catch(() => {});
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
          probe.src = t.contentUrl;
        });

      let cursor = 0;
      const worker = async () => {
        // Small head start so playback wins the bandwidth race at startup.
        await new Promise((r) => setTimeout(r, 4_000));
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
      setState((s) => ({ ...s, currentTime: gt }));
    };

    const onPlaying = () =>
      setState((s) => ({ ...s, isPlaying: true, buffering: false }));
    const onPause = () => {
      setState((s) => ({ ...s, isPlaying: false }));
      // Persist on every pause so a later force-close can't lose the position.
      const np = stateRef.current.nowPlaying;
      const pos = lastPosRef.current;
      if (np && pos?.key === npKey(np)) {
        void syncProgress(np, pos.time, pos.trackIndex, pos.trackLocal);
      }
    };
    const onWaiting = () => setState((s) => ({ ...s, buffering: true }));
    const onCanPlay = () => setState((s) => ({ ...s, buffering: false }));

    const onEnded = () => {
      const np = stateRef.current.nowPlaying;
      const ti = stateRef.current.currentTrackIndex;
      if (!np) return;
      if (ti < np.tracks.length - 1) {
        loadTrack(np, ti + 1, 0);
      } else {
        setState((s) => ({ ...s, isPlaying: false }));
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
    audio.addEventListener("ended", onEnded);

    return () => {
      audio.removeEventListener("timeupdate", onTimeUpdate);
      audio.removeEventListener("playing", onPlaying);
      audio.removeEventListener("pause", onPause);
      audio.removeEventListener("waiting", onWaiting);
      audio.removeEventListener("canplay", onCanPlay);
      audio.removeEventListener("ended", onEnded);
    };
  }, [getAudio, globalTime, loadTrack, syncProgress]);

  // Throttle (don't stop) background downloads during sustained buffering so the
  // local cache can still finish while playback uses the network.
  useEffect(() => {
    setAudioPlaybackActive(Boolean(state.nowPlaying));
  }, [state.nowPlaying]);

  useEffect(() => {
    if (!state.buffering) {
      setMediaDownloadThrottled(false);
      return;
    }
    const timer = setTimeout(() => setMediaDownloadThrottled(true), 4_000);
    return () => clearTimeout(timer);
  }, [state.buffering]);

  const stateRef = useRef(state);
  stateRef.current = state;

  useEffect(() => {
    if (state.sleepTimerEndAt == null) return;

    const tick = () => {
      const endAt = stateRef.current.sleepTimerEndAt;
      if (endAt == null) return;
      const now = Date.now();
      if (now >= endAt) {
        getAudio().pause();
        setState((s) => ({
          ...s,
          sleepTimerEndAt: null,
          sleepTimerSecondsRemaining: null,
          sleepTimerPresetMinutes: null,
        }));
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

  useEffect(() => {
    syncTimer.current = setInterval(() => {
      const np = stateRef.current.nowPlaying;
      const pos = lastPosRef.current;
      if (np && pos?.key === npKey(np)) {
        void syncProgress(np, pos.time, pos.trackIndex, pos.trackLocal);
      }
    }, SYNC_INTERVAL);
    return () => clearInterval(syncTimer.current);
  }, [syncProgress]);

  useEffect(() => {
    // sendBeacon can't set Authorization headers, so the beacon endpoints take
    // the access token as a query param instead.
    const handler = (mode: "close" | "sync" = "close") => {
      const np = stateRef.current.nowPlaying;
      const pos = lastPosRef.current;
      if (!np || pos?.key !== npKey(np)) return;
      const token = localStorage.getItem("access_token") || "";
      if (np.source === "abs" && np.sessionId) {
        // "sync" keeps the session alive (app merely backgrounded and may keep
        // playing); "close" is for real page unloads.
        const endpoint = mode === "close" ? "close-beacon" : "sync-beacon";
        navigator.sendBeacon?.(
          `/api/stream/abs/${np.sessionId}/${endpoint}?token=${encodeURIComponent(token)}`,
          new Blob(
            [JSON.stringify({ currentTime: pos.time, duration: np.totalDuration })],
            { type: "application/json" }
          )
        );
      } else if (np.source === "rd" && np.streamHistoryId) {
        navigator.sendBeacon?.(
          `/api/stream/rd/history/sync-beacon?token=${encodeURIComponent(token)}`,
          new Blob(
            [
              JSON.stringify({
                stream_history_id: np.streamHistoryId,
                progress_seconds: pos.time,
                total_seconds: np.totalDuration || 0,
                current_track_index: pos.trackIndex,
                track_position_seconds: Math.max(0, pos.trackLocal),
              }),
            ],
            { type: "application/json" }
          )
        );
      }
    };
    const onUnload = () => handler("close");
    // visibilitychange->hidden is the only signal that reliably fires before
    // Android kills a backgrounded app; pagehide/beforeunload often never run.
    // Use "sync" so backgrounded playback keeps its ABS session alive.
    const onVisibility = () => {
      if (document.visibilityState === "hidden") handler("sync");
    };
    window.addEventListener("beforeunload", onUnload);
    window.addEventListener("pagehide", onUnload);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("beforeunload", onUnload);
      window.removeEventListener("pagehide", onUnload);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  const playABS = useCallback(
    async (itemId: string) => {
      probeAbortRef.current?.abort();
      // Wake storage / ABS ahead of the play handshake (fire-and-forget).
      void api.post(`/stream/abs/${itemId}/warmup`).catch(() => {});

      const { data } = await api.post(`/stream/abs/${itemId}/play`);

      const np: NowPlaying = {
        source: "abs",
        sessionId: data.sessionId,
        itemId,
        title: data.title,
        author: data.author,
        coverUrl: data.coverUrl,
        tracks: data.tracks,
        totalDuration: data.duration,
      };
      setState((s) => ({
        ...s,
        nowPlaying: np,
        currentTime: data.startOffset || 0,
        duration: data.duration,
        currentTrackIndex: 0,
        expanded: false,
      }));

      const startOffset = data.startOffset || 0;
      let trackIdx = 0;
      let localStart = startOffset;
      for (let i = 0; i < np.tracks.length; i++) {
        const t = np.tracks[i];
        if (startOffset >= t.startOffset && startOffset < t.startOffset + t.duration) {
          trackIdx = i;
          localStart = startOffset - t.startOffset;
          break;
        }
      }
      loadTrack(np, trackIdx, localStart);

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
          setState((s) => {
            if (s.nowPlaying?.source !== "abs" || s.nowPlaying.itemId !== itemId) return s;
            return { ...s, nowPlaying: { ...s.nowPlaying, absChapters } };
          });
        })
        .catch(() => {});
    },
    [loadTrack]
  );

  const playRD = useCallback(
    (
      tracks: Track[],
      title: string,
      author?: string,
      coverUrl?: string,
      streamHistoryId?: number,
      resume: number | RDResumeInfo = 0
    ) => {
      probeAbortRef.current?.abort();
      const tracksCopy = tracks.map((t) => ({ ...t }));
      const totalDuration = recalcOffsets(tracksCopy);
      const np: NowPlaying = {
        source: "rd",
        streamHistoryId,
        title,
        author: author || "",
        coverUrl: coverUrl || "",
        tracks: tracksCopy,
        totalDuration,
      };

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

      // Probe remaining tracks in the background to discover durations
      probeAllTracks(tracksCopy, trackIdx);

      // Download the whole book to local storage while it streams, so
      // pausing and resuming later never has to touch the debrid service.
      void cacheBookAudio(tracksCopy);
    },
    [loadTrack, probeAllTracks]
  );

  const togglePlay = useCallback(() => {
    const audio = getAudio();
    if (audio.paused) {
      audio.play().catch(() => {});
    } else {
      audio.pause();
    }
  }, [getAudio]);

  // Explicit play/pause for external controllers (Android Auto, lock screen).
  // Toggle semantics there are dangerous: if native and web state disagree,
  // "pause" would start playback.
  const play = useCallback(() => {
    getAudio().play().catch(() => {});
  }, [getAudio]);

  const pause = useCallback(() => {
    getAudio().pause();
  }, [getAudio]);

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

  const persistPlaybackProgress = useCallback(
    async (np: NowPlaying, time: number, trackIndex: number, trackLocalTime?: number) => {
      // This is the last chance to save the listening position — retry once and
      // tell the user if it still fails instead of silently losing progress.
      let ok = await syncProgress(np, time, trackIndex, trackLocalTime);
      if (!ok) {
        await new Promise((r) => setTimeout(r, 1000));
        ok = await syncProgress(np, time, trackIndex, trackLocalTime);
      }
      if (!ok) {
        toast("Couldn't save your listening progress — check your connection", "error");
      }
      if (np.source === "abs" && np.sessionId) {
        try {
          await api.post(`/stream/abs/${np.sessionId}/close`, {
            currentTime: time,
            duration: np.totalDuration,
          });
        } catch {
          /* progress already saved via sync above */
        }
      }
    },
    [syncProgress, toast]
  );

  const dismissPlayer = useCallback(() => {
    probeAbortRef.current?.abort();
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
  }, [getAudio, persistPlaybackProgress]);

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
      if (idx < ch.length - 1) seek(ch[idx + 1].start);
      return;
    }
    const ti = stateRef.current.currentTrackIndex;
    if (np.tracks.length > 1 && ti < np.tracks.length - 1) jumpToTrack(ti + 1);
  }, [seek, jumpToTrack]);

  /** Ref so Media Session action handlers always call latest playback helpers */
  const mediaActionsRef = useRef({
    togglePlay,
    play,
    pause,
    seek,
    seekRelative,
    skipChapterPrev,
    skipChapterNext,
    dismissPlayer,
  });
  const playActionsRef = useRef({ playABS, playRD });
  mediaActionsRef.current = {
    togglePlay,
    play,
    pause,
    seek,
    seekRelative,
    skipChapterPrev,
    skipChapterNext,
    dismissPlayer,
  };
  playActionsRef.current = { playABS, playRD };

  useEffect(() => {
    void registerNativeMediaHandlers(
      {
        togglePlay: () => mediaActionsRef.current.togglePlay(),
        play: () => mediaActionsRef.current.play(),
        pause: () => mediaActionsRef.current.pause(),
        seek: (t) => mediaActionsRef.current.seek(t),
        seekRelative: (d) => mediaActionsRef.current.seekRelative(d),
        skipChapterPrev: () => mediaActionsRef.current.skipChapterPrev(),
        skipChapterNext: () => mediaActionsRef.current.skipChapterNext(),
        dismissPlayer: () => mediaActionsRef.current.dismissPlayer(),
      },
      {
        playABS: (id) => playActionsRef.current.playABS(id),
        playRD: (...args) => playActionsRef.current.playRD(...args),
        togglePlay: () => mediaActionsRef.current.togglePlay(),
      }
    );
  }, []);

  useEffect(() => {
    if (!("mediaSession" in navigator)) return;

    const nav = navigator.mediaSession;
    const SKIP = MEDIA_SKIP_SECONDS;

    nav.setActionHandler("play", () => {
      mediaActionsRef.current.play();
    });
    nav.setActionHandler("pause", () => {
      mediaActionsRef.current.pause();
    });
    nav.setActionHandler("stop", () => {
      mediaActionsRef.current.dismissPlayer();
    });
    nav.setActionHandler("seekbackward", (d) => {
      const sec =
        d?.seekOffset != null && isFinite(d.seekOffset) ? d.seekOffset : SKIP;
      mediaActionsRef.current.seekRelative(-sec);
    });
    nav.setActionHandler("seekforward", (d) => {
      const sec =
        d?.seekOffset != null && isFinite(d.seekOffset) ? d.seekOffset : SKIP;
      mediaActionsRef.current.seekRelative(sec);
    });
    nav.setActionHandler("previoustrack", () => {
      mediaActionsRef.current.skipChapterPrev();
    });
    nav.setActionHandler("nexttrack", () => {
      mediaActionsRef.current.skipChapterNext();
    });

    nav.setActionHandler("seekto", (ev) => {
      const t = ev.seekTime;
      if (t != null && isFinite(t)) mediaActionsRef.current.seek(t);
    });

    return () => {
      try {
        nav.setActionHandler("play", null);
        nav.setActionHandler("pause", null);
        nav.setActionHandler("stop", null);
        nav.setActionHandler("seekbackward", null);
        nav.setActionHandler("seekforward", null);
        nav.setActionHandler("previoustrack", null);
        nav.setActionHandler("nexttrack", null);
        nav.setActionHandler("seekto", null);
      } catch {
        /* ignore */
      }
    };
  }, []);

  useEffect(() => {
    if (!("mediaSession" in navigator)) return;
    if (!state.nowPlaying) {
      clearMediaSessionPlayback();
      return;
    }

    const np = state.nowPlaying;
    const scope = playbackScope(np, state.currentTime, state.currentTrackIndex);
    const trackLabel =
      np.tracks.length > 1 && np.tracks[state.currentTrackIndex]?.title
        ? np.tracks[state.currentTrackIndex].title
        : "";

    const artUrl = toAbsoluteArtworkUrl(np.coverUrl);
    const artwork: MediaImage[] = artUrl
      ? [
          { src: artUrl, sizes: "512x512", type: "image/jpeg" },
          { src: artUrl, sizes: "192x192", type: "image/jpeg" },
        ]
      : [];

    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: scope.label || np.title,
        artist: np.author || "Audiobook",
        album: trackLabel || np.title,
        artwork,
      });
    } catch {
      /* invalid artwork URL etc */
    }
  }, [state.nowPlaying, state.currentTrackIndex, state.currentTime]);

  useEffect(() => {
    if (!("mediaSession" in navigator) || !state.nowPlaying) return;
    navigator.mediaSession.playbackState =
      state.isPlaying && !state.buffering ? "playing" : "paused";
  }, [state.isPlaying, state.buffering, state.nowPlaying]);

  useEffect(() => {
    if (!("mediaSession" in navigator) || !state.nowPlaying) return;
    const ms = navigator.mediaSession as MediaSession & {
      setPositionState?: (state: MediaPositionState | null) => void;
    };
    if (typeof ms.setPositionState !== "function") return;

    const scope = playbackScope(
      state.nowPlaying,
      state.currentTime,
      state.currentTrackIndex
    );
    const d = scope.duration;
    const pos = scope.position;
    if (!isFinite(d) || d <= 0) return;

    try {
      ms.setPositionState({
        duration: d,
        playbackRate: Math.max(state.playbackRate, 0.25),
        position: Math.min(Math.max(pos, 0), d),
      });
    } catch {
      /* invalid combo during seeks */
    }
  }, [
    state.currentTime,
    state.currentTrackIndex,
    state.playbackRate,
    state.nowPlaying,
    state.isPlaying,
  ]);

  useEffect(() => {
    void syncNativeMediaSession(
      state.nowPlaying,
      state.isPlaying,
      state.currentTime,
      state.currentTrackIndex,
      state.playbackRate,
      state.buffering
    );
  }, [
    state.nowPlaying,
    state.isPlaying,
    state.currentTime,
    state.currentTrackIndex,
    state.playbackRate,
    state.buffering,
  ]);

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
