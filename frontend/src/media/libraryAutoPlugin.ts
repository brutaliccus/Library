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
    }) => void
  ): Promise<void>;
  resolveBrowseChildren(options: {
    requestId: string;
    children: BrowseChild[];
  }): Promise<void>;
  bringToForeground(): Promise<void>;
  addListener(
    eventName: "browseRequest",
    listenerFunc: (event: { parentId: string; requestId: string }) => void
  ): Promise<PluginListenerHandle>;
}

export const LibraryAuto = registerPlugin<LibraryAutoPlugin>("LibraryAuto");
