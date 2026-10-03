"""W8: cost & provider search (seeded MOCK directory) and bill due-date reminders."""
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

try:
    from billing_support import bill_payload, claim_payload, plan_payload, world  # noqa: F401
except ImportError:
    from tests.billing_support import bill_payload, claim_payload, plan_payload, world  # noqa: F401

from app.models.billing import BillingBill
from app.models.shared import utcnow
from app.services import provider_search as ps
from app.services.billing_reminders import reminder_phase, run_bill_reminder_sweep


def ok(r, code=200):
    assert r.status_code == code, r.text
    return r.json()


# ---------------------------------------------------------------- provider search

def test_meta_and_mock_labels(world):
    c = world.client(world.A)
    meta = ok(c.get("/api/billing/providers/meta"))
    assert meta["mock"] is True and "ESTIMATE" in meta["label"]
    assert {"wheelchair", "asl_interpreter"} <= {a["key"] for a in meta["accessibility"]}
    res = ok(c.get("/api/billing/providers/search"))
    assert res["mock"] is True and res["count"] >= 5 and "ESTIMATE" in res["label"]
    assert all(r["availability_is_mock"] and r["next_openings"] and all(o["mock"] for o in r["next_openings"]) for r in res["results"])


def test_service_filter_and_estimate_ranges_are_decimal_strings_with_uncertainty(world):
    c = world.client(world.A)
    res = ok(c.get("/api/billing/providers/search", params={"service": "mri"}))
    assert {r["name"] for r in res["results"]} == {"Riverside General Hospital", "Northgate Imaging Center"}
    for r in res["results"]:
        e = r["estimate"]
        assert e["mock"] is True and "ESTIMATE" in e["label"] and e["uncertainty_pct"] >= 25 and e["assumptions"]
        lo, hi = Decimal(e["total_low"]), Decimal(e["total_high"])
        assert 0 < lo < hi and Decimal(e["you_pay_low"]) <= Decimal(e["you_pay_high"])
        assert e["basis"] == "self_pay"  # no plan chosen -> full price range
    # cheaper provider has the lower estimate
    by = {r["name"]: Decimal(r["estimate"]["total_low"]) for r in res["results"]}
    assert by["Northgate Imaging Center"] < by["Riverside General Hospital"]


def test_plan_makes_in_network_cheaper_and_labels_out_of_network(world):
    c = world.client(world.A)
    plan = ok(c.post("/api/billing/plans", json=plan_payload(deductible_annual="500.00", copay_amount="25.00", coinsurance_pct="20")), 201)
    res = ok(c.get("/api/billing/providers/search", params={"service": "primary_care_visit", "plan_id": plan["id"]}))
    rows = {r["name"]: r for r in res["results"]}
    inn, = [r for n, r in rows.items() if n == "Lakeside Family Clinic"]
    assert inn["network"] == "in_network" and inn["estimate"]["basis"] == "in_network_plan" and inn["estimate"]["confidence"] == "medium"
    assert Decimal(inn["estimate"]["you_pay_low"]) == Decimal("25.00")  # copay when deductible is met
    assert Decimal(inn["estimate"]["you_pay_high"]) > Decimal("25.00")  # deductible left -> higher end
    assert any("deductible" in a.lower() for a in inn["estimate"]["assumptions"])
    # a provider outside the plan's network is clearly labelled and uncertain
    oon = ok(c.get("/api/billing/providers/search", params={"service": "lab_panel", "plan_id": plan["id"]}))
    west = [r for r in oon["results"] if r["name"] == "Westside Community Lab"][0]
    assert west["network"] == "out_of_network" and west["estimate"]["confidence"] == "low" and west["estimate"]["uncertainty_pct"] >= 50
    assert any("not in your plan's network" in a for a in west["estimate"]["assumptions"])
    only = ok(c.get("/api/billing/providers/search", params={"service": "lab_panel", "plan_id": plan["id"], "in_network_only": True}))
    assert {r["name"] for r in only["results"]} and all(r["network"] == "in_network" for r in only["results"])


def test_deductible_already_used_lowers_the_high_estimate(world):
    c = world.client(world.A)
    plan = ok(c.post("/api/billing/plans", json=plan_payload(deductible_annual="500.00")), 201)
    before = ok(c.get("/api/billing/providers/search", params={"service": "xray", "plan_id": plan["id"], "max_distance_miles": 50}))
    year = str(utcnow().year)
    ok(c.post("/api/billing/claims", json=claim_payload(plan_id=plan["id"], service_date=f"{year}-01-15", deductible="500.00",
                                                        patient_responsibility="560.00", coinsurance="30.00", copay="30.00")), 201)
    after = ok(c.get("/api/billing/providers/search", params={"service": "xray", "plan_id": plan["id"], "max_distance_miles": 50}))
    hi_b = {r["id"]: Decimal(r["estimate"]["you_pay_high"]) for r in before["results"] if r["network"] == "in_network"}
    hi_a = {r["id"]: Decimal(r["estimate"]["you_pay_high"]) for r in after["results"] if r["network"] == "in_network"}
    assert hi_b and all(hi_a[k] <= hi_b[k] for k in hi_b) and any(hi_a[k] < hi_b[k] for k in hi_b)


