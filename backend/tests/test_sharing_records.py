"""Consent for structured RECORD categories (W7 medical data + W8 billing data) - W2."""
import uuid
from datetime import timedelta

import pytest

try:
    from test_documents_support import world, uniq  # noqa: F401
except ImportError:
    from tests.test_documents_support import world, uniq  # noqa: F401

from app.db import SessionLocal
from app.models.documents import RECORD_CATEGORIES, SHARE_CATEGORIES, ShareGrant, ShareTokenAttempt
from app.models.shared import utcnow
from app.models.staff import StaffUser
from app.services import consent
from app.services.seed_billing import seed_billing
from app.services.seed_medical import seed_medical

CLINICAL = ["history", "medications", "allergies", "vaccinations", "results"]


@pytest.fixture
def rw(world):
    """world + seeded health data for patient A and B, plus one staff user per role at provider 0."""
    seed_medical(world.db, world.A.id)
    seed_medical(world.db, world.B.id)
    seed_billing(world.db, world.A.id)
    world.db.commit()
    world.roles = {}
    for role in ("front_desk", "nurse", "physician", "admin"):
        s = StaffUser(name=f"{role} test", email=f"{role}-{uuid.uuid4().hex[:8]}@example.test", password_hash="x",
                      role=role, provider_id=world.provs[0].id)
        world.db.add(s)
        world.roles[role] = s
    world.db.commit()
    return world


def grant(w, patient, provider, cats, **extra):
    r = w.client(patient).post("/api/sharing/grants", json={"provider_id": provider.id, "categories": cats, **extra})
    assert r.status_code == 201, r.text
    return r.json()["grants"]


def read(w, staff, patient, cat):
    return consent.get_records_for_staff(w.db, staff_id=staff.id, staff_role=staff.role, patient_id=patient.id,
                                         category=cat)


def listing(w, staff, patient):
    return consent.list_visible_records(w.db, staff_id=staff.id, staff_role=staff.role, patient_id=patient.id)


def test_single_category_list_covers_records(rw):
    assert set(RECORD_CATEGORIES) == set(CLINICAL + ["billing"])
    assert set(RECORD_CATEGORIES) <= set(SHARE_CATEGORIES)
    assert "lab_result" in SHARE_CATEGORIES  # document categories still valid
    assert rw.client(rw.A).get("/api/sharing/categories").json()["records"][0]["value"] == "history"


def test_no_grant_returns_nothing(rw):
    nurse = rw.roles["nurse"]
    assert listing(rw, nurse, rw.A) == {}
    assert consent.list_visible_record_categories(rw.db, staff_id=nurse.id, staff_role="nurse", patient_id=rw.A.id) == []
    for cat in RECORD_CATEGORIES:
        assert not consent.staff_can_read_category(rw.db, staff_id=nurse.id, patient_id=rw.A.id, category=cat)
        with pytest.raises(PermissionError):
            read(rw, nurse, rw.A, cat)
    assert rw.audit(rw.A.id, "record_access_denied")
    assert not rw.notifications("patient", rw.A.id, "record_viewed")


def test_unknown_category_and_unlinked_staff(rw):
    nurse = rw.roles["nurse"]
    grant(rw, rw.A, rw.provs[0], ["results"])
    with pytest.raises(PermissionError):
        read(rw, nurse, rw.A, "everything")
    with pytest.raises(PermissionError):
        read(rw, nurse, rw.A, "lab_result")  # a document category is not a record category

    class Ghost:
        id = "00000000-0000-0000-0000-000000000000"
        role = "physician"
    with pytest.raises(PermissionError):
        read(rw, Ghost, rw.A, "results")
    assert listing(rw, Ghost, rw.A) == {}


def test_category_scoping(rw):
    phys = rw.roles["physician"]
    grant(rw, rw.A, rw.provs[0], ["medications"])
    out = listing(rw, phys, rw.A)
    assert list(out) == ["medications"] and out["medications"]
    assert all(i["record_type"] for i in out["medications"])
    for cat in ("allergies", "results", "history", "vaccinations", "billing"):
        with pytest.raises(PermissionError):
            read(rw, phys, rw.A, cat)
    # another organization's staff gets nothing from this grant
    assert listing(rw, rw.staff[1], rw.A) == {}
    # allergies carry the explicit status item when granted
    grant(rw, rw.A, rw.provs[0], ["allergies"])
    assert read(rw, phys, rw.A, "allergies")[0]["record_type"] == "allergy_status"


