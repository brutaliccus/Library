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
import { saveAaResumeSnapshot } from "./aaResumeSnapshot";

export type { LibraryAutoAction, BrowseChild } from "./libraryAutoPlugin";
export { LibraryAuto } from "./libraryAutoPlugin";

let autoHandlersRegistered = false;
let playHandlers: AutoPlayHandlers | null = null;

let lastMetaKey = "";
let lastPosSyncAt = 0;
const POS_SYNC_INTERVAL_MS = 1_000;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Wake the WebView before transport play so audio.play() isn't rejected while frozen. */
async function withWebViewReady(fn: () => void): Promise<void> {
  try {
    await LibraryAuto.bringToForeground();
    await sleep(350);
  } catch {
    /* native only */
  }
  fn();
}

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
      {
        action: "play",
        fn: () => {
          void withWebViewReady(() => handlers.play());
        },
      },
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
      lastMetaKey = "";
      await LibraryAuto.syncPlayback({ active: false, playing: false });
      return;
    }

    const scope = playbackScope(np, globalTime, trackIndex);
    const trackLabel =
      np.tracks.length > 1 && np.tracks[trackIndex]?.title
        ? np.tracks[trackIndex].title
        : "";

    const d = scope.duration;
    const pos = scope.position;
    const metaKey = `${scope.label}|${np.title}|${np.author}|${np.coverUrl}|${trackIndex}|${trackLabel}`;
    const metaChanged = metaKey !== lastMetaKey;
    const now = Date.now();
    const posDue = now - lastPosSyncAt >= POS_SYNC_INTERVAL_MS;

    if (!metaChanged && !posDue) return;

    if (metaChanged) lastMetaKey = metaKey;
    if (posDue) lastPosSyncAt = now;

    const safePos = isFinite(pos) ? Math.max(0, pos) : 0;
    if ("source" in np && (np.source === "abs" || np.source === "rd")) {
      saveAaResumeSnapshot(
        np as import("../types/player").NowPlaying,
        safePos,
        trackIndex,
        Math.max(0, globalTime - (np.tracks[trackIndex]?.startOffset ?? 0))
      );
    }

    if (metaChanged) {
      const artUrl = toAbsoluteArtworkUrl(np.coverUrl);
      const artwork = artUrl
        ? [
            { src: artUrl, sizes: "512x512", type: "image/jpeg" },
            { src: artUrl, sizes: "192x192", type: "image/jpeg" },
          ]
        : [];

      await LibraryAuto.syncPlayback({
        active: true,
        playing: isPlaying,
        title: scope.label || np.title,
        artist: np.author || "Audiobook",
        album: trackLabel || np.title,
        duration: isFinite(d) && d > 0 ? d : 0,
        position: safePos,
        playbackRate: Math.max(playbackRate, 0.25),
        artwork,
      });
      return;
    }

    await LibraryAuto.syncPlayback({
      active: true,
      playing: isPlaying,
      position: safePos,
      playbackRate: Math.max(playbackRate, 0.25),
      positionOnly: true,
    });
  } catch {
    /* plugin unavailable */
  }
}
