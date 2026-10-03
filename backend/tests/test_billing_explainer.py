"""W8: deterministic EOB/bill explainer - Decimal math, discrepancy detection, wording, quoting of fields."""
from datetime import date
from decimal import Decimal

import pytest

from app.services import eob_explainer as ex
from app.services.eob_explainer import explain_bill_eob


def bill(**kw):
    b = dict(id="b1", provider_name="Riverside General Hospital", service_date=date(2026, 8, 1),
             billed_amount="1850.00", amount_due="344.00", due_date=date(2026, 12, 1), account_number="A1")
    b.update(kw)
    return b


def claim(**kw):
    c = dict(id="c1", claim_number="CLM-1", status="paid", billed_amount="1850.00", allowed_amount="1120.00",
             insurer_paid="776.00", deductible="100.00", copay="50.00", coinsurance="194.00",
             patient_responsibility="344.00", denial_reason=None, service_date=date(2026, 8, 1),
             provider_name="Riverside General Hospital")
    c.update(kw)
    return c


def codes(res):
    return {f["code"] for f in res["flags"]}


def flag(res, code):
    return next(f for f in res["flags"] if f["code"] == code)


def test_clean_bill_has_no_flags_and_math_adds_up():
    r = explain_bill_eob(bill(), claim())
    assert r["flags"] == [] and r["worst_severity"] == "ok"
    assert r["patient_owes_estimate"] == "344.00" and r["expected_patient_share"] == "344.00"
    by = {ln["key"]: ln for ln in r["lines"]}
    assert by["discount"]["amount"] == "730.00"  # billed - allowed
    assert by["allowed"]["amount"] == "1120.00" and by["insurer_paid"]["amount"] == "776.00"
    # components: 100 + 50 + 194 == 344 == allowed - paid
    assert sum(Decimal(by[k]["amount"]) for k in ("deductible", "copay", "coinsurance")) == Decimal(by["patient_responsibility"]["amount"])
    assert Decimal(by["allowed"]["amount"]) - Decimal(by["insurer_paid"]["amount"]) == Decimal("344.00")


def test_every_line_and_flag_quotes_exact_fields():
    r = explain_bill_eob(bill(amount_due="1074.00"), claim())
    for ln in r["lines"]:
        assert ln["source"]["field"] and "value" in ln["source"]
    by = {ln["key"]: ln for ln in r["lines"]}
    assert by["allowed"]["source"] == {"field": "claim.allowed_amount", "value": "1120.00"}
    assert by["bill_due"]["source"] == {"field": "bill.amount_due", "value": "1074.00"}
    assert r["flags"]
    for f in r["flags"]:
        assert f["fields"], f["code"]
        assert all(set(x) == {"field", "value"} for x in f["fields"])
    f = flag(r, "bill_exceeds_patient_share")
    quoted = {x["field"]: x["value"] for x in f["fields"]}
    assert quoted["bill.amount_due"] == "1074.00" and quoted["claim.patient_responsibility"] == "344.00"


def test_deliberate_discrepancy_bill_exceeds_share_and_allowed():
    r = explain_bill_eob(bill(amount_due="1074.00"), claim())
    f = flag(r, "bill_exceeds_patient_share")
    assert f["severity"] == "problem" and "$730.00 more" in f["message"]
    assert "plan discount was not applied" in f["message"]  # 1850 - 776 == 1074
    assert "bill_exceeds_allowed" not in codes(r)  # 1074.00 < allowed 1120.00
    r_over = explain_bill_eob(bill(amount_due="1300.00"), claim())
    assert "bill_exceeds_allowed" in codes(r_over)  # more than the whole allowed amount
    assert r["worst_severity"] == "problem"
    assert r["patient_owes_estimate"] == "344.00"  # trust the EOB share, not the inflated bill
    assert r["questions_to_ask"]


def test_bill_slightly_over_share_is_flagged_but_not_over_allowed():
    r = explain_bill_eob(bill(amount_due="400.00"), claim())
    assert "bill_exceeds_patient_share" in codes(r) and "bill_exceeds_allowed" not in codes(r)
    assert "$56.00 more" in flag(r, "bill_exceeds_patient_share")["message"]


def test_bill_below_share_is_info_only():
    r = explain_bill_eob(bill(amount_due="300.00"), claim())
    assert codes(r) == {"bill_below_patient_share"} and flag(r, "bill_below_patient_share")["severity"] == "info"


def test_components_dont_sum():
    r = explain_bill_eob(bill(), claim(coinsurance="190.00"))  # 100+50+190 = 340 != 344
    f = flag(r, "components_dont_sum")
    assert f["severity"] == "problem" and "$340.00" in f["message"] and "$344.00" in f["message"]
    assert "$4.00" in f["message"]


