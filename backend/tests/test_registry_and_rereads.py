"""Manual registry lookups during an outage, trade names on scanned certificates, re-reading an incomplete reading,
supplier events about one document, and duplicate candidates.

API tests run on the real PostgreSQL test database with the test-only fixture reader. No live model calls.
"""

import copy

from sqlalchemy import select

from app.db import session_scope
from app.extraction.contract import ModelField, ModelReading, validate_reading
from app.extraction.service import FIXTURE_DIR, get_reader
from app.models import AuditEvent, Extraction, Verification
from app.pdf.inspect import inspect_pdf, page_texts
from app.pipeline.checks import run_checks
from app.pipeline.compare import compare
from app.pipeline.decide import route
from app.pipeline.evidence import EvalInput, ResolutionRec
from app.pipeline.runner import REREAD_REQUESTED

from .test_api_flow import case, op, run_worker_until_idle, staff, vendor
from .test_reviewer_judgements import _check, _image_only
from .test_clearances_and_visibility import _submit, _upload
from .test_safeguards import blockers
from .test_scenarios_logic import SCEN, evidence, policy, registry


# ---------------------------------------------------------------------------- manual lookups during an outage

def test_manual_lookup_during_an_outage_is_not_overwritten_api(app_client):
    rv = staff(app_client)
    assert rv.post("/api/review/demo/registry-outage", json={"on": True}).status_code == 200
    try:
        v = vendor(app_client, "Nimbus Outage Fallback Private Limited")
        req = _submit(v, "happy_nimbus")["request_id"]
        c = case(rv, req)
        assert _check(c, "verification:gst_registry_check")["owner"] == "system"
        f = SCEN["happy_nimbus"]["form"]
        r = rv.post(f"/api/review/requests/{req}/verification", json={
            "operation_id": op(), "expected_version": c["request"]["version"], "identifier": f["tax"]["gstin"],
            "result": "found", "legal_name": f["business"]["legal_name"], "status": "Active"})
        assert r.status_code == 200, r.text
        run_worker_until_idle()
        c = case(rv, req)
        assert _check(c, "verification:gst_registry_check")["status"] == "satisfied"
        with session_scope() as db:
            results = [(m, x) for m, x in db.execute(select(Verification.method, Verification.result)
                                                     .where(Verification.request_id == req, Verification.identifier_type == "GSTIN")
                                                         .order_by(Verification.id))]
        assert results == [("simulated_registry", "unavailable"), ("manual_registry", "found")]  # no lookup after it
    finally:
        rv.post("/api/review/demo/registry-outage", json={"on": False})


# ---------------------------------------------------------------------------- trade names on scanned certificates

def _trade_name_packet(gst_layout: str):
    s = SCEN["trade_name_supported"]
    docs = evidence(s["documents"])
    gst = next(d for d in docs if d.slot == "gst_certificate")
    gst.layout = gst_layout
    gst.reading.fields["trade_name"] = gst.reading.fields["trade_name"].model_copy(
        update={"status": "not_found", "value": None, "normalized": None, "provenance": "none", "page": None})
    return EvalInput(form=s["form"], docs=docs, policy=policy(), verifications=registry(s["form"]))


def test_trade_name_missed_on_a_scanned_gst_certificate_goes_to_a_reviewer():
    inp = _trade_name_packet("scanned")
    bh = blockers(run_checks(inp, compare(inp)))["credibility:bank_holder"]
    assert bh.owner == "reviewer" and bh.status == "needs_review"
    inp.resolutions = [ResolutionRec(bh.key, bh.fingerprint, "accepted", "Trade name printed on page 1", "Priya")]
    assert route(run_checks(inp, compare(inp))).outcome == "ready"


def test_absent_trade_name_on_a_digital_certificate_still_asks_the_vendor():
    inp = _trade_name_packet("digital")
    bh = blockers(run_checks(inp, compare(inp)))["credibility:bank_holder"]
    assert bh.owner == "vendor"  # a text layer that lacks the name is real absence, not supported automatically


# ---------------------------------------------------------------------------- re-reading an incomplete reading

