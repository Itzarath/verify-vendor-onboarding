"""Hidden characters inside values, finance clearances that cover identity, every PAN card assessed, the simulated
registry always named as such, the policy a correction is pinned to, failed evaluations and access-code trimming.

API tests run on the real PostgreSQL test database with the test-only fixture reader. No live model calls.
"""

import copy
import hashlib
import uuid

import pymupdf
import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import session_scope
from app.extraction.contract import ModelField, ModelReading, _in_text, validate_reading
from app.extraction.service import FIXTURE_DIR, get_reader
from app.models import Assessment, Extraction, Message, Policy, Request
from app.pdf.inspect import HIDDEN_MARK, inspect_pdf, page_texts
from app.pipeline.checks import run_checks
from app.pipeline.compare import compare
from app.pipeline.decide import route
from app.pipeline.evidence import DocEvidence, EvalInput, ResolutionRec

from .test_api_flow import case, op, run_worker_until_idle, vendor
from .test_auto_approval import _add_policy, as_user, decisions, make_buyer
from .test_publish_guards import _load
from .test_clearances_and_visibility import _approve, _submit, _upload
from .test_scenarios_logic import SCEN, blockers, evaluate, evidence, policy, registry

ACCOUNT = "000412300045821"


def _drain():
    run_worker_until_idle(50)


# ---------------------------------------------------------------------------- hidden characters are not spaces

def _page_with_account(hide: int | None, how: str = "cover", grouped: bool = False) -> bytes:
    """A bank letter page printing ACCOUNT, with the character at `hide` not visible: covered by a white box, or
    printed in white as its own text run."""
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=300)
    page.insert_text((30, 40), "SYNTHETIC TEST DOCUMENT - bank letter", fontsize=12)
    page.insert_text((30, 75), "Account holder: NIMBUS LEDGER SOLUTIONS PRIVATE LIMITED", fontsize=12)
    page.insert_text((30, 145), "IFSC: DECB0000412", fontsize=14)
    x, y, size = 30, 110, 18
    label = "Account number: "
    if grouped:
        page.insert_text((x, y), label + " ".join(ACCOUNT[i:i + 4] for i in range(0, len(ACCOUNT), 4)), fontsize=size)
    elif hide is None or how == "cover":
        page.insert_text((x, y), label + ACCOUNT, fontsize=size)
        if hide is not None:
            span = next(s for s in page.get_texttrace() if ACCOUNT in "".join(chr(c[0]) for c in s["chars"]))
            start = "".join(chr(c[0]) for c in span["chars"]).index(ACCOUNT)
            page.draw_rect(pymupdf.Rect(span["chars"][start + hide][3]), color=(1, 1, 1), fill=(1, 1, 1), overlay=True)
    else:  # three text runs: before, the hidden character in white, after
        before, ch, after = label + ACCOUNT[:hide], ACCOUNT[hide], ACCOUNT[hide + 1:]
        page.insert_text((x, y), before, fontsize=size)
        x += pymupdf.get_text_length(before, fontsize=size)
        page.insert_text((x, y), ch, fontsize=size, color=(1, 1, 1))
        x += pymupdf.get_text_length(ch, fontsize=size)
        page.insert_text((x, y), after, fontsize=size)
    data = doc.tobytes()
    doc.close()
    return data


def _verified(data: bytes, value: str) -> bool:
    info = inspect_pdf(data, 10 * 2**20, 10)
    reading = validate_reading(ModelReading(document_type="bank_proof", fields=[
        ModelField(key="account_number", status="found", value=value, page=1)]), [p.kind for p in info.pages],
        page_texts(data))
    return reading.fields["account_number"].provenance == "text_layer_verified"


@pytest.mark.parametrize("how", ["cover", "white_run"])
@pytest.mark.parametrize("hide", [0, 6, len(ACCOUNT) - 1])
def test_a_value_is_never_verified_across_or_next_to_a_hidden_character(how, hide):
    data = _page_with_account(hide, how)
    assert inspect_pdf(data, 10 * 2**20, 10).pages[0].hidden_chars >= 1
    shortened = ACCOUNT[:hide] + ACCOUNT[hide + 1:]
    assert not _verified(data, shortened)  # the reader leaves the hidden digit out
    assert not _verified(data, ACCOUNT)  # the full number isn't visible either
    for part in (ACCOUNT[:hide], ACCOUNT[hide + 1:]):  # nor is a visible part of it
        if len(part) >= 4:
            assert not _verified(data, part), part
    assert HIDDEN_MARK in page_texts(data)[0]  # the visible text marks where characters are hidden


