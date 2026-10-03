"""APP_ENV=production behaviour (PROD agent): no OTP exposure, fail-closed email, headers, docs off,
cookies, staff invite code, rate limits, size limits, generic errors, email change by OTP."""
import logging
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import otp_support as s
from otp_support import PW, strict  # noqa: F401  (fixture)
from app import hardening, settings
from app.main import app
from app.models.auth import OtpCode
from app.models.shared import Patient
from app.services import mailer

SMTP_SECRET = "prod-test-app-password-NEVER-SHOWN"


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def _code(msg) -> str:
    return re.search(r"Your code: (\d{6})", msg.get_body(preferencelist=("plain",)).get_content()).group(1)


@pytest.fixture
def prod(strict):
    strict.setenv("APP_ENV", "production")
    strict.setenv("SECRET_KEY", "test-secret-key-for-prod-mode-tests-only")
    strict.setenv("AUTH_RATE_LIMIT_PER_MINUTE", "0")
    strict.delenv("COOKIE_SECURE", raising=False)
    strict.delenv("SHOW_DEMO_BANNER", raising=False)
    hardening.RATE_LIMITER.reset()
    yield strict
    hardening.RATE_LIMITER.reset()


@pytest.fixture
def prod_smtp(prod):
    FakeSMTP.sent = []
    for k, v in {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587", "SMTP_USER": "sender@example.test",
                 "SMTP_FROM": "sender@example.test", "SMTP_PASSWORD": SMTP_SECRET}.items():
        prod.setenv(k, v)
    prod.setattr("smtplib.SMTP", FakeSMTP)
    prod.setattr(mailer, "is_reserved_recipient", lambda addr: False)  # FakeSMTP stays in-process
    return prod


def https_client() -> TestClient:
    return TestClient(app, base_url="https://testserver")


def _otp_rows() -> int:
    with s.db() as d:
        return d.scalar(select(func.count()).select_from(OtpCode))


# ------------------------------------------------------------------ fail closed without SMTP
def test_prod_without_smtp_fails_closed_with_clear_503(prod, capsys):
    prod.setenv("DEV_SHOW_OTP", "1")  # must be ignored
    known = s.make_patient()
    c = s.client()
    r = c.post("/api/auth/login", json={"email": known, "password": PW})
    assert r.status_code == 503 and "Email sending isn't configured" in r.json()["detail"]
    for path, body in (("/api/auth/signup", {"email": s.email(), "password": "Orange-Falcon-Lantern-42", "name": "New Person"}),
                       ("/api/auth/forgot-password", {"email": known}),
                       ("/api/auth/resend-verification", {"email": known}),
                       ("/api/staff/auth/forgot-password", {"email": known})):
        r = c.post(path, json=body)
        assert r.status_code == 503, (path, r.text)
        assert "dev_otp" not in r.text
    assert _otp_rows() == 0  # no code was even created
    assert "DEV MODE" not in capsys.readouterr().out
    cfg = c.get("/api/auth/config").json()
    assert cfg["app_env"] == "production" and cfg["email_delivery"] == "not_configured"
    assert cfg["dev_otp_inline"] is False and cfg["email_ready"] is False


def test_mailer_never_prints_codes_in_production(prod, capsys):
    with pytest.raises(mailer.MailNotConfigured):
        mailer.send_otp_email("a@example.test", "424242", "login", 10)
    with pytest.raises(mailer.MailNotConfigured):
        mailer.send_notice_email("a@example.test", "s", "h", ["x"])
    out = capsys.readouterr()
    assert "424242" not in out.out + out.err


def test_readyz_reports_mailer(prod):
    prod.setenv("READYZ_TOKEN", "prod-readyz-token")  # HARD: details only with the token in production
    r = s.client().get("/readyz", headers={"X-Readyz-Token": "prod-readyz-token"})
    assert r.status_code == 503 and r.json()["checks"]["mailer"] == {
        "ok": False, "configured": False, "app_env": "production", "mode": "not_configured"}
    assert s.client().get("/readyz").json() == {"status": "unavailable"}


def test_readyz_mailer_ok_when_configured(prod_smtp):
    prod_smtp.setenv("READYZ_TOKEN", "prod-readyz-token")
    m = s.client().get("/readyz", headers={"X-Readyz-Token": "prod-readyz-token"}).json()["checks"]["mailer"]
    assert m["ok"] and m["configured"] and m["mode"] == "smtp"


