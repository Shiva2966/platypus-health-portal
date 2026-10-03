# Deploying ONE internet-facing instance

One server, one PostgreSQL database, one public `https://` URL used by the **phone PWA**, the **Android app**
and the **Windows hospital desktop app**.

> **HIPAA / compliance - read first.** This is a hackathon demo with **synthetic data only**. It is **not HIPAA
> compliant** and must not hold real patient information. See section 9 for what real PHI would require.

Files: `Dockerfile`, `docker-compose.yml`, `deploy/Caddyfile`, `.env.example`, `scripts/`, `docs/DATABASE.md`.
*Not verified in the build environment:* Docker is not installed on the dev machine, so the compose/Caddy files
were syntax-checked but never run there. Everything database-related was run against real PostgreSQL 16 and
SQLite (see `docs/DATABASE.md`). Do the smoke test in section 6 on first deploy.

## 1. Recommended path (concrete)

**A. One small VPS + Docker Compose + Caddy (recommended; ~US$5-12/month, you control everything).**
Any provider: Hetzner CX22, DigitalOcean 2 GB droplet, etc. Ubuntu 24.04, 2 GB RAM is plenty for a demo.

**B. Managed platform** (Render, Fly.io, Railway): deploy the same `Dockerfile` as a web service and attach the
platform's *managed PostgreSQL* (gives you backups/PITR, patching, TLS). Set the same env vars (section 3),
health check path `/healthz`. The app accepts the `postgres://` URL these platforms hand out. Check the plan's
current backup/retention terms - free database tiers are often time-limited or have no backups.

Choose B if you want the least ops work and a database someone else keeps alive; choose A for fixed cost and control.

## 2. Path A step by step

```bash
# 1) server: create it, point DNS  A-record  hp.example.com -> server IP
ssh root@SERVER
apt update && apt install -y docker.io docker-compose-v2 git ufw
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable      # 5432 and 8000 stay closed

# 2) code
git clone <your repo> /opt/healthportal && cd /opt/healthportal
cp .env.example .env && nano .env      # see section 3

# 3) start database + app + TLS proxy + nightly backups
docker compose --profile tls --profile backup up -d --build
docker compose ps                      # db healthy, app healthy
docker compose logs -f app             # "Application startup complete"; migrations ran (AUTO_MIGRATE=1)
```
Caddy obtains and renews the Let's Encrypt certificate for `PUBLIC_HOSTNAME` automatically (ports 80/443 must be reachable).
Update: `git pull && docker compose --profile tls --profile backup up -d --build` (migrations apply on start).

Local rehearsal with PostgreSQL (no TLS): `docker compose up -d --build` -> http://127.0.0.1:8000
(set `HP_SECURE_COOKIES=0` for plain http).

Without Docker, on Windows, for development against real PostgreSQL:
```powershell
.\.venv\Scripts\python -m pip install -r requirements-pgtest.txt
$env:DATABASE_URL = (.\.venv\Scripts\python scripts\pg_dev.py newdb)   # embedded PostgreSQL 16, data in .pgdata\
$env:AUTO_MIGRATE = "1"
.\.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 3. Environment variables / secrets

Secrets live **only** in `.env` on the server (git-ignored, `chmod 600 .env`) or the platform's secret store - never in git, logs, or the APK/EXE.

| Variable | Production value |
|---|---|
| `POSTGRES_PASSWORD` | long random (`python -c "import secrets;print(secrets.token_urlsafe(32))"`) |
| `DATABASE_URL` | set by compose; on a platform use its connection string (`postgres://...` is fine) |
| `AUTO_MIGRATE` | `1` |
| `HP_SECURE_COOKIES` | `1` |
| `TRUST_PROXY` | `1` behind Caddy/platform proxy (client IPs for throttling) |
| `PUBLIC_HOSTNAME` | `hp.example.com` |
| `SMTP_*`, `OTP_SECRET`, `STAFF_INVITE_CODE` | see `docs/EMAIL_SETUP.md`; `OTP_SECRET` long random |
| `DEV_SHOW_OTP` | **`0`** (never return OTP codes in API responses on a public server) |
| `SIGNUP_VERIFY_REQUIRED`, `LOGIN_OTP_REQUIRED` | **`1`** |

## 4. Cookies, CORS, and the three clients

Current server behaviour: session cookie `hp_patient` (and the staff cookie) is **HttpOnly**, **SameSite=Lax**,
**Secure** whenever the request is https or `HP_SECURE_COOKIES=1`; state-changing requests with a foreign
`Origin` are rejected (CSRF defence); there is **no CORS** middleware.

That works when every client loads the app **from the server's own origin**:
* **PWA** - installed from `https://hp.example.com`. Works.
* **Android app** - configure the WebView/Capacitor shell to load the remote URL (`server.url = https://hp.example.com`), not a bundled
  copy at `capacitor://localhost`/`https://localhost`; then cookies are first-party and nothing else is needed.
* **Windows desktop (Electron)** - `win.loadURL('https://hp.example.com/staff/')`. Same reasoning.

