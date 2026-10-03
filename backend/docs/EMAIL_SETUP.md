# Email sign-in codes (OTP) - setup

The app emails a 6-digit code (a) to confirm a new email address at sign-up, (b) as a second step at every
sign-in, and (c) to reset a forgotten password. This page shows how to turn on **real email** using a Gmail
account. Until you do, the app runs in **dev mode**: nothing is sent, the code is printed in the server console
(and shown on screen only if `DEV_SHOW_OTP=1`), so demos and tests still work.

> **Security rules:** the Gmail password lives **only** in your local `.env` file (which is git-ignored). Never put it in
> code, README, chat, screenshots, tests, or commits. The app never logs it and never logs or stores OTP codes in
> plain text.

## Turn on real email (3 steps)

1. **Create a Gmail App Password** for the sending account (`velagapudishivaadithya@gmail.com`).
   - The Google account must have **2-Step Verification** turned on (Google Account > Security).
   - Open <https://myaccount.google.com/apppasswords>, name it e.g. "Health Portal", click **Create**.
   - Google shows a 16-character password once. Copy it (spaces don't matter). This is *not* your normal Gmail password,
     and you can revoke it at any time on the same page.
2. **Put it in `.env`** (copy `.env.example` to `.env` first; `.env` is already in `.gitignore`):

   ```
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=velagapudishivaadithya@gmail.com
   SMTP_FROM=velagapudishivaadithya@gmail.com
   SMTP_PASSWORD=<paste the 16-character app password here>
   DEV_SHOW_OTP=0
   ```
3. **Restart the server** (`python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`) and sign up with an email
   you can read. `GET /api/auth/config` now reports `"email_delivery": "smtp"`.

If sending fails, the server log says why (without secrets): *"rejected the login"* = wrong/expired App Password or 2-Step
Verification is off; *"couldn't reach the email server"* = firewall / no internet / wrong host or port.

## Limits and caveats of Gmail SMTP

- **About 500 emails per day** for a normal Gmail account (about 2,000 for Google Workspace). Each sign-up or sign-in
  uses one email, so this is fine for a hackathon demo but not for real traffic.
- Messages from a personal Gmail address sent via SMTP **may land in spam** the first time. Ask testers to check the spam
  folder and mark "Not spam". There is no custom-domain authentication (SPF/DKIM/DMARC) you control.
- The `From` address is always your Gmail account; Gmail rewrites other values.
- **For production, use a transactional email provider** (Resend, SendGrid, Postmark, or AWS SES) with your own domain
  and SPF/DKIM set up. They all offer SMTP: just change `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASSWORD`
  and `SMTP_FROM` - no code changes. (Port 465 is also supported and uses implicit TLS.)

## Dev mode (no email configured)

- The code is printed in the server console: `[DEV MODE - email not configured] login code for you@example.com: 123456`.
- With `DEV_SHOW_OTP=1` the API also returns `dev_otp` and the sign-in screens show a "Demo mode" box with the code,
  so a demo works on a phone with no email at all.
- As soon as `SMTP_USER` and `SMTP_PASSWORD` are set, console printing stops and `dev_otp` is **never** returned,
  even if `DEV_SHOW_OTP=1` is left on by mistake.
- Existing demo/seed accounts are already verified; they only need the sign-in code (shown inline in dev mode).

## Behaviour and settings

| Setting (env) | Default | Meaning |
| --- | --- | --- |
| `SIGNUP_VERIFY_REQUIRED` | `1` | New accounts exist only after the emailed code is confirmed. |
| `LOGIN_OTP_REQUIRED` | `1` | Emailed code after the correct password. "Trust this device" skips it for 30 days. |
| `OTP_TTL_MINUTES` | `10` | Code lifetime. Codes are single-use and 5 wrong tries lock that code. |
| `OTP_RESEND_COOLDOWN_SECONDS` | `45` | Minimum gap between codes for the same email. |
| `OTP_EMAIL_HOURLY_CAP` / `OTP_IP_HOURLY_CAP` | `6` / `30` | Codes per email / per IP per hour. |
| `LOGIN_MAX_FAILURES`, `LOGIN_LOCKOUT_MINUTES` | `5`, `15` | Bad-password lockout, doubling on repeated failures. |
| `TRUST_DEVICE_DAYS` | `30` | Length of "remember this device". |
| `OTP_SECRET` | auto | HMAC key for stored code hashes; set a long random value in production (`data/.otp_secret` is auto-created locally). |
| `STAFF_INVITE_CODE` | empty | Staff self sign-up requires it. If empty and email is live, staff sign-up is closed (accounts are admin-made). |
| `TRUST_PROXY` | `0` | Set `1` behind a reverse proxy so per-IP limits use `X-Forwarded-For`. |

Setting `SIGNUP_VERIFY_REQUIRED=0` / `LOGIN_OTP_REQUIRED=0` restores plain password sign-in. Use that only for local
testing; the test suite does it for older tests automatically.

## Privacy notes

- Emails contain only the app name, the code, an expiry and "ignore this if it wasn't you". No names, health information,
  appointments or documents are ever put in an email.
- Sign-in and password-reset responses are intentionally generic ("If that email can be used, we've sent a code") so
  nobody can discover which emails have accounts.
- After a password reset or change, other sessions and "trusted devices" are signed out.

## API quick reference

Patients use `/api/auth/*`, staff use `/api/staff/auth/*` (same endpoints):
`config`, `signup`, `verify-email`, `resend-verification`, `login`, `verify-login-otp`, `resend-login-otp`,
`forgot-password`, `reset-password`, `change-password`, `email-change/request`, `email-change/confirm`, `logout`,
`sessions`, `sessions/revoke-all`, `sessions/{id}`, `trusted-devices`, `trusted-devices/{id}`.
