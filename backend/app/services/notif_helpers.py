"""W3 notification helpers. Extends (does not replace) app/services/notifications.notify.

`notify_pref` honours the recipient's per-kind preferences (table notification_prefs);
list/count endpoints ALSO hide disabled kinds so rows created by other modules via plain
`notify()` still respect the user's choice.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.shared import Notification, utcnow
from app.models.staff import NotificationPref, StaffUser
from app.services.notifications import KIND_LABELS, KINDS, notify  # noqa: F401  (KIND_LABELS re-exported)


def disabled_kinds(db: Session, recipient_type: str, recipient_id: str) -> set[str]:
    rows = db.execute(select(NotificationPref.kind).where(
        NotificationPref.recipient_type == recipient_type,
        NotificationPref.recipient_id == recipient_id,
        NotificationPref.enabled.is_(False))).scalars().all()
    return set(rows)


def kind_enabled(db: Session, recipient_type: str, recipient_id: str, kind: str) -> bool:
    return kind not in disabled_kinds(db, recipient_type, recipient_id)


def get_prefs(db: Session, recipient_type: str, recipient_id: str) -> list[dict]:
    off = disabled_kinds(db, recipient_type, recipient_id)
    return [{"kind": k, "label": KIND_LABELS.get(k, k), "enabled": k not in off}
            for k in KINDS.get(recipient_type, [])]


def set_prefs(db: Session, recipient_type: str, recipient_id: str, prefs: dict[str, bool]) -> None:
    valid = set(KINDS.get(recipient_type, []))
    for kind, enabled in prefs.items():
        if kind not in valid:
            raise ValueError(f"Unknown notification kind: {kind}")
        row = db.execute(select(NotificationPref).where(
            NotificationPref.recipient_type == recipient_type,
            NotificationPref.recipient_id == recipient_id,
            NotificationPref.kind == kind)).scalar_one_or_none()
        if row:
            row.enabled = bool(enabled)
        else:
            db.add(NotificationPref(recipient_type=recipient_type, recipient_id=recipient_id,
                                    kind=kind, enabled=bool(enabled)))
    db.flush()


def notify_pref(db: Session, *, recipient_type: str, recipient_id: str, kind: str, title: str,
                body: str = "", link: str | None = None) -> Notification | None:
    """Like notify() but returns None (and creates nothing) if the recipient switched this kind off."""
    if not kind_enabled(db, recipient_type, recipient_id, kind):
        return None
    return notify(db, recipient_type=recipient_type, recipient_id=recipient_id, kind=kind,
                  title=title, body=body, link=link)


def provider_staff_ids(db: Session, provider_id: str | None,
                       roles: tuple[str, ...] = ("front_desk", "nurse", "physician")) -> list[str]:
    if not provider_id:
        return []
    return list(db.execute(select(StaffUser.id).where(
        StaffUser.provider_id == provider_id, StaffUser.active.is_(True),
        StaffUser.role.in_(roles))).scalars())


def notify_provider_staff(db: Session, *, provider_id: str | None, kind: str, title: str, body: str = "",
                          link: str | None = None,
                          roles: tuple[str, ...] = ("front_desk", "nurse", "physician")) -> int:
    """Notify every active staff user of a provider (e.g. W1 calls this when a patient submits an
    appointment request). Returns how many notifications were created."""
    n = 0
    for sid in provider_staff_ids(db, provider_id, roles):
        if notify_pref(db, recipient_type="staff", recipient_id=sid, kind=kind, title=title,
                       body=body, link=link):
            n += 1
    return n


def visible_query(db: Session, recipient_type: str, recipient_id: str):
    """Base SELECT for a recipient's notifications, hiding kinds they switched off."""
    q = select(Notification).where(Notification.recipient_type == recipient_type,
                                   Notification.recipient_id == recipient_id)
    off = disabled_kinds(db, recipient_type, recipient_id)
    if off:
        q = q.where(Notification.kind.not_in(off))
    return q


def unread_count(db: Session, recipient_type: str, recipient_id: str) -> int:
    sub = visible_query(db, recipient_type, recipient_id).where(Notification.read_at.is_(None)).subquery()
    return db.execute(select(func.count()).select_from(sub)).scalar_one()


def mark_all_read(db: Session, recipient_type: str, recipient_id: str) -> int:
    rows = db.execute(select(Notification).where(
        Notification.recipient_type == recipient_type, Notification.recipient_id == recipient_id,
        Notification.read_at.is_(None))).scalars().all()
    now = utcnow()
    for r in rows:
        r.read_at = now
    db.flush()
    return len(rows)


def notif_dict(n: Notification) -> dict:
    return {"id": n.id, "ts": n.ts.isoformat() + "Z" if n.ts else None, "kind": n.kind, "title": n.title,
            "body": n.body, "link": n.link, "read": n.read_at is not None,
            "read_at": n.read_at.isoformat() + "Z" if n.read_at else None}
