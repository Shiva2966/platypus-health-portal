"""Bill <-> claim (EOB) matching (W8). Deterministic scoring, 0-100.

A claim can be matched to at most ONE bill (DB unique constraint + friendly 409).  Auto-match only links when the
best candidate is strong and clearly better than the runner-up; otherwise the patient chooses.
"""
from __future__ import annotations

import re
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import BillingBill, BillingClaim
from app.services.billing_service import audit, conflict, flush_or_409, get_owned, refresh_bill_status, claim_dict
from app.services.eob_explainer import D

STRONG = 70      # auto-match threshold
POSSIBLE = 40    # listed as a suggestion
MARGIN = 15      # best must beat runner-up by this much to auto-match
_STOP = {"the", "of", "and", "inc", "llc", "ltd", "pc", "pllc", "md", "dr", "co", "corp", "center", "centre"}


def _tokens(s: str | None) -> set[str]:
    return {t for t in re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split() if t and t not in _STOP}


def score_match(bill: BillingBill, claim: BillingClaim) -> tuple[int, list[str]]:
    """Return (score, reasons)."""
    reasons: list[str] = []
    score = 0
    a, b = _tokens(bill.provider_name), _tokens(claim.provider_name)
    if a and b:
        jac = len(a & b) / len(a | b)
        pts = round(40 * jac)
        if pts:
            score += pts
            reasons.append(f"Provider names {'match' if jac == 1 else 'overlap'} ({pts} pts)")
    gap = abs((bill.service_date - claim.service_date).days)
    if gap == 0:
        score += 30
        reasons.append("Same service date (30 pts)")
    elif gap <= 3:
        score += 20
        reasons.append(f"Service dates {gap} day(s) apart (20 pts)")
    elif gap <= 14:
        score += 8
        reasons.append(f"Service dates {gap} days apart (8 pts)")
    if D(bill.billed_amount) == D(claim.billed_amount):
        score += 15
        reasons.append("Same billed amount (15 pts)")
    if claim.patient_responsibility is not None and D(bill.amount_due) == D(claim.patient_responsibility):
        score += 15
        reasons.append("Amount due equals your share on the EOB (15 pts)")
    elif claim.patient_responsibility is not None and D(claim.patient_responsibility) > 0:
        ratio = abs(D(bill.amount_due) - D(claim.patient_responsibility)) / D(claim.patient_responsibility)
        if ratio <= Decimal("0.10"):
            score += 5
            reasons.append("Amount due is within 10% of your share (5 pts)")
    return min(score, 100), reasons


def suggestions(db: Session, patient_id: str, bill_id: str) -> list[dict]:
    bill = get_owned(db, BillingBill, bill_id, patient_id, "bill")
    taken = {cid for (cid,) in db.execute(select(BillingBill.claim_id).where(
        BillingBill.patient_id == patient_id, BillingBill.claim_id.is_not(None), BillingBill.id != bill.id)).all()}
    out = []
    for c in db.scalars(select(BillingClaim).where(BillingClaim.patient_id == patient_id)).all():
        s, why = score_match(bill, c)
        if s >= POSSIBLE:
            out.append({"claim": claim_dict(c), "score": s, "reasons": why, "already_matched_elsewhere": c.id in taken,
                        "strength": "strong" if s >= STRONG else "possible"})
    return sorted(out, key=lambda x: -x["score"])


def link(db: Session, patient_id: str, bill_id: str, claim_id: str, *, method: str = "manual") -> BillingBill:
    bill = get_owned(db, BillingBill, bill_id, patient_id, "bill")
    claim = get_owned(db, BillingClaim, claim_id, patient_id, "claim")
    other = db.scalar(select(BillingBill).where(BillingBill.claim_id == claim.id, BillingBill.id != bill.id))
    if other:
        raise conflict(f"Claim {claim.claim_number} is already matched to another bill ({other.provider_name}). "
                       "Unmatch that bill first.", existing_id=other.id)
    bill.claim_id, bill.match_status = claim.id, "matched"
    bill.match_score = score_match(bill, claim)[0]
    if claim.plan_id and not bill.plan_id:
        bill.plan_id = claim.plan_id
    flush_or_409(db, f"Claim {claim.claim_number} is already matched to another bill.")
    refresh_bill_status(db, bill)
    audit(db, patient_id, "billing_bill_matched", "billing_bill", bill.id, {"claim_id": claim.id, "method": method})
    return bill


def unlink(db: Session, patient_id: str, bill_id: str) -> BillingBill:
    bill = get_owned(db, BillingBill, bill_id, patient_id, "bill")
    if not bill.claim_id:
        raise HTTPException(409, "This bill isn't matched to a claim.")
    old = bill.claim_id
    bill.claim_id, bill.match_status, bill.match_score = None, "unmatched", None
    db.flush()
    audit(db, patient_id, "billing_bill_unmatched", "billing_bill", bill.id, {"claim_id": old})
    return bill


def auto_match(db: Session, patient_id: str, bill_id: str | None = None) -> dict:
    """Link every unmatched bill (or just ``bill_id``) whose best claim is strong, free and clearly ahead."""
    q = select(BillingBill).where(BillingBill.patient_id == patient_id, BillingBill.claim_id.is_(None),
                                  BillingBill.status != "void")
    if bill_id:
        get_owned(db, BillingBill, bill_id, patient_id, "bill")
        q = q.where(BillingBill.id == bill_id)
    matched, skipped = [], []
    for bill in db.scalars(q).all():
        cands = [s for s in suggestions(db, patient_id, bill.id) if not s["already_matched_elsewhere"]]
        if cands and cands[0]["score"] >= STRONG and (len(cands) == 1 or cands[0]["score"] - cands[1]["score"] >= MARGIN):
            link(db, patient_id, bill.id, cands[0]["claim"]["id"], method="auto")
            matched.append({"bill_id": bill.id, "claim_id": cands[0]["claim"]["id"], "score": cands[0]["score"]})
        else:
            skipped.append({"bill_id": bill.id, "reason": "no clear match" if not cands else "more than one possible claim"})
    return {"matched": matched, "needs_review": skipped}
