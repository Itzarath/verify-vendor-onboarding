"""Automatic approval of clean requests (policy setting approval.auto_approve_when_clean).

The rule is generic, so most tests create their OWN throwaway buyer with its own policy instead of relying on
Meridian: this tests the rule (any buyer, any policy version), and keeps each test independent of the others (the
prepared samples share bank accounts, which would raise duplicate-account reviews across tests).
"""

import copy
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.db import session_scope
from app.models import Buyer, Decision, Message, Policy, Request, User, VendorProfile
from app.pipeline import registry
from app.pipeline import worker as W
from app.pipeline.checks import CheckOutcome
from app.pipeline.finalise import CannotApprove, approve_request
from app.pipeline.runner import auto_approval_block, execute_run
from app.schemas.policy import BuyerPolicy

from .test_api_flow import case, new_client, op, run_worker_until_idle, vendor
from .test_reviewer_judgements import _check, _image_only, _resolve
from .test_clearances_and_visibility import _approve, _submit, _upload

POL = Path(__file__).resolve().parents[1] / "policies"


def _policy_doc(slug: str, version: int, auto: bool) -> dict:
    raw = json.loads((POL / "buyer_a.json").read_text(encoding="utf-8"))
    raw.update(policy_key=f"autotest-{slug}", version=version, label=f"Autotest policy v{version}",
               approval={"auto_approve_when_clean": auto})
    return BuyerPolicy.model_validate(raw).model_dump(mode="json")


def _add_policy(buyer_id: int, slug: str, version: int, auto: bool) -> None:
    doc = _policy_doc(slug, version, auto)
    with session_scope() as db:
        pol = Policy(buyer_id=buyer_id, policy_key=doc["policy_key"], version=version, document=doc,
                     sha256=hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest())
        db.add(pol)
        db.flush()
        db.get(Buyer, buyer_id).active_policy_id = pol.id


def make_buyer(auto: bool = True, version: int = 1) -> SimpleNamespace:
    """A fresh fictional buyer with its own policy, reviewer and finance user."""
    slug = "autotest-" + uuid.uuid4().hex[:8]
    with session_scope() as db:
        buyer = Buyer(slug=slug, name=f"Autotest {slug[-8:]} (fictional)")
        db.add(buyer)
        db.flush()
        rv = User(email=f"rv-{slug}@test.example", display_name="Test reviewer", role="reviewer", buyer_id=buyer.id)
        fin = User(email=f"fin-{slug}@test.example", display_name="Test finance", role="finance", buyer_id=buyer.id)
        db.add_all([rv, fin])
        db.flush()
        ids = SimpleNamespace(slug=slug, buyer_id=buyer.id, reviewer_id=rv.id, finance_id=fin.id)
    _add_policy(ids.buyer_id, slug, version, auto)
    return ids


def as_user(app_client, user_id: int):
    c = new_client(app_client)
    assert c.post("/api/auth/login", json={"user_id": user_id}).status_code == 200
    return c


def decisions(req: int) -> list:
    with session_scope() as db:
        return [(d.outcome, d.actor_type, d.rule) for d in db.scalars(select(Decision).where(Decision.request_id == req))]


def decide_events(c: dict) -> list[str]:
    return [e["title"] for e in c.get("run_events", [])]


# ---------------------------------------------------------------------------- the clean path

