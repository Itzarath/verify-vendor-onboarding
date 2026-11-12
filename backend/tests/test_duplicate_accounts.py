"""One canonical bank-account key (formatting can't hide a duplicate), re-read scope, a recovery claim racing a
correction, and clearances that cover only the records they saw.

API tests run on the real PostgreSQL test database with the test-only fixture reader. No live model calls. Each scenario uses its own throwaway buyer.
"""

import hashlib
import importlib.util
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.db import get_engine, session_scope
from app.extraction.service import get_reader
from app.models import AuditEvent, Extraction, Request, ReviewResolution, Revision, Run, Task
from app.pdf.inspect import inspect_pdf
from app.pipeline import worker as W
from app.pipeline.crossfacts import bank_key

from .test_api_flow import case, op, run_worker_until_idle, vendor
from .test_auto_approval import as_user, decisions, make_buyer
from .test_publish_guards import _load, _variant
from .test_reviewer_judgements import _check, _resolve
from .test_clearances_and_visibility import _approve, _submit, _upload

DUP = "credibility:duplicate_bank"


def _drain():
    run_worker_until_idle(50)


def _submit_form(v, key: str = "happy_nimbus", change=None) -> int:
    form, docs = _load(v, key)
    if change:
        change(form)
    r = v.post("/api/vendor/submit", json={"operation_id": op(), "form": form, "documents": docs})
    assert r.status_code == 200, r.text
    return r.json()["request_id"]


def _spaced(field: str, ch: str):
    """Copy/paste formatting: a whitespace character inside the account number or IFSC."""
    def change(form):
        v = form["bank"][field]
        form["bank"][field] = v[:4] + ch + v[4:]
    return change


# ---------------------------------------------------------------------------- one canonical account key

@pytest.mark.parametrize("field,ch,spaced_first", [
    ("account_number", " ", True),   # no-break space, as copied from formatted text
    ("ifsc", " ", True),
    ("account_number", " ", True),   # thin space
    ("account_number", "　", True),   # ideographic space
    ("account_number", " ", False),  # the plain form first, the formatted copy second
])
def test_formatting_inside_an_account_does_not_hide_a_duplicate(app_client, field, ch, spaced_first):
    _drain()
    b = make_buyer(auto=True)
    first, second = (_spaced(field, ch), None) if spaced_first else (None, _spaced(field, ch))
    req_a = _submit_form(vendor(app_client, "Formatted Account One Private Limited", buyer=b.slug), change=first)
    _drain()
    assert decisions(req_a) == [("approved", "system", "auto_approve_clean")]  # valid, and the first of its account
    req_b = _submit_form(vendor(app_client, "Formatted Account Two Private Limited", buyer=b.slug), change=second)
    _drain()
    assert decisions(req_b) == []  # the same account: a person must look, never an automatic approval
    dup = _check(case(as_user(app_client, b.reviewer_id), req_b), DUP)
    assert dup["status"] == "needs_review" and dup["evidence"]["other_vendor_records"] == 1
    with session_scope() as db:
        keys = {db.get(Revision, db.get(Request, r).current_revision_id).bank_account_key for r in (req_a, req_b)}
    assert len(keys) == 1 and None not in keys


