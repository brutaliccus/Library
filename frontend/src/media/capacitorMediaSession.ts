/**
 * Native Android/iOS media session (lock screen + notification controls).
 * WebView on Android does not expose the Media Session Web API — use @capgo plugin.
 */
import { Capacitor } from "@capacitor/core";
import { playbackScope } from "../utils/playerNav";
import { MEDIA_SKIP_SECONDS, toAbsoluteArtworkUrl } from "./playerMediaSession";

interface NowPlayingLike {
  title: string;
  author: string;
  coverUrl: string;
  totalDuration: number;
  absChapters?: { start: number; end: number | null; title: string }[];
  tracks: { startOffset: number; duration: number; title: string }[];
}

export type { NowPlayingLike };

export function isNativeApp(): boolean {
  return Capacitor.isNativePlatform();
}

type MediaSessionPlugin = typeof import("@capgo/capacitor-media-session").MediaSession;

let nativeMs: MediaSessionPlugin | null = null;
let nativeHandlersRegistered = false;
let notificationPermissionRequested = false;

async function ensurePlaybackNotificationPermission(): Promise<void> {
  if (Capacitor.getPlatform() !== "android" || notificationPermissionRequested) return;
  notificationPermissionRequested = true;
  try {
    const { LocalNotifications } = await import("@capacitor/local-notifications");
    const perm = await LocalNotifications.checkPermissions();
    if (perm.display !== "granted") {
      await LocalNotifications.requestPermissions();
    }
  } catch {
    /* plugin unavailable */
  }
}

async function getNativeMediaSession(): Promise<MediaSessionPlugin | null> {
  if (!isNativeApp()) return null;
  if (!nativeMs) {
    const mod = await import("@capgo/capacitor-media-session");
    nativeMs = mod.MediaSession;
  }
  return nativeMs;
}

export interface MediaActionHandlers {
  togglePlay: () => void;
  /** Explicit play/pause so a native/web state desync can't invert the action. */
  play: () => void;
  pause: () => void;
  seek: (time: number) => void;
  seekRelative: (delta: number) => void;
  skipChapterPrev: () => void;
  skipChapterNext: () => void;
  dismissPlayer: () => void;
}

export async function registerNativeMediaHandlers(
  handlers: MediaActionHandlers,
  playHandlers?: import("./androidAutoBrowse").AutoPlayHandlers
): Promise<void> {
  if (Capacitor.getPlatform() === "android") {
    try {
      const { registerAndroidAutoHandlers } = await import("./libraryAuto");
      await registerAndroidAutoHandlers(handlers, playHandlers);
    } catch (err) {
      console.warn("Android Auto registration failed:", err);
    }
    // Phone lock screen + Android Auto use LibraryMediaBrowserService's session.
    return;
  }

  const ms = await getNativeMediaSession();
  if (!ms || nativeHandlersRegistered) return;

  const SKIP = MEDIA_SKIP_SECONDS;
  const actions: Array<{
    action:
      | "play"
      | "pause"
      | "seekbackward"
      | "seekforward"
      | "previoustrack"
      | "nexttrack"
      | "seekto"
      | "stop";
    fn: (details?: { seekTime?: number | null }) => void;
  }> = [
    { action: "play", fn: () => handlers.play() },
    { action: "pause", fn: () => handlers.pause() },
    { action: "stop", fn: () => handlers.dismissPlayer() },
    {
      action: "seekbackward",
      fn: () => handlers.seekRelative(-SKIP),
    },
    {
      action: "seekforward",
      fn: () => handlers.seekRelative(SKIP),
    },
    { action: "previoustrack", fn: () => handlers.skipChapterPrev() },
    { action: "nexttrack", fn: () => handlers.skipChapterNext() },
    {
      action: "seekto",
      fn: (d) => {
        const t = d?.seekTime;
        if (t != null && isFinite(t)) handlers.seek(t);
      },
    },
  ];

  for (const { action, fn } of actions) {
    await ms.setActionHandler({ action }, (details) => fn(details));
  }
  nativeHandlersRegistered = true;
}

export async function syncNativeMediaSession(
  np: NowPlayingLike | null,
  isPlaying: boolean,
  globalTime: number,
  trackIndex: number,
  playbackRate: number,
  buffering = false
): Promise<void> {
  const effectivelyPlaying = isPlaying && !buffering;

  if (Capacitor.getPlatform() === "android") {
    if (np) {
      await ensurePlaybackNotificationPermission();
    }
    const { syncAndroidAutoPlayback } = await import("./libraryAuto");
    await syncAndroidAutoPlayback(
      np,
      effectivelyPlaying,
      globalTime,
      trackIndex,
      playbackRate
    );
    return;
  }

  const ms = await getNativeMediaSession();
  if (!ms) return;

  if (!np) {
    await ms.setPlaybackState({ playbackState: "none" });
    return;
  }

  const scope = playbackScope(np, globalTime, trackIndex);
  const trackLabel =
    np.tracks.length > 1 && np.tracks[trackIndex]?.title
      ? np.tracks[trackIndex].title
      : "";

  const artUrl = toAbsoluteArtworkUrl(np.coverUrl);
  const artwork = artUrl
    ? [
        { src: artUrl, sizes: "512x512", type: "image/jpeg" },
        { src: artUrl, sizes: "192x192", type: "image/jpeg" },
      ]
    : [];

  await ms.setMetadata({
    title: scope.label || np.title,
    artist: np.author || "Audiobook",
    album: trackLabel || np.title,
    artwork,
  });

  await ms.setPlaybackState({
    playbackState: effectivelyPlaying ? "playing" : "paused",
  });

  const d = scope.duration;
  const pos = scope.position;
  if (isFinite(d) && d > 0) {
    await ms.setPositionState({
      duration: d,
      playbackRate: Math.max(playbackRate, 0.25),
      position: Math.min(Math.max(pos, 0), d),
    });
  }
}
