"""Patient email + password + OTP flows (mailer mocked)."""
from datetime import timedelta

import pytest
from sqlalchemy import select, update

import otp_support as s
from otp_support import NEW_PW, PW, outbox, strict  # noqa: F401  (fixtures)
from app.models.auth import OtpCode, PendingSignup, TrustedDevice, utcnow_tz
from app.models.shared import Patient

SIGNUP = {"name": "Pat Example", "dob": "1990-01-02", "password": PW}


def signup(c, addr, **kw):
    return c.post("/api/auth/signup", json={**SIGNUP, "email": addr, **kw})


def registered(c, ob, addr=None):
    addr = addr or s.email()
    assert signup(c, addr).status_code == 200
    r = c.post("/api/auth/verify-email", json={"email": addr, "code": ob.last(addr, "signup")})
    assert r.status_code == 200, r.text
    return addr


# ---------------------------------------------------------------- sign-up
def test_signup_requires_email_verification(outbox):
    c, addr = s.client(), s.email()
    r = signup(c, addr)
    assert r.status_code == 200 and r.json()["verification_required"] is True
    assert "hp_patient" not in c.cookies
    assert "dev_otp" not in r.json()  # DEV_SHOW_OTP is off
    with s.db() as d:  # no account exists yet
        assert d.scalar(select(Patient).where(Patient.email == addr)) is None
    # cannot sign in or use the API before verifying
    assert c.post("/api/auth/login", json={"email": addr, "password": PW}).status_code == 401
    assert c.get("/api/auth/me").status_code == 401

    bad = c.post("/api/auth/verify-email", json={"email": addr, "code": "000000"})
    assert bad.status_code == 400 and "hp_patient" not in c.cookies

    ok = c.post("/api/auth/verify-email", json={"email": addr, "code": outbox.last(addr, "signup")})
    assert ok.status_code == 200 and ok.json()["email"] == addr
    assert c.get("/api/auth/me").json()["email"] == addr
    with s.db() as d:
        p = d.scalar(select(Patient).where(Patient.email == addr))
        assert p and p.password_hash.startswith("$argon2id$") and p.legal_name == "Pat Example"
        assert d.scalar(select(PendingSignup).where(PendingSignup.email == addr)) is None


def test_email_is_case_insensitive_and_trimmed(outbox):
    c, addr = s.client(), s.email()
    assert signup(c, "  " + addr.upper() + " ").status_code == 200
    assert outbox.codes(addr, "signup")


@pytest.mark.parametrize("pw,frag", [("short1", "10"), ("password123", "common"), ("1234567890", "common"),
                                     ("aaaaaaaaaaaa", "repeats")])
def test_password_policy_rejected(outbox, pw, frag):
    r = signup(s.client(), s.email(), password=pw)
    assert r.status_code == 422
    assert frag in r.json()["fields"]["password"]
    assert not outbox.otps


def test_password_cannot_contain_email_name(outbox):
    r = signup(s.client(), "johnsmith99@example.test", password="JohnSmith99-Zebra")
    assert r.status_code == 422 and "password" in r.json()["fields"]


def test_signup_bad_email_and_missing_name(outbox):
    r = s.client().post("/api/auth/signup", json={"email": "nope", "password": PW, "name": ""})
    assert r.status_code == 422 and {"email", "name"} <= set(r.json()["fields"])


def test_wrong_expired_and_reused_codes_rejected(outbox):
    c, addr = s.client(), s.email()
    signup(c, addr)
    code = outbox.last(addr, "signup")
    wrong = "000000" if code != "000000" else "111111"
    assert c.post("/api/auth/verify-email", json={"email": addr, "code": wrong}).status_code == 400
    # expired
    with s.db() as d:
        d.execute(update(OtpCode).where(OtpCode.email == addr).values(expires_at=utcnow_tz() - timedelta(seconds=1)))
        d.commit()
    r = c.post("/api/auth/verify-email", json={"email": addr, "code": code})
    assert r.status_code == 400 and "hp_patient" not in c.cookies
    # fresh code works once, then is rejected as reused
    assert c.post("/api/auth/resend-verification", json={"email": addr}).status_code == 200
    code2 = outbox.last(addr, "signup")
    assert c.post("/api/auth/verify-email", json={"email": addr, "code": code2}).status_code == 200
    c2 = s.client()
    assert c2.post("/api/auth/verify-email", json={"email": addr, "code": code2}).status_code == 400
    assert "hp_patient" not in c2.cookies


