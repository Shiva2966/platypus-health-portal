"""Read access to a patient's billing data for OTHER modules (W8).

Billing is PRIVATE to the patient by default.  Staff can see it only while an active, unexpired, unrevoked
share grant with category ``billing`` exists for their organization (W2's ``share_grants``).

    get_billing_for_patient(db, patient_id)        -> dict   no authorization check - call it AFTER you authorized
    staff_has_billing_grant(db, staff_id, patient_id) -> bool
    get_billing_for_staff(db, staff_id=..., patient_id=...) -> dict   checks the grant, audits, notifies the patient;
                                                                     raises PermissionError otherwise

Money in the result is strings with 2 decimals.
"""
from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.shared import Provider, utcnow
from app.services import billing_service as bs
from app.services.audit import log_event
from app.services.notifications import notify

BILLING_CATEGORY = "billing"


def get_billing_for_patient(db: Session, patient_id: str, *, for_staff: bool = False) -> dict:
    """Everything W8 stores for this patient. NO authorization here: the caller must have done it.

    ``for_staff=True`` removes the patient's private free-text (notes, dispute messages, verification notes)."""
    plans = [bs.plan_dict(p) for p in bs.list_plans(db, patient_id)]
    bills = bs.list_bills(db, patient_id)
    claims = bs.list_claims(db, patient_id)
    disputes = [bs.dispute_dict(d) for d in bs.list_disputes(db, patient_id)]
    summary = bs.summary(db, patient_id)
    if for_staff:
        (summary.get("next_due") or {}).pop("notes", None)
        for p in plans:
            p.pop("verification_note", None)
            if p.get("verification_status") not in ("verified", "failed"):
                p["source_label"] = "Patient-entered - not verified"
        for b in bills:
            b.pop("notes", None)
        for c in claims:
            c.pop("notes", None)
        for d in disputes:
            d.pop("message", None)
            d.pop("resolution_note", None)
    return {"patient_id": patient_id, "plans": plans, "bills": bills, "claims": claims, "disputes": disputes,
            "summary": summary}


def _staff_provider_id(db: Session, staff_id: str) -> str | None:
    try:
        row = db.execute(text("SELECT provider_id FROM staff_users WHERE id = :i"), {"i": staff_id}).first()
    except Exception:
        db.rollback()
        return None
    return row[0] if row and row[0] else None


def active_billing_grants(db: Session, patient_id: str) -> list:
    from app.models.documents import ShareGrant

    now = utcnow()
    return list(db.scalars(select(ShareGrant).where(
        ShareGrant.patient_id == patient_id, ShareGrant.scope_type == "category",
        ShareGrant.category == BILLING_CATEGORY, ShareGrant.revoked_at.is_(None),
        ShareGrant.expires_at > now)).all())


def staff_has_billing_grant(db: Session, staff_id: str, patient_id: str) -> bool:
    provider_id = _staff_provider_id(db, staff_id)
    if not provider_id:
        return False
    return any(g.provider_id == provider_id for g in active_billing_grants(db, patient_id))


def get_billing_for_staff(db: Session, *, staff_id: str, patient_id: str, staff_role: str | None = None) -> dict:
    """Authorized staff read. Always audited; the patient is told. Commits (so a denied attempt stays in the log)."""
    allowed = staff_has_billing_grant(db, staff_id, patient_id)
    if not allowed:
        log_event(db, actor_type="staff", actor_id=staff_id, patient_id=patient_id, action="billing_access_denied",
                  resource_type="billing", detail={"reason": "no active billing grant"})
        db.commit()
        raise PermissionError("No shared billing records. The patient has not shared billing with your organization.")
    data = get_billing_for_patient(db, patient_id, for_staff=True)
    provider = db.get(Provider, _staff_provider_id(db, staff_id) or "")
    log_event(db, actor_type="staff", actor_id=staff_id, patient_id=patient_id, action="billing_viewed",
              resource_type="billing", detail={"role": staff_role, "provider": provider.name if provider else None})
    notify(db, recipient_type="patient", recipient_id=patient_id, kind="document_viewed",
           title=f"{provider.name if provider else 'A provider'} viewed your billing records",
           body="You shared billing records with this organization. You can stop sharing any time.", link="#/sharing")
    db.commit()
    return data
