"""Cost & provider search over the seeded MOCK directory (W8): /api/billing/providers, /api/billing/costs.

Patient-only (uses your plan to estimate your share). All prices are estimate ranges, labeled as such.
"""
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.billing import InsurancePlan
from app.models.shared import Patient
from app.security import current_patient
from app.services import billing_service as bs
from app.services import provider_search as ps

router = APIRouter(prefix="/api/billing", tags=["billing-search"])


def _plan_and_deductible(db: Session, patient_id: str, plan_id: Optional[str]):
    if not plan_id:
        return None, None
    plan = bs.get_owned(db, InsurancePlan, plan_id, patient_id, "insurance plan")
    year = bs.today().year
    used = Decimal("0.00")
    for c in bs.list_claims(db, patient_id):
        if c["plan_id"] == plan.id and c["deductible"] and c["service_date"][:4] == str(year):
            used += Decimal(c["deductible"])
    return plan, used


@router.get("/providers/meta")
def meta():
    return ps.directory_meta()


@router.get("/providers/search")
def search(service: Optional[str] = None, specialty: Optional[str] = None, q: Optional[str] = Query(None, max_length=100),
           area: Optional[str] = None, lat: Optional[float] = Query(None, ge=-90, le=90),
           lon: Optional[float] = Query(None, ge=-180, le=180), max_distance_miles: Optional[float] = Query(None, gt=0, le=500),
           accessibility: list[str] = Query(default=[]), plan_id: Optional[str] = None, in_network_only: bool = False,
           date_from: Optional[date] = None, date_to: Optional[date] = None, sort: str = "distance",
           p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    plan, used = _plan_and_deductible(db, p.id, plan_id)
    try:
        return ps.search_providers(service=service, specialty=specialty, q=q, area=area, lat=lat, lon=lon,
                                   max_distance_miles=max_distance_miles, accessibility=accessibility, plan=plan,
                                   in_network_only=in_network_only, date_from=date_from, date_to=date_to, sort=sort,
                                   deductible_applied=used)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/providers/{provider_id}")
def provider_detail(provider_id: str, service: Optional[str] = None, plan_id: Optional[str] = None,
                    area: Optional[str] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,
                    p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    plan, used = _plan_and_deductible(db, p.id, plan_id)
    res = ps.get_provider(provider_id, service=service, plan=plan, area=area, date_from=date_from, date_to=date_to,
                          deductible_applied=used)
    if res is None:
        raise HTTPException(404, "We couldn't find that provider.")
    return res
