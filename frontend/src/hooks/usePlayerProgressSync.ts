/**
 * Progress persistence for the audio player, isolated from playback wiring:
 * - syncProgress: one authenticated API save (ABS session sync or RD history)
 * - periodic background saves every SYNC_INTERVAL while a book is loaded
 * - sendBeacon saves on page unload / app backgrounding (Android kills
 *   backgrounded WebViews without firing normal unload events)
 * - persistPlaybackProgress: last-chance save with retry + user-visible error
 */
import { useCallback, useEffect } from "react";
import api from "../api/client";
import { toAbsoluteUrl } from "../api/instanceUrl";
import { useToast } from "../contexts/ToastContext";
import { npKey, type NowPlaying, type PlaybackPosition } from "../types/player";
import {
  getOfflineProgress,
  isLikelyOffline,
  progressKeyForAbs,
  progressKeyForRd,
  saveOfflineProgress,
} from "../utils/offlinePlayback";

const SYNC_INTERVAL = 30_000;

function progressKeyFor(np: NowPlaying): string | null {
  if (np.source === "abs" && np.itemId) return progressKeyForAbs(np.itemId);
  if (np.source === "rd") {
    return progressKeyForRd({
      libraryItemId: np.libraryItemId,
      streamHistoryId: np.streamHistoryId,
    });
  }
  return null;
}

function persistLocalProgress(
  np: NowPlaying,
  time: number,
  trackIndex: number,
  trackLocalTime?: number,
  playbackRate?: number | null
): boolean {
  const trackLocal = trackLocalTime ?? Math.max(0, time - (np.tracks[trackIndex]?.startOffset ?? 0));
  const key = progressKeyFor(np);
  if (!key) return false;

  // Never let an older AA/Exo tick clobber fresher phone progress.
  const existing = getOfflineProgress(key);
  if (
    existing &&
    existing.updatedAt > 0 &&
    time + 5 < (existing.time || 0) &&
    Date.now() - existing.updatedAt < 15 * 60_000
  ) {
    return false;
  }

  const payload = { time, trackIndex, trackLocal, playbackRate };
  if (np.source === "abs" && np.itemId) {
    saveOfflineProgress(progressKeyForAbs(np.itemId), payload);
  } else if (np.source === "rd") {
    saveOfflineProgress(key, payload);
    // Also mirror under the alternate key so lib↔history lookups both resume.
    if (np.libraryItemId != null && np.streamHistoryId != null) {
      saveOfflineProgress(progressKeyForRd({ streamHistoryId: np.streamHistoryId })!, payload);
      saveOfflineProgress(progressKeyForRd({ libraryItemId: np.libraryItemId })!, payload);
    }
  }

  // Keep AA playable-cache seek offset aligned with authoritative local progress.
  const mediaId =
    np.source === "abs" && np.itemId
      ? `play/abs/${np.itemId}`
      : np.source === "rd" && np.streamHistoryId != null
        ? `play/rdhist/${np.streamHistoryId}`
        : "";
  if (mediaId) {
    void import("../media/aaPlayableCache")
      .then(({ stampAaPlayablePosition }) =>
        stampAaPlayablePosition(mediaId, time, trackIndex)
      )
      .catch(() => {});
  }
  return true;
}

interface ProgressSyncSources {
  /** Latest nowPlaying without re-subscribing effects to state changes. */
  getNowPlaying: () => NowPlaying | null;
  /** Last known-good playback position (never a still-buffering element's 0:00). */
  getPosition: () => PlaybackPosition | null;
  /** Current playback rate for local resume mirroring. */
  getPlaybackRate?: () => number;
}