If a client instead bundles its own HTML and calls the API cross-origin, cookies with `SameSite=Lax` will **not**
be sent. That needs server work that is not implemented (requested from W1 in CONTRACT.md): an allow-list env
`CORS_ALLOWED_ORIGINS`, `Access-Control-Allow-Credentials: true`, session cookies with `SameSite=None; Secure`,
and the CSRF check consulting the same allow-list. Do not enable `*` origins with credentials.

## 5. Rate limiting, headers, uploads
* In the app: login/OTP throttling, per-account lockout and share-token attempt limits already exist (W1/W2/W6).
* At the edge (not enabled by default - Caddy core has no limiter): put Cloudflare (free plan rate-limit rule, e.g.
  60 req/min/IP on `/api/*`, 5/min on `/api/auth/*`) in front, **or** build Caddy with `caddy-ratelimit`
  (`xcaddy build --with github.com/mholt/caddy-ratelimit`) and add a `rate_limit { zone api { key {remote_host} events 120 window 1m } }` block, **or** use nginx `limit_req`.
* `deploy/Caddyfile` sets HSTS, nosniff, a 20 MB body cap (app limit is 15 MB), gzip/zstd, and hides `/readyz` from the public.
* Keep port 8000 reachable only from the proxy (compose binds it to 127.0.0.1).

## 6. First-deploy smoke test
```bash
curl -s https://hp.example.com/healthz                  # {"status":"ok"}
docker compose exec app python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/readyz').read())"
docker compose exec app python scripts/verify_blobs.py  # RESULT: CLEAN
docker compose exec app python seed.py                  # optional: synthetic demo data (check README)
# sign up on the PWA, upload a PDF, log in on the desktop app as staff, request/approve access, revoke.
```
Then do a **restore drill** (section 7) before you call it done.

## 7. Backups and disaster recovery
* `--profile backup` runs a nightly `pg_dump -Fc` into `./backups` (14 kept, integrity-listed). Prefer
  `docker compose exec app python scripts/backup.py --verify-restore` (adds manifest + hashes + full restore test).
  Cron example: `30 2 * * * cd /opt/healthportal && docker compose exec -T app python scripts/backup.py --keep 14 --verify-restore`.
* **Copy backups off the server** (`rclone sync ./backups remote:hp-backups`) - same-disk backups die with the disk.
* Restore: bring up a fresh database, then
  `docker compose exec app python scripts/restore.py --latest --target postgresql+psycopg://hp:PW@db:5432/healthportal_restored`
  (refuses a non-empty target), point `DATABASE_URL` at it, restart the app. `scripts/verify_blobs.py` must say CLEAN.
* RPO ~24 h (nightly) unless you add WAL archiving / managed PITR; RTO well under an hour with off-site backups. Details: `docs/DATABASE.md`.

## 8. Logging hygiene (no PHI in logs)
* The app image runs uvicorn with `--no-access-log` (URLs can contain search text such as patient names).
* PostgreSQL logs never include bind parameters (`log_parameter_max_length=0`); only slow-statement text.
* Caddy access log strips query strings, cookies and Authorization, and masks client IPs to /24.
* Application code must not log request bodies, names, emails, OTP codes or document names - review any new `log.*` call.
  `get_db` logs only a generic warning. Audit rows (`audit_log`) intentionally record who/what/when for patients to review;
  they are data, not log files - treat the database and its backups as PHI.
* Docker: `logging: {driver: json-file, options: {max-size: "10m", max-file: "5"}}` per service if the host default is unbounded.

## 9. Honest HIPAA statement
This build is **a demo with synthetic data and is NOT HIPAA-compliant**. Before real PHI it would at least need:
1. A signed **BAA** with the hosting provider, managed-database vendor, email/SMS provider (Gmail SMTP is **not** acceptable), backup storage, and any monitoring vendor.
2. **Encryption at rest** for database volumes, backups and file BLOBs (managed DB encryption or LUKS/KMS; consider application-level envelope encryption for documents) and key management; TLS 1.2+ everywhere including DB connections (`sslmode=verify-full`).
3. **MFA** for all staff (TOTP/WebAuthn, not only emailed codes), SSO for the hospital, session timeouts, break-glass procedure.
4. **Audit controls**: tamper-evident logs shipped to separate storage, regular access review, alerting on unusual access (the in-DB append-only log is a start, not sufficient: a DB owner can still drop triggers).
5. **Access management**: least-privilege DB roles (the app should not own the schema in production), role-based staff access, patient identity proofing, minimum-necessary disclosure, emergency access.
6. **Backup/DR** with tested off-site encrypted copies, documented retention and secure deletion, point-in-time recovery, redundancy/failover.
7. **Administrative safeguards**: risk analysis, policies, workforce training, incident/breach response and notification plan, vendor management, penetration testing, vulnerability patching, logging/monitoring (SIEM).
8. **Data rights**: export, amendment, accounting of disclosures, retention/deletion workflows (the demo only sketches these).
Also consider state laws and, for EU users, GDPR. This document is engineering guidance, not legal advice.