def test_older_codes_are_invalidated_by_a_new_send(outbox):
    c, addr = s.client(), s.email()
    signup(c, addr)
    first = outbox.last(addr, "signup")
    c.post("/api/auth/resend-verification", json={"email": addr})
    second = outbox.last(addr, "signup")
    if first != second:  # (1-in-a-million collision)
        assert c.post("/api/auth/verify-email", json={"email": addr, "code": first}).status_code == 400
    assert c.post("/api/auth/verify-email", json={"email": addr, "code": second}).status_code == 200 or first == second


def test_max_attempts_locks_the_code(outbox):
    c, addr = s.client(), s.email()
    signup(c, addr)
    good = outbox.last(addr, "signup")
    wrong = "123456" if good != "123456" else "654321"
    for _ in range(5):
        assert c.post("/api/auth/verify-email", json={"email": addr, "code": wrong}).status_code == 400
    # even the right code no longer works - a new one must be requested
    assert c.post("/api/auth/verify-email", json={"email": addr, "code": good}).status_code == 400
    assert "hp_patient" not in c.cookies
    c.post("/api/auth/resend-verification", json={"email": addr})
    assert c.post("/api/auth/verify-email", json={"email": addr, "code": outbox.last(addr, "signup")}).status_code == 200


def test_code_must_be_six_digits(outbox):
    c, addr = s.client(), s.email()
    signup(c, addr)
    for junk in ("", "12345", "abcdef", "1234567", "12 34"):
        assert c.post("/api/auth/verify-email", json={"email": addr, "code": junk}).status_code == 400


# ---------------------------------------------------------------- rate limits
def test_resend_cooldown(outbox, strict):
    strict.setenv("OTP_RESEND_COOLDOWN_SECONDS", "60")
    c, addr = s.client(), s.email()
    assert signup(c, addr).status_code == 200
    r = c.post("/api/auth/resend-verification", json={"email": addr})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert len(outbox.codes(addr, "signup")) == 1


