"""Reviewer judgements bound to the evidence they saw, judgements that return to the reviewer after the supplier answers,
stable confirmation fingerprints, uploads and file names, values hidden by an answer, manual registry results and
incomplete model replies.

API tests run on the real PostgreSQL test database with the test-only fixture reader. No live model calls. Scripted model replies go through the real validator.
"""

import copy
import io
import threading
import time

import pytest
from sqlalchemy import select

from app.db import get_engine
from app.extraction.contract import DocumentReading, ModelField, ModelReading, validate_reading
from app.extraction.provider import LLMError
from app.extraction.reader import _validator
from app.extraction.service import FIXTURE_DIR, get_reader
from app.models import Document, User, Verification
from app.pipeline.checks import run_checks
from app.pipeline.compare import compare
from app.pipeline.decide import route
from app.pipeline.evidence import DocEvidence, EvalInput, ResolutionRec, VerificationRec
from app.services import commands

from .test_api_flow import BANK_CONFIRMATION, case, op, run_worker_until_idle, staff, vendor
from .test_clearances_and_visibility import _approve, _submit, _upload, _variant
from .test_safeguards import _reading, blockers, clear_reviewer_items
from .test_scenarios_logic import READER, SCEN, evaluate, evidence, policy, registry


def _check(c: dict, key: str) -> dict:
    return next(x for x in c["assessment"]["checks"] if x["key"] == key)


def _resolve(rv, req: int, key: str, outcome: str, note: str):
    c = case(rv, req)
    x = _check(c, key)
    r = rv.post(f"/api/review/requests/{req}/resolve", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "check_key": key,
        "fingerprint": x["evidence"]["fingerprint"], "outcome": outcome, "note": note,
        "confirmation": BANK_CONFIRMATION if key == "change:bank" else None})
    assert r.status_code == 200, r.text
    run_worker_until_idle()


# ---------------------------------------------------------------------------- a bank-holder clearance is bound to its proof

def test_bank_holder_clearance_is_bound_to_the_proof_and_gst_files_logic():
    inp, _, checks, _ = evaluate("trade_name_supported")
    fp = blockers(checks)["credibility:bank_holder"].fingerprint
    inp.resolutions = [ResolutionRec("credibility:bank_holder", fp, "accepted", "Trade name on GST certificate", "Priya")]
    assert route(run_checks(inp, compare(inp))).outcome == "ready"
    for slot in ("bank_proof", "gst_certificate"):
        other = copy.deepcopy(inp)
        other.doc_in_slot(slot).sha256 = "f" * 64  # same facts, different file
        b = blockers(run_checks(other, compare(other)))
        assert "credibility:bank_holder" in b and b["credibility:bank_holder"].status == "needs_review", slot


def test_replaced_bank_proof_cannot_ride_on_the_old_ownership_review_api(app_client):
    v = vendor(app_client, "Tejas Replaced Cheque Private Limited")
    req = _submit(v, "trade_name_supported")["request_id"]
    rv = staff(app_client)
    clear_reviewer_items(rv, req)
    assert case(rv, req)["request"]["state"] == "READY_FOR_DECISION"

    new_proof = _upload(v, _variant("tejas_cheque.pdf", "Replacement copy issued on request"), "cheque_copy.pdf")
    _submit(v, "trade_name_supported", based_on=1, replace={"bank_proof": new_proof})
    c = case(rv, req)
    bh = _check(c, "credibility:bank_holder")
    assert c["request"]["state"] == "ACTION_REQUIRED"
    assert bh["status"] == "needs_review" and bh["owner"] == "reviewer"
    assert _approve(rv, req).status_code == 409
    c = clear_reviewer_items(rv, req)  # a person reviews the replacement -> eligible again
    assert c["request"]["state"] == "READY_FOR_DECISION" and _approve(rv, req).status_code == 200


# ---------------------------------------------------------------------------- rejected judgements and supplier clarifications

def test_a_rejected_judgement_returns_to_the_reviewer_after_the_vendor_answers_logic():
    inp, _, checks, _ = evaluate("scanned_llp_buyer_b", pol="buyer_b.json")
    fp = blockers(checks)["confirm:pan_card"].fingerprint
    no = ResolutionRec("confirm:pan_card", fp, "not_accepted", "Please explain the PAN on page 1", "Priya", revision_id=1)
    inp.resolutions, inp.revision_id = [no], 1
    c = blockers(run_checks(inp, compare(inp)))["confirm:pan_card"]
    assert c.owner == "vendor" and c.status == "not_satisfied"  # same revision: the request to the vendor stands
    inp.revision_id, inp.revision_number = 2, 2  # the vendor answered with revision 2, same file
    c = blockers(run_checks(inp, compare(inp)))["confirm:pan_card"]
    assert c.owner == "reviewer" and c.status == "needs_review"
    assert c.evidence["previous_judgement"]["note"] == "Please explain the PAN on page 1"


