import type { CapacitorConfig } from "@capacitor/cli";

/**
 * Android app = native shell around the hosted web app.
 *
 * Pointing at the live server (instead of bundling the SPA) keeps everything
 * working with zero duplication: the service-worker audio cache, media-session
 * lock-screen controls, streaming proxy URLs, and every future deploy reaches
 * the app instantly without rebuilding the APK.
 *
 * Change `server.url` if your public URL differs (must be https).
 */
const config: CapacitorConfig = {
  appId: "com.freiverse.library",
  appName: "Library",
  webDir: "../backend/static",
  server: {
    url: "https://library.example.com",
    androidScheme: "https",
  },
  android: {
    allowMixedContent: false,
    backgroundColor: "#030712", // matches the app's gray-950 background
  },
  plugins: {
    MediaSession: {
      foregroundService: "always",
    },
  },
};

export default config;
