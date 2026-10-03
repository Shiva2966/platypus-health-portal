"""Consent engine (W2): share grants, share tokens, access requests, staff document access.

Public interface (see CONTRACT.md):
    list_visible_documents, get_document_for_staff, create_access_request, redeem_share_token,
    active_grants_for_patient, revoke_grant, approve_access_request, deny_access_request,
    run_expiry_sweep

Conventions
- Functions add rows to the caller's session and COMMIT before returning (so the audit trail of a
  denied attempt survives the exception).
- Grants target a Provider (organization). A staff member sees what their provider has been granted.
- All datetimes are naive UTC.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.documents import (
    CATEGORIES, CLINICAL_RECORD_CATEGORIES, RECORD_CATEGORIES, SHARE_CATEGORIES,
    AccessRequest, Document, DocumentBlob, ShareGrant, ShareToken, ShareTokenAttempt, utcnow,
)
from app.models.shared import AuditLog, Patient, Provider
from app.models.staff import StaffUser
from app.security import hash_token
from app.services.audit import log_event
from app.services.notifications import notify

log = logging.getLogger("health_portal.consent")

MAX_GRANT_DAYS = 365
REQUEST_LIFETIME_DAYS = 7
EXPIRING_SOON = timedelta(hours=24)
DOC_COLS = (
    Document.id, Document.patient_id, Document.name, Document.description, Document.category,
    Document.mime_type, Document.size_bytes, Document.sha256, Document.source, Document.created_at,
    Document.updated_at, Document.is_private, Document.uploaded_by_type, Document.deleted_at,
)


class TooManyAttempts(Exception):
    """Raised by redeem_share_token when a staff member exceeds the failed-attempt limit."""


class InvalidToken(ValueError):
    """Generic failure for unknown / expired / revoked / used-up tokens (deliberately uniform)."""


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


# --------------------------------------------------------------------------- helpers

def _loads(s: str | None) -> list:
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def clean_categories(categories) -> list[str]:
    out = []
    for c in categories or []:
        c = str(c).strip().lower()
        if c in SHARE_CATEGORIES and c not in out:
            out.append(c)
        elif c not in SHARE_CATEGORIES and c:
            raise ValueError(f"Unknown category: {c[:40]}")
    return out


CATEGORY_LABELS = {
    "history": "Medical history", "medications": "Medications", "allergies": "Allergies",
    "vaccinations": "Vaccinations & preventive care", "results": "Test results",
    "billing": "Billing (bills, claims, insurance)", "lab_result": "Lab result documents",
}


def category_label(c: str | None) -> str:
    c = c or ""
    return CATEGORY_LABELS.get(c) or c.replace("_", " ").capitalize()


def _clamp_days(days, default=7) -> int:
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = default
    return max(1, min(days, MAX_GRANT_DAYS))


def staff_provider_id(db: Session, staff_id: str) -> str | None:
    """Provider (organization) a staff user belongs to (W3's staff_users.provider_id)."""
    info = staff_info(db, staff_id)
    return info["provider_id"] if info and info["active"] else None


def staff_info(db: Session, staff_id: str) -> dict | None:
    """{'provider_id','role','active'} straight from staff_users (the authoritative role), or None."""
    s = db.get(StaffUser, staff_id) if staff_id else None
    if s is None:
        return None
    return {"provider_id": s.provider_id or None, "role": s.role, "active": bool(s.active)}


def staff_ids_for_provider(db: Session, provider_id: str) -> list[str]:
    return list(db.scalars(select(StaffUser.id).where(StaffUser.provider_id == provider_id)))


def _staff_name(db: Session, staff_id: str) -> str | None:
    s = db.get(StaffUser, staff_id) if staff_id else None
    return s.name if s else None


def provider_name(db: Session, provider_id: str | None) -> str:
    p = db.get(Provider, provider_id) if provider_id else None
    return p.name if p else "A provider"


def _notify_provider_staff(db: Session, provider_id: str, *, kind: str, title: str, body: str = "",
                           link: str | None = None, extra_ids: list[str] | None = None) -> None:
    ids = list(dict.fromkeys(staff_ids_for_provider(db, provider_id) + [i for i in (extra_ids or []) if i]))
    for sid in ids:
        notify(db, recipient_type="staff", recipient_id=sid, kind=kind, title=title, body=body, link=link)


def _grant_is_active(g: ShareGrant, now: datetime) -> bool:
    return g.revoked_at is None and g.expires_at > now


def _grant_matches_doc(g: ShareGrant, doc) -> bool:
    if doc.deleted_at is not None or doc.patient_id != g.patient_id:
        return False
    if g.scope_type == "document":
        return g.document_id == doc.id
    if g.scope_type == "category":
        return doc.category == g.category and (g.include_private or not doc.is_private)
    return False


def _meta(doc, grant_expires: datetime | None = None) -> dict:
    return {
        "id": doc.id,
        "patient_id": doc.patient_id,
        "name": doc.name,
        "description": doc.description,
        "category": doc.category,
        "mime": doc.mime_type,
        "mime_type": doc.mime_type,
        "size": doc.size_bytes,
        "size_bytes": doc.size_bytes,
        "source": doc.source,
        "is_private": bool(doc.is_private),
        "uploaded_at": iso(doc.created_at),
        "created_at": iso(doc.created_at),
        "updated_at": iso(doc.updated_at),
        "grant_expires_at": iso(grant_expires),
    }


def patient_documents_meta(db: Session, patient_id: str) -> list:
    """Non-deleted document rows (no BLOB loaded) for a patient."""
    return db.execute(
        select(*DOC_COLS).where(Document.patient_id == patient_id, Document.deleted_at.is_(None))
        .order_by(Document.created_at.desc())
    ).all()


def _active_grants(db: Session, patient_id: str, provider_id: str | None = None) -> list[ShareGrant]:
    now = utcnow()
    q = select(ShareGrant).where(
        ShareGrant.patient_id == patient_id, ShareGrant.revoked_at.is_(None), ShareGrant.expires_at > now
    )
    if provider_id:
        q = q.where(ShareGrant.provider_id == provider_id)
    return list(db.scalars(q).all())


def _visible_for_provider(db: Session, patient_id: str, provider_id: str) -> dict[str, tuple]:
    """document_id -> (doc row, latest grant expiry) for everything the provider may currently see."""
    grants = _active_grants(db, patient_id, provider_id)
    if not grants:
        return {}
    out: dict[str, tuple] = {}
    for d in patient_documents_meta(db, patient_id):
        exp = None
        for g in grants:
            if _grant_matches_doc(g, d):
                exp = max(exp, g.expires_at) if exp else g.expires_at
        if exp:
            out[d.id] = (d, exp)
    return out


# --------------------------------------------------------------------------- staff-facing

def list_visible_documents(db: Session, *, staff_id: str, staff_role: str | None, patient_id: str) -> list[dict]:
    """Documents of `patient_id` visible to this staff member's provider via active grants."""
    pid = staff_provider_id(db, staff_id)
    if not pid:
        return []
    vis = _visible_for_provider(db, patient_id, pid)
    return [_meta(d, exp) for d, exp in sorted(vis.values(), key=lambda t: t[0].created_at, reverse=True)]


def get_document_for_staff(db: Session, *, staff_id: str, staff_role: str | None, patient_id: str,
                           document_id: str) -> tuple[dict, bytes]:
    """Return (meta, bytes) if authorized. ALWAYS audits; notifies the patient on success.

    Raises PermissionError (uniform message) when there is no valid grant or the document
    does not exist - callers must not reveal which.
    """
    pid = staff_provider_id(db, staff_id)
    vis = _visible_for_provider(db, patient_id, pid) if pid else {}
    hit = vis.get(document_id)
    if not hit:
        log_event(db, actor_type="staff", actor_id=staff_id, patient_id=patient_id,
                  action="document_access_denied", resource_type="document", resource_id=document_id,
                  detail={"provider_id": pid, "role": staff_role})
        db.commit()
        raise PermissionError("Not authorized to view this document.")
    row = db.get(Document, document_id)
    if row is None or row.deleted_at is not None or row.patient_id != patient_id:
        db.commit()
        raise PermissionError("Not authorized to view this document.")
    meta = _meta(row, hit[1])
    blob = db.get(DocumentBlob, row.id)
    if blob is None:
        db.commit()
        raise PermissionError("Not authorized to view this document.")
    data = bytes(blob.data)
    pname = provider_name(db, pid)
    sname = _staff_name(db, staff_id)
    log_event(db, actor_type="staff", actor_id=staff_id, patient_id=patient_id, action="document_viewed",
              resource_type="document", resource_id=row.id,
              detail={"provider_id": pid, "provider": pname, "staff": sname, "role": staff_role,
                      "document": row.name})
    notify(db, recipient_type="patient", recipient_id=patient_id, kind="document_viewed",
           title=f"{pname} viewed {row.name}",
           body=f"{sname + ' at ' if sname else ''}{pname} opened \"{row.name}\".",
           link="#sharing")
    db.commit()
    return meta, data


def create_access_request(db: Session, *, staff_id: str, patient_id: str, categories=None, document_ids=None,
                          purpose: str | None = None, duration_days: int = 7) -> AccessRequest:
    pid = staff_provider_id(db, staff_id)
    if not pid:
        raise PermissionError("Staff account is not linked to a provider.")
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise LookupError("Patient not found.")
    cats = clean_categories(categories)
    dids = [str(d) for d in (document_ids or [])][:100]
    if not cats and not dids:
        raise ValueError("Select at least one category of records to request.")
    # Never confirm which document ids exist: unknown ids are silently kept out of the stored request.
    if dids:
        valid = {r[0] for r in db.execute(select(Document.id).where(
            Document.id.in_(dids), Document.patient_id == patient_id, Document.deleted_at.is_(None))).all()}
        dids = [d for d in dids if d in valid]
        if not dids and not cats:
            raise ValueError("Select at least one category of records to request.")
    now = utcnow()
    # collapse duplicate pending requests from the same provider
    existing = db.scalar(select(AccessRequest).where(
        AccessRequest.patient_id == patient_id, AccessRequest.provider_id == pid,
        AccessRequest.status == "pending", AccessRequest.expires_at > now))
    if existing and set(_loads(existing.categories_json)) == set(cats) \
            and set(_loads(existing.document_ids_json)) == set(dids):
        return existing
    req = AccessRequest(
        staff_id=staff_id, provider_id=pid, patient_id=patient_id,
        categories_json=json.dumps(cats), document_ids_json=json.dumps(dids),
        purpose=(purpose or "").strip()[:300] or None, duration_days=_clamp_days(duration_days),
        status="pending", created_at=now, expires_at=now + timedelta(days=REQUEST_LIFETIME_DAYS),
    )
    db.add(req)
    db.flush()
    pname = provider_name(db, pid)
    log_event(db, actor_type="staff", actor_id=staff_id, patient_id=patient_id, action="access_requested",
              resource_type="access_request", resource_id=req.id,
              detail={"provider_id": pid, "categories": cats, "document_ids": dids,
                      "purpose": req.purpose, "duration_days": req.duration_days})
    notify(db, recipient_type="patient", recipient_id=patient_id, kind="access_request",
           title=f"{pname} is requesting access to your records",
           body=(req.purpose or "No purpose given") + f" - for {req.duration_days} day(s).",
           link="#sharing")
    db.commit()
    return req


# --- share tokens -----------------------------------------------------------

TOKEN_FAIL_LIMIT = 5
TOKEN_FAIL_WINDOW = 300  # seconds


def reset_token_attempts(db: Session | None = None) -> None:
    """Clear the failed-attempt log (tests / admin)."""
    own = db is None
    if own:
        from app.db import SessionLocal
        db = SessionLocal()
    try:
        db.execute(ShareTokenAttempt.__table__.delete())
        db.commit()
    finally:
        if own:
            db.close()


def _attempt_check(db: Session, key: str) -> None:
    """Raise TooManyAttempts if `key` has too many FAILED redemptions in the window (stored in the DB)."""
    since = utcnow() - timedelta(seconds=TOKEN_FAIL_WINDOW)
    n = db.scalar(select(func.count()).select_from(ShareTokenAttempt).where(
        ShareTokenAttempt.key == key, ShareTokenAttempt.ts > since))
    if (n or 0) >= TOKEN_FAIL_LIMIT:
        raise TooManyAttempts("Too many failed attempts. Please wait a few minutes.")


def _attempt_fail(db: Session, key: str) -> None:
    """Record a failed attempt (caller commits) and opportunistically purge old rows."""
    now = utcnow()
    db.add(ShareTokenAttempt(key=key, ts=now))
    db.execute(ShareTokenAttempt.__table__.delete().where(
        ShareTokenAttempt.ts < now - timedelta(seconds=TOKEN_FAIL_WINDOW * 4)))

def create_share_token(db: Session, *, patient_id: str, categories=None, document_ids=None,
                       purpose: str | None = None, expires_in_minutes: int = 60, max_uses: int = 1,
                       grant_days: int = 3, include_private: bool = False) -> tuple[str, ShareToken]:
    """Create a token. Returns (raw_token, row). The raw token is NEVER stored or retrievable again."""
    cats = clean_categories(categories)
    dids = _owned_document_ids(db, patient_id, document_ids)
    if not cats and not dids:
        raise ValueError("Choose at least one document or category to share.")
    try:
        minutes = max(1, min(int(expires_in_minutes), 60 * 24 * 7))
    except (TypeError, ValueError):
        minutes = 60
    try:
        uses = max(1, min(int(max_uses), 25))
    except (TypeError, ValueError):
        uses = 1
    raw = secrets.token_urlsafe(24)  # 192 random bits, no PII
    row = ShareToken(
        patient_id=patient_id, token_hash=hash_token(raw), categories_json=json.dumps(cats),
        document_ids_json=json.dumps(dids), include_private=bool(include_private),
        purpose=(purpose or "").strip()[:300] or None, grant_days=_clamp_days(grant_days, 3),
        max_uses=uses, use_count=0, expires_at=utcnow() + timedelta(minutes=minutes),
    )
    db.add(row)
    db.flush()
    log_event(db, actor_type="patient", actor_id=patient_id, patient_id=patient_id, action="share_token_created",
              resource_type="share_token", resource_id=row.id,
              detail={"categories": cats, "document_ids": dids, "max_uses": uses, "expires_at": iso(row.expires_at)})
    db.commit()
    return raw, row


def redeem_share_token(db: Session, *, token: str, staff_id: str) -> dict:
    """Redeem a QR/share token for the staff member's provider. Returns a grant summary dict.

    Raises InvalidToken (uniform), TooManyAttempts, PermissionError (staff has no provider).
    """
    key = f"staff:{staff_id}"
    _attempt_check(db, key)
    pid = staff_provider_id(db, staff_id)
    if not pid:
        raise PermissionError("Staff account is not linked to a provider.")
    token = (token or "").strip()
    row = None
    if 16 <= len(token) <= 200:
        candidate = db.scalar(select(ShareToken).where(ShareToken.token_hash == hash_token(token)))
        # constant-time compare of the hashes (guards against any DB-side timing differences)
        if candidate is not None and hmac.compare_digest(candidate.token_hash, hash_token(token)):
            row = candidate
    now = utcnow()
    if row is None or row.revoked_at is not None or row.expires_at <= now or row.use_count >= row.max_uses:
        _attempt_fail(db, key)
        if row is not None:
            log_event(db, actor_type="staff", actor_id=staff_id, patient_id=row.patient_id,
                      action="share_token_rejected", resource_type="share_token", resource_id=row.id,
                      detail={"provider_id": pid})
        db.commit()
        raise InvalidToken("This code is invalid, expired or already used.")
    # atomic use-count increment (prevents racing double use)
    res = db.execute(update(ShareToken).where(ShareToken.id == row.id, ShareToken.use_count < ShareToken.max_uses)
                     .values(use_count=ShareToken.use_count + 1))
    if res.rowcount != 1:
        db.rollback()
        _attempt_fail(db, key)
        db.commit()
        raise InvalidToken("This code is invalid, expired or already used.")
    cats = _loads(row.categories_json)
    dids = _loads(row.document_ids_json)
    expires = now + timedelta(days=row.grant_days)
    grants = _create_grants(db, patient_id=row.patient_id, provider_id=pid, categories=cats, document_ids=dids,
                            purpose=row.purpose, expires_at=expires, include_private=row.include_private,
                            via="token", token_id=row.id)
    pname = provider_name(db, pid)
    sname = _staff_name(db, staff_id)
    log_event(db, actor_type="staff", actor_id=staff_id, patient_id=row.patient_id, action="share_token_redeemed",
              resource_type="share_token", resource_id=row.id,
              detail={"provider_id": pid, "provider": pname, "grants": [g.id for g in grants]})
    notify(db, recipient_type="patient", recipient_id=row.patient_id, kind="share_redeemed",
           title=f"{pname} used your share code",
           body=f"{pname} can view the shared records until {iso(expires)}.", link="#sharing")
    notify(db, recipient_type="staff", recipient_id=staff_id, kind="document_shared",
           title="A patient shared records with you",
           body=f"Access via share code until {iso(expires)}.", link=f"#patient/{row.patient_id}")
    patient = db.get(Patient, row.patient_id)
    db.commit()
    return {
        "patient_id": row.patient_id,
        "patient_name": patient.display_name if patient else None,
        "provider_id": pid,
        "grant_ids": [g.id for g in grants],
        "categories": cats,
        "document_ids": dids,
        "purpose": row.purpose,
        "expires_at": iso(expires),
    }


# --- grants -------------------------------------------------------------------

def _owned_document_ids(db: Session, patient_id: str, document_ids) -> list[str]:
    ids = list(dict.fromkeys(str(d) for d in (document_ids or [])))[:200]
    if not ids:
        return []
    found = {r[0] for r in db.execute(select(Document.id).where(
        Document.id.in_(ids), Document.patient_id == patient_id, Document.deleted_at.is_(None))).all()}
    if len(found) != len(ids):
        # same message whether the doc is missing or belongs to someone else
        raise LookupError("One or more documents were not found.")
    return ids


def _create_grants(db: Session, *, patient_id: str, provider_id: str, categories, document_ids, purpose,
                   expires_at: datetime, include_private: bool, via: str, access_request_id: str | None = None,
                   token_id: str | None = None) -> list[ShareGrant]:
    existing = _active_grants(db, patient_id, provider_id)
    out: list[ShareGrant] = []

    def upsert(scope_type: str, document_id: str | None, category: str | None):
        for g in existing:
            if g.scope_type == scope_type and g.document_id == document_id and g.category == category:
                if expires_at > g.expires_at:
                    g.expires_at = expires_at
                    g.expiry_notified_at = None
                if include_private and not g.include_private:
                    g.include_private = True
                out.append(g)
                return
        g = ShareGrant(patient_id=patient_id, provider_id=provider_id, scope_type=scope_type,
                       document_id=document_id, category=category, include_private=bool(include_private),
                       purpose=(purpose or None), via=via, access_request_id=access_request_id,
                       token_id=token_id, expires_at=expires_at)
        db.add(g)
        out.append(g)

    for c in categories or []:
        upsert("category", None, c)
    for d in document_ids or []:
        upsert("document", d, None)
    db.flush()
    return out


def share_with_provider(db: Session, *, patient_id: str, provider_id: str, document_ids=None, categories=None,
                        purpose: str | None = None, days: int | None = 7, expires_at: datetime | None = None,
                        include_private: bool = False) -> list[ShareGrant]:
    """Patient-initiated share (per-document or per-category) with a provider."""
    if db.get(Provider, provider_id) is None:
        raise LookupError("Provider not found.")
    cats = clean_categories(categories)
    dids = _owned_document_ids(db, patient_id, document_ids)
    if not cats and not dids:
        raise ValueError("Choose at least one document or category to share.")
    now = utcnow()
    exp = expires_at or now + timedelta(days=_clamp_days(days))
    if exp <= now:
        raise ValueError("The expiry must be in the future.")
    if exp > now + timedelta(days=MAX_GRANT_DAYS):
        raise ValueError("The expiry is too far in the future (maximum one year).")
    grants = _create_grants(db, patient_id=patient_id, provider_id=provider_id, categories=cats, document_ids=dids,
                            purpose=purpose, expires_at=exp, include_private=include_private, via="patient")
    pname = provider_name(db, provider_id)
    names = _doc_names(db, dids)
    log_event(db, actor_type="patient", actor_id=patient_id, patient_id=patient_id, action="share_granted",
              resource_type="share_grant", resource_id=grants[0].id if grants else None,
              detail={"provider_id": provider_id, "provider": pname, "categories": cats,
                      "documents": names, "expires_at": iso(exp), "purpose": purpose})
    patient = db.get(Patient, patient_id)
    what = ", ".join(names + [f"all {category_label(c).lower()}" for c in cats]) or "records"
    _notify_provider_staff(db, provider_id, kind="document_shared",
                           title=f"{patient.display_name if patient else 'A patient'} shared records with you",
                           body=f"{what} - until {iso(exp)}.", link=f"#patient/{patient_id}")
    db.commit()
    return grants


def _doc_names(db: Session, ids: list[str]) -> list[str]:
    if not ids:
        return []
    rows = db.execute(select(Document.id, Document.name).where(Document.id.in_(ids))).all()
    m = {r[0]: r[1] for r in rows}
    return [m[i] for i in ids if i in m]


def grant_to_dict(db: Session, g: ShareGrant, now: datetime | None = None) -> dict:
    now = now or utcnow()
    doc_name = None
    if g.scope_type == "document" and g.document_id:
        doc_name = (_doc_names(db, [g.document_id]) or [None])[0]
    status = "revoked" if g.revoked_at else ("expired" if g.expires_at <= now else "active")
    return {
        "id": g.id, "patient_id": g.patient_id, "provider_id": g.provider_id,
        "provider_name": provider_name(db, g.provider_id), "scope_type": g.scope_type,
        "document_id": g.document_id, "document_name": doc_name, "category": g.category,
        "category_label": category_label(g.category) if g.category else None,
        "is_record": g.category in RECORD_CATEGORIES,
        "include_private": bool(g.include_private), "purpose": g.purpose, "via": g.via,
        "expires_at": iso(g.expires_at), "revoked_at": iso(g.revoked_at), "created_at": iso(g.created_at),
        "status": status,
    }


def active_grants_for_patient(db: Session, patient_id: str) -> list[dict]:
    now = utcnow()
    grants = sorted(_active_grants(db, patient_id), key=lambda g: g.created_at, reverse=True)
    return [grant_to_dict(db, g, now) for g in grants]


def all_grants_for_patient(db: Session, patient_id: str, limit: int = 100) -> list[dict]:
    rows = db.scalars(select(ShareGrant).where(ShareGrant.patient_id == patient_id)
                      .order_by(ShareGrant.created_at.desc()).limit(limit)).all()
    now = utcnow()
    return [grant_to_dict(db, g, now) for g in rows]


def revoke_grant(db: Session, *, patient_id: str, grant_id: str) -> dict:
    """Revoke one grant. Blocks FUTURE access only; data already received cannot be recalled."""
    g = db.get(ShareGrant, grant_id)
    if g is None or g.patient_id != patient_id:
        raise LookupError("Grant not found.")
    if g.revoked_at is None:
        g.revoked_at = utcnow()
        pname = provider_name(db, g.provider_id)
        what = (_doc_names(db, [g.document_id]) or ["a document"])[0] if g.scope_type == "document" \
            else f"all {category_label(g.category).lower()}"
        log_event(db, actor_type="patient", actor_id=patient_id, patient_id=patient_id, action="share_revoked",
                  resource_type="share_grant", resource_id=g.id,
                  detail={"provider_id": g.provider_id, "provider": pname, "scope": what})
        patient = db.get(Patient, patient_id)
        extra = []
        if g.access_request_id:
            req = db.get(AccessRequest, g.access_request_id)
            if req and req.staff_id:
                extra.append(req.staff_id)
        _notify_provider_staff(db, g.provider_id, kind="access_revoked",
                               title=f"{patient.display_name if patient else 'A patient'} revoked your access",
                               body=f"Access to {what} was revoked.", link=f"#patient/{patient_id}",
                               extra_ids=extra)
    db.commit()
    d = grant_to_dict(db, g)
    d["retention_note"] = RETENTION_NOTE
    return d


RETENTION_NOTE = ("Revoking stops all future access. Anything the provider already viewed or saved "
                  "before you revoked cannot be taken back.")


def revoke_document_grants(db: Session, *, patient_id: str, document_id: str, provider_id: str | None = None) -> int:
    """Revoke every active document-scoped grant for a document (optionally one provider)."""
    n = 0
    for g in _active_grants(db, patient_id, provider_id):
        if g.scope_type == "document" and g.document_id == document_id:
            revoke_grant(db, patient_id=patient_id, grant_id=g.id)
            n += 1
    return n


def revoke_token(db: Session, *, patient_id: str, token_id: str) -> bool:
    t = db.get(ShareToken, token_id)
    if t is None or t.patient_id != patient_id:
        raise LookupError("Share code not found.")
    if t.revoked_at is None:
        t.revoked_at = utcnow()
        log_event(db, actor_type="patient", actor_id=patient_id, patient_id=patient_id,
                  action="share_token_revoked", resource_type="share_token", resource_id=t.id)
    db.commit()
    return True


def list_tokens(db: Session, patient_id: str) -> list[dict]:
    now = utcnow()
    rows = db.scalars(select(ShareToken).where(ShareToken.patient_id == patient_id)
                      .order_by(ShareToken.created_at.desc()).limit(50)).all()
    out = []
    for t in rows:
        status = "revoked" if t.revoked_at else "used" if t.use_count >= t.max_uses else \
            "expired" if t.expires_at <= now else "active"
        out.append({"id": t.id, "categories": _loads(t.categories_json),
                    "document_ids": _loads(t.document_ids_json), "purpose": t.purpose,
                    "max_uses": t.max_uses, "use_count": t.use_count, "expires_at": iso(t.expires_at),
                    "created_at": iso(t.created_at), "status": status, "grant_days": t.grant_days})
    return out


# --- access requests (patient side) ---------------------------------------------

def request_to_dict(db: Session, r: AccessRequest) -> dict:
    return {
        "id": r.id, "provider_id": r.provider_id, "provider_name": provider_name(db, r.provider_id),
        "staff_id": r.staff_id, "staff_name": _staff_name(db, r.staff_id) if r.staff_id else None,
        "patient_id": r.patient_id, "categories": _loads(r.categories_json),
        "document_ids": _loads(r.document_ids_json), "purpose": r.purpose, "duration_days": r.duration_days,
        "status": r.status, "created_at": iso(r.created_at), "decided_at": iso(r.decided_at),
        "expires_at": iso(r.expires_at),
    }


def list_access_requests(db: Session, patient_id: str, status: str | None = None) -> list[dict]:
    run_expiry_sweep(db)
    q = select(AccessRequest).where(AccessRequest.patient_id == patient_id)
    if status:
        q = q.where(AccessRequest.status == status)
    rows = db.scalars(q.order_by(AccessRequest.created_at.desc()).limit(100)).all()
    return [request_to_dict(db, r) for r in rows]


def get_access_request(db: Session, patient_id: str, request_id: str) -> AccessRequest:
    r = db.get(AccessRequest, request_id)
    if r is None or r.patient_id != patient_id:
        raise LookupError("Request not found.")
    return r


def preview_scope(db: Session, *, patient_id: str, categories=None, document_ids=None,
                  include_private: bool = False) -> dict:
    """Exactly which documents a share with this scope would reveal (shown BEFORE confirming)."""
    cats = clean_categories(categories)
    dids = set(_owned_document_ids(db, patient_id, document_ids))
    docs = []
    excluded_private = 0
    for d in patient_documents_meta(db, patient_id):
        by_doc = d.id in dids
        by_cat = d.category in cats
        if by_doc or (by_cat and (include_private or not d.is_private)):
            docs.append({"id": d.id, "name": d.name, "category": d.category, "mime": d.mime_type,
                         "size": d.size_bytes, "is_private": bool(d.is_private),
                         "description": d.description})
        elif by_cat and d.is_private:
            excluded_private += 1
    return {"documents": docs, "categories": cats, "excluded_private_count": excluded_private,
            "records": record_counts(db, patient_id, [c for c in cats if c in RECORD_CATEGORIES]),
            "note": RETENTION_NOTE}


# --------------------------------------------------------------------------- structured records (W7 + W8)
# Role limits for record categories (documents keep W3's own rules). Billing ALSO needs an explicit
# `billing` grant like every other category - there is no wildcard grant.
#   front_desk : billing only (no clinical data)
#   nurse      : clinical (history, medications, allergies, vaccinations, results) + billing
#   physician  : clinical + billing
#   admin      : NOTHING (audit/staff administration only)
ROLE_RECORD_CATEGORIES: dict[str, frozenset] = {
    "front_desk": frozenset({"billing"}),
    "nurse": frozenset(CLINICAL_RECORD_CATEGORIES + ["billing"]),
    "physician": frozenset(CLINICAL_RECORD_CATEGORIES + ["billing"]),
    "admin": frozenset(),
}


def staff_role_allows(role: str | None, category: str) -> bool:
    return category in ROLE_RECORD_CATEGORIES.get(role or "", frozenset())


def _read_category(db: Session, patient_id: str, category: str) -> list[dict]:
    """Raw read through W7/W8 (lazy imports; NO authorization - callers must have checked)."""
    if category == "billing":
        from app.services import billing_read
        return [{"record_type": "billing", **billing_read.get_billing_for_patient(db, patient_id, for_staff=True)}]
    from app.services import medical_read
    return medical_read.get_category_for_patient(db, patient_id, category)


def record_counts(db: Session, patient_id: str, categories: list[str]) -> list[dict]:
    """Per-category item counts for the 'preview what will be shared' screen (patient side)."""
    out = []
    for c in categories:
        if c not in RECORD_CATEGORIES:
            continue
        entry = {"category": c, "label": category_label(c), "count": 0, "detail": ""}
        try:
            items = _read_category(db, patient_id, c)
            if c == "billing":
                b = items[0]
                parts = [(len(b.get("bills") or []), "bill"), (len(b.get("claims") or []), "claim"),
                         (len(b.get("plans") or []), "insurance plan")]
                entry["count"] = sum(n for n, _ in parts)
                entry["detail"] = ", ".join(f"{n} {w}{'' if n == 1 else 's'}" for n, w in parts)
            elif c == "allergies":
                real = [i for i in items if i.get("record_type") != "allergy_status"]
                entry["count"] = len(real)
                st = next((i for i in items if i.get("record_type") == "allergy_status"), None)
                entry["detail"] = (st or {}).get("status_label") or ((st or {}).get("status") or "").replace("_", " ")
            else:
                entry["count"] = len(items)
        except Exception:  # the share itself still works; the screen says the count is unknown, never "0"
            log.exception("share preview: counting %s failed for patient %s", c, patient_id)
            entry["count"] = None
            entry["detail"] = "Couldn't count these items right now. Everything in this category will still be shared."
        out.append(entry)
    return out


def _granted_record_categories(db: Session, patient_id: str, provider_id: str) -> dict[str, datetime]:
    """record category -> latest expiry of an active category grant for this provider."""
    out: dict[str, datetime] = {}
    for g in _active_grants(db, patient_id, provider_id):
        if g.scope_type == "category" and g.category in RECORD_CATEGORIES:
            out[g.category] = max(out.get(g.category, g.expires_at), g.expires_at)
    return out


def staff_can_read_category(db: Session, *, staff_id: str, patient_id: str, category: str,
                            staff_role: str | None = None) -> bool:
    """True only if the staff member is active, their ROLE may see `category`, and their provider holds an
    active, unexpired, unrevoked grant for it. The role is read from staff_users (authoritative); the
    `staff_role` argument is accepted for signature symmetry and ignored. No audit; use
    get_records_for_staff to actually read."""
    if category not in RECORD_CATEGORIES:
        return False
    info = staff_info(db, staff_id)
    if not info or not info["active"] or not info["provider_id"]:
        return False
    if not staff_role_allows(info["role"], category):
        return False
    return category in _granted_record_categories(db, patient_id, info["provider_id"])


def list_visible_record_categories(db: Session, *, staff_id: str, staff_role: str | None, patient_id: str) -> list[dict]:
    """Which record categories this staff member may open right now - NO data, NO audit
    (cheap enough for page loads): [{category, label, grant_expires_at}]."""
    info = staff_info(db, staff_id)
    if not info or not info["active"] or not info["provider_id"]:
        return []
    granted = _granted_record_categories(db, patient_id, info["provider_id"])
    return [{"category": c, "label": category_label(c), "grant_expires_at": iso(granted[c])}
            for c in RECORD_CATEGORIES if c in granted and staff_role_allows(info["role"], c)]


def get_records_for_staff(db: Session, *, staff_id: str, staff_role: str | None, patient_id: str,
                          category: str) -> list[dict]:
    """Records of ONE category for authorized staff. Raises PermissionError (uniform message) when the
    category is unknown, the role may not see it, or there is no active grant. ALWAYS audited;
    on success the patient is notified (`record_viewed`). Billing is returned as a one-item list
    ``[{"record_type": "billing", plans, bills, claims, disputes, summary}]``. Commits."""
    info = staff_info(db, staff_id) or {}
    pid = info.get("provider_id")
    ok = staff_can_read_category(db, staff_id=staff_id, patient_id=patient_id, category=category)
    if not ok:
        reason = ("unknown_category" if category not in RECORD_CATEGORIES else
                  "role" if info and not staff_role_allows(info.get("role"), category) else "no_grant")
        log_event(db, actor_type="staff", actor_id=staff_id, patient_id=patient_id, action="record_access_denied",
                  resource_type="records", resource_id=str(category)[:64],
                  detail={"provider_id": pid, "role": info.get("role"), "reason": reason})
        db.commit()
        raise PermissionError("Not authorized to view these records.")
    items = _read_category(db, patient_id, category)
    n = len(items[0].get("bills") or []) + len(items[0].get("claims") or []) if category == "billing" else len(items)
    _audit_record_view(db, staff_id, info, patient_id, category, n)
    db.commit()
    return items


def _record_view_window() -> timedelta:
    import os

    try:
        minutes = int(os.environ.get("RECORD_VIEW_NOTIFY_WINDOW_MINUTES", "30"))
    except ValueError:
        minutes = 30
    return timedelta(minutes=max(0, minutes))


def _recently_viewed(db: Session, staff_id: str, patient_id: str, category: str) -> bool:
    window = _record_view_window()
    if not window:
        return False
    since = utcnow() - window
    return db.scalar(
        select(AuditLog.id).where(
            AuditLog.action == "record_viewed", AuditLog.actor_type == "staff", AuditLog.actor_id == staff_id,
            AuditLog.patient_id == patient_id, AuditLog.resource_id == category, AuditLog.ts >= since,
        ).limit(1)
    ) is not None


def _audit_record_view(db: Session, staff_id: str, info: dict, patient_id: str, category: str, n: int) -> None:
    """Every view is audited; the patient notification is coalesced to one per staff member, patient and
    category per RECORD_VIEW_NOTIFY_WINDOW_MINUTES (default 30) so re-opening a tab doesn't spam."""
    pid = info.get("provider_id")
    pname = provider_name(db, pid)
    sname = _staff_name(db, staff_id)
    label = category_label(category)
    already_notified = _recently_viewed(db, staff_id, patient_id, category)
    log_event(db, actor_type="staff", actor_id=staff_id, patient_id=patient_id, action="record_viewed",
              resource_type="records", resource_id=category,
              detail={"provider_id": pid, "provider": pname, "staff": sname, "role": info.get("role"),
                      "category": category, "document": label, "count": n})
    if already_notified:
        return
    notify(db, recipient_type="patient", recipient_id=patient_id, kind="record_viewed",
           title=f"{pname} viewed your {label.lower()}",
           body=f"{sname + ' at ' if sname else ''}{pname} opened your {label.lower()}.", link="#/sharing")


def list_visible_records(db: Session, *, staff_id: str, staff_role: str | None, patient_id: str) -> dict[str, list[dict]]:
    """{category: [items]} for EVERY record category this staff member may see (grant + role). Each
    category returned is audited and notified like get_records_for_staff. Categories without a grant (or
    barred by role) are simply absent. Use list_visible_record_categories for a no-data/no-audit peek."""
    out: dict[str, list[dict]] = {}
    for entry in list_visible_record_categories(db, staff_id=staff_id, staff_role=staff_role, patient_id=patient_id):
        out[entry["category"]] = get_records_for_staff(db, staff_id=staff_id, staff_role=staff_role,
                                                       patient_id=patient_id, category=entry["category"])
    return out


def approve_access_request(db: Session, *, patient_id: str, request_id: str, categories=None, document_ids=None,
                           duration_days: int | None = None, include_private: bool = True) -> dict:
    """Approve (optionally narrowing categories / documents / duration). Creates grants, notifies staff."""
    run_expiry_sweep(db)
    r = get_access_request(db, patient_id, request_id)
    if r.status != "pending":
        raise ValueError(f"This request is already {r.status}.")
    asked_cats, asked_docs = _loads(r.categories_json), _loads(r.document_ids_json)
    cats = asked_cats if categories is None else [c for c in clean_categories(categories) if c in asked_cats]
    if document_ids is None:
        dids = asked_docs
    else:
        want = [str(d) for d in document_ids]
        dids = [d for d in want if d in asked_docs or d in _docs_in_cats(db, patient_id, asked_cats)]
        dids = _owned_document_ids(db, patient_id, dids)
    if not cats and not dids:
        raise ValueError("Select at least one item to approve, or deny the request.")
    days = _clamp_days(duration_days if duration_days else r.duration_days)
    now = utcnow()
    exp = now + timedelta(days=min(days, MAX_GRANT_DAYS))
    grants = _create_grants(db, patient_id=patient_id, provider_id=r.provider_id, categories=cats,
                            document_ids=dids, purpose=r.purpose, expires_at=exp,
                            include_private=include_private, via="access_request", access_request_id=r.id)
    r.status = "approved"
    r.decided_at = now
    patient = db.get(Patient, patient_id)
    log_event(db, actor_type="patient", actor_id=patient_id, patient_id=patient_id, action="access_approved",
              resource_type="access_request", resource_id=r.id,
              detail={"provider_id": r.provider_id, "categories": cats, "document_ids": dids,
                      "expires_at": iso(exp), "grants": [g.id for g in grants]})
    _notify_provider_staff(db, r.provider_id, kind="access_approved",
                           title=f"{patient.display_name if patient else 'The patient'} approved your access request",
                           body=f"Access until {iso(exp)}.", link=f"#patient/{patient_id}", extra_ids=[r.staff_id])
    db.commit()
    out = request_to_dict(db, r)
    out["grant_ids"] = [g.id for g in grants]
    out["grants_expire_at"] = iso(exp)
    return out


def _docs_in_cats(db: Session, patient_id: str, cats: list[str]) -> set[str]:
    return {d.id for d in patient_documents_meta(db, patient_id) if d.category in cats}


def deny_access_request(db: Session, *, patient_id: str, request_id: str) -> dict:
    run_expiry_sweep(db)
    r = get_access_request(db, patient_id, request_id)
    if r.status != "pending":
        raise ValueError(f"This request is already {r.status}.")
    r.status = "denied"
    r.decided_at = utcnow()
    patient = db.get(Patient, patient_id)
    log_event(db, actor_type="patient", actor_id=patient_id, patient_id=patient_id, action="access_denied",
              resource_type="access_request", resource_id=r.id, detail={"provider_id": r.provider_id})
    _notify_provider_staff(db, r.provider_id, kind="access_denied",
                           title=f"{patient.display_name if patient else 'The patient'} declined your access request",
                           body="No records were shared.", link=f"#patient/{patient_id}", extra_ids=[r.staff_id])
    db.commit()
    return request_to_dict(db, r)


def requests_for_staff(db: Session, *, staff_id: str, patient_id: str | None = None) -> list[dict]:
    """Requests made by this staff member's provider (for W3's UI status display)."""
    pid = staff_provider_id(db, staff_id)
    if not pid:
        return []
    run_expiry_sweep(db)
    q = select(AccessRequest).where(AccessRequest.provider_id == pid)
    if patient_id:
        q = q.where(AccessRequest.patient_id == patient_id)
    rows = db.scalars(q.order_by(AccessRequest.created_at.desc()).limit(100)).all()
    return [request_to_dict(db, r) for r in rows]


# --- sweep -----------------------------------------------------------------------

def run_expiry_sweep(db: Session) -> dict:
    """Expire stale pending requests and warn patients (share_expiring) about grants ending within 24h.

    Safe to call often (idempotent). Commits only if something changed.
    """
    now = utcnow()
    changed = False
    expired_requests = 0
    for r in db.scalars(select(AccessRequest).where(
            AccessRequest.status == "pending", AccessRequest.expires_at.is_not(None),
            AccessRequest.expires_at <= now)).all():
        r.status = "expired"
        r.decided_at = now
        expired_requests += 1
        changed = True
    warned = 0
    soon = now + EXPIRING_SOON
    for g in db.scalars(select(ShareGrant).where(
            ShareGrant.revoked_at.is_(None), ShareGrant.expires_at > now, ShareGrant.expires_at <= soon,
            ShareGrant.expiry_notified_at.is_(None))).all():
        what = (_doc_names(db, [g.document_id]) or ["a document"])[0] if g.scope_type == "document" \
            else f"all {category_label(g.category).lower()}"
        notify(db, recipient_type="patient", recipient_id=g.patient_id, kind="share_expiring",
               title=f"Sharing with {provider_name(db, g.provider_id)} ends soon",
               body=f"Access to {what} expires {iso(g.expires_at)}. Open Sharing to review.", link="#sharing")
        g.expiry_notified_at = now
        warned += 1
        changed = True
    if changed:
        db.commit()
    return {"requests_expired": expired_requests, "expiry_warnings": warned}


# --- access history ----------------------------------------------------------------

HISTORY_ACTIONS = (
    "document_viewed", "document_access_denied", "access_requested", "access_approved", "access_denied",
    "share_granted", "share_revoked", "share_token_created", "share_token_redeemed", "share_token_revoked",
    "share_token_rejected", "record_viewed", "record_access_denied",
)


def access_history(db: Session, patient_id: str, limit: int = 100) -> list[dict]:
    rows = db.scalars(select(AuditLog).where(
        AuditLog.patient_id == patient_id, AuditLog.action.in_(HISTORY_ACTIONS))
        .order_by(AuditLog.id.desc()).limit(limit)).all()
    out = []
    for r in rows:
        try:
            detail = json.loads(r.detail) if r.detail else {}
        except ValueError:
            detail = {}
        out.append({"id": r.id, "ts": iso(r.ts), "actor_type": r.actor_type, "action": r.action,
                    "resource_type": r.resource_type, "provider": detail.get("provider"),
                    "document": detail.get("document") or detail.get("scope"),
                    "detail": detail if isinstance(detail, dict) else {}})
    return out
