/**
 * Generate Android launcher mipmaps + AA attribution icon from backend/static/icon-512.png
 */
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");
const srcIcon = path.resolve(root, "../backend/static/icon-512.png");
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

/** Adaptive-icon safe zone — ~66%; use 70% fill to match PWA without clipping. */
const FOREGROUND_INSET = 0.70;
/** Legacy launcher — nearly full bleed like the PWA icon. */
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

async function writeBuffer(buffer, dir, name) {
  const folder = path.join(resDir, dir);
  await mkdir(folder, { recursive: true });
  await sharp(buffer).toFile(path.join(folder, name));
}

const input = await sharp(srcIcon).toBuffer();

for (const [folder, size] of Object.entries(SIZES)) {
  const buf = await paddedIcon(input, size, LAUNCHER_INSET);
  await writeBuffer(buf, folder, "ic_launcher.png");
  await writeBuffer(buf, folder, "ic_launcher_round.png");
}

for (const [folder, size] of Object.entries(FOREGROUND_SIZES)) {
  const buf = await paddedIcon(input, size, FOREGROUND_INSET);
  await writeBuffer(buf, folder, "ic_launcher_foreground.png");
}

// Full-color icon kept for the Android Auto media browser service. The old
// monochrome white-on-transparent version rendered as a blank tile in the
// Android Auto launcher (the manifest now points at @mipmap/ic_launcher, but
// keep this drawable colored in case anything still references it).
const drawableDir = path.join(resDir, "drawable");
await mkdir(drawableDir, { recursive: true });
await sharp(await paddedIcon(input, 240, 0.8)).toFile(
  path.join(drawableDir, "ic_aa_attribution.png")
);

console.log("Android icons generated from", srcIcon);
