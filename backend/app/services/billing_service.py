"""Insurance plans, claims/EOBs, bills, payments/receipts, disputes (W8).

Conventions
* Every function takes ``patient_id`` and only ever touches that patient's rows (other patients' ids give a 404,
  never a 403, so existence is not leaked).
* Money is ``Decimal`` end to end; API output is a string with 2 decimals.
* Service functions add rows + audit entries to the caller's session and ``flush``; the ROUTE commits, so a
  record + its audit entry commit or roll back together.
* Friendly errors: duplicates -> HTTP 409 with a plain sentence; bad input -> ``FieldError`` (422 with fields).
"""
from __future__ import annotations

import secrets
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import FieldError
from app.models.billing import (BillingBill, BillingClaim, BillingDispute, BillingPayment, InsurancePlan)
from app.models.shared import Patient, utcnow
from app.services.audit import log_event
from app.services.eob_explainer import D, explain_bill_eob

ZERO = Decimal("0.00")
OPEN_STATUSES = ("unpaid", "partially_paid", "in_dispute")


# --------------------------------------------------------------------------- tiny helpers

def today() -> date:
    return utcnow().date()


def ms(d: Decimal | None) -> str | None:
    """Money -> '123.45' (string; never float)."""
    return None if d is None else f"{D(d):.2f}"


def iso(v: date | datetime | None) -> str | None:
    if v is None:
        return None
    return v.isoformat(timespec="seconds") + "Z" if isinstance(v, datetime) else v.isoformat()


def norm_text(s: str | None) -> str:
    return " ".join((s or "").split())


def norm_name(s: str | None) -> str:
    return norm_text(s).lower()


def audit(db: Session, patient_id: str, action: str, rtype: str, rid: str, detail: dict | None = None) -> None:
    log_event(db, actor_type="patient", actor_id=patient_id, patient_id=patient_id, action=action,
              resource_type=rtype, resource_id=rid, detail=detail)


def not_found(what: str) -> HTTPException:
    return HTTPException(404, f"We couldn't find that {what}.")


def get_owned(db: Session, model, rid: str, patient_id: str, what: str):
    row = db.get(model, rid) if rid else None
    if row is None or row.patient_id != patient_id:
        raise not_found(what)
    return row


def conflict(msg: str, **extra) -> HTTPException:
    """409 with a plain-sentence ``detail`` (the UI shows it as-is). Extras go in X-* headers for API clients."""
    headers = {"X-Existing-Id": str(extra["existing_id"])} if extra.get("existing_id") else {}
    if extra.get("needs_confirmation"):
        headers["X-Needs-Confirmation"] = "1"
    return HTTPException(409, msg, headers=headers or None)


def flush_or_409(db: Session, friendly: str) -> None:
    """Flush; a unique-constraint race becomes a friendly 409 instead of a 500."""
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(409, friendly) from e


# --------------------------------------------------------------------------- insurance plans

def plan_dict(p: InsurancePlan) -> dict:
    t = today()
    active = p.effective_date <= t and (p.end_date is None or p.end_date >= t)
    return {
        "id": p.id, "insurer_name": p.insurer_name, "plan_name": p.plan_name, "plan_type": p.plan_type,
        "member_id": p.member_id, "group_number": p.group_number,
        "policyholder_name": p.policyholder_name, "policyholder_relationship": p.policyholder_relationship,
        "policyholder_dob": iso(p.policyholder_dob), "coverage_rank": p.coverage_rank,
        "effective_date": iso(p.effective_date), "end_date": iso(p.end_date), "is_active": active,
        "deductible_annual": ms(p.deductible_annual), "out_of_pocket_max": ms(p.out_of_pocket_max),
        "copay_amount": ms(p.copay_amount), "coinsurance_pct": None if p.coinsurance_pct is None else f"{Decimal(p.coinsurance_pct):.2f}",
        "insurer_phone": p.insurer_phone,
        "verification_status": p.verification_status, "verified_at": iso(p.verified_at),
        "verification_note": p.verification_note,
        # honest label for the UI
        "source_label": ("Verified (mock check)" if p.verification_status == "verified"
                         else "Verification failed (mock check)" if p.verification_status == "failed"
                         else "Entered by you - not verified"),
        "card_front_document_id": p.card_front_document_id, "card_back_document_id": p.card_back_document_id,
        "created_at": iso(p.created_at), "updated_at": iso(p.updated_at),
    }


