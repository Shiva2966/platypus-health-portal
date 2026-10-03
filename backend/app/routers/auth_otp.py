"""Email + password sign-up / sign-in with emailed OTP (W6).

Patients:  /api/auth/*          Staff:  /api/staff/auth/*     (same flows, separate accounts, cookies, sessions)

  POST signup | register          email, password, name[, dob]  -> emails a 6-digit code; NO account yet
  POST verify-email               email, code[, trust_device]   -> creates the account + signs in
  POST resend-verification        email
  POST login                      email, password -> {otp_required, otp_challenge_id}  (NOT a session)
                                  (a valid "trusted device" cookie, or LOGIN_OTP_REQUIRED=0, signs in directly)
  POST verify-login-otp           otp_challenge_id, code[, trust_device] -> session
  POST resend-login-otp           otp_challenge_id
  POST forgot-password            email     (always the same generic answer)
  POST reset-password             email, code, new_password
  POST change-password            current_password, new_password            (signed in)
  POST email-change/request|confirm                                          (signed in)
  POST logout
  GET  sessions | POST sessions/revoke-all | DELETE sessions/{id}
  GET  trusted-devices | DELETE trusted-devices/{id}
  GET  config

Because this module sorts before patient_*/staff_* in app/routers, its /login and /register take
precedence over the older password-only routes with the same path.
"""
import json
import logging
import os
import random
import re
import secrets
from datetime import date, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import settings
from app.db import get_db
from app.models.auth import PendingSignup, TrustedDevice, as_utc, utcnow_tz
from app.models.shared import Patient, Provider
from app.security import current_patient, hash_token
from app.services import auth_passwords as pw
from app.services import mailer, otp
from app.services.audit import log_event

log = logging.getLogger("health-portal.auth")
router = APIRouter()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
BAD_LOGIN = "Incorrect email or password."
GENERIC_SENT = "If that email can be used, we've sent a 6-digit code to it. It expires in {m} minutes."


# ======================================================================================
# account kinds (patient / staff) - everything account-specific lives here
# ======================================================================================

class Kind:
    type: str
    prefix: str
    session_cookie: str
    trust_cookie: str
    samesite: str

    def find(self, db: Session, email: str):  # pragma: no cover
        raise NotImplementedError


class PatientKind(Kind):
    type, prefix, session_cookie, trust_cookie, samesite = "patient", "/api/auth", "hp_patient", "hp_trust_patient", "lax"

    def find(self, db, email):
        return db.scalar(select(Patient).where(func.lower(Patient.email) == email))

    def get(self, db, acct_id):
        return db.get(Patient, acct_id)

    def active(self, acct) -> bool:
        return True

    def display(self, acct) -> str:
        return acct.display_name

    def create(self, db, p: PendingSignup):
        acct = Patient(email=p.email, password_hash=p.password_hash, legal_name=p.name, dob=p.dob)
        db.add(acct)
        db.flush()
        return acct

    def start_session(self, db, acct, request, response):
        from app.security import create_patient_session

        create_patient_session(db, acct, response, request)  # commits

    def end_session(self, db, request, response):
        from app.security import destroy_patient_session

        destroy_patient_session(db, request, response)

    @property
    def session_model(self):
        from app.models.core import PatientSession

        return PatientSession

    @property
    def owner_col(self):
        return self.session_model.patient_id

    def audit(self, db, acct, action, detail=None):
        log_event(db, actor_type="patient", actor_id=acct.id, patient_id=acct.id, action=action,
                  resource_type="account", detail=detail)

    def current(self):
        return current_patient

    def public(self, acct) -> dict:
        return {"id": acct.id, "display_name": acct.display_name, "email": acct.email, "role": "patient"}