def test_controls_fully_visible_and_grouped_numbers_stay_verified():
    assert _verified(_page_with_account(None), ACCOUNT)
    assert _verified(_page_with_account(None, grouped=True), ACCOUNT)  # printed in groups of four
    assert not _verified(_page_with_account(None), ACCOUNT[:6] + ACCOUNT[7:])  # a digit left out of a visible number
    assert not _in_text(ACCOUNT, f"{ACCOUNT[:6]}{HIDDEN_MARK}{ACCOUNT[6:]}")


def _bank_letter_with_hidden_digit(tag: str) -> tuple[bytes, str]:
    """The Nimbus bank letter with one digit of its account number covered (new bytes); and the shortened number."""
    src = (FIXTURE_DIR / "documents" / "nimbus_bank_letter.pdf").read_bytes()
    doc = pymupdf.open(stream=src, filetype="pdf")
    page = doc[0]
    span = next(s for s in page.get_texttrace() if "".join(chr(c[0]) for c in s["chars"]) == ACCOUNT)
    page.draw_rect(pymupdf.Rect(span["chars"][6][3]), color=(1, 1, 1), fill=(1, 1, 1), overlay=True)
    doc.set_metadata({"subject": tag})
    data = doc.tobytes()
    doc.close()
    reader = get_reader()
    entry = copy.deepcopy(reader._by_hash[hashlib.sha256(src).hexdigest()])
    shortened = ACCOUNT[:6] + ACCOUNT[7:]
    entry["fields"]["account_number"] = shortened  # a reader that leaves the covered digit out (fault injected)
    reader._by_hash[hashlib.sha256(data).hexdigest()] = entry
    return data, shortened


@pytest.mark.parametrize("stored", [False, True])
def test_a_shortened_account_from_a_covered_digit_is_never_approved_automatically(app_client, stored):
    """Fresh reading, and a reading stored under the older rules (re-checked at evaluation, no model call)."""
    _drain()
    b = make_buyer(auto=True)
    v = vendor(app_client, "Covered Digit Private Limited", buyer=b.slug)
    data, shortened = _bank_letter_with_hidden_digit(uuid.uuid4().hex)
    doc_id = _upload(v, data, "bank.pdf")
    reader = get_reader()
    if stored:
        info = inspect_pdf(data, 10 * 2**20, 10)
        old = reader.read(data, [p.kind for p in info.pages]).reading
        old.fields["account_number"] = old.fields["account_number"].model_copy(update={"provenance": "text_layer_verified"})
        with session_scope() as db:
            db.add(Extraction(document_id=doc_id, config_key=reader.config_key, status="complete",
                              detected_type="bank_proof", result=old.model_dump(), provider="test-fixture",
                              model="older-rules", latency_ms=0, usage={}))
    form, docs = _load(v, "happy_nimbus")
    form["bank"]["account_number"] = shortened
    docs["bank_proof"] = doc_id
    r = v.post("/api/vendor/submit", json={"operation_id": op(), "form": form, "documents": docs})
    assert r.status_code == 200, r.text
    req = r.json()["request_id"]
    _drain()
    assert decisions(req) == []
    with session_scope() as db:
        a = db.get(Assessment, db.get(Request, req).current_assessment_id)
        ex = db.get(Extraction, a.summary["documents"]["bank_proof"]["extraction_id"])
        assert ex.result["fields"]["account_number"]["provenance"] != "text_layer_verified"
        if stored:  # the stored reading itself is unchanged; the re-checked copy is linked to it
            assert ex.reused_from_id is not None and ex.model == "older-rules"
    assert case(as_user(app_client, b.reviewer_id), req)["request"]["state"] != "APPROVED"


# ---------------------------------------------------------------------------- finance's clearance covers identity

def _finance_cleared(form_change):
    s1, s2 = SCEN["bank_change_rev1"], SCEN["bank_change_rev2"]
    cleared = {d.slot: d.sha256 for d in evidence(s2["documents"])}
    form = copy.deepcopy(s2["form"])
    form_change(form)
    inp = EvalInput(form=form, docs=evidence(s2["documents"]), policy=policy(), verifications=registry(form),
                    revision_number=3, previous_form=s2["form"], previous_docs=cleared,
                    baselines={"bank": (s2["form"], "revision 2, as cleared by finance", cleared),
                               "identity": (s1["form"], "revision 1")})
    return inp


