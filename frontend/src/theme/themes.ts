export const PRESET_THEME_IDS = ["ocean", "ember", "forest", "dusk"] as const;
export const THEME_IDS = [...PRESET_THEME_IDS, "custom"] as const;
export type PresetThemeId = (typeof PRESET_THEME_IDS)[number];
export type ThemeId = (typeof THEME_IDS)[number];

export const DEFAULT_THEME: PresetThemeId = "ocean";

/** Persisted so cold start / settings-loading don't flash DEFAULT then re-apply native icons. */
export const THEME_STORAGE_KEY = "library-ui-theme";
export const CUSTOM_THEME_COLORS_KEY = "library-ui-theme-custom";

export interface ThemeMeta {
  id: ThemeId;
  label: string;
  description: string;
  /** Preview swatches: accent, surface, background */
  swatches: [string, string, string];
}

/** User-picked custom palette: accent, surface, background. */
export interface CustomThemeColors {
  accent: string;
  surface: string;
  background: string;
}

export const DEFAULT_CUSTOM_COLORS: CustomThemeColors = {
  accent: "#4c6ef5",
  surface: "#1f2937",
  background: "#030712",
};

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
  {
    id: "custom",
    label: "Custom",
    description: "Pick your own accent, surface, and background",
    swatches: [
      DEFAULT_CUSTOM_COLORS.accent,
      DEFAULT_CUSTOM_COLORS.surface,
      DEFAULT_CUSTOM_COLORS.background,
    ],
  },
];

/** Presets only — library default theme cannot be custom. */
export const PRESET_THEMES: ThemeMeta[] = THEMES.filter((t) => t.id !== "custom");

export function isThemeId(value: string | null | undefined): value is ThemeId {
  return !!value && (THEME_IDS as readonly string[]).includes(value);
}

export function isPresetThemeId(value: string | null | undefined): value is PresetThemeId {
  return !!value && (PRESET_THEME_IDS as readonly string[]).includes(value);
}

export function normalizeThemeId(value: string | null | undefined): ThemeId {
  return isThemeId(value) ? value : DEFAULT_THEME;
}

/** Normalize a library default (custom is not allowed → ocean). */
export function normalizePresetThemeId(value: string | null | undefined): PresetThemeId {
  return isPresetThemeId(value) ? value : DEFAULT_THEME;
}

function clampByte(n: number): number {
  return Math.max(0, Math.min(255, Math.round(n)));
}

function parseHex(hex: string): { r: number; g: number; b: number } | null {
  const raw = hex.trim().replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}$/.test(raw) && !/^[0-9a-fA-F]{3}$/.test(raw)) return null;
  const full =
    raw.length === 3
      ? raw
          .split("")
          .map((c) => c + c)
          .join("")
      : raw;
  return {
    r: parseInt(full.slice(0, 2), 16),
    g: parseInt(full.slice(2, 4), 16),
    b: parseInt(full.slice(4, 6), 16),
  };
}

export function normalizeHexColor(hex: string, fallback: string): string {
  const parsed = parseHex(hex);
  if (!parsed) return fallback.startsWith("#") ? fallback : `#${fallback}`;
  const { r, g, b } = parsed;
  return `#${[r, g, b].map((n) => n.toString(16).padStart(2, "0")).join("")}`;
}

function rgbTuple(hex: string): string {
  const p = parseHex(hex) || parseHex(DEFAULT_CUSTOM_COLORS.accent)!;
  return `${p.r} ${p.g} ${p.b}`;
}

function mixHex(a: string, b: string, t: number): string {
  const pa = parseHex(a) || { r: 0, g: 0, b: 0 };
  const pb = parseHex(b) || { r: 255, g: 255, b: 255 };
  const r = clampByte(pa.r + (pb.r - pa.r) * t);
  const g = clampByte(pa.g + (pb.g - pa.g) * t);
  const bl = clampByte(pa.b + (pb.b - pa.b) * t);
  return `#${[r, g, bl].map((n) => n.toString(16).padStart(2, "0")).join("")}`;
}

function darken(hex: string, amount: number): string {
  return mixHex(hex, "#000000", amount);
}

function lighten(hex: string, amount: number): string {
  return mixHex(hex, "#ffffff", amount);
}