def _plan_key(insurer: str, member_id: str) -> str:
    return f"{norm_name(insurer)}|{norm_text(member_id).upper()}"


def _ranges_overlap(a_from: date, a_to: date | None, b_from: date, b_to: date | None) -> bool:
    return (a_to is None or a_to >= b_from) and (b_to is None or b_to >= a_from)


def _check_plan_rules(db: Session, patient_id: str, data: dict, exclude_id: str | None) -> None:
    if data.get("end_date") and data["end_date"] < data["effective_date"]:
        raise FieldError({"end_date": "The end date can't be before the start date."})
    key = _plan_key(data["insurer_name"], data["member_id"])
    dup = db.scalar(select(InsurancePlan).where(InsurancePlan.patient_id == patient_id,
                                                InsurancePlan.dedupe_key == key,
                                                InsurancePlan.id != (exclude_id or "")))
    if dup:
        raise conflict(f"You already saved this plan ({dup.insurer_name}, member ID {dup.member_id}). "
                       "Edit the existing plan instead of adding it again.", existing_id=dup.id)
    rank = data["coverage_rank"]
    others = db.scalars(select(InsurancePlan).where(InsurancePlan.patient_id == patient_id,
                                                    InsurancePlan.coverage_rank == rank,
                                                    InsurancePlan.id != (exclude_id or ""))).all()
    for o in others:
        if _ranges_overlap(data["effective_date"], data.get("end_date"), o.effective_date, o.end_date):
            raise conflict(f"You already have a {rank} plan ({o.insurer_name}) during these dates. "
                           f"Set an end date on that plan, or save this one as {'secondary' if rank == 'primary' else 'a different order'}.",
                           existing_id=o.id)


def create_plan(db: Session, patient_id: str, data: dict) -> InsurancePlan:
    data = dict(data)
    data["insurer_name"] = norm_text(data["insurer_name"])
    data["member_id"] = norm_text(data["member_id"])
    _check_plan_rules(db, patient_id, data, None)
    plan = InsurancePlan(patient_id=patient_id, dedupe_key=_plan_key(data["insurer_name"], data["member_id"]), **data)
    db.add(plan)
    flush_or_409(db, "You already saved this plan.")
    audit(db, patient_id, "billing_plan_created", "insurance_plan", plan.id, {"insurer": plan.insurer_name})
    return plan


_COVERAGE_FIELDS = ("insurer_name", "member_id", "group_number", "policyholder_name", "effective_date", "end_date",
                    "policyholder_relationship", "policyholder_dob")


def update_plan(db: Session, patient_id: str, plan_id: str, data: dict) -> InsurancePlan:
    plan = get_owned(db, InsurancePlan, plan_id, patient_id, "insurance plan")
    merged = {k: getattr(plan, k) for k in ("insurer_name", "member_id", "effective_date", "end_date", "coverage_rank")}
    merged.update({k: v for k, v in data.items() if k in merged})
    if "insurer_name" in data:
        merged["insurer_name"] = norm_text(data["insurer_name"])
    if "member_id" in data:
        merged["member_id"] = norm_text(data["member_id"])
    _check_plan_rules(db, patient_id, merged, plan.id)
    changed_coverage = any(k in data and getattr(plan, k) != (norm_text(v) if isinstance(v, str) else v)
                           for k, v in data.items() if k in _COVERAGE_FIELDS)
    for k, v in data.items():
        if k in ("insurer_name", "member_id"):
            v = norm_text(v)
        setattr(plan, k, v)
    plan.dedupe_key = _plan_key(plan.insurer_name, plan.member_id)
    if changed_coverage and plan.verification_status != "entered":
        plan.verification_status, plan.verified_at = "entered", None
        plan.verification_note = "Coverage details changed after the last check. Please verify again."
    flush_or_409(db, "You already saved this plan.")
    audit(db, patient_id, "billing_plan_updated", "insurance_plan", plan.id, {"fields": sorted(data)})
    return plan