def test_clean_application_is_approved_automatically_with_honest_wording(app_client):
    b = make_buyer(auto=True)
    v = vendor(app_client, "Clean Supplier Private Limited", buyer=b.slug)
    req = _submit(v, "happy_nimbus")["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    c = case(rv, req)
    assert c["request"]["state"] == "APPROVED"
    assert decisions(req) == [("approved", "system", "auto_approve_clean")]
    reason = c["decisions"][-1]["reason"]
    assert "SIMULATED" in reason and "not bank verification or payment authorisation" in reason
    # optional / not-applicable items are tolerated, and not counted as passed
    statuses = {x["status"] for x in c["assessment"]["checks"]}
    assert statuses <= {"satisfied", "info", "not_applicable"} and statuses & {"info", "not_applicable"}
    assert "Not counted as passed" in reason
    with session_scope() as db:
        kinds = [m.kind for m in db.scalars(select(Message).where(Message.request_id == req).order_by(Message.id))]
        assert kinds == ["received", "approved"]  # no "awaiting final approval" message
        assert db.scalar(select(VendorProfile).where(VendorProfile.request_id == req)) is not None
    run_id = c["runs"][0]["id"]
    titles = [e["title"] for e in rv.get(f"/api/runs/{run_id}/events").json()["events"]]
    assert any(t.startswith("Approved automatically") for t in titles)
    assert not any("ready for final approval" in t for t in titles)


def test_vendor_only_correction_still_qualifies(app_client):
    b = make_buyer(auto=True)
    v = vendor(app_client, "Late Bank Letter Private Limited", buyer=b.slug)
    s = v.post("/api/vendor/samples/happy_nimbus/load").json()
    docs = {k: d["document_id"] for k, d in s["documents"].items() if k != "bank_proof"}
    r = v.post("/api/vendor/submit", json={"operation_id": op(), "form": s["form"], "documents": docs})
    assert r.status_code == 200, r.text
    run_worker_until_idle()
    req = r.json()["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    c = case(rv, req)
    assert c["request"]["state"] == "ACTION_REQUIRED"
    assert {t["owner"] for t in c["tasks"] if t["status"] == "open"} == {"vendor"}
    _submit(v, "happy_nimbus", based_on=1)  # the vendor supplies the missing document
    assert case(rv, req)["request"]["state"] == "APPROVED"
    assert decisions(req) == [("approved", "system", "auto_approve_clean")]


# ---------------------------------------------------------------------------- human judgement keeps human approval

def test_resolved_review_history_keeps_human_final_approval(app_client):
    b = make_buyer(auto=True)
    v = vendor(app_client, "Trade Name Supplier Private Limited", buyer=b.slug)
    req = _submit(v, "trade_name_supported")["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    _resolve(rv, req, "credibility:bank_holder", "accepted", "Trade name on the GST certificate")
    c = case(rv, req)
    assert c["request"]["state"] == "READY_FOR_DECISION"  # accepting a finding is not approving the vendor
    titles = [e["title"] for e in rv.get(f"/api/runs/{c['runs'][0]['id']}/events").json()["events"]]
    assert any(t.startswith("Not approved automatically: a person reviewed a finding") for t in titles)
    assert decisions(req) == []
    assert _approve(rv, req).status_code == 200
    assert decisions(req) == [("approved", "reviewer", None)]


def test_scanned_values_still_need_a_person_and_then_a_person_approves(app_client):
    b = make_buyer(auto=True)
    v = vendor(app_client, "Scanned Letter Private Limited", buyer=b.slug)
    scan = _upload(v, _image_only("nimbus_bank_letter.pdf"), "bank_scan.pdf")
    req = _submit(v, "happy_nimbus", replace={"bank_proof": scan})["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    assert _check(case(rv, req), "confirm:bank_proof")["status"] == "needs_review"
    _resolve(rv, req, "confirm:bank_proof", "accepted", "Checked against page 1")
    assert case(rv, req)["request"]["state"] == "READY_FOR_DECISION"
    assert decisions(req) == []


def test_finance_review_history_keeps_human_final_approval(app_client):
    b = make_buyer(auto=True)
    v = vendor(app_client, "Changed Bank Supplier LLP", buyer=b.slug)
    req = _submit(v, "bank_change_rev1")["request_id"]
    _submit(v, "bank_change_rev2", based_on=1)
    rv, fin = as_user(app_client, b.reviewer_id), as_user(app_client, b.finance_id)
    _resolve(fin, req, "change:bank", "accepted", "Called the vendor; verified")
    c = case(rv, req)
    assert c["request"]["state"] == "READY_FOR_DECISION", [x["key"] for x in c["assessment"]["checks"] if x["blocking"]]
    assert decisions(req) == []


def test_manual_registry_lookup_keeps_human_final_approval(app_client):
    b = make_buyer(auto=True)
    registry.state.set_outage(b.buyer_id, True)
    try:
        v = vendor(app_client, "Outage Day Private Limited", buyer=b.slug)
        req = _submit(v, "happy_nimbus")["request_id"]
        rv = as_user(app_client, b.reviewer_id)
        c = case(rv, req)
        assert c["request"]["state"] == "ACTION_REQUIRED"  # an unavailable registry keeps it pending
        assert _check(c, "verification:gst_registry_check")["owner"] == "system"
        f = v.post("/api/vendor/samples/happy_nimbus/load").json()["form"]
        r = rv.post(f"/api/review/requests/{req}/verification", json={
            "operation_id": op(), "expected_version": c["request"]["version"], "identifier": f["tax"]["gstin"],
            "result": "found", "legal_name": f["business"]["legal_name"], "status": "Active"})
        assert r.status_code == 200, r.text
        run_worker_until_idle()
        assert case(rv, req)["request"]["state"] == "READY_FOR_DECISION"  # a person recorded the lookup
        assert decisions(req) == []
    finally:
        registry.state.set_outage(b.buyer_id, False)


def test_technical_retry_after_an_outage_does_not_disqualify_but_is_named(app_client):
    b = make_buyer(auto=True)
    registry.state.set_outage(b.buyer_id, True)
    v = vendor(app_client, "Retry After Outage Private Limited", buyer=b.slug)
    req = _submit(v, "happy_nimbus")["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    registry.state.set_outage(b.buyer_id, False)
    c = case(rv, req)
    r = rv.post(f"/api/review/requests/{req}/retry", json={"operation_id": op(), "expected_version": c["request"]["version"]})
    assert r.status_code == 200, r.text
    run_worker_until_idle()
    c = case(rv, req)
    assert c["request"]["state"] == "APPROVED"
    assert "Technical steps before this decision: 1 retry requested" in c["decisions"][-1]["reason"]


# ---------------------------------------------------------------------------- policy pinning and the switch itself

def test_policy_off_stays_manual(app_client):
    b = make_buyer(auto=False)
    v = vendor(app_client, "Manual Buyer Supplier Private Limited", buyer=b.slug)
    req = _submit(v, "happy_nimbus")["request_id"]
    assert case(as_user(app_client, b.reviewer_id), req)["request"]["state"] == "READY_FOR_DECISION"
    assert decisions(req) == []


def test_a_request_keeps_the_policy_version_it_started_under(app_client):
    b = make_buyer(auto=False, version=1)
    v = vendor(app_client, "Pinned Policy Private Limited", buyer=b.slug)
    req = _submit(v, "happy_nimbus")["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    assert case(rv, req)["request"]["state"] == "READY_FOR_DECISION"
    _add_policy(b.buyer_id, b.slug, 2, auto=True)  # the buyer switches automatic approval on in a new version
    _submit(v, "happy_nimbus", based_on=1)  # the old request is re-evaluated: still pinned to v1
    assert case(rv, req)["request"]["state"] == "READY_FOR_DECISION"
    assert decisions(req) == []
    # a new application under the new version is approved automatically (the old one is closed first, so its bank
    # account doesn't count as shared)
    c = case(rv, req)
    assert rv.post(f"/api/review/requests/{req}/reject", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "reason": "Test: closing the v1 request",
        "vendor_explanation": "Closed for a policy-version test."}).status_code == 200
    v2 = vendor(app_client, "Second Pinned Policy Private Limited", buyer=b.slug)
    req2 = _submit(v2, "happy_nimbus")["request_id"]
    assert case(rv, req2)["request"]["state"] == "APPROVED"


def test_a_non_test_policy_cannot_switch_automatic_approval_on():
    raw = json.loads((POL / "buyer_a.json").read_text(encoding="utf-8"))
    raw.update(is_test_policy=False, verification=[{"id": "gst", "identifier": "GSTIN", "accepted_methods": ["manual_registry"]}],
               approval={"auto_approve_when_clean": True})
    with pytest.raises(ValueError, match="only a test policy"):
        BuyerPolicy.model_validate(raw)
    assert BuyerPolicy.model_validate({**raw, "approval": {}}).approval.auto_approve_when_clean is False  # default off


# ---------------------------------------------------------------------------- races and recovery: exactly one outcome

def test_a_recovered_run_publishes_exactly_one_approval(app_client):
    b = make_buyer(auto=True)
    v = vendor(app_client, "Recovered Run Private Limited", buyer=b.slug)
    s = v.post("/api/vendor/samples/happy_nimbus/load").json()
    r = v.post("/api/vendor/submit", json={"operation_id": op(), "form": s["form"],
                                           "documents": {k: d["document_id"] for k, d in s["documents"].items()}})
    req = r.json()["request_id"]
    first = W.claim_one()  # a worker takes the run, then stalls
    assert first and first != W.EXHAUSTED
    with session_scope() as db:
        db.execute(text("UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"), {"id": first[0]})
    second = W.claim_one()  # the lease expired: another worker reclaims it
    assert second and second[0] == first[0] and second[1] != first[1]
    execute_run(first[0], first[1])  # the stale worker wakes up: it must not publish
    execute_run(second[0], second[1])
    assert decisions(req) == [("approved", "system", "auto_approve_clean")]
    with session_scope() as db:
        assert len(db.scalars(select(VendorProfile).where(VendorProfile.request_id == req)).all()) == 1
        assert [m.kind for m in db.scalars(select(Message).where(Message.request_id == req))].count("approved") == 1


def test_the_shared_finaliser_refuses_a_second_approval(app_client):
    b = make_buyer(auto=True)
    v = vendor(app_client, "Double Approval Private Limited", buyer=b.slug)
    req = _submit(v, "happy_nimbus")["request_id"]
    with session_scope() as db:
        r = db.get(Request, req)
        from app.models import Revision, Vendor
        with pytest.raises(CannotApprove):
            approve_request(db, r, db.get(Vendor, r.vendor_id), db.get(Revision, r.current_revision_id),
                            r.current_assessment_id, actor_type="system", actor_id=None, audit_actor_type="system",
                            rule="auto_approve_clean", reason="again", message_body="again")
        db.rollback()
    assert decisions(req) == [("approved", "system", "auto_approve_clean")]


# ---------------------------------------------------------------------------- the eligibility rule, case by case

def _ok_checks():
    return [CheckOutcome("required_fields", "completeness", "satisfied", "t", "r"),
            CheckOutcome("document:other", "completeness", "info", "t", "r"),
            CheckOutcome("verification:gst_registry_check", "verification", "satisfied", "t", "r", evidence={"method": "simulated_registry"})]


def _ctx(auto=True, kind="onboarding", profile=None, same_policy=True):
    pol = BuyerPolicy.model_validate(_policy_doc("unit", 1, auto))
    return (SimpleNamespace(kind=kind, policy_id=1, id=0), SimpleNamespace(policy_id=1 if same_policy else 2),
            SimpleNamespace(current_profile_id=profile), pol)


@pytest.mark.parametrize("change,expect", [
    ({"auto": False}, "does not approve automatically"),
    ({"same_policy": False}, "does not approve automatically"),
    ({"kind": "change"}, "first onboarding"),
    ({"profile": 7}, "first onboarding"),
])
def test_eligibility_refuses_by_request_and_policy(change, expect):
    req, run, vendor_, pol = _ctx(**change)
    assert expect in auto_approval_block(None, req, run, vendor_, _ok_checks(), pol)


@pytest.mark.parametrize("status", ["unable", "needs_review", "not_satisfied"])
def test_eligibility_refuses_any_check_that_is_not_fully_satisfied_even_if_non_blocking(status):
    req, run, vendor_, pol = _ctx()
    checks = _ok_checks() + [CheckOutcome("document:optional_scan", "completeness", status, "t", "r", blocking=False)]
    assert "not fully satisfied" in auto_approval_block(None, req, run, vendor_, checks, pol)


def test_eligibility_needs_the_required_verification_to_have_passed():
    req, run, vendor_, pol = _ctx()
    checks = [c for c in _ok_checks() if not c.key.startswith("verification:")]
    assert "required verification" in auto_approval_block(None, req, run, vendor_, checks, pol)