class StaffKind(Kind):
    type, prefix, session_cookie, trust_cookie, samesite = "staff", "/api/staff/auth", "hp_staff", "hp_trust_staff", "strict"

    def find(self, db, email):
        from app.models.staff import StaffUser

        return db.scalar(select(StaffUser).where(func.lower(StaffUser.email) == email))

    def get(self, db, acct_id):
        from app.models.staff import StaffUser

        return db.get(StaffUser, acct_id)

    def active(self, acct) -> bool:
        return bool(acct.active)

    def display(self, acct) -> str:
        return acct.name

    def create(self, db, p: PendingSignup):
        from app.models.staff import StaffUser

        extra = json.loads(p.extra_json or "{}")
        acct = StaffUser(email=p.email, password_hash=p.password_hash, name=p.name, role="front_desk",
                         provider_id=extra.get("provider_id") or None, active=True)
        db.add(acct)
        db.flush()
        return acct

    def start_session(self, db, acct, request, response):
        from app.services.staff_security import create_staff_session

        create_staff_session(db, acct, request, response)  # commits

    def end_session(self, db, request, response):
        from app.services.staff_security import destroy_staff_session

        destroy_staff_session(db, request, response)

    @property
    def session_model(self):
        from app.models.staff import StaffSession

        return StaffSession

    @property
    def owner_col(self):
        return self.session_model.staff_id

    def audit(self, db, acct, action, detail=None):
        log_event(db, actor_type="staff", actor_id=acct.id, patient_id=None, action=action,
                  resource_type="account", detail=detail)

    def current(self):
        from app.services.staff_security import current_staff

        return current_staff

    def public(self, acct) -> dict:
        base = {"id": acct.id, "display_name": acct.name, "name": acct.name, "email": acct.email, "role": acct.role}
        try:  # same payload W3's staff UI already expects from /api/staff/me (provider name, capabilities)
            from sqlalchemy.orm import object_session

            from app.routers.staff_auth import staff_payload

            return {**base, **staff_payload(object_session(acct), acct)}
        except Exception:
            return base


PATIENT, STAFF = PatientKind(), StaffKind()


# ======================================================================================
# helpers
# ======================================================================================

def client_ip(request: Request) -> str | None:
    # app.hardening.ClientIPMiddleware has already replaced request.client with the real client IP
    # (CF-Connecting-IP / rightmost X-Forwarded-For, only from a trusted proxy).
    return request.client.host[:64] if request.client else None


INVITE_WINDOW_SECONDS = 15 * 60


def _check_invite(request: Request, code: str, expected: str) -> bool:
    """Constant-time invite comparison with a per-IP limit on wrong guesses (429 + Retry-After)."""
    from app.hardening import RATE_LIMITER

    limit = settings.invite_failures_per_window()
    key = f"invite:{client_ip(request) or '?'}"
    if limit > 0:
        wait = RATE_LIMITER.blocked_for(key, limit, INVITE_WINDOW_SECONDS)
        if wait:
            raise HTTPException(429, "Too many wrong invite codes. Please wait and try again later.",
                                headers={"Retry-After": str(wait)})
    if secrets.compare_digest(_norm_invite(code).encode(), expected.encode()):
        return True
    if limit > 0:
        RATE_LIMITER.allow(key, limit, INVITE_WINDOW_SECONDS)
    return False


def _ua(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:300]


def _device_label(request: Request) -> str:
    ua = _ua(request)
    browser = next((n for k, n in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
                                    ("Safari/", "Safari")) if k in ua), "Browser")
    os_ = next((n for k, n in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("Windows", "Windows"),
                               ("Mac OS", "Mac"), ("Linux", "Linux")) if k in ua), "device")
    return f"{browser} on {os_}"


def _http(err: otp.OtpError) -> HTTPException:
    headers = {"Retry-After": str(err.retry_after)} if err.retry_after else None
    return HTTPException(err.status, err.message, headers=headers)


def _fields(fields: dict[str, str], detail: str = "Please check the highlighted fields.") -> JSONResponse:
    return JSONResponse({"detail": detail, "fields": fields}, status_code=422)


def _dev_fields(code: str) -> dict:
    """The code is returned to the browser ONLY in development with no SMTP configured AND DEV_SHOW_OTP=1."""
    if settings.dev_codes_visible():
        return {"dev_otp": code}
    return {}


def _norm_invite(code: str | None) -> str:
    """Invite codes are typed on phones: ignore case, spaces and dashes."""
    return re.sub(r"[\s\-]+", "", code or "").upper()


