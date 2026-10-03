"""Patient sharing / consent endpoints (/api/sharing)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.documents import ShareToken
from app.models.shared import Patient, Provider
from app.security import current_patient
from app.services import consent, files

router = APIRouter(tags=["sharing"])  # what app.main includes
sh = APIRouter(prefix="/api/sharing")
compat = APIRouter()  # exact GETs W1's dashboard expects (see CONTRACT)


class ScopeIn(BaseModel):
    document_ids: list[str] = Field(default_factory=list, max_length=200)
    categories: list[str] = Field(default_factory=list, max_length=20)
    include_private: bool = False


class ShareIn(ScopeIn):
    provider_id: str
    purpose: Optional[str] = Field(None, max_length=300)
    expires_in_days: Optional[int] = Field(7, ge=1, le=365)
    expires_at: Optional[datetime] = None


class TokenIn(ScopeIn):
    purpose: Optional[str] = Field(None, max_length=300)
    expires_in_minutes: int = Field(60, ge=1, le=60 * 24 * 7)
    max_uses: int = Field(1, ge=1, le=25)
    grant_days: int = Field(3, ge=1, le=365)


class ApproveIn(BaseModel):
    categories: Optional[list[str]] = None
    document_ids: Optional[list[str]] = None
    duration_days: Optional[int] = Field(None, ge=1, le=365)
    include_private: bool = True


def _call(fn, *a, **kw):
    """Map service exceptions to HTTP errors without leaking details."""
    try:
        return fn(*a, **kw)
    except LookupError as e:
        raise HTTPException(404, str(e) or "Not found.")
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@sh.get("/providers")
def providers(db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    rows = db.scalars(select(Provider).order_by(Provider.name)).all()
    return {"providers": [{"id": p.id, "name": p.name, "specialty": p.specialty} for p in rows]}


@sh.get("/categories")
def share_categories(patient: Patient = Depends(current_patient)):
    """The single list of categories a grant / share code / request may name, for pickers."""
    from app.models.documents import CATEGORIES, RECORD_CATEGORIES
    return {
        "records": [{"value": c, "label": consent.category_label(c)} for c in RECORD_CATEGORIES],
        # 'billing' documents are covered by the 'billing' record category above
        "documents": [{"value": c, "label": consent.category_label(c)} for c in CATEGORIES if c not in RECORD_CATEGORIES],
    }


@sh.get("/overview")
def overview(db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    consent.run_expiry_sweep(db)
    return {
        "grants": consent.active_grants_for_patient(db, patient.id),
        "pending_requests": consent.list_access_requests(db, patient.id, "pending"),
        "tokens": consent.list_tokens(db, patient.id),
        "retention_note": consent.RETENTION_NOTE,
    }


@sh.post("/preview")
def preview(body: ScopeIn, db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    """Exactly which documents this share would reveal."""
    return _call(consent.preview_scope, db, patient_id=patient.id, categories=body.categories,
                 document_ids=body.document_ids, include_private=body.include_private)


@sh.get("/grants")
def grants(include_inactive: bool = False, db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    if include_inactive:
        return {"grants": consent.all_grants_for_patient(db, patient.id)}
    return {"grants": consent.active_grants_for_patient(db, patient.id)}


@sh.post("/grants", status_code=201)
def create_grant(body: ShareIn, db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    exp = body.expires_at
    if exp is not None and exp.tzinfo is not None:
        exp = exp.astimezone(timezone.utc).replace(tzinfo=None)
    rows = _call(consent.share_with_provider, db, patient_id=patient.id, provider_id=body.provider_id,
                 document_ids=body.document_ids, categories=body.categories, purpose=body.purpose,
                 days=body.expires_in_days, expires_at=exp,
                 include_private=body.include_private)
    return {"grants": [consent.grant_to_dict(db, g) for g in rows]}


@sh.delete("/grants/{grant_id}")
def revoke(grant_id: str, db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    return _call(consent.revoke_grant, db, patient_id=patient.id, grant_id=grant_id)


@sh.get("/requests")
def requests_list(status: Optional[str] = None, db: Session = Depends(get_db),
                  patient: Patient = Depends(current_patient)):
    if status and status not in ("pending", "approved", "denied", "expired"):
        raise HTTPException(422, "Unknown status.")
    return {"requests": consent.list_access_requests(db, patient.id, status)}


@sh.get("/requests/{request_id}/preview")
def request_preview(request_id: str, db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    r = _call(consent.get_access_request, db, patient.id, request_id)
    scope = _call(consent.preview_scope, db, patient_id=patient.id, categories=consent._loads(r.categories_json),
                  document_ids=consent._loads(r.document_ids_json), include_private=True)
    scope["excluded_private_count"] = 0
    return {"request": consent.request_to_dict(db, r), **scope}


@sh.post("/requests/{request_id}/approve")
def approve(request_id: str, body: ApproveIn, db: Session = Depends(get_db),
            patient: Patient = Depends(current_patient)):
    return _call(consent.approve_access_request, db, patient_id=patient.id, request_id=request_id,
                 categories=body.categories, document_ids=body.document_ids,
                 duration_days=body.duration_days, include_private=body.include_private)


@sh.post("/requests/{request_id}/deny")
def deny(request_id: str, db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    return _call(consent.deny_access_request, db, patient_id=patient.id, request_id=request_id)


@sh.post("/tokens", status_code=201)
def create_token(body: TokenIn, db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    """Create a share code. The raw token (and QR) is returned ONCE; only its hash is stored."""
    raw, row = _call(consent.create_share_token, db, patient_id=patient.id, categories=body.categories,
                     document_ids=body.document_ids, purpose=body.purpose,
                     expires_in_minutes=body.expires_in_minutes, max_uses=body.max_uses,
                     grant_days=body.grant_days, include_private=body.include_private)
    return {"id": row.id, "token": raw, "qr_svg": files.qr_svg(raw), "expires_at": consent.iso(row.expires_at),
            "max_uses": row.max_uses, "grant_days": row.grant_days,
            "preview": consent.preview_scope(db, patient_id=patient.id, categories=consent._loads(row.categories_json),
                                             document_ids=consent._loads(row.document_ids_json),
                                             include_private=row.include_private)}


@sh.get("/tokens")
def tokens(db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    return {"tokens": consent.list_tokens(db, patient.id)}


@sh.delete("/tokens/{token_id}")
def revoke_token(token_id: str, db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    _call(consent.revoke_token, db, patient_id=patient.id, token_id=token_id)
    return {"ok": True}


@sh.get("/history")
def history(db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    return {"history": consent.access_history(db, patient.id), "retention_note": consent.RETENTION_NOTE}


@sh.post("/sweep")
def sweep(db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    """Run the expiry sweep now (idempotent)."""
    return consent.run_expiry_sweep(db)


@compat.get("/api/shares/active")
def shares_active(db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    """Active (unrevoked, unexpired) grants as a plain list."""
    return consent.active_grants_for_patient(db, patient.id)


@compat.get("/api/access-requests")
def access_requests(status: Optional[str] = None, db: Session = Depends(get_db),
                    patient: Patient = Depends(current_patient)):
    """Access requests as a plain list (optionally ?status=pending)."""
    if status and status not in ("pending", "approved", "denied", "expired"):
        raise HTTPException(422, "Unknown status.")
    return consent.list_access_requests(db, patient.id, status)


@compat.post("/api/access-requests/{request_id}/approve")
def access_request_approve(request_id: str, body: ApproveIn, db: Session = Depends(get_db),
                           patient: Patient = Depends(current_patient)):
    return approve(request_id, body, db, patient)


@compat.post("/api/access-requests/{request_id}/deny")
def access_request_deny(request_id: str, db: Session = Depends(get_db), patient: Patient = Depends(current_patient)):
    return deny(request_id, db, patient)


router.include_router(sh)
router.include_router(compat)