def test_distance_filter_and_sort(world):
    c = world.client(world.A)
    near = ok(c.get("/api/billing/providers/search", params={"area": "downtown", "max_distance_miles": 3}))
    assert near["count"] >= 1 and all(r["distance_miles"] is not None and r["distance_miles"] <= 3 for r in near["results"] if r["distance_miles"] is not None)
    allr = ok(c.get("/api/billing/providers/search", params={"area": "downtown"}))
    d = [r["distance_miles"] for r in allr["results"] if r["distance_miles"] is not None]
    assert d == sorted(d) and allr["count"] > near["count"]
    far = ok(c.get("/api/billing/providers/search", params={"area": "northgate", "max_distance_miles": 3}))
    assert {r["name"] for r in far["results"]} != {r["name"] for r in near["results"]}
    # custom coordinates override the named area
    custom = ok(c.get("/api/billing/providers/search", params={"lat": 39.8750, "lon": -89.6350, "max_distance_miles": 1}))
    assert "Northgate Imaging Center" in {r["name"] for r in custom["results"]}
    # cost sort and soonest sort
    cost = ok(c.get("/api/billing/providers/search", params={"service": "lab_panel", "sort": "cost"}))
    lows = [Decimal(r["estimate"]["you_pay_low"]) for r in cost["results"]]
    assert lows == sorted(lows)
    soon = ok(c.get("/api/billing/providers/search", params={"sort": "soonest"}))
    firsts = [r["next_openings"][0]["start"] for r in soon["results"]]
    assert firsts == sorted(firsts)
    assert c.get("/api/billing/providers/search", params={"sort": "vibes"}).status_code == 422
    assert c.get("/api/billing/providers/search", params={"service": "teleportation"}).status_code == 422


def test_accessibility_filters_require_all(world):
    c = world.client(world.A)
    res = ok(c.get("/api/billing/providers/search", params=[("accessibility", "asl_interpreter"), ("accessibility", "wheelchair")]))
    assert res["count"] >= 1
    for r in res["results"]:
        keys = {a["key"] for a in r["accessibility"]}
        assert {"asl_interpreter", "wheelchair"} <= keys
    assert res["count"] < ok(c.get("/api/billing/providers/search"))["count"]
    none = ok(c.get("/api/billing/providers/search", params=[("accessibility", "hearing_loop"), ("accessibility", "sensory_friendly"), ("accessibility", "telehealth")]))
    assert none["count"] == 0


def test_availability_window_filter_and_determinism():
    start = date(2026, 10, 5)  # a Monday
    a = ps.search_providers(date_from=start, date_to=start, today=start)
    assert a["count"] >= 1
    for r in a["results"]:
        assert all(o["start"].startswith("2026-10-05") for o in r["next_openings"])
    # a single Sunday: only providers open Sundays remain
    sun = ps.search_providers(date_from=date(2026, 10, 11), date_to=date(2026, 10, 11), today=start)
    assert 0 < sun["count"] < a["count"] + 5
    assert all("Sun" in o["label"] for r in sun["results"] for o in r["next_openings"])
    # same query, same answer (no randomness)
    assert ps.search_providers(date_from=start, date_to=start + timedelta(days=7), today=start) == \
           ps.search_providers(date_from=start, date_to=start + timedelta(days=7), today=start)
    assert ps.search_providers(date_from=start + timedelta(days=7), date_to=start, today=start)["window"]["from"] == "2026-10-05"


def test_text_and_specialty_filters_and_detail(world):
    c = world.client(world.A)
    assert {r["name"] for r in ok(c.get("/api/billing/providers/search", params={"q": "heart"}))["results"]} == {"Springfield Heart Specialists"}
    assert all("Family" in r["specialty"] for r in ok(c.get("/api/billing/providers/search", params={"specialty": "family"}))["results"])
    det = ok(c.get("/api/billing/providers/mock-lakeside-family"))
    assert det["openings"] and det["estimates"] and det["mock"] is True and det["distance_miles"] is not None
    assert c.get("/api/billing/providers/does-not-exist").status_code == 404
    other_plan = ok(world.client(world.B).post("/api/billing/plans", json=plan_payload()), 201)
    assert c.get("/api/billing/providers/search", params={"plan_id": other_plan["id"]}).status_code == 404  # someone else's plan


