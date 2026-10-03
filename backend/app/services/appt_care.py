"""Caregivers + dependents (W9).

* A caregiver is NOT a patient: separate ``caregiver_accounts`` login, separate cookie/session table.
* Access is per patient via a ``caregiver_links`` row: scoped (record categories / manage appointments / manage
  bills), expiring, revocable.  EVERY check goes through :func:`active_link` which re-evaluates revocation and
  expiry on each request, so revoking or expiring takes effect immediately.
* EVERY caregiver action writes an audit entry (:func:`audit`) and tells the patient (``caregiver_activity``).
* Functions flush but never commit - callers commit (one transaction per request).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import UnmappedColumnError

from app.errors import FieldError
from app.models import billing as billing_models
from app.models import medical as med
from app.models.appointments import (CaregiverAccount, CaregiverLink, CaregiverSession, DependentProfile)
from app.models.shared import Patient, utcnow
from app.security import hash_token, new_token
from app.services import auth_passwords, mailer
from app.services.audit import log_event
from app.services.notifications import notify
from app.settings import dev_codes_visible

log = logging.getLogger("health_portal.caregivers")

# Record categories a caregiver can be allowed to VIEW  (key -> (W7 model attr, label))
RECORD_CATEGORIES = {
    "medications": ("MedMedication", "Medications"),
    "allergies": ("MedAllergy", "Allergies"),
    "history": ("MedHistory", "Medical history"),
    "vaccinations": ("MedVaccination", "Vaccinations & preventive care"),
    "results": ("MedResult", "Test results"),
    "providers": ("MedProvider", "Doctors & care team"),
}
MAX_ACTIVE_LINKS = 10
INVITE_VALID_DAYS = 7
SESSION_HOURS = 8
CAREGIVER_COOKIE = "hp_caregiver"
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


class CareError(Exception):
    """Business-rule problem with an HTTP-ish status (409 conflict, 404 not found, 403 forbidden, 410 gone)."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message, self.status = message, status


# ----------------------------------------------------------------------------- audit + notify

def audit(db: Session, *, link: CaregiverLink, caregiver: CaregiverAccount | None, action: str,
          resource_type: str | None = None, resource_id: str | None = None, detail: dict | None = None,
          notify_patient: bool = False, title: str | None = None) -> None:
    """Audit entry for a caregiver action.  audit_log.actor_type allows only patient|staff|system, so the actor is
    'system' with detail.acting_as='caregiver' + caregiver id/email (see CONTRACT Requests)."""
    d = {"acting_as": "caregiver", "caregiver_id": caregiver.id if caregiver else link.caregiver_id,
         "caregiver_email": caregiver.email if caregiver else link.invite_email, "link_id": link.id, **(detail or {})}
    log_event(db, actor_type="system", actor_id=d["caregiver_id"], patient_id=link.patient_id, action=action,
              resource_type=resource_type, resource_id=resource_id, detail=d)
    if notify_patient:
        name = caregiver.name if caregiver else (link.invite_name or link.invite_email)
        notify(db, recipient_type="patient", recipient_id=link.patient_id, kind="caregiver_activity",
               title=title or f"{name} (caregiver): {action.replace('caregiver_', '').replace('_', ' ')}",
               body="You can see everything caregivers did in your activity log.", link="#/care")


def tell_patient(db: Session, link: CaregiverLink, caregiver: CaregiverAccount, title: str) -> None:
    """Notify the patient that a caregiver did something that changed their data."""
    notify(db, recipient_type="patient", recipient_id=link.patient_id, kind="caregiver_activity",
           title=f"{caregiver.name} (caregiver): {title}"[:200],
           body="You can see everything caregivers did in your activity log.", link="#/care")


# ----------------------------------------------------------------------------- link state

