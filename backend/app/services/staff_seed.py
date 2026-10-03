"""Demo staff accounts (synthetic). `seed_staff(db)` is idempotent and safe for seed.py to call."""
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.shared import Provider
from app.models.staff import StaffUser
from app.services.auth_passwords import hash_password

DEMO_PASSWORD = "Staff-Demo-2026!"

DEMO_PROVIDERS = [
    {"name": "Riverside General Hospital", "specialty": "Hospital", "address": "100 Demo Way, Springfield (synthetic)",
     "phone": "555-0100", "availability": ["Mon-Fri 8-17"]},
    {"name": "Lakeside Family Clinic", "specialty": "Family medicine", "address": "22 Sample Rd, Springfield (synthetic)",
     "phone": "555-0122", "availability": ["Mon 9-12", "Wed 13-17"]},
]

# (name, email, role, provider name)
DEMO_STAFF = [
    ("Frida Front-Desk", "frontdesk@riverside.demo", "front_desk", "Riverside General Hospital"),
    ("Nora Nurse", "nurse@riverside.demo", "nurse", "Riverside General Hospital"),
    ("Dr. Phil Physician", "doctor@riverside.demo", "physician", "Riverside General Hospital"),
    ("Adam Admin", "admin@riverside.demo", "admin", "Riverside General Hospital"),
    ("Dr. Lena Lakeside", "doctor@lakeside.demo", "physician", "Lakeside Family Clinic"),
]


def seed_staff(db: Session, *, quiet: bool = False) -> dict:
    """Create demo providers (if missing) and staff users. Returns {'providers': {...}, 'staff': [...]}."""
    providers: dict[str, str] = {}
    for p in DEMO_PROVIDERS:
        row = db.scalar(select(Provider).where(Provider.name == p["name"]))
        if row is None:
            row = Provider(name=p["name"], specialty=p["specialty"], address=p["address"], phone=p["phone"],
                           availability_json=json.dumps(p["availability"]), cost_estimates_json="[]")
            db.add(row)
            db.flush()
        providers[p["name"]] = row.id

    created = []
    for name, email, role, prov in DEMO_STAFF:
        u = db.scalar(select(StaffUser).where(StaffUser.email == email))
        if u is None:
            u = StaffUser(name=name, email=email, password_hash=hash_password(DEMO_PASSWORD), role=role,
                          provider_id=providers[prov], active=True)
            db.add(u)
            created.append(u)
    db.commit()

    if not quiet:
        print("\nStaff portal demo logins (synthetic) - open /staff/")
        for name, email, role, prov in DEMO_STAFF:
            print(f"  {role:<11} {email:<28} password: {DEMO_PASSWORD}   [{prov}]")
    return {"providers": providers, "staff": [u.email for u in created]}
