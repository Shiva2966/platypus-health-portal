"""W8: claims, bills, balances, payments/receipts, disputes, matching, duplicate prevention, isolation, seed."""
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

try:
    from billing_support import bill_payload, claim_payload, plan_payload, world  # noqa: F401
except ImportError:
    from tests.billing_support import bill_payload, claim_payload, plan_payload, world  # noqa: F401

from app.models.shared import utcnow


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


# ---------------------------------------------------------------- claims

def test_claim_crud_and_duplicate_prevention(world):
    c = world.client(world.A)
    pl = claim_payload(claim_number="clm-777")
    cl = ok(c.post("/api/billing/claims", json=pl), 201)
    assert cl["billed_amount"] == "500.00" and isinstance(cl["billed_amount"], str)  # money never float
    r = c.post("/api/billing/claims", json=dict(pl, claim_number="  CLM-777 "))
    assert r.status_code == 409 and "already saved" in r.json()["detail"] and r.headers["X-Existing-Id"] == cl["id"]
    ok(world.client(world.B).post("/api/billing/claims", json=pl), 201)  # other patient may use the same number
    upd = ok(c.put(f"/api/billing/claims/{cl['id']}", json={"status": "denied", "denial_reason": "No prior approval"}))
    assert upd["status"] == "denied"
    # renaming onto an existing claim number is a friendly conflict too
    other = ok(c.post("/api/billing/claims", json=claim_payload(claim_number="CLM-888")), 201)
    assert c.put(f"/api/billing/claims/{other['id']}", json={"claim_number": "CLM-777"}).status_code == 409
    assert c.delete(f"/api/billing/claims/{other['id']}").status_code == 204


def test_claim_validation(world):
    c = world.client(world.A)
    assert "status" in c.post("/api/billing/claims", json=claim_payload(status="maybe")).json()["fields"]
    assert c.post("/api/billing/claims", json=claim_payload(billed_amount="-1")).status_code == 422
    assert c.post("/api/billing/claims", json=claim_payload(plan_id=str(uuid.uuid4()))).status_code == 422


# ---------------------------------------------------------------- bills, balances, payments

def test_bill_duplicate_prevention(world):
    c = world.client(world.A)
    pl = bill_payload(provider_name="Dup Clinic", account_number="acc-1")
    b = ok(c.post("/api/billing/bills", json=pl), 201)
    r = c.post("/api/billing/bills", json=dict(pl, provider_name="  dup   clinic ", account_number="ACC-1"))
    assert r.status_code == 409 and "already saved" in r.json()["detail"] and r.headers["X-Existing-Id"] == b["id"]
    # different amount or date -> a different bill
    ok(c.post("/api/billing/bills", json=dict(pl, billed_amount="501.00")), 201)
    ok(c.post("/api/billing/bills", json=dict(pl, service_date="2026-08-02")), 201)
    ok(world.client(world.B).post("/api/billing/bills", json=pl), 201)  # same details, other patient
    # editing a bill into a duplicate is blocked
    third = ok(c.post("/api/billing/bills", json=dict(pl, billed_amount="777.00")), 201)
    assert c.put(f"/api/billing/bills/{third['id']}", json={"billed_amount": "500.00"}).status_code == 409