def link_state(link: CaregiverLink, now=None) -> str:
    """invited | invite_expired | active | expired | revoked | declined"""
    now = now or utcnow()
    if link.status == "revoked" or link.revoked_at:
        return "revoked"
    if link.status == "declined":
        return "declined"
    if link.status == "invited":
        return "invite_expired" if link.invite_expires_at < now else "invited"
    return "expired" if link.expires_at < now else "active"


def link_dict(link: CaregiverLink) -> dict:
    return {"id": link.id, "email": link.invite_email, "name": link.invite_name, "relationship": link.relationship_,
            "state": link_state(link), "record_categories": json.loads(link.record_categories_json or "[]"),
            "manage_appointments": bool(link.manage_appointments), "manage_bills": bool(link.manage_bills),
            "invited_at": link.invited_at.isoformat() + "Z", "accepted_at": link.accepted_at.isoformat() + "Z" if link.accepted_at else None,
            "expires_at": link.expires_at.isoformat() + "Z", "revoked_at": link.revoked_at.isoformat() + "Z" if link.revoked_at else None}


def categories_list() -> list[dict]:
    return [{"key": k, "label": v[1]} for k, v in RECORD_CATEGORIES.items()]


def _norm_email(e: str | None) -> str:
    e = (e or "").strip().lower()
    if not EMAIL_RE.match(e) or len(e) > 254:
        raise FieldError({"email": "Enter a valid email address."})
    return e


def _clean_scope(categories, manage_appointments, manage_bills) -> list[str]:
    cats = [c for c in dict.fromkeys(categories or []) if c in RECORD_CATEGORIES]
    if len(cats) != len(list(dict.fromkeys(categories or []))):
        raise FieldError({"record_categories": "Unknown record category."})
    if not cats and not manage_appointments and not manage_bills:
        raise FieldError({"record_categories": "Choose at least one thing this person can do."})
    return cats


# ----------------------------------------------------------------------------- invitations

def _send_invite_email(patient: Patient, link: CaregiverLink, token: str) -> str:
    """Returns 'smtp' | 'dev' | 'failed' (failure is logged; the patient sees it).  No health information in it."""
    base = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")
    url = f"{base}/caregiver?invite={token}"
    try:
        res = mailer.send_notice_email(
            link.invite_email, "You were invited as a caregiver",
            f"{patient.display_name} invited you to help with their health account",
            [f"You can be given limited access until {link.expires_at.date().isoformat()}. "
             f"Open this link within {INVITE_VALID_DAYS} days to accept and create your caregiver login:", url,
             "If you do not know this person, ignore this email - nothing happens unless you accept."])
        return res.mode
    except mailer.MailError as e:
        log.error("caregiver invite email to %s failed: %s", mailer.mask_email(link.invite_email), e)
        return "failed"


def dev_token_visible() -> bool:
    return dev_codes_visible()


def invite_caregiver(db: Session, patient: Patient, *, email: str, name: str | None, relationship: str | None,
                     record_categories, manage_appointments: bool, manage_bills: bool,
                     access_days: int = 90) -> tuple[CaregiverLink, str, str]:
    """Create an invitation.  Returns (link, raw_token, mail_mode).  The raw token exists only in the email."""
    email = _norm_email(email)
    if email == (patient.email or "").lower():
        raise FieldError({"email": "That is your own email address."})
    if not isinstance(access_days, int) or not 1 <= access_days <= 365:
        raise FieldError({"access_days": "Choose between 1 and 365 days."})
    cats = _clean_scope(record_categories, manage_appointments, manage_bills)
    name = (name or "").strip()[:200] or None
    rel = (relationship or "").strip()[:100] or None
    now = utcnow()
    live = db.scalars(select(CaregiverLink).where(CaregiverLink.patient_id == patient.id,
                                                  CaregiverLink.status.in_(("invited", "active")),
                                                  CaregiverLink.revoked_at.is_(None))).all()
    live = [l for l in live if link_state(l, now) in ("invited", "active")]
    if any(l.invite_email == email for l in live):
        raise CareError("This person already has an invitation or access. Revoke it first to start over.")
    if len(live) >= MAX_ACTIVE_LINKS:
        raise CareError(f"You can have at most {MAX_ACTIVE_LINKS} caregivers at once.")
    token = new_token()
    link = CaregiverLink(patient_id=patient.id, invite_email=email, invite_name=name, relationship_=rel,
                         invite_token_hash=hash_token(token), status="invited",
                         record_categories_json=json.dumps(cats), manage_appointments=bool(manage_appointments),
                         manage_bills=bool(manage_bills), invited_at=now,
                         invite_expires_at=now + timedelta(days=INVITE_VALID_DAYS),
                         expires_at=now + timedelta(days=access_days))
    db.add(link)
    db.flush()
    log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="caregiver_invited",
              resource_type="caregiver_link", resource_id=link.id,
              detail={"email": email, "categories": cats, "manage_appointments": bool(manage_appointments),
                      "manage_bills": bool(manage_bills), "expires_at": link.expires_at.isoformat()})
    return link, token, _send_invite_email(patient, link, token)


