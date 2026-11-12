"""Start-up upgrades of stored policies, the same bank account submitted between the checks and publishing, the shared
finaliser's re-checks, recovered runs named in automatic decisions, and re-reads limited to one file.

API tests run on the real PostgreSQL test database with the test-only fixture reader. No live model calls. Every scenario uses its own throwaway buyer (or throwaway seed slugs), so it tests the rule rather than a sample.
"""

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import seed as seed_module
from app.db import session_scope
from app.extraction.service import FIXTURE_DIR, get_reader
from app.models import (Assessment, AuditEvent, Buyer, Extraction, Message, Policy, Request, Revision, Task, User,
                        Vendor)
from app.pdf.inspect import inspect_pdf
from app.pipeline import worker as W
from app.pipeline.finalise import CannotApprove, approve_request
from app.pipeline.runner import execute_run
from app.schemas.policy import BuyerPolicy
from app.services import commands

from .test_api_flow import case, op, run_worker_until_idle, vendor
from .test_auto_approval import as_user, decisions, make_buyer
from .test_reviewer_judgements import _check
from .test_clearances_and_visibility import _approve, _submit, _upload

POL = Path(__file__).resolve().parents[1] / "policies"


def _sha(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def _load(v, key: str) -> tuple[dict, dict]:
    s = v.post(f"/api/vendor/samples/{key}/load").json()
    return s["form"], {k: d["document_id"] for k, d in s["documents"].items()}


def _post_submit(v, key: str):
    form, docs = _load(v, key)
    r = v.post("/api/vendor/submit", json={"operation_id": op(), "form": form, "documents": docs})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------- upgrade from stored policies

def _legacy(file: str, **changes) -> dict:
    """A policy document as the previous release stored it: serialised before the `approval` setting existed."""
    doc = BuyerPolicy.model_validate(json.loads((POL / file).read_text(encoding="utf-8"))).model_dump(mode="json")
    doc.pop("approval")
    doc.update(changes)
    return doc


def _legacy_buyers(monkeypatch, docs: dict[str, dict]) -> dict[str, int]:
    """Throwaway buyers whose stored policies were written by the previous release, and a seed list pointing them at
    today's policy files (so the real startup seeds THEM, not Meridian/Kestrel)."""
    tag = uuid.uuid4().hex[:8]
    buyers, ids = [], {}
    with session_scope() as db:
        for file, doc in docs.items():
            slug = f"legacy-{tag}-{file.split('.')[0]}"
            b = Buyer(slug=slug, name=f"Legacy {slug} (fictional)")
            db.add(b)
            db.flush()
            pol = Policy(buyer_id=b.id, policy_key=doc["policy_key"], version=doc["version"], document=doc, sha256=_sha(doc))
            db.add(pol)
            db.flush()
            b.active_policy_id = pol.id
            ids[file] = b.id
            buyers.append({"slug": slug, "name": b.name, "policy": file,
                           "users": [(f"rv-{slug}@test.example", "Legacy reviewer", "reviewer")]})
    monkeypatch.setattr(seed_module, "BUYERS", buyers)
    return ids


def test_startup_upgrades_a_database_populated_by_the_previous_release(app_client, monkeypatch):
    ids = _legacy_buyers(monkeypatch, {"buyer_a.json": _legacy("buyer_a.json", version=1, label="Meridian-like v1"),
                                       "buyer_b.json": _legacy("buyer_b.json")})
    with session_scope() as db:
        slug_a = db.get(Buyer, ids["buyer_a.json"]).slug
        v1_id = db.get(Buyer, ids["buyer_a.json"]).active_policy_id
    # A request made under the old release, pinned to the old v1 (no automatic approval).
    v = vendor(app_client, "Legacy Pinned Private Limited", buyer=slug_a)
    old_req = _submit(v, "happy_nimbus")["request_id"]

    with TestClient(app_client.app):  # the real startup: seeding, self-test (worker off in tests)
        pass

    with session_scope() as db:
        kb = db.get(Buyer, ids["buyer_b.json"])
        kpol = db.get(Policy, kb.active_policy_id)
        assert kpol.version == 1 and "approval" not in kpol.document  # stored documents are never rewritten
        assert kpol.sha256 == _sha(kpol.document)
        mb = db.get(Buyer, ids["buyer_a.json"])
        assert db.get(Policy, mb.active_policy_id).version == 2  # the new file version became active
        assert "approval" not in db.get(Policy, v1_id).document
        rv_id = db.scalar(select(User.id).where(User.email == f"rv-{slug_a}@test.example"))
        assert db.get(Request, old_req).policy_id == v1_id
    rv = as_user(app_client, rv_id)
    _submit(v, "happy_nimbus", based_on=1)  # re-evaluated after the upgrade: still pinned to v1, still manual
    assert case(rv, old_req)["request"]["state"] == "READY_FOR_DECISION" and decisions(old_req) == []
    c = case(rv, old_req)
    assert rv.post(f"/api/review/requests/{old_req}/reject", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "reason": "Test: closing the v1 request",
        "vendor_explanation": "Closed for an upgrade test."}).status_code == 200
    new_req = _submit(vendor(app_client, "Legacy New Private Limited", buyer=slug_a), "happy_nimbus")["request_id"]
    assert decisions(new_req) == [("approved", "system", "auto_approve_clean")]  # new requests get v2


