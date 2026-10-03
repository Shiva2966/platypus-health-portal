"""W8: billing is PRIVATE to the patient by default; staff see it only with an active `billing` grant."""
from datetime import timedelta

import pytest

try:
    from billing_support import bill_payload, claim_payload, plan_payload, world  # noqa: F401
except ImportError:
    from tests.billing_support import bill_payload, claim_payload, plan_payload, world  # noqa: F401

from fastapi.testclient import TestClient

from app.main import app
from app.models.documents import ShareGrant
from app.models.shared import utcnow
from app.services import billing_read


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


def populate(world, patient, confirm=True):
    if confirm:
        world.confirm(patient)
    c = world.client(patient)
    ok(c.post("/api/billing/plans", json=plan_payload()), 201)
    b = ok(c.post("/api/billing/bills", json=bill_payload(notes="PRIVATE NOTE about my budget")), 201)
    ok(c.post("/api/billing/claims", json=claim_payload(notes="private claim note")), 201)
    ok(c.post(f"/api/billing/bills/{b['id']}/dispute", json={"reason_code": "other", "message": "private dispute text"}), 201)
    return b


def test_staff_gets_nothing_by_default(world):
    populate(world, world.A)
    s = world.staff_client(world.staff)
    r = s.get(f"/api/staff/patients/{world.A.id}/billing")
    assert r.status_code == 403 and "No shared billing records" in r.json()["detail"]
    # the denied attempt is audited, but the patient is NOT told "viewed"
    assert world.audit(world.A, "billing_access_denied")
    assert not world.notifications(world.A, "document_viewed")
    with pytest.raises(PermissionError):
        billing_read.get_billing_for_staff(world.db, staff_id=world.staff.id, patient_id=world.A.id)


def test_dob_confirmation_is_required_even_with_a_grant(world):
    populate(world, world.A, confirm=False)
    world.grant_billing(world.A, world.prov)
    s = world.staff_client(world.staff)
    r = s.get(f"/api/staff/patients/{world.A.id}/billing")
    assert r.status_code == 403 and "date of birth" in r.json()["detail"]
    assert not world.notifications(world.A, "document_viewed")
    world.confirm(world.A)
    assert s.get(f"/api/staff/patients/{world.A.id}/billing").status_code == 200


def test_other_grants_do_not_unlock_billing(world):
    populate(world, world.A)
    # document-category grants (even 'insurance'), and document-scope grants, are NOT billing consent
    for cat in ("insurance", "lab_result", "other"):
        world.db.add(ShareGrant(patient_id=world.A.id, provider_id=world.prov.id, scope_type="category", category=cat,
                                expires_at=utcnow() + timedelta(days=3), include_private=True))
    world.db.commit()
    assert not billing_read.staff_has_billing_grant(world.db, world.staff.id, world.A.id)
    assert world.staff_client(world.staff).get(f"/api/staff/patients/{world.A.id}/billing").status_code == 403


def test_billing_grant_unlocks_only_that_patient_and_that_org(world):
    populate(world, world.A)
    populate(world, world.B)
    world.grant_billing(world.A, world.prov)
    s = world.staff_client(world.staff)
    data = ok(s.get(f"/api/staff/patients/{world.A.id}/billing"))
    assert len(data["bills"]) == 1 and len(data["plans"]) == 1 and data["patient_id"] == world.A.id
    # not another patient who shared nothing
    assert s.get(f"/api/staff/patients/{world.B.id}/billing").status_code == 403
    # not another organization's staff
    assert world.staff_client(world.other_staff).get(f"/api/staff/patients/{world.A.id}/billing").status_code == 403
    # patient was told, and the view is audited
    assert world.notifications(world.A, "document_viewed")
    assert world.audit(world.A, "billing_viewed")


