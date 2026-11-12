"""A clearance covers exactly the proof it saw; only text a person can see counts as proof; assessments made by older
checks are re-evaluated before approval; evidence views stay bound to their revision; ambiguous duplicates stay ambiguous.

API tests run on the real PostgreSQL test database with the test-only fixture reader. No live model calls.
"""

import hashlib
import io
import threading

import pymupdf
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.extraction.contract import ModelField, _in_text
from app.extraction.service import FIXTURE_DIR, get_reader
from app.models import Assessment, Request
from app.pdf import sandbox
from app.pdf.inspect import IntakeError, inspect_pdf, page_texts
from app.pipeline.checks import ENGINE_VERSION
from app.pipeline.runner import reassess_outdated

from .test_api_flow import BANK_CONFIRMATION, case, op, run_worker_until_idle, staff, submit_sample, vendor
from .test_safeguards import _reading, blockers, clear_reviewer_items
from .test_scenarios_logic import SCEN, evaluate, evidence, policy


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _variant(name: str, note: str) -> bytes:
    """A different file with the same facts: the fixture plus one visible line. Its gold label is the original's."""
    src = (FIXTURE_DIR / "documents" / name).read_bytes()
    doc = pymupdf.open(stream=src, filetype="pdf")
    doc[0].insert_text((60, 790), note, fontsize=8)
    data = doc.tobytes()
    reader = get_reader()
    reader._by_hash[_sha(data)] = reader._by_hash[_sha(src)]
    return data