def delete_plan(db: Session, patient_id: str, plan_id: str) -> None:
    plan = get_owned(db, InsurancePlan, plan_id, patient_id, "insurance plan")
    db.delete(plan)
    audit(db, patient_id, "billing_plan_deleted", "insurance_plan", plan_id, {"insurer": plan.insurer_name})
    db.flush()


def verify_plan(db: Session, patient_id: str, plan_id: str) -> InsurancePlan:
    """MOCK eligibility check. Deterministic, offline, never contacts an insurer."""
    plan = get_owned(db, InsurancePlan, plan_id, patient_id, "insurance plan")
    t = today()
    mid = plan.member_id.upper().replace("-", "").replace(" ", "")
    if len(mid) < 5 or "INVALID" in mid or mid.endswith("0000"):
        status, note = "failed", "Mock check: the insurer did not recognise this member ID. Check the card and try again."
    elif plan.effective_date > t:
        status, note = "failed", f"Mock check: coverage has not started yet (starts {plan.effective_date.isoformat()})."
    elif plan.end_date is not None and plan.end_date < t:
        status, note = "failed", f"Mock check: coverage ended on {plan.end_date.isoformat()}."
    else:
        status, note = "verified", f"Mock check: coverage looks active on {t.isoformat()}."
    plan.verification_status, plan.verified_at = status, (utcnow() if status == "verified" else None)
    plan.verification_note = note + " No real insurer was contacted (demo)."
    db.flush()
    audit(db, patient_id, "billing_plan_verification_run", "insurance_plan", plan.id, {"result": status, "mock": True})
    return plan


def set_card_image(db: Session, patient_id: str, plan_id: str, side: str, document_id: str | None) -> InsurancePlan:
    """Link an uploaded card photo (stored by W2's document service) to a plan. document_id=None unlinks."""
    plan = get_owned(db, InsurancePlan, plan_id, patient_id, "insurance plan")
    if side not in ("front", "back"):
        raise FieldError({"side": "Choose front or back."})
    if document_id:
        from app.models.documents import Document

        doc = db.get(Document, document_id)
        if doc is None or doc.patient_id != patient_id or doc.deleted_at is not None:
            raise FieldError({"document_id": "We couldn't find that file in your documents."})
    setattr(plan, f"card_{side}_document_id", document_id)
    audit(db, patient_id, "billing_plan_card_linked", "insurance_plan", plan.id, {"side": side})
    db.flush()
    return plan


def list_plans(db: Session, patient_id: str) -> list[InsurancePlan]:
    order = {"primary": 0, "secondary": 1, "tertiary": 2}
    rows = db.scalars(select(InsurancePlan).where(InsurancePlan.patient_id == patient_id)).all()
    return sorted(rows, key=lambda p: (order.get(p.coverage_rank, 9), p.effective_date))


# --------------------------------------------------------------------------- claims / EOBs

def claim_dict(c: BillingClaim, bill_id: str | None = None) -> dict:
    return {
        "id": c.id, "plan_id": c.plan_id, "claim_number": c.claim_number, "insurer_name": c.insurer_name,
        "provider_name": c.provider_name, "service_date": iso(c.service_date),
        "service_description": c.service_description,
        "billed_amount": ms(c.billed_amount), "allowed_amount": ms(c.allowed_amount),
        "insurer_paid": ms(c.insurer_paid), "deductible": ms(c.deductible), "copay": ms(c.copay),
        "coinsurance": ms(c.coinsurance), "patient_responsibility": ms(c.patient_responsibility),
        "status": c.status, "denial_reason": c.denial_reason, "eob_document_id": c.eob_document_id,
        "notes": c.notes, "matched_bill_id": bill_id,
        "created_at": iso(c.created_at), "updated_at": iso(c.updated_at),
    }


def _claim_key(claim_number: str) -> str:
    return norm_text(claim_number).upper()


def _check_plan_owned(db: Session, patient_id: str, plan_id: str | None) -> None:
    if plan_id and get_owned_or_none(db, InsurancePlan, plan_id, patient_id) is None:
        raise FieldError({"plan_id": "We couldn't find that insurance plan."})


