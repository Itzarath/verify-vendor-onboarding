"""Pure-logic scenario tests (no database): fixture documents -> gold readings -> compare -> checks -> route."""

import json
from pathlib import Path

import pytest

from app.extraction.service import FIXTURE_DIR, FixtureReader
from app.pdf.inspect import inspect_pdf
from app.pipeline.checks import run_checks
from app.pipeline.compare import compare
from app.pipeline.decide import compose_vendor_message, route
from app.pipeline.evidence import DocEvidence, EvalInput, ResolutionRec, VerificationRec
from app.pipeline.registry import lookup_gstin
from app.schemas.policy import BuyerPolicy

POL = Path(__file__).resolve().parents[1] / "policies"
SCEN = json.loads((FIXTURE_DIR / "scenarios.json").read_text(encoding="utf-8"))
READER = FixtureReader()


def policy(name="buyer_a.json"):
    return BuyerPolicy.model_validate(json.loads((POL / name).read_text(encoding="utf-8")))


def evidence(docs: dict) -> list[DocEvidence]:
    out = []
    for i, (slot, fname) in enumerate(docs.items()):
        data = (FIXTURE_DIR / "documents" / fname).read_bytes()
        info = inspect_pdf(data, 10 * 2**20, 10)
        r = READER.read(data, [p.kind for p in info.pages])
        out.append(DocEvidence(slot, i + 1, fname, info.sha256, info.layout, info.page_count, r.status, r.reading, r.error))
    return out


def registry(form):
    g = (form.get("tax") or {}).get("gstin")
    if not g:
        return []
    a = lookup_gstin(g)
    return [VerificationRec(1, "GSTIN", g, "simulated_registry", a.result, a.observed, "sim", "system")]


def evaluate(key, pol="buyer_a.json", **kw):
    s = SCEN[key]
    inp = EvalInput(form=s["form"], docs=evidence(s["documents"]), policy=policy(pol),
                    verifications=registry(s["form"]), **kw)
    cmp = compare(inp)
    checks = run_checks(inp, cmp)
    return inp, cmp, checks, route(checks)


def first_submission(s1):
    """What the runner passes for revision 2+: change reviews are judged against the first submission."""
    return {"bank": (s1["form"], "the first submission (revision 1)"),
            "identity": (s1["form"], "the first submission (revision 1)")}


def blockers(checks):
    return {c.key: c for c in checks if c.blocking and c.status != "satisfied"}


def test_happy_path_ready():
    _, _, checks, r = evaluate("happy_nimbus")
    assert r.outcome == "ready", [(c.key, c.reason) for c in r.blockers]
    keys = {c.key for c in checks if c.status == "satisfied"}
    assert {"cross:gstin_pan", "match:account_number", "verification:gst_registry_check", "credibility:bank_holder"} <= keys


def test_trade_name_supported_goes_to_reviewer_then_ready():
    inp, cmp, checks, r = evaluate("trade_name_supported")
    assert r.outcome == "action_required"
    b = blockers(checks)
    assert set(b) == {"credibility:bank_holder"} and b["credibility:bank_holder"].owner == "reviewer"
    fp = b["credibility:bank_holder"].fingerprint
    inp.resolutions = [ResolutionRec("credibility:bank_holder", fp, "accepted", "Trade name on GST certificate", "Priya (Reviewer)")]
    assert route(run_checks(inp, compare(inp))).outcome == "ready"


def test_trade_name_unsupported_asks_vendor():
    _, _, checks, r = evaluate("trade_name_unsupported")
    b = blockers(checks)
    assert b["credibility:bank_holder"].owner == "vendor"
    assert r.tasks["vendor:bank_holder"].owner == "vendor"


def test_director_pan_gives_one_precise_vendor_task():
    _, _, checks, r = evaluate("director_pan")
    vendor_tasks = [t for t in r.tasks.values() if t.owner == "vendor"]
    assert [t.task_key for t in vendor_tasks] == ["vendor:pan"], r.tasks.keys()
    text = " ".join(vendor_tasks[0].vendor_lines)
    assert "belongs to an individual" in text and "AAPCS2291H" in text
    assert "vendor:gst_identity" not in r.tasks  # GSTIN is consistent with the company; the PAN is the problem


def test_identity_conflict_flags_gst_identity_high_severity():
    _, _, checks, r = evaluate("identity_conflict")
    assert "vendor:gst_identity" in r.tasks
    hi = [c for c in checks if c.severity == "high" and c.blocking]
    assert {"cross:gstin_pan", "match:legal_name:gst_certificate", "verification:gst_registry_check"} <= {c.key for c in hi}


def test_bank_change_correction_requires_finance_review():
    s1, s2 = SCEN["bank_change_rev1"], SCEN["bank_change_rev2"]
    _, _, c1, r1 = evaluate("bank_change_rev1")
    assert "vendor:upload:incorporation_certificate" in r1.tasks
    prev_docs = {slot: d.sha256 for slot, d in ((d.slot, d) for d in evidence(s1["documents"]))}
    inp, cmp, c2, r2 = evaluate("bank_change_rev2", revision_number=2, previous_form=s1["form"], previous_docs=prev_docs,
                                baselines=first_submission(s1))
    b = blockers(c2)
    assert "document:registration_evidence" not in b  # the requested correction is satisfied
    assert set(b) == {"change:bank"} and b["change:bank"].owner == "finance"
    assert "••4821 → ••7390" in b["change:bank"].reason
    # finance approves the NEW account -> ready
    inp.resolutions = [ResolutionRec("change:bank", b["change:bank"].fingerprint, "accepted", "Verified new account", "Finance")]
    assert route(run_checks(inp, compare(inp))).outcome == "ready"


