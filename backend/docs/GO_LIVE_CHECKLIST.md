# Go-live checklist (before ANY real patient data)

Honest status as of 2026-10-02. The app now runs in a hardened `APP_ENV=production` mode, but **software
hardening is not compliance**. Do not store real protected health information (PHI) until every item in
sections 1 to 3 is done and signed off by whoever is legally responsible for the data.

## 1. Legal / contractual (blocking)

- [ ] **HIPAA Business Associate Agreement (BAA) with the hosting provider** (compute, database, backups,
      logs). Pick a host that signs BAAs (for example AWS, GCP, Azure, or Aptible). Without one, hosting PHI there is a HIPAA violation.
- [ ] **Email provider with a BAA, or no PHI in email at all.** The current sender is a personal Gmail
      account over SMTP. **Consumer Gmail is not HIPAA-eligible and Google will not sign a BAA for it.**
      Today the emails contain only the app name, a 6-digit code and generic text (no names, no health data),
      and that must stay true. For production, move to a provider that signs a BAA (Google Workspace with a BAA,
      AWS SES, Paubox, etc.) on your own domain with SPF, DKIM and DMARC set up.
- [ ] Privacy policy and terms of use, reviewed by counsel, linked from the sign-up screen.
- [ ] Notice of Privacy Practices / patient consent text (if acting as or for a covered entity).
- [ ] Decide who the covered entity is and who is the business associate. Document it.
- [ ] Breach notification process: who decides, 60-day HHS and individual notification timeline, contact list,
      template letters. Run a tabletop exercise.

## 2. Security controls (blocking)

- [ ] **HTTPS only** behind a TLS proxy (Caddy profile in `docker-compose.yml`). Set `TRUST_PROXY=1` there.
      HSTS is sent automatically on HTTPS requests. Keep `COOKIE_SECURE` empty (= on).
- [ ] **Encryption at rest**: encrypted disk/volume for PostgreSQL *and* for backups. Document documents/BLOB
      storage encryption (they live in the database). Consider column-level encryption for the most sensitive fields.
      *Partly done 2026-10-03 (HARD):* uploaded files are AES-256-GCM encrypted in the app (`DATA_ENCRYPTION_KEY`),
      off-site backup copies are encrypted, and BitLocker reports On for C:. The key needs an offline copy, and other
      columns rely on disk encryption (`docs/OPERATIONS.md`).
- [ ] Move from SQLite to managed PostgreSQL (`DATABASE_URL`), `AUTO_MIGRATE=1`, least-privilege DB role.
- [ ] **MFA**: email OTP is on for every sign-in (`LOGIN_OTP_REQUIRED=1`). Email OTP is weak against mailbox
      compromise. Add TOTP or WebAuthn at least for staff and admins before go-live.
- [ ] `SECRET_KEY`: auto-generated into `.env` if missing. Move it (and `SMTP_PASSWORD`) into a secrets
      manager; rotate the Gmail app password if it was ever pasted anywhere else.
- [x] Set `STAFF_INVITE_CODE` (or keep staff self sign-up disabled and create staff as admin). Done 2026-10-03 (OPS).
- [x] **Wipe the demo data.** Done 2026-10-03 (OPS): old DB backed up and moved to `data/app.db.retired-*`, fresh
      empty DB via `alembic upgrade head`, owner admin created by `scripts/create_admin.py` (see `docs/PUBLIC_ACCESS.md`). The current `data/app.db` was seeded with demo patients and staff whose
      passwords are published in the README (`Staff-Demo-2026!`, `Demo-Patient-2026!`). Start production on
      an empty database. `seed.py` now refuses demo data in production unless `--force` is given.
- [ ] Independent **penetration test** and fix findings. Include auth/OTP, consent/sharing, file upload,
      staff authorization, IDOR checks.
- [ ] Dependency and container vulnerability scanning in CI (pip-audit, image scan). Manual pip-audit 2026-10-03:
      only `pip` itself was flagged (upgraded); no CI yet.
- [ ] Rate limits are in-process (per worker). With several workers or instances, add limits at the proxy/WAF too.
      (Per-IP limits on all `/api` routes, stricter on auth/OTP/upload/invite codes, real client IP behind the tunnel: done.)

## 3. Operations (blocking)

- [ ] **Backups**: automated, encrypted, off-site, retention policy, and a **tested restore**
      (`scripts/backup.py` / `restore.py`). Record restore time. *2026-10-03:* automated every 6 h + at logon with
      restore verification and 7-day retention. The encrypted off-site copy is ready but waits for the owner to set `BACKUP_OFFSITE_DIR`.
- [ ] **Audit log review**: audit rows are append-only in the DB. Assign someone to review staff access
      weekly; alert on unusual volume (`document_viewed`, `record_viewed`, `*_access_denied`).
- [ ] Centralized logging with retention and access control. Logs must not contain PHI: query strings are
      stripped from access logs and unhandled errors are logged without messages in production. Verify this
      on the real log pipeline.
- [ ] Monitoring: `/healthz` (liveness) and `/readyz` (DB, migrations, audit triggers, **mailer**). `/readyz`
      returns 503 in production when email isn't configured, so sign-in can't silently break.
- [ ] Incident response runbook + on-call contact.
- [ ] Data retention and deletion policy (patient deletion requests exist in the app; define the back-office process).
- [ ] Workforce HIPAA training for anyone with staff/admin access; access reviews when people leave.

## 4. Already done in code (production mode)

- OTP codes are only emailed to the address the user typed. They never appear in API responses, the UI or
  logs, and `DEV_SHOW_OTP` is ignored. Without SMTP, every code-sending endpoint fails closed with
  503 "Email sending isn't configured".
- argon2id password hashing (legacy scrypt still verifies), lockout/backoff, OTP attempt limits, auth rate limit.
- Changing the sign-in email requires the current password plus a code sent to the new address. The old address gets a notice.
- CSP, X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy, COOP, and HSTS on HTTPS. HttpOnly and
  SameSite cookies, Secure by default. Request size limits. CORS only for `ALLOWED_ORIGINS`.
  OpenAPI docs are off. Error pages are generic.
- Demo banner and demo staff logins are hidden unless `SHOW_DEMO_BANNER=1`.

## 5. Known gaps (not blocking for a pilot with synthetic data, but plan them)

- No TOTP/WebAuthn yet; no session device binding. Idle sign-out (patient 30 min / staff 20 min) is in place.
- In-memory rate limiter resets on restart and isn't shared across workers.
- Clinical content (results, medications) is patient-entered or mock; no EHR/FHIR integration, no real insurer verification.
- Accessibility and usability not formally audited.
