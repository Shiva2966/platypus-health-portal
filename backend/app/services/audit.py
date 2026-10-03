"""Audit logging. Adds a row to the caller's session and flushes; the CALLER commits."""
import json

from app.models.shared import AuditLog


def log_event(
    db,
    *,
    actor_type: str,
    actor_id: str | None,
    patient_id: str | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    detail=None,
) -> AuditLog:
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, default=str)
    row = AuditLog(
        actor_type=actor_type,
        actor_id=actor_id,
        patient_id=patient_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        detail=detail,
    )
    db.add(row)
    db.flush()
    return row