def test_balance_status_and_payments(world):
    c = world.client(world.A)
    b = ok(c.post("/api/billing/bills", json=bill_payload(amount_due="100.00")), 201)
    assert b["balance"] == "100.00" and b["status"] == "unpaid" and b["paid_total"] == "0.00"
    p1 = ok(c.post(f"/api/billing/bills/{b['id']}/payments",
                   json={"amount": "40.10", "paid_on": "2026-09-01", "method": "card", "confirmation_number": "C-1"}), 201)
    assert p1["receipt"]["balance_after_payment"] == "59.90" and p1["receipt_number"].startswith("RCP-")
    got = ok(c.get(f"/api/billing/bills/{b['id']}"))
    assert got["status"] == "partially_paid" and got["balance"] == "59.90" and len(got["payments"]) == 1
    # Decimal exactness: 0.10 + 0.20 style sums
    p2 = ok(c.post(f"/api/billing/bills/{b['id']}/payments", json={"amount": "59.90", "paid_on": "2026-09-02"}), 201)
    full = ok(c.get(f"/api/billing/bills/{b['id']}"))
    assert full["status"] == "paid" and full["balance"] == "0.00" and full["days_until_due"] is None
    assert Decimal(full["paid_total"]) == Decimal("100.00")
    # undo restores the balance and status
    assert c.delete(f"/api/billing/payments/{p2['id']}").status_code == 204
    again = ok(c.get(f"/api/billing/bills/{b['id']}"))
    assert again["status"] == "partially_paid" and again["balance"] == "59.90"


def test_payment_rules_overpay_and_duplicates(world):
    c = world.client(world.A)
    b = ok(c.post("/api/billing/bills", json=bill_payload(amount_due="100.00")), 201)
    url = f"/api/billing/bills/{b['id']}/payments"
    r = c.post(url, json={"amount": "100.01", "paid_on": "2026-09-01"})
    assert r.status_code == 422 and "amount" in r.json()["fields"] and "balance of $100.00" in r.json()["fields"]["amount"]
    assert c.post(url, json={"amount": "0", "paid_on": "2026-09-01"}).status_code == 422
    assert c.post(url, json={"amount": "-5", "paid_on": "2026-09-01"}).status_code == 422
    ok(c.post(url, json={"amount": "20.00", "paid_on": "2026-09-01", "confirmation_number": "CONF-9"}), 201)
    dup = c.post(url, json={"amount": "10.00", "paid_on": "2026-09-05", "confirmation_number": "conf-9"})
    assert dup.status_code == 201  # confirmation numbers are exact; different case = different string
    dup2 = c.post(url, json={"amount": "5.00", "paid_on": "2026-09-05", "confirmation_number": "CONF-9"})
    assert dup2.status_code == 409 and "CONF-9" in dup2.json()["detail"]
    # no confirmation number: same amount+date+method needs an explicit "separate payment" confirmation
    ok(c.post(url, json={"amount": "7.00", "paid_on": "2026-09-06"}), 201)
    again = c.post(url, json={"amount": "7.00", "paid_on": "2026-09-06"})
    assert again.status_code == 409 and again.headers.get("X-Needs-Confirmation") == "1"
    ok(c.post(url, json={"amount": "7.00", "paid_on": "2026-09-06", "separate_payment": True}), 201)


def test_receipt_contents(world):
    c = world.client(world.A)
    b = ok(c.post("/api/billing/bills", json=bill_payload(provider_name="Receipt Clinic", amount_due="80.00",
                                                          account_number="R-1")), 201)
    pay = ok(c.post(f"/api/billing/bills/{b['id']}/payments",
                    json={"amount": "30.00", "paid_on": "2026-09-01", "note": "online"}), 201)
    rc = ok(c.get(f"/api/billing/payments/{pay['id']}/receipt"))
    assert rc["provider_name"] == "Receipt Clinic" and rc["amount"] == "30.00" and rc["balance_after_payment"] == "50.00"
    assert rc["patient_name"] == "Billing Patient 0" and "not an official receipt" in rc["footer"]
    assert world.audit(world.A, "billing_payment_recorded")


def test_void_bill_blocks_payments_and_hides_from_outstanding(world):
    c = world.client(world.A)
    b = ok(c.post("/api/billing/bills", json=bill_payload(amount_due="100.00")), 201)
    v = ok(c.put(f"/api/billing/bills/{b['id']}", json={"status": "void"}))
    assert v["status"] == "void"
    assert c.post(f"/api/billing/bills/{b['id']}/payments", json={"amount": "1.00", "paid_on": "2026-09-01"}).status_code == 409
    assert c.put(f"/api/billing/bills/{b['id']}", json={"status": "paid"}).status_code == 422  # can't fake paid


