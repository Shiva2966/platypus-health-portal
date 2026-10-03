"""Create a fresh demo database with SYNTHETIC data only (never real PHI).

    python seed.py                   # only creates missing tables - NO demo data, nothing wiped
    python seed.py --demo            # wipes the database and re-creates the demo data
    python seed.py --demo --keep     # keep existing data, only add what's missing (best effort)
Demo data is refused when APP_ENV=production unless --force is also given.

Each workstream exposes an optional seed function; whichever exist are called (missing ones are skipped):
  app.services.staff_seed.seed_staff(db)              staff users + providers (W3)
  app.services.seed_medical.seed_medical(db, pid)     history, meds, allergies, results (W7)
  app.services.seed_billing.seed_billing(db, pid)     insurance, bills, EOBs (W8)
  app.services.seed_appointments.seed_appointments(db, pid)   (W9)
  app.services.seed_documents.seed_documents(db, pid)         (W2)
  app.services.seed_sharing.seed_sharing(db, pid)             (W2)
"""
import importlib
import os
import sys
from datetime import date, timedelta

from sqlalchemy import select

from app.db import Base, SessionLocal, engine, import_all_models
from app.models.core import EmergencyContact
from app.models.shared import Patient
from app.services.auth_passwords import hash_password
from app.services.audit import log_event
from app.services.notifications import notify

PATIENT_PASSWORD = "Demo-Patient-2026!"

PATIENTS = [
    dict(email="jordan.ellis@example.test", legal_name="Jordan Alexander Ellis", preferred_name="Jordan", dob="1987-03-14",
         phone="555-0142", address="418 Maple Court, Springfield (synthetic)", pronouns="they/them", verification_status="verified",
         verified_note="Identity checked in person at Riverside General Hospital (synthetic).", allergy_status="unknown"),
    dict(email="casey.morgan@example.test", legal_name="Casey Morgan", preferred_name=None, dob="1992-11-02",
         phone=None, address=None, pronouns=None, verification_status="unverified", verified_note=None, allergy_status="unknown"),
]

CONTACTS = [
    dict(name="Riley Ellis", relationship_="sister", phone="555-0177", email="riley.ellis@example.test", is_primary=True, authorized_to_access_records=True,
         notes="Lives nearby (synthetic)."),
    dict(name="Sam Ellis", relationship_="parent", phone="555-0188", email=None, is_primary=False, authorized_to_access_records=False, notes=None),
]

OPTIONAL_SEEDS = [
    ("app.services.seed_medical", "seed_medical"),
    ("app.services.seed_billing", "seed_billing"),
    ("app.services.seed_appointments", "seed_appointments"),
    ("app.services.seed_documents", "seed_documents"),
    ("app.services.seed_sharing", "seed_sharing"),
]


def reset_database():
    import_all_models()
    url = str(engine.url)
    if not url.startswith("sqlite") and "--force" not in sys.argv:
        sys.exit("Refusing to wipe a non-SQLite database without --force.")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def main():
    from app.settings import is_production

    if "--demo" not in sys.argv:
        import_all_models()
        Base.metadata.create_all(engine)
        print("Tables are ready. No demo data was created (use `python seed.py --demo` on a development database).")
        return
    if is_production() and "--force" not in sys.argv:
        sys.exit("APP_ENV=production: refusing to load demo accounts with published passwords. "
                 "Use APP_ENV=development, or add --force if you really mean it.")
    keep = "--keep" in sys.argv
    if keep:
        import_all_models()
        Base.metadata.create_all(engine)
    else:
        reset_database()

    report = []
    with SessionLocal() as db:
        # ---- staff + providers (W3) ----
        try:
            from app.services.staff_seed import seed_staff
            seed_staff(db, quiet=True)
            report.append("staff + providers: ok")
        except ImportError:
            report.append("staff + providers: SKIPPED (staff_seed not present)")

        patient_ids = {}
        for spec in PATIENTS:
            p = db.scalar(select(Patient).where(Patient.email == spec["email"]))
            if p is None:
                p = Patient(password_hash=hash_password(PATIENT_PASSWORD), **spec)
                db.add(p)
                db.flush()
                log_event(db, actor_type="system", actor_id=None, patient_id=p.id, action="demo_patient_created")
            patient_ids[spec["email"]] = p.id

        jordan = patient_ids[PATIENTS[0]["email"]]
        if not db.scalar(select(EmergencyContact.id).where(EmergencyContact.patient_id == jordan)):
            for c in CONTACTS:
                db.add(EmergencyContact(patient_id=jordan, **c))
        notify(db, recipient_type="patient", recipient_id=jordan, kind="info_outdated", title="Welcome to the demo",
               body="Everything here is synthetic. Explore freely.", link="#/dashboard")
        db.commit()

        for mod, fn in OPTIONAL_SEEDS:
            try:
                f = getattr(importlib.import_module(mod), fn)
            except (ImportError, AttributeError):
                report.append(f"{fn}: SKIPPED (not present yet)")
                continue
            try:
                f(db, jordan)
                db.commit()
                report.append(f"{fn}: ok")
            except Exception as e:  # keep seeding the rest; show the problem
                db.rollback()
                report.append(f"{fn}: FAILED -> {e!r}")

    print("\n=== Demo data created (SYNTHETIC, not real people) ===")
    for line in report:
        print("  -", line)
    print("\nPATIENT PORTAL  http://localhost:8000/")
    for spec in PATIENTS:
        print(f"  {spec['email']:<30} password: {PATIENT_PASSWORD}   ({spec['legal_name']})")
    print("\nSTAFF PORTAL    http://localhost:8000/staff/   (see list above if staff were seeded)")
    try:
        from app.services.staff_seed import DEMO_PASSWORD, DEMO_STAFF
        for name, email, role, prov in DEMO_STAFF:
            print(f"  {email:<30} password: {DEMO_PASSWORD}   [{role}, {prov}]")
    except ImportError:
        pass
    print("\nTip: email codes (OTP) print in the server console when SMTP is not configured; "
          "set LOGIN_OTP_REQUIRED=0 to skip the login code for demos.\n")


if __name__ == "__main__":
    main()