def test_insurer_paid_plus_share_must_equal_allowed():
    r = explain_bill_eob(bill(), claim(insurer_paid="700.00"))  # 700 + 344 = 1044 != 1120
    f = flag(r, "allowed_not_paid_plus_share")
    assert "$1,044.00" in f["message"] and "$1,120.00" in f["message"]


def test_allowed_greater_than_billed():
    r = explain_bill_eob(bill(), claim(billed_amount="1000.00", allowed_amount="1120.00"))
    assert "allowed_exceeds_billed" in codes(r)


def test_bill_and_claim_prices_differ():
    r = explain_bill_eob(bill(billed_amount="1900.00"), claim())
    assert "billed_mismatch" in codes(r)


def test_denied_claim_is_flagged_with_reason_and_no_math_noise():
    r = explain_bill_eob(bill(amount_due="410.00", billed_amount="410.00"),
                         claim(status="denied", billed_amount="410.00", allowed_amount=None, insurer_paid=None,
                               deductible=None, copay=None, coinsurance=None, patient_responsibility=None,
                               denial_reason="Needs prior approval"))
    f = flag(r, "claim_denied")
    assert f["severity"] == "problem" and "Needs prior approval" in f["message"]
    assert not codes(r) & {"components_dont_sum", "bill_exceeds_patient_share", "bill_exceeds_allowed"}
    assert r["expected_patient_share"] is None
    assert r["patient_owes_basis"].startswith("the bill minus")  # falls back, and says so


@pytest.mark.parametrize("status", ["processing", "submitted"])
def test_pending_claim_flag_and_missing_info(status):
    r = explain_bill_eob(bill(amount_due="900.00"), claim(status=status, allowed_amount=None, insurer_paid=None,
                                                          deductible=None, copay=None, coinsurance=None,
                                                          patient_responsibility=None))
    assert "claim_pending" in codes(r) and flag(r, "claim_pending")["severity"] == "warning"
    # not decided yet -> don't nag about missing EOB numbers
    assert not [m for m in r["missing_info"] if m["field"].startswith("claim.")]


def test_decided_claim_lists_missing_fields():
    r = explain_bill_eob(bill(), claim(allowed_amount=None, insurer_paid=None, deductible=None, copay=None,
                                       coinsurance=None, patient_responsibility=None))
    missing = {m["field"] for m in r["missing_info"]}
    assert {"claim.allowed_amount", "claim.insurer_paid", "claim.patient_responsibility"} <= missing
    assert any("deductible" in m for m in missing)


def test_no_claim():
    r = explain_bill_eob(bill(due_date=None, amount_due="250.00"), None)
    assert "no_claim" in codes(r) and r["has_claim"] is False
    assert r["patient_owes_estimate"] == "250.00"
    assert {"claim", "bill.due_date"} <= {m["field"] for m in r["missing_info"]}


def test_duplicate_service_dates_for_bills_and_claims():
    other = bill(id="b2", amount_due="75.00")
    r = explain_bill_eob(bill(), claim(), related_bills=[other, bill()])  # the bill itself is ignored
    f = flag(r, "duplicate_service_date")
    assert "$75.00" in f["message"] and f["severity"] == "warning"
    r2 = explain_bill_eob(bill(), claim(), related_claims=[claim(id="c2", claim_number="CLM-2"), claim()])
    assert "CLM-2" in flag(r2, "duplicate_service_date")["message"]
    r3 = explain_bill_eob(bill(), claim(), related_bills=[bill(id="b3", service_date=date(2026, 7, 1)),
                                                          bill(id="b4", provider_name="Someone Else")])
    assert "duplicate_service_date" not in codes(r3)


def test_overpaid_and_paid_more_than_share():
    r = explain_bill_eob(bill(amount_due="344.00"), claim(), payments_total="400.00")
    assert "overpaid" in codes(r) and "paid_more_than_share" not in codes(r)  # one flag, not two for the same problem
    assert r["patient_owes_estimate"] == "0.00"
    r2 = explain_bill_eob(bill(amount_due="1074.00"), claim(), payments_total="400.00")  # paid more than share, not more than bill
    assert "paid_more_than_share" in codes(r2) and "overpaid" not in codes(r2)


def test_payments_reduce_estimate():
    r = explain_bill_eob(bill(), claim(), payments_total="100.00")
    assert r["patient_owes_estimate"] == "244.00" and r["bill_balance"] == "244.00"