def get_owned_or_none(db: Session, model, rid: str, patient_id: str):
    row = db.get(model, rid) if rid else None
    return row if row is not None and row.patient_id == patient_id else None


def _check_doc_owned(db: Session, patient_id: str, field: str, doc_id: str | None) -> None:
    if not doc_id:
        return
    from app.models.documents import Document

    doc = db.get(Document, doc_id)
    if doc is None or doc.patient_id != patient_id or doc.deleted_at is not None:
        raise FieldError({field: "We couldn't find that file in your documents."})


def create_claim(db: Session, patient_id: str, data: dict) -> BillingClaim:
    data = dict(data)
    data["claim_number"] = norm_text(data["claim_number"])
    data["insurer_name"] = norm_text(data["insurer_name"])
    data["provider_name"] = norm_text(data["provider_name"])
    _check_plan_owned(db, patient_id, data.get("plan_id"))
    _check_doc_owned(db, patient_id, "eob_document_id", data.get("eob_document_id"))
    key = _claim_key(data["claim_number"])
    dup = db.scalar(select(BillingClaim).where(BillingClaim.patient_id == patient_id, BillingClaim.claim_key == key))
    if dup:
        raise conflict(f"Claim {dup.claim_number} is already saved ({dup.provider_name}, {dup.service_date.isoformat()}). "
                       "Open it to edit instead of adding it again.", existing_id=dup.id)
    claim = BillingClaim(patient_id=patient_id, claim_key=key, **data)
    db.add(claim)
    flush_or_409(db, f"Claim {data['claim_number']} is already saved.")
    audit(db, patient_id, "billing_claim_created", "billing_claim", claim.id, {"claim_number": claim.claim_number})
    return claim


def update_claim(db: Session, patient_id: str, claim_id: str, data: dict) -> BillingClaim:
    claim = get_owned(db, BillingClaim, claim_id, patient_id, "claim")
    data = dict(data)
    for k in ("claim_number", "insurer_name", "provider_name"):
        if k in data and data[k] is not None:
            data[k] = norm_text(data[k])
    _check_plan_owned(db, patient_id, data.get("plan_id"))
    _check_doc_owned(db, patient_id, "eob_document_id", data.get("eob_document_id"))
    if "claim_number" in data:
        key = _claim_key(data["claim_number"])
        dup = db.scalar(select(BillingClaim).where(BillingClaim.patient_id == patient_id,
                                                   BillingClaim.claim_key == key, BillingClaim.id != claim.id))
        if dup:
            raise conflict(f"Claim {dup.claim_number} is already saved. Open it to edit instead.", existing_id=dup.id)
        claim.claim_key = key
    for k, v in data.items():
        setattr(claim, k, v)
    flush_or_409(db, "That claim number is already saved.")
    audit(db, patient_id, "billing_claim_updated", "billing_claim", claim.id, {"fields": sorted(data)})
    return claim


def delete_claim(db: Session, patient_id: str, claim_id: str) -> None:
    claim = get_owned(db, BillingClaim, claim_id, patient_id, "claim")
    for b in db.scalars(select(BillingBill).where(BillingBill.claim_id == claim.id)).all():
        b.claim_id, b.match_status, b.match_score = None, "unmatched", None
    db.flush()
    db.delete(claim)
    audit(db, patient_id, "billing_claim_deleted", "billing_claim", claim_id, {"claim_number": claim.claim_number})
    db.flush()


def list_claims(db: Session, patient_id: str) -> list[dict]:
    claims = db.scalars(select(BillingClaim).where(BillingClaim.patient_id == patient_id)
                        .order_by(BillingClaim.service_date.desc(), BillingClaim.claim_number)).all()
    matched = dict(db.execute(select(BillingBill.claim_id, BillingBill.id)
                              .where(BillingBill.patient_id == patient_id, BillingBill.claim_id.is_not(None))).all())
    return [claim_dict(c, matched.get(c.id)) for c in claims]


# --------------------------------------------------------------------------- bills

def _bill_key(provider: str, service_date: date, billed: Decimal, account: str | None) -> str:
    return f"{norm_name(provider)}|{service_date.isoformat()}|{D(billed):.2f}|{norm_text(account).upper()}"


