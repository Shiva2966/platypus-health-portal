"""Reminder sweep (appointment + bill-due reminders, staff inbox catch-up).

`run_reminder_sweep(db)` is idempotent: every reminder is keyed in `reminder_log` (bills: on the bill
row), so running it every few minutes never duplicates. It is scheduled by app/scheduler.py.
"""
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.appointments import Appointment
from app.models.shared import Notification, Patient, Provider, utcnow
from app.models.staff import ReminderLog
from app.services import billing_reminders as _billing_reminders
from app.services.notif_helpers import notify_pref, provider_staff_ids

log = logging.getLogger("health-portal.reminders")
APPT_REMINDER_WINDOW = timedelta(hours=24)
BILL_DUE_WINDOW_DAYS = 3


def _claim(db: Session, key: str) -> bool:
    """Reserve a dedupe key. False if this reminder was already sent."""
    if db.get(ReminderLog, key) is not None:
        return False
    try:
        with db.begin_nested():
            db.add(ReminderLog(key=key))
            db.flush()
    except IntegrityError:
        return False
    return True


def _parse_when(text: str | None) -> datetime | None:
    if not text:
        return None
    t = text.strip().replace("Z", "")
    for candidate in (t, t.replace(" ", "T")):
        try:
            d = datetime.fromisoformat(candidate)
            return d.replace(tzinfo=None) if d.tzinfo else d
        except ValueError:
            continue
    log.warning("unparseable appointment time %r; no reminder sent", text)
    return None


def appointment_reminders(db: Session, now: datetime) -> int:
    n = 0
    rows = db.execute(select(Appointment).where(Appointment.status.in_(("booked", "rescheduled")))).scalars().all()
    for a in rows:
        when = _parse_when(a.scheduled_for)
        if not when or not (now <= when <= now + APPT_REMINDER_WINDOW):
            continue
        if not _claim(db, f"appt24:{a.id}:{a.scheduled_for}"):
            continue
        prov = db.get(Provider, a.provider_id) if a.provider_id else None
        if notify_pref(db, recipient_type="patient", recipient_id=a.patient_id, kind="appointment_update",
                       title="Appointment reminder",
                       body=f"You have an appointment{' with ' + prov.name if prov else ''} at {a.scheduled_for}.",
                       link="#/appointments"):
            n += 1
    return n


def bill_reminders(db: Session, today: date) -> int:
    """`bill_due` reminders are owned by W8 (idempotent via `billing_bills.last_reminder_key`, honours prefs)."""
    return _billing_reminders.run_bill_reminder_sweep(db, today=today, window_days=BILL_DUE_WINDOW_DAYS)


def appointment_inbox_sync(db: Session) -> int:
    """Make sure staff were told about every submitted appointment request (even if the patient-side
    code did not call notify_provider_staff)."""
    n = 0
    rows = db.execute(select(Appointment).where(Appointment.status == "requested",
                                                Appointment.provider_id.is_not(None))).scalars().all()
    for a in rows:
        if not _claim(db, f"apptreq:{a.id}:{a.submitted_at.isoformat() if a.submitted_at else ''}"):
            continue
        patient = db.get(Patient, a.patient_id)
        who = patient.display_name if patient else "A patient"
        since = (a.submitted_at or a.created_at) - timedelta(minutes=2)
        for sid in provider_staff_ids(db, a.provider_id):
            already = db.scalar(select(Notification.id).where(
                Notification.recipient_type == "staff", Notification.recipient_id == sid,
                Notification.kind.in_(("appointment_request", "intake_received")), Notification.ts >= since,
                or_(Notification.title.like(f"%{who}%"), Notification.link.like(f"%{a.id}%"),
                    Notification.body.like(f"%{a.id}%"))).limit(1))
            if already:  # the patient-side code already notified this person
                continue
            if notify_pref(db, recipient_type="staff", recipient_id=sid, kind="appointment_request",
                           title=f"New appointment request from {who}",
                           body="Open the appointment inbox to accept, reschedule or decline.",
                           link=f"#/appointments/{a.id}"):
                n += 1
    return n


def run_reminder_sweep(db: Session) -> dict:
    """Run the three reminder steps once, committing after each. Safe to call repeatedly.
    A failing step is logged with its traceback and reported as "error"; the other steps still run."""
    result: dict = {}
    now = utcnow()
    for name, fn in (("appointment_reminders", lambda: appointment_reminders(db, now)),
                     ("bill_reminders", lambda: bill_reminders(db, now.date())),
                     ("staff_appointment_requests", lambda: appointment_inbox_sync(db))):
        try:
            result[name] = fn()
            db.commit()
        except Exception:
            db.rollback()
            log.exception("reminder step %s failed", name)
            result[name] = "error"
    return result
