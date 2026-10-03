"""HTTP hardening layer (PROD agent): installed once by app/main.py via `install(app)`.

* request size limits (Content-Length and streamed bodies) -> 413
* per-IP rate limit on sign-in / sign-up / OTP endpoints  -> 429
* security headers (X-Frame-Options, Permissions-Policy, COOP, HSTS on HTTPS) on top of main.py's CSP/nosniff
* cookie flags: HttpOnly always, Secure per COOKIE_SECURE (production default on), optional COOKIE_SAMESITE
* demo banner / "(Demo)" titles stripped from HTML unless SHOW_DEMO_BANNER; <html data-demo="0|1"> for scripts
* CORS for explicit ALLOWED_ORIGINS only
* generic error pages; in production unhandled errors are logged without messages (they can contain PHI)
"""
import json
import logging
import re
import threading
import time
from collections import deque

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import settings

log = logging.getLogger("health-portal.http")

AUTH_POST_PREFIXES = ("/api/auth/", "/api/staff/auth/", "/api/staff/login", "/api/caregiver/login",
                      "/api/caregiver/accept")
AUTH_EXEMPT = ("/api/auth/logout", "/api/staff/auth/logout")

_BANNER_RE = re.compile(rb'[ \t]*<div class="demo-banner"[^>]*>.*?</div>[ \t]*\r?\n?', re.S)
_TITLE_RE = re.compile(rb"(<title>)(.*?)(</title>)", re.S | re.I)
_HTML_TAG_RE = re.compile(rb"<html(?![^>]*data-demo)", re.I)

GENERIC_500 = "Something went wrong on our side. Please try again in a moment."
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title></head><body style="font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;color:#17212b">
<h1 style="font-size:1.4rem">{title}</h1><p>{text}</p><p><a href="/">Go to the home page</a></p></body></html>"""


def fastapi_kwargs() -> dict:
    if settings.docs_enabled():
        return {}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


class _RateLimiter:
    def __init__(self):
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= window:
                q.popleft()
            if len(q) >= limit:
                return False, max(1, int(window - (now - q[0])) + 1)
            q.append(now)
            if len(self._hits) > 50_000:  # bound memory under a flood of distinct IPs
                for k in [k for k, v in self._hits.items() if not v][:10_000]:
                    self._hits.pop(k, None)
            return True, 0

    def blocked_for(self, key: str, limit: int, window: float = 60.0) -> int:
        """Seconds until `key` may try again (0 = not blocked). Does not record a hit."""
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            while q and now - q[0] >= window:
                q.popleft()
            if q is not None and len(q) >= limit:
                return max(1, int(window - (now - q[0])) + 1)
            return 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


RATE_LIMITER = _RateLimiter()


def _header(scope, name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1")
    return ""


def real_client_ip(scope) -> str:
    """The end user's IP. Forwarding headers are believed only from a trusted direct peer (TRUSTED_PROXY_IPS,
    default loopback = the local cloudflared tunnel; TRUST_PROXY=1 trusts any peer). CF-Connecting-IP wins;
    otherwise the RIGHTMOST X-Forwarded-For entry that is not a trusted proxy (left entries are client-supplied)."""
    client = scope.get("client")
    peer = client[0] if client else "unknown"
    trusted = settings.trusted_proxy_ips()
    if not (settings.trust_proxy() or peer in trusted):
        return peer
    cf = _header(scope, b"cf-connecting-ip").strip()
    if cf:
        return cf[:64]
    parts = [p.strip() for p in _header(scope, b"x-forwarded-for").split(",") if p.strip()]
    for p in reversed(parts):
        if p not in trusted:
            return p[:64]
    return peer


_client_ip = real_client_ip


class ClientIPMiddleware:
    """Outermost layer: rewrite scope["client"] (and scheme from X-Forwarded-Proto) for requests relayed by a
    trusted proxy, so request.client.host is the real client IP for every throttle, audit row and session."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            peer = client[0] if client else None
            if peer and (settings.trust_proxy() or peer in settings.trusted_proxy_ips()):
                ip = real_client_ip(scope)
                proto = _header(scope, b"x-forwarded-proto").split(",")[0].strip().lower()
                if ip != peer or proto in ("http", "https"):
                    scope = dict(scope)
                    scope["client"] = (ip, client[1] if client else 0)
                    if proto in ("http", "https") and scope["type"] == "http":
                        scope["scheme"] = proto
        return await self.app(scope, receive, send)


