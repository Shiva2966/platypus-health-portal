"""Bills, claims/EOBs, matching, payments/receipts, disputes, explanations, summary (W8): /api/billing/*.

PRIVATE to the logged-in patient. Staff never reach these routes (patient cookie only); staff access, if the
patient granted category `billing`, goes through app/routers/billing_staff.py.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.billing import BillingClaim
from app.models.shared import Patient
from app.routers.billing_schemas import (BillIn, BillUpdate, ClaimIn, ClaimUpdate, DisputeClose, DisputeIn, MatchIn,
                                         PaymentIn)
from app.security import current_patient
from app.services import billing_match as bm
from app.services import billing_read, billing_reminders
from app.services import billing_service as bs

router = APIRouter(prefix="/api/billing", tags=["billing"])

_CLAIM_REQUIRED = {"claim_number", "insurer_name", "provider_name", "service_date", "billed_amount", "status"}
_BILL_REQUIRED = {"provider_name", "service_date", "billed_amount", "amount_due"}


def _clean(d: dict, required: set) -> dict:
    return {k: v for k, v in d.items() if not (v is None and k in required)}


# ---------------- overview / privacy ----------------

@router.get("/summary")
def summary(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return bs.summary(db, p.id)


@router.get("/privacy")
def privacy(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    """Who (if anyone) can see your billing. Default: nobody but you."""
    from app.models.shared import Provider

    grants = billing_read.active_billing_grants(db, p.id)
    names = {x.id: x.name for x in db.query(Provider).filter(Provider.id.in_([g.provider_id for g in grants] or [""])).all()}
    return {"private_by_default": True, "shared": bool(grants),
            "shared_with": [{"grant_id": g.id, "provider_id": g.provider_id, "provider_name": names.get(g.provider_id),
                             "expires_at": g.expires_at.isoformat(timespec="seconds") + "Z"} for g in grants],
            "how_to_share": "Use Sharing and choose the 'billing' category. You can stop sharing at any time."}


@router.get("/export")
def export_all(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    """Everything W8 stores about you (for W9's privacy export)."""
    return billing_read.get_billing_for_patient(db, p.id)


# ---------------- claims / EOBs ----------------

@router.get("/claims")
def list_claims(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return bs.list_claims(db, p.id)


@router.post("/claims", status_code=201)
def create_claim(body: ClaimIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    claim = bs.create_claim(db, p.id, body.model_dump())
    db.commit()
    return bs.claim_dict(claim)


@router.get("/claims/{claim_id}")
def get_claim(claim_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return bs.claim_dict(bs.get_owned(db, BillingClaim, claim_id, p.id, "claim"))


@router.put("/claims/{claim_id}")
def update_claim(claim_id: str, body: ClaimUpdate, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    claim = bs.update_claim(db, p.id, claim_id, _clean(body.model_dump(exclude_unset=True), _CLAIM_REQUIRED))
    db.commit()
    return bs.claim_dict(claim)


@router.delete("/claims/{claim_id}", status_code=204)
def delete_claim(claim_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    bs.delete_claim(db, p.id, claim_id)
    db.commit()


# ---------------- bills ----------------

@router.get("/bills")
def list_bills(status: Optional[str] = Query(None), p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return bs.list_bills(db, p.id, status)


@router.post("/bills", status_code=201)
def create_bill(body: BillIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    bill = bs.create_bill(db, p.id, body.model_dump())
    db.commit()
    return bs.bill_dict(db, bill)


@router.get("/bills/{bill_id}")
def get_bill(bill_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return bs.get_bill(db, p.id, bill_id)


@router.put("/bills/{bill_id}")
def update_bill(bill_id: str, body: BillUpdate, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    bill = bs.update_bill(db, p.id, bill_id, _clean(body.model_dump(exclude_unset=True), _BILL_REQUIRED))
    db.commit()
    return bs.bill_dict(db, bill)


@router.delete("/bills/{bill_id}", status_code=204)
def delete_bill(bill_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    bs.delete_bill(db, p.id, bill_id)
    db.commit()


@router.get("/bills/{bill_id}/explain")
def explain(bill_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    """Deterministic plain-language explanation (estimate, not legal or financial advice)."""
    return bs.explain_for_bill(db, p.id, bill_id)


# ---------------- matching ----------------

@router.get("/bills/{bill_id}/match-suggestions")
def match_suggestions(bill_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return bm.suggestions(db, p.id, bill_id)


@router.post("/bills/{bill_id}/match")
def match(bill_id: str, body: MatchIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    bill = bm.link(db, p.id, bill_id, body.claim_id)
    db.commit()
    return bs.bill_dict(db, bill)


@router.post("/bills/{bill_id}/unmatch")
def unmatch(bill_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    bill = bm.unlink(db, p.id, bill_id)
    db.commit()
    return bs.bill_dict(db, bill)


@router.post("/match/auto")
def auto_match(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    res = bm.auto_match(db, p.id)
    db.commit()
    return res


# ---------------- payments + receipts ----------------

@router.get("/bills/{bill_id}/payments")
def list_payments(bill_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    from app.models.billing import BillingBill

    bs.get_owned(db, BillingBill, bill_id, p.id, "bill")
    return [bs.payment_dict(x) for x in bs.list_payments(db, p.id, bill_id)]


@router.post("/bills/{bill_id}/payments", status_code=201)
def add_payment(bill_id: str, body: PaymentIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    pay = bs.add_payment(db, p.id, bill_id, body.model_dump())
    db.commit()
    return {**bs.payment_dict(pay), "receipt": bs.receipt(db, p.id, pay.id)}


@router.delete("/payments/{payment_id}", status_code=204)
def delete_payment(payment_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    bs.delete_payment(db, p.id, payment_id)
    db.commit()


@router.get("/payments/{payment_id}/receipt")
def get_receipt(payment_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return bs.receipt(db, p.id, payment_id)


# ---------------- disputes ----------------

@router.get("/disputes")
def list_disputes(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return [bs.dispute_dict(x) for x in bs.list_disputes(db, p.id)]


@router.post("/bills/{bill_id}/dispute", status_code=201)
def open_dispute(bill_id: str, body: DisputeIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    d = bs.open_dispute(db, p.id, bill_id, body.model_dump())
    db.commit()
    return bs.dispute_dict(d)


@router.post("/disputes/{dispute_id}/close")
def close_dispute(dispute_id: str, body: DisputeClose, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    d = bs.close_dispute(db, p.id, dispute_id, body.outcome, body.note)
    db.commit()
    return bs.dispute_dict(d)


# ---------------- reminders ----------------

@router.post("/reminders/run")
def run_my_reminders(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    """Patient-triggered 'check my due dates now' (scoped to this patient). The global sweep is for W3's scheduler."""
    return {"notifications_created": billing_reminders.run_bill_reminder_sweep(db, patient_id=p.id)}