# ------------------------------------------------------------------ no OTP exposure with SMTP
def test_prod_otp_only_goes_to_typed_address(prod_smtp, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    prod_smtp.setenv("DEV_SHOW_OTP", "1")  # ignored in production
    known = s.make_patient()
    c = https_client()
    r = c.post("/api/auth/login", json={"email": known, "password": PW})
    assert r.status_code == 200 and r.json()["otp_required"]
    assert "dev_otp" not in r.json() and "code" not in r.json()
    assert len(FakeSMTP.sent) == 1
    msg = FakeSMTP.sent[0]
    assert msg["To"] == known
    code = _code(msg)
    assert code not in r.text and code not in str(r.headers)
    # headers of a well-formed transactional email
    assert msg["Subject"] == f"Your {mailer.app_name()} verification code"
    assert mailer.app_name() in msg["From"] and "sender@example.test" in msg["From"]
    assert msg["Reply-To"] and msg["Message-ID"] and msg["Date"]
    assert msg.get_body(preferencelist=("plain",)) is not None and msg.get_body(preferencelist=("html",)) is not None
    assert "Demo application" not in msg.get_body(preferencelist=("html",)).get_content()
    ok = c.post("/api/auth/verify-login-otp", json={"otp_challenge_id": r.json()["otp_challenge_id"], "code": code})
    assert ok.status_code == 200
    out = capsys.readouterr()
    everything = caplog.text + out.out + out.err
    assert code not in everything and SMTP_SECRET not in everything and PW not in everything


def test_prod_config_hides_dev_flags(prod_smtp):
    prod_smtp.setenv("DEV_SHOW_OTP", "1")
    cfg = s.client().get("/api/auth/config").json()
    assert cfg["dev_otp_inline"] is False and cfg["email_delivery"] == "smtp"


# ------------------------------------------------------------------ headers / docs / banner / cookies
def test_security_headers(prod):
    r = s.client().get("/")
    h = r.headers
    assert "default-src 'self'" in h["content-security-policy"]
    assert h["x-frame-options"] == "SAMEORIGIN" and h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer" and "permissions-policy" in h
    assert "strict-transport-security" not in h  # plain HTTP
    hs = https_client().get("/").headers
    assert hs["strict-transport-security"].startswith("max-age=")
    api = s.client().get("/api/auth/config").headers
    assert api["cache-control"] == "no-store" and api["x-frame-options"] == "SAMEORIGIN"


def test_demo_banner_hidden_in_production_shown_in_development(prod):
    for path in ("/", "/staff/"):
        r = s.client().get(path)
        assert r.status_code == 200
        assert "demo-banner" not in r.text and "(Demo)" not in r.text.split("</title>")[0]
        assert 'data-demo="0"' in r.text
        assert int(r.headers["content-length"]) == len(r.content)
    prod.setenv("APP_ENV", "development")
    r = s.client().get("/")
    assert "demo-banner" in r.text and 'data-demo="1"' in r.text


def test_openapi_docs_disabled_in_production(prod):
    assert hardening.fastapi_kwargs() == {"docs_url": None, "redoc_url": None, "openapi_url": None}
    probe = FastAPI(**hardening.fastapi_kwargs())
    c = TestClient(probe)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404
    import inspect

    import app.main as main_mod
    assert "**hardening.fastapi_kwargs()" in inspect.getsource(main_mod)
    prod.setenv("APP_ENV", "development")
    assert hardening.fastapi_kwargs() == {}


def test_cookies_secure_httponly_samesite_in_production(prod):
    prod.setenv("LOGIN_OTP_REQUIRED", "0")
    known = s.make_patient()
    r = s.client().post("/api/auth/login", json={"email": known, "password": PW})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "hp_patient=" in cookie and "Secure" in cookie and "HttpOnly" in cookie and "SameSite=" in cookie
    prod.setenv("COOKIE_SAMESITE", "strict")
    cookie = s.client().post("/api/auth/login", json={"email": known, "password": PW}).headers["set-cookie"]
    assert "SameSite=Strict" in cookie and cookie.count("SameSite") == 1


def test_development_cookies_not_forced_secure_on_http(strict):
    strict.setenv("LOGIN_OTP_REQUIRED", "0")
    known = s.make_patient()
    cookie = s.client().post("/api/auth/login", json={"email": known, "password": PW}).headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" not in cookie


def test_cors_only_for_allowed_origins_via_csrf_check(prod):
    c = s.client()
    r = c.post("/api/auth/login", json={"email": "x@example.test", "password": "y"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    prod.setenv("ALLOWED_ORIGINS", "https://app.example.com")
    r = c.post("/api/auth/login", json={"email": "x@example.test", "password": "y"}, headers={"Origin": "https://app.example.com"})
    assert r.status_code == 401  # passed the origin check, then normal bad-login


# ------------------------------------------------------------------ staff invite, rate limit, size limit, errors
def test_staff_signup_requires_invite_code_in_production(prod_smtp):
    body = {"email": s.email(), "password": "Orange-Falcon-Lantern-42", "name": "New Staff"}
    prod_smtp.setenv("STAFF_SELF_SIGNUP", "1")
    assert s.client().post("/api/staff/auth/signup", json=body).status_code == 403
    prod_smtp.setenv("STAFF_INVITE_CODE", "letmein-staff-2026")
    assert s.client().post("/api/staff/auth/signup", json={**body, "invite_code": "wrong"}).status_code == 403
    r = s.client().post("/api/staff/auth/signup", json={**body, "invite_code": "letmein-staff-2026"})
    assert r.status_code == 200 and "dev_otp" not in r.json()


def test_auth_rate_limit(prod):
    prod.setenv("AUTH_RATE_LIMIT_PER_MINUTE", "3")
    c = s.client()
    codes = [c.post("/api/auth/login", json={"email": "nobody@example.test", "password": "wrong-password"}).status_code
             for _ in range(4)]
    assert codes[:3] == [401, 401, 401] and codes[3] == 429
    assert c.get("/api/auth/config").status_code == 200  # GETs are not limited


def test_request_size_limits(prod):
    prod.setenv("MAX_JSON_BYTES", "1000")
    c = s.client()
    big = '{"email":"' + "a" * 5000 + '@x.test","password":"p"}'
    r = c.post("/api/auth/login", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    prod.setenv("MAX_REQUEST_BYTES", "2000")
    r = c.post("/api/documents", files={"file": ("a.txt", b"x" * 5000, "text/plain")}, data={"name": "a"})
    assert r.status_code == 413


def test_unhandled_errors_are_generic_and_not_logged_with_message(prod, caplog):
    probe = FastAPI()

    @probe.get("/api/boom")
    def boom():
        raise ValueError("Jordan Ellis DOB 1987-03-14")

    probe.add_middleware(hardening.HardeningMiddleware)
    caplog.set_level(logging.DEBUG)
    r = TestClient(probe, raise_server_exceptions=False).get("/api/boom")
    assert r.status_code == 500 and r.json() == {"detail": hardening.GENERIC_500}
    assert "Jordan" not in caplog.text and "ValueError" in caplog.text


def test_access_log_query_strings_are_stripped():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/api/staff/patients/search?q=Jordan&dob=1987-03-14", "1.1", 200), None)
    settings._StripQueryFilter().filter(rec)
    assert "Jordan" not in rec.getMessage() and "/api/staff/patients/search" in rec.getMessage()


def test_html_404_is_generic(prod):
    r = s.client().get("/no-such-page", headers={"Accept": "text/html"})
    assert r.status_code == 404 and "Page not found" in r.text
    assert s.client().get("/api/no-such").json() == {"detail": "Not Found"}


# ------------------------------------------------------------------ email change needs OTP at the NEW address
def test_profile_email_change_requires_otp_to_new_address(prod_smtp):
    prod_smtp.setenv("LOGIN_OTP_REQUIRED", "0")
    old = s.make_patient()
    new = s.email()
    c = https_client()
    assert c.post("/api/auth/login", json={"email": old, "password": PW}).status_code == 200
    # the plain profile endpoint refuses to change the sign-in email
    r = c.put("/api/profile", json={"email": new, "legal_name": "Test Patient"})
    assert r.status_code == 409
    # wrong password -> nothing sent
    r = c.post("/api/auth/email-change/request", json={"new_email": new, "password": "not-my-password"})
    assert r.status_code == 422 and not FakeSMTP.sent
    r = c.post("/api/auth/email-change/request", json={"new_email": new, "password": PW})
    assert r.status_code == 200 and "dev_otp" not in r.json()
    otp_mails = [m for m in FakeSMTP.sent if m["Subject"].endswith("verification code")]
    assert len(otp_mails) == 1 and otp_mails[0]["To"] == new  # only the NEW address gets the code
    code = _code(otp_mails[0])
    wrong = "000000" if code != "000000" else "111111"
    assert c.post("/api/auth/email-change/confirm", json={"new_email": new, "code": wrong}).status_code == 400
    r = c.post("/api/auth/email-change/confirm", json={"new_email": new, "code": code})
    assert r.status_code == 200 and r.json()["email"] == new
    with s.db() as d:
        assert d.scalar(select(Patient.email).where(func.lower(Patient.email) == new)) == new


def test_email_change_fails_closed_without_smtp(prod):
    prod.setenv("LOGIN_OTP_REQUIRED", "0")
    old = s.make_patient()
    c = https_client()
    assert c.post("/api/auth/login", json={"email": old, "password": PW}).status_code == 200
    r = c.post("/api/auth/email-change/request", json={"new_email": s.email(), "password": PW})
    assert r.status_code == 503 and "Email sending isn't configured" in r.json()["detail"]


# ------------------------------------------------------------------ secret key + .env editing
def test_write_env_key_preserves_other_bytes(tmp_path):
    p = tmp_path / ".env"
    original = b"# comment\r\nSMTP_PASSWORD=keep me exactly\r\nDEV_SHOW_OTP=1\r\nOTHER = x \r\n"
    p.write_bytes(original)
    settings.write_env_key(p, "DEV_SHOW_OTP", "0")
    settings.write_env_key(p, "APP_ENV", "production")
    assert p.read_bytes() == original.replace(b"DEV_SHOW_OTP=1", b"DEV_SHOW_OTP=0") + b"APP_ENV=production\r\n"


def test_secret_key_generated_in_production_when_missing(prod, tmp_path):
    prod.delenv("SECRET_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_bytes(b"A=1\n")
    prod.setattr(settings, "ENV_FILE", env)
    assert settings.ensure_secret_key() is True
    data = env.read_bytes().decode()
    assert data.startswith("A=1\nSECRET_KEY=") and len(data.split("SECRET_KEY=")[1].strip()) >= 40
    assert settings.ensure_secret_key() is False  # now present
