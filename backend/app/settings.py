"""Runtime mode + production policy (PROD agent). Everything is read from the environment at call time.

    APP_ENV=production|development   default: production (anything unrecognised also means production)

production:  OTP codes are only ever emailed (never in responses/UI/logs; DEV_SHOW_OTP ignored), OTP
             endpoints fail closed with 503 when SMTP isn't configured, SECRET_KEY is required, staff
             self sign-up needs STAFF_INVITE_CODE, OpenAPI docs are off, cookies are Secure by default,
             auth endpoints are rate limited, the demo banner is hidden unless SHOW_DEMO_BANNER=1.
development: console OTP fallback, DEV_SHOW_OTP=1 may show codes inline, docs on, demo banner on.
"""
import logging
import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

try:  # real environment variables always win over .env
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE, override=False)
except Exception:  # pragma: no cover
    pass

log = logging.getLogger("health-portal.settings")

MAIL_NOT_CONFIGURED = "Email sending isn't configured, so we can't send your verification code. Please contact support."


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _flag(name: str, default: bool) -> bool:
    v = _env(name).lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def app_env() -> str:
    return "development" if _env("APP_ENV").lower() in ("development", "dev", "local", "test") else "production"


def is_production() -> bool:
    return app_env() == "production"


def mail_configured() -> bool:
    from app.services import mailer

    return mailer.smtp_configured()


def mail_required_but_missing() -> bool:
    return is_production() and not mail_configured()


def dev_codes_visible() -> bool:
    """Codes / invite tokens may be returned to the browser: development + no SMTP + DEV_SHOW_OTP=1 only."""
    return (not is_production()) and (not mail_configured()) and _env("DEV_SHOW_OTP") == "1"


def show_demo_banner() -> bool:
    return _flag("SHOW_DEMO_BANNER", not is_production())


def docs_enabled() -> bool:
    return _flag("ENABLE_API_DOCS", not is_production())


def staff_invite_code() -> str:
    return _env("STAFF_INVITE_CODE")


def trust_proxy() -> bool:
    return _env("TRUST_PROXY") == "1"


def request_is_https(scope_or_request) -> bool:
    scope = getattr(scope_or_request, "scope", scope_or_request)
    if scope.get("scheme") == "https":
        return True
    peer = (scope.get("client") or ("",))[0]
    if trust_proxy() or peer in trusted_proxy_ips():
        for k, v in scope.get("headers") or []:
            if k == b"x-forwarded-proto":
                return v.decode("latin-1").split(",")[0].strip().lower() == "https"
    return False


def cookie_secure_mode() -> bool | None:
    """True = always Secure, False = never forced, None = only on HTTPS requests."""
    v = _env("COOKIE_SECURE").lower()
    if v in ("1", "true", "yes", "on") or _env("HP_SECURE_COOKIES") == "1":
        return True
    if v in ("0", "false", "no", "off"):
        return None if not is_production() else False
    return True if is_production() else None


def cookie_samesite() -> str:
    v = _env("COOKIE_SAMESITE").lower()
    return v if v in ("lax", "strict", "none") else ""


def allowed_origins() -> list[str]:
    raw = _env("ALLOWED_ORIGINS") or _env("CORS_ALLOWED_ORIGINS")
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip() and o.strip() != "*"]


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def max_request_bytes() -> int:
    return _int("MAX_REQUEST_BYTES", 20 * 1024 * 1024)


def max_json_bytes() -> int:
    return _int("MAX_JSON_BYTES", 1024 * 1024)


def auth_rate_limit_per_minute() -> int:
    return _int("AUTH_RATE_LIMIT_PER_MINUTE", 30 if is_production() else 0)


def api_rate_limit_per_minute() -> int:
    """Every /api/* request, per client IP (0 = off)."""
    return _int("API_RATE_LIMIT_PER_MINUTE", 600 if is_production() else 0)


def otp_rate_limit_per_minute() -> int:
    """Code-entry / code-sending endpoints (verify, resend, forgot/reset password), per client IP."""
    return _int("OTP_RATE_LIMIT_PER_MINUTE", 10 if is_production() else 0)


def upload_rate_limit_per_minute() -> int:
    """POST/PUT of document files, per client IP."""
    return _int("UPLOAD_RATE_LIMIT_PER_MINUTE", 20 if is_production() else 0)


def invite_failures_per_window() -> int:
    """Wrong staff invite codes allowed per client IP per 15 minutes (0 = off). Applies in every mode."""
    return _int("INVITE_MAX_FAILURES", 5)