def test_vendor_clarification_can_be_accepted_by_the_reviewer_api(app_client):
    v = vendor(app_client, "Tejas Clarified Private Limited")
    req = _submit(v, "trade_name_supported")["request_id"]
    rv = staff(app_client)
    _resolve(rv, req, "credibility:bank_holder", "not_accepted", "Please send a bank letter for this account")
    bh = _check(case(rv, req), "credibility:bank_holder")
    assert bh["status"] == "not_satisfied" and bh["owner"] == "vendor"

    s = v.post("/api/vendor/samples/trade_name_supported/load").json()
    r = v.post("/api/vendor/submit", json={
        "operation_id": op(), "form": s["form"], "based_on_revision": 1,
        "documents": {k: d["document_id"] for k, d in s["documents"].items()},
        "vendor_note": "Our bank confirms the account belongs to the company; letter attached to our email."})
    assert r.status_code == 200, r.text
    run_worker_until_idle()
    bh = _check(case(rv, req), "credibility:bank_holder")
    assert bh["status"] == "needs_review" and bh["owner"] == "reviewer"
    _resolve(rv, req, "credibility:bank_holder", "accepted", "Bank letter checked")
    assert _check(case(rv, req), "credibility:bank_holder")["status"] == "satisfied"


# ---------------------------------------------------------------------------- confirmation fingerprints

def _image_only(name: str) -> bytes:
    """An image-only copy of a fixture (every value needs a person's confirmation). Its gold label is the original's."""
    import hashlib

    import pymupdf
    src = pymupdf.open(stream=(FIXTURE_DIR / "documents" / name).read_bytes(), filetype="pdf")
    out = pymupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=110)
        out.new_page(width=page.rect.width, height=page.rect.height).insert_image(page.rect, pixmap=pix)
    data = out.tobytes()
    gold = hashlib.sha256((FIXTURE_DIR / "documents" / name).read_bytes()).hexdigest()
    for reader in (READER, get_reader()):  # the scenario tests' reader, and the app's when the API tests run
        if hasattr(reader, "_by_hash"):
            reader._by_hash[hashlib.sha256(data).hexdigest()] = reader._by_hash[gold]
    return data


def _jsonb_order(reading: DocumentReading) -> DocumentReading:
    """What PostgreSQL JSONB gives back: object keys ordered by length, then bytes."""
    d = reading.model_dump()
    d["fields"] = {k: d["fields"][k] for k in sorted(d["fields"], key=lambda k: (len(k), k))}
    return DocumentReading.model_validate(d)


def test_field_order_does_not_change_a_confirmation_fingerprint_logic():
    from app.pdf.inspect import inspect_pdf
    data = _image_only("nimbus_bank_letter.pdf")
    info = inspect_pdf(data, 10 * 2**20, 10)
    out = READER.read(data, [p.kind for p in info.pages])
    s = SCEN["happy_nimbus"]
    docs = [d for d in evidence(s["documents"]) if d.slot != "bank_proof"]
    bank = DocEvidence("bank_proof", 99, "scan.pdf", info.sha256, info.layout, info.page_count, out.status, out.reading)
    inp = EvalInput(form=s["form"], docs=[*docs, bank], policy=policy(), verifications=registry(s["form"]))
    first = blockers(run_checks(inp, compare(inp)))["confirm:bank_proof"]
    asked = [f["field"] for f in first.evidence["unconfirmed_fields"]]
    assert set(asked) == {"account_holder", "account_number", "ifsc"}
    reordered = _jsonb_order(bank.reading)
    assert [k for k in reordered.fields if k in asked] != [k for k in bank.reading.fields if k in asked]  # really differs
    bank.reading = reordered
    inp.docs.reverse()  # and documents finishing in another order
    assert blockers(run_checks(inp, compare(inp)))["confirm:bank_proof"].fingerprint == first.fingerprint


def test_confirmed_fresh_scan_stays_confirmed_after_the_cache_round_trip_api(app_client):
    v = vendor(app_client, "Nimbus Fresh Scan Private Limited")
    fresh = _upload(v, _image_only("nimbus_bank_letter.pdf"), "bank_scan.pdf")  # never read before: a fresh reading
    req = _submit(v, "happy_nimbus", replace={"bank_proof": fresh})["request_id"]
    rv = staff(app_client)
    assert _check(case(rv, req), "confirm:bank_proof")["status"] == "needs_review"
    _resolve(rv, req, "confirm:bank_proof", "accepted", "Checked against page 1")  # re-evaluation reads the stored copy
    x = _check(case(rv, req), "confirm:bank_proof")
    assert x["status"] == "satisfied", x["reason"]


