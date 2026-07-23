# Library Site — Browser Extension

Chrome / Brave (Manifest V3) extension that right-clicks **magnet links** (and related download URLs) and creates a request in your Library Site queue — the same request pipeline as searching in the app.

## Install (unpacked)

### Chrome
1. Open `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked**
4. Select this folder: `browser-extension/`

### Brave
1. Open `brave://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked**
4. Select this folder: `browser-extension/`

After loading, pin the extension if you like, then open **Settings** (extension options) to connect a library.

## Connect a library

1. Open the extension **Options** page (popup → Settings, or right-click the extension icon → Options).
2. Enter your **Library URL** (e.g. `https://library.example.com` or `http://localhost:8000`).
3. Sign in with the same **email + password** used in the web/app.
4. When prompted, **Allow** host access for that library origin.
5. You can connect multiple libraries; the context menu becomes a submenu.

**Advanced:** “Paste tokens instead” accepts an access + refresh JWT pair (e.g. copied from a logged-in session) if you prefer not to type a password into the extension.

**Disconnect:** Use **Disconnect** on any saved library to remove tokens from `chrome.storage.local`.

## Usage

1. On a torrent site (e.g. AudioBook Bay), find a **magnet:** link.
2. Right-click the link → **Send to [Library Name]** (or **Send to Library** → pick one).
3. Or select a magnet URL text → **Send selection to …**.
4. A notification confirms success or failure. If login expired, you’ll be prompted to reconnect.

Also supported when the link matches:
- Anna’s Archive `/md5/{hash}` pages (ebook request via `annas_archive`)
- Direct `.torrent` file URLs

## API endpoints used

| Action | Method | Path | Notes |
|--------|--------|------|--------|
| Login | `POST` | `/api/auth/login` | Body: `{ email, password }` |
| Refresh | `POST` | `/api/auth/refresh` | Body: `{ refresh_token }` |
| Profile | `GET` | `/api/auth/me` | Token paste validation |
| Library name | `GET` | `/api/libraries/me` | Display name for menus |
| Create request | `POST` | `/api/requests` | Same payload as the web app |

### Example request body (magnet)

```json
{
  "title": "Book Title",
  "author": "Author Name",
  "magnet_link": "magnet:?xt=urn:btih:…&dn=…",
  "media_type": "audiobook",
  "indexer": "Browser Extension",
  "source": "browser_extension"
}
```

Auth header: `Authorization: Bearer <access_token>`.

Access tokens expire (~15 minutes); the extension refreshes with the refresh token (~7 days) before calling the API.

## Backend changes

**None.** The existing `POST /api/requests` API already accepts `magnet_link` / `download_url` with JWT auth. The service worker uses optional host permissions, so library API calls do not depend on server CORS allowlisting `chrome-extension://` origins.

## Permissions justification

| Permission | Why |
|------------|-----|
| `contextMenus` | Right-click “Send to Library” on magnets / selections |
| `storage` | Saved library origins + JWT sessions (`chrome.storage.local`) |
| `notifications` | Success / failure toasts (no silent failures) |
| `alarms` | Periodic context-menu rebuild after service worker sleep |
| `optional_host_permissions` (`http://*/*`, `https://*/*`) | Requested **only** for each library base URL you add, so the extension can call that server’s `/api/*` |

The extension does **not** read browsing history or inject content scripts into torrent sites.

## Security notes

- Tokens live in `chrome.storage.local` (and are removed on Disconnect).
- Magnets and tokens are not written to logs.
- Prefer HTTPS library URLs in production.
- If a refresh fails, settings open so you can reconnect.

## Manual test plan

### Against a local Library Site

1. Run the backend (e.g. `http://localhost:8000`) with a user who has completed library onboarding (debrid keys or server fallback).
2. Load the unpacked extension; connect `http://localhost:8000` with that user.
3. Click **Test** on the connected library — expect “Session OK”.
4. Open any page, paste a magnet into the address bar or a text field, select it, right-click → **Send selection to …**.
5. Confirm a browser notification and a new row under **Requests** in the web app (`pending` → pipeline progress).
6. Sign out / invalidate refresh (or wait for expiry after deleting refresh token via Disconnect + reconnect) and confirm expired sessions open Options with a clear error.
7. Connect a second library URL (or second account) and confirm the submenu lists both names.

### Against ABB / similar

1. Open an AudioBook Bay (or similar) release page.
2. Right-click the **magnet** link → Send to Library.
3. Verify the request title roughly matches the magnet `dn=` name and the download pipeline picks it up (Real-Debrid / Torbox as configured).

## Layout

```
browser-extension/
  manifest.json
  README.md
  background/service-worker.js
  lib/{api,storage,magnet}.js
  options/
  popup/
  icons/
  scripts/check.mjs          # lightweight sanity checks
```

## Sanity check

```bash
node browser-extension/scripts/check.mjs
```