def paid_total(db: Session, bill_id: str) -> Decimal:
    v = db.scalar(select(func.coalesce(func.sum(BillingPayment.amount), 0)).where(BillingPayment.bill_id == bill_id))
    return D(v) or ZERO


def refresh_bill_status(db: Session, bill: BillingBill) -> BillingBill:
    """Derive status from payments + disputes. 'void' is sticky."""
    if bill.status == "void":
        return bill
    open_dispute = db.scalar(select(func.count()).select_from(BillingDispute)
                             .where(BillingDispute.bill_id == bill.id, BillingDispute.status == "open"))
    paid = paid_total(db, bill.id)
    if open_dispute:
        bill.status = "in_dispute"
    elif paid >= D(bill.amount_due) or D(bill.amount_due) == ZERO:
        bill.status = "paid"
    elif paid > ZERO:
        bill.status = "partially_paid"
    else:
        bill.status = "unpaid"
    return bill


def bill_dict(db: Session, b: BillingBill, *, paid: Decimal | None = None) -> dict:
    paid = paid_total(db, b.id) if paid is None else paid
    balance = D(b.amount_due) - paid
    t = today()
    days = (b.due_date - t).days if b.due_date else None
    is_open = b.status not in ("paid", "void") and balance > ZERO
    claim = db.get(BillingClaim, b.claim_id) if b.claim_id else None
    return {
        "id": b.id, "provider_name": b.provider_name, "account_number": b.account_number,
        "service_date": iso(b.service_date), "statement_date": iso(b.statement_date),
        "description": b.description, "billed_amount": ms(b.billed_amount), "amount_due": ms(b.amount_due),
        "paid_total": ms(paid), "balance": ms(balance), "due_date": iso(b.due_date),
        "days_until_due": days if is_open else None, "overdue": bool(is_open and days is not None and days < 0),
        "status": b.status, "plan_id": b.plan_id, "claim_id": b.claim_id,
        "claim_number": claim.claim_number if claim else None, "claim_status": claim.status if claim else None,
        "match_status": b.match_status, "match_score": b.match_score, "notes": b.notes,
        "created_at": iso(b.created_at), "updated_at": iso(b.updated_at),
    }


def create_bill(db: Session, patient_id: str, data: dict) -> BillingBill:
    data = dict(data)
    data["provider_name"] = norm_text(data["provider_name"])
    if data.get("account_number"):
        data["account_number"] = norm_text(data["account_number"])
    _check_plan_owned(db, patient_id, data.get("plan_id"))
    key = _bill_key(data["provider_name"], data["service_date"], data["billed_amount"], data.get("account_number"))
    dup = db.scalar(select(BillingBill).where(BillingBill.patient_id == patient_id, BillingBill.dedupe_key == key))
    if dup:
        raise conflict(f"This bill looks like one you already saved ({dup.provider_name}, service on "
                       f"{dup.service_date.isoformat()}, ${D(dup.billed_amount):,.2f}). Open it instead of adding it again.",
                       existing_id=dup.id)
    bill = BillingBill(patient_id=patient_id, dedupe_key=key, status="unpaid", match_status="unmatched", **data)
    db.add(bill)
    flush_or_409(db, "This bill is already saved.")
    refresh_bill_status(db, bill)
    audit(db, patient_id, "billing_bill_created", "billing_bill", bill.id, {"provider": bill.provider_name})
    return bill


def update_bill(db: Session, patient_id: str, bill_id: str, data: dict) -> BillingBill:
    bill = get_owned(db, BillingBill, bill_id, patient_id, "bill")
    data = dict(data)
    if "provider_name" in data and data["provider_name"]:
        data["provider_name"] = norm_text(data["provider_name"])
    _check_plan_owned(db, patient_id, data.get("plan_id"))
    status = data.pop("status", None)  # only 'void' <-> normal is user-settable here
    for k, v in data.items():
        setattr(bill, k, v)
    key = _bill_key(bill.provider_name, bill.service_date, bill.billed_amount, bill.account_number)
    dup = db.scalar(select(BillingBill).where(BillingBill.patient_id == patient_id, BillingBill.dedupe_key == key,
                                              BillingBill.id != bill.id))
    if dup:
        db.rollback()
        raise conflict("Another saved bill has the same provider, service date, price and account number.",
                       existing_id=dup.id)
    bill.dedupe_key = key
    if status == "void":
        bill.status = "void"
    elif status in ("unpaid",) and bill.status == "void":
        bill.status = "unpaid"
    refresh_bill_status(db, bill)
    flush_or_409(db, "Another saved bill has the same details.")
    audit(db, patient_id, "billing_bill_updated", "billing_bill", bill.id, {"fields": sorted(data) + ([status] if status else [])})
    return bill