def test_summary_totals(world):
    c = world.client(world.A)
    today = utcnow().date()
    ok(c.post("/api/billing/bills", json=bill_payload(amount_due="100.00", due_date=str(today - timedelta(days=3)))), 201)
    b2 = ok(c.post("/api/billing/bills", json=bill_payload(amount_due="50.25", due_date=str(today + timedelta(days=2)))), 201)
    ok(c.post(f"/api/billing/bills/{b2['id']}/payments", json={"amount": "0.25", "paid_on": str(today)}), 201)
    s = ok(c.get("/api/billing/summary"))
    assert s["total_outstanding"] == "150.00" and s["overdue_count"] == 1 and s["overdue_total"] == "100.00"
    assert s["due_soon_count"] == 1 and s["next_due"]["id"] == b2["id"] and s["private"] is True


# ---------------------------------------------------------------- disputes

def test_dispute_lifecycle_and_single_open(world):
    c = world.client(world.A)
    b = ok(c.post("/api/billing/bills", json=bill_payload(amount_due="100.00")), 201)
    d = ok(c.post(f"/api/billing/bills/{b['id']}/dispute",
                  json={"reason_code": "amount_wrong", "message": "Insurer says I owe less.", "disputed_amount": "40.00"}), 201)
    assert d["status"] == "open" and d["disputed_amount"] == "40.00"
    assert ok(c.get(f"/api/billing/bills/{b['id']}"))["status"] == "in_dispute"
    second = c.post(f"/api/billing/bills/{b['id']}/dispute", json={"reason_code": "other", "message": "Again please"})
    assert second.status_code == 409 and "already an open dispute" in second.json()["detail"]
    closed = ok(c.post(f"/api/billing/disputes/{d['id']}/close", json={"outcome": "resolved", "note": "Provider fixed it"}))
    assert closed["status"] == "resolved" and closed["closed_at"]
    assert ok(c.get(f"/api/billing/bills/{b['id']}"))["status"] == "unpaid"  # restored from payments
    assert c.post(f"/api/billing/disputes/{d['id']}/close", json={"outcome": "withdrawn"}).status_code == 409
    # a new dispute can be opened after closing
    ok(c.post(f"/api/billing/bills/{b['id']}/dispute", json={"reason_code": "duplicate_charge", "message": "Charged twice"}), 201)
    assert len(ok(c.get("/api/billing/disputes"))) == 2


def test_dispute_validation(world):
    c = world.client(world.A)
    b = ok(c.post("/api/billing/bills", json=bill_payload()), 201)
    r = c.post(f"/api/billing/bills/{b['id']}/dispute", json={"reason_code": "nope", "message": "x"})
    assert r.status_code == 422 and {"reason_code", "message"} <= set(r.json()["fields"])


# ---------------------------------------------------------------- matching

def _pair(c, prov="Match Clinic", **bill_kw):
    cl = ok(c.post("/api/billing/claims", json=claim_payload(provider_name=prov)), 201)
    b = ok(c.post("/api/billing/bills", json=bill_payload(provider_name=prov, amount_due="60.00", **bill_kw)), 201)
    return b, cl


def test_match_suggestions_link_unlink(world):
    c = world.client(world.A)
    b, cl = _pair(c)
    sug = ok(c.get(f"/api/billing/bills/{b['id']}/match-suggestions"))
    assert sug[0]["claim"]["id"] == cl["id"] and sug[0]["score"] >= 70 and sug[0]["strength"] == "strong" and sug[0]["reasons"]
    linked = ok(c.post(f"/api/billing/bills/{b['id']}/match", json={"claim_id": cl["id"]}))
    assert linked["match_status"] == "matched" and linked["claim_id"] == cl["id"] and linked["claim_status"] == "paid"
    ex = ok(c.get(f"/api/billing/bills/{b['id']}/explain"))
    assert ex["has_claim"] and ex["flags"] == [] and ex["patient_owes_estimate"] == "60.00"
    assert [x for x in ok(c.get("/api/billing/claims")) if x["id"] == cl["id"]][0]["matched_bill_id"] == b["id"]
    un = ok(c.post(f"/api/billing/bills/{b['id']}/unmatch"))
    assert un["match_status"] == "unmatched" and un["claim_id"] is None
    assert c.post(f"/api/billing/bills/{b['id']}/unmatch").status_code == 409


