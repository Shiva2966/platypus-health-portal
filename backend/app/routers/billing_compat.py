"""Dashboard alias requested by W1: GET /api/bills/summary (patient-only, same data as /api/billing/summary)."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.shared import Patient
from app.security import current_patient
from app.services import billing_service as bs
from app.services.eob_explainer import D

router = APIRouter(tags=["billing-compat"])


@router.get("/api/bills/summary")
def bills_summary(p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    s = bs.summary(db, p.id)
    open_bills = [b for b in bs.list_bills(db, p.id)
                  if b["status"] not in ("paid", "void") and D(b["balance"]) > 0]
    return {
        "total_outstanding": s["total_outstanding"], "count_outstanding": len(open_bills),
        "overdue_count": s["overdue_count"], "due_soon_count": s["due_soon_count"],
        "items": [{"id": b["id"], "provider_name": b["provider_name"], "balance": b["balance"],
                   "due_date": b["due_date"], "claim_status": b["claim_status"], "status": b["status"],
                   "overdue": b["overdue"]} for b in open_bills[:10]],
        "link": "#/billing",
    }
