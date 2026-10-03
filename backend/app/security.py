"""Password hashing, opaque tokens, and patient session handling.

Staff sessions are W3's (app/services/staff_security.py) but may reuse the helpers here
(hash_password, verify_password, new_token, hash_token).
"""
import base64
import hashlib
import hmac
import os
import secrets
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.shared import Patient, utcnow

PATIENT_COOKIE = "hp_patient"
SESSION_HOURS = 8
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


_DUMMY_HASH = hash_password("not-a-real-password")


def burn_password_time(password: str) -> None:
    """Call when the user doesn't exist so timing doesn't reveal which emails exist."""
    verify_password(password, _DUMMY_HASH)


def new_token(nbytes: int = 32) -> str:
    """Opaque URL-safe random token (256 bits by default). Contains no personal data."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---- patient sessions (server-side, so logout really revokes) ----

def create_patient_session(db: Session, patient: Patient, response: Response, request: Request) -> None:
    from app.models.core import PatientSession  # local import: avoids cycle at import time

    token = new_token()
    db.add(PatientSession(token_hash=hash_token(token), patient_id=patient.id,
                          expires_at=utcnow() + timedelta(hours=SESSION_HOURS)))
    db.commit()
    secure = request.url.scheme == "https" or os.environ.get("HP_SECURE_COOKIES") == "1"
    response.set_cookie(PATIENT_COOKIE, token, max_age=SESSION_HOURS * 3600, httponly=True,
                        samesite="lax", secure=secure, path="/")


def destroy_patient_session(db: Session, request: Request, response: Response) -> None:
    from app.models.core import PatientSession

    token = request.cookies.get(PATIENT_COOKIE)
    if token:
        row = db.get(PatientSession, hash_token(token))
        if row:
            db.delete(row)
            db.commit()
    response.delete_cookie(PATIENT_COOKIE, path="/")


def current_patient(request: Request, db: Session = Depends(get_db)) -> Patient:
    """Dependency: the logged-in Patient or 401. Use on EVERY patient route."""
    from app.models.core import PatientSession

    token = request.cookies.get(PATIENT_COOKIE)
    if not token:
        raise HTTPException(401, "Please sign in.")
    sess = db.get(PatientSession, hash_token(token))
    if not sess or sess.expires_at < utcnow():
        raise HTTPException(401, "Your session has expired. Please sign in again.")
    from app import session_idle

    if not session_idle.touch("patient", sess.token_hash, request):
        db.delete(sess)
        db.commit()
        raise HTTPException(401, session_idle.message("patient"))
    patient = db.get(Patient, sess.patient_id)
    if not patient:
        raise HTTPException(401, "Please sign in.")
    return patient


# ---- tiny in-memory login throttle (demo-grade; per process) ----
_FAILS: dict[str, list[float]] = {}


def throttle_check(key: str, limit: int = 6, window: int = 300) -> None:
    import time

    now = time.time()
    hits = [t for t in _FAILS.get(key, []) if now - t < window]
    _FAILS[key] = hits
    if len(hits) >= limit:
        raise HTTPException(429, "Too many failed attempts. Please wait a few minutes and try again.")


def throttle_fail(key: str) -> None:
    import time

    _FAILS.setdefault(key, []).append(time.time())


def throttle_reset(key: str) -> None:
    _FAILS.pop(key, None)