def test_expiry_blocks(rw):
    nurse = rw.roles["nurse"]
    grant(rw, rw.A, rw.provs[0], ["results"])
    assert read(rw, nurse, rw.A, "results")
    g = rw.db.query(ShareGrant).filter_by(patient_id=rw.A.id, category="results").one()
    g.expires_at = utcnow() - timedelta(minutes=1)
    rw.db.commit()
    with pytest.raises(PermissionError):
        read(rw, nurse, rw.A, "results")
    assert listing(rw, nurse, rw.A) == {}


def test_revoke_blocks_future_access(rw):
    nurse = rw.roles["nurse"]
    g = grant(rw, rw.A, rw.provs[0], ["results", "medications"])
    assert read(rw, nurse, rw.A, "results")
    gid = next(x["id"] for x in g if x["category"] == "results")
    assert rw.client(rw.A).delete(f"/api/sharing/grants/{gid}").status_code == 200
    with pytest.raises(PermissionError):
        read(rw, nurse, rw.A, "results")
    assert read(rw, nurse, rw.A, "medications")  # the other grant is untouched
    assert rw.notifications("staff", nurse.id, "access_revoked")


@pytest.mark.parametrize("role,allowed", [
    ("front_desk", {"billing"}),
    ("nurse", set(CLINICAL) | {"billing"}),
    ("physician", set(CLINICAL) | {"billing"}),
    ("admin", set()),
])
def test_role_matrix_with_everything_granted(rw, role, allowed):
    staff = rw.roles[role]
    grant(rw, rw.A, rw.provs[0], list(RECORD_CATEGORIES))
    got = set(listing(rw, staff, rw.A))
    assert got == allowed
    for cat in RECORD_CATEGORIES:
        ok = consent.staff_can_read_category(rw.db, staff_id=staff.id, patient_id=rw.A.id, category=cat)
        assert ok == (cat in allowed)
        if cat in allowed:
            assert read(rw, staff, rw.A, cat)
        else:
            with pytest.raises(PermissionError):
                read(rw, staff, rw.A, cat)
    assert consent.staff_role_allows(role, "results") == ("results" in allowed)


def test_role_denial_is_audited_with_reason(rw):
    grant(rw, rw.A, rw.provs[0], ["results"])
    fd = rw.roles["front_desk"]
    with pytest.raises(PermissionError):
        read(rw, fd, rw.A, "results")
    ev = [a for a in rw.audit(rw.A.id, "record_access_denied") if a.actor_id == fd.id]
    assert ev and '"role"' in ev[-1].detail and "role" in ev[-1].detail
    assert not rw.notifications("patient", rw.A.id, "record_viewed")


def test_inactive_staff_denied(rw):
    nurse = rw.roles["nurse"]
    grant(rw, rw.A, rw.provs[0], ["results"])
    nurse.active = False
    rw.db.commit()
    with pytest.raises(PermissionError):
        read(rw, nurse, rw.A, "results")
    assert listing(rw, nurse, rw.A) == {}


def test_billing_private_by_default(rw):
    nurse, fd = rw.roles["nurse"], rw.roles["front_desk"]
    grant(rw, rw.A, rw.provs[0], CLINICAL)  # every clinical category, but NOT billing
    assert "billing" not in listing(rw, nurse, rw.A)
    for s in (nurse, fd, rw.roles["physician"]):
        with pytest.raises(PermissionError):
            read(rw, s, rw.A, "billing")
    # a billing DOCUMENT grant or a single document does not unlock billing data
    d = rw.upload(rw.A, name="Invoice", category="billing")
    r = rw.client(rw.A).post("/api/sharing/grants", json={"provider_id": rw.provs[0].id, "document_ids": [d["id"]]})
    assert r.status_code == 201
    with pytest.raises(PermissionError):
        read(rw, nurse, rw.A, "billing")
    # explicit billing grant unlocks it, as one structured item without private free text
    grant(rw, rw.A, rw.provs[0], ["billing"])
    items = read(rw, nurse, rw.A, "billing")
    assert len(items) == 1 and items[0]["record_type"] == "billing"
    assert {"plans", "bills", "claims"} <= set(items[0])
    assert all("notes" not in b for b in items[0]["bills"])