OTP_PATH_PARTS = ("/verify-email", "/verify-login-otp", "/resend-verification", "/resend-login-otp",
                  "/forgot-password", "/reset-password", "/email-change/")
UPLOAD_PREFIXES = ("/api/documents",)


def _rate_check(scope, path: str, method: str) -> tuple[bool, int, str]:
    """Per-IP sliding-window limits, strictest first. Returns (ok, retry_after_seconds, message)."""
    if not path.startswith("/api/"):
        return True, 0, ""
    ip = real_client_ip(scope)
    rules = []
    if method == "POST" and path.startswith(AUTH_POST_PREFIXES) and path not in AUTH_EXEMPT:
        if any(p in path for p in OTP_PATH_PARTS):
            rules.append(("otp", settings.otp_rate_limit_per_minute(),
                          "Too many code attempts. Please wait a minute and try again."))
        rules.append(("auth", settings.auth_rate_limit_per_minute(),
                      "Too many attempts. Please wait a minute and try again."))
    if method in ("POST", "PUT") and path.startswith(UPLOAD_PREFIXES):
        rules.append(("upload", settings.upload_rate_limit_per_minute(),
                      "Too many uploads. Please wait a minute and try again."))
    rules.append(("api", settings.api_rate_limit_per_minute(),
                  "Too many requests. Please slow down and try again in a minute."))
    for name, limit, msg in rules:
        if limit > 0:
            ok, retry = RATE_LIMITER.allow(f"{name}:{ip}", limit)
            if not ok:
                return False, retry, msg
    return True, 0, ""


def _fix_cookie(value: str, secure_mode, https: bool, samesite: str) -> str:
    parts = [p.strip() for p in value.split(";")]
    attrs = {p.split("=", 1)[0].strip().lower() for p in parts[1:]}
    if samesite:
        parts = [p for p in parts if not p.lower().startswith("samesite")]
        parts.append("SameSite=" + samesite.capitalize())
    elif "samesite" not in attrs:
        parts.append("SameSite=Lax")
    if "httponly" not in attrs:
        parts.append("HttpOnly")
    want_secure = secure_mode is True or (secure_mode is None and https) or samesite == "none"
    if want_secure and "secure" not in attrs:
        parts.append("Secure")
    return "; ".join(parts)


