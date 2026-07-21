import { normalizeThemeId, type ThemeId } from "./themes";

let lastFaviconTheme: ThemeId | null = null;

/** Hex accents used for meta theme-color (matches icon backgrounds). */
const THEME_META: Record<ThemeId, { themeColor: string }> = {
  ocean: { themeColor: "#030712" },
  ember: { themeColor: "#100b09" },
  forest: { themeColor: "#060c08" },
  dusk: { themeColor: "#020617" },
};

function setMetaContent(name: string, content: string): void {
  let meta = document.querySelector(`meta[name="${name}"]`) as HTMLMetaElement | null;
  if (!meta) {
    meta = document.createElement("meta");
    meta.name = name;
    document.head.appendChild(meta);
  }
  meta.content = content;
}

function ensureLink(rel: string, sizes?: string): HTMLLinkElement {
  const sel = sizes
    ? `link[rel="${rel}"][sizes="${sizes}"]`
    : `link[rel="${rel}"]`;
  let link = document.querySelector(sel) as HTMLLinkElement | null;
  if (!link) {
    link = document.createElement("link");
    link.rel = rel;
    if (sizes) link.setAttribute("sizes", sizes);
    document.head.appendChild(link);
  }
  return link;
}

/** Update browser tab / PWA icon links for the active theme. */
export function applyThemedFavicons(themeRaw: string): void {
  if (typeof document === "undefined") return;
  const theme = normalizeThemeId(themeRaw);
  if (lastFaviconTheme === theme) return;
  lastFaviconTheme = theme;
  const bust = `v=${theme}`;
  const icon192 = `/icons/icon-192-${theme}.png?${bust}`;
  const icon512 = `/icons/icon-512-${theme}.png?${bust}`;

  const small = ensureLink("icon", "192x192");
  small.type = "image/png";
  small.href = icon192;

  const large = ensureLink("icon", "512x512");
  large.type = "image/png";
  large.href = icon512;

  const apple = ensureLink("apple-touch-icon");
  apple.href = icon192;

  setMetaContent("msapplication-TileImage", icon192);
  setMetaContent("theme-color", THEME_META[theme].themeColor);
  setMetaContent("msapplication-TileColor", THEME_META[theme].themeColor);
}

/**
 * Native launcher-icon switching is intentionally disabled.
 * PackageManager activity-alias toggles were killing the Capacitor process.
 * In-app CSS theme + web favicons still update; home-screen icon stays put.
 */
export async function applyNativeAppIconTheme(_themeRaw: string): Promise<void> {
  /* no-op — see ThemeIconHelper.java */
}

export async function applyAppIconTheme(themeRaw: string): Promise<void> {
  applyThemedFavicons(themeRaw);
  // Native launcher aliases are not toggled (crash-prone on Android).
}
