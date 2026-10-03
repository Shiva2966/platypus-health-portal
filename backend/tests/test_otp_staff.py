"""Staff email + password + OTP flows, and patient/staff session separation."""
from sqlalchemy import select

import otp_support as s
from otp_support import PW, outbox, strict  # noqa: F401  (fixtures)
from app.models.staff import StaffUser

P = "/api/staff/auth"


def staff_signin(c, ob, addr, password=PW, path=f"{P}/login", trust=False):
    r = c.post(path, json={"email": addr, "password": password})
    assert r.status_code == 200, r.text
    if r.json().get("otp_required"):
        r = c.post(f"{P}/verify-login-otp", json={"otp_challenge_id": r.json()["otp_challenge_id"],
                                                  "code": ob.last(addr, "login"), "trust_device": trust})
        assert r.status_code == 200, r.text
    return r


def test_staff_login_requires_otp(outbox):
    addr = s.make_staff()
    c = s.client()
    r = c.post(f"{P}/login", json={"email": addr, "password": PW})
    assert r.json()["otp_required"] is True and "hp_staff" not in c.cookies
    assert c.get("/api/staff/me").status_code == 401
    assert c.post(f"{P}/verify-login-otp", json={"otp_challenge_id": r.json()["otp_challenge_id"], "code": "000000"}).status_code == 400
    ok = c.post(f"{P}/verify-login-otp", json={"otp_challenge_id": r.json()["otp_challenge_id"], "code": outbox.last(addr, "login")})
    assert ok.status_code == 200 and ok.json()["role"] == "nurse"
    assert c.get("/api/staff/me").json()["email"] == addr


def test_legacy_staff_login_route_is_otp_protected(outbox):
    addr = s.make_staff()
    c = s.client()
    r = c.post("/api/staff/login", json={"email": addr, "password": PW})
    assert r.status_code == 200 and r.json()["otp_required"] is True and "hp_staff" not in c.cookies
    assert c.get("/api/staff/me").status_code == 401
    staff_signin(c, outbox, addr, path="/api/staff/login")
    assert c.get("/api/staff/me").status_code == 200


def test_inactive_staff_cannot_sign_in(outbox):
    addr = s.make_staff(active=False)
    r = s.client().post(f"{P}/login", json={"email": addr, "password": PW})
    assert r.status_code == 401 and not outbox.otps


def test_staff_trusted_device(outbox):
    addr = s.make_staff()
    c = s.client()
    staff_signin(c, outbox, addr, trust=True)
    c.post(f"{P}/logout")
    assert c.get("/api/staff/me").status_code == 401
    assert c.post(f"{P}/login", json={"email": addr, "password": PW}).json()["otp_required"] is False
    assert c.get("/api/staff/me").status_code == 200


def test_staff_reset_password(outbox):
    addr = s.make_staff()
    c = s.client()
    assert c.post(f"{P}/forgot-password", json={"email": addr}).status_code == 200
    assert c.post(f"{P}/reset-password", json={"email": addr, "code": outbox.last(addr, "reset"), "new_password": s.NEW_PW}).status_code == 200
    assert c.post(f"{P}/login", json={"email": addr, "password": PW}).status_code == 401
    staff_signin(c, outbox, addr, password=s.NEW_PW)
    assert c.get("/api/staff/me").status_code == 200


def test_staff_signup_needs_invite_when_configured(outbox, strict):
    strict.setenv("STAFF_INVITE_CODE", "HOSPITAL-INVITE-7")
    c, addr = s.client(), s.email()
    body = {"email": addr, "password": PW, "name": "Nina Nurse"}
    assert c.post(f"{P}/signup", json=body).status_code == 403
    assert c.post(f"{P}/signup", json={**body, "invite_code": "wrong"}).status_code == 403
    assert not outbox.otps
    assert c.post(f"{P}/signup", json={**body, "invite_code": "HOSPITAL-INVITE-7"}).status_code == 200
    r = c.post(f"{P}/verify-email", json={"email": addr, "code": outbox.last(addr, "signup")})
    assert r.status_code == 200 and r.json()["role"] == "front_desk"  # self sign-up never gets a clinical/admin role
    assert c.get("/api/staff/me").status_code == 200
    with s.db() as d:
        assert d.scalar(select(StaffUser).where(StaffUser.email == addr)).role == "front_desk"