async def _send_json(send, status: int, detail: str, extra_headers: list | None = None) -> None:
    body = json.dumps({"detail": detail}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")] + (extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class _TooLarge(Exception):
    pass


class HardeningMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, method = scope.get("path", ""), scope.get("method", "GET")

        limit = settings.max_request_bytes()
        if _header(scope, b"content-type").lower().startswith("application/json"):
            limit = min(limit, settings.max_json_bytes())
        cl = _header(scope, b"content-length")
        if cl.isdigit() and int(cl) > limit:
            return await _send_json(send, 413, "That request is too large.")

        ok, retry, msg = _rate_check(scope, path, method)
        if not ok:
            return await _send_json(send, 429, msg, [(b"retry-after", str(retry).encode())])

        received = 0
        too_large = False

        async def limited_receive():
            nonlocal received, too_large
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > limit:
                    too_large = True
                    raise _TooLarge()
            return msg

        https = settings.request_is_https(scope)
        secure_mode, samesite = settings.cookie_secure_mode(), settings.cookie_samesite()
        show_demo = settings.show_demo_banner()
        state = {"started": False, "html": None, "swallow": False}

        def finish_headers(raw_headers):
            out, names = [], set()
            for k, v in raw_headers:
                lk = k.lower()
                if lk == b"set-cookie":
                    v = _fix_cookie(v.decode("latin-1"), secure_mode, https, samesite).encode("latin-1")
                names.add(lk)
                out.append((k, v))
            extra = [(b"x-frame-options", b"SAMEORIGIN"), (b"x-content-type-options", b"nosniff"),
                     (b"referrer-policy", b"no-referrer"), (b"cross-origin-opener-policy", b"same-origin"),
                     (b"x-permitted-cross-domain-policies", b"none"),
                     (b"permissions-policy", b"camera=(self), microphone=(), geolocation=(self), payment=(), usb=()")]
            if https:
                extra.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
            out.extend((k, v) for k, v in extra if k not in names)
            return out

        async def wrapped_send(message):
            if state["swallow"]:
                return
            if message["type"] == "http.response.start":
                if too_large:
                    state["swallow"] = True
                    state["started"] = True
                    return await _send_json(send, 413, "That request is too large.")
                state["started"] = True
                message = dict(message)
                message["headers"] = finish_headers(message.get("headers") or [])
                ctype = next((v for k, v in message["headers"] if k.lower() == b"content-type"), b"")
                if message.get("status") == 200 and ctype.lower().startswith(b"text/html"):
                    state["html"] = {"start": message, "chunks": []}
                    return
                return await send(message)
            if message["type"] == "http.response.body" and state["html"] is not None:
                h = state["html"]
                h["chunks"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = _rewrite_html(b"".join(h["chunks"]), show_demo)
                start = h["start"]
                start["headers"] = [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"] + \
                                   [(b"content-length", str(len(body)).encode())]
                state["html"] = None
                await send(start)
                return await send({"type": "http.response.body", "body": body})
            return await send(message)

        try:
            await self.app(scope, limited_receive, wrapped_send)
        except _TooLarge:
            if not state["started"]:
                await _send_json(send, 413, "That request is too large.")
        except Exception as exc:
            if not settings.is_production():
                raise
            tb = exc.__traceback__
            while tb is not None and tb.tb_next is not None:
                tb = tb.tb_next
            where = f"{tb.tb_frame.f_code.co_filename.rsplit(chr(92), 1)[-1].rsplit('/', 1)[-1]}:{tb.tb_lineno}" if tb else "?"
            log.error("Unhandled %s on %s %s at %s", type(exc).__name__, method, path, where)  # no message: may hold PHI
            if not state["started"]:
                if path.startswith("/api/"):
                    await _send_json(send, 500, GENERIC_500)
                else:
                    page = _PAGE.format(title="Something went wrong", text=GENERIC_500).encode()
                    await send({"type": "http.response.start", "status": 500,
                                "headers": finish_headers([(b"content-type", b"text/html; charset=utf-8"),
                                                           (b"content-length", str(len(page)).encode())])})
                    await send({"type": "http.response.body", "body": page})


def _rewrite_html(body: bytes, show_demo: bool) -> bytes:
    body = _HTML_TAG_RE.sub(b'<html data-demo="' + (b"1" if show_demo else b"0") + b'"', body, count=1)
    if not show_demo:
        body = _BANNER_RE.sub(b"", body)
        body = _TITLE_RE.sub(lambda m: m.group(1) + re.sub(rb"\s*\(Demo\)", b"", m.group(2)) + m.group(3), body, count=1)
    return body


async def _http_exc(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404 and not request.url.path.startswith("/api/") \
            and "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(_PAGE.format(title="Page not found", text="We couldn't find that page."), status_code=404)
    return await http_exception_handler(request, exc)


async def _unhandled(request: Request, exc: Exception):
    log.error("Unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": GENERIC_500}, status_code=500)
    return HTMLResponse(_PAGE.format(title="Something went wrong", text=GENERIC_500), status_code=500)


def install(app: FastAPI) -> None:
    settings.startup_checks()
    origins = settings.allowed_origins()
    if origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["Content-Type", "Accept"], max_age=600)
    app.add_middleware(HardeningMiddleware)
    app.add_middleware(ClientIPMiddleware)  # added last = runs first
    app.add_exception_handler(StarletteHTTPException, _http_exc)
    app.add_exception_handler(Exception, _unhandled)
