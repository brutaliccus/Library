/**
 * Copy pdf.js runtime assets into public/ so getDocument can load
 * JBIG2 wasm (soft masks), CMaps, standard fonts, and ICC profiles.
 */
import { cpSync, mkdirSync, rmSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = join(__dirname, "..");
const srcRoot = join(root, "node_modules", "pdfjs-dist");
const destRoot = join(root, "public", "pdfjs");

const folders = ["wasm", "cmaps", "standard_fonts", "iccs"];

if (!existsSync(join(srcRoot, "wasm", "jbig2.wasm"))) {
  console.error("pdfjs-dist wasm/jbig2.wasm missing — run npm install");
  process.exit(1);
}

rmSync(destRoot, { recursive: true, force: true });
mkdirSync(destRoot, { recursive: true });

for (const folder of folders) {
  const from = join(srcRoot, folder);
  const to = join(destRoot, folder);
  if (!existsSync(from)) {
    console.warn(`skip missing ${folder}`);
    continue;
  }
  cpSync(from, to, { recursive: true });
  console.log(`copied pdfjs-dist/${folder} → public/pdfjs/${folder}`);
}
