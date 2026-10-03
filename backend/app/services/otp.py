"""One-time passcodes (email OTP), trusted devices, login-failure lockout.

All limits are read from the environment at call time (see docs/EMAIL_SETUP.md):
    OTP_TTL_MINUTES=10  OTP_MAX_ATTEMPTS=5  OTP_RESEND_COOLDOWN_SECONDS=45
    OTP_EMAIL_HOURLY_CAP=6  OTP_IP_HOURLY_CAP=30
    LOGIN_MAX_FAILURES=5  LOGIN_LOCKOUT_MINUTES=15  TRUST_DEVICE_DAYS=30
    OTP_SECRET=<random string>  (HMAC key; auto-generated into data/.otp_secret when unset - set it in production)

Transactions: functions here `flush` but do NOT commit, with ONE deliberate exception - failed
verification attempts are committed immediately so a rollback later can never "refund" a guess.

Anti-enumeration: callers create an OtpCode row even for emails that have no account (they just don't
send the email), so cooldown / hourly caps / timing behave identically for every address.
"""
import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models.auth import LoginFailure, OtpCode, TrustedDevice, as_utc, utcnow_tz

log = logging.getLogger("health-portal.auth")

ROOT = Path(__file__).resolve().parent.parent.parent
GENERIC_BAD_CODE = "That code isn't right or has expired. Check the newest email, or ask for a new code."


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def ttl_minutes() -> int:
    return max(1, _int_env("OTP_TTL_MINUTES", 10))


def resend_cooldown() -> int:
    return max(0, _int_env("OTP_RESEND_COOLDOWN_SECONDS", 45))


def max_attempts() -> int:
    return max(1, _int_env("OTP_MAX_ATTEMPTS", 5))


def login_otp_required() -> bool:
    return (os.environ.get("LOGIN_OTP_REQUIRED", "1").strip().lower() not in ("0", "false", "no", "off"))


def signup_verify_required() -> bool:
    return (os.environ.get("SIGNUP_VERIFY_REQUIRED", "1").strip().lower() not in ("0", "false", "no", "off"))


def trust_days() -> int:
    return max(1, _int_env("TRUST_DEVICE_DAYS", 30))


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