def test_a_real_difference_in_a_stored_version_is_still_refused(monkeypatch):
    stored = _legacy("buyer_b.json")
    stored["reviews"] = {**stored["reviews"], "gst_state_differs_from_address": "ignore"}
    _legacy_buyers(monkeypatch, {"buyer_b.json": stored})
    with pytest.raises(RuntimeError, match="changed on disk"):
        seed_module.seed()


# ---------------------------------------------------------------------------- another vendor, same account

def _drain():
    run_worker_until_idle(50)


def test_same_account_submitted_between_checks_and_publish_is_not_approved_automatically(app_client, monkeypatch):
    _drain()
    b = make_buyer(auto=True)
    va = vendor(app_client, "First Account Private Limited", buyer=b.slug)
    vb = vendor(app_client, "Second Account Private Limited", buyer=b.slug)
    req_a = _post_submit(va, "happy_nimbus")["request_id"]
    claimed = W.claim_one()
    assert claimed and claimed != W.EXHAUSTED
    from app.pipeline import runner
    real_emit, fired = runner.emit, []

    def emit(run_id, stage, *a, **k):
        if stage == "decide" and not fired:  # after the checks, before the publish
            fired.append(_post_submit(vb, "happy_nimbus")["request_id"])
        return real_emit(run_id, stage, *a, **k)

    monkeypatch.setattr(runner, "emit", emit)
    execute_run(claimed[0], claimed[1])
    monkeypatch.setattr(runner, "emit", real_emit)
    assert fired
    rv = as_user(app_client, b.reviewer_id)
    c = case(rv, req_a)
    assert decisions(req_a) == []
    dup = _check(c, "credibility:duplicate_bank")
    assert dup["status"] == "needs_review" and dup["evidence"]["other_vendor_records"] == 1
    titles = [e["title"] for e in rv.get(f"/api/runs/{claimed[0]}/events").json()["events"]]
    assert any(t.startswith("Checked again before publishing") for t in titles)
    assert not any(t.startswith("Approved automatically") for t in titles)
    _drain()
    assert decisions(fired[0]) == []  # the second application needs a person too


def test_a_submission_in_flight_holds_the_publish_until_it_commits(app_client, monkeypatch):
    """The second count is serialised with the submission: an uncommitted submission of the same account makes the
    publish wait, then the publish sees it. Without the shared account lock it would approve from a stale count."""
    _drain()
    b = make_buyer(auto=True)
    va = vendor(app_client, "Waiting Publish Private Limited", buyer=b.slug)
    vb = vendor(app_client, "In Flight Submission Private Limited", buyer=b.slug)
    req_a = _post_submit(va, "happy_nimbus")["request_id"]
    claimed = W.claim_one()
    assert claimed and claimed != W.EXHAUSTED
    form_b, docs_b = _load(vb, "happy_nimbus")
    in_flight, release = threading.Event(), threading.Event()
    real_remember = commands.remember

    def remember(db, user, action, *a, **k):
        if action == "submit" and user.vendor_id is not None and not in_flight.is_set():
            in_flight.set()  # written and locked, not committed
            assert release.wait(30)
        return real_remember(db, user, action, *a, **k)

    monkeypatch.setattr(commands, "remember", remember)
    out = {}
    tb = threading.Thread(target=lambda: out.setdefault("b", vb.post("/api/vendor/submit", json={
        "operation_id": op(), "form": form_b, "documents": docs_b})))
    tb.start()
    assert in_flight.wait(30)
    ta = threading.Thread(target=execute_run, args=claimed[:2])
    ta.start()
    waiting = False
    for _ in range(300):  # the publish must be waiting for the account lock held by the submission
        with session_scope() as db:
            if db.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")):
                waiting = True
                break
        if not ta.is_alive():
            break
        time.sleep(0.05)
    release.set()
    tb.join(30)
    ta.join(30)
    assert out["b"].status_code == 200, out["b"].text
    assert waiting, "the publish did not wait for the in-flight submission of the same account"
    assert decisions(req_a) == []
    assert _check(case(as_user(app_client, b.reviewer_id), req_a), "credibility:duplicate_bank")["status"] == "needs_review"
    _drain()


