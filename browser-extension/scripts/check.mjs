/**
 * Lightweight sanity checks (no browser APIs).
 * Run: node browser-extension/scripts/check.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  extractMagnet,
  extractAnnasMd5,
  extractDownloadUrl,
  titleFromMagnet,
  splitTitleAuthor,
  inferMediaType,
} from "../lib/magnet.js";
import { normalizeOrigin } from "../lib/storage.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(readFileSync(join(root, "manifest.json"), "utf8"));

assert.equal(manifest.manifest_version, 3);
assert.ok(manifest.background?.service_worker);
assert.ok(manifest.permissions.includes("contextMenus"));
assert.ok(manifest.optional_host_permissions?.length);

// Chromium rejects magnet: in match patterns; that breaks contextMenus.create.
const sw = readFileSync(join(root, "background/service-worker.js"), "utf8");
const swCode = sw.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
assert.equal(
  /targetUrlPatterns/.test(swCode),
  false,
  "service worker must not use targetUrlPatterns (magnet: is unsupported)"
);
assert.match(sw, /chrome\.runtime\.onStartup/);
assert.match(sw, /chrome\.storage\.onChanged/);
assert.match(sw, /scheduleRebuildMenus/);

const magnet =
  "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Patrick+Rothfuss+-+The+Name+of+the+Wind";
assert.equal(extractMagnet(`  ${magnet}  `), magnet);
assert.equal(extractMagnet("not a magnet"), null);
assert.equal(titleFromMagnet(magnet).includes("Name of the Wind"), true);

const { title, author } = splitTitleAuthor("Patrick Rothfuss - The Name of the Wind");
assert.ok(title);
assert.ok(author);

assert.equal(
  extractAnnasMd5("https://annas-archive.org/md5/0123456789abcdef0123456789abcdef"),
  "0123456789abcdef0123456789abcdef"
);
assert.equal(
  extractDownloadUrl("https://cdn.example.com/files/book.torrent"),
  "https://cdn.example.com/files/book.torrent"
);
assert.equal(inferMediaType("Some Book.epub"), "ebook");
assert.equal(inferMediaType("Some Audiobook"), "audiobook");

assert.equal(normalizeOrigin("library.example.com"), "https://library.example.com");
assert.equal(normalizeOrigin("http://localhost:8000/"), "http://localhost:8000");

// storage.js imports chrome at runtime only via functions — normalizeOrigin is pure.
console.log("browser-extension checks passed");