def resend_invite(db: Session, patient: Patient, link: CaregiverLink) -> tuple[str, str]:
    if link_state(link) not in ("invited", "invite_expired"):
        raise CareError("Only pending invitations can be re-sent.")
    token = new_token()
    link.invite_token_hash = hash_token(token)
    link.invite_expires_at = utcnow() + timedelta(days=INVITE_VALID_DAYS)
    db.flush()
    log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="caregiver_invite_resent",
              resource_type="caregiver_link", resource_id=link.id)
    return token, _send_invite_email(patient, link, token)


def update_scope(db: Session, patient: Patient, link: CaregiverLink, *, record_categories, manage_appointments: bool,
                 manage_bills: bool, access_days: int | None = None) -> CaregiverLink:
    if link_state(link) not in ("invited", "active"):
        raise CareError("This access has ended. Invite the person again.")
    cats = _clean_scope(record_categories, manage_appointments, manage_bills)
    link.record_categories_json = json.dumps(cats)
    link.manage_appointments, link.manage_bills = bool(manage_appointments), bool(manage_bills)
    if access_days is not None:
        if not 1 <= access_days <= 365:
            raise FieldError({"access_days": "Choose between 1 and 365 days."})
        link.expires_at = utcnow() + timedelta(days=access_days)
    db.flush()
    log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="caregiver_scope_changed",
              resource_type="caregiver_link", resource_id=link.id,
              detail={"categories": cats, "manage_appointments": link.manage_appointments, "manage_bills": link.manage_bills})
    return link


def revoke(db: Session, patient: Patient, link: CaregiverLink) -> CaregiverLink:
    """Immediate: the caregiver's next request fails.  Their open sessions stay but have no patients."""
    if link.revoked_at is None:
        link.revoked_at = utcnow()
        link.status = "revoked"
        link.invite_token_hash = None
        db.flush()
        log_event(db, actor_type="patient", actor_id=patient.id, patient_id=patient.id, action="caregiver_revoked",
                  resource_type="caregiver_link", resource_id=link.id, detail={"email": link.invite_email})
    return link


def own_link(db: Session, patient: Patient, link_id: str) -> CaregiverLink:
    link = db.get(CaregiverLink, link_id)
    if link is None or link.patient_id != patient.id:
        raise CareError("Not found.", 404)
    return link


def list_links(db: Session, patient_id: str) -> list[CaregiverLink]:
    return list(db.scalars(select(CaregiverLink).where(CaregiverLink.patient_id == patient_id)
                           .order_by(CaregiverLink.invited_at.desc())))


# ----------------------------------------------------------------------------- caregiver accounts

def _hash(pw: str) -> str:
    return auth_passwords.hash_password(pw)


def _verify(pw: str, stored: str) -> bool:
    return auth_passwords.verify_password(pw, stored)


def _password_problem(pw: str, email: str, name: str) -> str | None:
    return auth_passwords.validate_password(pw, email=email, name=name)