def test_staff_view_strips_patient_private_text(world):
    populate(world, world.A)
    world.grant_billing(world.A, world.prov)
    txt = world.staff_client(world.staff).get(f"/api/staff/patients/{world.A.id}/billing").text
    for secret in ("PRIVATE NOTE", "private claim note", "private dispute text"):
        assert secret not in txt
    # the patient's own view still has them
    mine = world.client(world.A).get("/api/billing/export").text
    assert "PRIVATE NOTE" in mine and "private dispute text" in mine
    # the post-authorization helper keeps everything unless asked
    assert "PRIVATE NOTE" in str(billing_read.get_billing_for_patient(world.db, world.A.id))
    assert "PRIVATE NOTE" not in str(billing_read.get_billing_for_patient(world.db, world.A.id, for_staff=True))


def test_staff_view_uses_neutral_plan_label(world):
    populate(world, world.A)
    mine = billing_read.get_billing_for_patient(world.db, world.A.id)
    staff = billing_read.get_billing_for_patient(world.db, world.A.id, for_staff=True)
    unverified = [p for p in mine["plans"] if p["verification_status"] not in ("verified", "failed")]
    assert unverified and "by you" in unverified[0]["source_label"]
    assert "by you" not in str(staff).lower()
    assert any(p["source_label"] == "Patient-entered - not verified" for p in staff["plans"])


def test_expired_and_revoked_grants_lose_access(world):
    populate(world, world.A)
    g = world.grant_billing(world.A, world.prov)
    s = world.staff_client(world.staff)
    assert s.get(f"/api/staff/patients/{world.A.id}/billing").status_code == 200
    g.revoked_at = utcnow()
    world.db.commit()
    assert s.get(f"/api/staff/patients/{world.A.id}/billing").status_code == 403
    g.revoked_at = None
    g.expires_at = utcnow() - timedelta(minutes=1)
    world.db.commit()
    assert s.get(f"/api/staff/patients/{world.A.id}/billing").status_code == 403


def test_privacy_endpoint_reports_sharing_state(world):
    c = world.client(world.A)
    p = ok(c.get("/api/billing/privacy"))
    assert p["private_by_default"] is True and p["shared"] is False and p["shared_with"] == []
    world.grant_billing(world.A, world.prov)
    p = ok(c.get("/api/billing/privacy"))
    assert p["shared"] is True and p["shared_with"][0]["provider_id"] == world.prov.id


def test_sessions_do_not_cross_over(world):
    populate(world, world.A)
    world.grant_billing(world.A, world.prov)
    # a staff cookie cannot call patient billing routes; a patient cookie cannot call the staff route
    staff_c = world.staff_client(world.staff)
    assert staff_c.get("/api/billing/bills").status_code == 401
    assert staff_c.get("/api/billing/export").status_code == 401
    patient_c = world.client(world.A)
    assert patient_c.get(f"/api/staff/patients/{world.A.id}/billing").status_code == 401
    assert TestClient(app).get(f"/api/staff/patients/{world.A.id}/billing").status_code == 401


def test_front_desk_and_physician_roles_ok_admin_not(world):
    from app.models.staff import StaffUser
    import uuid
    populate(world, world.A)
    world.grant_billing(world.A, world.prov)
    admin = StaffUser(name="Adam Admin", email=f"adm-{uuid.uuid4().hex[:6]}@example.test", password_hash="x",
                      role="admin", provider_id=world.prov.id)
    world.db.add(admin)
    world.db.commit()
    assert world.staff_client(admin).get(f"/api/staff/patients/{world.A.id}/billing").status_code == 403
    assert world.staff_client(world.staff).get(f"/api/staff/patients/{world.A.id}/billing").status_code == 200


def test_billing_is_not_in_document_listings(world):
    """Billing rows are never exposed through the generic document/consent surface."""
    from app.services import consent
    populate(world, world.A)
    world.grant_billing(world.A, world.prov)
    docs = consent.list_visible_documents(world.db, staff_id=world.staff.id, staff_role="nurse", patient_id=world.A.id)
    assert docs == []
