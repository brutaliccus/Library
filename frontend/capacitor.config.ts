import type { CapacitorConfig } from "@capacitor/cli";

/**
 * Android app ships a bundled SPA (webDir). Users enter their Library server
 * URL in the app on first sign-in / account request (editable in Settings).
 * No hardcoded server.url — one APK works for any self-hosted instance.
 */
const config: CapacitorConfig = {
  appId: "com.freiverse.library",
  appName: "Library",
  webDir: "../backend/static",
  server: {
    // Local bundled assets (not a remote host). Origin becomes https://localhost.
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