export function usePlayerProgressSync({
  getNowPlaying,
  getPosition,
  getPlaybackRate,
}: ProgressSyncSources) {
  const { toast } = useToast();

  /** Returns true when the progress save succeeded (or there was nothing to save). */
  const syncProgress = useCallback(
    async (
      np: NowPlaying,
      time: number,
      trackIndex: number,
      trackLocalTime?: number
    ): Promise<boolean> => {
      // Always keep a local resume point so offline replay works without ABS/API.
      // Skip server write too when this tick would rewind fresher local progress.
      const wroteLocal = persistLocalProgress(
        np,
        time,
        trackIndex,
        trackLocalTime,
        getPlaybackRate?.()
      );
      if (!wroteLocal) return true;

      // Offline / no live session: local save is enough.
      if (isLikelyOffline()) return true;
      if (np.source === "abs" && !np.sessionId) return true;

      if (np.source === "abs" && np.sessionId) {
        try {
          await api.post(`/stream/abs/${np.sessionId}/sync`, {
            currentTime: time,
            duration: np.totalDuration,
          });
          return true;
        } catch {
          // Local progress already saved; only treat as failure when online.
          return isLikelyOffline();
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
          return isLikelyOffline();
        }
      }
      return true;
    },
    [getPlaybackRate]
  );

  const persistPlaybackProgress = useCallback(
    async (np: NowPlaying, time: number, trackIndex: number, trackLocalTime?: number) => {
      // This is the last chance to save the listening position — retry once and
      // tell the user if it still fails instead of silently losing progress.
      let ok = await syncProgress(np, time, trackIndex, trackLocalTime);
      if (!ok) {
        await new Promise((r) => setTimeout(r, 1000));
        ok = await syncProgress(np, time, trackIndex, trackLocalTime);
      }
      // Don't nag about network saves when we're offline — local progress is enough.
      if (!ok && !isLikelyOffline()) {
        toast("Couldn't save your listening progress — check your connection", "error");
      }
      if (np.source === "abs" && np.sessionId && !isLikelyOffline()) {
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

  // Periodic background save while a book is loaded.
  useEffect(() => {
    const id = setInterval(() => {
      const np = getNowPlaying();
      const pos = getPosition();
      if (np && pos?.key === npKey(np)) {
        void syncProgress(np, pos.time, pos.trackIndex, pos.trackLocal);
      }
    }, SYNC_INTERVAL);
    return () => clearInterval(id);
  }, [syncProgress, getNowPlaying, getPosition]);

  useEffect(() => {
    // sendBeacon can't set Authorization headers, so the beacon endpoints take
    // the access token as a query param instead.
    const handler = (mode: "close" | "sync" = "close") => {
      const np = getNowPlaying();
      const pos = getPosition();
      if (!np || pos?.key !== npKey(np)) return;
      // Always persist locally first (works offline). Skip network if this
      // tick would rewind fresher phone progress (stale AA/Exo attach).
      const wrote = persistLocalProgress(
        np,
        pos.time,
        pos.trackIndex,
        pos.trackLocal,
        getPlaybackRate?.()
      );
      if (!wrote || isLikelyOffline()) return;
      const token = localStorage.getItem("access_token") || "";
      if (np.source === "abs" && np.sessionId) {
        // "sync" keeps the session alive (app merely backgrounded and may keep
        // playing); "close" is for real page unloads.
        const endpoint = mode === "close" ? "close-beacon" : "sync-beacon";
        navigator.sendBeacon?.(
          toAbsoluteUrl(
            `/api/stream/abs/${np.sessionId}/${endpoint}?token=${encodeURIComponent(token)}`
          ),
          new Blob(
            [JSON.stringify({ currentTime: pos.time, duration: np.totalDuration })],
            { type: "application/json" }
          )
        );
      } else if (np.source === "rd" && np.streamHistoryId) {
        navigator.sendBeacon?.(
          toAbsoluteUrl(
            `/api/stream/rd/history/sync-beacon?token=${encodeURIComponent(token)}`
          ),
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
  }, [getNowPlaying, getPosition, getPlaybackRate]);

  return { syncProgress, persistPlaybackProgress };
}