def _upload(v, data: bytes, filename: str) -> int:
    r = v.post("/api/vendor/documents", files={"file": (filename, io.BytesIO(data), "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()["document_id"]


def _submit(v, key: str, based_on: int | None = None, replace: dict | None = None):
    s = v.post(f"/api/vendor/samples/{key}/load").json()
    docs = {k: d["document_id"] for k, d in s["documents"].items()}
    docs.update(replace or {})
    body = {"operation_id": op(), "form": s["form"], "documents": docs}
    if based_on is not None:
        body["based_on_revision"] = based_on
    r = v.post("/api/vendor/submit", json=body)
    assert r.status_code == 200, r.text
    run_worker_until_idle()
    return r.json()


def _finance_accepts(app_client, req: int):
    rv = staff(app_client)
    c = case(rv, req)
    bank = next(x for x in c["assessment"]["checks"] if x["key"] == "change:bank")
    fin = staff(app_client, role="finance")
    ok = fin.post(f"/api/review/requests/{req}/resolve", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "check_key": "change:bank",
        "fingerprint": bank["evidence"]["fingerprint"], "outcome": "accepted", "note": "Called the vendor; verified",
        "confirmation": BANK_CONFIRMATION})
    assert ok.status_code == 200, ok.text
    run_worker_until_idle()


def _approve(rv, req: int):
    c = case(rv, req)
    return rv.post(f"/api/review/requests/{req}/approve", json={
        "operation_id": op(), "expected_version": c["request"]["version"], "assessment_id": c["assessment"]["id"]})


# ---------------------------------------------------------------------------- a clearance covers its exact proof

def test_new_proof_after_finance_clearance_needs_finance_logic():
    s1, s2 = SCEN["bank_change_rev1"], SCEN["bank_change_rev2"]
    cleared_docs = {d.slot: d.sha256 for d in evidence(s2["documents"])}
    label = "revision 2, as cleared by finance"
    # same packet as the cleared revision: covered, nothing for finance to do
    _, _, checks, r = evaluate("bank_change_rev2", revision_number=3, previous_form=s2["form"],
                               previous_docs=cleared_docs, baselines={"bank": (s2["form"], label, cleared_docs),
                                                                      "identity": (s1["form"], "revision 1")})
    assert "change:bank" not in blockers(checks)
    # same account details, different proof file: the clearance does not cover it
    other = {**cleared_docs, "bank_proof": "f" * 64}
    _, _, checks, _ = evaluate("bank_change_rev2", revision_number=3, previous_form=s2["form"], previous_docs=other,
                               baselines={"bank": (s2["form"], label, other), "identity": (s1["form"], "revision 1")})
    b = blockers(checks)
    assert "change:bank" in b and b["change:bank"].owner == "finance"
    assert b["change:bank"].title == "Bank proof replaced after finance review"


def test_replaced_proof_after_clearance_cannot_be_approved_api(app_client):
    v = vendor(app_client, "Indigo Replaced Proof LLP")
    req = _submit(v, "bank_change_rev1")["request_id"]
    _submit(v, "bank_change_rev2", based_on=1)
    _finance_accepts(app_client, req)
    rv = staff(app_client)
    clear_reviewer_items(rv, req)
    assert case(rv, req)["request"]["state"] == "READY_FOR_DECISION"

    # revision 3: same account, IFSC and holder, but a different proof file
    new_proof = _upload(v, _variant("indigo_cheque_new.pdf", "Replacement copy issued on request"), "cheque_copy.pdf")
    _submit(v, "bank_change_rev2", based_on=2, replace={"bank_proof": new_proof})
    c = case(rv, req)
    assert c["request"]["state"] == "ACTION_REQUIRED"
    assert "finance:bank_change" in {t["key"] for t in c["tasks"] if t["status"] == "open"}
    assert _approve(rv, req).status_code == 409

    # revision 4 resubmits revision 3 unchanged: still not covered by the revision-2 clearance
    _submit(v, "bank_change_rev2", based_on=3, replace={"bank_proof": new_proof})
    c = case(rv, req)
    assert "finance:bank_change" in {t["key"] for t in c["tasks"] if t["status"] == "open"}
    assert _approve(rv, req).status_code == 409

    # finance reviews the replacement -> now eligible
    _finance_accepts(app_client, req)
    clear_reviewer_items(rv, req)
    c = case(rv, req)
    assert c["request"]["state"] == "READY_FOR_DECISION", [(x["key"], x["status"], x["owner"], x["reason"][:200])
                                                          for x in c["assessment"]["checks"]
                                                          if x["blocking"] and x["status"] != "satisfied"]
    assert _approve(rv, req).status_code == 200


def test_unchanged_packet_after_clearance_stays_eligible_api(app_client):
    v = vendor(app_client, "Indigo Same Packet LLP")
    req = _submit(v, "bank_change_rev1")["request_id"]
    _submit(v, "bank_change_rev2", based_on=1)
    _finance_accepts(app_client, req)
    _submit(v, "bank_change_rev2", based_on=2)  # identical to the cleared revision
    rv = staff(app_client)
    c = clear_reviewer_items(rv, req)
    assert c["request"]["state"] == "READY_FOR_DECISION"
    assert "finance:bank_change" not in {t["key"] for t in c["tasks"] if t["status"] == "open"}


# ---------------------------------------------------------------------------- only text a person can see is proof

def _page(draw) -> bytes:
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((36, 60), "SYNTHETIC TEST DOCUMENT - FICTIONAL QA ONLY")
    p.insert_text((36, 120), "Account holder: NIMBUS LEDGER SOLUTIONS PRIVATE LIMITED")
    draw(doc, p)
    p.insert_text((36, 220), "IFSC: DCBL0000417")
    return doc.tobytes()


def _covered(doc, p):
    p.insert_text((36, 170), "Account number: 000412300045821")
    p.draw_rect(pymupdf.Rect(30, 150, 550, 185), color=None, fill=(1, 1, 1))  # opaque white box painted over it


def _transparent_band(doc, p):
    p.draw_rect(pymupdf.Rect(30, 150, 550, 185), color=None, fill=(0.1, 0.2, 0.5), fill_opacity=0)
    p.insert_text((36, 170), "Account number: 000412300045821", color=(1, 1, 1))


def _off_page(doc, p):
    p.insert_text((520, 170), "A/c: 000412300045821")  # starts on the page, the number runs off its edge


def _under_image(doc, p):
    p.insert_text((36, 170), "Account number: 000412300045821")
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 10), False)
    pix.set_rect(pix.irect, (250, 250, 250))
    p.insert_image(pymupdf.Rect(30, 150, 550, 185), pixmap=pix, keep_proportion=False)  # an image painted over the text


def _overprinted(doc, p):
    p.insert_text((36, 170), "Account number: 000412300045821")
    p.insert_text((36, 170), "Account number: 999988887777666", color=(1, 1, 1))  # white digits drawn over black ones


