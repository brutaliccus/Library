/**
 * Shared brand mark (book + headphones) as SVG, tinted per theme.
 */

export const THEME_ICON_COLORS = {
  ocean: { bg: "#030712", accent: "#5c7cfa", book: "#ffffff" },
  ember: { bg: "#100b09", accent: "#ef4444", book: "#ffffff" },
  forest: { bg: "#060c08", accent: "#22c55e", book: "#ffffff" },
  dusk: { bg: "#020617", accent: "#2dd4bf", book: "#ffffff" },
};

export const THEME_IDS = Object.keys(THEME_ICON_COLORS);

/** @param {{ bg: string, accent: string, book: string }} colors */
export function brandIconSvg({ bg, accent, book }) {
  // viewBox 0..512 — matches existing PWA / Android icon composition.
  return `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <rect width="512" height="512" rx="108" fill="${bg}"/>
  <!-- Headphones headband -->
  <path d="M128 218c0-70 56-126 128-126s128 56 128 126"
        fill="none" stroke="${accent}" stroke-width="40" stroke-linecap="round"/>
  <!-- Ear cups -->
  <rect x="92" y="210" width="56" height="88" rx="20" fill="${accent}"/>
  <rect x="364" y="210" width="56" height="88" rx="20" fill="${accent}"/>
  <!-- Open book -->
  <path d="M256 168 L148 196 v176 l108-36 Z" fill="${book}"/>
  <path d="M256 168 L364 196 v176 l-108-36 Z" fill="${book}"/>
  <!-- Spine -->
  <path d="M256 168 v168" stroke="${bg}" stroke-width="10" stroke-linecap="round"/>
  <!-- Page lines (left) -->
  <path d="M176 232 h52 M176 262 h52 M176 292 h44"
        fill="none" stroke="${bg}" stroke-width="7" stroke-linecap="round" opacity="0.35"/>
  <!-- Page lines (right) -->
  <path d="M284 232 h52 M284 262 h52 M284 292 h44"
        fill="none" stroke="${bg}" stroke-width="7" stroke-linecap="round" opacity="0.35"/>
</svg>`;
}
