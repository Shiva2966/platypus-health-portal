"""The ONE background scheduler for periodic jobs (reminders, consent expiry).

* Started once from app.main's lifespan (`start()`), stopped there on shutdown (`stop()`).
* One machine-wide runner: an exclusive OS file lock (default: `scheduler.lock` next to the SQLite file,
  or `data/scheduler.lock`; override with HP_SCHEDULER_LOCK). Other processes (uvicorn --reload child,
  extra workers) stay on standby and take over within a minute if the runner dies.
* Every job runs in its own DB session; a failure is logged with a traceback and never stops other jobs.
  SQLite 'database is locked' is retried (bounded) before the run is counted as failed.
* HP_DISABLE_SCHEDULER=1 turns it off (the test suite does; tests call the job functions directly).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.db import DB_PATH, ROOT, SessionLocal, is_db_locked
from app.models.shared import utcnow
from app.services import consent, reminders

log = logging.getLogger("health-portal.scheduler")

TICK_SECONDS = 5
STARTUP_DELAY_SECONDS = 5
STANDBY_RETRY_SECONDS = 60
LOCKED_RETRIES = 3


def _seconds(env: str, default: int) -> int:
    raw = (os.environ.get(env) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{env} must be a whole number of seconds, got {raw!r}") from None
    if value < 10:
        raise ValueError(f"{env} must be at least 10 seconds, got {value}")
    return value


def enabled() -> bool:
    return os.environ.get("HP_DISABLE_SCHEDULER", "").strip() not in ("1", "true", "yes")


def default_lock_path() -> Path:
    if os.environ.get("HP_SCHEDULER_LOCK"):
        return Path(os.environ["HP_SCHEDULER_LOCK"])
    return (DB_PATH.parent if DB_PATH else ROOT / "data") / "scheduler.lock"


@dataclass
class Job:
    name: str
    func: Callable[[Session], object]
    interval_seconds: int
    next_run: float = 0.0
    runs: int = 0
    failures: int = 0
    last_started_at: datetime | None = None
    last_ok_at: datetime | None = None
    last_error: str | None = None  # exception class name only (messages can contain data)

    def status(self) -> dict:
        return {"interval_seconds": self.interval_seconds, "runs": self.runs, "failures": self.failures,
                "last_started_at": self.last_started_at.isoformat() + "Z" if self.last_started_at else None,
                "last_ok_at": self.last_ok_at.isoformat() + "Z" if self.last_ok_at else None,
                "last_error": self.last_error}


def build_jobs() -> list[Job]:
    """The complete list of periodic work. Add new jobs HERE, nowhere else."""
    return [
        Job("reminders", reminders.run_reminder_sweep, _seconds("HP_REMINDER_SWEEP_SECONDS", 300)),
        Job("consent_expiry", consent.run_expiry_sweep, _seconds("HP_CONSENT_SWEEP_SECONDS", 600)),
    ]


class FileLock:
    """Non-blocking exclusive lock on one byte of a file; released by the OS if the process dies."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> bool:
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


class Scheduler:
    def __init__(self, jobs: list[Job], lock_path: Path, *, tick_seconds: float = TICK_SECONDS,
                 startup_delay: float = STARTUP_DELAY_SECONDS, session_factory=SessionLocal):
        self.jobs = jobs
        self.lock = FileLock(lock_path)
        self.tick_seconds = tick_seconds
        self.startup_delay = startup_delay
        self.session_factory = session_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            raise RuntimeError("Scheduler.start() called twice in one process")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="hp-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout)
            if t.is_alive():
                log.error("Scheduler thread did not stop within %.0fs (a job is still running)", timeout)
        self.lock.release()

    def _loop(self) -> None:
        if self._stop.wait(self.startup_delay):
            return
        next_lock_try = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if not self.lock.held and now >= next_lock_try:
                if self.lock.acquire():
                    log.info("Scheduler active in pid %s (lock %s); jobs: %s", os.getpid(), self.lock.path,
                             ", ".join(f"{j.name}/{j.interval_seconds}s" for j in self.jobs))
                else:
                    log.info("Scheduler on standby in pid %s: another process holds %s", os.getpid(), self.lock.path)
                    next_lock_try = now + STANDBY_RETRY_SECONDS
            if self.lock.held:
                for job in self.jobs:
                    if self._stop.is_set():
                        break
                    if time.monotonic() >= job.next_run:
                        self.run_job(job)
            self._stop.wait(self.tick_seconds)

    def run_job(self, job: Job) -> bool:
        """Run one job now in its own session. Returns True on success. Never raises."""
        job.runs += 1
        job.last_started_at = utcnow()
        job.next_run = time.monotonic() + job.interval_seconds
        for attempt in range(1, LOCKED_RETRIES + 1):
            db = self.session_factory()
            try:
                result = job.func(db)
                db.commit()
                job.last_ok_at, job.last_error = utcnow(), None
                log.info("job %s ok: %s", job.name, result)
                return True
            except Exception as exc:
                db.rollback()
                if is_db_locked(exc) and attempt < LOCKED_RETRIES:
                    log.warning("job %s: database is locked (attempt %d/%d); retrying", job.name, attempt,
                                LOCKED_RETRIES)
                    self._stop.wait(2 * attempt)
                    continue
                job.failures += 1
                job.last_error = type(exc).__name__
                log.exception("job %s FAILED (%s); next try in %ss", job.name, job.last_error, job.interval_seconds)
                return False
            finally:
                db.close()
        return False

    def status(self) -> dict:
        return {"enabled": True, "thread_alive": self.running,
                "role": "active" if self.lock.held else "standby",
                "jobs": {j.name: j.status() for j in self.jobs}}


_SCHEDULER: Scheduler | None = None


def start() -> None:
    """Called once by app.main's lifespan."""
    global _SCHEDULER
    if not enabled():
        log.warning("Background scheduler DISABLED (HP_DISABLE_SCHEDULER=1): no reminders or expiry sweeps")
        return
    if _SCHEDULER is not None and _SCHEDULER.running:
        raise RuntimeError("Scheduler already running in this process")
    _SCHEDULER = Scheduler(build_jobs(), default_lock_path())
    _SCHEDULER.start()


def stop() -> None:
    global _SCHEDULER
    if _SCHEDULER is not None:
        _SCHEDULER.stop()
        _SCHEDULER = None


def status() -> dict:
    if _SCHEDULER is None:
        return {"enabled": enabled(), "thread_alive": False, "role": "stopped", "jobs": {}}
    return _SCHEDULER.status()