def test_a_reviewer_approval_sees_a_formatted_copy_of_the_account(app_client):
    _drain()
    b = make_buyer(auto=False)
    req_a = _submit(vendor(app_client, "Plain Account Private Limited", buyer=b.slug), "happy_nimbus")["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    assert case(rv, req_a)["request"]["state"] == "READY_FOR_DECISION"
    _submit_form(vendor(app_client, "Pasted Account Private Limited", buyer=b.slug),
                 change=_spaced("account_number", " "))
    r = _approve(rv, req_a)
    assert r.status_code == 409 and r.json()["error"] == "re_evaluating", r.text
    _drain()
    assert decisions(req_a) == [] and _check(case(rv, req_a), DUP)["status"] == "needs_review"


def test_the_migration_fills_in_keys_for_existing_revisions(app_client):
    b = make_buyer(auto=False)
    req = _submit_form(vendor(app_client, "Backfill Private Limited", buyer=b.slug), change=_spaced("account_number", " "))
    _drain()
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0003_bank_account_key.py"
    spec = importlib.util.spec_from_file_location("migration_0003", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    with session_scope() as db:
        rev_id = db.get(Request, req).current_revision_id
        form = db.get(Revision, rev_id).form
    with get_engine().begin() as conn:  # as a revision stored before the column existed
        conn.execute(text("UPDATE revisions SET bank_account_key = NULL WHERE id = :id"), {"id": rev_id})
    with get_engine().begin() as conn:
        assert mig.backfill(conn) >= 1
    with session_scope() as db:
        key = db.get(Revision, rev_id).bank_account_key
    plain = {**form, "bank": {**form["bank"], "account_number": form["bank"]["account_number"].replace(" ", "")}}
    assert key == bank_key(form) == bank_key(plain) and " " not in key


# ---------------------------------------------------------------------------- one read per request

def _incomplete(data: bytes):
    """A stored reading of these bytes with one critical field left out (as an older reading could be)."""
    reader = get_reader()
    reading = reader.read(data, [p.kind for p in inspect_pdf(data, 10 * 2**20, 10).pages]).reading
    key = next(k for k, f in reading.fields.items() if f.critical and f.status == "found")
    reading.fields[key] = reading.fields[key].model_copy(
        update={"status": "not_found", "value": None, "normalized": None, "provenance": "none", "page": None})
    return reading


def _reread(rv, req: int, slot: str) -> int:
    c = case(rv, req)
    r = rv.post(f"/api/review/requests/{req}/reread", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "slot": slot})
    assert r.status_code == 200, r.text
    _drain()
    return r.json()["run_id"]


@pytest.mark.parametrize("failing,other", [("gst_certificate", "bank_proof"), ("bank_proof", "gst_certificate")])
def test_a_failed_reread_does_not_widen_the_next_reread(app_client, monkeypatch, failing, other):
    import uuid

    _drain()
    b = make_buyer(auto=False)
    v = vendor(app_client, "Successive Rereads Private Limited", buyer=b.slug)
    tag = uuid.uuid4().hex
    data = {"bank_proof": _variant("nimbus_bank_letter.pdf", tag), "gst_certificate": _variant("nimbus_gst.pdf", tag)}
    sha = {s: hashlib.sha256(d).hexdigest() for s, d in data.items()}
    ids = {s: _upload(v, d, f"{s}.pdf") for s, d in data.items()}
    reader = get_reader()
    with session_scope() as db:  # incomplete stored readings of both files
        for s, d in data.items():
            reading = _incomplete(d)
            db.add(Extraction(document_id=ids[s], config_key=reader.config_key, status="partial",
                              detected_type=reading.detected_type, result=reading.model_dump(), provider="test-fixture",
                              model="historical", latency_ms=0, usage={}))
    calls = []
    real_read = type(reader).read
    monkeypatch.setattr(type(reader), "read", lambda self, pdf, kinds: (
        calls.append(hashlib.sha256(pdf).hexdigest()), real_read(self, pdf, kinds))[1])
    try:
        req = _submit(v, "happy_nimbus", replace=ids)["request_id"]
        assert calls == []  # both stored readings reused
        rv = as_user(app_client, b.reviewer_id)
        docs = case(rv, req)["assessment"]["summary"]["documents"]
        assert docs["bank_proof"]["status"] == docs["gst_certificate"]["status"] == "partial"

        reader.fail_hashes.add(sha[failing])  # the first re-read meets a provider outage
        _reread(rv, req, failing)
        assert calls == [sha[failing]]
        assert case(rv, req)["assessment"]["summary"]["documents"][failing]["status"] == "failed"

        calls.clear()
        run_id = _reread(rv, req, other)  # the outage is still on: only the chosen file may be read
        assert calls == [sha[other]]
        docs = case(rv, req)["assessment"]["summary"]["documents"]
        assert docs[other]["status"] == "complete" and docs[failing]["status"] == "failed"
        titles = [e["title"] for e in rv.get(f"/api/runs/{run_id}/events").json()["events"]]
        assert any("not read again: this re-read covers only the selected file" in t for t in titles)

        reader.fail_hashes.discard(sha[failing])  # the outage is over: the ordinary retry reads the failed file once
        calls.clear()
        c = case(rv, req)
        assert rv.post(f"/api/review/requests/{req}/retry", json={
            "operation_id": op(), "expected_version": c["request"]["version"]}).status_code == 200
        _drain()
        assert calls == [sha[failing]]
        docs = case(rv, req)["assessment"]["summary"]["documents"]
        assert docs[failing]["status"] == docs[other]["status"] == "complete"
    finally:
        reader.fail_hashes.discard(sha[failing])


# ---------------------------------------------------------------------------- recovery claim vs correction

def test_a_recovery_claim_and_a_correction_do_not_deadlock(app_client, monkeypatch):
    """The order that deadlocked: the claim holds the run row and is about to write RUN_RECOVERED; a correction holds the vendor
    and request and waits for the run. The audit insert's foreign-key checks must not wait for the correction."""
    _drain()
    b = make_buyer(auto=False)
    v = vendor(app_client, "Recovery Overlap Private Limited", buyer=b.slug)
    req = _submit_form(v)
    first = W.claim_one()
    assert first and first != W.EXHAUSTED
    with session_scope() as db:
        db.execute(text("UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"), {"id": first[0]})

    holding, release = threading.Event(), threading.Event()
    real_audit = W.AuditEvent

    def audit(**kw):
        holding.set()  # the claim has updated (and locked) the run row; its audit row is not written yet
        assert release.wait(30)
        return real_audit(**kw)

    monkeypatch.setattr(W, "AuditEvent", audit)
    out = {}

    def claim():
        try:
            out["claim"] = W.claim_one()
        except Exception as exc:  # noqa: BLE001
            out["claim"] = exc

    tc = threading.Thread(target=claim)
    tc.start()
    assert holding.wait(30)
    form, docs = _load(v, "happy_nimbus")
    tv = threading.Thread(target=lambda: out.setdefault("correction", v.post("/api/vendor/submit", json={
        "operation_id": op(), "form": form, "documents": docs, "based_on_revision": 1})))
    tv.start()
    waiting = False
    for _ in range(300):  # the correction is waiting for the run row the claim holds
        with session_scope() as db:
            if db.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                              "AND query ILIKE 'UPDATE runs%'")):
                waiting = True
                break
        time.sleep(0.05)
    release.set()
    tc.join(30)
    tv.join(30)
    assert waiting, "the correction never waited for the claimed run, so the overlap was not exercised"
    assert not isinstance(out["claim"], Exception), out["claim"]
    assert out["claim"][0] == first[0] and out["claim"][2] == 2
    assert out["correction"].status_code == 200, out["correction"].text
    with session_scope() as db:
        assert db.get(Run, first[0]).status == "SUPERSEDED"  # the correction replaced the recovered run
        assert db.scalar(select(AuditEvent).where(AuditEvent.request_id == req, AuditEvent.event_type == "RUN_RECOVERED"))
    _drain()
    assert case(as_user(app_client, b.reviewer_id), req)["request"]["state"] == "READY_FOR_DECISION"


