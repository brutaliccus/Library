import { registerPlugin } from "@capacitor/core";
import type { PluginListenerHandle } from "@capacitor/core";

export type LibraryAutoAction =
  | "play"
  | "pause"
  | "seekbackward"
  | "seekforward"
  | "previoustrack"
  | "nexttrack"
  | "seekto"
  | "stop"
  | "playmedia";

export interface BrowseChild {
  mediaId: string;
  title: string;
  subtitle?: string;
  browsable: boolean;
  iconUri?: string;
}

export interface NativePlaybackEvent {
  mediaId?: string;
  title?: string;
  artist?: string;
  album?: string;
  coverUrl?: string;
  playing?: boolean;
  duration?: number;
  position?: number;
  playbackRate?: number;
  trackIndex?: number;
  nativeOwner?: boolean;
  error?: string;
}

export interface PlayableTrackPayload {
  contentUrl: string;
  title?: string;
  startOffset?: number;
  duration?: number;
  mimeType?: string;
}

interface LibraryAutoPlugin {
  syncPlayback(options: {
    active: boolean;
    playing: boolean;
    title?: string;
    artist?: string;
    album?: string;
    duration?: number;
    position?: number;
    playbackRate?: number;
    artwork?: { src: string; sizes?: string; type?: string }[];
    /** When true, only update transport state (position/playing) — no metadata or artwork. */
    positionOnly?: boolean;
  }): Promise<void>;
  setActionHandler(
    options: { action: LibraryAutoAction },
    callback: (details: {
      action: string;
      seekTime?: number | null;
      mediaId?: string;
      nativeStarted?: boolean;
    }) => void
  ): Promise<void>;
  resolveBrowseChildren(options: {
    requestId: string;
    children: BrowseChild[];
  }): Promise<void>;
  /** Persist a browse folder natively for locked-phone Android Auto. */
  cacheBrowseChildren(options: {
    parentId: string;
    children: BrowseChild[];
    /** When true, allow clearing a non-empty native cache (live empty confirm). */
    allowEmpty?: boolean;
  }): Promise<void>;
  /** Persist track URLs + auth so locked AA can start ExoPlayer without JS. */
  cachePlayableMedia(options: {
    mediaId: string;
    title: string;
    author?: string;
    coverUrl?: string;
    authToken?: string;
    position?: number;
    trackIndex?: number;
    totalDuration?: number;
    tracks: PlayableTrackPayload[];
  }): Promise<void>;
  /** Append base64 chunk to on-disk offline audio (Android large books). */
  appendAudioDiskCache(options: {
    storageKey: string;
    data: string;
    contentType?: string;
    total?: number;
    offset?: number;
  }): Promise<{ ok: boolean; size?: number }>;
  finalizeAudioDiskCache(options: {
    storageKey: string;
  }): Promise<{ ok: boolean; uri?: string; path?: string; size?: number }>;
  getAudioDiskCacheUri(options: {
    storageKey: string;
  }): Promise<{
    complete: boolean;
    size?: number;
    partialSize?: number;
    uri?: string;
    path?: string;
  }>;
  deleteAudioDiskCache(options: {
    storageKey?: string;
    urlPrefix?: string;
    all?: boolean;
  }): Promise<void>;
  getNativePlaybackState(): Promise<{
    nativeOwner: boolean;
    playing: boolean;
    mediaId?: string;
    position?: number;
  }>;
  getPlayableMedia(options: { mediaId: string }): Promise<{
    mediaId?: string;
    title?: string;
    author?: string;
    coverUrl?: string;
    position?: number;
    trackIndex?: number;
    totalDuration?: number;
    tracks?: PlayableTrackPayload[];
  }>;
  /** Release ExoPlayer before WebView HTML5 audio takes over. */
  handOffNativePlayback(): Promise<void>;
  bringToForeground(): Promise<void>;
  addListener(
    eventName: "browseRequest",
    listenerFunc: (event: { parentId: string; requestId: string }) => void
  ): Promise<PluginListenerHandle>;
  addListener(
    eventName: "nativePlayback",
    listenerFunc: (event: NativePlaybackEvent) => void
  ): Promise<PluginListenerHandle>;
}

export const LibraryAuto = registerPlugin<LibraryAutoPlugin>("LibraryAuto");