def test_reviewer_can_reread_an_old_incomplete_reading_api(app_client):
    v = vendor(app_client, "Nimbus Old Partial Reading Private Limited")
    data = _image_only("nimbus_bank_letter.pdf")  # fresh bytes, never read
    doc_id = _upload(v, data, "bank_letter.pdf")
    # An incomplete reading stored by an older version: the reply had left out the IFSC although the file shows it.
    info = inspect_pdf(data, 10 * 2**20, 10)
    reader = get_reader()
    reading = reader.read(data, [p.kind for p in info.pages]).reading
    reading.fields["ifsc"] = reading.fields["ifsc"].model_copy(
        update={"status": "not_found", "value": None, "normalized": None, "provenance": "none", "page": None})
    with session_scope() as db:
        db.add(Extraction(document_id=doc_id, config_key=reader.config_key, status="partial", detected_type="bank_proof",
                          result=reading.model_dump(), provider="test-fixture", model="historical", latency_ms=0, usage={}))

    req = _submit(v, "happy_nimbus", replace={"bank_proof": doc_id})["request_id"]
    rv = staff(app_client)
    c = case(rv, req)
    assert "vendor:upload:bank_proof" in {t["key"] for t in c["tasks"] if t["status"] == "open"}
    assert c["assessment"]["summary"]["documents"]["bank_proof"]["status"] == "partial"

    # only an incomplete reading can be re-read
    bad = rv.post(f"/api/review/requests/{req}/reread", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "slot": "pan_card"})
    assert bad.status_code == 409 and bad.json()["error"] == "not_incomplete"

    r = rv.post(f"/api/review/requests/{req}/reread", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "slot": "bank_proof"})
    assert r.status_code == 200, r.text
    run_worker_until_idle()
    c = case(rv, req)
    assert c["assessment"]["summary"]["documents"]["bank_proof"]["status"] == "complete"
    assert "vendor:upload:bank_proof" not in {t["key"] for t in c["tasks"] if t["status"] == "open"}
    with session_scope() as db:
        statuses = [s for (s,) in db.execute(select(Extraction.status).where(Extraction.document_id == doc_id)
                                             .order_by(Extraction.id))]
        assert statuses == ["partial", REREAD_REQUESTED, "complete"]  # the old reading and the request stay on record
        assert db.scalar(select(AuditEvent).where(AuditEvent.request_id == req,
                                                  AuditEvent.event_type == "READING_REREAD_REQUESTED")) is not None


# ---------------------------------------------------------------------------- supplier events about one document

def test_vendor_events_say_which_ones_are_about_one_document_api(app_client):
    v = vendor(app_client, "Nimbus Progress Private Limited")
    run_id = _submit(v, "happy_nimbus")["run_id"]
    evs = v.get(f"/api/runs/{run_id}/events").json()["events"]
    reads = [e for e in evs if e["stage"] == "read"]
    per_doc = [e for e in reads if e["detail"] == {"scope": "document"}]
    assert len(per_doc) >= 5  # one per document at least
    summary = [e for e in reads if e["status"] in ("completed", "warning") and e["detail"] is None]
    assert len(summary) == 1 and summary[0]["title"].startswith("Read ")  # only the stage summary completes the stage
    assert all(set((e["detail"] or {})) <= {"scope"} for e in evs)  # nothing else leaks to the vendor


# ---------------------------------------------------------------------------- duplicate candidates

def test_a_conflicting_candidate_after_four_equal_ones_is_not_lost():
    data = (FIXTURE_DIR / "documents" / "nimbus_bank_letter.pdf").read_bytes()
    kinds = [p.kind for p in inspect_pdf(data, 10 * 2**20, 10).pages]
    alts = ["DECB0000412", "decb0000412", "DECB 0000412", "DECB0000412", "DECB0000413"]
    r = validate_reading(ModelReading(document_type="bank_proof", fields=[
        ModelField(key="ifsc", status="found", value="DECB0000412", alternatives=alts, page=1)]), kinds, page_texts(data))
    assert r.fields["ifsc"].status == "ambiguous"
    assert r.fields["ifsc"].alternatives == ["DECB0000412", "DECB0000413"]
    r2 = validate_reading(ModelReading(document_type="bank_proof", fields=[
        ModelField(key="ifsc", status="found", value="DECB0000412", alternatives=alts[:4], page=1)]), kinds, page_texts(data))
    assert r2.fields["ifsc"].status == "found" and r2.fields["ifsc"].alternatives == []  # same value spelt 4 ways


def test_scenario_readings_unchanged():
    # every demo document's reading keeps its status under the new candidate handling
    for key in ("happy_nimbus", "scanned_llp_buyer_b", "trade_name_supported"):
        for d in evidence(copy.deepcopy(SCEN[key])["documents"]):
            assert all(f.status != "ambiguous" for f in d.reading.fields.values()), (key, d.slot)