# ---------------------------------------------------------------------------- opening any file name

@pytest.mark.parametrize("name", ["PAN – final.pdf", "बैंक प्रमाण.pdf", 'odd"name\\.pdf'])
def test_any_filename_can_be_opened_api(app_client, name):
    v = vendor(app_client, f"Unicode Name {abs(hash(name))} Private Limited")
    doc = _upload(v, (FIXTURE_DIR / "documents" / "nimbus_pan.pdf").read_bytes(), name)
    r = v.get(f"/api/documents/{doc}/file")
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    assert "filename*=UTF-8''" in r.headers["content-disposition"]


# ---------------------------------------------------------------------------- simultaneous uploads of one file

def test_two_simultaneous_uploads_of_the_same_file_share_one_document(app_client):
    from sqlalchemy.orm import Session
    v = vendor(app_client, "Two Tabs Private Limited")
    uid = v.get("/api/auth/me").json()["id"]
    data = (FIXTURE_DIR / "documents" / "nimbus_gst.pdf").read_bytes()
    first_flushed, release, out = threading.Event(), threading.Event(), {}

    def tab_a():
        with Session(get_engine()) as db:
            out["a"] = commands.upload_document(db, db.get(User, uid), "gst.pdf", data).id
            first_flushed.set()
            release.wait(10)  # hold the uncommitted insert while tab B tries the same file
            db.commit()

    def tab_b():
        with Session(get_engine()) as db:
            out["b"] = commands.upload_document(db, db.get(User, uid), "gst.pdf", data).id
            db.commit()

    a = threading.Thread(target=tab_a)
    a.start()
    assert first_flushed.wait(10)
    b = threading.Thread(target=tab_b)
    b.start()
    time.sleep(1.0)  # B has missed A's uncommitted row and is now waiting on the unique index
    release.set()
    a.join(10), b.join(10)
    assert out.get("a") and out.get("a") == out.get("b")
    with Session(get_engine()) as db:
        vid = db.get(User, uid).vendor_id
        assert len(db.scalars(select(Document).where(Document.vendor_id == vid)).all()) == 1


# ---------------------------------------------------------------------------- values hidden by an answer

@pytest.mark.parametrize("answer,ident,task", [("tax.gst_registered", "tax.gstin", "vendor:gstin"),
                                               ("tax.msme_registered", "tax.udyam_number", "vendor:msme")])
def test_a_number_hidden_by_answering_no_does_not_block(answer, ident, task):
    s = copy.deepcopy(SCEN["happy_nimbus"])
    sec, a = answer.split(".")
    _, i = ident.split(".")
    s["form"][sec][a] = "no"
    s["form"][sec][i] = "27ZZZZZ0000Z1Z5" if i == "gstin" else "UDYAM-MH-00-0000000"  # left over, now hidden
    inp = EvalInput(form=s["form"], docs=evidence(s["documents"]), policy=policy(), verifications=registry(s["form"]))
    b = blockers(run_checks(inp, compare(inp)))
    assert task not in {c.task_key for c in b.values()}, [(c.key, c.reason) for c in b.values()]


# ---------------------------------------------------------------------------- unread GST certificates and misread scans

def test_unread_gst_certificate_is_not_a_vendor_bank_ownership_demand():
    s = SCEN["trade_name_supported"]
    docs = evidence(s["documents"])
    gst = next(d for d in docs if d.slot == "gst_certificate")
    gst.status, gst.reading, gst.error = "failed", None, {"kind": "unavailable", "message": "provider overloaded"}
    inp = EvalInput(form=s["form"], docs=docs, policy=policy(), verifications=registry(s["form"]))
    r = route(run_checks(inp, compare(inp)))
    assert not [t for t in r.tasks if t.startswith("vendor:")], list(r.tasks)
    bh = next(c for c in run_checks(inp, compare(inp)) if c.key == "credibility:bank_holder")
    assert bh.owner == "system" and bh.task_key == "system:reading"


