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

## Notes

- Rebuild/sync the APK when you want UI changes in the store build (`npm run android:sync`).
- Streaming, offline cache, media session, and Android Auto still work; API calls
  use the stored server URL instead of same-origin.
- Prefer HTTPS for the library URL. Cleartext HTTP may be blocked by Android.