# ---------------------------------------------------------------------------- a clearance covers its records

def _vendor_id(rv, req: int) -> int:
    return case(rv, req)["vendor"]["id"]


def _cleared_second_vendor(app_client):
    """Vendor A, then vendor B with the same account; a reviewer accepts B's shared account (it is A's)."""
    _drain()
    b = make_buyer(auto=False)
    rv = as_user(app_client, b.reviewer_id)
    req_a = _submit(vendor(app_client, "Group Company A Private Limited", buyer=b.slug), "happy_nimbus")["request_id"]
    req_b = _submit(vendor(app_client, "Group Company B Private Limited", buyer=b.slug), "happy_nimbus")["request_id"]
    assert _check(case(rv, req_b), DUP)["status"] == "needs_review"
    _resolve(rv, req_b, DUP, "accepted", "Test: A and B are group companies sharing a treasury account")
    _clear_same_identity(rv, req_b)
    assert case(rv, req_b)["request"]["state"] == "READY_FOR_DECISION"
    return b, rv, req_a, req_b


def _clear_same_identity(rv, req: int):
    """These vendors also share the sample's PAN and GSTIN, which has its own review (credibility:duplicate_identity);
    the tests here are about the shared account, so that review is accepted whenever it is open."""
    if _check(case(rv, req), "credibility:duplicate_identity")["status"] == "needs_review":
        _resolve(rv, req, "credibility:duplicate_identity", "accepted", "Test: same group, separate vendor records")


def _asked_again(rv, req_b: int, expect_ids: list[int]):
    r = _approve(rv, req_b)
    assert r.status_code == 409 and r.json()["error"] == "re_evaluating", r.text
    _drain()
    c = case(rv, req_b)
    dup = _check(c, DUP)
    assert c["request"]["state"] == "ACTION_REQUIRED" and dup["status"] == "needs_review"
    assert dup["evidence"]["other_vendor_ids"] == sorted(expect_ids)
    with session_scope() as db:
        assert db.scalar(select(Task).where(Task.request_id == req_b, Task.status == "open", Task.owner == "reviewer"))
        assert db.scalar(select(ReviewResolution.id).where(ReviewResolution.request_id == req_b))  # history kept
    assert decisions(req_b) == []


def test_a_third_vendor_on_the_account_needs_a_fresh_judgement(app_client):
    b, rv, req_a, req_b = _cleared_second_vendor(app_client)
    req_c = _submit_form(vendor(app_client, "Third Vendor Private Limited", buyer=b.slug))
    _asked_again(rv, req_b, [_vendor_id(rv, req_a), _vendor_id(rv, req_c)])
    _resolve(rv, req_b, DUP, "accepted", "Test: C is in the same group too")
    _clear_same_identity(rv, req_b)
    assert _approve(rv, req_b).status_code == 200
    assert decisions(req_b) == [("approved", "reviewer", None)]
    _drain()


def test_a_different_vendor_with_the_same_count_needs_a_fresh_judgement(app_client):
    b, rv, req_a, req_b = _cleared_second_vendor(app_client)
    c = case(rv, req_a)
    assert rv.post(f"/api/review/requests/{req_a}/reject", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "reason": "Test: closing A",
        "vendor_explanation": "Closed for a test."}).status_code == 200
    req_c = _submit_form(vendor(app_client, "Replacement Vendor Private Limited", buyer=b.slug))
    _asked_again(rv, req_b, [_vendor_id(rv, req_c)])  # still one other record, but not the one the reviewer judged
    _drain()