def _require_mail() -> None:
    """Production fails closed: no SMTP -> no codes are created at all."""
    if settings.mail_required_but_missing():
        raise HTTPException(503, settings.MAIL_NOT_CONFIGURED)


def _meta() -> dict:
    return {"expires_in": otp.ttl_minutes() * 60, "resend_after": otp.resend_cooldown()}


def _send_bg(email: str, code: str, purpose: str) -> None:
    try:
        mailer.send_otp_email(email, code, purpose, otp.ttl_minutes())
    except mailer.MailError as e:
        log.error("OTP email failed purpose=%s to=%s: %s", purpose, mailer.mask_email(email), e)
    except Exception:  # never let a background task crash noisily with sensitive locals
        log.error("OTP email failed purpose=%s to=%s (unexpected error)", purpose, mailer.mask_email(email))


def _notice_bg(email: str, subject: str, headline: str, lines: list[str]) -> None:
    try:
        mailer.send_notice_email(email, subject, headline, lines)
    except Exception as e:
        log.error("Notice email failed to=%s (%s)", mailer.mask_email(email), type(e).__name__)


def _set_trust_cookie(kind: Kind, request: Request, response: Response, token: str) -> None:
    secure = request.url.scheme == "https" or os.environ.get("HP_SECURE_COOKIES") == "1"
    response.set_cookie(kind.trust_cookie, token, max_age=otp.trust_days() * 86400, httponly=True,
                        samesite=kind.samesite, secure=secure, path=kind.prefix)


def _check_email(email: str) -> str | None:
    if not email:
        return "Enter your email address."
    if len(email) > 254 or not EMAIL_RE.match(email):
        return "That doesn't look like an email address."
    return None