def _vector_bar(doc, p):  # cover the number, then draw unrelated ink across the cover
    p.insert_text((36, 170), "Account number: 000412300045821")
    p.draw_rect(pymupdf.Rect(30, 150, 550, 185), fill=(1, 1, 1), color=None)
    p.draw_line((30, 165), (550, 165), color=(0, 0, 0), width=1)


def _faint_text_over_cover(doc, p):  # unrelated faint text drawn across the cover must not "explain" the digits
    p.insert_text((36, 170), "Account number: 000412300045821")
    p.draw_rect(pymupdf.Rect(30, 150, 550, 185), fill=(1, 1, 1), color=None)
    p.insert_text((36, 170), "WWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWW", color=(0.8, 0.1, 0.1), fill_opacity=0.15)


def _other_text_over_cover(doc, p):  # opaque unrelated text on top of the covered digits
    p.insert_text((36, 170), "Account number: 000412300045821")
    p.draw_rect(pymupdf.Rect(30, 150, 550, 185), fill=(1, 1, 1), color=None)
    p.insert_text((36, 172), "SPECIMEN COPY SPECIMEN COPY SPECIMEN COPY", fontsize=14)


@pytest.mark.parametrize("draw", [_covered, _transparent_band, _off_page, _under_image, _overprinted, _vector_bar,
                                  _faint_text_over_cover, _other_text_over_cover])
def test_invisible_account_number_is_not_text_layer_proof(draw):
    data = _page(draw)
    text = page_texts(data)[0]
    assert "NIMBUS LEDGER SOLUTIONS" in text and "DCBL0000417" in text  # the visible lines are kept
    hidden = "999988887777666" if draw is _overprinted else "000412300045821"
    assert not _in_text(hidden, text)
    info = inspect_pdf(data, 10 * 2**20, 10)
    assert info.pages[0].hidden_chars >= 5


@pytest.mark.parametrize("draw", [_covered, _transparent_band, _vector_bar])
def test_invisible_value_needs_confirmation_not_verification(draw):
    from app.pipeline.checks import run_checks
    from app.pipeline.compare import compare
    from app.pipeline.decide import route
    from app.pipeline.evidence import DocEvidence, EvalInput, VerificationRec
    import copy

    data = _page(draw)
    info = inspect_pdf(data, 10 * 2**20, 10)
    from app.extraction.contract import ModelReading, validate_reading
    reading = validate_reading(ModelReading(document_type="bank_proof", fields=[
        ModelField(key="account_holder", status="found", value="NIMBUS LEDGER SOLUTIONS PRIVATE LIMITED", page=1),
        ModelField(key="account_number", status="found", value="000412300045821", page=1),
        ModelField(key="ifsc", status="found", value="DCBL0000417", page=1)]),
        [p.kind for p in info.pages], page_texts(data))
    assert reading.fields["account_number"].provenance == "quote_not_in_text"
    s = copy.deepcopy(SCEN["happy_nimbus"])
    s["form"]["bank"]["account_number"] = "000412300045821"
    docs = [d for d in evidence(s["documents"]) if d.slot != "bank_proof"]
    docs.append(DocEvidence("bank_proof", 99, "bank.pdf", info.sha256, info.layout, info.page_count, "complete", reading))
    inp = EvalInput(form=s["form"], docs=docs, policy=policy(), verifications=[
        VerificationRec(1, "GSTIN", s["form"]["tax"]["gstin"], "simulated_registry", "found",
                        {"legal_name": s["form"]["business"]["legal_name"], "status": "Active"}, "sim", "system")])
    checks = run_checks(inp, compare(inp))
    assert "confirm:bank_proof" in blockers(checks) and route(checks).outcome != "ready"


def test_every_key_value_in_the_demo_documents_stays_visible():
    import json

    from app.extraction.templates import TEMPLATES

    gold = json.loads((FIXTURE_DIR / "gold_labels.json").read_text(encoding="utf-8"))
    checked = 0
    for name, entry in gold.items():
        path = FIXTURE_DIR / "documents" / name
        if not path.exists():
            continue
        data = path.read_bytes()
        info = inspect_pdf(data, 10 * 2**20, 10)
        texts = page_texts(data)
        page = entry["page"]
        if info.pages[page - 1].kind != "text":
            continue  # scans are read from the image and confirmed by a person anyway
        # the faint diagonal watermark is visible text (on PAN cards and cheques the card itself covers part of it)
        assert "SYNTHETIC TEST" in "\n".join(texts), name
        for key in TEMPLATES[entry["type"]]["critical"]:
            value = entry["fields"].get(key)
            if value and key not in entry.get("unreadable", []):
                assert _in_text(value, texts[page - 1]), (name, key, value)
                checked += 1
    assert checked >= 40


