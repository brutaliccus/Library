# Android App (Capacitor)

The Android app ships a **bundled** copy of the web UI. On first launch, users
enter their self-hosted Library URL (HTTPS) when signing in or requesting an
account. The URL is stored on the device and can be changed later under
**Settings → Library server**.

One prebuilt APK works with any Library instance — nothing is hardcoded at build
time.

## Building the APK

Prerequisites: Android Studio with an SDK installed (API 34+ recommended).

```bash
cd frontend
npm run android:sync   # builds the web app into backend/static + syncs Android
npm run android:open   # opens the project in Android Studio
```

In Android Studio:

1. Open **`frontend/android`** as the project root (File > Open — not the repo root, not `frontend/`).
2. Wait for **Gradle sync** to finish (bottom status bar). If sync failed, fix that first — several Build menu items stay grayed out until sync succeeds.
3. For a quick installable APK (fine for personal use on your own phone):
   - **Build > Build App Bundle(s) / APK(s) > Build APK(s)**
   - Output: `frontend/android/app/build/outputs/apk/debug/app-debug.apk`
   - This is debug-signed automatically; install with `adb install app-debug.apk` or copy to the phone.

4. For a **release / signed** APK, use one of these (the exact menu label varies by Android Studio version):
   - **Build > Generate Signed App Bundle or APK…** (if present)
   - Or press **Ctrl+Shift+A** (Find Action), type `signed`, pick **Generate Signed App Bundle or APK**
   - Create (or reuse) a keystore — keep it safe, you need the same one for updates.
   - Choose **APK**, `release` variant, finish.

   Release output (unsigned until you sign via the wizard):  
   `frontend/android/app/build/outputs/apk/release/app-release-unsigned.apk`

## First launch (users)

1. Open the app → enter **Library server URL** (e.g. `https://library.example.com`)
2. Create the admin account, sign in, or request an account
3. Change the URL anytime in **Settings → Library server** (signs you out)

Your server must allow CORS from the Capacitor WebView origin (`https://localhost`).
Current backend builds already include this.

## In-app APK updates (GitHub Releases)

Signed-in Android users get:

- A **blocking “Update required” modal** when a newer APK is available and
  **Force Android APK updates** is on (Admin default), or when the installed
  `versionCode` is below **Minimum Android versionCode** (default **51** = 1.50)
- A soft **Update available** banner (dismissible) only when force updates are
  turned off and the install is still above the minimum
- A system notification (when the app is in the background) with **Update now**
  (no Dismiss action when the update is required)
- **Settings → Android app update** to check / download manually

After each APK version change, the app clears the WebView HTTP/asset cache so
bundled SPA files are not stuck on the previous build.

The server calls GitHub `releases/latest` for the configured repo (default
`brutaliccus/Library`) and looks for a `.apk` asset. The update API also returns
`minVersionCode` and `forceUpdate` from Admin → Config → **Android / mobile**.

Themed app icons (ocean / ember / forest / dusk) are generated with
`npm run icons:themed` in `frontend/` (PWA favicons + Android mipmaps). The app
switches launcher and Android Auto icons when the UI theme changes.

Put `versionCode: N` in the
release body (the GitHub Action does this automatically).

### Publish a release

1. Add Actions secrets: `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`,
   `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD`
2. Prefer **`.\deploy.ps1`** — after the Pi deploy it detects APK-relevant changes
   since the last `android-v*` tag, bumps `versionName` / `versionCode`, commits,
   and pushes `android-v{name}+{code}` which starts the Actions build.
   - Skip: `.\deploy.ps1 -SkipApk`
   - Force: `.\deploy.ps1 -ForceApk`
   - Standalone: `.\scripts\release_android_apk.ps1` (optional `-Force` / `-DryRun`)
3. Or manually: **Actions → Android APK release → Run workflow**, or push a tag
   like `android-v1.5+6`
4. Users on older builds will be forced to update after their next check (when
   force updates / min version apply). Raise **Minimum Android versionCode** in
   Admin after a required release if you need to block a specific floor.

Working tree must be **clean** for the auto-release step (so the tag matches what
you deployed). Commit first, then deploy.

Admin → Config → **Android APK GitHub repo** can point at a fork. Optional GitHub
token raises API rate limits. **Force Android APK updates** and **Minimum Android
versionCode** control the hard gate (defaults: force on, min = 51).

## Notes

- Rebuild/sync the APK when you want UI changes in the store build (`npm run android:sync`).
- Streaming, offline cache, media session, and Android Auto still work; API calls
  use the stored server URL instead of same-origin.
- Prefer HTTPS for the library URL. Cleartext HTTP may be blocked by Android.
- Keep using the **same signing keystore** for every release or Android will refuse
  the in-place update.
