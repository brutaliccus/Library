/**
 * Media session wiring for the audio player, isolated from playback logic:
 * - native handler registration (Android Auto + @capgo media session plugin)
 * - web Media Session API action handlers (lock screen / browser UI)
 * - metadata, playback-state, and position-state sync for both web and native
 *
 * Action callbacks are read through refs so handlers registered once always
 * call the latest playback helpers without re-registering.
 */
import { useEffect, useRef } from "react";
import {
  clearMediaSessionPlayback,
  MEDIA_SKIP_SECONDS,
  toAbsoluteArtworkUrl,
} from "../media/playerMediaSession";
import {
  registerNativeMediaHandlers,
  syncNativeMediaSession,
} from "../media/capacitorMediaSession";
import { playbackScope } from "../utils/playerNav";
import type { NowPlaying, Track, RDResumeInfo } from "../types/player";

export interface PlayerMediaActions {
  togglePlay: () => void;
  /** Explicit play/pause: external controllers (Android Auto, lock screen) must
   * not use toggle semantics — a native/web state desync would invert the action. */
  play: () => void;
  pause: () => void;
  seek: (time: number) => void;
  seekRelative: (delta: number) => void;
  skipChapterPrev: () => void;
  skipChapterNext: () => void;
  dismissPlayer: () => void;
}

export interface PlayerPlayActions {
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
}

interface MediaSessionPlayerState {
  nowPlaying: NowPlaying | null;
  isPlaying: boolean;
  buffering: boolean;
  currentTime: number;
  currentTrackIndex: number;
  playbackRate: number;
}

export function usePlayerMediaSession(
  state: MediaSessionPlayerState,
  actions: PlayerMediaActions,
  playActions: PlayerPlayActions
) {
  const actionsRef = useRef(actions);
  actionsRef.current = actions;
  const playActionsRef = useRef(playActions);
  playActionsRef.current = playActions;

  // Native (Capacitor) handlers: Android Auto browse/play + plugin media session.
  useEffect(() => {
    void registerNativeMediaHandlers(
      {
        togglePlay: () => actionsRef.current.togglePlay(),
        play: () => actionsRef.current.play(),
        pause: () => actionsRef.current.pause(),
        seek: (t) => actionsRef.current.seek(t),
        seekRelative: (d) => actionsRef.current.seekRelative(d),
        skipChapterPrev: () => actionsRef.current.skipChapterPrev(),
        skipChapterNext: () => actionsRef.current.skipChapterNext(),
        dismissPlayer: () => actionsRef.current.dismissPlayer(),
      },
      {
        playABS: (id) => playActionsRef.current.playABS(id),
        playRD: (...args) => playActionsRef.current.playRD(...args),
        play: () => actionsRef.current.play(),
        togglePlay: () => actionsRef.current.togglePlay(),
      }
    );
  }, []);

  // Web Media Session API action handlers (browser / lock screen).
  useEffect(() => {
    if (!("mediaSession" in navigator)) return;

    const nav = navigator.mediaSession;
    const SKIP = MEDIA_SKIP_SECONDS;

    nav.setActionHandler("play", () => {
      actionsRef.current.play();
    });
    nav.setActionHandler("pause", () => {
      actionsRef.current.pause();
    });
    nav.setActionHandler("stop", () => {
      actionsRef.current.dismissPlayer();
    });
    nav.setActionHandler("seekbackward", (d) => {
      const sec =
        d?.seekOffset != null && isFinite(d.seekOffset) ? d.seekOffset : SKIP;
      actionsRef.current.seekRelative(-sec);
    });
    nav.setActionHandler("seekforward", (d) => {
      const sec =
        d?.seekOffset != null && isFinite(d.seekOffset) ? d.seekOffset : SKIP;
      actionsRef.current.seekRelative(sec);
    });
    nav.setActionHandler("previoustrack", () => {
      actionsRef.current.skipChapterPrev();
    });
    nav.setActionHandler("nexttrack", () => {
      actionsRef.current.skipChapterNext();
    });

    nav.setActionHandler("seekto", (ev) => {
      const t = ev.seekTime;
      if (t != null && isFinite(t)) actionsRef.current.seek(t);
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

  // Web metadata (title / artist / artwork).
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

  // Web playback state.
  useEffect(() => {
    if (!("mediaSession" in navigator) || !state.nowPlaying) return;
    navigator.mediaSession.playbackState =
      state.isPlaying && !state.buffering ? "playing" : "paused";
  }, [state.isPlaying, state.buffering, state.nowPlaying]);

  // Web position state (progress bar on lock screens that support it).
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

  // Native (Android Auto + plugin) metadata/state/position sync.
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
}
