"""W8: insurance plans, claims, bills, payments/receipts, disputes, matching, duplicate prevention, patient isolation."""
import uuid
from decimal import Decimal

import pytest

try:
    from billing_support import bill_payload, claim_payload, plan_payload, world  # noqa: F401
except ImportError:
    from tests.billing_support import bill_payload, claim_payload, plan_payload, world  # noqa: F401


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


# ---------------------------------------------------------------- plans

def test_plan_crud_and_entered_vs_verified(world):
    c = world.client(world.A)
    p = ok(c.post("/api/billing/plans", json=plan_payload()), 201)
    assert p["verification_status"] == "entered" and "not verified" in p["source_label"]
    assert p["deductible_annual"] == "1500.00" and p["coinsurance_pct"] == "20.00"
    v = ok(c.post(f"/api/billing/plans/{p['id']}/verify"))
    assert v["verification_status"] == "verified" and "mock" in v["verification_note"].lower() and v["verified_at"]
    # changing coverage details drops verification back to 'entered'
    u = ok(c.put(f"/api/billing/plans/{p['id']}", json={"member_id": "NEW-99999"}))
    assert u["verification_status"] == "entered" and u["verified_at"] is None
    # changing something harmless does not
    ok(c.post(f"/api/billing/plans/{p['id']}/verify"))
    u2 = ok(c.put(f"/api/billing/plans/{p['id']}", json={"insurer_phone": "555-0000"}))
    assert u2["verification_status"] == "verified"
    assert len(ok(c.get("/api/billing/plans"))) == 1
    assert c.delete(f"/api/billing/plans/{p['id']}").status_code == 204
    assert ok(c.get("/api/billing/plans")) == []


def test_mock_verification_can_fail(world):
    c = world.client(world.A)
    p = ok(c.post("/api/billing/plans", json=plan_payload(member_id="BAD-INVALID-1")), 201)
    assert ok(c.post(f"/api/billing/plans/{p['id']}/verify"))["verification_status"] == "failed"
    p2 = ok(c.post("/api/billing/plans", json=plan_payload(coverage_rank="secondary", effective_date="2099-01-01")), 201)
    r = ok(c.post(f"/api/billing/plans/{p2['id']}/verify"))
    assert r["verification_status"] == "failed" and "not started" in r["verification_note"]


def test_multiple_plans_primary_secondary_and_overlap_rules(world):
    c = world.client(world.A)
    ok(c.post("/api/billing/plans", json=plan_payload()), 201)
    sec = ok(c.post("/api/billing/plans", json=plan_payload(insurer_name="Pinecrest Supplemental", coverage_rank="secondary",
                                                            policyholder_relationship="spouse", policyholder_name="Alex Test")), 201)
    assert sec["coverage_rank"] == "secondary" and sec["policyholder_relationship"] == "spouse"
    # second primary during the same dates -> friendly 409
    r = c.post("/api/billing/plans", json=plan_payload(insurer_name="Other Insurer"))
    assert r.status_code == 409 and "already have a primary plan" in r.json()["detail"]
    # a primary for a later, non-overlapping period is fine once the first one ends
    plans = ok(c.get("/api/billing/plans"))
    assert [p["coverage_rank"] for p in plans] == ["primary", "secondary"]
    first = plans[0]
    ok(c.put(f"/api/billing/plans/{first['id']}", json={"end_date": "2026-06-30"}))
    ok(c.post("/api/billing/plans", json=plan_payload(insurer_name="Other Insurer", effective_date="2026-07-01")), 201)


def test_duplicate_plan_prevented_case_insensitive(world):
    c = world.client(world.A)
    pl = plan_payload(member_id="ab-12345")
    ok(c.post("/api/billing/plans", json=pl), 201)
    dup = dict(pl, insurer_name="  evergreen   HEALTH plan ", member_id="AB-12345", coverage_rank="secondary")
    r = c.post("/api/billing/plans", json=dup)
    assert r.status_code == 409 and "already saved this plan" in r.json()["detail"] and r.headers.get("X-Existing-Id")
    # same member id at a different insurer is not a duplicate
    ok(c.post("/api/billing/plans", json=dict(pl, insurer_name="Different Co", coverage_rank="secondary")), 201)
    # the same plan can belong to another patient
    ok(world.client(world.B).post("/api/billing/plans", json=pl), 201)


def test_plan_validation_errors_are_field_level(world):
    c = world.client(world.A)
    r = c.post("/api/billing/plans", json=plan_payload(end_date="2025-01-01"))
    assert r.status_code == 422 and "end_date" in r.json()["fields"]
    r = c.post("/api/billing/plans", json=plan_payload(plan_type="gold"))
    assert r.status_code == 422 and "plan_type" in r.json()["fields"]
    r = c.post("/api/billing/plans", json=plan_payload(deductible_annual="-5"))
    assert r.status_code == 422 and "deductible_annual" in r.json()["fields"]
    r = c.post("/api/billing/plans", json=plan_payload(deductible_annual="10.123"))
    assert r.status_code == 422
    r = c.post("/api/billing/plans", json={"insurer_name": "X"})
    assert r.status_code == 422 and "member_id" in r.json()["fields"]


def _upload_png(client):
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
           b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")
    png += uuid.uuid4().bytes  # unique bytes -> no duplicate-file rejection
    r = client.post("/api/documents", data={"name": "Insurance card front", "category": "insurance"},
                    files={"file": ("card.png", png, "application/octet-stream")})
    return r


def test_card_image_link_must_be_own_document(world):
    a, b = world.client(world.A), world.client(world.B)
    plan = ok(a.post("/api/billing/plans", json=plan_payload()), 201)
    up = _upload_png(a)
    if up.status_code != 201:
        pytest.skip("document upload not available: " + up.text[:100])
    doc = up.json()
    linked = ok(a.post(f"/api/billing/plans/{plan['id']}/card", json={"side": "front", "document_id": doc["id"]}))
    assert linked["card_front_document_id"] == doc["id"]
    # someone else's document, a made-up id, and a bad side are all rejected
    other = _upload_png(b).json()
    r = a.post(f"/api/billing/plans/{plan['id']}/card", json={"side": "back", "document_id": other["id"]})
    assert r.status_code == 422 and "document_id" in r.json()["fields"]
    assert a.post(f"/api/billing/plans/{plan['id']}/card", json={"side": "back", "document_id": str(uuid.uuid4())}).status_code == 422
    assert a.post(f"/api/billing/plans/{plan['id']}/card", json={"side": "top", "document_id": doc["id"]}).status_code == 422
    # unlink
    assert ok(a.post(f"/api/billing/plans/{plan['id']}/card", json={"side": "front", "document_id": None}))["card_front_document_id"] is None
