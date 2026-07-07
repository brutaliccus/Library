/** Seconds — keep in sync with AudiobookTransport skip buttons */
export const MEDIA_SKIP_SECONDS = 15;

export function toAbsoluteArtworkUrl(url: string): string {
  if (!url.trim()) return "";
  try {
    return new URL(url, window.location.origin).href;
  } catch {
    return url;
  }
}

export function clearMediaSessionPlayback(): void {
  if (!("mediaSession" in navigator)) return;
  try {
    navigator.mediaSession.metadata = null;
    navigator.mediaSession.playbackState = "none";
  } catch {
    /* ignore */
  }
}
