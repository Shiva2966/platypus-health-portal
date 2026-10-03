"""Startup self-check: the app refuses to start when its wiring or database is incomplete.

Run from app.main's lifespan after init_db(). Every problem found is listed in ONE error so the
operator sees the whole picture in the server log. Checks:

1. every router module in app/routers/ is listed in app.main.ROUTER_MODULES and all its routes are mounted;
2. every model module in app/models/ is listed in app.db.MODEL_MODULES and every table exists in the DB;
3. every cross-module service function in REQUIRED_SERVICES exists and is callable;
4. the database is at the Alembic head revision (when Alembic manages it; production requires that).
"""
from __future__ import annotations

import importlib
import pkgutil
import re

from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from starlette.routing import Match

import app.models as models_pkg
import app.routers as routers_pkg
from app import settings
from app.db import MODEL_MODULES, Base, auto_migrate_enabled
from app.db_ops.migrate import migration_state

# Functions other modules call across ownership boundaries. If one is renamed or removed, startup fails
# here with a clear message instead of a 500 on the first request that needs it.
REQUIRED_SERVICES: dict[str, tuple[str, ...]] = {
    "app.services.audit": ("log_event",),
    "app.services.notifications": ("notify",),
    "app.services.notif_helpers": ("notify_pref", "notify_provider_staff", "provider_staff_ids", "unread_count",
                                   "visible_query", "mark_all_read", "get_prefs", "set_prefs"),
    "app.services.consent": ("list_visible_documents", "get_document_for_staff", "create_access_request",
                             "redeem_share_token", "active_grants_for_patient", "requests_for_staff",
                             "request_to_dict", "run_expiry_sweep", "get_records_for_staff",
                             "list_visible_record_categories", "staff_can_read_category"),
    "app.services.medical_read": ("get_category_for_patient", "list_corrections", "ser_correction", "summary",
                                  "timeline", "list_results", "result_trends"),
    "app.services.medical_write": ("create_record", "update_record", "delete_record", "confirm_record",
                                   "create_correction", "resolve_correction", "withdraw_correction"),
    "app.services.billing_read": ("get_billing_for_patient", "get_billing_for_staff", "active_billing_grants"),
    "app.services.billing_reminders": ("run_bill_reminder_sweep",),
    "app.services.appointments": ("list_for_provider", "set_status", "submit_request", "get_dashboard_summary"),
    "app.services.reminders": ("run_reminder_sweep",),
    "app.services.mailer": ("send_otp_email", "send_notice_email", "smtp_configured", "mask_email"),
    "app.services.otp": ("issue_otp", "check_code", "find_by_challenge", "lockout_seconds"),
    "app.services.auth_passwords": ("hash_password", "verify_password", "burn_password_time", "validate_password"),
    "app.services.staff_security": ("current_staff", "require_role", "staff_from_request", "create_staff_session"),
    "app.services.staff_portal": ("search_patients", "require_confirmed_patient", "visible_documents"),
    "app.services.files": ("validate_upload", "delivery_headers", "make_thumbnail", "qr_svg"),
    "app.security": ("current_patient", "create_patient_session", "destroy_patient_session"),
}


class StartupCheckError(RuntimeError):
    pass


def _modules(pkg) -> set[str]:
    return {m.name for m in pkgutil.iter_modules(pkg.__path__)}


_PARAM = re.compile(r"\{[^}]+\}")


def _is_mounted(app: FastAPI, route: APIRoute) -> bool:
    """Ask the app's own router whether a request for this route would reach a handler (public API only)."""
    path = _PARAM.sub("1", route.path)
    for method in route.methods or {"GET"}:
        scope = {"type": "http", "path": path, "method": method, "root_path": "", "headers": [], "query_string": b""}
        if not any(r.matches(scope)[0] == Match.FULL for r in app.router.routes):
            return False
    return True


def check_routers(app: FastAPI, router_modules: tuple[str, ...]) -> list[str]:
    problems = []
    for name in sorted(_modules(routers_pkg) - set(router_modules)):
        mod = importlib.import_module(f"app.routers.{name}")
        if hasattr(mod, "router"):
            problems.append(f"app/routers/{name}.py defines a router but is not listed in app.main.ROUTER_MODULES")
    for name in router_modules:
        mod = importlib.import_module(f"app.routers.{name}")
        router = getattr(mod, "router", None)
        if router is None:
            problems.append(f"app.routers.{name} is listed in ROUTER_MODULES but has no `router`")
            continue
        missing = [r.path for r in router.routes if isinstance(r, APIRoute) and not _is_mounted(app, r)]
        if missing:
            problems.append(f"app.routers.{name}: routes not mounted: {', '.join(sorted(set(missing))[:5])}")
    return problems


def check_models(engine: Engine) -> list[str]:
    problems = [f"app/models/{n}.py is not listed in app.db.MODEL_MODULES"
                for n in sorted(_modules(models_pkg) - set(MODEL_MODULES))]
    existing = set(inspect(engine).get_table_names())
    missing = sorted(set(Base.metadata.tables) - existing)
    if missing:
        problems.append(f"database is missing tables: {', '.join(missing)}")
    return problems


def check_services() -> list[str]:
    problems = []
    for module, names in REQUIRED_SERVICES.items():
        mod = importlib.import_module(module)
        for n in names:
            if not callable(getattr(mod, n, None)):
                problems.append(f"{module}.{n} is missing (other modules call it)")
    return problems


def check_migrations(engine: Engine) -> list[str]:
    st = migration_state(engine)
    if st["managed"]:
        if not st["up_to_date"]:
            return [f"database is at Alembic revision {st['current'] or 'none'} but the code needs {st['head']}; "
                    "run scripts\\migrate.ps1 (or start with AUTO_MIGRATE=1)"]
        return []
    if auto_migrate_enabled() or settings.is_production():
        return ["database is not managed by Alembic (no alembic_version table); production needs "
                "`alembic upgrade head` on an empty database or `alembic stamp head` after verifying the schema"]
    return []  # development database created by create_all


def run(app: FastAPI, engine: Engine, router_modules: tuple[str, ...]) -> None:
    problems = check_routers(app, router_modules) + check_services() + check_models(engine) + check_migrations(engine)
    if problems:
        raise StartupCheckError("Startup self-check FAILED - refusing to start:\n  - " + "\n  - ".join(problems))
