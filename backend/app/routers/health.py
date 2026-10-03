"""Liveness / readiness probes (W5).

* GET /healthz  - process is up (no DB access; for container liveness).
* GET /readyz   - DB reachable, schema/migrations in the expected state, audit triggers installed.
                  200 when ready, 503 otherwise.  Contains NO patient data and no secrets.
                  Production + public caller: only {"status": "ok"|"unavailable"}. Full checks need the
                  READYZ_TOKEN in an `X-Readyz-Token` (or `Authorization: Bearer`) header, or a direct
                  loopback request.
"""
import hmac
import os
import time

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import auto_migrate_enabled, get_db
from app.db_ops.migrate import migration_state
from app.db_ops.triggers import audit_triggers_present

router = APIRouter(tags=["health"])


@router.get("/healthz", include_in_schema=False)
def healthz(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ok"}


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _detail_allowed(request: Request) -> bool:
    """Full check details: development, a matching READYZ_TOKEN header, or a direct loopback request
    (not one relayed by the local tunnel/proxy, which adds forwarding headers)."""
    from app import settings

    if not settings.is_production():
        return True
    token = (os.environ.get("READYZ_TOKEN") or "").strip()
    given = request.headers.get("x-readyz-token", "")
    if not given and request.headers.get("authorization", "").lower().startswith("bearer "):
        given = request.headers["authorization"][7:]
    if token and given and hmac.compare_digest(given.strip().encode(), token.encode()):
        return True
    relayed = any(h in request.headers for h in ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "forwarded"))
    return bool(request.client) and request.client.host in _LOOPBACK and not relayed


@router.get("/readyz", include_in_schema=False)
def readyz(request: Request, response: Response, db: Session = Depends(get_db)):
    body = _readyz(response, db)
    if _detail_allowed(request):
        return body
    return {"status": "ok"} if response.status_code != 503 else {"status": "unavailable"}


def _readyz(response: Response, db: Session) -> dict:
    response.headers["Cache-Control"] = "no-store"
    checks: dict = {}
    ok = True
    bind = db.get_bind()
    t0 = time.perf_counter()
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = {"ok": True, "engine": bind.dialect.name,
                              "latency_ms": round((time.perf_counter() - t0) * 1000, 2)}
    except Exception as e:
        db.rollback()
        checks["database"] = {"ok": False, "engine": bind.dialect.name, "error": type(e).__name__}
        response.status_code = 503
        return {"status": "unavailable", "checks": checks}

    try:
        st = migration_state(bind.engine if hasattr(bind, "engine") else bind)
        # Required to be at head only when migrations are in charge (AUTO_MIGRATE=1);
        # dev databases created by create_all are reported but accepted.
        mig_ok = st["up_to_date"] or (not st["managed"] and not auto_migrate_enabled())
        checks["migrations"] = {"ok": mig_ok, "mode": "alembic" if st["managed"] else "create_all",
                                "current": st["current"], "head": st["head"]}
        ok &= mig_ok
    except Exception as e:
        checks["migrations"] = {"ok": False, "error": type(e).__name__}
        ok = False

    try:
        conn = db.connection()
        trig = audit_triggers_present(conn)
        checks["audit_append_only"] = {"ok": trig}
        ok &= trig
    except Exception as e:
        db.rollback()
        checks["audit_append_only"] = {"ok": False, "error": type(e).__name__}
        ok = False

    try:
        from app import settings
        from app.services import mailer

        configured = mailer.smtp_configured()
        mail_ok = configured or not settings.is_production()
        checks["mailer"] = {"ok": mail_ok, "configured": configured, "app_env": settings.app_env(),
                            "mode": "smtp" if configured else ("not_configured" if settings.is_production() else "console")}
        ok &= mail_ok
    except Exception as e:
        checks["mailer"] = {"ok": False, "error": type(e).__name__}
        ok = False

    db.rollback()
    if not ok:
        response.status_code = 503
    return {"status": "ready" if ok else "not_ready", "checks": checks}