def test_access_request_flow_billing_opt_in(rw):
    nurse = rw.roles["nurse"]
    req = consent.create_access_request(rw.db, staff_id=nurse.id, patient_id=rw.A.id,
                                        categories=["medications", "billing"], document_ids=[],
                                        purpose="Pre-op", duration_days=3)
    assert req.status == "pending"
    c = rw.client(rw.A)
    pv = c.get(f"/api/sharing/requests/{req.id}/preview").json()
    assert {r["category"] for r in pv["records"]} == {"medications", "billing"}
    # patient ticks only medications (billing stays private)
    r = c.post(f"/api/sharing/requests/{req.id}/approve", json={"categories": ["medications"]})
    assert r.status_code == 200
    assert read(rw, nurse, rw.A, "medications")
    with pytest.raises(PermissionError):
        read(rw, nurse, rw.A, "billing")
    # cannot widen beyond the request
    req2 = consent.create_access_request(rw.db, staff_id=nurse.id, patient_id=rw.A.id, categories=["results"],
                                         document_ids=[], purpose="x", duration_days=1)
    c.post(f"/api/sharing/requests/{req2.id}/approve", json={"categories": ["results", "history"]})
    assert read(rw, nurse, rw.A, "results")
    with pytest.raises(PermissionError):
        read(rw, nurse, rw.A, "history")


def test_token_with_record_categories(rw):
    nurse = rw.roles["nurse"]
    c = rw.client(rw.A)
    assert c.post("/api/sharing/tokens", json={"categories": ["nonsense"]}).status_code == 422
    t = c.post("/api/sharing/tokens", json={"categories": ["results", "billing"]}).json()
    assert {r["category"] for r in t["preview"]["records"]} == {"results", "billing"}
    s = consent.redeem_share_token(rw.db, token=t["token"], staff_id=nurse.id)
    assert set(s["categories"]) == {"results", "billing"}
    assert read(rw, nurse, rw.A, "results") and read(rw, nurse, rw.A, "billing")
    with pytest.raises(PermissionError):
        read(rw, nurse, rw.A, "medications")


def test_audit_and_notification_per_category_viewed(rw):
    phys = rw.roles["physician"]
    grant(rw, rw.A, rw.provs[0], ["results", "medications"])
    out = listing(rw, phys, rw.A)  # two categories -> two audit rows + two notifications
    assert set(out) == {"results", "medications"}
    ev = [a for a in rw.audit(rw.A.id, "record_viewed") if a.actor_id == phys.id]
    assert {e.resource_id for e in ev} == {"results", "medications"}
    assert all(e.actor_type == "staff" and e.resource_type == "records" for e in ev)
    n = rw.notifications("patient", rw.A.id, "record_viewed")
    assert len(n) == 2 and all(rw.provs[0].name in x.title for x in n)
    assert any("test results" in x.title.lower() for x in n)
    # the peek (no data) does NOT audit or notify
    before = len(rw.audit(rw.A.id, "record_viewed"))
    consent.list_visible_record_categories(rw.db, staff_id=phys.id, staff_role="physician", patient_id=rw.A.id)
    assert len(rw.audit(rw.A.id, "record_viewed")) == before
    # shows up in the patient's access history
    hist = rw.client(rw.A).get("/api/sharing/history").json()["history"]
    assert any(h["action"] == "record_viewed" for h in hist)


def test_repeat_views_audited_but_notification_coalesced(rw):
    phys, nurse = rw.roles["physician"], rw.roles["nurse"]
    grant(rw, rw.A, rw.provs[0], ["allergies", "results"])
    for _ in range(4):
        read(rw, phys, rw.A, "allergies")
    ev = [a for a in rw.audit(rw.A.id, "record_viewed") if a.actor_id == phys.id and a.resource_id == "allergies"]
    assert len(ev) == 4  # every view is audited
    notes = rw.notifications("patient", rw.A.id, "record_viewed")
    assert len(notes) == 1
    read(rw, phys, rw.A, "results")  # different category -> new notification
    read(rw, nurse, rw.A, "allergies")  # different staff member -> new notification
    assert len(rw.notifications("patient", rw.A.id, "record_viewed")) == 3


