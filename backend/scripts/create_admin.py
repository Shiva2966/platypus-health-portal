"""Create (or re-lock) a staff ADMIN account whose password nobody knows.

    .venv\\Scripts\\python.exe scripts\\create_admin.py owner@example.com "Owner Name"

The password is a random 48-byte secret that is hashed and immediately discarded, so the account can only
be used after its owner sets a password via "Forgot your password?" on /staff/ (emailed 6-digit code,
POST /api/staff/auth/forgot-password + /reset-password). Re-running on an existing account makes it an
active admin again and replaces its password with a new unknown one (existing sessions are ended).
Prints no secrets.
"""
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select  # noqa: E402

from app.db import SessionLocal, init_db, redacted, get_database_url  # noqa: E402
from app.models.staff import StaffSession, StaffUser  # noqa: E402
from app.services.audit import log_event  # noqa: E402
from app.services.auth_passwords import hash_password  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv or "@" not in argv[0]:
        print(__doc__)
        return 2
    email = argv[0].strip().lower()
    name = (argv[1] if len(argv) > 1 else "Administrator").strip() or "Administrator"
    init_db()
    print(f"database: {redacted(get_database_url())}")
    with SessionLocal() as db:
        u = db.scalar(select(StaffUser).where(func.lower(StaffUser.email) == email))
        unusable = hash_password(secrets.token_urlsafe(48))
        if u is None:
            u = StaffUser(email=email, name=name, role="admin", password_hash=unusable, active=True, provider_id=None)
            db.add(u)
            db.flush()
            action = "staff_created"
        else:
            u.role, u.active, u.password_hash = "admin", True, unusable
            db.execute(delete(StaffSession).where(StaffSession.staff_id == u.id))
            action = "staff_updated"
        log_event(db, actor_type="system", actor_id=None, patient_id=None, action=action, resource_type="staff_user",
                  resource_id=u.id, detail={"role": "admin", "password": "unusable_random_reset_required"})
        db.commit()
        print(f"{action}: admin {email} (no usable password; set one via 'Forgot your password?' on /staff/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