def _plain(draw) -> str:
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    draw(doc, p)
    return page_texts(pymupdf.open(stream=doc.tobytes(), filetype="pdf").tobytes())[0]


def test_ordinary_layouts_are_not_mistaken_for_hidden_text():
    def underlined(doc, p):
        p.insert_text((72, 100), "Account number: 50100012345678")
        p.draw_line((72, 102), (260, 102), color=(0, 0, 0), width=0.6)  # drawn after the text

    def on_a_photo(doc, p):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
        for x in range(8):
            for y in range(8):
                pix.set_pixel(x, y, (40 + 20 * x, 90 + 10 * y, 160))
        p.insert_image(pymupdf.Rect(60, 80, 400, 120), pixmap=pix, keep_proportion=False)
        p.insert_text((72, 104), "Account number: 50100012345678", color=(1, 1, 1))

    def simulated_bold(doc, p):
        p.insert_text((72, 100), "Account number: 50100012345678")
        p.insert_text((72.3, 100), "Account number: 50100012345678")

    def inside_a_form_object(doc, p):
        src = pymupdf.open()
        src.new_page(width=595, height=842).insert_text((72, 100), "Account number: 50100012345678")
        p.show_pdf_page(p.rect, src, 0)

    def small_grey_footer(doc, p):
        p.insert_text((72, 800), "Synthetic specimen generated for testing 50100012345678", fontsize=7,
                      color=(0.45, 0.45, 0.45))

    for draw in (underlined, on_a_photo, simulated_bold, inside_a_form_object, small_grey_footer):
        assert _in_text("50100012345678", _plain(draw)), draw.__name__


# ---------------------------------------------------------------------------- older assessments are not approvable

