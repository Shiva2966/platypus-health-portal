"""Shared helpers/fixtures for the W6 OTP tests. Not a test module."""
import os
import tempfile
import uuid
import warnings

warnings.filterwarnings("ignore")
os.environ.setdefault("HP_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="hp_w6_"), "test.db"))
os.environ["HP_DISABLE_SWEEP"] = "1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.auth import LoginFailure, OtpCode, PendingSignup, TrustedDevice  # noqa: E402
from app.models.shared import Patient  # noqa: E402
from app.models.staff import StaffUser  # noqa: E402
from app.services import auth_passwords, mailer  # noqa: E402

PW = "Tr1cky-Umbrella-Walrus"
NEW_PW = "Orange-Falcon-Lantern-42"


def email() -> str:
    return f"w6-{uuid.uuid4().hex[:10]}@example.test"


def client() -> TestClient:
    return TestClient(app)


def db():
    return SessionLocal()


class Outbox:
    """Captures what the (mocked) mailer would have sent."""

    def __init__(self):
        self.otps: list[dict] = []
        self.notices: list[dict] = []

    def codes(self, to: str, purpose: str) -> list[str]:
        return [m["code"] for m in self.otps if m["to"] == to and m["purpose"] == purpose]

    def last(self, to: str, purpose: str) -> str:
        c = self.codes(to, purpose)
        assert c, f"no {purpose} email was sent to {to}"
        return c[-1]


@pytest.fixture
def strict(monkeypatch):
    """Real behaviour: verification + login OTP required, no cooldown unless a test asks for it."""
    for k, v in {"LOGIN_OTP_REQUIRED": "1", "SIGNUP_VERIFY_REQUIRED": "1", "OTP_RESEND_COOLDOWN_SECONDS": "0",
                 "DEV_SHOW_OTP": "0", "STAFF_INVITE_CODE": "", "SMTP_USER": "", "SMTP_PASSWORD": "",
                 "OTP_EMAIL_HOURLY_CAP": "50", "OTP_IP_HOURLY_CAP": "500", "LOGIN_MAX_FAILURES": "5",
                 "STAFF_SELF_SIGNUP": ""}.items():
        monkeypatch.setenv(k, v)
    s = SessionLocal()
    try:
        for m in (OtpCode, LoginFailure, PendingSignup, TrustedDevice):
            s.execute(delete(m))
        s.commit()
    finally:
        s.close()
    yield monkeypatch


@pytest.fixture
def outbox(strict, monkeypatch):
    box = Outbox()

    def fake_otp(to, code, purpose, ttl_minutes=10):
        box.otps.append({"to": to, "code": code, "purpose": purpose})
        return mailer.SendResult("dev", False)

    def fake_notice(to, subject, headline, lines):
        box.notices.append({"to": to, "subject": subject, "headline": headline, "lines": lines})
        return mailer.SendResult("dev", False)

    monkeypatch.setattr(mailer, "send_otp_email", fake_otp)
    monkeypatch.setattr(mailer, "send_notice_email", fake_notice)
    return box


def make_patient(addr: str | None = None, password: str = PW, scrypt: bool = False) -> str:
    """A pre-existing (seed-like) patient: already verified because the row exists."""
    from app import security

    addr = addr or email()
    s = SessionLocal()
    try:
        p = Patient(email=addr, password_hash=(security.hash_password(password) if scrypt
                                               else auth_passwords.hash_password(password)),
                    legal_name="Test Patient", dob="1985-05-05")
        s.add(p)
        s.commit()
        return addr
    finally:
        s.close()


def make_staff(addr: str | None = None, password: str = PW, role: str = "nurse", active: bool = True) -> str:
    addr = addr or email()
    s = SessionLocal()
    try:
        s.add(StaffUser(email=addr, password_hash=auth_passwords.hash_password(password), name="Test Staff",
                        role=role, active=active))
        s.commit()
        return addr
    finally:
        s.close()


def signin_patient(c: TestClient, outbox: Outbox, addr: str, password: str = PW, trust: bool = False):
    """Full password + OTP sign-in; returns the verify response."""
    r = c.post("/api/auth/login", json={"email": addr, "password": password})
    assert r.status_code == 200, r.text
    if not r.json().get("otp_required"):
        return r
    r2 = c.post("/api/auth/verify-login-otp", json={"otp_challenge_id": r.json()["otp_challenge_id"],
                                                    "code": outbox.last(addr, "login"), "trust_device": trust})
    assert r2.status_code == 200, r2.text
    return r2
