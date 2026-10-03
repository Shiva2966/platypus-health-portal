"""Staff sessions (separate cookie + table from patient sessions) and role checks."""
import os
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.shared import utcnow
from app.models.staff import STAFF_ROLES, StaffSession, StaffUser
from app.security import hash_token, new_token

STAFF_COOKIE = "hp_staff"
STAFF_SESSION_HOURS = 8

# Which document categories a role may see (None = every authorized category).
FRONT_DESK_CATEGORIES = {"insurance", "billing", "bill", "eob", "identification", "id", "administrative",
                         "admin", "forms", "consent", "insurance_card", "claims"}
ROLE_CAPS = {
    "front_desk": {"identity", "appointments", "documents_admin", "request_access"},
    "nurse": {"identity", "appointments", "documents_clinical", "request_access"},
    "physician": {"identity", "appointments", "documents_clinical", "request_access"},
    "admin": {"audit", "staff_admin"},
}


def role_can(role: str, cap: str) -> bool:
    return cap in ROLE_CAPS.get(role, set())


def create_staff_session(db: Session, staff: StaffUser, request: Request, response: Response) -> None:
    token = new_token()
    db.add(StaffSession(token_hash=hash_token(token), staff_id=staff.id,
                        expires_at=utcnow() + timedelta(hours=STAFF_SESSION_HOURS)))
    db.commit()
    secure = request.url.scheme == "https" or os.environ.get("HP_SECURE_COOKIES") == "1"
    response.set_cookie(STAFF_COOKIE, token, max_age=STAFF_SESSION_HOURS * 3600, httponly=True,
                        samesite="strict", secure=secure, path="/")


def destroy_staff_session(db: Session, request: Request, response: Response) -> None:
    token = request.cookies.get(STAFF_COOKIE)
    if token:
        row = db.get(StaffSession, hash_token(token))
        if row:
            db.delete(row)
            db.commit()
    response.delete_cookie(STAFF_COOKIE, path="/")


def staff_from_request(request: Request, db: Session) -> StaffUser | None:
    token = request.cookies.get(STAFF_COOKIE)
    if not token:
        return None
    sess = db.get(StaffSession, hash_token(token))
    if not sess or sess.expires_at < utcnow():
        return None
    from app import session_idle

    if not session_idle.touch("staff", sess.token_hash, request):
        db.delete(sess)
        db.commit()
        return None
    staff = db.get(StaffUser, sess.staff_id)
    if not staff or not staff.active or staff.role not in STAFF_ROLES:
        return None
    return staff


def current_staff(request: Request, db: Session = Depends(get_db)) -> StaffUser:
    """Dependency: the logged-in StaffUser or 401. Patient cookies are never accepted here."""
    staff = staff_from_request(request, db)
    if not staff:
        raise HTTPException(401, "Please sign in to the staff portal.")
    return staff


def require_role(*roles: str):
    """Dependency factory: `Depends(require_role('nurse', 'physician'))` -> StaffUser or 403."""
    allowed = set(roles)

    def dep(staff: StaffUser = Depends(current_staff)) -> StaffUser:
        if staff.role not in allowed:
            raise HTTPException(403, "Your role does not have access to this.")
        return staff

    return dep