def test_invite_code_ignores_case_spaces_and_dashes(outbox, strict):
    strict.setenv("STAFF_INVITE_CODE", "HOSPITAL-INVITE-7")
    body = {"password": PW, "name": "Nina Nurse"}
    for typed in ("hospital-invite-7", " Hospital Invite 7 ", "HOSPITALINVITE7"):
        assert s.client().post(f"{P}/signup", json={**body, "email": s.email(), "invite_code": typed}).status_code == 200, typed
    assert s.client().post(f"{P}/signup", json={**body, "email": s.email(), "invite_code": "HOSPITAL-INVITE-8"}).status_code == 403


def test_staff_signup_closed_when_email_is_live_without_invite(outbox, strict):
    strict.setenv("SMTP_USER", "x@example.test")
    strict.setenv("SMTP_PASSWORD", "dummy-not-a-real-secret")
    r = s.client().post(f"{P}/signup", json={"email": s.email(), "password": PW, "name": "X Y"})
    assert r.status_code == 403


def test_staff_signup_dev_mode_flow(outbox):
    c, addr = s.client(), s.email()
    assert c.post(f"{P}/signup", json={"email": addr, "password": PW, "name": "Dev Staff"}).status_code == 200
    assert c.get("/api/staff/me").status_code == 401
    assert c.post(f"{P}/verify-email", json={"email": addr, "code": outbox.last(addr, "signup")}).status_code == 200
    assert c.get("/api/staff/me").status_code == 200


# ------------------------------------------------ separation of patient and staff
def test_patient_session_cannot_be_used_as_staff(outbox):
    c = s.client()
    s.signin_patient(c, outbox, s.make_patient())
    assert c.get("/api/auth/me").status_code == 200
    assert c.get("/api/staff/me").status_code == 401
    assert c.get(f"{P}/sessions").status_code == 401
    # even a hand-copied patient cookie value in the staff cookie slot is useless
    c2 = s.client()
    c2.cookies.set("hp_staff", c.cookies["hp_patient"])
    assert c2.get("/api/staff/me").status_code == 401


def test_staff_session_cannot_be_used_as_patient(outbox):
    c = s.client()
    staff_signin(c, outbox, s.make_staff())
    assert c.get("/api/staff/me").status_code == 200
    assert c.get("/api/auth/me").status_code == 401
    assert c.get("/api/auth/sessions").status_code == 401
    c2 = s.client()
    c2.cookies.set("hp_patient", c.cookies["hp_staff"])
    assert c2.get("/api/auth/me").status_code == 401


def test_patient_credentials_do_not_work_on_staff_login_and_vice_versa(outbox):
    p, st = s.make_patient(), s.make_staff()
    c = s.client()
    assert c.post(f"{P}/login", json={"email": p, "password": PW}).status_code == 401
    assert c.post("/api/auth/login", json={"email": st, "password": PW}).status_code == 401
    # a login challenge issued for a patient can't be redeemed on the staff endpoint
    chal = c.post("/api/auth/login", json={"email": p, "password": PW}).json()["otp_challenge_id"]
    assert c.post(f"{P}/verify-login-otp", json={"otp_challenge_id": chal, "code": outbox.last(p, "login")}).status_code == 400
    assert c.get("/api/staff/me").status_code == 401


def test_staff_sessions_listing(outbox):
    addr = s.make_staff()
    a, b = s.client(), s.client()
    staff_signin(a, outbox, addr)
    staff_signin(b, outbox, addr)
    assert len(a.get(f"{P}/sessions").json()["items"]) == 2
    assert a.post(f"{P}/sessions/revoke-all", json={}).json()["revoked"] == 1
    assert b.get("/api/staff/me").status_code == 401 and a.get("/api/staff/me").status_code == 200
