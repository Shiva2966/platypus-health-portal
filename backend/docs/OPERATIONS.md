# Operations runbook (laptop deployment)

The live server runs on this Windows laptop: uvicorn on `127.0.0.1:8000` behind a public HTTPS tunnel
(`scripts\start_public.ps1`, see `docs/PUBLIC_ACCESS.md`). This page covers keys, backups, restore, uptime
and the security limits. All commands run from `C:\Users\sav11\Projects\health-portal`.

## 1. Keys in `.env` - back them up NOW

`.env` holds the secrets (`SECRET_KEY`, `SMTP_PASSWORD`, `DATA_ENCRYPTION_KEY`, `READYZ_TOKEN`, ...). Its file
permissions allow only your Windows account (`icacls .env` shows a single `UOFI\sav11:(F)` entry).

> **`DATA_ENCRYPTION_KEY` encrypts every uploaded file in the database and every off-site backup.
> If you lose it, those files are gone for good. Nobody, including the developers, can recover them.**
> Copy the line `DATA_ENCRYPTION_KEY=...` from `.env` into a password manager (or print it and keep it in a
> safe) **today**, somewhere that is NOT the laptop and NOT the backup folder. Do the same after any rotation.

Show the value only when you are copying it: `notepad .env`.

### Encryption at rest
* Uploaded files (`document_blobs.data`) are encrypted with AES-256-GCM in the application
  (`app/data_crypto.py`): encrypted on write, decrypted on read, with a versioned header. Rows written before
  the key existed stay readable and are converted by `scripts\encrypt_existing_blobs.py`.
  The SHA-256 in `documents.sha256` is of the plaintext, so `scripts\verify_blobs.py` still proves integrity
  (it also reports how many files are encrypted vs plaintext, and any that cannot be decrypted).
* The rest of the database (names, records, audit log) is not encrypted by the app. That is what disk
  encryption is for: **BitLocker** on drive C:. Check (Administrator PowerShell): `manage-bde -status C:`
  -> "Protection Status: Protection On". If it is off: Settings > Privacy & security > Device encryption
  (or Control Panel > BitLocker Drive Encryption > Turn on), and save the recovery key outside the laptop.
* Rotating the data key: move the old value to `DATA_ENCRYPTION_KEYS_OLD=<old>`, generate a new one
  (`set_env.py --generate=DATA_ENCRYPTION_KEY` after deleting the old line), restart with
  `start_public.ps1 -AppOnly`, then `.venv\Scripts\python.exe scripts\encrypt_existing_blobs.py --rotate`.
  Keep the old key until no backup that needs it is still retained.

## 2. Backups

* Scheduled task **HealthPortalBackup** runs `scripts\backup.ps1 -VerifyRestore -Log` every 6 hours and 10
  minutes after logon. Each run makes a consistent SQLite snapshot in `backups\` plus a manifest (row counts,
  hashes; no patient data), restores it into a scratch database to prove it works, and keeps the newest 28
  (7 days). Log: `data\logs\backup.log`.
* Manual run: `.venv\Scripts\python.exe scripts\backup.py --verify-restore`
* Check the tasks: `Get-ScheduledTask HealthPortal* | Get-ScheduledTaskInfo`
  (re-create them with `powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1`).

### Off-site copy (recommended)
Local backups die with the laptop. Set an off-site folder and every backup is also written there **encrypted**
(`<name>.sqlite3.enc`, AES-256-GCM with `BACKUP_ENCRYPTION_KEY`, or `DATA_ENCRYPTION_KEY` when that is empty),
decrypted once to verify it, and pruned to `BACKUP_OFFSITE_KEEP` (default 28):

```powershell
.venv\Scripts\python.exe scripts\set_env.py "BACKUP_OFFSITE_DIR=C:\Users\sav11\OneDrive\HealthPortalBackups"
```

OneDrive is present on this laptop at `C:\Users\sav11\OneDrive`, so that folder syncs to the cloud. It's a
university (UOFI) account, so check that its policy allows storing this data, even encrypted. Any other synced
folder or USB drive also works. The encrypted copies are useless without the key (section 1).

### Restore
1. Stop the app: `powershell -ExecutionPolicy Bypass -File scripts\start_public.ps1 -Stop`
   (the watchdog restarts it within ~30 s, so first run `Stop-ScheduledTask HealthPortalWatchdog`, and end any
   `watchdog.ps1` powershell process).
2. Restore into a new file and check it:
   `.venv\Scripts\python.exe scripts\restore.py --latest --target sqlite:///data/restored.db`
   or an off-site copy: `... scripts\restore.py C:\Users\sav11\OneDrive\HealthPortalBackups\healthportal_<stamp>.sqlite3.enc --target sqlite:///data/restored.db`
   It must say `verification: PASSED`. Encrypted copies need the same key in `.env`.