def test_claim_can_match_only_one_bill(world):
    c = world.client(world.A)
    b1, cl = _pair(c, prov="Single Match Clinic")
    b2 = ok(c.post("/api/billing/bills", json=bill_payload(provider_name="Single Match Clinic", amount_due="60.00",
                                                           billed_amount="123.00")), 201)
    ok(c.post(f"/api/billing/bills/{b1['id']}/match", json={"claim_id": cl["id"]}))
    r = c.post(f"/api/billing/bills/{b2['id']}/match", json={"claim_id": cl["id"]})
    assert r.status_code == 409 and "already matched to another bill" in r.json()["detail"]
    # suggestions mark it as taken, auto-match won't steal it
    sug = ok(c.get(f"/api/billing/bills/{b2['id']}/match-suggestions"))
    assert all(s["already_matched_elsewhere"] for s in sug if s["claim"]["id"] == cl["id"])
    res = ok(c.post("/api/billing/match/auto"))
    assert not [m for m in res["matched"] if m["bill_id"] == b2["id"]]


def test_auto_match_links_strong_and_skips_ambiguous(world):
    c = world.client(world.A)
    b, cl = _pair(c, prov="Auto Match Clinic")
    # ambiguous: two near-identical claims for another bill
    ok(c.post("/api/billing/claims", json=claim_payload(provider_name="Twin Clinic", service_date="2026-07-01")), 201)
    ok(c.post("/api/billing/claims", json=claim_payload(provider_name="Twin Clinic", service_date="2026-07-01")), 201)
    amb = ok(c.post("/api/billing/bills", json=bill_payload(provider_name="Twin Clinic", service_date="2026-07-01", amount_due="60.00")), 201)
    res = ok(c.post("/api/billing/match/auto"))
    assert {"bill_id": b["id"], "claim_id": cl["id"]} .items() <= [m for m in res["matched"] if m["bill_id"] == b["id"]][0].items()
    assert any(n["bill_id"] == amb["id"] for n in res["needs_review"])
    assert ok(c.get(f"/api/billing/bills/{amb['id']}"))["match_status"] == "unmatched"


def test_deleting_a_claim_unmatches_its_bill(world):
    c = world.client(world.A)
    b, cl = _pair(c, prov="Del Clinic")
    ok(c.post(f"/api/billing/bills/{b['id']}/match", json={"claim_id": cl["id"]}))
    assert c.delete(f"/api/billing/claims/{cl['id']}").status_code == 204
    after = ok(c.get(f"/api/billing/bills/{b['id']}"))
    assert after["match_status"] == "unmatched" and after["claim_id"] is None


def test_wrong_provider_is_not_suggested(world):
    c = world.client(world.A)
    cl = ok(c.post("/api/billing/claims", json=claim_payload(provider_name="Totally Different Eye Center",
                                                             service_date="2026-02-01", billed_amount="9.00")), 201)
    b = ok(c.post("/api/billing/bills", json=bill_payload(provider_name="Zeta Orthopedics", service_date="2026-08-01")), 201)
    assert ok(c.get(f"/api/billing/bills/{b['id']}/match-suggestions")) == []


# ---------------------------------------------------------------- explain through the API

