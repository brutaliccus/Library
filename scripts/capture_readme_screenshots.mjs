#!/usr/bin/env node
/**
 * Capture README feature screenshots from a running Library instance.
 * Uses Playwright via npm exec so it is not a core install dependency.
 *
 * PowerShell:
 *   $env:LIBRARY_BASE_URL = "http://127.0.0.1:8085"
 *   $env:LIBRARY_ADMIN_EMAIL = "admin@example.com"
 *   $env:LIBRARY_ADMIN_PASSWORD = "secret"
 *   node scripts/capture_readme_screenshots.mjs
 *
 * Flags: --base-url --email --password --out-dir --search --book-path --headed
 */

import { spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync, unlinkSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, "..");
const PW_VERSION = "1.51.0";

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--headed") out.headed = true;
    else if (a.startsWith("--") && i + 1 < argv.length) out[a.slice(2)] = argv[++i];
  }
  return out;
}

function requireValue(name, value) {
  if (!value) {
    console.error("Missing " + name + ". Set env or pass the matching flag.");
    process.exit(1);
  }
  return value;
}

function npmExec(args) {
  return spawnSync(
    "npm",
    ["exec", "--yes", "--package=playwright@" + PW_VERSION, "--", ...args],
    {
      cwd: REPO_ROOT,
      stdio: "inherit",
      shell: process.platform === "win32",
    }
  );
}

const args = parseArgs(process.argv.slice(2));
const baseUrl = requireValue(
  "LIBRARY_BASE_URL / --base-url",
  args["base-url"] || process.env.LIBRARY_BASE_URL
).replace(/\/$/, "");
const email = requireValue(
  "LIBRARY_ADMIN_EMAIL / --email",
  args.email || process.env.LIBRARY_ADMIN_EMAIL
);
const password = requireValue(
  "LIBRARY_ADMIN_PASSWORD / --password",
  args.password || process.env.LIBRARY_ADMIN_PASSWORD
);
const outDir = resolve(
  REPO_ROOT,
  args["out-dir"] || process.env.LIBRARY_SCREENSHOT_DIR || "docs/images"
);
const searchQuery = args.search || process.env.LIBRARY_SEARCH_QUERY || "harry";
const bookPathOverride = args["book-path"] || process.env.LIBRARY_BOOK_PATH || "";
const headed = Boolean(args.headed || process.env.LIBRARY_SCREENSHOT_HEADED);

mkdirSync(outDir, { recursive: true });

const workerPath = join(REPO_ROOT, "scripts", ".capture_readme_screenshots_worker.mjs");

const workerSource = [
  "import { chromium } from 'playwright';",
  "import { mkdirSync } from 'node:fs';",
  "import { join } from 'node:path';",
  "",
  "const baseUrl = " + JSON.stringify(baseUrl) + ";",
  "const email = " + JSON.stringify(email) + ";",
  "const password = " + JSON.stringify(password) + ";",
  "const outDir = " + JSON.stringify(outDir) + ";",
  "const searchQuery = " + JSON.stringify(searchQuery) + ";",
  "const bookPathOverride = " + JSON.stringify(bookPathOverride) + ";",
  "const headed = " + JSON.stringify(headed) + ";",
  "",
  "mkdirSync(outDir, { recursive: true });",
  "",
  "async function login(page) {",
  "  await page.goto(baseUrl + '/login', { waitUntil: 'domcontentloaded' });",
  "  await page.waitForSelector('input[autocomplete=\"username\"], input[type=\"email\"], input[type=\"text\"]', { timeout: 30000 });",
  "  const emailInput = (await page.$('input[autocomplete=\"username\"]')) || (await page.$('input[type=\"email\"]')) || (await page.$('form input[type=\"text\"]'));",
  "  const passwordInput = await page.$('input[type=\"password\"]');",
  "  if (!emailInput || !passwordInput) throw new Error('Could not find login form fields on /login');",
  "  await emailInput.fill(email);",
  "  await passwordInput.fill(password);",
  "  await Promise.all([",
  "    page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 45000 }),",
  "    page.click('button[type=\"submit\"]'),",
  "  ]);",
  "}",
  "",
  "async function settle(page) {",
  "  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});",
  "  await page.waitForTimeout(800);",
  "}",
  "",
  "async function shot(page, name, pathOrUrl) {",
  "  const target = pathOrUrl.startsWith('http') ? pathOrUrl : baseUrl + (pathOrUrl.startsWith('/') ? '' : '/') + pathOrUrl;",
  "  console.log('-> ' + name + ': ' + target);",
  "  await page.goto(target, { waitUntil: 'domcontentloaded', timeout: 60000 });",
  "  await settle(page);",
  "  const file = join(outDir, name + '.png');",
  "  await page.screenshot({ path: file, fullPage: false });",
  "  console.log('  wrote ' + file);",
  "}",
  "",
  "const browser = await chromium.launch({ headless: !headed });",
  "const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });",
  "const page = await context.newPage();",
  "try {",
  "  await login(page);",
  "  await settle(page);",
  "  await shot(page, 'store-home', '/');",
  "  await shot(page, 'search', '/search?q=' + encodeURIComponent(searchQuery));",
  "  await shot(page, 'my-library', '/my-library');",
  "  let bookPath = bookPathOverride;",
  "  if (!bookPath) {",
  "    await page.goto(baseUrl + '/', { waitUntil: 'domcontentloaded' });",
  "    await settle(page);",
  "    bookPath = await page.evaluate(() => {",
  "      const anchors = Array.from(document.querySelectorAll('a[href*=\"/book/\"]'));",
  "      const hit = anchors.find((a) => { try { return new URL(a.href, location.origin).pathname.startsWith('/book/'); } catch { return false; } });",
  "      if (!hit) return '';",
  "      const u = new URL(hit.href, location.origin);",
  "      return u.pathname + u.search;",
  "    });",
  "  }",
  "  if (bookPath) await shot(page, 'book-detail', bookPath);",
  "  else console.warn('No /book/ link found. Set LIBRARY_BOOK_PATH=/book/... to capture book-detail.png');",
  "  await shot(page, 'downloads', '/downloads');",
  "  await shot(page, 'admin-overview', '/admin');",
  "  console.log('\\nDone. PNGs are in ' + outDir);",
  "} finally {",
  "  await browser.close();",
  "}",
].join("\n");

writeFileSync(workerPath, workerSource, "utf8");

console.log("Ensuring Playwright Chromium is installed...");
const install = npmExec(["playwright", "install", "chromium"]);
if (install.status !== 0) {
  try { unlinkSync(workerPath); } catch {}
  process.exit(install.status ?? 1);
}

console.log("Capturing screenshots from " + baseUrl + " ...");
const run = npmExec(["node", workerPath]);
try { unlinkSync(workerPath); } catch {}
process.exit(run.status ?? 1);