def find_link_by_token(db: Session, token: str) -> CaregiverLink:
    link = db.scalar(select(CaregiverLink).where(CaregiverLink.invite_token_hash == hash_token(token or "")))
    if link is None:
        raise CareError("This invitation link is not valid.", 404)
    return link


def invite_preview(db: Session, token: str) -> dict:
    link = find_link_by_token(db, token)
    st = link_state(link)
    if st != "invited":
        raise CareError("This invitation has expired or is no longer valid. Ask the person to invite you again.", 410)
    p = db.get(Patient, link.patient_id)
    has_account = db.scalar(select(func.count()).select_from(CaregiverAccount).where(
        CaregiverAccount.email == link.invite_email)) > 0
    return {"email": link.invite_email, "invited_by": p.display_name if p else "A patient", "relationship": link.relationship_,
            "record_categories": [RECORD_CATEGORIES[c][1] for c in json.loads(link.record_categories_json)],
            "manage_appointments": link.manage_appointments, "manage_bills": link.manage_bills,
            "access_until": link.expires_at.isoformat() + "Z", "has_account": has_account}


def accept_invite(db: Session, *, token: str, name: str | None, password: str) -> tuple[CaregiverAccount, CaregiverLink]:
    link = find_link_by_token(db, token)
    if link_state(link) != "invited":
        raise CareError("This invitation has expired or is no longer valid. Ask the person to invite you again.", 410)
    acct = db.scalar(select(CaregiverAccount).where(CaregiverAccount.email == link.invite_email))
    if acct is None:
        name = (name or link.invite_name or "").strip()
        if not name:
            raise FieldError({"name": "Enter your name."})
        problem = _password_problem(password, link.invite_email, name)
        if problem:
            raise FieldError({"password": problem})
        acct = CaregiverAccount(email=link.invite_email, name=name[:200], password_hash=_hash(password))
        db.add(acct)
        db.flush()
    else:
        if acct.disabled_at or not _verify(password, acct.password_hash):
            raise CareError("That password is not correct for this caregiver account.", 401)
    link.caregiver_id = acct.id
    link.status = "active"
    link.accepted_at = utcnow()
    link.invite_token_hash = None  # single use
    db.flush()
    audit(db, link=link, caregiver=acct, action="caregiver_accepted_invite", resource_type="caregiver_link",
          resource_id=link.id, notify_patient=True, title=f"{acct.name} accepted your caregiver invitation")
    return acct, link


def decline_invite(db: Session, token: str) -> None:
    link = find_link_by_token(db, token)
    if link_state(link) != "invited":
        raise CareError("This invitation is no longer valid.", 410)
    link.status = "declined"
    link.invite_token_hash = None
    db.flush()
    notify(db, recipient_type="patient", recipient_id=link.patient_id, kind="caregiver_activity",
           title=f"{link.invite_name or link.invite_email} declined your caregiver invitation", link="#/care")


def login(db: Session, email: str, password: str) -> CaregiverAccount:
    acct = db.scalar(select(CaregiverAccount).where(CaregiverAccount.email == (email or "").strip().lower()))
    if acct is None:
        auth_passwords.burn_password_time(password or "")
        raise CareError("Email or password is not correct.", 401)
    if acct.disabled_at or not _verify(password or "", acct.password_hash):
        raise CareError("Email or password is not correct.", 401)
    return acct


def create_session(db: Session, acct: CaregiverAccount) -> str:
    token = new_token()
    db.add(CaregiverSession(token_hash=hash_token(token), caregiver_id=acct.id,
                            expires_at=utcnow() + timedelta(hours=SESSION_HOURS)))
    db.flush()
    return token


def session_account(db: Session, token: str | None) -> CaregiverAccount | None:
    if not token:
        return None
    s = db.get(CaregiverSession, hash_token(token))
    if s is None or s.expires_at < utcnow():
        return None
    acct = db.get(CaregiverAccount, s.caregiver_id)
    return acct if acct and not acct.disabled_at else None


