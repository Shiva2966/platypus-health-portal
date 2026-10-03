# Health Portal - Clients (Android APK + Windows hospital app)

**DEMO ONLY. Synthetic data. Do not enter real health information.** The apps talk to the demo
backend over plain HTTP on your LAN.

```
health-portal-clients/
  config/server.json          <- shared default server URL (edit this one file)
  android-app/                <- Capacitor project -> patient APK
  desktop-app/                <- Electron project  -> hospital Windows app
  HealthPortal-patient-debug.apk   <- built APK (copy of the Gradle output)
  scripts/                    <- helper scripts (firewall, show IP)
```

> **Current setup (2026-10-03): public HTTPS via Cloudflare Tunnel - use this instead of the LAN steps below.**
> The backend runs in production mode (Secure cookies need HTTPS) and listens on 127.0.0.1 only. Start it with
> `powershell -ExecutionPolicy Bypass -File C:\Users\sav11\Projects\health-portal\scripts\start_public.ps1`; it prints an
> `https://<random>.trycloudflare.com` URL and writes it to `config/server.json`. Enter that URL in the APK's Server URL
> screen / the desktop app's File > Server Settings. The URL changes on every restart. Details:
> `health-portal\docs\PUBLIC_ACCESS.md`.

Both clients are thin shells around the backend's web UI (`/` = patient, `/staff/` = hospital).
Nothing is duplicated: change the backend and both clients pick it up (no rebuild needed).

---------------------------------------------------------------------------------------------------

## 0. One-time network setup (laptop)

### Find the laptop LAN IP
```powershell
ipconfig | findstr /i "IPv4"
# or, nicer:
powershell -ExecutionPolicy Bypass -File scripts\show-ip.ps1
```
Use the IPv4 of your **Wi-Fi** adapter. Typical home/hotspot values look like `192.168.x.x` or `10.x.x.x`.
Phone and laptop must be on the **same Wi-Fi network**.

> Heads-up for this machine: when this was set up, Wi-Fi had `100.70.130.48` (a 100.64.0.0/10
> "carrier-grade NAT" range) and Tailscale had `100.114.78.114`. Many shared/guest/campus Wi-Fi
> networks isolate clients from each other ("AP/client isolation"), so a phone cannot reach the laptop even
> with the right IP. If that happens use your **phone's hotspot** (laptop joins the phone's Wi-Fi) or a
> home router, or install Tailscale on the phone and use the laptop's Tailscale IP.

### Open Windows Firewall for port 8000 (run PowerShell **as Administrator**)
```powershell
New-NetFirewallRule -DisplayName "Health Portal 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private,Public
```
(or `powershell -ExecutionPolicy Bypass -File scripts\open-firewall.ps1` as Administrator).
Remove it afterwards: `Remove-NetFirewallRule -DisplayName "Health Portal 8000"`.

### Start the backend (must listen on 0.0.0.0, not 127.0.0.1)
```powershell
cd C:\Users\sav11\Projects\health-portal
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# if the project uses its venv:  .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Check from the laptop: `curl.exe http://<LAN-IP>:8000/` and `curl.exe http://<LAN-IP>:8000/staff/`
Check from the phone: open Chrome and visit `http://<LAN-IP>:8000/`. If that does not load,
the APK will not load either (firewall / wrong IP / Wi-Fi isolation).

### Set the default server URL
Edit `config/server.json`:
```json
{ "serverUrl": "http://192.168.1.50:8000" }
```
This is only the **default** baked into new builds. Each app also has its own settings screen
(below), so you can change the URL at any time without rebuilding. The placeholder
`192.168.1.50` is NOT your IP - change it.

---------------------------------------------------------------------------------------------------

## 1. Patient app (Android APK)

### What it is
Capacitor wrapper. On launch it shows a small local screen, tests the saved server URL, then opens
the patient portal (`http://<ip>:8000/`) full screen. Package id `com.healthportal.demo`.

