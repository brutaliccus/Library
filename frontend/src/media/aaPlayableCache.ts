/**
 * Cache playable track queues natively so Android Auto can start ExoPlayer
 * while the phone is locked (no WebView / JS required for PCM).
 */
import { Capacitor } from "@capacitor/core";
import { LibraryAuto } from "./libraryAutoPlugin";
import { toAbsoluteUrl } from "../api/instanceUrl";
import type { Track } from "../types/player";

const AA_PLAY_ABS_PREFIX = "play/abs/";
const AA_PLAY_RD_HIST_PREFIX = "play/rdhist/";

export interface CachePlayableOpts {
  mediaId: string;
  title: string;
  author?: string;
  coverUrl?: string;
  tracks: Track[];
  /** Track-local seconds */
  position?: number;
  trackIndex?: number;
  totalDuration?: number;
}

function absMediaId(itemId: string): string {
  return `${AA_PLAY_ABS_PREFIX}${itemId}`;
}

function rdMediaId(historyId: number): string {
  return `${AA_PLAY_RD_HIST_PREFIX}${historyId}`;
}

export async function cachePlayableForAndroidAuto(
  opts: CachePlayableOpts
): Promise<void> {
  if (Capacitor.getPlatform() !== "android") return;
  if (!opts.mediaId || !opts.tracks?.length) return;

  const token =
    typeof localStorage !== "undefined"
      ? localStorage.getItem("access_token") || ""
      : "";

  try {
    await LibraryAuto.cachePlayableMedia({
      mediaId: opts.mediaId,
      title: opts.title,
      author: opts.author || "",
      coverUrl: opts.coverUrl ? toAbsoluteUrl(opts.coverUrl) : "",
      authToken: token,
      position: Math.max(0, opts.position ?? 0),
      trackIndex: Math.max(0, opts.trackIndex ?? 0),
      totalDuration: Math.max(0, opts.totalDuration ?? 0),
      tracks: opts.tracks.map((t) => ({
        contentUrl: toAbsoluteUrl(t.contentUrl),
        title: t.title || "",
        startOffset: t.startOffset ?? 0,
        duration: t.duration ?? 0,
        mimeType: t.mimeType || "",
      })),
    });
  } catch {
    /* plugin unavailable */
  }
}

export async function cacheAbsPlayable(
  itemId: string,
  title: string,
  author: string,
  coverUrl: string,
  tracks: Track[],
  totalDuration: number,
  position = 0,
  trackIndex = 0
): Promise<void> {
  await cachePlayableForAndroidAuto({
    mediaId: absMediaId(itemId),
    title,
    author,
    coverUrl,
    tracks,
    totalDuration,
    position,
    trackIndex,
  });
}

export async function cacheRdPlayable(
  historyId: number,
  title: string,
  author: string,
  coverUrl: string,
  tracks: Track[],
  totalDuration: number,
  position = 0,
  trackIndex = 0
): Promise<void> {
  await cachePlayableForAndroidAuto({
    mediaId: rdMediaId(historyId),
    title,
    author,
    coverUrl,
    tracks,
    totalDuration,
    position,
    trackIndex,
  });
}