def end_session(db: Session, token: str | None) -> None:
    if token:
        s = db.get(CaregiverSession, hash_token(token))
        if s:
            db.delete(s)


def active_links_for(db: Session, caregiver_id: str) -> list[CaregiverLink]:
    now = utcnow()
    rows = db.scalars(select(CaregiverLink).where(CaregiverLink.caregiver_id == caregiver_id))
    return [l for l in rows if link_state(l, now) == "active"]


def active_link(db: Session, caregiver: CaregiverAccount, patient_id: str, need: str | None = None) -> CaregiverLink:
    """The one authorization gate.  ``need``: None | 'appointments' | 'bills' | 'records:<category>'.
    Raises CareError(403) - the same message whether the patient does not exist or access ended."""
    link = db.scalar(select(CaregiverLink).where(CaregiverLink.caregiver_id == caregiver.id,
                                                 CaregiverLink.patient_id == patient_id))
    if link is None or link_state(link) != "active":
        raise CareError("You do not have access to this person's information.", 403)
    if need == "appointments" and not link.manage_appointments:
        raise CareError("You were not given permission to manage appointments.", 403)
    if need == "bills" and not link.manage_bills:
        raise CareError("You were not given permission to see bills.", 403)
    if need and need.startswith("records:") and need.split(":", 1)[1] not in json.loads(link.record_categories_json or "[]"):
        raise CareError("You were not given permission to see this kind of record.", 403)
    return link


# ----------------------------------------------------------------------------- caregiver data access (read-only)

_SKIP_COLS = ("hash", "password", "secret")


def _row(obj) -> dict:
    """Plain dict of a model row: dates as ISO text, money as text, no BLOBs, no hashes/secrets."""
    out = {}
    mapper = sa_inspect(obj.__class__)
    for c in obj.__table__.columns:
        if any(s in c.key for s in _SKIP_COLS):
            continue
        # QA fix: column key may differ from the mapped attribute name (e.g. relationship_)
        try:
            attr = mapper.get_property_by_column(c).key
        except UnmappedColumnError:
            attr = c.key
        v = getattr(obj, attr)
        if isinstance(v, (bytes, bytearray, memoryview)):
            continue
        out[c.key] = v.isoformat() if hasattr(v, "isoformat") else (str(v) if v.__class__.__name__ == "Decimal" else v)
    return out


def records_for(db: Session, link: CaregiverLink, category: str) -> list[dict]:
    if category not in RECORD_CATEGORIES:
        raise CareError("Unknown record category.", 404)
    model = getattr(med, RECORD_CATEGORIES[category][0])
    rows = db.scalars(select(model).where(model.patient_id == link.patient_id).order_by(model.created_at.desc()).limit(200))
    return [_row(r) for r in rows]


def bills_for(db: Session, link: CaregiverLink) -> dict:
    out = {}
    for key, model in (("bills", billing_models.BillingBill), ("claims", billing_models.BillingClaim)):
        out[key] = [_row(r) for r in db.scalars(select(model).where(model.patient_id == link.patient_id).limit(200))]
    return out


# ----------------------------------------------------------------------------- dependents