def delete_bill(db: Session, patient_id: str, bill_id: str) -> None:
    bill = get_owned(db, BillingBill, bill_id, patient_id, "bill")
    db.delete(bill)  # payments + disputes cascade
    audit(db, patient_id, "billing_bill_deleted", "billing_bill", bill_id, {"provider": bill.provider_name})
    db.flush()


def list_bills(db: Session, patient_id: str, status: str | None = None) -> list[dict]:
    q = select(BillingBill).where(BillingBill.patient_id == patient_id)
    if status:
        q = q.where(BillingBill.status == status)
    bills = db.scalars(q.order_by(BillingBill.due_date.is_(None), BillingBill.due_date, BillingBill.service_date.desc())).all()
    paid = dict(db.execute(select(BillingPayment.bill_id, func.sum(BillingPayment.amount))
                           .where(BillingPayment.patient_id == patient_id).group_by(BillingPayment.bill_id)).all())
    return [bill_dict(db, b, paid=D(paid.get(b.id)) or ZERO) for b in bills]


def get_bill(db: Session, patient_id: str, bill_id: str) -> dict:
    bill = get_owned(db, BillingBill, bill_id, patient_id, "bill")
    d = bill_dict(db, bill)
    d["payments"] = [payment_dict(p) for p in list_payments(db, patient_id, bill.id)]
    d["disputes"] = [dispute_dict(x) for x in list_disputes(db, patient_id, bill.id)]
    return d


# --------------------------------------------------------------------------- payments + receipts

def payment_dict(p: BillingPayment) -> dict:
    return {"id": p.id, "bill_id": p.bill_id, "amount": ms(p.amount), "paid_on": iso(p.paid_on), "method": p.method,
            "confirmation_number": p.confirmation_number, "receipt_number": p.receipt_number, "note": p.note,
            "receipt_document_id": p.receipt_document_id, "created_at": iso(p.created_at)}


def list_payments(db: Session, patient_id: str, bill_id: str) -> list[BillingPayment]:
    return list(db.scalars(select(BillingPayment).where(BillingPayment.patient_id == patient_id,
                                                        BillingPayment.bill_id == bill_id)
                           .order_by(BillingPayment.paid_on, BillingPayment.created_at, BillingPayment.id)).all())


def _new_receipt_number(paid_on: date) -> str:
    return f"RCP-{paid_on:%Y%m%d}-{secrets.token_hex(3).upper()}"


def add_payment(db: Session, patient_id: str, bill_id: str, data: dict) -> BillingPayment:
    bill = get_owned(db, BillingBill, bill_id, patient_id, "bill")
    data = dict(data)
    amount = D(data["amount"])
    if bill.status == "void":
        raise conflict("This bill is marked void, so payments can't be added.")
    paid = paid_total(db, bill.id)
    balance = D(bill.amount_due) - paid
    if amount is None or amount <= ZERO:
        raise FieldError({"amount": "Enter an amount greater than $0.00."})
    if amount > balance + Decimal("0.001"):
        raise FieldError({"amount": f"That's more than the balance of ${balance:,.2f}. "
                                    "If you were charged more, record a dispute instead."})
    conf = norm_text(data.get("confirmation_number")) or None
    if conf:
        dup = db.scalar(select(BillingPayment).where(BillingPayment.bill_id == bill.id,
                                                     BillingPayment.confirmation_number == conf))
        if dup:
            raise conflict(f"A payment with confirmation number {conf} is already recorded for this bill "
                           f"(receipt {dup.receipt_number}).", existing_id=dup.id)
    elif not data.get("separate_payment"):
        dup = db.scalar(select(BillingPayment).where(BillingPayment.bill_id == bill.id,
                                                     BillingPayment.amount == amount,
                                                     BillingPayment.paid_on == data["paid_on"],
                                                     BillingPayment.method == data.get("method", "card")))
        if dup:
            raise conflict(f"A payment of ${amount:,.2f} on {dup.paid_on.isoformat()} is already recorded "
                           f"(receipt {dup.receipt_number}). If this is a second payment, confirm it is separate.",
                           existing_id=dup.id, needs_confirmation=True)
    _check_doc_owned(db, patient_id, "receipt_document_id", data.get("receipt_document_id"))
    pay = BillingPayment(patient_id=patient_id, bill_id=bill.id, amount=amount, paid_on=data["paid_on"],
                         method=data.get("method", "card"), confirmation_number=conf, note=data.get("note"),
                         receipt_document_id=data.get("receipt_document_id"),
                         receipt_number=_new_receipt_number(data["paid_on"]))
    db.add(pay)
    flush_or_409(db, "That payment is already recorded.")
    refresh_bill_status(db, bill)
    audit(db, patient_id, "billing_payment_recorded", "billing_payment", pay.id, {"bill_id": bill.id, "amount": ms(amount)})
    return pay