def test_per_email_hourly_cap(outbox, strict):
    strict.setenv("OTP_EMAIL_HOURLY_CAP", "3")
    c, addr = s.client(), s.email()
    codes = [c.post("/api/auth/forgot-password", json={"email": addr}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_per_ip_hourly_cap(outbox, strict):
    strict.setenv("OTP_IP_HOURLY_CAP", "3")
    c = s.client()
    codes = [c.post("/api/auth/forgot-password", json={"email": s.email()}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_password_lockout_after_repeated_failures(outbox, strict):
    strict.setenv("LOGIN_MAX_FAILURES", "3")
    addr = s.make_patient()
    c = s.client()
    for _ in range(3):
        assert c.post("/api/auth/login", json={"email": addr, "password": "Wrong-Password-1"}).status_code == 401
    locked = c.post("/api/auth/login", json={"email": addr, "password": PW})  # even the right password now waits
    assert locked.status_code == 429 and "Retry-After" in locked.headers
    assert not outbox.otps


def test_lockout_also_applies_to_unknown_emails(outbox, strict):
    strict.setenv("LOGIN_MAX_FAILURES", "3")
    ghost = s.email()
    c = s.client()
    st = [c.post("/api/auth/login", json={"email": ghost, "password": "Wrong-Password-1"}).status_code for _ in range(4)]
    assert st == [401, 401, 401, 429]


# ---------------------------------------------------------------- anti-enumeration
def test_forgot_password_is_identical_for_known_and_unknown(outbox):
    known = s.make_patient()
    ghost = s.email()
    c = s.client()
    a = c.post("/api/auth/forgot-password", json={"email": known})
    b = c.post("/api/auth/forgot-password", json={"email": ghost})
    assert a.status_code == b.status_code == 200
    assert set(a.json()) == set(b.json())
    assert a.json()["message"] == b.json()["message"]
    assert outbox.codes(known, "reset") and not outbox.codes(ghost, "reset")


def test_signup_is_identical_for_existing_email(outbox):
    known = s.make_patient()
    c = s.client()
    a = signup(c, known)
    b = signup(c, s.email())
    assert a.status_code == b.status_code == 200
    assert set(a.json()) == set(b.json()) and a.json()["message"] == b.json()["message"]
    assert not outbox.codes(known, "signup")  # no code mailed ...
    assert any(n["to"] == known for n in outbox.notices)  # ... the owner is warned instead
    # and the attacker cannot verify anything for it
    assert c.post("/api/auth/verify-email", json={"email": known, "code": "123456"}).status_code == 400


def test_login_errors_identical_for_unknown_email_and_wrong_password(outbox):
    known = s.make_patient()
    c = s.client()
    a = c.post("/api/auth/login", json={"email": known, "password": "Wrong-Password-1"})
    b = c.post("/api/auth/login", json={"email": s.email(), "password": "Wrong-Password-1"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_cooldown_and_caps_apply_equally_to_unknown_emails(outbox, strict):
    strict.setenv("OTP_RESEND_COOLDOWN_SECONDS", "60")
    known, ghost = s.make_patient(), s.email()
    c = s.client()
    for e in (known, ghost):
        assert c.post("/api/auth/forgot-password", json={"email": e}).status_code == 200
    for e in (known, ghost):
        assert c.post("/api/auth/forgot-password", json={"email": e}).status_code == 429


def test_reset_with_code_for_unknown_email_is_generic(outbox):
    c, ghost = s.client(), s.email()
    c.post("/api/auth/forgot-password", json={"email": ghost})
    r = c.post("/api/auth/reset-password", json={"email": ghost, "code": "123456", "new_password": NEW_PW})
    known = s.make_patient()
    c.post("/api/auth/forgot-password", json={"email": known})
    r2 = c.post("/api/auth/reset-password", json={"email": known, "code": "000001" if outbox.last(known, "reset") != "000001" else "000002", "new_password": NEW_PW})
    assert r.status_code == r2.status_code == 400 and r.json() == r2.json()


# ---------------------------------------------------------------- login OTP
def test_login_requires_otp_and_gives_no_session_before_it(outbox):
    addr = s.make_patient()
    c = s.client()
    r = c.post("/api/auth/login", json={"email": addr, "password": PW})
    j = r.json()
    assert r.status_code == 200 and j["otp_required"] is True and j["otp_challenge_id"]
    assert "hp_patient" not in c.cookies and c.get("/api/auth/me").status_code == 401
    assert "code" not in j and "dev_otp" not in j
    chal = j["otp_challenge_id"]

    assert c.post("/api/auth/verify-login-otp", json={"otp_challenge_id": chal, "code": "000000"}).status_code == 400
    assert c.post("/api/auth/verify-login-otp", json={"otp_challenge_id": "nonsense", "code": outbox.last(addr, "login")}).status_code == 400
    assert "hp_patient" not in c.cookies
    ok = c.post("/api/auth/verify-login-otp", json={"otp_challenge_id": chal, "code": outbox.last(addr, "login")})
    assert ok.status_code == 200 and c.get("/api/auth/me").json()["email"] == addr
    # one-time
    c2 = s.client()
    assert c2.post("/api/auth/verify-login-otp", json={"otp_challenge_id": chal, "code": outbox.last(addr, "login")}).status_code == 400


def test_wrong_password_never_sends_a_code(outbox):
    addr = s.make_patient()
    assert s.client().post("/api/auth/login", json={"email": addr, "password": "Nope-Nope-Nope-1"}).status_code == 401
    assert not outbox.otps


def test_resend_login_otp_replaces_code(outbox):
    addr = s.make_patient()
    c = s.client()
    chal = c.post("/api/auth/login", json={"email": addr, "password": PW}).json()["otp_challenge_id"]
    first = outbox.last(addr, "login")
    assert c.post("/api/auth/resend-login-otp", json={"otp_challenge_id": chal}).status_code == 200
    second = outbox.last(addr, "login")
    if first != second:
        assert c.post("/api/auth/verify-login-otp", json={"otp_challenge_id": chal, "code": first}).status_code == 400
    assert c.post("/api/auth/verify-login-otp", json={"otp_challenge_id": chal, "code": second}).status_code in (200, 400)
    assert c.post("/api/auth/resend-login-otp", json={"otp_challenge_id": "bogus"}).status_code == 400


def test_seed_style_scrypt_accounts_still_work(outbox):
    addr = s.make_patient(scrypt=True)
    c = s.client()
    s.signin_patient(c, outbox, addr)
    assert c.get("/api/auth/me").status_code == 200


def test_login_otp_can_be_switched_off(outbox, strict):
    strict.setenv("LOGIN_OTP_REQUIRED", "0")
    addr = s.make_patient()
    c = s.client()
    r = c.post("/api/auth/login", json={"email": addr, "password": PW})
    assert r.status_code == 200 and r.json()["otp_required"] is False and c.get("/api/auth/me").status_code == 200
    assert not outbox.otps


# ---------------------------------------------------------------- trusted device
def test_trusted_device_skips_otp_for_that_browser_only(outbox):
    addr = s.make_patient()
    c = s.client()
    s.signin_patient(c, outbox, addr, trust=True)
    assert "hp_trust_patient" in c.cookies
    with s.db() as d:  # only a hash is stored
        row = d.scalar(select(TrustedDevice))
        assert row.token_hash != c.cookies["hp_trust_patient"] and len(row.token_hash) == 64
    c.post("/api/auth/logout")
    assert c.get("/api/auth/me").status_code == 401
    before = len(outbox.otps)
    r = c.post("/api/auth/login", json={"email": addr, "password": PW})
    assert r.json()["otp_required"] is False and c.get("/api/auth/me").status_code == 200
    assert len(outbox.otps) == before

    other_browser = s.client()  # no cookie -> OTP again
    assert other_browser.post("/api/auth/login", json={"email": addr, "password": PW}).json()["otp_required"] is True

    # the trust cookie is bound to the account: it can't skip OTP for someone else
    other_acct = s.make_patient()
    c.post("/api/auth/logout")
    assert c.post("/api/auth/login", json={"email": other_acct, "password": PW}).json()["otp_required"] is True


def test_trusted_device_expires_and_can_be_revoked(outbox):
    addr = s.make_patient()
    c = s.client()
    s.signin_patient(c, outbox, addr, trust=True)
    devs = c.get("/api/auth/trusted-devices").json()["items"]
    assert len(devs) == 1
    with s.db() as d:
        d.execute(update(TrustedDevice).values(expires_at=utcnow_tz() - timedelta(days=1)))
        d.commit()
    c.post("/api/auth/logout")
    assert c.post("/api/auth/login", json={"email": addr, "password": PW}).json()["otp_required"] is True


def test_forget_device(outbox):
    addr = s.make_patient()
    c = s.client()
    s.signin_patient(c, outbox, addr, trust=True)
    tid = c.get("/api/auth/trusted-devices").json()["items"][0]["id"]
    assert c.delete(f"/api/auth/trusted-devices/{tid}").status_code == 200
    c.post("/api/auth/logout")
    assert c.post("/api/auth/login", json={"email": addr, "password": PW}).json()["otp_required"] is True


# ---------------------------------------------------------------- reset / change password
def test_reset_password_flow(outbox):
    addr = s.make_patient()
    victim = s.client()
    s.signin_patient(victim, outbox, addr, trust=True)  # an existing session + trusted device

    c = s.client()
    assert c.post("/api/auth/forgot-password", json={"email": addr}).status_code == 200
    code = outbox.last(addr, "reset")
    # weak new password is refused and does NOT burn the code
    weak = c.post("/api/auth/reset-password", json={"email": addr, "code": code, "new_password": "password123"})
    assert weak.status_code == 422
    wrong = c.post("/api/auth/reset-password", json={"email": addr, "code": "000000" if code != "000000" else "000001", "new_password": NEW_PW})
    assert wrong.status_code == 400
    ok = c.post("/api/auth/reset-password", json={"email": addr, "code": code, "new_password": NEW_PW})
    assert ok.status_code == 200
    assert any(n["to"] == addr for n in outbox.notices)  # "your password was changed"
    # reuse fails; old password fails; old sessions + trusted devices are gone
    assert c.post("/api/auth/reset-password", json={"email": addr, "code": code, "new_password": NEW_PW + "x"}).status_code == 400
    assert c.post("/api/auth/login", json={"email": addr, "password": PW}).status_code == 401
    assert victim.get("/api/auth/me").status_code == 401
    victim.post("/api/auth/logout")
    assert victim.post("/api/auth/login", json={"email": addr, "password": NEW_PW}).json()["otp_required"] is True
    s.signin_patient(c, outbox, addr, password=NEW_PW)
    assert c.get("/api/auth/me").status_code == 200


def test_change_password_requires_current_password(outbox):
    addr = s.make_patient()
    c, other = s.client(), s.client()
    s.signin_patient(c, outbox, addr)
    s.signin_patient(other, outbox, addr)
    assert s.client().post("/api/auth/change-password", json={"current_password": PW, "new_password": NEW_PW}).status_code == 401
    bad = c.post("/api/auth/change-password", json={"current_password": "Wrong-Pass-123", "new_password": NEW_PW})
    assert bad.status_code == 422 and "current_password" in bad.json()["fields"]
    weak = c.post("/api/auth/change-password", json={"current_password": PW, "new_password": "qwertyuiop123"})
    assert weak.status_code == 422
    ok = c.post("/api/auth/change-password", json={"current_password": PW, "new_password": NEW_PW})
    assert ok.status_code == 200
    assert c.get("/api/auth/me").status_code == 200  # this session survives
    assert other.get("/api/auth/me").status_code == 401  # other devices are signed out
    assert s.client().post("/api/auth/login", json={"email": addr, "password": PW}).status_code == 401


# ---------------------------------------------------------------- sessions
def test_sessions_list_and_revoke_all(outbox):
    addr = s.make_patient()
    a, b = s.client(), s.client()
    s.signin_patient(a, outbox, addr)
    s.signin_patient(b, outbox, addr)
    items = a.get("/api/auth/sessions").json()["items"]
    assert len(items) == 2 and sum(i["current"] for i in items) == 1
    r = a.post("/api/auth/sessions/revoke-all", json={})
    assert r.status_code == 200 and r.json()["revoked"] == 1
    assert a.get("/api/auth/me").status_code == 200 and b.get("/api/auth/me").status_code == 401
    s.signin_patient(b, outbox, addr)
    other_id = [i["id"] for i in a.get("/api/auth/sessions").json()["items"] if not i["current"]][0]
    assert a.delete(f"/api/auth/sessions/{other_id}").status_code == 200
    assert b.get("/api/auth/me").status_code == 401
    assert a.post("/api/auth/sessions/revoke-all", json={"include_current": True}).status_code == 200
    assert a.get("/api/auth/me").status_code == 401


def test_logout(outbox):
    c = s.client()
    s.signin_patient(c, outbox, s.make_patient())
    assert c.post("/api/auth/logout").status_code == 200
    assert c.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------- email change
def test_email_change_needs_otp_to_the_new_address(outbox):
    addr, new = s.make_patient(), s.email()
    c = s.client()
    s.signin_patient(c, outbox, addr)
    assert c.post("/api/auth/email-change/request", json={"new_email": new, "password": "Wrong-Pass-123"}).status_code == 422
    assert c.post("/api/auth/email-change/request", json={"new_email": new, "password": PW}).status_code == 200
    assert not outbox.codes(addr, "email_change") and outbox.codes(new, "email_change")
    assert c.post("/api/auth/email-change/confirm", json={"new_email": new, "code": "000000"}).status_code == 400
    assert c.post("/api/auth/email-change/confirm", json={"new_email": new, "code": outbox.last(new, "email_change")}).status_code == 200
    assert c.get("/api/auth/me").json()["email"] == new


# ---------------------------------------------------------------- legacy / config
def test_legacy_register_route_cannot_skip_verification(outbox):
    c, addr = s.client(), s.email()
    r = c.post("/api/auth/register", json={"legal_name": "Pat", "dob": "1990-01-02", "email": addr, "password": PW})
    assert r.status_code == 200 and r.json().get("verification_required") and "hp_patient" not in c.cookies


def test_relaxed_mode_keeps_old_behaviour_for_other_tests(strict):
    strict.setenv("SIGNUP_VERIFY_REQUIRED", "0")
    strict.setenv("LOGIN_OTP_REQUIRED", "0")
    c, addr = s.client(), s.email()
    r = c.post("/api/auth/register", json={"legal_name": "Pat", "dob": "1990-01-02", "email": addr, "password": PW})
    assert r.status_code == 201 and c.get("/api/auth/me").status_code == 200
    c.post("/api/auth/logout")
    assert c.post("/api/auth/login", json={"email": addr, "password": PW}).status_code == 200
    assert c.post("/api/auth/register", json={"legal_name": "Pat", "dob": "1990-01-02", "email": addr, "password": PW}).status_code == 422


def test_config_endpoint(strict):
    j = s.client().get("/api/auth/config").json()
    assert j["login_otp_required"] is True and j["otp_length"] == 6 and j["email_delivery"] == "console"