/** Build brand + gray CSS variable map from three user colors. */
export function customColorsToCssVars(colors: CustomThemeColors): Record<string, string> {
  const accent = normalizeHexColor(colors.accent, DEFAULT_CUSTOM_COLORS.accent);
  const surface = normalizeHexColor(colors.surface, DEFAULT_CUSTOM_COLORS.surface);
  const background = normalizeHexColor(colors.background, DEFAULT_CUSTOM_COLORS.background);

  return {
    "--brand-50": rgbTuple(lighten(accent, 0.92)),
    "--brand-100": rgbTuple(lighten(accent, 0.8)),
    "--brand-200": rgbTuple(lighten(accent, 0.6)),
    "--brand-300": rgbTuple(lighten(accent, 0.4)),
    "--brand-400": rgbTuple(lighten(accent, 0.2)),
    "--brand-500": rgbTuple(accent),
    "--brand-600": rgbTuple(darken(accent, 0.12)),
    "--brand-700": rgbTuple(darken(accent, 0.28)),
    "--brand-800": rgbTuple(darken(accent, 0.4)),
    "--brand-900": rgbTuple(darken(accent, 0.55)),

    "--gray-50": rgbTuple(mixHex(background, "#ffffff", 0.96)),
    "--gray-100": rgbTuple(mixHex(background, "#ffffff", 0.9)),
    "--gray-200": rgbTuple(mixHex(surface, "#ffffff", 0.75)),
    "--gray-300": rgbTuple(mixHex(surface, "#ffffff", 0.55)),
    "--gray-400": rgbTuple(mixHex(surface, "#ffffff", 0.35)),
    "--gray-500": rgbTuple(mixHex(surface, "#ffffff", 0.2)),
    "--gray-600": rgbTuple(mixHex(surface, background, 0.25)),
    "--gray-700": rgbTuple(mixHex(surface, background, 0.15)),
    "--gray-800": rgbTuple(surface),
    "--gray-900": rgbTuple(mixHex(surface, background, 0.55)),
    "--gray-950": rgbTuple(background),
  };
}

export function normalizeCustomColors(
  raw: Partial<CustomThemeColors> | null | undefined
): CustomThemeColors {
  return {
    accent: normalizeHexColor(raw?.accent || "", DEFAULT_CUSTOM_COLORS.accent),
    surface: normalizeHexColor(raw?.surface || "", DEFAULT_CUSTOM_COLORS.surface),
    background: normalizeHexColor(raw?.background || "", DEFAULT_CUSTOM_COLORS.background),
  };
}

export function readCachedCustomColors(): CustomThemeColors {
  try {
    const raw = localStorage.getItem(CUSTOM_THEME_COLORS_KEY);
    if (!raw) return { ...DEFAULT_CUSTOM_COLORS };
    return normalizeCustomColors(JSON.parse(raw) as Partial<CustomThemeColors>);
  } catch {
    return { ...DEFAULT_CUSTOM_COLORS };
  }
}

export function writeCachedCustomColors(colors: CustomThemeColors): void {
  try {
    localStorage.setItem(
      CUSTOM_THEME_COLORS_KEY,
      JSON.stringify(normalizeCustomColors(colors))
    );
  } catch {
    /* ignore */
  }
}

const CUSTOM_VAR_NAMES = [
  "--brand-50",
  "--brand-100",
  "--brand-200",
  "--brand-300",
  "--brand-400",
  "--brand-500",
  "--brand-600",
  "--brand-700",
  "--brand-800",
  "--brand-900",
  "--gray-50",
  "--gray-100",
  "--gray-200",
  "--gray-300",
  "--gray-400",
  "--gray-500",
  "--gray-600",
  "--gray-700",
  "--gray-800",
  "--gray-900",
  "--gray-950",
] as const;

/** Clear inline custom overrides so preset CSS rules take effect again. */
export function clearCustomThemeCssVars(): void {
  try {
    const root = document.documentElement;
    for (const name of CUSTOM_VAR_NAMES) {
      root.style.removeProperty(name);
    }
  } catch {
    /* ignore */
  }
}

export function applyCustomThemeCssVars(colors: CustomThemeColors): void {
  try {
    const root = document.documentElement;
    const vars = customColorsToCssVars(colors);
    for (const [name, value] of Object.entries(vars)) {
      root.style.setProperty(name, value);
    }
  } catch {
    /* ignore */
  }
}

/** Last explicitly chosen theme, or null if never stored. */
export function readCachedTheme(): ThemeId | null {
  try {
    const raw = localStorage.getItem(THEME_STORAGE_KEY);
    return isThemeId(raw) ? raw : null;
  } catch {
    return null;
  }
}

export function writeCachedTheme(theme: ThemeId): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    /* ignore */
  }
}

/** Apply CSS theme tokens immediately (no native icon I/O). */
export function applyThemeCss(theme: ThemeId, customColors?: CustomThemeColors): void {
  try {
    document.documentElement.setAttribute("data-theme", theme);
  } catch {
    /* ignore */
  }
  if (theme === "custom") {
    const colors = customColors ?? readCachedCustomColors();
    writeCachedCustomColors(colors);
    applyCustomThemeCssVars(colors);
  } else {
    clearCustomThemeCssVars();
  }
  writeCachedTheme(theme);
}

export function applyThemeToDocument(
  theme: ThemeId,
  customColors?: CustomThemeColors
): void {
  applyThemeCss(theme, customColors);
  // Favicons always; on Android also switch the single AA MediaBrowserService.
  // Custom falls back to ocean icons (no generated icon set).
  const iconTheme = theme === "custom" ? DEFAULT_THEME : theme;
  void import("./themeIcon")
    .then((m) => m.applyAppIconTheme(iconTheme))
    .catch(() => {
      /* ignore */
    });
}

/** Call once before React mounts so first paint matches last theme. */
export function bootstrapThemeFromCache(): void {
  const cached = readCachedTheme();
  if (cached) {
    applyThemeCss(cached);
    const iconTheme = cached === "custom" ? DEFAULT_THEME : cached;
    void import("./themeIcon")
      .then((m) => m.applyNativeAppIconTheme(iconTheme))
      .catch(() => {});
  }
}
