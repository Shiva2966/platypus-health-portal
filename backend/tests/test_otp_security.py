"""No plaintext codes at rest, none in logs, dev-mode rules, and mailer behaviour (SMTP faked)."""
import logging
import smtplib
import sqlite3

import pytest
from sqlalchemy import text

import otp_support as s
from otp_support import PW, outbox, strict  # noqa: F401  (fixtures)
from app.services import mailer, otp

SECRET_PASSWORD = "app-pass-SHOULD-NEVER-APPEAR"


def _code_in(msg) -> str:
    import re

    return re.search(r"Your code: (\d{6})", msg.get_body(preferencelist=("plain",)).get_content()).group(1)


class FakeSMTP:
    sent: list = []
    fail_connects = 0
    auth_error = False
    init_args: list = []

    def __init__(self, host, port, timeout=None, **kw):
        FakeSMTP.init_args.append((host, port, timeout))
        if FakeSMTP.fail_connects > 0:
            FakeSMTP.fail_connects -= 1
            raise OSError("connection refused (simulated)")
        self.tls = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        assert context is not None  # certificate checking context supplied
        self.tls = True

    def login(self, user, password):
        assert self.tls, "must STARTTLS before sending credentials"
        if FakeSMTP.auth_error:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials " + password.encode())
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def live_smtp(strict, monkeypatch):
    FakeSMTP.sent, FakeSMTP.fail_connects, FakeSMTP.auth_error, FakeSMTP.init_args = [], 0, False, []
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "sender@example.test")
    monkeypatch.setenv("SMTP_FROM", "sender@example.test")
    monkeypatch.setenv("SMTP_PASSWORD", SECRET_PASSWORD)
    monkeypatch.setenv("SMTP_RETRY_DELAY", "0")
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    # FakeSMTP never leaves the process; let the suite's @example.test addresses reach it.
    monkeypatch.setattr(mailer, "is_reserved_recipient", lambda addr: False)
    return FakeSMTP


# ------------------------------------------------------------ data at rest
def test_no_plaintext_codes_or_passwords_in_database(outbox):
    c, addr = s.client(), s.email()
    c.post("/api/auth/signup", json={"email": addr, "password": PW, "name": "Pat Example", "dob": "1990-01-02"})
    signup_code = outbox.last(addr, "signup")
    known = s.make_patient()
    c.post("/api/auth/login", json={"email": known, "password": PW})
    login_code = outbox.last(known, "login")
    c.post("/api/auth/forgot-password", json={"email": known})
    reset_code = outbox.last(known, "reset")

    with s.db() as d:
        rows = d.execute(text("SELECT * FROM otp_codes")).mappings().all()
        assert len(rows) >= 3
        blob = " ".join(str(v) for r in rows for v in r.values())
        pend = " ".join(str(v) for r in d.execute(text("SELECT * FROM pending_signups")).mappings() for v in r.values())
    for code in {signup_code, login_code, reset_code}:
        assert f"${code}" not in blob and f" {code} " not in f" {blob} "
        for r in rows:
            assert r["code_hash"] != code and code not in r["code_hash"].split("$")[0]
    for r in rows:
        assert "$" in r["code_hash"] and len(r["code_hash"]) > 60
    assert PW not in pend and "argon2id" in pend


def test_code_hash_is_salted_and_constant_time_compared():
    a, b = otp.make_code_hash("123456", "a@x.test", "login"), otp.make_code_hash("123456", "a@x.test", "login")
    assert a != b  # per-code salt
    assert otp.code_matches(a, "123456", "a@x.test", "login")
    assert not otp.code_matches(a, "123457", "a@x.test", "login")
    assert not otp.code_matches(a, "123456", "other@x.test", "login")  # bound to email
    assert not otp.code_matches(a, "123456", "a@x.test", "reset")  # bound to purpose
    assert not otp.code_matches("garbage", "123456", "a@x.test", "login")
    import inspect

    assert "compare_digest" in inspect.getsource(otp.code_matches)


def test_codes_are_six_digits_and_unpredictably_distributed():
    seen = set()
    for _ in range(50):
        with s.db() as d:
            issued = otp.issue_otp(d, account_type="patient", email=s.email(), purpose="login")
            assert len(issued.code) == 6 and issued.code.isdigit()
            seen.add(issued.code)
            d.rollback()
    assert len(seen) > 40