def test_stale_bank_review_does_not_carry_over():
    s1 = SCEN["bank_change_rev1"]
    prev_docs = {d.slot: d.sha256 for d in evidence(s1["documents"])}
    inp, _, c2, _ = evaluate("bank_change_rev2", revision_number=2, previous_form=s1["form"], previous_docs=prev_docs,
                             baselines=first_submission(s1))
    inp.resolutions = [ResolutionRec("change:bank", "0000000000000000", "accepted", "old account ok", "Finance")]
    checks = run_checks(inp, compare(inp))
    bank = next(c for c in checks if c.key == "change:bank")
    assert bank.status == "needs_review" and bank.evidence.get("superseded_reviews")


def test_msme_conditional_differs_by_buyer():
    _, _, _, ra = evaluate("scanned_llp_buyer_b", "buyer_a.json")
    assert "vendor:upload:msme_certificate" in ra.tasks
    _, _, cb, rb = evaluate("scanned_llp_buyer_b", "buyer_b.json")
    assert "vendor:upload:msme_certificate" not in rb.tasks
    # Buyer B does not accept simulated registry evidence -> reviewer must do a manual lookup
    reg = next(c for c in cb if c.key == "verification:gst_registry_check")
    assert reg.status == "unable" and reg.owner == "reviewer"
    assert reg.evidence["ignored_results"][0]["why"] == "method not accepted by this policy"


def test_scanned_pan_values_marked_visual():
    inp, cmp, checks, _ = evaluate("scanned_llp_buyer_b")
    pan_obs = [o for o in cmp.fields["pan"].observations if o.doc_type == "pan_card"][0]
    assert pan_obs.provenance == "visual_unconfirmed" and pan_obs.relation == "agrees"


def test_unreadable_account_asks_for_clearer_copy():
    _, _, checks, r = evaluate("unreadable_account")
    b = blockers(checks)
    assert b["document:bank_evidence"].status == "unable"
    assert "clearer copy" in b["document:bank_evidence"].vendor_text


def test_conduct_declined_auto_rejects_but_unanswered_does_not():
    _, _, _, r = evaluate("conduct_declined")
    assert r.outcome == "auto_reject" and r.auto_reject.rule == "conduct_declined"
    _, _, _, r2 = evaluate("conduct_unanswered")
    assert r2.outcome == "action_required" and "vendor:declarations" in r2.tasks


def test_unknown_condition_input_asks_instead_of_assuming():
    s = json.loads(json.dumps(SCEN["happy_nimbus"]))
    s["form"]["tax"]["msme_registered"] = None
    inp = EvalInput(form=s["form"], docs=evidence(s["documents"]), policy=policy(), verifications=registry(s["form"]))
    r = route(run_checks(inp, compare(inp)))
    assert r.outcome == "action_required" and "vendor:form_fields" in r.tasks


def test_wrong_document_in_slot_detected():
    s = json.loads(json.dumps(SCEN["happy_nimbus"]))
    s["documents"]["bank_proof"] = "nimbus_gst.pdf"
    inp = EvalInput(form=s["form"], docs=evidence(s["documents"]), policy=policy(), verifications=registry(s["form"]))
    checks = run_checks(inp, compare(inp))
    bank = next(c for c in checks if c.key == "document:bank_evidence")
    assert bank.status == "not_satisfied" and "appears to be" in bank.vendor_text


def test_provider_failure_is_system_task_not_vendor_problem():
    reader = FixtureReader()
    data = (FIXTURE_DIR / "documents" / "nimbus_bank_letter.pdf").read_bytes()
    import hashlib
    reader.fail_hashes.add(hashlib.sha256(data).hexdigest())
    s = SCEN["happy_nimbus"]
    docs = evidence(s["documents"])
    bank = next(d for d in docs if d.slot == "bank_proof")
    out = reader.read(data, ["text"])
    bank.status, bank.reading, bank.error = out.status, out.reading, out.error
    inp = EvalInput(form=s["form"], docs=docs, policy=policy(), verifications=registry(s["form"]))
    r = route(run_checks(inp, compare(inp)))
    assert r.outcome == "action_required" and set(r.tasks) == {"system:reading"}
    msg = compose_vendor_message(r, "Nimbus", 1, [])
    assert msg.kind == "under_review"  # vendor isn't asked to do anything


def test_registry_outage_blocks_without_blaming_vendor():
    from app.pipeline import registry as reg
    reg.state.outage = True
    try:
        _, _, checks, r = evaluate("happy_nimbus")
    finally:
        reg.state.outage = False
    v = next(c for c in checks if c.key == "verification:gst_registry_check")
    assert v.status == "unable" and v.owner == "system"
    assert r.outcome == "action_required"


@pytest.mark.parametrize("key", list(SCEN))
def test_every_scenario_produces_a_vendor_message(key):
    s = SCEN[key]
    _, _, _, r = evaluate(key)
    m = compose_vendor_message(r, s["form"]["business"]["legal_name"], 1, [])
    assert m.subject and m.body
