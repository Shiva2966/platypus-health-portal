"""Notifications. Adds a row to the caller's session and flushes; the CALLER commits.

This module is the ONE source of truth for notification kinds and their labels (preferences UI, inbox
filters). `notify()` raises ValueError for an unknown recipient type or kind, so a typo fails loudly in
tests instead of creating rows nobody can filter or switch off.
"""
from sqlalchemy.orm import Session

from app.models.shared import Notification

PATIENT_KINDS = ("access_request", "document_viewed", "share_expiring", "share_revoked",
                 "appointment_update", "bill_due", "info_outdated", "doc_uploaded",
                 "record_viewed", "share_redeemed", "correction_update", "caregiver_activity")
STAFF_KINDS = ("document_shared", "access_approved", "access_denied", "access_revoked",
               "appointment_request", "intake_received")
KINDS: dict[str, tuple[str, ...]] = {"patient": PATIENT_KINDS, "staff": STAFF_KINDS}

KIND_LABELS = {
    "access_request": "Hospital access requests", "document_viewed": "A document of mine was viewed",
    "share_expiring": "Sharing is about to expire", "share_revoked": "Sharing was revoked",
    "appointment_update": "Appointment updates & reminders", "bill_due": "Bill due reminders",
    "info_outdated": "Information may be out of date", "doc_uploaded": "Document uploaded",
    "record_viewed": "My health data was viewed", "share_redeemed": "A share code was used",
    "correction_update": "Correction request updates", "caregiver_activity": "What my caregivers did",
    "document_shared": "A patient shared documents", "access_approved": "Access request approved",
    "access_denied": "Access request denied", "access_revoked": "Access revoked by patient",
    "appointment_request": "New appointment requests", "intake_received": "Intake forms received",
}


def notify(
    db: Session,
    *,
    recipient_type: str,
    recipient_id: str,
    kind: str,
    title: str,
    body: str = "",
    link: str | None = None,
) -> Notification:
    if kind not in KINDS.get(recipient_type, ()):
        raise ValueError(f"Unknown notification kind {kind!r} for recipient type {recipient_type!r}")
    if not recipient_id:
        raise ValueError("notify() needs a recipient_id")
    row = Notification(
        recipient_type=recipient_type,
        recipient_id=recipient_id,
        kind=kind,
        title=title[:200],
        body=body or "",
        link=link,
    )
    db.add(row)
    db.flush()
    return row