def test_coalescing_window_can_be_disabled(rw, monkeypatch):
    monkeypatch.setenv("RECORD_VIEW_NOTIFY_WINDOW_MINUTES", "0")
    phys = rw.roles["physician"]
    grant(rw, rw.A, rw.provs[0], ["allergies"])
    read(rw, phys, rw.A, "allergies")
    read(rw, phys, rw.A, "allergies")
    assert len(rw.notifications("patient", rw.A.id, "record_viewed")) == 2


def test_cross_patient_isolation(rw):
    nurse = rw.roles["nurse"]
    grant(rw, rw.A, rw.provs[0], list(CLINICAL))
    assert read(rw, nurse, rw.A, "results")
    for cat in CLINICAL:
        with pytest.raises(PermissionError):
            read(rw, nurse, rw.B, cat)
    assert listing(rw, nurse, rw.B) == {}
    # data returned for A never contains B's rows
    ids_a = {i["id"] for i in read(rw, nurse, rw.A, "results")}
    from app.services import medical_read
    ids_b = {i["id"] for i in medical_read.get_category_for_patient(rw.db, rw.B.id, "results")}
    assert ids_a and ids_b and not ids_a & ids_b
    # patient B can neither see nor revoke A's grants
    gid = rw.client(rw.A).get("/api/sharing/grants").json()["grants"][0]["id"]
    assert rw.client(rw.B).delete(f"/api/sharing/grants/{gid}").status_code == 404
    assert rw.client(rw.B).get("/api/shares/active").json() == []


def test_preview_lists_record_categories_with_counts(rw):
    c = rw.client(rw.A)
    pv = c.post("/api/sharing/preview", json={"categories": ["medications", "allergies", "billing"]}).json()
    by = {r["category"]: r for r in pv["records"]}
    assert set(by) == {"medications", "allergies", "billing"}
    assert by["medications"]["count"] >= 1 and by["billing"]["count"] >= 1
    assert "bill" in by["billing"]["detail"]
    assert by["allergies"]["label"] == "Allergies"
    # document-only scope has no record lines
    assert c.post("/api/sharing/preview", json={"categories": ["lab_result"]}).json()["records"] == []


def test_grants_expose_record_flag_and_label(rw):
    grant(rw, rw.A, rw.provs[0], ["results", "lab_result"])
    gs = {g["category"]: g for g in rw.client(rw.A).get("/api/shares/active").json()}
    assert gs["results"]["is_record"] is True and gs["results"]["category_label"] == "Test results"
    assert gs["lab_result"]["is_record"] is False


def test_share_notifies_staff_with_readable_label(rw):
    grant(rw, rw.A, rw.provs[0], ["medications"])
    n = rw.notifications("staff", rw.roles["nurse"].id, "document_shared")
    assert n and "medications" in n[0].body.lower()


def test_token_lockout_is_stored_in_database(rw):
    consent.reset_token_attempts()
    nurse = rw.roles["nurse"]
    for _ in range(consent.TOKEN_FAIL_LIMIT):
        with pytest.raises(consent.InvalidToken):
            consent.redeem_share_token(rw.db, token="z" * 30, staff_id=nurse.id)
    other = SessionLocal()  # a brand-new session/"worker" sees the same lockout
    try:
        assert other.query(ShareTokenAttempt).filter_by(key=f"staff:{nurse.id}").count() == consent.TOKEN_FAIL_LIMIT
        with pytest.raises(consent.TooManyAttempts):
            consent.redeem_share_token(other, token="z" * 30, staff_id=nurse.id)
    finally:
        other.close()
    # window passes -> allowed to try again
    rw.db.query(ShareTokenAttempt).update({ShareTokenAttempt.ts: utcnow() - timedelta(seconds=consent.TOKEN_FAIL_WINDOW + 5)})
    rw.db.commit()
    with pytest.raises(consent.InvalidToken):
        consent.redeem_share_token(rw.db, token="z" * 30, staff_id=nurse.id)
    consent.reset_token_attempts()