@pytest.mark.parametrize("field,value", [("tax.pan", "AAACZ9999Q"), ("business.legal_name", "Different Entity Private Limited")])
def test_an_identity_correction_after_finance_clearance_needs_finance_again(field, value):
    def change(form):
        sec, key = field.split(".")
        form[sec][key] = value
    inp = _finance_cleared(change)
    b = blockers(run_checks(inp, compare(inp)))
    assert "change:bank" in b and b["change:bank"].owner == "finance", sorted(b)
    assert b["change:bank"].title == "Legal identity changed after finance review"
    # A reviewer accepting the identity change does not clear it; only finance's review of THIS identity does.
    fp = b["change:bank"].fingerprint
    inp.resolutions = [ResolutionRec("change:identity", (b.get("change:identity") or b["change:bank"]).fingerprint,
                                     "accepted", "identity typo fixed", "Priya")]
    assert "change:bank" in blockers(run_checks(inp, compare(inp)))
    inp.resolutions.append(ResolutionRec("change:bank", fp, "accepted", "account confirmed for the new identity", "Farah"))
    assert "change:bank" not in blockers(run_checks(inp, compare(inp)))


def test_control_unchanged_identity_stays_covered_by_the_clearance():
    inp = _finance_cleared(lambda form: None)
    assert "change:bank" not in blockers(run_checks(inp, compare(inp)))


# ---------------------------------------------------------------------------- every PAN card is assessed

def _with_pan_cards(cards: list[tuple[str, str]]):
    """happy_nimbus with its PAN card replaced by these observations: (holder name, provenance)."""
    s = SCEN["happy_nimbus"]
    base = [d for d in evidence(s["documents"]) if d.slot != "pan_card"]
    pan = next(d for d in evidence(s["documents"]) if d.slot == "pan_card")
    out = []
    for i, (name, prov) in enumerate(cards):
        f = pan.reading.fields["holder_name"].model_copy(update={"value": name, "normalized": name, "provenance": prov})
        reading = pan.reading.model_copy(update={"fields": {**pan.reading.fields, "holder_name": f}})
        out.append(DocEvidence("pan_card" if i == 0 else f"supporting_{i}", 100 + i, f"pan{i}.pdf", f"{i:064d}",
                               pan.layout, pan.page_count, "complete", reading))
    inp = EvalInput(form=s["form"], docs=base + out, policy=policy(), verifications=registry(s["form"]))
    return inp


def _name_check(inp):
    return next(c for c in run_checks(inp, compare(inp)) if c.key == "match:legal_name:pan_card")


def test_accepting_a_scanned_variant_does_not_clear_another_holders_pan_card():
    variant = "NIMBUS LEDGER SOLUTION PRIVATE LIMITED"
    inp = _with_pan_cards([(variant, "visual_unconfirmed"), ("COBALT WAVE PRIVATE LIMITED", "text_layer_verified")])
    c = _name_check(inp)
    assert c.status == "not_satisfied" and c.owner == "vendor" and "COBALT WAVE" in c.reason
    # The narrow acceptance the old code applied (form, the variant, its provenance) changes nothing.
    from app.pipeline.evidence import fingerprint
    inp.resolutions = [ResolutionRec("match:legal_name:pan_card", fingerprint(SCEN["happy_nimbus"]["form"]["business"]["legal_name"],
                                                                         variant, "visual_unconfirmed"),
                                     "accepted", "scan shows the same company", "Priya")]
    c = _name_check(inp)
    assert c.status == "not_satisfied" and c.owner == "vendor"
    assert route(run_checks(inp, compare(inp))).outcome != "ready"


def test_one_judgement_covers_exactly_the_variants_it_was_shown():
    a, b2 = "NIMBUS LEDGER SOLUTION PRIVATE LIMITED", "NIMBUS LEDGERS SOLUTIONS PRIVATE LIMITED"
    one = _with_pan_cards([(a, "visual_unconfirmed")])
    c = _name_check(one)
    assert c.status == "needs_review"
    one.resolutions = [ResolutionRec(c.key, c.fingerprint, "accepted", "same company", "Priya")]
    assert _name_check(one).status == "satisfied"  # control: one variant, accepted
    two = _with_pan_cards([(a, "visual_unconfirmed"), (b2, "visual_unconfirmed")])
    c2 = _name_check(two)
    assert c2.status == "needs_review" and a in c2.reason and b2 in c2.reason
    two.resolutions = one.resolutions  # the judgement on the first variant alone
    assert _name_check(two).status == "needs_review"


# ---------------------------------------------------------------------------- simulated registry is always named

