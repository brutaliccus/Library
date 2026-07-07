import { Capacitor } from "@capacitor/core";
import type { MediaActionHandlers, NowPlayingLike } from "./capacitorMediaSession";
import { playbackScope } from "../utils/playerNav";
import { toAbsoluteArtworkUrl } from "./playerMediaSession";
import { LibraryAuto, type LibraryAutoAction } from "./libraryAutoPlugin";
import {
  handlePlayMediaId,
  startAndroidAutoBrowseListener,
  type AutoPlayHandlers,
} from "./androidAutoBrowse";

export type { LibraryAutoAction, BrowseChild } from "./libraryAutoPlugin";
export { LibraryAuto } from "./libraryAutoPlugin";

let autoHandlersRegistered = false;
let playHandlers: AutoPlayHandlers | null = null;

export async function registerAndroidAutoHandlers(
  handlers: MediaActionHandlers,
  play?: AutoPlayHandlers
): Promise<void> {
  if (Capacitor.getPlatform() !== "android") return;

  try {
    if (play) playHandlers = play;

    await startAndroidAutoBrowseListener();

    if (autoHandlersRegistered) return;

    const SKIP = 15;
    const actions: Array<{
      action: LibraryAutoAction;
      fn: (details?: {
        seekTime?: number | null;
        mediaId?: string;
      }) => void;
    }> = [
      { action: "play", fn: () => handlers.play() },
      { action: "pause", fn: () => handlers.pause() },
      { action: "stop", fn: () => handlers.dismissPlayer() },
      { action: "seekbackward", fn: () => handlers.seekRelative(-SKIP) },
      { action: "seekforward", fn: () => handlers.seekRelative(SKIP) },
      { action: "previoustrack", fn: () => handlers.skipChapterPrev() },
      { action: "nexttrack", fn: () => handlers.skipChapterNext() },
      {
        action: "seekto",
        fn: (d) => {
          const t = d?.seekTime;
          if (t != null && isFinite(t)) handlers.seek(t);
        },
      },
      {
        action: "playmedia",
        fn: (d) => {
          const id = d?.mediaId;
          if (id && playHandlers) void handlePlayMediaId(id, playHandlers);
        },
      },
    ];

    for (const { action, fn } of actions) {
      await LibraryAuto.setActionHandler({ action }, (details) => fn(details));
    }
    autoHandlersRegistered = true;
  } catch (err) {
    console.warn("Android Auto handlers unavailable:", err);
  }
}

export async function syncAndroidAutoPlayback(
  np: NowPlayingLike | null,
  isPlaying: boolean,
  globalTime: number,
  trackIndex: number,
  playbackRate: number
): Promise<void> {
  if (Capacitor.getPlatform() !== "android") return;

  try {
    if (!np) {
      await LibraryAuto.syncPlayback({ active: false, playing: false });
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

    const d = scope.duration;
    const pos = scope.position;

    await LibraryAuto.syncPlayback({
      active: true,
      playing: isPlaying,
      title: scope.label || np.title,
      artist: np.author || "Audiobook",
      album: trackLabel || np.title,
      duration: isFinite(d) && d > 0 ? d : 0,
      position: isFinite(pos) ? Math.max(0, pos) : 0,
      playbackRate: Math.max(playbackRate, 0.25),
      artwork,
    });
  } catch {
    /* plugin unavailable */
  }
}