def test_estimate_math_is_decimal_and_ordered():
    p = ps._BY_ID["mock-lakeside-family"]
    e = ps.estimate_cost(p, "primary_care_visit", None)
    assert Decimal(e["total_low"]) == Decimal("120") * Decimal("0.90") and Decimal(e["total_high"]) == Decimal("260") * Decimal("0.90")
    plan = {"insurer_name": "Evergreen Health Plan", "deductible_annual": "1000", "copay_amount": "40", "coinsurance_pct": "25"}
    e2 = ps.estimate_cost(p, "lab_panel", plan, deductible_applied=Decimal("1000"))  # deductible met, non-visit -> coinsurance
    allowed_lo = Decimal(e2["total_low"])
    assert Decimal(e2["you_pay_low"]) == (allowed_lo * Decimal("0.25")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert e2["deductible_remaining_assumed"] == "0.00"


# ---------------------------------------------------------------- reminders

def mk_bill(c, due, amount="100.00", **kw):
    return ok(c.post("/api/billing/bills", json=bill_payload(amount_due=amount, due_date=str(due), **kw)), 201)


def test_reminder_phases():
    d = date(2026, 10, 10)
    assert reminder_phase(d + timedelta(days=10), d) is None
    assert reminder_phase(d + timedelta(days=3), d) == "soon" and reminder_phase(d + timedelta(days=1), d) == "soon"
    assert reminder_phase(d, d) == "today"
    assert reminder_phase(d - timedelta(days=1), d) == "overdue-w0" and reminder_phase(d - timedelta(days=8), d) == "overdue-w1"


def test_sweep_notifies_once_per_phase_and_uses_bill_due_kind(world):
    c = world.client(world.A)
    today = utcnow().date()
    soon = mk_bill(c, today + timedelta(days=2), provider_name="Soon Clinic")
    mk_bill(c, today + timedelta(days=30), provider_name="Far Clinic")
    late = mk_bill(c, today - timedelta(days=4), provider_name="Late Clinic")
    n = run_bill_reminder_sweep(world.db)
    mine = world.notifications(world.A, "bill_due")
    titles = sorted(x.title for x in mine)
    assert n >= 2 and titles == ["Bill due soon: Soon Clinic", "Bill overdue: Late Clinic"]
    assert all(x.link == "#/billing" for x in mine)
    # idempotent
    assert run_bill_reminder_sweep(world.db) == 0
    assert len(world.notifications(world.A, "bill_due")) == 2
    # next phase for the same bill (due today) fires once more
    world.db.expire_all()
    b = world.db.get(BillingBill, soon["id"])
    b.due_date = today
    world.db.commit()
    assert run_bill_reminder_sweep(world.db) == 1
    assert "due today" in world.notifications(world.A, "bill_due")[-1].body
    # nothing leaked to the other patient
    assert world.notifications(world.B, "bill_due") == []


def test_sweep_skips_paid_disputed_void_and_no_due_date(world):
    c = world.client(world.A)
    today = utcnow().date()
    paid = mk_bill(c, today + timedelta(days=1), amount="50.00", provider_name="Paid Clinic")
    ok(c.post(f"/api/billing/bills/{paid['id']}/payments", json={"amount": "50.00", "paid_on": str(today)}), 201)
    disp = mk_bill(c, today + timedelta(days=1), provider_name="Disputed Clinic")
    ok(c.post(f"/api/billing/bills/{disp['id']}/dispute", json={"reason_code": "other", "message": "checking this"}), 201)
    void = mk_bill(c, today + timedelta(days=1), provider_name="Void Clinic")
    ok(c.put(f"/api/billing/bills/{void['id']}", json={"status": "void"}))
    ok(c.post("/api/billing/bills", json=bill_payload(due_date=None, provider_name="Undated Clinic")), 201)
    assert run_bill_reminder_sweep(world.db, patient_id=world.A.id) == 0
    assert world.notifications(world.A, "bill_due") == []
    # closing the dispute un-pauses it
    d = c.get("/api/billing/disputes").json()[0]
    ok(c.post(f"/api/billing/disputes/{d['id']}/close", json={"outcome": "resolved"}))
    assert run_bill_reminder_sweep(world.db, patient_id=world.A.id) == 1


def test_sweep_amount_is_balance_not_total(world):
    c = world.client(world.A)
    today = utcnow().date()
    b = mk_bill(c, today + timedelta(days=1), amount="100.00", provider_name="Balance Clinic")
    ok(c.post(f"/api/billing/bills/{b['id']}/payments", json={"amount": "60.00", "paid_on": str(today)}), 201)
    run_bill_reminder_sweep(world.db, patient_id=world.A.id)
    body = world.notifications(world.A, "bill_due")[0].body
    assert "$40.00" in body


def test_patient_can_run_own_sweep_only(world):
    ca, cb = world.client(world.A), world.client(world.B)
    today = utcnow().date()
    mk_bill(ca, today + timedelta(days=1), provider_name="A Clinic")
    mk_bill(cb, today + timedelta(days=1), provider_name="B Clinic")
    assert ok(ca.post("/api/billing/reminders/run"))["notifications_created"] == 1
    assert len(world.notifications(world.A, "bill_due")) == 1 and world.notifications(world.B, "bill_due") == []
