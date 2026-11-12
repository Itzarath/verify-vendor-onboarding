"""Single bounded background worker, coordinated through the database.

Claims a QUEUED run (or a RUNNING run whose lease expired because its worker
died) with FOR NO KEY UPDATE SKIP LOCKED, gives it a fresh lease token, and executes
it. Only the holder of the current token can publish results.
"""

from __future__ import annotations

import logging
import threading
import uuid

from sqlalchemy import select, text

from ..config import get_settings
from ..db import session_scope
from ..models import AuditEvent, Request, RunEvent
from .runner import _fail_run, execute_run

log = logging.getLogger("worker")

CLAIM_SQL = text("""
UPDATE runs SET status = 'RUNNING', lease_token = :tok, attempt = attempt + 1,
       lease_expires_at = now() + make_interval(secs => :lease), started_at = COALESCE(started_at, now())
WHERE id = (
    SELECT id FROM runs
    WHERE status = 'QUEUED' OR (status = 'RUNNING' AND lease_expires_at < now())
    ORDER BY queued_at
    FOR NO KEY UPDATE SKIP LOCKED
    LIMIT 1)
RETURNING id, attempt, request_id
""")


NEXT_LEASE_SQL = text("SELECT extract(epoch FROM (min(lease_expires_at) - now())) FROM runs WHERE status = 'RUNNING'")

EXHAUSTED = "exhausted"  # claim_one handled a run that ran out of attempts; there may be more work right behind it


class AttemptsExhausted(Exception):
    pass


def seconds_until_next_lease_expiry() -> float | None:
    """How long until a RUNNING run left by a stopped worker can be reclaimed (None: no such run)."""
    with session_scope() as db:
        v = db.execute(NEXT_LEASE_SQL).scalar()
    return None if v is None else max(0.0, float(v))


def claim_one() -> tuple[int, str, int] | str | None:
    s = get_settings()
    tok = uuid.uuid4().hex
    with session_scope() as db:  # the claim commits on its own: it only ever locks the run row
        row = db.execute(CLAIM_SQL, {"tok": tok, "lease": s.run_lease_s}).first()
        if not row:
            return None
        run_id, attempt, request_id = row
        if attempt > 1:
            db.add(RunEvent(run_id=run_id, stage="receive", status="warning",
                            title=f"Recovered after an interrupted attempt — attempt {attempt}"))
            # Also in the request's audit trail, so a decision can name it as a technical step. Its foreign-key checks
            # take only KEY SHARE locks on the request and vendor, which never wait for our row locks (see db.py).
            db.add(AuditEvent(request_id=request_id, vendor_id=db.scalar(select(Request.vendor_id).where(Request.id == request_id)),
                              event_type="RUN_RECOVERED", actor_type="system", data={"run_id": run_id, "attempt": attempt}))
    if attempt > s.max_run_attempts:
        # Separate transaction, same lock order as everything else (request -> run).
        _fail_run(run_id, tok, AttemptsExhausted(f"gave up after {attempt - 1} interrupted attempts"))
        return EXHAUSTED
    return run_id, tok, attempt


class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="evaluation-worker")
        self.stop_event = threading.Event()
        self.wake = threading.Event()

    def run(self):
        s = get_settings()
        log.info("worker started")
        errors = 0
        while not self.stop_event.is_set():
            try:
                claimed = claim_one()
                errors = 0
            except Exception:  # noqa: BLE001 - database briefly unavailable: retry soon, don't wait for the idle poll
                errors += 1
                log.exception("claim failed")
                self._pause(min(60.0, 2.0 ** min(errors, 6)))
                continue
            if claimed == EXHAUSTED:
                continue  # a terminal failure was recorded; keep draining the queue
            if claimed:
                run_id, tok, _ = claimed
                try:
                    execute_run(run_id, tok)
                except Exception:  # noqa: BLE001 - never let one run take the only worker down
                    log.exception("run %s: could not record its outcome; it is retried when its lease expires", run_id)
                continue
            # Nothing claimable right now. Wake when a stopped worker's run becomes reclaimable, when new work is
            # queued (nudge), or after the idle safety interval. The interval is long so that a scale-to-zero
            # database can pause while the demo is unused.
            try:
                until = seconds_until_next_lease_expiry()
            except Exception:  # noqa: BLE001
                log.exception("lease check failed")
                until = 30.0
            self._pause(s.worker_poll_s if until is None else min(s.worker_poll_s, until + 1.0))

    def _pause(self, seconds: float) -> None:
        self.wake.wait(seconds)
        self.wake.clear()


_worker: Worker | None = None


def start_worker() -> Worker:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = Worker()
        _worker.start()
    return _worker


def worker_alive() -> bool | None:
    """None when no worker was started in this process (tests, WORKER_ENABLED=false)."""
    return None if _worker is None else _worker.is_alive()


def nudge() -> None:
    if _worker is None:
        return
    if not _worker.is_alive() and get_settings().worker_enabled:
        log.error("the evaluation worker had stopped; restarting it")
        start_worker()
        return
    _worker.wake.set()
