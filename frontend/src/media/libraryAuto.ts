import { Capacitor } from "@capacitor/core";
import type { MediaActionHandlers, NowPlayingLike } from "./capacitorMediaSession";
import { playbackScope } from "../utils/playerNav";
import { MEDIA_SKIP_SECONDS, toAbsoluteArtworkUrl } from "./playerMediaSession";
import {
  LibraryAuto,
  type LibraryAutoAction,
  type NativePlaybackEvent,
} from "./libraryAutoPlugin";
import {
  handlePlayMediaId,
  prefetchAndroidAutoBrowseCache,
  startAndroidAutoBrowseListener,
  type AutoPlayHandlers,
} from "./androidAutoBrowse";
import { saveAaResumeSnapshot } from "./aaResumeSnapshot";

export type { LibraryAutoAction, BrowseChild } from "./libraryAutoPlugin";
export { LibraryAuto } from "./libraryAutoPlugin";

let autoHandlersRegistered = false;
let playHandlers: AutoPlayHandlers | null = null;
let nativePlaybackListener: { remove: () => Promise<void> } | null = null;

/** True while ExoPlayer owns PCM — WebView must not start a second decoder. */
let nativeOwnsPlayback = false;
let lastNativeEvent: NativePlaybackEvent | null = null;
let attachNativeHandler: ((ev: NativePlaybackEvent) => void) | null = null;

let lastMetaKey = "";
let lastChapterKey = "";
let lastPosSyncAt = 0;
let lastPlayingSynced: boolean | null = null;
/** After AA/lock play, ignore stale playing=false syncs until audio catches up. */
let ignorePausedSyncUntil = 0;
const POS_SYNC_INTERVAL_MS = 1_000;
const PAUSED_SYNC_GRACE_MS = 2_500;
/** Cap Activity wakes — startActivity storms rebooted some OEM devices. */
let lastBringToForegroundAt = 0;
const BRING_TO_FOREGROUND_COOLDOWN_MS = 8_000;

export function isNativePlaybackOwner(): boolean {
  return nativeOwnsPlayback;
}

export function getLastNativePlaybackEvent(): NativePlaybackEvent | null {
  return lastNativeEvent;
}

/** PlayerContext registers to mirror native position into React state without HTML5 play. */
export function setNativePlaybackAttachHandler(
  handler: ((ev: NativePlaybackEvent) => void) | null
): void {
  attachNativeHandler = handler;
}

export async function handOffNativeToWebView(): Promise<void> {
  try {
    await LibraryAuto.handOffNativePlayback();
  } catch {
    /* ignore */
  }
  nativeOwnsPlayback = false;
}

/** Cooldown-guarded Activity wake used by JS retry / AA paths. */
export async function bringToForegroundSafe(): Promise<void> {
  const now = Date.now();
  if (now - lastBringToForegroundAt < BRING_TO_FOREGROUND_COOLDOWN_MS) return;
  lastBringToForegroundAt = now;
  try {
    await LibraryAuto.bringToForeground();
  } catch {
    /* plugin unavailable */
  }
}

function markOptimisticPlaying(): void {
  lastPlayingSynced = true;
  ignorePausedSyncUntil = Date.now() + PAUSED_SYNC_GRACE_MS;
}

