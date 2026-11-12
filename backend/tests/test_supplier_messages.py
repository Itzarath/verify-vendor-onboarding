"""What the supplier is told: only the reviewer's request (never the internal reasoning), certificates edited after
signing, Composition taxpayers, no blanket "every check passed", and bank-change wording.

API tests run on the real PostgreSQL test database with the test-only fixture reader. No live model calls.
"""

import json

from app.pdf.signature import read_signatures
from app.pipeline.checks import run_checks
from app.pipeline.compare import compare
from app.pipeline.decide import compose_vendor_message
from app.pipeline.evidence import ResolutionRec, VerificationRec

from .test_api_flow import case, op, run_worker_until_idle, staff, vendor
from .test_verification_and_risk import _gst_checks, _gst_doc_with, _signed_pdf
from .test_clearances_and_visibility import _submit
from .test_scenarios_logic import SCEN, blockers, evaluate, evidence, first_submission

INTERNAL = "INTERNAL_NOTE_7731: looks like a fraud pattern we saw last year"


# ---------------------------------------------------------------------------- the supplier sees only the request

def test_a_rejected_review_shows_the_vendor_only_the_request_never_the_reasoning(app_client):
    v = vendor(app_client, "Private Reasoning Private Limited")
    req = _submit(v, "trade_name_supported")["request_id"]
    rv = staff(app_client)
    c = case(rv, req)
    x = next(k for k in c["assessment"]["checks"] if k["key"] == "credibility:bank_holder")
    ask = "Please upload a bank letter that names the company as the account holder."
    r = rv.post(f"/api/review/requests/{req}/resolve", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "check_key": "credibility:bank_holder",
        "fingerprint": x["evidence"]["fingerprint"], "outcome": "not_accepted", "note": INTERNAL, "vendor_text": ask})
    assert r.status_code == 200, r.text
    run_worker_until_idle()

    o = v.get("/api/vendor/overview").json()["current"]
    seen = json.dumps(o) + json.dumps(v.get(f"/api/runs/{o['last_run']['id']}/events").json()["events"])
    assert "INTERNAL_NOTE_7731" not in seen  # tasks, messages and the run log
    assert ask in json.dumps(o["vendor_tasks"]) and ask in json.dumps(o["messages"])
    # The reviewer still sees both, kept apart.
    c = case(rv, req)
    assert c["resolutions"][-1]["note"] == INTERNAL and c["resolutions"][-1]["vendor_text"] == ask


def test_an_earlier_decision_without_a_vendor_request_gets_a_neutral_one():
    inp, _, checks, _ = evaluate("trade_name_supported")
    fp = blockers(checks)["credibility:bank_holder"].fingerprint
    inp.resolutions, inp.revision_id = [ResolutionRec("credibility:bank_holder", fp, "not_accepted", INTERNAL, "Priya",
                                                      revision_id=1)], 1
    c = blockers(run_checks(inp, compare(inp)))["credibility:bank_holder"]
    assert c.owner == "vendor" and "INTERNAL_NOTE_7731" not in c.vendor_text and "could not accept" in c.vendor_text
    assert INTERNAL in c.reason  # the reviewer's own view keeps the reasoning


# ---------------------------------------------------------------------------- certificates edited after signing

def test_an_edited_signed_file_is_recorded_as_changed_after_signing():
    ok = read_signatures(_signed_pdf())
    edited = {"signatures": [{**ok["signatures"][0], "status": "broken"}], "changed_after_signing": False}
    c = _gst_doc_with(edited)
    assert c.status == "needs_review" and "changed after" in c.reason
    assert c.evidence["changed_after_signing"] is True and c.evidence["content_added_after_signing"] is False
    clean = _gst_doc_with(ok)
    assert clean.evidence["changed_after_signing"] is False


# ---------------------------------------------------------------------------- Composition taxpayers

def test_a_composition_supplier_gets_no_input_tax_credit_advice():
    form = SCEN["happy_nimbus"]["form"]
    g = form["tax"]["gstin"]
    found = {"legal_name": form["business"]["legal_name"].upper(), "status": "Active"}

    def returns(*observed):
        recs = [VerificationRec(i + 1, "GSTIN", g, "manual_registry", "found", {**found, **o}, "x", "reviewer")
                for i, o in enumerate(observed)]
        return _gst_checks("happy_nimbus", verifications=recs)["verification:gst_returns"]
    gap = {"taxpayer_type": "Composition", "gstr3b_missed": 3, "gstr1_missed": 2}
    for c in (returns(gap), returns(gap, {})):  # also after a later lookup that left everything blank
        assert c.status == "needs_review" and c.owner == "finance"
        assert "no input tax credit is involved" in c.reason
        assert "at risk" not in c.reason and "hold the GST portion" not in c.reason
    c = returns({**gap, "taxpayer_type": "Regular"})
    assert "at risk" in c.reason and "Rule 37A" in c.reason


# ---------------------------------------------------------------------------- no blanket "every check passed"

def test_the_supplier_is_not_told_every_automated_check_passed():
    _, _, _, routing = evaluate("happy_nimbus")
    assert routing.outcome == "ready"
    m = compose_vendor_message(routing, "Nimbus Ledger Solutions Private Limited", 2, [])
    assert "passed all automated checks" not in m.body and "checks and reviews are complete" in m.body


# ---------------------------------------------------------------------------- bank-change wording

def test_a_bank_change_never_claims_the_proof_is_consistent():
    s1 = SCEN["bank_change_rev1"]
    prev_docs = {d.slot: d.sha256 for d in evidence(s1["documents"])}
    _, _, checks, _ = evaluate("bank_change_rev2", revision_number=2, previous_form=s1["form"], previous_docs=prev_docs,
                               baselines=first_submission(s1))
    reason = blockers(checks)["change:bank"].reason
    assert "consistent" not in reason and "listed separately" in reason
