/**
 * Generate themed brand icons for PWA/favicon + Android launcher / Auto.
 * Run: node scripts/generate-themed-icons.mjs
 */
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";
import { THEME_ICON_COLORS, THEME_IDS, brandIconSvg } from "./brand-icon.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");
const publicDir = path.resolve(root, "public");
const publicIconsDir = path.resolve(publicDir, "icons");
const backendStatic = path.resolve(root, "../backend/static");
const backendIcons = path.resolve(backendStatic, "icons");
const resDir = path.resolve(root, "android/app/src/main/res");

const SIZES = {
  "mipmap-mdpi": 48,
  "mipmap-hdpi": 72,
  "mipmap-xhdpi": 96,
  "mipmap-xxhdpi": 144,
  "mipmap-xxxhdpi": 192,
};

const FOREGROUND_SIZES = {
  "mipmap-mdpi": 108,
  "mipmap-hdpi": 162,
  "mipmap-xhdpi": 216,
  "mipmap-xxhdpi": 324,
  "mipmap-xxxhdpi": 432,
};

const FOREGROUND_INSET = 0.7;
const LAUNCHER_INSET = 0.94;

async function paddedIcon(input, canvasSize, inset) {
  const inner = Math.max(1, Math.round(canvasSize * inset));
  const pad = Math.floor((canvasSize - inner) / 2);
  return sharp(input)
    .resize(inner, inner, { fit: "contain", background: { r: 0, g: 0, b: 0, alpha: 0 } })
    .extend({
      top: pad,
      bottom: canvasSize - inner - pad,
      left: pad,
      right: canvasSize - inner - pad,
      background: { r: 0, g: 0, b: 0, alpha: 0 },
    })
    .png()
    .toBuffer();
}

async function writePng(buffer, filePath) {
  await mkdir(path.dirname(filePath), { recursive: true });
  await sharp(buffer).png().toFile(filePath);
}

async function renderThemePng(themeId, size) {
  const colors = THEME_ICON_COLORS[themeId];
  const svg = Buffer.from(brandIconSvg(colors));
  return sharp(svg).resize(size, size).png().toBuffer();
}

async function writeAdaptiveXml(themeId, bgColor) {
  const anyDpi = path.join(resDir, "mipmap-anydpi-v26");
  await mkdir(anyDpi, { recursive: true });
  const colorName = `ic_launcher_background_${themeId}`;
  const xml = `<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@color/${colorName}"/>
    <foreground android:drawable="@mipmap/ic_launcher_${themeId}_foreground"/>
</adaptive-icon>
`;
  await writeFile(path.join(anyDpi, `ic_launcher_${themeId}.xml`), xml);
  await writeFile(path.join(anyDpi, `ic_launcher_${themeId}_round.xml`), xml);

  // Also keep default ic_launcher pointing at ocean.
  if (themeId === "ocean") {
    const def = `<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@color/ic_launcher_background"/>
    <foreground android:drawable="@mipmap/ic_launcher_foreground"/>
</adaptive-icon>
`;
    await writeFile(path.join(anyDpi, "ic_launcher.xml"), def);
    await writeFile(path.join(anyDpi, "ic_launcher_round.xml"), def);
  }

  return { colorName, bgColor };
}

async function writeColorsXml(entries) {
  const valuesDir = path.join(resDir, "values");
  await mkdir(valuesDir, { recursive: true });
  const lines = entries.map(([name, hex]) => `    <color name="${name}">${hex}</color>`);
  const xml = `<?xml version="1.0" encoding="utf-8"?>
<resources>
${lines.join("\n")}
</resources>
`;
  await writeFile(path.join(valuesDir, "ic_launcher_backgrounds.xml"), xml);
  // Legacy single color used by default adaptive icon.
  await writeFile(
    path.join(valuesDir, "ic_launcher_background.xml"),
    `<?xml version="1.0" encoding="utf-8"?>
<resources>
    <color name="ic_launcher_background">${THEME_ICON_COLORS.ocean.bg}</color>
</resources>
`
  );
}

async function generateAndroidTheme(themeId, src512) {
  for (const [folder, size] of Object.entries(SIZES)) {
    const buf = await paddedIcon(src512, size, LAUNCHER_INSET);
    const dir = path.join(resDir, folder);
    await mkdir(dir, { recursive: true });
    await sharp(buf).toFile(path.join(dir, `ic_launcher_${themeId}.png`));
    await sharp(buf).toFile(path.join(dir, `ic_launcher_${themeId}_round.png`));
    if (themeId === "ocean") {
      await sharp(buf).toFile(path.join(dir, "ic_launcher.png"));
      await sharp(buf).toFile(path.join(dir, "ic_launcher_round.png"));
    }
  }
  for (const [folder, size] of Object.entries(FOREGROUND_SIZES)) {
    const buf = await paddedIcon(src512, size, FOREGROUND_INSET);
    const dir = path.join(resDir, folder);
    await mkdir(dir, { recursive: true });
    await sharp(buf).toFile(path.join(dir, `ic_launcher_${themeId}_foreground.png`));
    if (themeId === "ocean") {
      await sharp(buf).toFile(path.join(dir, "ic_launcher_foreground.png"));
    }
  }
  await writeAdaptiveXml(themeId, THEME_ICON_COLORS[themeId].bg);
}

async function main() {
  await mkdir(publicIconsDir, { recursive: true });
  await mkdir(backendIcons, { recursive: true });

  const colorEntries = [];

  for (const themeId of THEME_IDS) {
    const png192 = await renderThemePng(themeId, 192);
    const png512 = await renderThemePng(themeId, 512);

    await writePng(png192, path.join(publicIconsDir, `icon-192-${themeId}.png`));
    await writePng(png512, path.join(publicIconsDir, `icon-512-${themeId}.png`));
    await writePng(png192, path.join(backendIcons, `icon-192-${themeId}.png`));
    await writePng(png512, path.join(backendIcons, `icon-512-${themeId}.png`));

    if (themeId === "ocean") {
      await writePng(png192, path.join(publicDir, "icon-192.png"));
      await writePng(png512, path.join(publicDir, "icon-512.png"));
      await writePng(png192, path.join(backendStatic, "icon-192.png"));
      await writePng(png512, path.join(backendStatic, "icon-512.png"));
    }

    await generateAndroidTheme(themeId, png512);
    colorEntries.push([`ic_launcher_background_${themeId}`, THEME_ICON_COLORS[themeId].bg]);

    // Android Auto attribution drawable (ocean default kept for compatibility).
    if (themeId === "ocean") {
      const drawableDir = path.join(resDir, "drawable");
      await mkdir(drawableDir, { recursive: true });
      await sharp(await paddedIcon(png512, 240, 0.8)).toFile(
        path.join(drawableDir, "ic_aa_attribution.png")
      );
    }
  }

  await writeColorsXml(colorEntries);
  console.log("Themed icons generated for:", THEME_IDS.join(", "));
  console.log("  PWA:", publicIconsDir);
  console.log("  Android mipmaps under", resDir);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