def delete_payment(db: Session, patient_id: str, payment_id: str) -> None:
    pay = get_owned(db, BillingPayment, payment_id, patient_id, "payment")
    bill = db.get(BillingBill, pay.bill_id)
    db.delete(pay)
    db.flush()
    if bill:
        refresh_bill_status(db, bill)
    audit(db, patient_id, "billing_payment_deleted", "billing_payment", payment_id, {"amount": ms(pay.amount)})


def receipt(db: Session, patient_id: str, payment_id: str) -> dict:
    pay = get_owned(db, BillingPayment, payment_id, patient_id, "payment")
    bill = db.get(BillingBill, pay.bill_id)
    patient = db.get(Patient, patient_id)
    before = Decimal("0.00")
    for p in list_payments(db, patient_id, bill.id):
        before += D(p.amount)
        if p.id == pay.id:
            break
    return {
        "receipt_number": pay.receipt_number, "payment_id": pay.id, "paid_on": iso(pay.paid_on),
        "amount": ms(pay.amount), "method": pay.method, "confirmation_number": pay.confirmation_number,
        "patient_name": patient.display_name if patient else None,
        "provider_name": bill.provider_name, "account_number": bill.account_number,
        "service_date": iso(bill.service_date), "description": bill.description,
        "bill_amount_due": ms(bill.amount_due), "balance_after_payment": ms(D(bill.amount_due) - before),
        "note": pay.note,
        "footer": "Receipt record created from the payment details you entered in this app (demo). "
                  "It is not an official receipt from the provider.",
    }


# --------------------------------------------------------------------------- disputes

def dispute_dict(x: BillingDispute) -> dict:
    return {"id": x.id, "bill_id": x.bill_id, "reason_code": x.reason_code, "message": x.message,
            "disputed_amount": ms(x.disputed_amount), "status": x.status, "resolution_note": x.resolution_note,
            "opened_at": iso(x.opened_at), "closed_at": iso(x.closed_at)}


def list_disputes(db: Session, patient_id: str, bill_id: str | None = None) -> list[BillingDispute]:
    q = select(BillingDispute).where(BillingDispute.patient_id == patient_id)
    if bill_id:
        q = q.where(BillingDispute.bill_id == bill_id)
    return list(db.scalars(q.order_by(BillingDispute.opened_at.desc())).all())


def open_dispute(db: Session, patient_id: str, bill_id: str, data: dict) -> BillingDispute:
    bill = get_owned(db, BillingBill, bill_id, patient_id, "bill")
    existing = db.scalar(select(BillingDispute).where(BillingDispute.bill_id == bill.id, BillingDispute.status == "open"))
    if existing:
        raise conflict("There is already an open dispute for this bill. Resolve or withdraw it first.", existing_id=existing.id)
    d = BillingDispute(patient_id=patient_id, bill_id=bill.id, reason_code=data["reason_code"],
                       message=norm_text(data["message"]), disputed_amount=data.get("disputed_amount"),
                       status="open", status_before=bill.status)
    db.add(d)
    flush_or_409(db, "There is already an open dispute for this bill.")
    refresh_bill_status(db, bill)
    audit(db, patient_id, "billing_dispute_opened", "billing_dispute", d.id, {"bill_id": bill.id, "reason": d.reason_code})
    return d