def test_explain_endpoint_flags_duplicate_service_dates(world):
    c = world.client(world.A)
    a = ok(c.post("/api/billing/bills", json=bill_payload(provider_name="Same Day Clinic", amount_due="50.00")), 201)
    ok(c.post("/api/billing/bills", json=bill_payload(provider_name="Same Day Clinic", amount_due="25.00",
                                                      billed_amount="900.00")), 201)
    ex = ok(c.get(f"/api/billing/bills/{a['id']}/explain"))
    assert "duplicate_service_date" in {f["code"] for f in ex["flags"]}
    assert "not legal or financial advice" in ex["disclaimer"].lower()


# ---------------------------------------------------------------- patient isolation

def test_patient_isolation_everywhere(world):
    a, b = world.client(world.A), world.client(world.B)
    plan = ok(a.post("/api/billing/plans", json=plan_payload()), 201)
    bill = ok(a.post("/api/billing/bills", json=bill_payload(amount_due="100.00")), 201)
    claim = ok(a.post("/api/billing/claims", json=claim_payload()), 201)
    pay = ok(a.post(f"/api/billing/bills/{bill['id']}/payments", json={"amount": "10.00", "paid_on": "2026-09-01"}), 201)
    dis = ok(a.post(f"/api/billing/bills/{bill['id']}/dispute", json={"reason_code": "other", "message": "Please check"}), 201)
    # B sees nothing of A's in lists / summary / export
    for url in ("/api/billing/plans", "/api/billing/bills", "/api/billing/claims", "/api/billing/disputes"):
        assert ok(b.get(url)) == [], url
    assert ok(b.get("/api/billing/summary"))["total_outstanding"] == "0.00"
    exp = ok(b.get("/api/billing/export"))
    assert exp["plans"] == exp["bills"] == exp["claims"] == exp["disputes"] == []
    # ... and every id-addressed endpoint answers 404 (never 403: existence isn't leaked)
    probes = [
        ("get", f"/api/billing/plans/{plan['id']}", None), ("put", f"/api/billing/plans/{plan['id']}", {"insurer_phone": "1"}),
        ("delete", f"/api/billing/plans/{plan['id']}", None), ("post", f"/api/billing/plans/{plan['id']}/verify", None),
        ("post", f"/api/billing/plans/{plan['id']}/card", {"side": "front", "document_id": None}),
        ("get", f"/api/billing/claims/{claim['id']}", None), ("put", f"/api/billing/claims/{claim['id']}", {"notes": "x"}),
        ("delete", f"/api/billing/claims/{claim['id']}", None),
        ("get", f"/api/billing/bills/{bill['id']}", None), ("put", f"/api/billing/bills/{bill['id']}", {"notes": "x"}),
        ("delete", f"/api/billing/bills/{bill['id']}", None), ("get", f"/api/billing/bills/{bill['id']}/explain", None),
        ("get", f"/api/billing/bills/{bill['id']}/match-suggestions", None),
        ("post", f"/api/billing/bills/{bill['id']}/match", {"claim_id": claim["id"]}),
        ("post", f"/api/billing/bills/{bill['id']}/unmatch", None),
        ("get", f"/api/billing/bills/{bill['id']}/payments", None),
        ("post", f"/api/billing/bills/{bill['id']}/payments", {"amount": "1.00", "paid_on": "2026-09-02"}),
        ("delete", f"/api/billing/payments/{pay['id']}", None), ("get", f"/api/billing/payments/{pay['id']}/receipt", None),
        ("post", f"/api/billing/bills/{bill['id']}/dispute", {"reason_code": "other", "message": "hello there"}),
        ("post", f"/api/billing/disputes/{dis['id']}/close", {"outcome": "resolved"}),
    ]
    for method, url, body in probes:
        r = getattr(b, method)(url, json=body) if body is not None else getattr(b, method)(url)
        assert r.status_code == 404, (method, url, r.status_code, r.text)
    # B cannot attach A's plan/claim to B's own records
    bb = ok(b.post("/api/billing/bills", json=bill_payload()), 201)
    assert b.post("/api/billing/bills", json=bill_payload(plan_id=plan["id"])).status_code == 422
    other_claim = ok(b.post("/api/billing/claims", json=claim_payload()), 201)
    assert b.post(f"/api/billing/bills/{bb['id']}/match", json={"claim_id": claim["id"]}).status_code == 404
    assert b.post(f"/api/billing/bills/{bb['id']}/match", json={"claim_id": other_claim["id"]}).status_code == 200
    # A's data is untouched
    assert ok(a.get(f"/api/billing/bills/{bill['id']}"))["balance"] == "90.00"
    assert ok(a.get(f"/api/billing/plans/{plan['id']}"))["insurer_name"] == "Evergreen Health Plan"


