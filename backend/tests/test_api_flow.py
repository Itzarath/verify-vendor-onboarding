"""End-to-end API tests against real PostgreSQL (test database) with the test-only fixture reader."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.pipeline.runner import execute_run
from app.pipeline.worker import EXHAUSTED, claim_one


def run_worker_until_idle(max_runs=10):
    n = 0
    while n < max_runs:
        c = claim_one()
        if not c:
            return n
        if c != EXHAUSTED:  # an exhausted run was recorded as failed; keep draining like the real worker
            execute_run(c[0], c[1])
        n += 1
    return n


def op():
    return uuid.uuid4().hex


def new_client(app_client) -> TestClient:
    return TestClient(app_client.app)


def vendor(app_client, name="Test Vendor", buyer="meridian"):
    c = new_client(app_client)
    r = c.post("/api/auth/new-vendor", json={"company_name": name, "buyer": buyer})
    assert r.status_code == 200, r.text
    return c


# What finance records when it accepts a bank change (commands.bank_change_confirmation).
BANK_CONFIRMATION = {"method": "call_back", "contact_source": "contract", "contact_person": "A. Rao, Accounts head",
                     "confirmed": True}


def staff(app_client, role="reviewer", buyer="Meridian"):
    c = new_client(app_client)
    p = c.get("/api/auth/personas").json()
    u = next(s for s in p["staff"] if s["role"] == role and s["buyer"].startswith(buyer))
    assert c.post("/api/auth/login", json={"user_id": u["id"]}).status_code == 200
    return c


def submit_sample(c, key, based_on=None, operation_id=None):
    s = c.post(f"/api/vendor/samples/{key}/load").json()
    body = {"operation_id": operation_id or op(), "form": s["form"],
            "documents": {k: v["document_id"] for k, v in s["documents"].items()}}
    if based_on is not None:
        body["based_on_revision"] = based_on
    r = c.post("/api/vendor/submit", json=body)
    return r, body


def case(rev, req_id):
    r = rev.get(f"/api/review/requests/{req_id}")
    assert r.status_code == 200, r.text
    return r.json()


def test_happy_path_end_to_end(app_client):
    v = vendor(app_client, "Nimbus Ledger Solutions Private Limited")
    r, body = submit_sample(v, "happy_nimbus")
    assert r.status_code == 200, r.text
    req_id = r.json()["request_id"]
    # duplicate delivery of the same operation -> same receipt, no second revision
    again = v.post("/api/vendor/submit", json=body)
    assert again.json()["revision"] == 1 and again.json().get("replayed")
    assert run_worker_until_idle() == 1
    rv = staff(app_client)
    c = case(rv, req_id)
    # Meridian's policy (v2) approves a clean application automatically: no person's judgement was needed.
    assert c["request"]["state"] == "APPROVED", [x for x in c["assessment"]["checks"] if x["blocking"]]
    d = c["decisions"][-1]
    assert d["outcome"] == "approved" and d["actor_type"] == "system" and d["rule"] == "auto_approve_clean"
    assert "SIMULATED" in d["reason"] and "not bank verification or payment authorisation" in d["reason"]
    stages = {e["stage"] for e in v.get(f"/api/runs/{c['runs'][0]['id']}/events").json()["events"]}
    assert stages == {"receive", "read", "compare", "check", "decide", "communicate"}
    again = rv.post(f"/api/review/requests/{req_id}/approve",
                    json={"operation_id": op(), "expected_version": c["request"]["version"], "assessment_id": c["assessment"]["id"]})
    assert again.status_code == 409  # already decided: no second approval
    ov = v.get("/api/vendor/overview").json()
    assert ov["current"]["public_status"] == "approved" and ov["vendor"]["has_approved_profile"]
    assert "approved automatically" in ov["current"]["messages"][0]["body"] and "simulated" in ov["current"]["messages"][0]["body"]
    assert ov["current"]["messages"][0]["kind"] == "approved"


def test_cross_vendor_access_denied(app_client):
    a = vendor(app_client, "Vendor A")
    b = vendor(app_client, "Vendor B")
    r, _ = submit_sample(a, "happy_nimbus")
    run_worker_until_idle()
    rv = staff(app_client)
    c = case(rv, r.json()["request_id"])
    doc_id = c["revisions"][0]["documents"][0]["document_id"]
    run_id = c["runs"][0]["id"]
    assert b.get(f"/api/documents/{doc_id}/file").status_code == 404
    assert b.get(f"/api/runs/{run_id}/events").status_code == 404
    assert b.get(f"/api/review/requests/{r.json()['request_id']}").status_code == 403
    # a vendor cannot use another vendor's document in its own submission
    s = b.post("/api/vendor/submit", json={"operation_id": op(), "form": {}, "documents": {"pan_card": doc_id}})
    assert s.status_code == 404
    # another buyer's reviewer cannot see it either
    other = staff(app_client, buyer="Kestrel")
    assert other.get(f"/api/review/requests/{r.json()['request_id']}").status_code == 404


def test_bank_change_correction_and_stale_approval(app_client):
    v = vendor(app_client, "Indigo Pixel Studios LLP")
    r, _ = submit_sample(v, "bank_change_rev1")
    req_id = r.json()["request_id"]
    run_worker_until_idle()
    rv = staff(app_client)
    c1 = case(rv, req_id)
    assert c1["request"]["state"] == "ACTION_REQUIRED"
    assert any(t["key"] == "vendor:upload:incorporation_certificate" and t["status"] == "open" for t in c1["tasks"])
    # vendor fixes the missing certificate AND changes bank account
    r2, _ = submit_sample(v, "bank_change_rev2", based_on=1)
    assert r2.status_code == 200 and r2.json()["revision"] == 2
    run_worker_until_idle()
    c2 = case(rv, req_id)
    open_keys = {t["key"] for t in c2["tasks"] if t["status"] == "open"}
    assert open_keys == {"finance:bank_change"}
    bank = next(x for x in c2["assessment"]["checks"] if x["key"] == "change:bank")
    # a procurement reviewer may not clear a finance review
    no = rv.post(f"/api/review/requests/{req_id}/resolve", json={
        "operation_id": op(), "expected_version": c2["request"]["version"], "check_key": "change:bank",
        "fingerprint": bank["evidence"]["fingerprint"], "outcome": "accepted", "note": "looks fine"})
    assert no.status_code == 403
    fin = staff(app_client, role="finance")
    ok = fin.post(f"/api/review/requests/{req_id}/resolve", json={
        "operation_id": op(), "expected_version": c2["request"]["version"], "check_key": "change:bank",
        "fingerprint": bank["evidence"]["fingerprint"], "outcome": "accepted", "note": "Called the vendor; new account verified",
        "confirmation": BANK_CONFIRMATION})
    assert ok.status_code == 200, ok.text
    run_worker_until_idle()
    c3 = case(rv, req_id)
    assert c3["request"]["state"] == "READY_FOR_DECISION"
    # reviewer has c3 open in a tab... meanwhile the vendor changes the bank account AGAIN (back to the old one)
    r3, _ = submit_sample(v, "bank_change_rev1", based_on=2)
    assert r3.status_code == 200
    stale = rv.post(f"/api/review/requests/{req_id}/approve", json={
        "operation_id": op(), "expected_version": c3["request"]["version"], "assessment_id": c3["assessment"]["id"]})
    assert stale.status_code == 409 and stale.json()["error"] == "stale_view"
    run_worker_until_idle()
    c4 = case(rv, req_id)
    assert c4["request"]["state"] == "ACTION_REQUIRED"
    assert "finance:bank_change" in {t["key"] for t in c4["tasks"] if t["status"] == "open"}  # earlier clearance no longer applies


def test_stale_vendor_tab_cannot_overwrite_newer_revision(app_client):
    v = vendor(app_client, "Two Tabs Pvt Ltd")
    r, _ = submit_sample(v, "bank_change_rev1")
    run_worker_until_idle()
    assert submit_sample(v, "bank_change_rev2", based_on=1)[0].status_code == 200
    late = submit_sample(v, "bank_change_rev2", based_on=1)[0]  # second tab, still thinks it's correcting rev 1
    assert late.status_code == 409 and late.json()["error"] == "stale_revision"


def test_superseded_run_cannot_publish(app_client):
    run_worker_until_idle()  # drain runs left queued by earlier tests
    v = vendor(app_client, "Race Condition LLP")
    r, _ = submit_sample(v, "bank_change_rev1")
    req_id = r.json()["request_id"]
    first = claim_one()  # worker picks up revision 1 ...
    assert first
    assert submit_sample(v, "bank_change_rev2", based_on=1)[0].status_code == 200  # ... vendor submits revision 2 meanwhile
    execute_run(first[0], first[1])  # old run finishes late
    rv = staff(app_client)
    c = case(rv, req_id)
    assert c["request"]["state"] == "PROCESSING"  # nothing published from the stale run
    assert {x["status"] for x in c["runs"] if x["id"] == first[0]} == {"SUPERSEDED"}
    run_worker_until_idle()
    c = case(rv, req_id)
    assert c["assessment"]["revision_id"] == c["current_revision_id"]


def test_conduct_refusal_auto_rejects_and_closes(app_client):
    v = vendor(app_client, "Refuser Pvt Ltd")
    r, _ = submit_sample(v, "conduct_declined")
    run_worker_until_idle()
    ov = v.get("/api/vendor/overview").json()
    assert ov["current"]["public_status"] == "rejected"
    again, _ = submit_sample(v, "happy_nimbus")
    assert again.status_code == 409 and again.json()["error"] == "request_closed"


def test_identity_conflict_reviewer_rejects_with_reason(app_client):
    v = vendor(app_client, "Harbourline Analytics Private Limited")
    r, _ = submit_sample(v, "identity_conflict")
    req_id = r.json()["request_id"]
    run_worker_until_idle()
    rv = staff(app_client)
    c = case(rv, req_id)
    assert c["request"]["state"] == "ACTION_REQUIRED"
    assert any(t["key"] == "vendor:gst_identity" for t in c["tasks"] if t["status"] == "open")
    bad = rv.post(f"/api/review/requests/{req_id}/reject", json={"operation_id": op(), "expected_version": c["request"]["version"],
                                                                  "reason": "x", "vendor_explanation": "y"})
    assert bad.status_code == 400
    ok = rv.post(f"/api/review/requests/{req_id}/reject", json={
        "operation_id": op(), "expected_version": c["request"]["version"],
        "reason": "GSTIN belongs to Seaview Data Consultants; vendor did not provide its own registration",
        "vendor_explanation": "We could not verify a GST registration held by Harbourline Analytics Private Limited."})
    assert ok.status_code == 200
    assert v.get("/api/vendor/overview").json()["current"]["public_status"] == "rejected"


def test_upload_intake_refusals(app_client):
    from app.extraction.service import FIXTURE_DIR
    v = vendor(app_client, "Upload Tester")
    for name, code in [("broken_truncated.pdf", "corrupt_pdf"), ("locked_password.pdf", "encrypted_pdf"), ("not_a_pdf.pdf", "not_pdf")]:
        data = (FIXTURE_DIR / "documents" / name).read_bytes()
        r = v.post("/api/vendor/documents", files={"file": (name, data, "application/pdf")})
        assert r.status_code == 400 and r.json()["error"] == code


def test_vendor_does_not_see_internal_signals(app_client):
    v = vendor(app_client, "Tejas Facility Services Private Limited")
    r, _ = submit_sample(v, "trade_name_supported")
    run_worker_until_idle()
    ov = v.get("/api/vendor/overview").json()
    assert ov["current"]["vendor_tasks"] == []
    evs = v.get(f"/api/runs/{ov['current']['last_run']['id']}/events").json()["events"]
    assert not any("trade name" in e["title"].lower() and e["stage"] == "check" and e["status"] == "warning" for e in evs)