3. Swap it in: rename `data\app.db` (and any `-wal`/`-shm`) to `*.before-restore`, rename `data\restored.db`
   to `data\app.db`.
4. `.venv\Scripts\python.exe scripts\verify_blobs.py` -> `RESULT: CLEAN`, then start again
   (`start_public.ps1`, or `Start-ScheduledTask HealthPortalWatchdog`).

## 3. Uptime on the laptop

* Scheduled task **HealthPortalWatchdog** runs `scripts\watchdog.ps1` 30 s after logon. It starts the tunnel
  and app when nothing is running, restarts **only the app** (same URL) when `/healthz` fails 3 times in a
  row, and does a full restart if the tunnel process is gone (a quick tunnel then gets a NEW URL). Log:
  `data\logs\watchdog.log`. The app's own output is in `data\public\uvicorn*.log` (rotated at every restart,
  3 kept; no access log, no query strings, errors without messages).
* Restart only the app (keeps the tunnel and URL): `powershell -ExecutionPolicy Bypass -File scripts\start_public.ps1 -AppOnly`
* Tasks only run while you are signed in to Windows. Locking the screen is fine; signing out stops the server.

### Power settings (checked 2026-10-03, read-only)
* Sleep after: **Never** on AC and on battery. Hibernate after: Never. Nothing needed to be changed.
* **Lid close action: Shut down** (on AC and battery). Closing the lid turns the server off. To keep it running
  with the lid closed while plugged in (not changed for you):
  ```powershell
  powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
  powercfg /setactive SCHEME_CURRENT
  ```
* If sleep is ever re-enabled, turn it off while plugged in: `powercfg /change standby-timeout-ac 0` and `powercfg /change hibernate-timeout-ac 0`.
* The machine supports Modern Standby (S0). Windows Update restarts will also stop it; the watchdog brings it
  back at the next logon.

## 4. Security limits (production defaults)

| What | Default | Setting |
|---|---|---|
| Any `/api/*` request, per client IP | 600/min | `API_RATE_LIMIT_PER_MINUTE` |
| Sign-in / sign-up POSTs | 30/min | `AUTH_RATE_LIMIT_PER_MINUTE` |
| Code entry/sending (verify, resend, forgot/reset password, email change) | 10/min | `OTP_RATE_LIMIT_PER_MINUTE` |
| Document uploads (POST/PUT `/api/documents...`) | 20/min | `UPLOAD_RATE_LIMIT_PER_MINUTE` |
| Wrong staff invite codes | 5 per 15 min | `INVITE_MAX_FAILURES` |
| Idle sign-out | patient 30 min, staff 20 min | `SESSION_IDLE_MINUTES_PATIENT/_STAFF` |
| Absolute session lifetime | 8 h | (code) |
| Upload size | 15 MB per file, 20 MB per request | `MAX_REQUEST_BYTES` |

Over a limit the API returns **429** with `Retry-After`. The client IP comes from `CF-Connecting-IP` (or the
rightmost `X-Forwarded-For` entry), and only when the request comes from a trusted local proxy
(`TRUSTED_PROXY_IPS`, default `127.0.0.1,::1`). Anyone else's forwarding headers are ignored. The limits live
in memory, so they reset when the app restarts. Badge/SSE polling does not count as activity for the idle
timeout.

The staff invite code is case-insensitive, and spaces and dashes are ignored. It's compared in constant time.

## 5. Health checks
* `GET /healthz` - process is up.
* `GET /readyz` - from the internet: only `{"status":"ok"}` (200) or `{"status":"unavailable"}` (503).
  Full checks (database, migrations, audit triggers, mailer) are shown to direct localhost requests or with the
  token: `Invoke-RestMethod https://<url>/readyz -Headers @{"X-Readyz-Token"=(Select-String '^READYZ_TOKEN=(.*)' .env).Matches[0].Groups[1].Value}`

## 6. Dependency check
`.venv\Scripts\python.exe -m pip install pip-audit` then `.venv\Scripts\python.exe -m pip_audit`.
2026-10-03: the only findings were in `pip` itself (24.2, upgraded to 26.2.1); none in the app's runtime packages.