@pytest.mark.parametrize("note", [None, "Checked the case file"])
def test_a_reviewer_approval_says_the_registry_check_was_simulated(app_client, note):
    _drain()
    b = make_buyer(auto=False)
    req = _submit(vendor(app_client, "Manual Simulated Private Limited", buyer=b.slug), "happy_nimbus")["request_id"]
    rv = as_user(app_client, b.reviewer_id)
    c = case(rv, req)
    assert c["request"]["state"] == "READY_FOR_DECISION"
    body = {"operation_id": op(), "expected_version": c["request"]["version"], "assessment_id": c["assessment"]["id"]}
    if note:
        body["note"] = note
    assert rv.post(f"/api/review/requests/{req}/approve", json=body).status_code == 200
    c = case(rv, req)
    assert "SIMULATED" in c["decisions"][-1]["reason"] and (note or "") in c["decisions"][-1]["reason"]
    with session_scope() as db:
        msg = db.scalar(select(Message.body).where(Message.request_id == req, Message.kind == "approved"))
    assert "simulated" in msg and "not bank verification" in msg


# ---------------------------------------------------------------------------- a correction shows its pinned policy

def test_the_correction_form_shows_the_requests_pinned_policy(app_client):
    _drain()
    b = make_buyer(auto=False, version=1)
    v = vendor(app_client, "Pinned Form Private Limited", buyer=b.slug)
    req = _submit(v, "happy_nimbus")["request_id"]
    _add_policy(b.buyer_id, b.slug, 2, False)  # the buyer publishes a new version while the request is open
    with session_scope() as db:
        pinned = db.get(Policy, db.get(Request, req).policy_id)
    assert pinned.version == 1
    assert v.get("/api/config/form").json()["policy"]["version"] == 1
    newcomer = vendor(app_client, "New Applicant Private Limited", buyer=b.slug)
    assert newcomer.get("/api/config/form").json()["policy"]["version"] == 2  # new applications use the active one


# ---------------------------------------------------------------------------- a failed evaluation tells the vendor

def test_a_failed_evaluation_replaces_the_checks_are_running_message(app_client, monkeypatch):
    _drain()
    b = make_buyer(auto=False)
    v = vendor(app_client, "Failed Evaluation Private Limited", buyer=b.slug)
    form, docs = _load(v, "happy_nimbus")
    reader = get_reader()

    def broken(self, pdf, kinds):
        raise RuntimeError("reader crashed (test)")

    from .test_publish_guards import _variant
    # A file not read before, so the (broken) reader is really called.
    docs["gst_certificate"] = _upload(v, _variant("nimbus_gst.pdf", uuid.uuid4().hex), "gst.pdf")
    monkeypatch.setattr(type(reader), "read", broken)
    r = v.post("/api/vendor/submit", json={"operation_id": op(), "form": form, "documents": docs})
    assert r.status_code == 200, r.text
    req = r.json()["request_id"]
    _drain()
    with session_scope() as db:
        assert db.get(Request, req).state == "ACTION_REQUIRED"
        msgs = db.scalars(select(Message).where(Message.request_id == req).order_by(Message.id)).all()
    assert msgs[-1].subject == "Checks paused — our team will retry" and "no action is needed from you" in msgs[-1].body
    assert sum(m.subject == msgs[-1].subject for m in msgs) == 1


# ---------------------------------------------------------------------------- the access code is trimmed once

@pytest.mark.parametrize("raw,expect", [("  operations-demo-code  ", "operations-demo-code"),
                                         ("operations-demo-code\n", "operations-demo-code"), ("   ", None)])
def test_a_padded_access_code_setting_is_trimmed(raw, expect, monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", raw)
    assert Settings(_env_file=None).demo_access_code == expect


def test_the_trimmed_code_can_be_entered(monkeypatch):
    from app.api import access

    s = Settings(_env_file=None, demo_access_code="  operations-demo-code  ")
    monkeypatch.setattr(access, "get_settings", lambda: s)
    assert access.gate_enabled()
    assert access.code_matches("operations-demo-code") and access.code_matches(" operations-demo-code ")
    assert not access.code_matches("wrong-code-1234")


# ---------------------------------------------------------------------------- the sample says what happens now

def test_the_happy_sample_describes_the_automatic_approval():
    inp, cmp, checks, r = evaluate("happy_nimbus")
    assert r.outcome == "ready" and policy().approval.auto_approve_when_clean
    expect = SCEN["happy_nimbus"]["expect"]
    assert "APPROVED automatically" in expect and "reviewer approves" in expect  # Meridian; and a manual buyer
    gen = (FIXTURE_DIR.parent / "generate.py").read_text(encoding="utf-8")
    assert "APPROVED automatically" in gen  # the generator and its output agree
