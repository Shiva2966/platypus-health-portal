"""Idle timeout for patient and staff sessions (HARD agent), on top of their 8-hour absolute lifetime.

A session that makes no "activity" request for SESSION_IDLE_MINUTES_PATIENT (default 30) /
SESSION_IDLE_MINUTES_STAFF (default 20) minutes is ended (the server-side row is deleted -> 401).
Background polling (notification badge/SSE, or any request with `X-Background: 1`) is checked but does not
count as activity, so an unattended open tab still times out. 0 disables.

Last-activity times are kept in process memory (single uvicorn worker): a server restart gives every open
session a fresh idle window, it never extends the absolute 8-hour expiry.
"""
from __future__ import annotations

import os
import threading
import time

BACKGROUND_PATHS = ("/api/notifications/unread-count", "/api/notifications/stream")
_DEFAULTS = {"patient": 30, "staff": 20}
_last: dict[str, float] = {}
_lock = threading.Lock()


def idle_minutes(kind: str) -> int:
    raw = (os.environ.get(f"SESSION_IDLE_MINUTES_{kind.upper()}") or "").strip()
    try:
        return int(raw) if raw else _DEFAULTS[kind]
    except ValueError:
        return _DEFAULTS[kind]


def is_background(request) -> bool:
    return request.url.path.startswith(BACKGROUND_PATHS) or request.headers.get("x-background") == "1"


def touch(kind: str, token_hash: str, request=None) -> bool:
    """Record activity; False if the session has been idle too long (caller ends it)."""
    limit = idle_minutes(kind) * 60
    if limit <= 0:
        return True
    key = f"{kind}:{token_hash}"
    now = time.monotonic()
    with _lock:
        last = _last.get(key)
        if last is not None and now - last > limit:
            _last.pop(key, None)
            return False
        if last is None or request is None or not is_background(request):
            _last[key] = now
        if len(_last) > 20_000:
            for k in [k for k, t in _last.items() if now - t > limit]:
                _last.pop(k, None)
    return True


def forget(kind: str, token_hash: str) -> None:
    with _lock:
        _last.pop(f"{kind}:{token_hash}", None)


def message(kind: str) -> str:
    return f"You were signed out after {idle_minutes(kind)} minutes without activity. Please sign in again."