def _need_unlocked(db: Session, kind: Kind, email: str, ip: str | None) -> None:
    wait = otp.lockout_seconds(db, account_type=kind.type, email=email, ip=ip)
    if wait > 0:
        mins = max(1, (wait + 59) // 60)
        raise HTTPException(429, f"Too many sign-in attempts. Please try again in about {mins} minute{'s' if mins != 1 else ''}.",
                            headers={"Retry-After": str(wait)})


def _revoke_sessions(db: Session, kind: Kind, acct, keep_hash: str | None = None) -> int:
    stmt = delete(kind.session_model).where(kind.owner_col == acct.id)
    if keep_hash:
        stmt = stmt.where(kind.session_model.token_hash != keep_hash)
    return db.execute(stmt).rowcount or 0


def _current_hash(kind: Kind, request: Request) -> str | None:
    tok = request.cookies.get(kind.session_cookie)
    return hash_token(tok) if tok else None


def _finish_login(kind: Kind, db: Session, acct, request: Request, response: Response, *,
                  trust: bool = False, label: str | None = None, via: str = "password") -> dict:
    """Create the session (commits everything pending: consumed OTP, audit row, trusted device)."""
    if trust:
        token = otp.create_trusted_device(db, account_type=kind.type, account_id=acct.id,
                                          label=label or _device_label(request))
        _set_trust_cookie(kind, request, response, token)
    kind.audit(db, acct, "login", json.dumps({"method": via}))
    kind.start_session(db, acct, request, response)
    return {"ok": True, "otp_required": False, **kind.public(acct)}


# ======================================================================================
# request bodies
# ======================================================================================

class SignupIn(BaseModel):
    email: str = ""
    password: str = ""
    name: str = ""
    legal_name: str = ""
    dob: str = ""
    provider_id: str = ""
    invite_code: str = ""


class VerifyIn(BaseModel):
    email: str = ""
    code: str = ""
    trust_device: bool = False
    device_label: str = ""


class EmailIn(BaseModel):
    email: str = ""


class LoginIn(BaseModel):
    email: str = ""
    password: str = ""


class LoginOtpIn(BaseModel):
    otp_challenge_id: str = ""
    code: str = ""
    trust_device: bool = False
    device_label: str = ""


class ChallengeIn(BaseModel):
    otp_challenge_id: str = ""


class ResetIn(BaseModel):
    email: str = ""
    code: str = ""
    new_password: str = ""


class ChangePwIn(BaseModel):
    current_password: str = ""
    new_password: str = ""


class RevokeIn(BaseModel):
    include_current: bool = False


class EmailChangeIn(BaseModel):
    new_email: str = ""
    password: str = ""
    code: str = ""


# ======================================================================================
# route factory
# ======================================================================================

def build(kind: Kind) -> APIRouter:
    r = APIRouter(prefix=kind.prefix, tags=[f"{kind.type} email auth"])
    current = kind.current()

    # ---------------- config ----------------
    @r.get("/config")
    def config():
        smtp = mailer.smtp_configured()
        return {"app_env": settings.app_env(), "login_otp_required": otp.login_otp_required(),
                "signup_verify_required": otp.signup_verify_required(),
                "email_delivery": "smtp" if smtp else ("not_configured" if settings.is_production() else "console"),
                "email_ready": smtp or not settings.is_production(),
                "dev_otp_inline": settings.dev_codes_visible(),
                "otp_length": 6, "trust_device_days": otp.trust_days(), "min_password_length": pw.MIN_LENGTH,
                **_meta()}

    # ---------------- sign-up ----------------
    @r.post("/signup")
    @r.post("/register", include_in_schema=False)
    def signup(body: SignupIn, request: Request, response: Response, bg: BackgroundTasks, db: Session = Depends(get_db)):
        email = otp.normalize_email(body.email)
        name = (body.name or body.legal_name).strip()
        ip = client_ip(request)
        errs: dict[str, str] = {}
        if (m := _check_email(email)):
            errs["email"] = m
        if not name or len(name) > 200:
            errs["name"] = "Enter your full name." if not name else "That name is too long."
        if (m := pw.validate_password(body.password, email=email, name=name)):
            errs["password"] = m
        dob = (body.dob or "").strip() or None
        if dob:
            try:
                if date.fromisoformat(dob) > date.today():
                    errs["dob"] = "Date of birth can't be in the future."
            except ValueError:
                errs["dob"] = "Enter the date as YYYY-MM-DD."
        extra: dict = {}
        if kind.type == "staff":
            expected = _norm_invite(os.environ.get("STAFF_INVITE_CODE", ""))
            allowed = (expected and _check_invite(request, body.invite_code, expected)) or \
                      (not expected and not settings.is_production()
                       and (not mailer.smtp_configured() or os.environ.get("STAFF_SELF_SIGNUP") == "1"))
            if not allowed:
                raise HTTPException(403, "Staff accounts are created by your hospital administrator. "
                                         "Ask them for an invite code.")
            if body.provider_id:
                if not db.get(Provider, body.provider_id):
                    errs["provider_id"] = "Choose your organization from the list."
                extra["provider_id"] = body.provider_id
        if errs:
            return _fields(errs)

        phash = pw.hash_password(body.password)  # always hashed -> timing doesn't reveal existing accounts
        existing = kind.find(db, email)

        if not otp.signup_verify_required():  # relaxed mode (tests / local dev only): no email step
            if existing:
                return _fields({"email": "An account with this email already exists."}, "Could not create the account.")
            pend = PendingSignup(account_type=kind.type, email=email, name=name, dob=dob, password_hash=phash,
                                 extra_json=json.dumps(extra) if extra else None,
                                 expires_at=utcnow_tz() + timedelta(minutes=30))
            acct = kind.create(db, pend)
            kind.audit(db, acct, "account_created", json.dumps({"verified": False}))
            result = _finish_login(kind, db, acct, request, response, via="signup")
            response.status_code = 201
            return result

        _require_mail()
        if random.random() < 0.05:
            otp.purge_expired(db)
        try:
            issued = otp.issue_otp(db, account_type=kind.type, email=email, purpose="signup", account_id=None,
                                   ip=ip, user_agent=_ua(request))
        except otp.OtpError as e:
            raise _http(e)
        if existing is None:
            pend = db.scalar(select(PendingSignup).where(PendingSignup.account_type == kind.type, PendingSignup.email == email))
            if pend is None:
                pend = PendingSignup(account_type=kind.type, email=email, name=name, dob=dob, password_hash=phash,
                                     extra_json=None, expires_at=utcnow_tz())
                db.add(pend)
            pend.name, pend.dob, pend.password_hash = name, dob, phash
            pend.extra_json = json.dumps(extra) if extra else None
            pend.created_at = utcnow_tz()
            pend.expires_at = utcnow_tz() + timedelta(hours=24)
            db.commit()
            bg.add_task(_send_bg, email, issued.code, "signup")
        else:  # already registered: don't reveal it; tell the real owner instead
            db.commit()
            bg.add_task(_notice_bg, email, f"Someone tried to create a {mailer.app_name()} account with your email",
                        "Your email is already registered",
                        ["Someone just tried to sign up using this email address, but you already have an account.",
                         "If it was you, simply sign in - or use 'Forgot password' on the sign-in page to choose a new password."])
        return {"ok": True, "verification_required": True, "message": GENERIC_SENT.format(m=otp.ttl_minutes()),
                **_meta(), **_dev_fields(issued.code)}

    @r.post("/resend-verification")
    def resend_verification(body: EmailIn, request: Request, bg: BackgroundTasks, db: Session = Depends(get_db)):
        email = otp.normalize_email(body.email)
        if (m := _check_email(email)):
            return _fields({"email": m})
        _require_mail()
        pend = db.scalar(select(PendingSignup).where(PendingSignup.account_type == kind.type, PendingSignup.email == email))
        real = pend is not None and as_utc(pend.expires_at) > utcnow_tz() and kind.find(db, email) is None
        try:
            issued = otp.issue_otp(db, account_type=kind.type, email=email, purpose="signup", account_id=None,
                                   ip=client_ip(request), user_agent=_ua(request))
        except otp.OtpError as e:
            raise _http(e)
        db.commit()
        if real:
            bg.add_task(_send_bg, email, issued.code, "signup")
        return {"ok": True, "message": GENERIC_SENT.format(m=otp.ttl_minutes()), **_meta(), **_dev_fields(issued.code)}

    @r.post("/verify-email")
    def verify_email(body: VerifyIn, request: Request, response: Response, db: Session = Depends(get_db)):
        email = otp.normalize_email(body.email)
        row = otp.find_latest(db, account_type=kind.type, email=email, purpose="signup")
        try:
            otp.check_code(db, row, body.code)
        except otp.OtpError as e:
            raise _http(e)
        pend = db.scalar(select(PendingSignup).where(PendingSignup.account_type == kind.type, PendingSignup.email == email))
        if pend is None or as_utc(pend.expires_at) <= utcnow_tz() or kind.find(db, email) is not None:
            db.rollback()
            raise HTTPException(400, otp.GENERIC_BAD_CODE)
        try:
            acct = kind.create(db, pend)
        except IntegrityError:
            db.rollback()
            raise HTTPException(400, otp.GENERIC_BAD_CODE)
        db.delete(pend)
        kind.audit(db, acct, "account_created", json.dumps({"email_verified": True}))
        return _finish_login(kind, db, acct, request, response, trust=body.trust_device,
                             label=body.device_label.strip() or None, via="signup_otp")

    # ---------------- sign-in ----------------
    @r.post("/login")
    def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
        email = otp.normalize_email(body.email)
        ip = client_ip(request)
        if not email or not body.password:
            return _fields({k: "This is required." for k, v in (("email", email), ("password", body.password)) if not v})
        _need_unlocked(db, kind, email, ip)
        acct = kind.find(db, email)
        ok = False
        if acct is not None and kind.active(acct):
            ok = pw.verify_password(body.password, acct.password_hash)
        else:
            pw.burn_password_time(body.password)
        if not ok:
            otp.record_login_failure(db, account_type=kind.type, email=email, ip=ip)
            raise HTTPException(401, BAD_LOGIN)
        otp.clear_login_failures(db, account_type=kind.type, email=email)
        if random.random() < 0.05:
            otp.purge_expired(db)

        trusted = otp.is_trusted_device(db, account_type=kind.type, account_id=acct.id,
                                        token=request.cookies.get(kind.trust_cookie))
        if not otp.login_otp_required() or trusted:
            return _finish_login(kind, db, acct, request, response, via="trusted_device" if trusted else "password")

        if settings.mail_required_but_missing():
            db.commit()
            _require_mail()
        try:
            issued = otp.issue_otp(db, account_type=kind.type, email=email, purpose="login", account_id=acct.id,
                                   ip=ip, user_agent=_ua(request), new_challenge=True)
        except otp.OtpError as e:
            db.commit()  # keep the trusted-device 'last used' touch etc.
            raise _http(e)
        db.commit()
        _send_sync(db, issued, email, "login")
        return {"ok": True, "otp_required": True, "otp_challenge_id": issued.challenge_id,
                "email_hint": mailer.mask_email(email), "message": f"We've emailed a 6-digit code to {mailer.mask_email(email)}.",
                **_meta(), **_dev_fields(issued.code)}

    def _send_sync(db: Session, issued: otp.IssuedOtp, email: str, purpose: str) -> None:
        try:
            mailer.send_otp_email(email, issued.code, purpose, otp.ttl_minutes())
        except mailer.MailError as e:
            log.error("OTP email failed purpose=%s to=%s: %s", purpose, mailer.mask_email(email), e)
            db.delete(issued.row)
            db.commit()
            if isinstance(e, mailer.MailNotConfigured):
                raise HTTPException(503, settings.MAIL_NOT_CONFIGURED)
            raise HTTPException(503, "We couldn't send your code right now. Please try again in a moment.")

    @r.post("/verify-login-otp")
    def verify_login_otp(body: LoginOtpIn, request: Request, response: Response, db: Session = Depends(get_db)):
        row = otp.find_by_challenge(db, account_type=kind.type, challenge_id=body.otp_challenge_id)
        try:
            otp.check_code(db, row, body.code)
        except otp.OtpError as e:
            raise _http(e)
        acct = kind.get(db, row.account_id) if row.account_id else None
        if acct is None or not kind.active(acct) or acct.id != row.account_id:
            db.rollback()
            raise HTTPException(400, otp.GENERIC_BAD_CODE)
        return _finish_login(kind, db, acct, request, response, trust=body.trust_device,
                             label=body.device_label.strip() or None, via="password+otp")

    @r.post("/resend-login-otp")
    def resend_login_otp(body: ChallengeIn, request: Request, db: Session = Depends(get_db)):
        row = otp.find_by_challenge(db, account_type=kind.type, challenge_id=body.otp_challenge_id)
        if row is None or not row.account_id or as_utc(row.created_at) < utcnow_tz() - timedelta(minutes=30):
            raise HTTPException(400, "That sign-in attempt has expired. Please enter your email and password again.")
        _require_mail()
        try:
            issued = otp.issue_otp(db, account_type=kind.type, email=row.email, purpose="login", account_id=row.account_id,
                                   ip=client_ip(request), user_agent=_ua(request), challenge_id=body.otp_challenge_id)
        except otp.OtpError as e:
            raise _http(e)
        db.commit()
        _send_sync(db, issued, row.email, "login")
        return {"ok": True, "message": f"We've emailed a new code to {mailer.mask_email(row.email)}.",
                **_meta(), **_dev_fields(issued.code)}

    # ---------------- password reset ----------------
    @r.post("/forgot-password")
    def forgot_password(body: EmailIn, request: Request, bg: BackgroundTasks, db: Session = Depends(get_db)):
        email = otp.normalize_email(body.email)
        if (m := _check_email(email)):
            return _fields({"email": m})
        _require_mail()
        acct = kind.find(db, email)
        real = acct is not None and kind.active(acct)
        try:
            issued = otp.issue_otp(db, account_type=kind.type, email=email, purpose="reset",
                                   account_id=acct.id if real else None, ip=client_ip(request), user_agent=_ua(request))
        except otp.OtpError as e:
            raise _http(e)
        db.commit()
        if real:
            bg.add_task(_send_bg, email, issued.code, "reset")
        return {"ok": True, "message": GENERIC_SENT.format(m=otp.ttl_minutes()), **_meta(), **_dev_fields(issued.code)}

    @r.post("/reset-password")
    def reset_password(body: ResetIn, request: Request, bg: BackgroundTasks, db: Session = Depends(get_db)):
        email = otp.normalize_email(body.email)
        if (m := pw.validate_password(body.new_password, email=email)):
            return _fields({"new_password": m})
        row = otp.find_latest(db, account_type=kind.type, email=email, purpose="reset")
        try:
            otp.check_code(db, row, body.code)
        except otp.OtpError as e:
            raise _http(e)
        acct = kind.find(db, email)
        if acct is None or not kind.active(acct) or row.account_id != acct.id:
            db.rollback()
            raise HTTPException(400, otp.GENERIC_BAD_CODE)
        acct.password_hash = pw.hash_password(body.new_password)
        _revoke_sessions(db, kind, acct)
        otp.revoke_trusted_devices(db, account_type=kind.type, account_id=acct.id)
        otp.clear_login_failures(db, account_type=kind.type, email=email)
        kind.audit(db, acct, "password_reset")
        db.commit()
        bg.add_task(_notice_bg, email, f"Your {mailer.app_name()} password was changed", "Your password was changed",
                    ["Your password was just reset. You've been signed out everywhere and will need to sign in again."])
        return {"ok": True, "message": "Your password has been changed. Please sign in with your new password."}

    # ---------------- signed-in account actions ----------------
    @r.post("/change-password")
    def change_password(body: ChangePwIn, request: Request, bg: BackgroundTasks, acct=Depends(current),
                        db: Session = Depends(get_db)):
        ip = client_ip(request)
        _need_unlocked(db, kind, acct.email, ip)
        if not pw.verify_password(body.current_password, acct.password_hash):
            otp.record_login_failure(db, account_type=kind.type, email=acct.email, ip=ip)
            return _fields({"current_password": "That isn't your current password."})
        if (m := pw.validate_password(body.new_password, email=acct.email)):
            return _fields({"new_password": m})
        if body.new_password == body.current_password:
            return _fields({"new_password": "Choose a password you haven't used here before."})
        acct.password_hash = pw.hash_password(body.new_password)
        _revoke_sessions(db, kind, acct, keep_hash=_current_hash(kind, request))
        otp.revoke_trusted_devices(db, account_type=kind.type, account_id=acct.id)
        kind.audit(db, acct, "password_changed")
        db.commit()
        bg.add_task(_notice_bg, acct.email, f"Your {mailer.app_name()} password was changed", "Your password was changed",
                    ["Your password was just changed from your account settings. Other devices have been signed out."])
        return {"ok": True, "message": "Password changed. Other devices were signed out."}

    @r.post("/email-change/request")
    def email_change_request(body: EmailChangeIn, request: Request, bg: BackgroundTasks, acct=Depends(current),
                             db: Session = Depends(get_db)):
        ip = client_ip(request)
        _need_unlocked(db, kind, acct.email, ip)
        if not pw.verify_password(body.password, acct.password_hash):
            otp.record_login_failure(db, account_type=kind.type, email=acct.email, ip=ip)
            return _fields({"password": "That isn't your current password."})
        new = otp.normalize_email(body.new_email)
        if (m := _check_email(new)):
            return _fields({"new_email": m})
        if new == otp.normalize_email(acct.email):
            return _fields({"new_email": "That is already your email address."})
        _require_mail()
        free = kind.find(db, new) is None
        try:
            issued = otp.issue_otp(db, account_type=kind.type, email=new, purpose="email_change",
                                   account_id=acct.id if free else None, ip=ip, user_agent=_ua(request))
        except otp.OtpError as e:
            raise _http(e)
        db.commit()
        if free:
            bg.add_task(_send_bg, new, issued.code, "email_change")
        return {"ok": True, "message": GENERIC_SENT.format(m=otp.ttl_minutes()), **_meta(), **_dev_fields(issued.code)}

    @r.post("/email-change/confirm")
    def email_change_confirm(body: EmailChangeIn, request: Request, bg: BackgroundTasks, acct=Depends(current),
                             db: Session = Depends(get_db)):
        new = otp.normalize_email(body.new_email)
        row = otp.find_latest(db, account_type=kind.type, email=new, purpose="email_change")
        try:
            otp.check_code(db, row, body.code)
        except otp.OtpError as e:
            raise _http(e)
        if row.account_id != acct.id or kind.find(db, new) is not None:
            db.rollback()
            raise HTTPException(400, otp.GENERIC_BAD_CODE)
        old = acct.email
        acct.email = new
        kind.audit(db, acct, "email_changed")
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(400, otp.GENERIC_BAD_CODE)
        bg.add_task(_notice_bg, old, f"Your {mailer.app_name()} email was changed", "Your sign-in email was changed",
                    ["The email address for your account was just changed. If this wasn't you, contact support."])
        return {"ok": True, "email": new}

    @r.post("/logout")
    def logout(request: Request, response: Response, db: Session = Depends(get_db)):
        kind.end_session(db, request, response)  # trusted-device cookie is kept on purpose
        return {"ok": True}

    # ---------------- sessions & trusted devices ----------------
    @r.get("/sessions")
    def sessions(request: Request, acct=Depends(current), db: Session = Depends(get_db)):
        cur = _current_hash(kind, request)
        now = utcnow_tz()
        rows = db.scalars(select(kind.session_model).where(kind.owner_col == acct.id)).all()
        return {"items": [{"id": s.token_hash[:16], "current": s.token_hash == cur, "expires_at": as_utc(s.expires_at)}
                          for s in rows if as_utc(s.expires_at) > now]}

    @r.post("/sessions/revoke-all")
    def revoke_all(body: RevokeIn, request: Request, response: Response, acct=Depends(current),
                   db: Session = Depends(get_db)):
        n = _revoke_sessions(db, kind, acct, keep_hash=None if body.include_current else _current_hash(kind, request))
        otp.revoke_trusted_devices(db, account_type=kind.type, account_id=acct.id)
        kind.audit(db, acct, "sessions_revoked", json.dumps({"count": n}))
        db.commit()
        if body.include_current:
            response.delete_cookie(kind.session_cookie, path="/")
        return {"ok": True, "revoked": n}

    @r.delete("/sessions/{sid}")
    def revoke_one(sid: str, request: Request, acct=Depends(current), db: Session = Depends(get_db)):
        if len(sid) < 8 or len(sid) > 64 or not re.fullmatch(r"[0-9a-f]+", sid):
            raise HTTPException(404, "Not found.")
        rows = db.scalars(select(kind.session_model).where(kind.owner_col == acct.id,
                                                           kind.session_model.token_hash.like(sid + "%"))).all()
        if len(rows) != 1:
            raise HTTPException(404, "Not found.")
        db.delete(rows[0])
        kind.audit(db, acct, "session_revoked")
        db.commit()
        return {"ok": True}

    @r.get("/trusted-devices")
    def trusted_devices(acct=Depends(current), db: Session = Depends(get_db)):
        now = utcnow_tz()
        rows = db.scalars(select(TrustedDevice).where(TrustedDevice.account_type == kind.type,
                                                      TrustedDevice.account_id == acct.id)
                          .order_by(TrustedDevice.created_at.desc())).all()
        return {"items": [{"id": t.id, "label": t.label, "created_at": as_utc(t.created_at),
                           "last_used_at": as_utc(t.last_used_at), "expires_at": as_utc(t.expires_at)}
                          for t in rows if as_utc(t.expires_at) > now]}

    @r.delete("/trusted-devices/{tid}")
    def forget_device(tid: str, acct=Depends(current), db: Session = Depends(get_db)):
        row = db.get(TrustedDevice, tid)
        if not row or row.account_type != kind.type or row.account_id != acct.id:
            raise HTTPException(404, "Not found.")
        db.delete(row)
        kind.audit(db, acct, "trusted_device_removed")
        db.commit()
        return {"ok": True}

    if kind.type == "staff":
        # W3's older password-only route is /api/staff/login. Shadow it (this module registers first) so
        # staff can never skip the email OTP step through the legacy path.
        legacy = APIRouter()
        legacy.add_api_route("/api/staff/login", login, methods=["POST"], include_in_schema=False)
        router.include_router(legacy)
    return r


router.include_router(build(PATIENT))
router.include_router(build(STAFF))
