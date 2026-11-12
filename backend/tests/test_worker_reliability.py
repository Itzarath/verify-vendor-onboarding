"""The single evaluation worker must keep draining work, recover stopped runs when their lease expires, survive errors,
and report the memory cap only when proven."""

import pytest
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.pdf import sandbox
from app.pipeline import runner
from app.pipeline import worker as W


def drive(monkeypatch, claims, until=None, execute=None, poll=3600.0):
    """Run the real Worker loop synchronously against scripted claim results; record claims, runs and pauses."""
    events: list[tuple] = []
    script = list(claims)
    w = W.Worker()

    def fake_claim():
        if not script:
            w.stop_event.set()
            return None
        x = script.pop(0)
        if isinstance(x, Exception):
            raise x
        events.append(("claim", x))
        return x

    def default_execute(run_id, token):
        events.append(("run", run_id))

    monkeypatch.setattr(W, "claim_one", fake_claim)
    monkeypatch.setattr(W, "seconds_until_next_lease_expiry", lambda: until)
    monkeypatch.setattr(W, "execute_run", execute or default_execute)
    monkeypatch.setattr(get_settings(), "worker_poll_s", poll)
    w._pause = lambda seconds: events.append(("pause", seconds))
    w.run()
    return events


def db_error():
    return OperationalError("SELECT 1", {}, Exception("server closed the connection unexpectedly"))


def test_after_an_exhausted_run_the_queue_keeps_draining(monkeypatch):
    ev = drive(monkeypatch, [W.EXHAUSTED, (7, "tok", 1)])
    assert ev[:3] == [("claim", W.EXHAUSTED), ("claim", (7, "tok", 1)), ("run", 7)]  # no idle pause in between


def test_a_database_error_is_retried_within_a_minute_not_after_the_idle_poll(monkeypatch):
    ev = drive(monkeypatch, [db_error(), (8, "tok", 1)])
    pauses = [s for kind, s in ev if kind == "pause"]
    assert pauses and pauses[0] <= 60 and ("run", 8) in ev


def test_a_stopped_workers_run_is_picked_up_when_its_lease_expires(monkeypatch):
    ev = drive(monkeypatch, [None], until=5.0)
    assert ("pause", 6.0) in ev  # min(idle poll, lease + 1 s), not the hour-long idle poll


def test_truly_idle_waits_the_long_poll_so_the_database_can_pause(monkeypatch):
    ev = drive(monkeypatch, [None], until=None)
    assert ("pause", 3600.0) in ev


def test_a_run_that_blows_up_does_not_take_the_worker_down(monkeypatch):
    def execute(run_id, token):
        if run_id == 1:
            raise db_error()

    ev = drive(monkeypatch, [(1, "a", 1), (2, "b", 1)], execute=execute)
    assert ("claim", (2, "b", 1)) in ev


def test_failure_to_record_a_failure_is_contained(monkeypatch):
    # stage fails, then saving that failure fails too (database gone): execute_run must return, not raise
    monkeypatch.setattr(runner, "_execute", lambda run_id, token: (_ for _ in ()).throw(RuntimeError("stage failed")))
    monkeypatch.setattr(runner, "_fail_run", lambda run_id, token, exc: (_ for _ in ()).throw(db_error()))
    monkeypatch.setattr(runner._Heartbeat, "start", lambda self: None)
    runner.execute_run(999_999, "tok")


def test_a_nudge_restarts_a_worker_that_has_stopped(monkeypatch):
    dead = W.Worker()  # never started, so not alive
    started = []
    monkeypatch.setattr(W, "_worker", dead)
    monkeypatch.setattr(W, "start_worker", lambda: started.append(True))
    monkeypatch.setattr(get_settings(), "worker_enabled", True)
    W.nudge()
    assert started == [True]


@pytest.mark.parametrize("outcome,expected", [
    (("error", "memory"), True),
    (("ok", "allocated"), False),
    (("busy", None), None),
    (("timeout", None), None),
    (("crash", -9), None),
    (("unreadable", None), None),
    (("intake", ("busy", "x")), None),
])
def test_memory_cap_is_reported_enforced_only_when_the_allocation_was_refused(monkeypatch, outcome, expected):
    monkeypatch.setattr(sandbox, "_child", lambda op, data, mb, kw: outcome)
    enforced, note = sandbox.memory_cap_check()
    assert enforced is expected
    assert (note is None) == (expected is not None)


def test_real_database_lease_wait_and_exhausted_run(app_client):
    from sqlalchemy import text

    from app.db import session_scope

    from .test_api_flow import run_worker_until_idle, submit_sample, vendor

    run_worker_until_idle()  # start from an empty queue
    v = vendor(app_client, "Lease Check Pvt Ltd")
    r, _ = submit_sample(v, "happy_nimbus")
    run_id = r.json()["run_id"]
    claimed = W.claim_one()  # like a worker that then stops: RUNNING with a live lease
    assert claimed and claimed[0] == run_id
    left = W.seconds_until_next_lease_expiry()
    assert left is not None and 0 < left <= get_settings().run_lease_s
    with session_scope() as db:  # the lease has run out and the attempts are used up
        db.execute(text("UPDATE runs SET lease_expires_at = now() - interval '1 second', attempt = :a WHERE id = :id"),
                   {"a": get_settings().max_run_attempts, "id": run_id})
    assert W.claim_one() == W.EXHAUSTED
    with session_scope() as db:
        status = db.execute(text("SELECT status FROM runs WHERE id = :id"), {"id": run_id}).scalar()
    assert status == "FAILED"