def test_plan_context_flags():
    plan = dict(effective_date=date(2026, 9, 1), end_date=None, deductible_annual="50.00")
    r = explain_bill_eob(bill(), claim(), plan)
    assert {"service_outside_coverage", "deductible_exceeds_plan"} <= codes(r)
    ok = explain_bill_eob(bill(), claim(), dict(effective_date=date(2026, 1, 1), end_date=None, deductible_annual="1500.00"))
    assert ok["flags"] == []


def test_decimal_precision_no_float_error():
    # 0.1 + 0.2 != 0.3 in float. Floats and strings must both be handled exactly.
    b = bill(billed_amount=0.3, amount_due=0.3)
    c = claim(billed_amount=0.3, allowed_amount=0.3, insurer_paid=0.0, deductible=0.1, copay=0.1, coinsurance=0.1,
              patient_responsibility=0.3)
    r = explain_bill_eob(b, c)
    assert "components_dont_sum" not in codes(r) and r["flags"] == []
    assert isinstance(ex.D(0.1), Decimal) and ex.D(0.1) == Decimal("0.10")
    assert ex.D("$1,234.565") == Decimal("1234.57") and ex.D("") is None and ex.D("abc") is None


def test_one_cent_tolerance():
    r = explain_bill_eob(bill(), claim(patient_responsibility="344.01", coinsurance="194.01"))
    assert "components_dont_sum" not in codes(r)
    r2 = explain_bill_eob(bill(), claim(patient_responsibility="344.02"))
    assert "components_dont_sum" in codes(r2)


def test_label_disclaimer_and_plain_language():
    r = explain_bill_eob(bill(amount_due="1074.00"), claim())
    assert "not legal or financial advice" in r["disclaimer"].lower()
    assert "estimate" in r["estimate_label"].lower() and "not legal or financial advice" in r["estimate_label"].lower()
    assert "not legal or financial advice" in r["summary"].lower()
    assert r["reading_grade_estimate"] <= 8.5, r["summary"]
    for term in ("Allowed amount", "Deductible", "Copay", "Coinsurance", "EOB", "Patient responsibility"):
        assert term in r["glossary"]
    for f in r["flags"]:
        assert ex.readability_grade(f["message"]) <= 12  # flag text stays simple too


def test_deterministic_same_input_same_output():
    a = explain_bill_eob(bill(amount_due="1074.00"), claim())
    b = explain_bill_eob(bill(amount_due="1074.00"), claim())
    assert a == b


def test_llm_hook_is_off_by_default_and_never_changes_numbers(monkeypatch):
    called = []
    ex.set_llm_explainer(lambda res: called.append(1) or "friendly text")
    try:
        monkeypatch.delenv("EOB_LLM_EXPLAINER", raising=False)
        r = explain_bill_eob(bill(), claim())
        assert r["llm_enabled"] is False and r["llm_note"] is None and not called
        base = {k: v for k, v in r.items() if k not in ("llm_enabled", "llm_note")}
        monkeypatch.setenv("EOB_LLM_EXPLAINER", "1")
        r2 = explain_bill_eob(bill(), claim())
        assert called and r2["llm_enabled"] is True and "AI" in r2["llm_note"]["label"]
        assert {k: v for k, v in r2.items() if k not in ("llm_enabled", "llm_note")} == base
        ex.set_llm_explainer(lambda res: 1 / 0)  # a failing helper must not break the explanation
        r3 = explain_bill_eob(bill(), claim())
        assert r3["llm_note"] is None and r3["lines"]
    finally:
        ex.set_llm_explainer(None)


def test_llm_env_without_hook_stays_off(monkeypatch):
    monkeypatch.setenv("EOB_LLM_EXPLAINER", "1")
    ex.set_llm_explainer(None)
    assert explain_bill_eob(bill(), claim())["llm_enabled"] is False


def test_orm_objects_are_accepted():
    from app.models.billing import BillingBill, BillingClaim
    b = BillingBill(provider_name="X Clinic", service_date=date(2026, 1, 2), billed_amount=Decimal("100.00"),
                    amount_due=Decimal("20.00"))
    c = BillingClaim(claim_number="Z", status="paid", billed_amount=Decimal("100.00"), allowed_amount=Decimal("60.00"),
                     insurer_paid=Decimal("40.00"), deductible=Decimal("0.00"), copay=Decimal("20.00"),
                     coinsurance=Decimal("0.00"), patient_responsibility=Decimal("20.00"))
    r = explain_bill_eob(b, c)
    assert r["flags"] == [] and r["patient_owes_estimate"] == "20.00"