def add_dependent(db: Session, guardian: Patient, *, name: str, dob: str | None, relationship: str,
                  notes: str | None = None) -> DependentProfile:
    name = (name or "").strip()
    errs = {}
    if not name:
        errs["name"] = "Enter their name."
    if not (relationship or "").strip():
        errs["relationship"] = "Say how you are related (for example: daughter)."
    if dob:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dob):
            errs["dob"] = "Use the format YYYY-MM-DD."
        elif dob > utcnow().date().isoformat():
            errs["dob"] = "Date of birth cannot be in the future."
    if errs:
        raise FieldError(errs)
    count = db.scalar(select(func.count()).select_from(DependentProfile).where(
        DependentProfile.guardian_patient_id == guardian.id, DependentProfile.ended_at.is_(None)))
    if count >= 10:
        raise CareError("You can manage at most 10 dependent profiles.")
    pid = None
    dep = Patient(email=f"dependent-{os.urandom(8).hex()}@dependents.invalid", password_hash="!no-login",
                  legal_name=name[:200], dob=dob or None, verification_status="unverified")
    db.add(dep)
    db.flush()
    pid = dep.id
    prof = DependentProfile(guardian_patient_id=guardian.id, dependent_patient_id=pid,
                            relationship_=relationship.strip()[:100], notes=(notes or "").strip()[:1000] or None)
    db.add(prof)
    db.flush()
    log_event(db, actor_type="patient", actor_id=guardian.id, patient_id=guardian.id, action="dependent_added",
              resource_type="dependent", resource_id=prof.id)
    log_event(db, actor_type="patient", actor_id=guardian.id, patient_id=pid, action="profile_created_by_guardian",
              resource_type="dependent", resource_id=prof.id)
    return prof


def dependent_dict(db: Session, prof: DependentProfile) -> dict:
    p = db.get(Patient, prof.dependent_patient_id)
    return {"id": prof.id, "patient_id": prof.dependent_patient_id, "name": p.legal_name if p else "",
            "dob": p.dob if p else None, "relationship": prof.relationship_, "notes": prof.notes}


def list_dependents(db: Session, guardian_id: str) -> list[DependentProfile]:
    return list(db.scalars(select(DependentProfile).where(DependentProfile.guardian_patient_id == guardian_id,
                                                          DependentProfile.ended_at.is_(None))
                           .order_by(DependentProfile.created_at)))


def own_dependent(db: Session, guardian: Patient, dep_id: str) -> DependentProfile:
    prof = db.get(DependentProfile, dep_id)
    if prof is None or prof.guardian_patient_id != guardian.id or prof.ended_at is not None:
        raise CareError("Not found.", 404)
    return prof


def update_dependent(db: Session, guardian: Patient, prof: DependentProfile, *, name=None, dob=None, relationship=None,
                     notes=None) -> DependentProfile:
    p = db.get(Patient, prof.dependent_patient_id)
    if name is not None:
        if not name.strip():
            raise FieldError({"name": "Enter their name."})
        p.legal_name = name.strip()[:200]
    if dob is not None:
        if dob and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dob):
            raise FieldError({"dob": "Use the format YYYY-MM-DD."})
        p.dob = dob or None
    if relationship is not None and relationship.strip():
        prof.relationship_ = relationship.strip()[:100]
    if notes is not None:
        prof.notes = notes.strip()[:1000] or None
    db.flush()
    log_event(db, actor_type="patient", actor_id=guardian.id, patient_id=guardian.id, action="dependent_updated",
              resource_type="dependent", resource_id=prof.id)
    return prof


def end_dependent(db: Session, guardian: Patient, prof: DependentProfile) -> None:
    """Stop managing a dependent (e.g. they turned 18).  The profile and its records are kept, not deleted."""
    prof.ended_at = utcnow()
    db.flush()
    log_event(db, actor_type="patient", actor_id=guardian.id, patient_id=guardian.id, action="dependent_removed",
              resource_type="dependent", resource_id=prof.id)


def resolve_subject(db: Session, actor: Patient, for_patient_id: str | None) -> Patient:
    """The patient an action is FOR: the signed-in patient, or one of their active dependents (else 404)."""
    if not for_patient_id or for_patient_id == actor.id:
        return actor
    ok = db.scalar(select(func.count()).select_from(DependentProfile).where(
        DependentProfile.guardian_patient_id == actor.id, DependentProfile.dependent_patient_id == for_patient_id,
        DependentProfile.ended_at.is_(None)))
    p = db.get(Patient, for_patient_id) if ok else None
    if p is None:
        raise CareError("Not found.", 404)
    return p
