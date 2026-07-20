export const THEME_IDS = ["ocean", "ember", "forest", "dusk"] as const;
export type ThemeId = (typeof THEME_IDS)[number];

export const DEFAULT_THEME: ThemeId = "ocean";

export interface ThemeMeta {
  id: ThemeId;
  label: string;
  description: string;
  /** Preview swatches: accent, surface */
  swatches: [string, string, string];
}

export const THEMES: ThemeMeta[] = [
  {
    id: "ocean",
    label: "Ocean",
    description: "Classic blue — the default look",
    swatches: ["#4c6ef5", "#111827", "#030712"],
  },
  {
    id: "ember",
    label: "Ember",
    description: "Dark red on warm parchment",
    swatches: ["#b93030", "#1c1410", "#100b09"],
  },
  {
    id: "forest",
    label: "Forest",
    description: "Deep greens for a cozy reading room",
    swatches: ["#16a34a", "#0e1611", "#060c08"],
  },
  {
    id: "dusk",
    label: "Dusk",
    description: "Cool teal for calm night reading",
    swatches: ["#0d9488", "#0f172a", "#020617"],
  },
];

export function isThemeId(value: string | null | undefined): value is ThemeId {
  return !!value && (THEME_IDS as readonly string[]).includes(value);
}

export function normalizeThemeId(value: string | null | undefined): ThemeId {
  return isThemeId(value) ? value : DEFAULT_THEME;
}

export function applyThemeToDocument(theme: ThemeId): void {
  try {
    document.documentElement.setAttribute("data-theme", theme);
  } catch {
    /* ignore */
  }
  // Browser tab favicon + Android launcher / Android Auto icons.
  void import("./themeIcon")
    .then((m) => m.applyAppIconTheme(theme))
    .catch(() => {
      /* ignore */
    });
}
