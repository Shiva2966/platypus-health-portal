"""Due-date reminders for bills (W8).  Scheduled via app/services/reminders.py -> app/scheduler.py:

    run_bill_reminder_sweep(db)        # idempotent; COMMITS; returns the number of notifications created

Rules: only bills with money still owed (balance > 0) that are unpaid / partially paid.  Bills in dispute are
paused.  One reminder per (due date, phase): "soon" (<= window_days before), "today", and "overdue" (again once a
week).  The dedupe key is stored on the bill (``last_reminder_key``), so re-running never double-notifies.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import BillingBill
from app.services.billing_service import ms, paid_total, today as _today
from app.services.eob_explainer import D
from app.services.notif_helpers import notify_pref

WINDOW_DAYS = 3


def reminder_phase(due: date, today: date, window_days: int = WINDOW_DAYS) -> str | None:
    days = (due - today).days
    if days < 0:
        return f"overdue-w{(-days) // 7}"
    if days == 0:
        return "today"
    if days <= window_days:
        return "soon"
    return None


def run_bill_reminder_sweep(db: Session, *, today: date | None = None, window_days: int = WINDOW_DAYS,
                            patient_id: str | None = None) -> int:
    today = today or _today()
    q = select(BillingBill).where(BillingBill.status.in_(("unpaid", "partially_paid")),
                                  BillingBill.due_date.is_not(None))
    if patient_id:
        q = q.where(BillingBill.patient_id == patient_id)
    sent = 0
    for bill in db.scalars(q).all():
        balance = D(bill.amount_due) - paid_total(db, bill.id)
        if balance <= 0:
            continue
        phase = reminder_phase(bill.due_date, today, window_days)
        if phase is None:
            continue
        key = f"{bill.due_date.isoformat()}:{phase}"
        if bill.last_reminder_key == key:
            continue
        days = (bill.due_date - today).days
        when = ("is due today" if days == 0 else f"is due in {days} day{'s' if days != 1 else ''}" if days > 0
                else f"was due {-days} day{'s' if -days != 1 else ''} ago")
        title = ("Bill overdue: " if days < 0 else "Bill due soon: ") + bill.provider_name
        bill.last_reminder_key = key  # mark even if the patient turned this kind off, so we don't retry daily
        row = notify_pref(db, recipient_type="patient", recipient_id=bill.patient_id, kind="bill_due", title=title,
                    body=f"${ms(balance)} {when} ({bill.due_date.isoformat()}). Open Bills & Insurance to review it first.",
                    link="#/billing")
        if row is not None:
            sent += 1
    db.commit()
    return sent