def test_one_misread_scan_character_goes_to_a_reviewer_not_the_vendor():
    holder = "NIMBUS LEDGER S0LUTIONS PRIVATE LIMITED"  # 'O' read as zero; not in the page's text layer
    info, reading = _reading("nimbus_bank_letter.pdf", [
        ModelField(key="account_holder", status="found", value=holder, page=1),
        ModelField(key="account_number", status="found", value="50200071234567", page=1),
        ModelField(key="ifsc", status="found", value="DCBL0000417", page=1)])
    s = copy.deepcopy(SCEN["happy_nimbus"])
    good = next(d for d in evidence(s["documents"]) if d.slot == "bank_proof")
    s["form"]["bank"]["account_number"] = good.reading.value("account_number")
    s["form"]["bank"]["ifsc"] = good.reading.value("ifsc")
    reading.fields["account_number"] = good.reading.fields["account_number"]
    reading.fields["ifsc"] = good.reading.fields["ifsc"]
    docs = [d for d in evidence(s["documents"]) if d.slot != "bank_proof"]
    docs.append(DocEvidence("bank_proof", 99, "bank.pdf", info.sha256, info.layout, info.page_count, "complete", reading))
    inp = EvalInput(form=s["form"], docs=docs, policy=policy(), verifications=registry(s["form"]))
    b = blockers(run_checks(inp, compare(inp)))
    assert not [c for c in b.values() if c.owner == "vendor"], [(c.key, c.reason) for c in b.values()]
    assert b["match:account_holder"].owner == "reviewer"
    inp.resolutions = [ResolutionRec(c.key, c.fingerprint, "accepted", "Page shows SOLUTIONS", "Priya") for c in b.values()]
    assert route(run_checks(inp, compare(inp))).outcome == "ready"


# ---------------------------------------------------------------------------- manual registry results

def test_a_manual_unavailable_result_does_not_strand_the_registry_check_api(app_client):
    v = vendor(app_client, "Nimbus Registry Retry Private Limited")
    req = _submit(v, "happy_nimbus")["request_id"]
    rv = staff(app_client)
    c = case(rv, req)
    gstin = SCEN["happy_nimbus"]["form"]["tax"]["gstin"]
    r = rv.post(f"/api/review/requests/{req}/verification", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "identifier": gstin, "result": "unavailable"})
    assert r.status_code == 200, r.text
    run_worker_until_idle()
    c = case(rv, req)
    assert _check(c, "verification:gst_registry_check")["status"] == "satisfied"
    assert "system:registry" not in {t["key"] for t in c["tasks"] if t["status"] == "open"}
    with get_engine().connect() as db:
        methods = [m for (m,) in db.execute(select(Verification.method).where(Verification.request_id == req, Verification.identifier_type == "GSTIN")
                                            .order_by(Verification.id))]
    assert methods == ["simulated_registry", "manual_registry", "simulated_registry"]  # the lookup really ran again


def test_manual_only_policy_asks_a_reviewer_not_a_processing_retry():
    s = SCEN["scanned_llp_buyer_b"]
    g = s["form"]["tax"]["gstin"]
    inp = EvalInput(form=s["form"], docs=evidence(s["documents"]), policy=policy("buyer_b.json"),
                    verifications=[VerificationRec(1, "GSTIN", g, "manual_registry", "unavailable", None, "portal", "reviewer")])
    v = next(c for c in run_checks(inp, compare(inp)) if c.key == "verification:gst_registry_check")
    assert v.owner == "reviewer" and v.task_key == "reviewer:registry"


# ---------------------------------------------------------------------------- incomplete model replies

def test_a_reply_that_omits_a_key_field_is_asked_again_not_blamed_on_the_pdf():
    check = _validator(1)
    reply = {"document_type": "bank_proof", "fields": [
        {"key": "account_holder", "status": "found", "value": "NIMBUS LEDGER SOLUTIONS PRIVATE LIMITED"},
        {"key": "account_number", "status": "found", "value": "50200071234567"}]}  # no entry for ifsc at all
    with pytest.raises(LLMError) as e:
        check(reply)
    assert e.value.kind == "bad_response" and "ifsc" in e.value.message
    reply["fields"].append({"key": "ifsc", "status": "not_found"})  # an explicit answer is valid
    check(reply)
    check({"document_type": "other", "fields": []})


# ---------------------------------------------------------------------------- ambiguous results loaded from the cache

def test_found_with_a_different_alternative_is_ambiguous_also_when_loaded_from_cache():
    data = (FIXTURE_DIR / "documents" / "nimbus_bank_letter.pdf").read_bytes()
    from app.pdf.inspect import inspect_pdf, page_texts
    kinds = [p.kind for p in inspect_pdf(data, 10 * 2**20, 10).pages]
    r = validate_reading(ModelReading(document_type="bank_proof", fields=[
        ModelField(key="ifsc", status="found", value="DECB0000412", alternatives=["DECB0000413"], page=1),
        ModelField(key="account_number", status="found", value="5020 0071", alternatives=["50200071"], page=1)]),
        kinds, page_texts(data))
    assert r.fields["ifsc"].status == "ambiguous" and r.fields["ifsc"].alternatives == ["DECB0000412", "DECB0000413"]
    assert r.fields["account_number"].status == "found"  # the same value written differently is no conflict
    # a reading stored before this rule gets the same treatment when it is loaded
    stored = r.model_dump()
    stored["fields"]["ifsc"].update(status="found", value="DECB0000412", normalized="DECB0000412")
    assert DocumentReading.model_validate(stored).fields["ifsc"].status == "ambiguous"