def trusted_proxy_ips() -> set[str]:
    """Direct peers allowed to tell us the real client IP (CF-Connecting-IP / X-Forwarded-For).
    Default: loopback only (the local cloudflared tunnel / reverse proxy)."""
    raw = _env("TRUSTED_PROXY_IPS", "127.0.0.1,::1")
    return {p.strip() for p in raw.split(",") if p.strip()}


# ---------------- SECRET_KEY ----------------

_KEY_LINE = re.compile(rb"^[ \t]*SECRET_KEY[ \t]*=")


def write_env_key(path: Path, key: str, value: str) -> None:
    """Set KEY=value in a .env file touching only that line (appends if absent); all other bytes stay as-is."""
    data = path.read_bytes() if path.exists() else b""
    nl = b"\r\n" if b"\r\n" in data else b"\n"
    pat = re.compile(rb"^[ \t]*" + re.escape(key.encode()) + rb"[ \t]*=.*?(?=\r?\n|\Z)", re.M)
    line = key.encode() + b"=" + value.encode()
    if pat.search(data):
        data = pat.sub(lambda _m: line, data, count=1)
    else:
        if data and not data.endswith(b"\n"):
            data += nl
        data += line + nl
    path.write_bytes(data)


def ensure_secret_key() -> bool:
    """Production needs SECRET_KEY. If missing, generate one into .env (only that key). Returns True if generated."""
    if _env("SECRET_KEY") or not is_production():
        return False
    value = secrets.token_urlsafe(48)
    try:
        write_env_key(ENV_FILE, "SECRET_KEY", value)
    except OSError as e:
        raise RuntimeError("SECRET_KEY is required in production and .env isn't writable. "
                           "Set SECRET_KEY in the environment.") from e
    os.environ["SECRET_KEY"] = value
    return True


# ---------------- startup ----------------

class _StripQueryFilter(logging.Filter):
    """uvicorn access log: drop query strings (search terms, names, DOBs) from logged URLs."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            record.args = args[:2] + (args[2].split("?", 1)[0],) + args[3:]
        return True


_started = False


def startup_checks() -> list[str]:
    """Idempotent. Returns the warnings it printed."""
    global _started
    if _started:
        return []
    _started = True
    logging.getLogger("uvicorn.access").addFilter(_StripQueryFilter())
    warnings: list[str] = []
    if not is_production():
        warnings.append("APP_ENV=development: console OTP fallback and dev conveniences are ON. "
                        "Never use this mode with real patient data.")
    else:
        if ensure_secret_key():
            warnings.append("SECRET_KEY was missing; a new random key was written to .env. Keep .env private and backed up.")
        if not mail_configured():
            warnings.append("SMTP is NOT configured (SMTP_USER/SMTP_PASSWORD). Sign-up, sign-in codes and password "
                            "reset will FAIL with 503 until it is set.")
        if not staff_invite_code():
            warnings.append("STAFF_INVITE_CODE is empty: staff self sign-up is disabled (admins can still create staff).")
        if _env("DEV_SHOW_OTP") == "1":
            warnings.append("DEV_SHOW_OTP=1 is ignored in production.")
        for name in ("LOGIN_OTP_REQUIRED", "SIGNUP_VERIFY_REQUIRED"):
            if _env(name, "1").lower() in ("0", "false", "no", "off"):
                warnings.append(f"{name}=0 weakens sign-in security in production.")
        if cookie_secure_mode() is False:
            warnings.append("COOKIE_SECURE=0 in production: session cookies can travel over plain HTTP.")
        if show_demo_banner():
            warnings.append("SHOW_DEMO_BANNER=1 in production.")
        if not _env("DATA_ENCRYPTION_KEY"):
            warnings.append("DATA_ENCRYPTION_KEY is empty: uploaded files are stored WITHOUT encryption at rest.")
        if not _env("READYZ_TOKEN"):
            warnings.append("READYZ_TOKEN is empty: /readyz details are only shown to direct localhost requests.")
    if _env("DATA_ENCRYPTION_KEY"):
        from app import data_crypto

        try:
            data_crypto.data_keys()
        except data_crypto.KeyMaterialError as e:
            raise RuntimeError(f"DATA_ENCRYPTION_KEY / DATA_ENCRYPTION_KEYS_OLD is invalid ({e}).") from e
    if warnings:
        bar = "!" * 78
        lines = [bar, f"  Health portal starting in {app_env().upper()} mode"] + [f"  - {w}" for w in warnings] + [bar]
        print("\n".join(lines), file=sys.stderr, flush=True)
        for w in warnings:
            log.warning(w)
    return warnings