def close_dispute(db: Session, patient_id: str, dispute_id: str, outcome: str, note: str | None) -> BillingDispute:
    d = get_owned(db, BillingDispute, dispute_id, patient_id, "dispute")
    if d.status != "open":
        raise conflict("This dispute is already closed.")
    if outcome not in ("resolved", "withdrawn"):
        raise FieldError({"outcome": "Choose resolved or withdrawn."})
    d.status, d.closed_at, d.resolution_note = outcome, utcnow(), note
    db.flush()
    bill = db.get(BillingBill, d.bill_id)
    if bill:
        refresh_bill_status(db, bill)
    audit(db, patient_id, f"billing_dispute_{outcome}", "billing_dispute", d.id, {"bill_id": d.bill_id})
    return d


# --------------------------------------------------------------------------- explain + summary

def explain_for_bill(db: Session, patient_id: str, bill_id: str) -> dict:
    bill = get_owned(db, BillingBill, bill_id, patient_id, "bill")
    claim = db.get(BillingClaim, bill.claim_id) if bill.claim_id else None
    plan = db.get(InsurancePlan, bill.plan_id or (claim.plan_id if claim else None) or "") if (bill.plan_id or (claim and claim.plan_id)) else None
    others_b = db.scalars(select(BillingBill).where(BillingBill.patient_id == patient_id, BillingBill.id != bill.id)).all()
    others_c = db.scalars(select(BillingClaim).where(BillingClaim.patient_id == patient_id)).all()
    res = explain_bill_eob(bill, claim, plan, payments_total=paid_total(db, bill.id),
                           related_bills=others_b, related_claims=others_c)
    res["bill_id"] = bill.id
    res["claim_id"] = claim.id if claim else None
    return res


def summary(db: Session, patient_id: str) -> dict:
    bills = list_bills(db, patient_id)
    outstanding = sum((D(b["balance"]) for b in bills if b["status"] not in ("paid", "void") and D(b["balance"]) > ZERO), ZERO)
    overdue = [b for b in bills if b["overdue"]]
    due_soon = [b for b in bills if b["days_until_due"] is not None and 0 <= b["days_until_due"] <= 7]
    upcoming = sorted((b for b in bills if b["due_date"] and b["days_until_due"] is not None and b["days_until_due"] >= 0),
                      key=lambda b: b["due_date"])
    claims = list_claims(db, patient_id)
    t = today()
    plans = list_plans(db, patient_id)
    deductible_progress = []
    for p in plans:
        if p.deductible_annual is None:
            continue
        used = ZERO
        for c in claims:
            if c["plan_id"] == p.id and c["deductible"] and c["service_date"][:4] == str(t.year):
                used += D(c["deductible"])
        deductible_progress.append({"plan_id": p.id, "insurer_name": p.insurer_name,
                                    "deductible_annual": ms(p.deductible_annual), "applied_per_claims_you_entered": ms(used),
                                    "estimate": True,
                                    "note": "Estimate from the claims you entered. Your insurer's number is the one that counts."})
    return {
        "total_outstanding": ms(outstanding), "bill_count": len(bills),
        "open_bill_count": sum(1 for b in bills if b["status"] not in ("paid", "void") and D(b["balance"]) > ZERO),
        "overdue_count": len(overdue), "overdue_total": ms(sum((D(b["balance"]) for b in overdue), ZERO)),
        "due_soon_count": len(due_soon), "next_due": upcoming[0] if upcoming else None,
        "unmatched_bills": sum(1 for b in bills if b["match_status"] == "unmatched" and b["status"] != "void"),
        "open_disputes": len([d for d in list_disputes(db, patient_id) if d.status == "open"]),
        "claims_pending": sum(1 for c in claims if c["status"] in ("submitted", "processing")),
        "claims_denied": sum(1 for c in claims if c["status"] in ("denied", "partially_denied")),
        "deductible_progress": deductible_progress,
        "private": True,
    }
