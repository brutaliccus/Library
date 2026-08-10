# README screenshots

PNG files in this folder are referenced from the root [README.md](../../README.md).

| File | Surface | Suggested viewport |
|------|---------|--------------------|
| `store-home.png` | Store / home shelves | Desktop 1440×900 |
| `search.png` | Search results | Desktop 1440×900 |
| `my-library.png` | My Library | Desktop 1440×900 |
| `book-detail.png` | Book detail / Find Downloads | Desktop 1440×900 |
| `downloads.png` | Downloads / requests | Desktop 1440×900 |
| `admin-overview.png` | Admin → Overview (health) | Desktop 1440×900 |

Until real captures exist, SVG placeholders with the same basenames sit beside this README so GitHub still renders something.

## Capture

Against a running Library instance:

```powershell
$env:LIBRARY_BASE_URL = "http://127.0.0.1:8085"
$env:LIBRARY_ADMIN_EMAIL = "you@example.com"
$env:LIBRARY_ADMIN_PASSWORD = "your-password"
node scripts/capture_readme_screenshots.mjs
```

See [Capturing screenshots](../capturing-screenshots.md) for options, login notes, and marketing polish tips.