class OtpError(Exception):
    """Carries an HTTP status + user-safe message (+ optional Retry-After seconds)."""

    def __init__(self, status: int, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status, self.message, self.retry_after = status, message, retry_after


@dataclass
class IssuedOtp:
    row: OtpCode
    code: str  # plaintext, handed to the mailer ONLY - never persisted or logged
    challenge_id: str | None = None


# ---------------- hashing ----------------

_secret_cache: bytes | None = None


def _secret() -> bytes:
    global _secret_cache
    env = os.environ.get("OTP_SECRET", "").strip() or os.environ.get("SECRET_KEY", "").strip()
    if env:
        return env.encode()
    if _secret_cache is None:
        path = ROOT / "data" / ".otp_secret"  # data/ is git-ignored
        try:
            if path.exists():
                _secret_cache = path.read_text().strip().encode()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                val = secrets.token_hex(32)
                path.write_text(val)
                _secret_cache = val.encode()
        except OSError:  # read-only FS: fall back to a per-process key (codes just don't survive restarts)
            _secret_cache = secrets.token_hex(32).encode()
    return _secret_cache


def _hash_code(code: str, salt: str, email: str, purpose: str) -> str:
    msg = f"{salt}|{email}|{purpose}|{code}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def make_code_hash(code: str, email: str, purpose: str) -> str:
    salt = secrets.token_hex(8)
    return f"{salt}${_hash_code(code, salt, email, purpose)}"


def code_matches(stored: str, code: str, email: str, purpose: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    expected = _hash_code(code, salt, email, purpose)
    return hmac.compare_digest(expected.encode(), digest.encode())  # constant time


def hash_challenge(challenge_id: str) -> str:
    return hashlib.sha256(challenge_id.encode()).hexdigest()


# ---------------- issuing ----------------

def _newest(db: Session, account_type: str, email: str, purpose: str) -> OtpCode | None:
    return db.scalars(select(OtpCode).where(
        OtpCode.account_type == account_type, OtpCode.email == email, OtpCode.purpose == purpose)
        .order_by(OtpCode.created_at.desc(), OtpCode.id.desc()).limit(1)).first()


def issue_otp(db: Session, *, account_type: str, email: str, purpose: str, account_id: str | None = None,
              ip: str | None = None, user_agent: str | None = None, challenge_id: str | None = None,
              new_challenge: bool = False) -> IssuedOtp:
    """Create a fresh code, invalidating older ones for the same (account type, email, purpose).

    Raises OtpError(429) for resend cooldown / hourly caps. Does not commit.
    """
    email = normalize_email(email)
    now = utcnow_tz()

    last = _newest(db, account_type, email, purpose)
    if last is not None:
        wait = resend_cooldown() - (now - as_utc(last.created_at)).total_seconds()
        if wait > 0:
            raise OtpError(429, f"Please wait {int(wait) + 1} seconds before asking for another code.", int(wait) + 1)

    hour_ago = now - timedelta(hours=1)
    email_cap = max(1, _int_env("OTP_EMAIL_HOURLY_CAP", 6))
    n_email = db.scalar(select(func.count()).select_from(OtpCode).where(
        OtpCode.email == email, OtpCode.created_at >= hour_ago)) or 0
    if n_email >= email_cap:
        raise OtpError(429, "Too many codes requested for this email. Please try again in about an hour.", 3600)
    if ip:
        ip_cap = max(1, _int_env("OTP_IP_HOURLY_CAP", 30))
        n_ip = db.scalar(select(func.count()).select_from(OtpCode).where(
            OtpCode.ip == ip, OtpCode.created_at >= hour_ago)) or 0
        if n_ip >= ip_cap:
            raise OtpError(429, "Too many codes requested from this network. Please try again later.", 3600)

    # one live code at a time
    db.execute(update(OtpCode).where(
        OtpCode.account_type == account_type, OtpCode.email == email, OtpCode.purpose == purpose,
        OtpCode.consumed_at.is_(None)).values(consumed_at=now))

    code = f"{secrets.randbelow(10**6):06d}"
    if new_challenge:
        challenge_id = secrets.token_urlsafe(24)
    row = OtpCode(account_type=account_type, account_id=account_id, email=email, purpose=purpose,
                  code_hash=make_code_hash(code, email, purpose),
                  challenge_hash=hash_challenge(challenge_id) if challenge_id else None,
                  created_at=now, expires_at=now + timedelta(minutes=ttl_minutes()),
                  attempts=0, max_attempts=max_attempts(), ip=(ip or None), user_agent=(user_agent or "")[:300] or None)
    db.add(row)
    db.flush()
    return IssuedOtp(row=row, code=code, challenge_id=challenge_id)


# ---------------- verifying ----------------

def find_latest(db: Session, *, account_type: str, email: str, purpose: str) -> OtpCode | None:
    return _newest(db, account_type, normalize_email(email), purpose)


def find_by_challenge(db: Session, *, account_type: str, challenge_id: str) -> OtpCode | None:
    if not challenge_id or len(challenge_id) > 200:
        return None
    return db.scalars(select(OtpCode).where(
        OtpCode.account_type == account_type, OtpCode.purpose == "login",
        OtpCode.challenge_hash == hash_challenge(challenge_id))
        .order_by(OtpCode.created_at.desc(), OtpCode.id.desc()).limit(1)).first()


def check_code(db: Session, row: OtpCode | None, code: str) -> OtpCode:
    """Validate `code` against `row` and mark it consumed (uncommitted). Raises OtpError(400) with a
    generic message for every failure (no row / expired / used / too many tries / wrong code)."""
    bad = OtpError(400, GENERIC_BAD_CODE)
    code = (code or "").strip().replace(" ", "")
    if row is None:
        # keep timing similar to a real check
        code_matches("x$y", code, "", "")
        raise bad
    now = utcnow_tz()
    if row.consumed_at is not None or as_utc(row.expires_at) <= now:
        raise bad
    # count the attempt FIRST (atomic) and persist it even if the caller later rolls back
    res = db.execute(update(OtpCode).where(
        OtpCode.id == row.id, OtpCode.consumed_at.is_(None), OtpCode.attempts < OtpCode.max_attempts)
        .values(attempts=OtpCode.attempts + 1))
    if res.rowcount != 1:
        db.rollback()
        raise bad
    db.commit()
    db.refresh(row)
    ok = len(code) == 6 and code.isdigit() and code_matches(row.code_hash, code, row.email, row.purpose)
    if not ok:
        if row.attempts >= row.max_attempts:  # lock this code for good
            db.execute(update(OtpCode).where(OtpCode.id == row.id, OtpCode.consumed_at.is_(None)).values(consumed_at=now))
            db.commit()
        raise bad
    # single use, race-safe: only one concurrent request can flip consumed_at
    res = db.execute(update(OtpCode).where(OtpCode.id == row.id, OtpCode.consumed_at.is_(None)).values(consumed_at=now))
    if res.rowcount != 1:
        db.rollback()
        raise bad
    return row


# ---------------- trusted devices ----------------

def create_trusted_device(db: Session, *, account_type: str, account_id: str, label: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = utcnow_tz()
    db.add(TrustedDevice(account_type=account_type, account_id=account_id,
                         token_hash=hashlib.sha256(token.encode()).hexdigest(), label=(label or "")[:120] or None,
                         created_at=now, last_used_at=now, expires_at=now + timedelta(days=trust_days())))
    db.flush()
    return token


def is_trusted_device(db: Session, *, account_type: str, account_id: str, token: str | None) -> bool:
    if not token or len(token) > 200:
        return False
    row = db.scalars(select(TrustedDevice).where(
        TrustedDevice.token_hash == hashlib.sha256(token.encode()).hexdigest(),
        TrustedDevice.account_type == account_type, TrustedDevice.account_id == account_id)).first()
    if row is None or as_utc(row.expires_at) <= utcnow_tz():
        return False
    row.last_used_at = utcnow_tz()
    return True


def revoke_trusted_devices(db: Session, *, account_type: str, account_id: str) -> int:
    return db.execute(delete(TrustedDevice).where(
        TrustedDevice.account_type == account_type, TrustedDevice.account_id == account_id)).rowcount or 0


# ---------------- bad-password lockout / backoff ----------------

def _fail_cfg() -> tuple[int, int]:
    return max(1, _int_env("LOGIN_MAX_FAILURES", 5)), max(1, _int_env("LOGIN_LOCKOUT_MINUTES", 15))


def lockout_seconds(db: Session, *, account_type: str, email: str, ip: str | None) -> int:
    """Seconds the caller must wait (0 = not locked). Works the same for unknown emails.

    Exponential backoff: after LOGIN_MAX_FAILURES bad passwords within an hour the lock lasts
    LOGIN_LOCKOUT_MINUTES, doubling with each further failure (max x16). Attempts made while locked
    are not counted (so an attacker can't extend a victim's lock by hammering).
    """
    email = normalize_email(email)
    limit, minutes = _fail_cfg()
    now = utcnow_tz()
    since = now - timedelta(hours=1)
    rows = db.scalars(select(LoginFailure.ts).where(
        LoginFailure.account_type == account_type, LoginFailure.email == email, LoginFailure.ip == ip,
        LoginFailure.ts >= since).order_by(LoginFailure.ts)).all()
    worst = 0
    if len(rows) >= limit:
        lock = minutes * 60 * (2 ** min(len(rows) - limit, 4))
        worst = max(worst, int(lock - (now - as_utc(rows[-1])).total_seconds()))
    # same email from many addresses (distributed guessing): higher threshold, same lock
    rows_all = db.scalars(select(LoginFailure.ts).where(
        LoginFailure.account_type == account_type, LoginFailure.email == email,
        LoginFailure.ts >= since).order_by(LoginFailure.ts)).all()
    if len(rows_all) >= limit * 4:
        lock = minutes * 60
        worst = max(worst, int(lock - (now - as_utc(rows_all[-1])).total_seconds()))
    if ip:
        ip_n = db.scalar(select(func.count()).select_from(LoginFailure).where(
            LoginFailure.ip == ip, LoginFailure.ts >= now - timedelta(minutes=minutes))) or 0
        if ip_n >= limit * 6:
            worst = max(worst, minutes * 60)
    return max(0, worst)


def record_login_failure(db: Session, *, account_type: str, email: str, ip: str | None) -> None:
    db.add(LoginFailure(account_type=account_type, email=normalize_email(email), ip=ip, ts=utcnow_tz()))
    db.commit()  # failures must survive any later rollback


def clear_login_failures(db: Session, *, account_type: str, email: str) -> None:
    db.execute(delete(LoginFailure).where(LoginFailure.account_type == account_type,
                                          LoginFailure.email == normalize_email(email)))


def purge_expired(db: Session) -> None:
    """Housekeeping (cheap; called opportunistically from sign-in): drop old codes/failures/pending sign-ups."""
    from app.models.auth import PendingSignup

    now = utcnow_tz()
    db.execute(delete(OtpCode).where(OtpCode.created_at < now - timedelta(days=2)))
    db.execute(delete(LoginFailure).where(LoginFailure.ts < now - timedelta(days=1)))
    db.execute(delete(PendingSignup).where(PendingSignup.expires_at < now))
    db.execute(delete(TrustedDevice).where(TrustedDevice.expires_at < now))
