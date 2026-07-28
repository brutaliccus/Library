import { registerPlugin, type PluginListenerHandle } from "@capacitor/core";

export interface AppUpdateNativePlugin {
  getInstalledVersion(): Promise<{ versionCode: number; versionName: string }>;
  downloadAndInstall(options: { url: string; authToken?: string }): Promise<{ filePath: string }>;
  showUpdateAvailable(options: {
    title: string;
    body: string;
    releaseKey: string;
    downloadUrl: string;
    authToken?: string;
    /** When true, notification has no dismiss action. */
    required?: boolean;
  }): Promise<void>;
  dismissUpdateNotification(): Promise<void>;
  /** Clear Chromium WebView HTTP/asset cache (after APK version change). */
  clearWebViewCache(): Promise<void>;
  addListener(
    eventName: "downloadProgress",
    listenerFunc: (event: { percent: number }) => void
  ): Promise<PluginListenerHandle>;
  addListener(
    eventName: "appUpdateRequested",
    listenerFunc: (event: { releaseKey: string; downloadUrl: string }) => void
  ): Promise<PluginListenerHandle>;
  addListener(
    eventName: "appUpdateDismissed",
    listenerFunc: (event: { releaseKey: string }) => void
  ): Promise<PluginListenerHandle>;
  addListener(
    eventName: "appUpdateNow",
    listenerFunc: (event: { releaseKey: string }) => void
  ): Promise<PluginListenerHandle>;
  addListener(
    eventName: "appUpdateFailed",
    listenerFunc: (event: { message: string }) => void
  ): Promise<PluginListenerHandle>;
}

export const AppUpdateNative = registerPlugin<AppUpdateNativePlugin>("AppUpdate");