def _ready_case(app_client, name: str):
    # Its own manual-approval buyer: on the seeded Meridian (automatic approval) the result would depend on whether
    # earlier tests had already used the sample's bank account.
    from .test_auto_approval import as_user, make_buyer

    b = make_buyer(auto=False)
    v = vendor(app_client, name, buyer=b.slug)
    req = _submit(v, "happy_nimbus")["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    c = clear_reviewer_items(rv, req)
    assert c["request"]["state"] == "READY_FOR_DECISION"
    return v, rv, req


def _make_assessment_legacy(req: int, drop_links=False):
    with session_scope() as db:
        r = db.get(Request, req)
        a = db.get(Assessment, r.current_assessment_id)
        summary = {k: v for k, v in a.summary.items() if k != "engine_version"}
        if drop_links:  # schema-0001 summaries carried no document or extraction ids
            summary["documents"] = {s: {k: v for k, v in d.items() if k not in ("document_id", "extraction_id")}
                                    for s, d in summary["documents"].items()}
        a.summary = summary


def test_assessment_from_older_checks_cannot_be_approved(app_client):
    _, rv, req = _ready_case(app_client, "Older Checks Pvt Ltd")
    _make_assessment_legacy(req)
    assert case(rv, req)["assessment"]["engine_current"] is False
    a = _approve(rv, req)
    assert a.status_code == 409 and a.json()["error"] == "outdated_assessment"
    # the reviewer re-evaluates under the current checks; history is kept
    c = case(rv, req)
    r = rv.post(f"/api/review/requests/{req}/retry", json={"operation_id": op(), "expected_version": c["request"]["version"]})
    assert r.status_code == 200, r.text
    run_worker_until_idle()
    c = clear_reviewer_items(rv, req)
    assert c["assessment"]["engine_current"] is True and c["request"]["state"] == "READY_FOR_DECISION"
    assert _approve(rv, req).status_code == 200


def test_startup_requeues_open_cases_assessed_by_older_checks(app_client):
    _, rv, req = _ready_case(app_client, "Upgrade Sweep Pvt Ltd")
    _make_assessment_legacy(req)
    assert reassess_outdated() >= 1
    assert case(rv, req)["request"]["state"] == "PROCESSING"
    assert reassess_outdated() == 0  # nothing left to queue
    run_worker_until_idle()
    c = clear_reviewer_items(rv, req)
    assert c["request"]["state"] == "READY_FOR_DECISION"
    with session_scope() as db:
        a = db.get(Assessment, db.get(Request, req).current_assessment_id)
        assert a.summary["engine_version"] == ENGINE_VERSION
    assert any(e["type"] == "REEVALUATION_QUEUED" for e in c["audit"])


# ---------------------------------------------------------------------------- evidence view bindings

def test_legacy_assessment_still_shows_its_readings(app_client):
    _, rv, req = _ready_case(app_client, "Legacy Readings Pvt Ltd")
    _make_assessment_legacy(req, drop_links=True)
    docs = case(rv, req)["revisions"][-1]["documents"]
    assert docs and all(d.get("reading") for d in docs), [d["slot"] for d in docs if not d.get("reading")]
    assert all(d["reading"]["legacy_link"] for d in docs)


def test_one_file_in_two_slots_keeps_each_slots_reading(app_client):
    v = vendor(app_client, "Two Slots One File Pvt Ltd")
    pan = _upload(v, _variant("nimbus_pan.pdf", "Copy for slot test"), "pan_copy.pdf")
    req = _submit(v, "happy_nimbus", replace={"pan_card": pan, "gst_certificate": pan})["request_id"]
    c = case(staff(app_client), req)
    summary = c["assessment"]["summary"]["documents"]
    assert summary["pan_card"]["extraction_id"] != summary["gst_certificate"]["extraction_id"]  # read once per slot
    # observations name their slot, so the evidence tab can open the right one of the two
    obs = [o for f in c["assessment"]["comparison"]["fields"].values() for o in f["observations"] if o["document_id"] == pan]
    assert {"pan_card", "gst_certificate"} <= {o["slot"] for o in obs}
    for d in c["revisions"][-1]["documents"]:
        if d["slot"] in ("pan_card", "gst_certificate"):
            assert d["reading"]["extraction_id"] == summary[d["slot"]]["extraction_id"], d["slot"]


# ---------------------------------------------------------------------------- contract and sandbox notes

def test_hyphen_joined_identifier_fragment_is_not_verified():
    assert not _in_text("0012345678", "A/c 9900-0012345678-1122")
    assert _in_text("9900-0012345678-1122", "A/c 9900-0012345678-1122")
    assert _in_text("AAK-4417", "LLPIN AAK-4417. The")


def test_found_then_ambiguous_duplicate_stays_ambiguous():
    _, reading = _reading("nimbus_pan.pdf", [
        ModelField(key="pan", status="found", value="ABCDE1234F", page=1),
        ModelField(key="pan", status="ambiguous", value="ABCDE1234F", alternatives=["ABCDE1234F", "ZZZZZ9999Z"], page=1)],
        dtype="pan_card")
    f = reading.fields["pan"]
    assert f.status == "ambiguous" and f.normalized is None and set(f.alternatives) == {"ABCDE1234F", "ZZZZZ9999Z"}


def test_three_different_duplicate_values_keep_every_candidate():
    _, reading = _reading("nimbus_bank_letter.pdf", [
        ModelField(key="ifsc", status="found", value="DCBL0000417", page=1),
        ModelField(key="ifsc", status="found", value="DCBL0000471", page=1),
        ModelField(key="ifsc", status="found", value="DCBL0000714", page=1)])
    f = reading.fields["ifsc"]
    assert f.status == "ambiguous" and set(f.alternatives) == {"DCBL0000417", "DCBL0000471", "DCBL0000714"}


def test_waiting_for_a_pdf_slot_is_bounded(monkeypatch):
    import time

    s = get_settings()
    monkeypatch.setattr(s, "pdf_isolation", True)
    monkeypatch.setattr(s, "pdf_timeout_s", 0.3)
    held = [sandbox._SLOTS.acquire(), sandbox._SLOTS.acquire()]  # both slots busy
    try:
        t = time.monotonic()
        with pytest.raises(IntakeError) as e:
            sandbox.run("texts", b"%PDF-1.4")
        assert e.value.code == "busy" and time.monotonic() - t < 2
    finally:
        for _ in held:
            sandbox._SLOTS.release()
    assert threading.active_count() >= 1
