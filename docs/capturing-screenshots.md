# Capturing README screenshots

You can automate screenshots with Playwright, or take them manually for a more polished marketing look.

## Automation (recommended for refresh)

`scripts/capture_readme_screenshots.mjs` logs into a running Library instance and writes PNGs into `docs/images/`.

### Prerequisites

- Node.js 18+
- A running Library app (Docker or local) with at least one admin account and some catalog/library content
- First run downloads Chromium via Playwright (~one-time)

### Environment

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `LIBRARY_BASE_URL` | yes* | — | e.g. `http://127.0.0.1:8085` or your HTTPS URL |
| `LIBRARY_ADMIN_EMAIL` | yes* | — | Admin login email / username |
| `LIBRARY_ADMIN_PASSWORD` | yes* | — | Admin password |
| `LIBRARY_SCREENSHOT_DIR` | no | `docs/images` | Output directory |
| `LIBRARY_BOOK_PATH` | no | auto | Path like `/book/...` if home has no book links |
| `LIBRARY_SEARCH_QUERY` | no | `harry` | Query used on `/search` |
| `PLAYWRIGHT_BROWSERS_PATH` | no | Playwright default | Optional browser cache path |

\* Or pass `--base-url`, `--email`, `--password` flags.

### Run (Windows PowerShell)

```powershell
$env:LIBRARY_BASE_URL = "http://127.0.0.1:8085"
$env:LIBRARY_ADMIN_EMAIL = "admin@example.com"
$env:LIBRARY_ADMIN_PASSWORD = "your-password"
node scripts/capture_readme_screenshots.mjs
```

### Run (Linux / macOS)

```bash
export LIBRARY_BASE_URL="http://127.0.0.1:8085"
export LIBRARY_ADMIN_EMAIL="admin@example.com"
export LIBRARY_ADMIN_PASSWORD="your-password"
node scripts/capture_readme_screenshots.mjs
```

The script uses `npx --yes playwright` so Playwright is not added to the app’s required install dependencies.

### What it captures

1. Store / home (`/`)
2. Search (`/search?q=…`)
3. My Library (`/my-library`)
4. Book detail (first `/book/…` link found, or `LIBRARY_BOOK_PATH`)
5. Downloads (`/downloads`)
6. Admin Overview (`/admin`)

## Manual capture (best for advertising)

Automation is great for keeping docs current. For launch marketing, a short manual pass often looks better:

1. Use a clean desktop browser window (hide bookmarks bar; zoom 100%).
2. Prefer a demo library with covers, series shelves, and a few in-progress downloads.
3. Crop to the content, not the whole OS desktop.
4. Export PNG (or WebP) at ~2× retina if you want crisp GitHub rendering.
5. Replace the files in `docs/images/` using the names in [images/README.md](images/README.md).

Mobile / Android Auto: capture on a device or emulator separately; the web script does not replace APK screenshots.

## Privacy

Do not commit screenshots that show real API keys, private emails, invite codes, or library content you do not want public. Prefer a throwaway admin and sample catalog data for public README images.