# ------------------------------------------------------------ logs / console / responses
def test_live_smtp_never_logs_or_prints_codes(live_smtp, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    known = s.make_patient()
    c = s.client()
    r = c.post("/api/auth/login", json={"email": known, "password": PW})
    assert r.status_code == 200 and len(live_smtp.sent) == 1
    msg = live_smtp.sent[0]
    code = _code_in(msg)
    assert code.isdigit() and len(code) == 6
    assert code not in msg["Subject"]
    c.post("/api/auth/forgot-password", json={"email": known})
    reset_code = _code_in(live_smtp.sent[-1])
    c.post("/api/auth/verify-login-otp", json={"otp_challenge_id": r.json()["otp_challenge_id"], "code": "999999"})
    out = capsys.readouterr()
    everything = caplog.text + out.out + out.err
    for secret in (code, reset_code, SECRET_PASSWORD, PW):
        assert secret not in everything
    assert "sent" in caplog.text  # something was logged, just not secrets


def test_live_smtp_never_returns_otp_even_with_dev_flag(live_smtp, strict):
    strict.setenv("DEV_SHOW_OTP", "1")
    known = s.make_patient()
    r = s.client().post("/api/auth/login", json={"email": known, "password": PW})
    assert "dev_otp" not in r.json() and "dev_otp" not in str(r.headers)
    assert s.client().post("/api/auth/forgot-password", json={"email": known}).json().get("dev_otp") is None


def test_dev_mode_prints_to_console_and_returns_code_only_when_flag_set(strict, capsys):
    known = s.make_patient()
    c = s.client()
    r = c.post("/api/auth/login", json={"email": known, "password": PW})
    assert "dev_otp" not in r.json()  # DEV_SHOW_OTP=0
    printed = capsys.readouterr().out
    assert "DEV MODE" in printed and known in printed
    strict.setenv("DEV_SHOW_OTP", "1")
    a = s.client().post("/api/auth/forgot-password", json={"email": s.email()})  # dev flag: shown even for unknown (uniform)
    assert len(a.json()["dev_otp"]) == 6
    chal = s.client().post("/api/auth/login", json={"email": known, "password": PW}).json()
    assert len(chal["dev_otp"]) == 6
    ok = c.post("/api/auth/verify-login-otp", json={"otp_challenge_id": chal["otp_challenge_id"], "code": chal["dev_otp"]})
    assert ok.status_code == 200


# ------------------------------------------------------------ mailer unit tests
def test_mailer_dev_mode_sends_nothing(strict, capsys):
    assert mailer.smtp_configured() is False
    res = mailer.send_otp_email("a@example.test", "123456", "login", 10)
    assert res.mode == "dev" and res.delivered is False
    assert "123456" in capsys.readouterr().out


def test_mailer_message_content(live_smtp):
    res = mailer.send_otp_email("patient@example.test", "482913", "signup", 10)
    assert res.mode == "smtp" and res.delivered
    msg = live_smtp.sent[0]
    assert msg["To"] == "patient@example.test" and "sender@example.test" in msg["From"]
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    for part in (plain, html):
        assert "482913" in part or "482 913" in part
        assert "10 minutes" in part
        assert "ignore" in part.lower()
        assert SECRET_PASSWORD not in part
    assert "My Health Records" in plain and "<html" in html
    assert live_smtp.init_args[0] == ("smtp.gmail.com", 587, 15.0)  # timeout is set


def test_mailer_retries_once_then_succeeds(live_smtp):
    live_smtp.fail_connects = 1
    assert mailer.send_otp_email("a@example.test", "111222", "login").delivered
    assert len(live_smtp.init_args) == 2 and len(live_smtp.sent) == 1


def test_mailer_gives_clear_error_after_second_failure(live_smtp):
    live_smtp.fail_connects = 5
    with pytest.raises(mailer.MailError) as e:
        mailer.send_otp_email("a@example.test", "111222", "login")
    assert len(live_smtp.init_args) == 2
    assert "111222" not in str(e.value) and SECRET_PASSWORD not in str(e.value) and "email server" in str(e.value)


def test_mailer_auth_failure_message_does_not_leak_password(live_smtp):
    live_smtp.auth_error = True
    with pytest.raises(mailer.MailError) as e:
        mailer.send_otp_email("a@example.test", "111222", "login")
    assert SECRET_PASSWORD not in str(e.value) and "App Password" in str(e.value)
    assert len(live_smtp.init_args) == 1  # no retry on bad credentials


def test_login_returns_503_if_email_cannot_be_sent(live_smtp):
    live_smtp.fail_connects = 10
    known = s.make_patient()
    r = s.client().post("/api/auth/login", json={"email": known, "password": PW})
    assert r.status_code == 503 and "otp_challenge_id" not in r.json()
    live_smtp.fail_connects = 0
    assert s.client().post("/api/auth/login", json={"email": known, "password": PW}).status_code == 200  # no cooldown penalty


def test_mailer_never_sends_to_reserved_domains(strict, monkeypatch):
    monkeypatch.setenv("SMTP_USER", "sender@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", SECRET_PASSWORD)
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("SMTP must not be contacted"))
    for addr in ("a@example.test", "b@x.test", "c@foo.invalid", "d@demo.example", "e@example.com",
                 "f@riverside.demo.test", "g@sub.example.org"):
        assert mailer.is_reserved_recipient(addr)
        assert mailer.send_otp_email(addr, "123456", "login").mode == "skipped"
        assert mailer.send_notice_email(addr, "s", "h", ["x"]).mode == "skipped"
    for addr in ("someone@gmail.com", "nurse@hospital.org", "x@testing.com"):
        assert not mailer.is_reserved_recipient(addr)


def test_mail_disabled_kill_switch(strict, monkeypatch, capsys):
    monkeypatch.setenv("SMTP_USER", "sender@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", SECRET_PASSWORD)
    monkeypatch.setenv("MAIL_DISABLED", "1")
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("SMTP must not be contacted"))
    assert mailer.smtp_configured() is False
    assert mailer.send_otp_email("someone@gmail.com", "123456", "login").mode == "dev"


def test_no_secrets_committed_in_repo_files():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    ignore = (root / ".gitignore").read_text().splitlines()
    assert ".env" in [x.strip() for x in ignore]
    example = root / ".env.example"
    if example.exists():
        for line in example.read_text().splitlines():
            if line.startswith("SMTP_PASSWORD="):
                assert line.split("=", 1)[1].strip() == ""
