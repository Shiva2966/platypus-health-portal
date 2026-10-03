"""QA regression tests: emergency contacts through the REAL sign-in path (email + OTP session cookie),
exactly the JSON the browser form (core_profile.js / Portal.openForm) sends, plus the UI wiring that
makes the OTP screens appear. Owner of the code under test: W1 (contacts) / W6 (OTP)."""
from pathlib import Path

import otp_support as s
from otp_support import PW, outbox, strict  # noqa: F401  (fixtures)

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def form_payload(**kw):
    """What Portal.openForm().values() posts: blank text -> null, checkboxes -> true/false."""
    base = {"name": "Maria Lopez", "relationship": "mother", "phone": "(555) 010-1234", "email": None,
            "is_primary": False, "authorized_to_access_records": False, "notes": None}
    base.update(kw)
    return base


def signed_in(outbox):  # noqa: F811
    addr = s.make_patient()
    c = s.client()
    s.signin_patient(c, outbox, addr)  # password -> emailed OTP -> session cookie
    assert "hp_patient" in c.cookies
    return c


def listing(c):
    r = c.get("/api/emergency-contacts")
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_add_edit_primary_delete_via_ui_payloads(outbox):  # noqa: F811
    c = signed_in(outbox)
    # --- add (first contact auto-primary) ---
    r = c.post("/api/emergency-contacts", json=form_payload())
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["is_primary"] is True and a["authorized_to_access_records"] is False
    r = c.post("/api/emergency-contacts", json=form_payload(name="Carlos Ruiz", relationship="friend", phone="555-010-9999"))
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["is_primary"] is False

    # --- edit (full form payload, as the Edit dialog sends it) ---
    r = c.put(f"/api/emergency-contacts/{b['id']}", json=form_payload(name="Carlos R.", relationship="friend", phone="555-010-9999",
                                                                       email="carlos@example.test", notes="neighbour"))
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Carlos R." and r.json()["email"] == "carlos@example.test"
    assert {i["name"] for i in listing(c)} == {"Maria Lopez", "Carlos R."}  # persisted (committed)

    # --- "Make primary" button: one primary at a time ---
    r = c.post(f"/api/emergency-contacts/{b['id']}/primary")
    assert r.status_code == 200 and r.json()["is_primary"] is True
    prim = {i["name"]: i["is_primary"] for i in listing(c)}
    assert prim == {"Maria Lopez": False, "Carlos R.": True}

    # --- primary toggle through the edit dialog checkbox ---
    r = c.put(f"/api/emergency-contacts/{a['id']}", json=form_payload(is_primary=True))
    assert r.status_code == 200
    assert {i["name"]: i["is_primary"] for i in listing(c)} == {"Maria Lopez": True, "Carlos R.": False}

    # --- delete the primary: another contact is promoted, nothing is left primary-less ---
    assert c.delete(f"/api/emergency-contacts/{a['id']}").status_code == 200
    items = listing(c)
    assert [i["name"] for i in items] == ["Carlos R."] and items[0]["is_primary"] is True
    assert c.delete(f"/api/emergency-contacts/{b['id']}").status_code == 200
    assert listing(c) == []
    assert c.delete(f"/api/emergency-contacts/{b['id']}").status_code == 404  # already gone -> clear 404