/** Wake the WebView before transport play so audio.play() isn't rejected while frozen. */
async function withWebViewReady(fn: () => void): Promise<void> {
  markOptimisticPlaying();
  try {
    // Native soft-wakes WebView timers + sticky-retries play after deep idle.
    // Do not stack another sleep here — that delayed audio.play() and widened
    // the native-focus vs WebView-focus race.
    void bringToForegroundSafe();
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
    // Keep native Continue/Library warm whenever handlers register (app open).
    void prefetchAndroidAutoBrowseCache();

    if (!nativePlaybackListener) {
      nativePlaybackListener = await LibraryAuto.addListener(
        "nativePlayback",
        (ev: NativePlaybackEvent) => {
          nativeOwnsPlayback = ev.nativeOwner === true;
          lastNativeEvent = ev;
          attachNativeHandler?.(ev);
        }
      );
    }

    if (autoHandlersRegistered) return;

    const SKIP = MEDIA_SKIP_SECONDS;
    const actions: Array<{
      action: LibraryAutoAction;
      fn: (details?: {
        seekTime?: number | null;
        mediaId?: string;
        nativeStarted?: boolean;
      }) => void;
    }> = [
      {
        action: "play",
        fn: () => {
          if (nativeOwnsPlayback) return;
          void withWebViewReady(() => handlers.play());
        },
      },
      {
        action: "pause",
        fn: () => {
          ignorePausedSyncUntil = 0;
          lastPlayingSynced = false;
          handlers.pause();
        },
      },
      { action: "stop", fn: () => handlers.dismissPlayer() },
      {
        action: "seekbackward",
        fn: () => {
          if (nativeOwnsPlayback) return;
          handlers.seekRelative(-SKIP);
        },
      },
      {
        action: "seekforward",
        fn: () => {
          if (nativeOwnsPlayback) return;
          handlers.seekRelative(SKIP);
        },
      },
      {
        action: "previoustrack",
        fn: () => {
          if (nativeOwnsPlayback) return;
          handlers.skipChapterPrev();
        },
      },
      {
        action: "nexttrack",
        fn: () => {
          if (nativeOwnsPlayback) return;
          handlers.skipChapterNext();
        },
      },
      {
        action: "seekto",
        fn: (d) => {
          if (nativeOwnsPlayback) return;
          const t = d?.seekTime;
          if (t != null && isFinite(t)) handlers.seek(t);
        },
      },
      {
        action: "playmedia",
        fn: (d) => {
          const id = d?.mediaId;
          const ph = playHandlers;
          // Native ExoPlayer already started — attach UI only, do not loadTrack/blob.
          if (d?.nativeStarted || nativeOwnsPlayback) {
            markOptimisticPlaying();
            if (lastNativeEvent) attachNativeHandler?.(lastNativeEvent);
            return;
          }
          if (id && ph) {
            void withWebViewReady(() => {
              void handlePlayMediaId(id, ph);
            });
          }
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


function aaMediaId(np: NowPlayingLike): string {
  const anyNp = np as {
    source?: string;
    itemId?: string;
    streamHistoryId?: number;
  };
  if (anyNp.source === "abs" && anyNp.itemId) return `play/abs/${anyNp.itemId}`;
  if (anyNp.source === "rd" && anyNp.streamHistoryId != null) {
    return `play/rdhist/${anyNp.streamHistoryId}`;
  }
  return "";
}

export async function syncAndroidAutoPlayback(
  np: NowPlayingLike | null,
  isPlaying: boolean,
  globalTime: number,
  trackIndex: number,
  playbackRate: number
): Promise<void> {
  if (Capacitor.getPlatform() !== "android") return;

  // Native owns PCM — do not push HTML5 paused ticks into MediaSession.
  if (nativeOwnsPlayback) return;

  try {
    if (!np) {
      lastMetaKey = "";
      lastChapterKey = "";
      lastPlayingSynced = null;
      ignorePausedSyncUntil = 0;
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
    // Chapter label changes every few minutes — don't treat that as a full
    // metadata/artwork reload (native decode + MediaSession binder thrash).
    const metaKey = `${np.title}|${np.author}|${np.coverUrl}|${trackIndex}|${trackLabel}|${d}`;
    const chapterKey = scope.label || "";
    const metaChanged = metaKey !== lastMetaKey;
    const now = Date.now();
    // Keep reporting playing while an AA/lock play is settling — React state often
    // still says paused until the audio element fires "playing".
    const reportPlaying =
      isPlaying || (lastPlayingSynced === true && now < ignorePausedSyncUntil);
    if (isPlaying) ignorePausedSyncUntil = 0;
    const playingChanged = lastPlayingSynced !== reportPlaying;
    const posDue = now - lastPosSyncAt >= POS_SYNC_INTERVAL_MS;
    const chapterChanged = chapterKey !== lastChapterKey;

    // Never throttle play/pause — AA button state must track the phone immediately.
    if (!metaChanged && !posDue && !playingChanged && !chapterChanged) return;

    if (metaChanged) lastMetaKey = metaKey;
    if (chapterChanged) lastChapterKey = chapterKey;
    if (posDue || playingChanged || chapterChanged) lastPosSyncAt = now;
    lastPlayingSynced = reportPlaying;

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
        playing: reportPlaying,
        mediaId: aaMediaId(np),
        // Android Auto MediaSession: TITLE (large) + ARTIST (small).
        // Put chapter in artist so the car shows Book / Chapter, not Author / Chapter.
        title: np.title || "Audiobook",
        artist: scope.label || trackLabel || np.author || "",
        album: np.author || "",
        duration: isFinite(d) && d > 0 ? d : 0,
        position: safePos,
        playbackRate: Math.max(playbackRate, 0.25),
        artwork,
      });
      return;
    }

    // Chapter-only change: push title/artist text without re-sending artwork.
    if (chapterChanged) {
      await LibraryAuto.syncPlayback({
        active: true,
        playing: reportPlaying,
        mediaId: aaMediaId(np),
        title: np.title || "Audiobook",
        artist: scope.label || trackLabel || np.author || "",
        album: np.author || "",
        duration: isFinite(d) && d > 0 ? d : 0,
        position: safePos,
        playbackRate: Math.max(playbackRate, 0.25),
      });
      return;
    }

    await LibraryAuto.syncPlayback({
      active: true,
      playing: reportPlaying,
      position: safePos,
      playbackRate: Math.max(playbackRate, 0.25),
      positionOnly: true,
    });
  } catch {
    /* plugin unavailable */
  }
}
