"""Synthetic billing demo data (W8).  ``seed_billing(db, patient_id)`` is idempotent and commits.

All names, ids and amounts are made up.  Includes ONE bill with a deliberate discrepancy (the provider asks for
the full price minus what the insurer paid, ignoring the plan discount), plus a clean matched bill, a pending
claim, a paid bill with a receipt, and a denied claim with an open dispute.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as Dec

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing import BillingBill, BillingClaim, InsurancePlan
from app.services import billing_match, billing_service as bs


def seed_billing(db: Session, patient_id: str) -> dict:
    existing = db.scalar(select(func.count()).select_from(InsurancePlan).where(InsurancePlan.patient_id == patient_id)) or 0
    existing += db.scalar(select(func.count()).select_from(BillingBill).where(BillingBill.patient_id == patient_id)) or 0
    if existing:
        return {"seeded": False, "reason": "billing data already present"}

    t = bs.today()
    d = lambda days: t + timedelta(days=days)  # noqa: E731

    primary = bs.create_plan(db, patient_id, dict(
        insurer_name="Evergreen Health Plan", plan_name="Evergreen Choice PPO", plan_type="ppo",
        member_id="EV-4410-2287", group_number="GRP-5521", policyholder_name="Sam Sample",
        policyholder_relationship="self", coverage_rank="primary", effective_date=t.replace(month=1, day=1),
        deductible_annual=Dec("1500.00"), out_of_pocket_max=Dec("5000.00"), copay_amount=Dec("30.00"),
        coinsurance_pct=Dec("20.00"), insurer_phone="555-0140"))
    bs.verify_plan(db, patient_id, primary.id)  # mock check -> verified
    secondary = bs.create_plan(db, patient_id, dict(
        insurer_name="Pinecrest Supplemental", plan_name="Pinecrest Gap Cover", plan_type="other",
        member_id="PC-90817", group_number=None, policyholder_name="Alex Sample",
        policyholder_relationship="spouse", coverage_rank="secondary", effective_date=t.replace(month=1, day=1),
        coinsurance_pct=None, insurer_phone="555-0141"))  # left as 'entered' on purpose (shows the difference)

    # --- 1. DELIBERATE DISCREPANCY: bill asks for full price minus insurer payment, ignoring the discount.
    # billed 1850.00, allowed 1120.00 -> discount 730.00; deductible 100 + copay 50 + coinsurance 20% of 970 = 194
    # insurer paid 776.00, your share 344.00.  Bill asks 1074.00 (= 1850 - 776) -> 730.00 too much.
    c1 = bs.create_claim(db, patient_id, dict(
        plan_id=primary.id, claim_number="EV-2026-100231", insurer_name="Evergreen Health Plan",
        provider_name="Riverside General Hospital", service_date=d(-48),
        service_description="Urgent care visit with X-ray (synthetic)", billed_amount=Dec("1850.00"),
        allowed_amount=Dec("1120.00"), insurer_paid=Dec("776.00"), deductible=Dec("100.00"), copay=Dec("50.00"),
        coinsurance=Dec("194.00"), patient_responsibility=Dec("344.00"), status="paid"))
    b1 = bs.create_bill(db, patient_id, dict(
        provider_name="Riverside General Hospital", account_number="RGH-77120", service_date=d(-48),
        statement_date=d(-30), description="Urgent care visit with X-ray", billed_amount=Dec("1850.00"),
        amount_due=Dec("1074.00"), due_date=d(2), plan_id=primary.id,
        notes="Seed: this bill has a deliberate discrepancy for the demo."))

    # --- 2. clean, matched: Lakeside office visit (copay only)
    c2 = bs.create_claim(db, patient_id, dict(
        plan_id=primary.id, claim_number="EV-2026-100877", insurer_name="Evergreen Health Plan",
        provider_name="Lakeside Family Clinic", service_date=d(-21), service_description="Office visit (synthetic)",
        billed_amount=Dec("240.00"), allowed_amount=Dec("150.00"), insurer_paid=Dec("120.00"), deductible=Dec("0.00"),
        copay=Dec("30.00"), coinsurance=Dec("0.00"), patient_responsibility=Dec("30.00"), status="paid"))
    b2 = bs.create_bill(db, patient_id, dict(
        provider_name="Lakeside Family Clinic", account_number="LFC-5530", service_date=d(-21), statement_date=d(-14),
        description="Office visit", billed_amount=Dec("240.00"), amount_due=Dec("30.00"), due_date=d(20), plan_id=primary.id))

    # --- 3. pending: MRI, claim still processing
    c3 = bs.create_claim(db, patient_id, dict(
        plan_id=primary.id, claim_number="EV-2026-101302", insurer_name="Evergreen Health Plan",
        provider_name="Northgate Imaging Center", service_date=d(-10), service_description="MRI, one joint (synthetic)",
        billed_amount=Dec("1400.00"), status="processing"))
    b3 = bs.create_bill(db, patient_id, dict(
        provider_name="Northgate Imaging Center", account_number="NIC-20991", service_date=d(-10), statement_date=d(-3),
        description="MRI, one joint", billed_amount=Dec("1400.00"), amount_due=Dec("1400.00"), due_date=d(14), plan_id=primary.id))

    # --- 4. paid with a receipt
    b4 = bs.create_bill(db, patient_id, dict(
        provider_name="Lakeside Family Clinic", account_number="LFC-5412", service_date=d(-90), statement_date=d(-80),
        description="Blood test panel", billed_amount=Dec("95.00"), amount_due=Dec("22.50"), due_date=d(-60), plan_id=primary.id))
    bs.add_payment(db, patient_id, b4.id, dict(amount=Dec("22.50"), paid_on=d(-65), method="card",
                                               confirmation_number="CONF-48812", note="Paid online (synthetic)"))
    c4 = bs.create_claim(db, patient_id, dict(
        plan_id=primary.id, claim_number="EV-2026-099540", insurer_name="Evergreen Health Plan",
        provider_name="Lakeside Family Clinic", service_date=d(-90), service_description="Blood test panel (synthetic)",
        billed_amount=Dec("95.00"), allowed_amount=Dec("45.00"), insurer_paid=Dec("22.50"), deductible=Dec("0.00"),
        copay=Dec("0.00"), coinsurance=Dec("22.50"), patient_responsibility=Dec("22.50"), status="paid"))

    # --- 5. denied claim + overdue bill in dispute
    c5 = bs.create_claim(db, patient_id, dict(
        plan_id=primary.id, claim_number="EV-2026-100590", insurer_name="Evergreen Health Plan",
        provider_name="Summit Physical Therapy", service_date=d(-40), service_description="Physical therapy x3 (synthetic)",
        billed_amount=Dec("410.00"), status="denied", denial_reason="Service needed prior approval (synthetic reason)"))
    b5 = bs.create_bill(db, patient_id, dict(
        provider_name="Summit Physical Therapy", account_number="SPT-3381", service_date=d(-40), statement_date=d(-25),
        description="Physical therapy, 3 sessions", billed_amount=Dec("410.00"), amount_due=Dec("410.00"),
        due_date=d(-5), plan_id=primary.id))
    bs.open_dispute(db, patient_id, b5.id, dict(
        reason_code="claim_denied", disputed_amount=Dec("410.00"),
        message="My insurer denied the claim for missing prior approval. I am asking the clinic to hold this bill while I appeal."))

    billing_match.auto_match(db, patient_id)  # links the strong matches; the patient can unmatch any of them
    db.commit()
    return {"seeded": True, "plans": 2, "claims": 5, "bills": 5, "discrepancy_bill_id": b1.id,
            "secondary_plan_id": secondary.id, "claim_ids": [c1.id, c2.id, c3.id, c4.id, c5.id],
            "bill_ids": [b1.id, b2.id, b3.id, b4.id, b5.id]}
