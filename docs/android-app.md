# Android App (Capacitor)

The Android app is a thin native shell (Capacitor) that loads the hosted web
app from `https://library.freiverse.com`. Because it points at the live
server:

- Every web deploy updates the app instantly — no APK rebuilds needed.
- The service-worker audio cache (instant resume) works the same as the PWA.
- Lock-screen / notification media controls work via the Media Session API.

The URL is set in `frontend/capacitor.config.ts` (`server.url`).

## Building the APK

Prerequisites: Android Studio with an SDK installed (API 34+ recommended).

```bash
cd frontend
npm run android:sync   # builds the web app + syncs the android project
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

   From a terminal (after signing is configured in Android Studio, or for debug only):

   ```powershell
   cd frontend\android
   .\gradlew.bat assembleDebug    # debug APK, no wizard needed
   .\gradlew.bat assembleRelease  # release APK (needs signing config for install)
   ```

## Changing the app icon / name

- Name: `frontend/android/app/src/main/res/values/strings.xml`
- Icons: replace the `mipmap-*` folders under `frontend/android/app/src/main/res/`
  (Android Studio: right-click `res` > New > Image Asset).

## Re-syncing after config changes

Any change to `capacitor.config.ts` requires:

```bash
cd frontend && npx cap sync android
```
