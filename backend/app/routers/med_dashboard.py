"""Small read endpoints other pages use (W7).  Patient-only."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.shared import Patient
from app.security import current_patient
from app.services import medical_read as R

router = APIRouter(tags=["medical"])


@router.get("/api/results/recent")
def recent_results(limit: int = 5, p: Patient = Depends(current_patient), db: Session = Depends(get_db)):
    """Dashboard widget (W1): newest results first.  ``flag`` is 'high'/'low' only when the value is outside the
    reporting lab's own reference range, otherwise null; the UI words it as "Outside lab range" - never as a diagnosis."""
    limit = max(1, min(int(limit or 5), 50))
    out = []
    for r in R.list_results(db, p.id)[:limit]:
        flag = {"above": "high", "below": "low"}.get(r["range_direction"]) if r["outside_range"] else None
        out.append({**r, "name": r["test_name"], "value": r["value_num"] if r["value_num"] is not None else r["value_text"],
                    "date": r["result_date"], "flag": flag, "link": "#/med_results"})
    return out
