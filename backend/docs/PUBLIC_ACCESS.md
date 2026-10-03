# Public HTTPS access (phones on any network, desktop app, APK)

The laptop's Wi-Fi sits behind carrier-grade NAT (100.70.x.x) and many networks isolate clients, so LAN access
is unreliable. Production mode also sets **Secure** cookies, which browsers only send over HTTPS. The fix that
works everywhere is a **Cloudflare Tunnel**: `cloudflared` makes an outbound connection to Cloudflare, and
Cloudflare serves the app at a public `https://` address. No router, firewall or port-forwarding changes are needed.

```
phone / desktop app --HTTPS--> Cloudflare edge --tunnel--> cloudflared (laptop) --HTTP--> uvicorn 127.0.0.1:8000
```

## Start / stop

```powershell
cd C:\Users\sav11\Projects\health-portal
powershell -ExecutionPolicy Bypass -File scripts\start_public.ps1          # start (prints the public URL)
powershell -ExecutionPolicy Bypass -File scripts\start_public.ps1 -Stop    # stop server + tunnel
powershell -ExecutionPolicy Bypass -File scripts\start_public.ps1 -AppOnly # restart only the app (same tunnel + URL)
```

What `start_public.ps1` does:

1. Stops the server and tunnel it started last time (pid files in `data\public\`; logs are there too).
2. Starts `cloudflared tunnel --url http://127.0.0.1:8000` and reads the `https://<random>.trycloudflare.com` URL.
3. Sets `ALLOWED_ORIGINS` in `.env` (only that key, via `scripts\set_env.py`) to the tunnel URL plus
   `https://localhost` and `capacitor://localhost` (the Android app's local start page).
4. Starts uvicorn on **127.0.0.1** with `--proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log` and
   `AUTO_MIGRATE=1`. Only the local tunnel process is trusted for `X-Forwarded-Proto/For`, so the app sees
   `https` (Secure cookies and HSTS work) and the real client IP (rate limits work). Nobody can spoof those
   headers from the LAN, because the server is not reachable from the LAN at all.
5. Waits for `/readyz`, then writes the URL to `health-portal-clients\config\server.json` and the
   clients' bundled copies (`desktop-app\server-default.json`, `android-app\www\server-default.json`).

`cloudflared` was installed with `winget install --id Cloudflare.cloudflared -e`. It lives in
`C:\Program Files (x86)\cloudflared\` (open a new terminal for it to be on PATH).

## The quick-tunnel URL is temporary

**Quick tunnel URLs change every time the tunnel restarts** (reboot, sleep that drops the connection, running
the script again). They come with no uptime guarantee and are meant for testing. After each restart:

* tell users the new URL, or re-enter it in the apps (below), and
* rebuild the APK only if you want the new URL as the app's built-in default (not required).

The laptop must stay on and awake while people use the app.

## What to enter in the apps

* **Phone browser / PWA:** open `https://<tunnel>/`, then menu > *Install app*. Over HTTPS this is a real PWA
  install with the offline shell.
* **Android APK:** first-run screen (or press Back on the portal's first page) > Server URL
  `https://<tunnel>` > *Save & open portal*. The APK loads the site itself, so cookies are first-party.
* **Windows hospital app:** File > Server Settings (`Ctrl+,`) > `https://<tunnel>` > Save (the app restarts).
  `npm start` picks up `config\server.json` automatically.
* **Staff in a browser:** `https://<tunnel>/staff/`.

**Camera / QR scanning on phones now works.** Browsers and Android WebView only allow camera access
(`getUserMedia`) on secure origins. Over plain `http://<LAN-IP>` it was blocked; over the tunnel's HTTPS it is allowed.

## Staff accounts

* **Staff sign-up:** on `/staff/` choose *Create account*, enter work email, name, password (10+ characters)
  and the **staff invite code** from `STAFF_INVITE_CODE` in `.env`. Capitals, spaces and dashes don't matter.
  After 5 wrong codes from one IP address, sign-up is blocked for 15 minutes (HTTP 429).
  A 6-digit code is emailed. After entering it, the account exists as an **active front desk** user with
  **no organization**. Without the right invite code, sign-up is refused (HTTP 403).
* **Admin promotes / assigns:** sign in as admin > *Staff accounts* > **Change role** (front desk / nurse /
  physician / admin) and **Organization**. Every change is written to the audit log (`staff_updated`) and ends
  that person's sessions so it applies immediately.
* **Organizations:** a fresh database has none. The admin clicks **Add organization** (hospital/clinic) first.
  The first organization an admin creates is assigned to that admin automatically. Patients share records with an
  organization, and staff only see what was shared with their own organization (`provider_created` is audited).
* **Rotate the invite code:** `.venv\Scripts\python.exe scripts\set_env.py STAFF_INVITE_CODE=<new>` and restart
  the server (`start_public.ps1`). Accounts that already exist are not affected.

### Owner admin account (first login)

The admin `velagapudishivaadithya@gmail.com` was created by `scripts\create_admin.py` with a **random password
that was never stored or shown**. To set a password:

1. Open `https://<tunnel>/staff/` and click **Forgot your password?**
2. Enter `velagapudishivaadithya@gmail.com` > *Send code*. A 6-digit code is emailed (valid 10 minutes).
3. Enter the code and a new password (10+ characters). Then sign in with email + password + the emailed sign-in code.

API equivalent: `POST /api/staff/auth/forgot-password {"email"}`, then
`POST /api/staff/auth/reset-password {"email","code","new_password"}`.
To lock the account again (e.g. if the password leaked), re-run
`.venv\Scripts\python.exe scripts\create_admin.py velagapudishivaadithya@gmail.com "Owner (Admin)"`.

## Permanent URL (recommended before real use)

`start_public.ps1` already supports both options below. Once the token is in `.env`, run
`start_public.ps1` (or reboot; the watchdog does it) and it uses the permanent tunnel instead of a quick one
(`-Tunnel auto` picks Cloudflare first, then ngrok, then a quick tunnel). Tokens are passed to the tunnel
program through its environment, never printed and never put on its command line.
Put them in `.env` with Notepad (`notepad .env`, add the lines, save). Only your Windows account can read `.env`.

### Option A: named Cloudflare Tunnel on your own domain (about US$10/year for the domain; tunnel is free)

1. Create a free Cloudflare account. Buy a domain in **Cloudflare Registrar** (at cost, about $10/year for `.com`),
   or add a domain you already own and switch its nameservers to the two Cloudflare gives you.
2. Cloudflare dashboard > **Zero Trust** > **Networks > Tunnels** > **Create a tunnel** > type **Cloudflared** >
   name `healthportal` > Save.
3. On the "Install and run a connector" page choose Windows. The command shown contains a long token after
   `service install`. Copy **only the token**. Do not run that command, because the script runs cloudflared itself.
4. **Public Hostname** tab > Add: subdomain `hp`, domain `example.com` (yours), Service type **HTTP**, URL
   `127.0.0.1:8000` > Save.
5. Add to `.env`:
   ```
   CF_TUNNEL_TOKEN=<the token>
   PUBLIC_URL=https://hp.example.com
   ```
6. `powershell -ExecutionPolicy Bypass -File scripts\start_public.ps1`. It sets `ALLOWED_ORIGINS` and writes the
   URL to the clients' `config\server.json`. Rebuild the APK once so the permanent URL is its default (optional).
7. Optional, in the Cloudflare dashboard: a WAF rate-limit rule (for example 60 req/min/IP on `/api/*`). The app
   already limits per IP (`docs/OPERATIONS.md`). You can also put Cloudflare Access (free for up to 50 users) in front of `/staff/`.

### Option B: ngrok free static domain (free account, no domain purchase)

1. Sign up at https://dashboard.ngrok.com (free). **Getting Started > Your Authtoken**: copy it.
2. **Domains** (Universal Gateway > Domains): the free plan includes one static domain like
   `<name>.ngrok-free.app`. Create or claim it and copy the name.
3. Install the agent: `winget install --id Ngrok.Ngrok -e` (open a new terminal afterwards).
4. Add to `.env`:
   ```
   NGROK_AUTHTOKEN=<the authtoken>
   NGROK_DOMAIN=<name>.ngrok-free.app
   ```
5. `powershell -ExecutionPolicy Bypass -File scripts\start_public.ps1`.

Free-plan caveats: browsers see an ngrok warning page on the first visit (click **Visit Site**). There are also
monthly bandwidth and request caps (check https://ngrok.com/pricing). ngrok terminates TLS and can see the
traffic, just like Cloudflare.

### Option C: a real server

Deploy with Docker Compose + Caddy (automatic Let's Encrypt) or a managed platform as described in `DEPLOY.md`.
This removes the dependency on the laptop being on and is the path to a HIPAA-capable setup (BAA host, managed PostgreSQL).

## Risks to keep in mind

* Quick tunnels are temporary, unauthenticated test endpoints. Anyone with the URL can reach the sign-in pages.
* Cloudflare terminates TLS, so it can see the traffic. A BAA with Cloudflare (Enterprise) would be needed for real PHI.
* Email is sent through personal Gmail, which is **not HIPAA-eligible** (see `docs/GO_LIVE_CHECKLIST.md`).
* The database is SQLite on the laptop disk. Uploaded files are encrypted by the app (AES-256-GCM,
  `DATA_ENCRYPTION_KEY`). Everything else relies on BitLocker. Backups run every 6 hours (scheduled task).
  Set `BACKUP_OFFSITE_DIR` for an encrypted off-site copy. See `docs/OPERATIONS.md`.
* `/readyz` only says ok/unavailable to the public. Details need `X-Readyz-Token` (`READYZ_TOKEN`) or localhost.