def test_partial_update_keeps_other_fields(outbox):  # noqa: F811
    """Regression: PUT/PATCH with only some keys used to 422 (name required) or silently reset the flags."""
    c = signed_in(outbox)
    a = c.post("/api/emergency-contacts", json=form_payload(authorized_to_access_records=True)).json()
    assert a["is_primary"] and a["authorized_to_access_records"]
    r = c.put(f"/api/emergency-contacts/{a['id']}", json={"phone": "555-0199"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["phone"] == "555-0199" and got["name"] == "Maria Lopez"
    assert got["is_primary"] is True and got["authorized_to_access_records"] is True  # not silently cleared
    r = c.patch(f"/api/emergency-contacts/{a['id']}", json={"notes": "call after 6pm"})
    assert r.status_code == 200 and r.json()["notes"] == "call after 6pm" and r.json()["is_primary"] is True


def test_emergency_contact_is_not_record_access(outbox):  # noqa: F811
    """'Authorized to see records' is only the patient's wish: no share grant / caregiver is created by it."""
    c = signed_in(outbox)
    a = c.post("/api/emergency-contacts", json=form_payload(authorized_to_access_records=True)).json()
    b = c.post("/api/emergency-contacts", json=form_payload(name="Sam Roe", phone="555-0123")).json()
    assert a["authorized_to_access_records"] is True and b["authorized_to_access_records"] is False
    grants = c.get("/api/sharing/grants")
    if grants.status_code == 200:
        body = grants.json()
        assert (body if isinstance(body, list) else body.get("items", body.get("grants", []))) == []
    # flipping one flag never touches the other
    r = c.put(f"/api/emergency-contacts/{b['id']}", json={"is_primary": True})
    assert r.json()["is_primary"] is True and r.json()["authorized_to_access_records"] is False
    assert {i["name"]: i["authorized_to_access_records"] for i in listing(c)} == {"Maria Lopez": True, "Sam Roe": False}


def test_validation_errors_are_field_level_and_other_patients_are_isolated(outbox):  # noqa: F811
    c1, c2 = signed_in(outbox), signed_in(outbox)
    r = c1.post("/api/emergency-contacts", json=form_payload(phone="call me maybe"))
    assert r.status_code == 422 and "phone" in r.json()["fields"]
    mine = c1.post("/api/emergency-contacts", json=form_payload()).json()
    assert c1.post("/api/emergency-contacts", json=form_payload()).status_code == 409  # duplicate name+phone
    for call in (lambda: c2.put(f"/api/emergency-contacts/{mine['id']}", json=form_payload(name="Hacked")),
                 lambda: c2.post(f"/api/emergency-contacts/{mine['id']}/primary"),
                 lambda: c2.delete(f"/api/emergency-contacts/{mine['id']}")):
        assert call().status_code == 404
    assert listing(c1)[0]["name"] == "Maria Lopez"


def test_requires_session(strict):  # noqa: F811
    c = s.client()
    assert c.get("/api/emergency-contacts").status_code == 401
    assert c.post("/api/emergency-contacts/x/primary").status_code == 401


# ---------------- OTP screens are actually wired into the page the browser loads ----------------

def test_index_loads_otp_auth_scripts_after_shell():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for needle in ("/static/js/shell.js", "/static/js/auth_flow.js", "/static/js/auth_patient.js", "auth_otp.css"):
        assert needle in html, needle
    assert html.index("shell.js") < html.index("auth_flow.js") < html.index("auth_patient.js")
    assert "Portal.authRenderer" in (STATIC / "js" / "auth_patient.js").read_text(encoding="utf-8")
    assert "authRenderer" in (STATIC / "js" / "shell.js").read_text(encoding="utf-8")


def test_signup_and_login_demand_a_code_by_default(outbox):  # noqa: F811
    c = s.client()
    cfg = c.get("/api/auth/config").json()
    assert cfg["signup_verify_required"] is True and cfg["login_otp_required"] is True
    addr = s.email()
    r = c.post("/api/auth/register", json={"legal_name": "Otp Wired", "dob": "1990-01-01", "email": addr, "password": PW})
    assert r.status_code == 200 and r.json()["verification_required"] is True
    assert "hp_patient" not in c.cookies  # no session before the code
    code = outbox.last(addr, "signup")
    r = c.post("/api/auth/verify-email", json={"email": addr, "code": code})
    assert r.status_code == 200 and "hp_patient" in c.cookies
    c2 = s.client()  # new device: password alone must NOT sign in
    r = c2.post("/api/auth/login", json={"email": addr, "password": PW})
    assert r.status_code == 200 and r.json()["otp_required"] is True and "hp_patient" not in c2.cookies
    assert c2.get("/api/emergency-contacts").status_code == 401
