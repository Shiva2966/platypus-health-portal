"""Insurance plans (W8): /api/billing/plans.  Patient-only; every query is scoped to the logged-in patient."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.shared import Patient
from app.routers.billing_schemas import CardIn, PlanIn, PlanUpdate
from app.security import current_patient
from app.services import billing_service as bs

router = APIRouter(prefix="/api/billing/plans", tags=["billing-insurance"])

_REQUIRED = {"insurer_name", "member_id", "policyholder_name", "effective_date", "plan_type", "coverage_rank",
             "policyholder_relationship"}


@router.get("")
def list_plans(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    return [bs.plan_dict(x) for x in bs.list_plans(db, p.id)]


@router.post("", status_code=201)
def create_plan(body: PlanIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    plan = bs.create_plan(db, p.id, body.model_dump())
    db.commit()
    return bs.plan_dict(plan)


@router.get("/{plan_id}")
def get_plan(plan_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    from app.models.billing import InsurancePlan

    return bs.plan_dict(bs.get_owned(db, InsurancePlan, plan_id, p.id, "insurance plan"))


@router.put("/{plan_id}")
def update_plan(plan_id: str, body: PlanUpdate, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    data = {k: v for k, v in body.model_dump(exclude_unset=True).items() if not (v is None and k in _REQUIRED)}
    plan = bs.update_plan(db, p.id, plan_id, data)
    db.commit()
    return bs.plan_dict(plan)


@router.delete("/{plan_id}", status_code=204)
def delete_plan(plan_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    bs.delete_plan(db, p.id, plan_id)
    db.commit()


@router.post("/{plan_id}/verify")
def verify_plan(plan_id: str, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    """MOCK eligibility check (offline, deterministic)."""
    plan = bs.verify_plan(db, p.id, plan_id)
    db.commit()
    return bs.plan_dict(plan)


@router.post("/{plan_id}/card")
def link_card(plan_id: str, body: CardIn, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    """Link a card photo already uploaded via POST /api/documents (category 'insurance')."""
    plan = bs.set_card_image(db, p.id, plan_id, body.side, body.document_id)
    db.commit()
    return bs.plan_dict(plan)
