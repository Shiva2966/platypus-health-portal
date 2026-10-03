"""App factory. Every router and model module is listed EXPLICITLY (ROUTER_MODULES here, MODEL_MODULES in
app/db.py). An import error in any of them stops the app from starting, and the startup self-check
(app/startup_check.py) refuses to start if a module exists that is not listed, a cross-module service
function is missing, a table is missing or the database is not at the Alembic head."""
import asyncio
import importlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError

from app import hardening, scheduler, settings, startup_check
from app.db import engine, init_db, is_db_locked
from app.errors import FieldError

log = logging.getLogger("health-portal")
STATIC = Path(__file__).resolve().parent / "static"

# Order matters only where two routers could match the same path; keep it alphabetical.
ROUTER_MODULES = (
    "appt_caregiver_portal", "appt_caregivers", "appt_patient", "appt_privacy",
    "auth_otp",
    "billing_bills", "billing_compat", "billing_plans", "billing_providers", "billing_staff",
    "documents",
    "health",
    "med_corrections", "med_dashboard", "med_lab_import", "med_prescriptions", "med_records",
    "notifications",
    "patient_profile",
    "sharing",
    "staff_admin", "staff_appointments", "staff_auth", "staff_patients", "staff_records",
)


@asynccontextmanager
async def lifespan(app_: FastAPI):
    init_db()
    startup_check.run(app_, engine, ROUTER_MODULES)
    scheduler.start()
    try:
        yield
    finally:
        await asyncio.to_thread(scheduler.stop)


app = FastAPI(title="Health Records Portal", lifespan=lifespan, **hardening.fastapi_kwargs())

for _name in ROUTER_MODULES:
    app.include_router(importlib.import_module(f"app.routers.{_name}").router)

init_db()  # also at import time so tests / scripts using TestClient without lifespan work

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; object-src 'self' blob:; "
    "frame-src 'self' blob:; connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # CSRF defence in depth (cookies are also SameSite=Lax): reject cross-origin state changes.
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin and origin != "null" and urlparse(origin).netloc != request.headers.get("host") \
                and origin.rstrip("/") not in settings.allowed_origins():
            return JSONResponse({"detail": "Cross-origin request blocked."}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", CSP)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"  # never let PHI sit in caches
    return response


@app.exception_handler(RequestValidationError)
async def validation_handler(_request: Request, exc: RequestValidationError):
    """Uniform error shape for the UI: {"detail": "...", "fields": {field: message}}"""
    fields = {}
    for e in exc.errors():
        loc = [str(p) for p in e.get("loc", []) if p not in ("body", "query", "path")]
        if loc:
            fields[loc[-1]] = str(e.get("msg", "Invalid value")).replace("Value error, ", "")
    return JSONResponse({"detail": "Please check the highlighted fields.", "fields": fields}, status_code=422)


@app.exception_handler(FieldError)
async def field_error_handler(_request: Request, exc: FieldError):
    return JSONResponse({"detail": exc.detail, "fields": exc.fields}, status_code=422)


@app.exception_handler(OperationalError)
async def db_operational_handler(request: Request, exc: OperationalError):
    """SQLite busy for longer than busy_timeout: nothing was committed, so the client may simply retry."""
    if is_db_locked(exc):
        log.warning("database is locked on %s %s; answered 503", request.method, request.url.path)
        return JSONResponse({"detail": "The system is busy. Nothing was saved - please try again in a moment."},
                            status_code=503, headers={"Retry-After": "2"})
    raise exc


@app.get("/api/health")
def health():
    return {"ok": True, "routers": list(ROUTER_MODULES), "failed_routers": {}, "scheduler": scheduler.status()}


@app.get("/api/ui-modules", include_in_schema=False)
def ui_modules():
    scripts = sorted(f"/static/js/{p.name}" for p in (STATIC / "js").glob("*.js"))
    return {"scripts": scripts}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC / "icons" / "icon-192.png", media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/staff", StaticFiles(directory=STATIC / "staff", html=True), name="staff")

hardening.install(app)