def test_dashboard_alias_summary(world):
    c = world.client(world.A)
    b = ok(c.post("/api/billing/bills", json=bill_payload(amount_due="80.00")), 201)
    paid = ok(c.post("/api/billing/bills", json=bill_payload(amount_due="20.00")), 201)
    ok(c.post(f"/api/billing/bills/{paid['id']}/payments", json={"amount": "20.00", "paid_on": "2026-09-01"}), 201)
    s = ok(c.get("/api/bills/summary"))
    assert s["total_outstanding"] == "80.00" and s["count_outstanding"] == 1
    assert [i["id"] for i in s["items"]] == [b["id"]] and {"provider_name", "balance", "due_date", "claim_status"} <= set(s["items"][0])
    assert ok(world.client(world.B).get("/api/bills/summary"))["items"] == []


def test_unauthenticated_requests_are_rejected(world):
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app)
    for url in ("/api/billing/plans", "/api/billing/bills", "/api/billing/claims", "/api/billing/summary",
                "/api/billing/disputes", "/api/billing/export", "/api/billing/providers/search"):
        assert anon.get(url).status_code == 401, url
    assert anon.post("/api/billing/bills", json=bill_payload()).status_code == 401


# ---------------------------------------------------------------- seed

def test_seed_billing_is_idempotent_and_has_a_deliberate_discrepancy(world):
    from app.services.seed_billing import seed_billing
    res = seed_billing(world.db, world.A.id)
    assert res["seeded"] is True and res["bills"] == 5 and res["claims"] == 5 and res["plans"] == 2
    assert seed_billing(world.db, world.A.id)["seeded"] is False
    c = world.client(world.A)
    plans = ok(c.get("/api/billing/plans"))
    assert {p["coverage_rank"]: p["verification_status"] for p in plans} == {"primary": "verified", "secondary": "entered"}
    ex = ok(c.get(f"/api/billing/bills/{res['discrepancy_bill_id']}/explain"))
    codes = {f["code"] for f in ex["flags"]}
    assert "bill_exceeds_patient_share" in codes and ex["worst_severity"] == "problem"
    assert ex["patient_owes_estimate"] == "344.00"
    # the rest of the seed covers: clean match, pending claim, paid with receipt, denied + dispute
    bills = {b["provider_name"] + b["account_number"]: b for b in ok(c.get("/api/billing/bills"))}
    statuses = sorted(b["status"] for b in bills.values())
    assert statuses == ["in_dispute", "paid", "unpaid", "unpaid", "unpaid"]
    assert all(b["match_status"] == "matched" for b in bills.values())
    clean = ok(c.get(f"/api/billing/bills/{res['bill_ids'][1]}/explain"))
    assert clean["flags"] == []
    pending = ok(c.get(f"/api/billing/bills/{res['bill_ids'][2]}/explain"))
    assert "claim_pending" in {f["code"] for f in pending["flags"]}
    denied = ok(c.get(f"/api/billing/bills/{res['bill_ids'][4]}/explain"))
    assert "claim_denied" in {f["code"] for f in denied["flags"]}
    # other patients get nothing from A's seed
    assert ok(world.client(world.B).get("/api/billing/bills")) == []