Features: first-run URL screen; cleartext HTTP to the LAN allowed (network security config +
`usesCleartextTraffic`); file/PDF upload via the system picker (Capacitor's WebChromeClient);
camera permission declared (photo capture in file picker); downloads go through Android
DownloadManager (session cookie forwarded), land in *Downloads*, and open in the phone's PDF/image viewer.

**Settings:** press **Back** while on the portal's first page - the Server URL screen appears
(also appears automatically if the server is unreachable). Change URL, *Test connection*, *Save & open*.

### Install the APK on a phone
The prebuilt debug APK: `C:\Users\sav11\Projects\health-portal-clients\HealthPortal-patient-debug.apk`
(original Gradle output: `android-app\android\app\build\outputs\apk\debug\app-debug.apk`).

**Option A - adb (USB cable):**
1. Phone: Settings > About phone > tap *Build number* 7 times; then Developer options > enable *USB debugging*.
2. Plug in the phone, accept the "Allow USB debugging" prompt.
3. ```powershell
   $adb = "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
   & $adb devices                       # phone must show as "device"
   & $adb install -r C:\Users\sav11\Projects\health-portal-clients\HealthPortal-patient-debug.apk
   ```

**Option B - file transfer:** copy the APK to the phone (USB file transfer, Google Drive, Telegram "saved
messages", email to yourself...), open it in the phone's *Files* app, allow "Install unknown apps" for
that app when asked, tap *Install*. (Play Protect may warn that it is an unknown/debug app - choose
*Install anyway*.)

### Rebuild the APK (needed only if you change `android-app/` code, `www/`, or the default URL)
Requirements already installed on this laptop: Node 24, **JDK 21** (Capacitor 7 needs 21; installed with
`winget install Microsoft.OpenJDK.21`, wired in through `android-app\android\gradle.properties`),
Android SDK in `%LOCALAPPDATA%\Android\Sdk` with platform 35 + build-tools.
```powershell
cd C:\Users\sav11\Projects\health-portal-clients\android-app
npm install                                  # first time only
npm run sync                                 # copies config/server.json + web assets into the Android project
cd android
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
.\gradlew.bat assembleDebug                  # first build ~3-5 min, later ones ~1 min
Copy-Item app\build\outputs\apk\debug\app-debug.apk ..\..\HealthPortal-patient-debug.apk
```
(`android\local.properties` holds `sdk.dir=...`; if you move machines, edit that path.)

### Known limits of the APK (be aware)
* **In-page QR/camera scanning (getUserMedia) will NOT work over plain `http://<ip>`.** Android WebView only
  allows camera access on secure origins (HTTPS or localhost). The patient *shows* the QR/token, which needs
  no camera. Photo capture through the upload picker works (it uses the camera app, not getUserMedia).
  The hospital side scans on the **desktop app**, which is configured to treat your server as secure.
  If you ever need scanning on the phone: put the backend behind HTTPS (e.g. a Tailscale/ngrok/Cloudflare tunnel).
* Debug-signed APK, for demos only (not Play Store ready).
* Downloads that the portal creates in JavaScript as `blob:` URLs are not supported by the native download
  handler; normal link/endpoint downloads (what the portal uses for documents) are.
* If the server cookie is set `Secure`, login will fail over HTTP in any browser or app - not an APK issue.

### Fallback if the APK does not work: install the PWA from Chrome
1. Phone Chrome > `http://<LAN-IP>:8000/`
2. Menu (three dots) > **Add to Home screen** / **Install app**.
   (Over plain HTTP, Chrome offers a home-screen *shortcut*; the full "install" with service worker needs HTTPS.
   For this demo the shortcut opens the portal fine.)

---------------------------------------------------------------------------------------------------

## 2. Hospital desktop app (Windows)

Electron shell opening `http://<ip>:8000/staff/`.

Features: settings dialog (**File > Server Settings**, `Ctrl+,`) - saved in
`%APPDATA%\Health Portal Hospital\config.json`, app restarts after saving; menu (File / Navigate / View / Help);
**File > Open PDF** (`Ctrl+O`) and **drag-and-drop a PDF onto the window** open it in a built-in PDF viewer
window (drops on the portal's own upload fields are left to the portal); downloads are saved to *Downloads*
and opened automatically; webcam allowed for the portal origin so QR scanning works over HTTP;
friendly error page with *Retry* when the server is down; single-instance.

### Run from source (simplest, always works)
```powershell
cd C:\Users\sav11\Projects\health-portal-clients\desktop-app
npm install        # first time only
npm start
```

### Build the Windows installer / portable exe
```powershell
cd C:\Users\sav11\Projects\health-portal-clients\desktop-app
npm install
npm run dist
```
Outputs in `desktop-app\dist\`:
* `HealthPortalHospital-1.0.0-portable.exe` - no install, double-click
* `HealthPortalHospital-Setup-1.0.0.exe` - installer
* `win-unpacked\Health Portal Hospital.exe` - unpacked run directly

`npm run pack` builds only the unpacked folder (fastest). Unsigned binaries: Windows SmartScreen may say
"Windows protected your PC" - *More info > Run anyway*.

If `npm run dist` fails with a symlink/permission error from winCodeSign, enable Windows
*Developer Mode* (Settings > For developers) or run PowerShell as Administrator, then retry;
`npm start` always works as a fallback.

---------------------------------------------------------------------------------------------------

## 3. Typical demo flow
1. `config/server.json` -> your laptop IP (optional, can also be set in each app).
2. Firewall rule once; start backend with `--host 0.0.0.0`.
3. Phone: install APK, first-run screen, enter `http://<LAN-IP>:8000`, Save.
4. Laptop: run hospital app (`npm start` or the .exe); File > Server Settings if the URL differs.
5. Patient uploads a PDF and shares it; hospital requests/redeems access and opens the PDF.

## 4. Troubleshooting
| Symptom | Check |
|---|---|
| APK says "Cannot reach ..." | Phone Chrome can open `http://<ip>:8000/`? Same Wi-Fi? Firewall rule? Backend started with `--host 0.0.0.0`? Wi-Fi client isolation (use phone hotspot)? |
| Desktop app shows error page | Backend running? Server Settings URL correct? `curl.exe http://<ip>:8000/staff/` |
| Staff page 404 | The backend must serve `/staff/` (hospital module may not be finished/mounted yet). |
| `adb devices` shows `unauthorized` | Accept the debugging prompt on the phone. |
| Gradle: `invalid source release: 21` | Gradle is running on JDK 17; `android\gradle.properties` must point `org.gradle.java.home` at JDK 21. |