def test_a_reviewer_cannot_approve_on_a_count_that_has_since_changed(app_client):
    """The same rule in the shared finaliser: a later submission of the same account makes the assessment out of date;
    the approval is refused and the application is checked again."""
    _drain()
    b = make_buyer(auto=False)
    va = vendor(app_client, "Manual First Private Limited", buyer=b.slug)
    req_a = _submit(va, "happy_nimbus")["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    assert case(rv, req_a)["request"]["state"] == "READY_FOR_DECISION"
    _post_submit(vendor(app_client, "Manual Second Private Limited", buyer=b.slug), "happy_nimbus")
    r = _approve(rv, req_a)
    assert r.status_code == 409 and r.json()["error"] == "re_evaluating", r.text
    assert decisions(req_a) == []
    assert case(rv, req_a)["request"]["state"] == "PROCESSING"
    _drain()
    c = case(rv, req_a)
    assert c["request"]["state"] == "ACTION_REQUIRED"
    assert _check(c, "credibility:duplicate_bank")["status"] == "needs_review"


# ---------------------------------------------------------------------------- finaliser contract: it re-checks itself

def _break_open_task(db, req, a):
    db.add(Task(request_id=req.id, task_key="reviewer:other", owner="reviewer", kind="review", title="x", detail="x"))


@pytest.mark.parametrize("breaker,expect", [
    (lambda db, req, a: setattr(req, "state", "ACTION_REQUIRED"), "the application is action required"),
    (lambda db, req, a: setattr(a, "superseded", True), "current, ready assessment"),
    (lambda db, req, a: setattr(a, "summary", {**a.summary, "engine_version": "engine-0"}), "earlier version"),
    (_break_open_task, "not all satisfied"),
])
def test_the_shared_finaliser_rechecks_the_request_itself(app_client, breaker, expect):
    b = make_buyer(auto=False)
    req_id = _submit(vendor(app_client, "Finaliser Contract Private Limited", buyer=b.slug), "happy_nimbus")["request_id"]
    with session_scope() as db:
        req = db.get(Request, req_id)
        assert req.state == "READY_FOR_DECISION"
        a = db.get(Assessment, req.current_assessment_id)
        breaker(db, req, a)
        db.flush()
        with pytest.raises(CannotApprove, match=expect):
            approve_request(db, req, db.get(Vendor, req.vendor_id), db.get(Revision, req.current_revision_id), a.id,
                            actor_type="reviewer", actor_id=None, audit_actor_type="reviewer", rule=None, reason="r",
                            message_body="m")
        db.rollback()
    assert decisions(req_id) == []


# ---------------------------------------------------------------------------- a recovered run is named

def test_an_interrupted_and_recovered_run_is_named_in_the_automatic_decision(app_client):
    _drain()
    b = make_buyer(auto=True)
    v = vendor(app_client, "Recovered Attempt Private Limited", buyer=b.slug)
    req = _post_submit(v, "happy_nimbus")["request_id"]
    first = W.claim_one()
    with session_scope() as db:
        db.execute(text("UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"), {"id": first[0]})
    second = W.claim_one()
    assert second[0] == first[0] and second[2] == 2
    execute_run(second[0], second[1])
    assert decisions(req) == [("approved", "system", "auto_approve_clean")]
    c = case(as_user(app_client, b.reviewer_id), req)
    assert "Technical steps before this decision: 1 interrupted run recovered" in c["decisions"][-1]["reason"]
    with session_scope() as db:
        assert db.scalar(select(AuditEvent).where(AuditEvent.request_id == req, AuditEvent.event_type == "RUN_RECOVERED"))
        body = db.scalar(select(Message.body).where(Message.request_id == req, Message.kind == "approved"))
        assert "not bank verification" in body and "simulated" in body


# ---------------------------------------------------------------------------- a re-read reads one file

def _variant(name: str, tag: str) -> bytes:
    """New bytes for a fixture document (same text layer and gold label), so no earlier reading is reused."""
    src = pymupdf.open(stream=(FIXTURE_DIR / "documents" / name).read_bytes(), filetype="pdf")
    src.set_metadata({"subject": tag})
    data = src.tobytes()
    reader = get_reader()
    gold = hashlib.sha256((FIXTURE_DIR / "documents" / name).read_bytes()).hexdigest()
    reader._by_hash[hashlib.sha256(data).hexdigest()] = reader._by_hash[gold]
    return data


def test_rereading_one_file_does_not_read_another_failed_file(app_client, monkeypatch):
    _drain()
    b = make_buyer(auto=False)
    v = vendor(app_client, "Selective Reread Private Limited", buyer=b.slug)
    tag = uuid.uuid4().hex
    bank, gst = _variant("nimbus_bank_letter.pdf", tag), _variant("nimbus_gst.pdf", tag)
    reader = get_reader()
    bank_id, gst_id = _upload(v, bank, "bank.pdf"), _upload(v, gst, "gst.pdf")
    # An incomplete stored reading of the bank letter (IFSC left out), and a provider outage for the GST certificate.
    reading = reader.read(bank, [p.kind for p in inspect_pdf(bank, 10 * 2**20, 10).pages]).reading
    reading.fields["ifsc"] = reading.fields["ifsc"].model_copy(
        update={"status": "not_found", "value": None, "normalized": None, "provenance": "none", "page": None})
    with session_scope() as db:
        db.add(Extraction(document_id=bank_id, config_key=reader.config_key, status="partial", detected_type="bank_proof",
                          result=reading.model_dump(), provider="test-fixture", model="historical", latency_ms=0, usage={}))
    gst_sha = hashlib.sha256(gst).hexdigest()
    reader.fail_hashes.add(gst_sha)
    try:
        req = _submit(v, "happy_nimbus", replace={"bank_proof": bank_id, "gst_certificate": gst_id})["request_id"]
        rv = as_user(app_client, b.reviewer_id)
        c = case(rv, req)
        docs = c["assessment"]["summary"]["documents"]
        assert docs["bank_proof"]["status"] == "partial" and docs["gst_certificate"]["status"] == "failed"

        calls = []
        real_read = type(reader).read
        monkeypatch.setattr(type(reader), "read", lambda self, pdf, kinds: (
            calls.append(hashlib.sha256(pdf).hexdigest()), real_read(self, pdf, kinds))[1])
        r = rv.post(f"/api/review/requests/{req}/reread", json={
            "operation_id": op(), "expected_version": c["request"]["version"], "slot": "bank_proof"})
        assert r.status_code == 200, r.text
        _drain()
        assert calls == [hashlib.sha256(bank).hexdigest()]  # one model call, for the chosen file only
        docs = case(rv, req)["assessment"]["summary"]["documents"]
        assert docs["bank_proof"]["status"] == "complete" and docs["gst_certificate"]["status"] == "failed"
        titles = [e["title"] for e in rv.get(f"/api/runs/{r.json()['run_id']}/events").json()["events"]]
        assert any("not read again: this re-read covers only the selected file" in t for t in titles)
        monkeypatch.setattr(type(reader), "read", real_read)

        reader.fail_hashes.discard(gst_sha)  # the outage is over: an ordinary retry reads the failed file
        c = case(rv, req)
        assert rv.post(f"/api/review/requests/{req}/retry", json={
            "operation_id": op(), "expected_version": c["request"]["version"]}).status_code == 200
        _drain()
        assert case(rv, req)["assessment"]["summary"]["documents"]["gst_certificate"]["status"] == "complete"
    finally:
        reader.fail_hashes.discard(gst_sha)
