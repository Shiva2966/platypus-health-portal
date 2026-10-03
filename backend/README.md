# Health Records Portal

A patient-controlled health-records portal (installable PWA at `/`) plus a hospital/staff portal (`/staff/`).
Patients keep their records, documents, bills and appointments; clinics only see what a patient shares, and every
view is logged and shown to the patient. English only.

Stack: Python 3 + FastAPI + SQLAlchemy (SQLite today, PostgreSQL-ready), plain HTML/CSS/vanilla JS, no build step.
Before storing real patient data, work through `docs/GO_LIVE_CHECKLIST.md` (legal, hosting, backups).

## How to test (owner)

### 1. Open the app
The server and the public HTTPS address are started by `scripts\start_public.ps1` (it runs the app on port 8000 and a
Cloudflare tunnel). It prints:

    PUBLIC URL (patients):  https://<random>.trycloudflare.com/
    STAFF PORTAL:           https://<random>.trycloudflare.com/staff/

The tunnel URL **changes every time the script restarts**. Stop everything with
`powershell -ExecutionPolicy Bypass -File scripts\start_public.ps1 -Stop`.
Health checks: `<URL>/readyz` must say ready (it is 503 if email or the database isn't OK).

### 2. Your admin account (staff side)
The ops setup created your staff admin account with `scripts\create_admin.py <your email>`. Its password is random and
unknown on purpose. To get in:
1. Open `<URL>/staff/` -> **Forgot your password?** -> enter your email.
2. Type the 6-digit code from the email and choose a new password.
3. Sign in (email + password + a new emailed code).
4. As admin: create your organization (e.g. "Riverside General Hospital") under staff management. Admins see the
   audit log and manage staff; they never see patient records.

### 3. Patient sign-up
1. Open `<URL>/` on a phone or computer -> **Create an account** (name, date of birth, email, password).
2. Enter the 6-digit code emailed to you (check spam the first time). Every sign-in also asks for a code
   (you can tick "don't ask on this device for 30 days").
3. Fill in the profile and an emergency contact, upload a PDF and a photo (name required, description optional),
   add an allergy and a medication, book an appointment with the intake form.

### 4. Staff sign-up with the invite code
1. A colleague opens `<URL>/staff/` -> **Request a staff account** and enters the **staff invite code**
   (`STAFF_INVITE_CODE`, set by ops in `.env`; ask whoever ran the setup). Without it, staff sign-up is refused.
2. New staff start as *front desk* with no organization. You (admin) open staff management, set their role
   (front desk / nurse / physician) and organization.

### 5. The consent flow to try first
1. Staff (nurse) searches the patient by name (any word order, middle names OK), confirms the date of birth ->
   sees **No shared records** -> **Request access** (pick categories, purpose).
2. Patient gets a notification -> **Sharing & consent** -> review -> approve.
3. Staff opens the PDF and the Allergies tab. Patient sees "viewed" entries in Access history and a notification
   (re-opening the same tab within 30 minutes is still logged but doesn't notify again).
4. Patient revokes -> staff immediately loses access (403 / "Not shared with you").
5. Also: Bills -> **Explain this bill**; Privacy -> **Export my data (ZIP)**.

### 6. Android APK / Windows desktop app
The native clients live in `C:\Users\sav11\Projects\health-portal-clients`. `start_public.ps1` writes the current
tunnel URL into `health-portal-clients\config\server.json` as their default. Because the URL changes on restart, each
app has a **Settings / Server address** screen: paste the current `https://...trycloudflare.com` URL there.
`ALLOWED_ORIGINS` (set by the script) already allows the tunnel URL and the app origins.

### Reporting problems
Note the time, the page, what you clicked and what you expected. Server logs are in the folder printed by
`start_public.ps1`; the audit log (staff admin) shows every access.

## Developer notes

```powershell
cd C:\Users\sav11\Projects\health-portal
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q          # in-process; never sends email (tests/conftest.py blanks SMTP_*)
```

- `APP_ENV=production` is the default: codes are only emailed (SMTP required, otherwise 503), no API docs, Secure
  cookies, auth rate limits. `APP_ENV=development` prints codes in the server console when SMTP isn't configured.
- Email: SMTP settings come only from `.env` (`docs/EMAIL_SETUP.md`). Mail is never sent to reserved test domains
  (`example.com/.net/.org`, `*.test`, `*.invalid`, `*.example`, `*.localhost`). `MAIL_DISABLED=1` turns real sending
  off for any process that inherits `.env` (in production that makes code-sending endpoints fail closed with 503).
- `python seed.py` only creates tables. Schema changes go through Alembic (`scripts\migrate.ps1`, `docs/DATABASE.md`).
- Deployment, Docker, PostgreSQL, CORS for bundled clients: `DEPLOY.md`. Module ownership and interfaces: `CONTRACT.md`.
  Known issues and QA history: `BUGS.md`.

## What's in it

| Area | Where |
|---|---|
| Patient profile, emergency contacts | `app/routers/patient_profile.py`, `app/static/js/core_profile.js` |
| Dashboard (appointments, intake, results, bills, sharing, requests, notifications, profile gaps) | `app/static/js/core_dashboard.js` (each widget degrades on its own) |
| Email + password + emailed code (OTP) sign-in | `app/routers/auth_otp.py`, `app/services/mailer.py` |
| Documents (stored in the database), sharing / consent / QR / access requests | `app/routers/documents.py`, `sharing.py`, `app/services/consent.py` |
| Staff portal, roles, appointment inbox, notifications, audit | `app/routers/staff_*.py`, `notifications.py`, `app/static/staff/` |
| Medical records, results + trends | `app/routers/med_*.py` |
| Insurance, bills, EOB explainer, cost search | `app/routers/billing_*.py` |
| Appointments + intake, caregivers, privacy, accessibility | `app/routers/appt_*.py` |
| Database tooling / deployment | `docs/DATABASE.md`, `DEPLOY.md` |

## Security notes (honest)

- argon2id passwords, emailed one-time codes for every sign-in, opaque server-side session cookies
  (HttpOnly, SameSite, Secure on HTTPS), auth rate limits.
- Patient ids are random UUIDs; share tokens are random, opaque, stored hashed. Every patient route filters by the
  signed-in patient; staff see only what a grant allows, and every staff view is audited (append-only audit table).
- CSP (no inline scripts), nosniff, no-store on API responses, cross-origin state-changing requests rejected.
- Not done: TOTP/WebAuthn, encryption at rest, HIPAA compliance, penetration test. See `docs/GO_LIVE_CHECKLIST.md`.
