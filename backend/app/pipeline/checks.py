"""Stage 4 — apply the buyer's policy to the evidence.

Each check produces a finding with a status, a reason, its evidence and — if it
blocks progress — who must act and what they must do. A failed check never
rejects a vendor by itself; only an explicit policy rule can (auto_reject).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date

from ..india.names import compare_names, normalise_name
from ..pdf.inspect import layout_label
from ..verification.bank import branch_place, name_match, same_bank
from ..india.validators import (
    CIN_STATE_CODES,
    DOCUMENT_DATE_FORMATS,
    GST_STATE_CODES,
    PAN_HOLDER_TYPES,
    canonical_state,
    check_account_number,
    check_email,
    check_gstin,
    check_gstin_pan_link,
    check_ifsc,
    check_pan,
    check_pan_matches_entity,
    check_past_date,
    check_pincode,
    check_registration_number,
    check_udyam,
    clean_identifier,
    parse_date,
)
from ..schemas.policy import evaluate_condition
from ..schemas.submission import DOCUMENT_TYPES, FIELD_LABELS, get_field
from .compare import BANK_REVIEW_IDENTITY, ComparisonResult, field_changes
from .evidence import EvalInput, fingerprint

BLOCKING_STATUSES = {"not_satisfied", "needs_review", "unable"}

# Version of the rules that decide eligibility. Bump whenever a check, a grounding rule or a change-review rule changes:
# an open assessment made under other rules can't be approved; it is re-evaluated first (see runner.reassess_outdated).
ENGINE_VERSION = "engine-15"

# A bank letter or statement proves the account as of its date; an old one may describe a closed or changed account.
BANK_PROOF_MAX_AGE_DAYS = 183  # about 6 months


@dataclass
class CheckOutcome:
    key: str
    group: str  # completeness | format | consistency | credibility | verification | declaration | change | scope
    status: str  # satisfied | not_satisfied | needs_review | unable | not_applicable | info
    title: str
    reason: str
    blocking: bool = False
    action: str | None = None  # request_information | review | retry
    owner: str | None = None  # vendor | reviewer | finance | system
    task_key: str | None = None
    task_title: str | None = None
    vendor_text: str | None = None
    fingerprint: str | None = None
    evidence: dict = field(default_factory=dict)
    auto_reject: bool = False
    rule: str | None = None
    severity: str = "normal"  # normal | high

    def to_dict(self) -> dict:
        return asdict(self)


def mask(acct: str | None) -> str:
    v = clean_identifier(acct)
    return f"••{v[-4:]}" if len(v) >= 4 else (v or "—")


TASK_TITLES = {
    "vendor:form_fields": "Complete the application form",
    "vendor:form_values": "Correct the contact, address or date details",
    "vendor:declarations": "Answer the declaration questions",
    "vendor:pan": "Provide the business PAN",
    "vendor:gstin": "Correct the GSTIN",
    "vendor:gst_identity": "Explain or correct the GST registration",
    "vendor:registration": "Correct the company / LLP registration details",
    "vendor:bank_details": "Correct the bank details",
    "vendor:bank_holder": "Evidence the bank account holder name",
    "vendor:bank_proof_date": "Provide a recent bank proof",
    "vendor:msme": "Correct the MSME (Udyam) details",
    "reviewer:bank_holder": "Review: account held in a trade name",
    "reviewer:registry": "Record the GST registry lookup",
    "reviewer:scope": "Scope review",
    "reviewer:gst_state": "Review: GST state differs from registered office",
    "reviewer:cin_state": "Review: CIN state differs from registered office",
    "reviewer:name_variant": "Review: name variation",
    "reviewer:duplicate_bank": "Review: bank account already on file",
    "reviewer:bank_proof_date": "Review: check the date on the bank proof",
    "reviewer:conflict": "Review: declared conflict of interest",
    "reviewer:identity_change": "Review: identity details changed",
    "reviewer:visual_read": "Review: unverified document value differs from the form",
    "reviewer:confirm_reading": "Review: confirm values the model read without text-layer proof",
    "reviewer:authenticity": "Review: a certificate may have been edited",
    "reviewer:duplicate_identity": "Review: same PAN or GSTIN as another vendor",
    "finance:bank_change": "Finance review: bank details changed",
    "finance:bank_account": "Finance review: the bank's name for the account differs",
    "finance:gst_returns": "Finance review: the supplier's GST returns are not all filed",
    "system:reading": "Retry document reading",
    "system:registry": "Retry the registry lookup",
}


class _Builder:
    def __init__(self, inp: EvalInput):
        self.inp = inp
        self.out: list[CheckOutcome] = []

    def add(self, c: CheckOutcome) -> CheckOutcome:
        if c.status in BLOCKING_STATUSES and c.blocking and c.task_key and not c.task_title:
            c.task_title = TASK_TITLES.get(c.task_key, c.title)
        self.out.append(c)
        return c

    def ok(self, key, group, title, reason, **ev):
        return self.add(CheckOutcome(key, group, "satisfied", title, reason, evidence=ev))

    def info(self, key, group, title, reason, **ev):
        return self.add(CheckOutcome(key, group, "info", title, reason, evidence=ev))

    def na(self, key, group, title, reason):
        return self.add(CheckOutcome(key, group, "not_applicable", title, reason))

    def vendor(self, key, group, title, reason, task_key, vendor_text, status="not_satisfied", severity="normal", **ev):
        return self.add(CheckOutcome(key, group, status, title, reason, blocking=True, action="request_information",
                                     owner="vendor", task_key=task_key, vendor_text=vendor_text, evidence=ev,
                                     severity=severity))

    def review(self, key, group, title, reason, owner, task_key, fp, **ev):
        """A judgement call. Satisfied only by a resolution bound to this exact evidence fingerprint."""
        res = self.inp.resolution_for(key, fp)
        if res and res.outcome == "accepted":
            done = {"by": res.actor, "note": res.note}
            how = ""
            if res.confirmation:  # a bank change confirmed independently (method, contact source, person)
                c = res.confirmation
                done["confirmation"] = {**c, "by": res.actor, "at": res.created_at.isoformat() if res.created_at else None}
                how = (f"; confirmed by {c.get('method_label')} with {c.get('contact_person')}, using the "
                       f"{c.get('contact_source_label')}")
            return self.add(CheckOutcome(key, group, "satisfied", title, f"{reason} — accepted by {res.actor}: {res.note}{how}",
                                         fingerprint=fp, evidence={**ev, "resolution": done}))
        if res and res.outcome == "not_accepted":
            answered = (res.revision_id is not None and self.inp.revision_id is not None
                        and res.revision_id != self.inp.revision_id)
            if not answered:
                # The note is the reviewer's internal reasoning; the vendor sees only what the reviewer wrote for them.
                ask = res.vendor_text or (f"A reviewer could not accept this item ({title}). Please check it and resubmit "
                                          "with a corrected or supporting document, or explain in the note when you resubmit.")
                return self.add(CheckOutcome(key, group, "not_satisfied", title, f"{reason} — not accepted by {res.actor}: {res.note}",
                                             blocking=True, action="request_information", owner="vendor",
                                             task_key=f"vendor:{key}", task_title=f"Answer the reviewer: {title}", vendor_text=ask, fingerprint=fp,
                                             evidence={**ev, "resolution": {"by": res.actor, "note": res.note}}))
            # The vendor answered the request with a new revision (an explanation or supporting file) while this
            # evidence stayed the same: the judgement goes back to a reviewer, with the earlier one kept on record.
            reason = (f"{reason} — earlier not accepted by {res.actor} ({res.note}); the vendor has since submitted "
                      f"revision {self.inp.revision_number}, so decide again")
            ev = {**ev, "previous_judgement": {"by": res.actor, "note": res.note, "outcome": res.outcome}}
        stale = [r for r in self.inp.resolutions if r.check_key == key and r.fingerprint != fp]
        if stale:
            ev = {**ev, "superseded_reviews": [{"by": r.actor, "note": r.note, "outcome": r.outcome} for r in stale]}
        return self.add(CheckOutcome(key, group, "needs_review", title, reason, blocking=True, action="review",
                                     owner=owner, task_key=task_key, fingerprint=fp, evidence=ev))


# Why a model-read value is not proven by the document's own text layer.
UNGROUNDED = {
    "visual_unconfirmed": "read from a scanned image",
    "quote_not_in_text": "not found in the page's text layer",
    "none": "its page could not be identified (the cited page does not exist or no page contains it)",
}
# Who issues each official certificate, as it appears in the signer certificate's name or organisation.
EXPECTED_SIGNERS = {
    "gst_certificate": ("GOODS AND SERVICES TAX NETWORK",),
    "incorporation_certificate": ("MINISTRY OF CORPORATE AFFAIRS", "MCA21"),
    "pan_card": ("INCOME TAX DEPARTMENT", "NSDL", "PROTEAN", "UTI INFRASTRUCTURE", "UTIITSL"),
}
SIGNED_SLOTS = (*EXPECTED_SIGNERS, "msme_certificate")
CHAIN_NOTE = ("Not checked in this demo: the certificate chain up to the CCA India root (production pins the CCA roots).")

# Registry statuses a reviewer can record; only "active" satisfies the check.
REGISTRY_STATUSES = {"active", "cancelled", "suspended", "inactive"}


def grounded(provenance: str | None) -> bool:
    """A value is grounded only when our code found it in the page's own text layer."""
    return provenance == "text_layer_verified"


def _obs_found(inp: EvalInput, dtype: str, key: str):
    """The first document of this type that has the field, and the observation: (doc, field) or (None, None)."""
    for d in inp.docs_of_type(dtype):
        f = d.obs(key)
        if f and f.status == "found":
            return d, f
    return None, None


def _obs_value(inp: EvalInput, dtype: str, key: str) -> str | None:
    _, f = _obs_found(inp, dtype, key)
    return f.normalized if f else None


def _sorted(items) -> list:
    """Fingerprint lists in a fixed order: a stored reading's field order is not stable (JSONB reorders keys), and
    neither is the order documents finish reading. Only the evidence itself may change a fingerprint."""
    return sorted(items, key=lambda x: json.dumps(x, sort_keys=True, default=str))


def _signer(s: dict) -> str:
    who = s.get("signer") or {}
    return ", ".join(x for x in (who.get("cn"), who.get("o")) if x) or "an unnamed signer"


def _authenticity(b: _Builder, d) -> None:
    """Is an official certificate unchanged since the issuing authority signed it? (pdf/signature.py)"""
    label = DOCUMENT_TYPES.get(d.slot, d.slot).split(" (")[0]
    key, title = f"authenticity:{d.slot}", f"Digital signature: {label}"
    sigs = d.signatures.get("signatures") or []
    broken = [s for s in sigs if s.get("status") == "broken"]
    valid = [s for s in sigs if s.get("status") == "valid"]
    added = bool(d.signatures.get("changed_after_signing"))  # the reader's flag: bytes added after the last signature
    # Changed either way: signed content edited in place (a broken signature) or content added after signing.
    ev = {"document": d.filename, "document_id": d.document_id, "signatures": sigs,
          "changed_after_signing": bool(broken) or added, "content_added_after_signing": added}
    if not sigs:
        b.info(key, "authenticity", title, f"{d.filename} has no digital signature (a scan or a re-saved copy), so its "
               "authenticity can't be proven from the file itself; the other checks still apply.", **ev)
        return
    fp = fingerprint("authenticity", d.slot, d.sha256)
    if broken or added:
        why = (f"its digital signature no longer matches the content: the file was changed after {_signer(broken[0])} "
               "signed it" if broken else f"content was added after {_signer(valid[-1] if valid else sigs[-1])} signed it")
        c = b.review(key, "authenticity", title, f"{d.filename}: {why}. Treat it as possibly edited and compare it with a "
                     "copy downloaded from the issuing portal.", "reviewer", "reviewer:authenticity", fp, **ev)
        c.severity = "high"
        return
    if not valid:
        b.info(key, "authenticity", title, f"{d.filename} carries a digital signature that could not be read; its "
               "authenticity is not proven from the file.", **ev)
        return
    s = valid[-1]
    issuer = (s.get("issuer") or {}).get("cn") or "an unnamed authority"
    expected = EXPECTED_SIGNERS.get(d.slot)
    names = " ".join(filter(None, ((s.get("signer") or {}).get(k) for k in ("cn", "o", "ou")))).upper()
    if expected is None or any(t in names for t in expected):
        b.ok(key, "authenticity", title, f"Digitally signed by {_signer(s)} (certificate from {issuer}) and unchanged "
             f"since signing. {CHAIN_NOTE}", **ev)
    else:
        b.review(key, "authenticity", title, f"{d.filename} is digitally signed by {_signer(s)}, which is not the "
                 f"authority that issues a {label}. Check where this copy came from.", "reviewer",
                 "reviewer:authenticity", fp, expected_signer=list(expected), **ev)


def _latest(inp: EvalInput, identifier_type: str, value: str):
    recs = [v for v in inp.verifications if v.identifier_type == identifier_type and v.identifier_value == value]
    return recs[-1] if recs else None


def _bank_at_source(b: _Builder, inp: EvalInput, acct: str, ifsc: str, bank_name: str | None, declared: str | None) -> None:
    """The branch from the IFSC directory, and the holder the bank itself names (verification/bank.py)."""
    if not ifsc or not check_ifsc(ifsc).ok:
        return
    v = _latest(inp, "IFSC", ifsc)
    if v is not None:
        o, demo = v.observed or {}, v.method == "demo_bank_directory"
        ev = {"ifsc": ifsc, "method": v.method, "source": v.source, "result": v.result, "observed": o}
        src = "SIMULATED demo directory: the sample banks are fictional" if demo else "RBI branch directory, live"
        title = "Bank branch (IFSC directory)"
        if v.result == "found":
            where = branch_place(o)
            if bank_name and o.get("BANK") and not same_bank(bank_name, o.get("BANK"), ifsc[:4]):
                b.vendor("verification:bank_branch", "verification", title,
                         f"IFSC {ifsc} belongs to {o.get('BANK')} ({where}), not {bank_name} ({src})", "vendor:bank_details",
                         f"The IFSC {ifsc} belongs to {o.get('BANK')} ({where}), but the form names {bank_name}. "
                         "Please check the IFSC and the bank name.", **ev)
            else:
                b.ok("verification:bank_branch", "verification", title, f"IFSC {ifsc} is {o.get('BANK')}, {where} ({src})", **ev)
        elif v.result == "not_found":
            b.vendor("verification:bank_branch", "verification", title, f"IFSC {ifsc} is not in the branch directory ({src})",
                     "vendor:bank_details", f"The IFSC {ifsc} is not a bank branch in the RBI directory: it may be mistyped, "
                     "or the branch may have closed or merged. Please check it against a cheque leaf or with your bank.", **ev)
        else:
            b.info("verification:bank_branch", "verification", title,
                   f"Not checked: the branch directory could not be reached ({v.source}). A retry checks again.", **ev)
    if not acct:
        return
    v = _latest(inp, "BANK_ACCOUNT", f"{acct}|{ifsc}")
    if v is None:
        return
    title, masked = "Bank account verified at the bank (penny drop)", f"••{acct[-4:]}"
    ev = {"account": masked, "ifsc": ifsc, "method": v.method, "source": v.source, "result": v.result}
    sandbox = " — SANDBOX penny drop (SIMULATED); production calls the bank through a payment provider"
    if v.result == "valid":
        name = (v.observed or {}).get("name_at_bank")
        score, verdict = name_match(name, declared)
        ev.update(name_at_bank=name, declared_holder=declared, name_match_score=score, name_match_result=verdict)
        if verdict == "MATCH":
            b.ok("verification:bank_account", "verification", title,
                 f"The bank confirms account {masked} is held by {name} (name match {score}/100){sandbox}", **ev)
        elif verdict == "PARTIAL_MATCH":
            b.review("verification:bank_account", "verification", title,
                     f"The bank names the holder of {masked} as '{name}', which only partly matches the declared holder "
                     f"'{declared}' (name match {score}/100){sandbox}. Confirm the account belongs to the vendor.",
                     "finance", "finance:bank_account", fingerprint(acct, ifsc, name, declared), **ev)
        else:
            b.vendor("verification:bank_account", "verification", title,
                         f"The bank names the holder of {masked} as '{name}', not '{declared}' (name match {score}/100)"
                         f"{sandbox}. Payments could go to someone other than the vendor.", "vendor:bank_details",
                         f"Your bank could not confirm that account {masked} is held by {declared}. Please check the "
                         "account number and IFSC, or provide an account held in your company's name.", severity="high", **ev)
    elif v.result == "invalid":
        b.vendor("verification:bank_account", "verification", title,
                 f"The bank reports no account {masked} at {ifsc}{sandbox}", "vendor:bank_details",
                 f"The bank reports that account {masked} does not exist at IFSC {ifsc}. Please check the account number.",
                 **ev)
    elif v.result == "no_record":
        b.info("verification:bank_account", "verification", title,
               f"Not verified at the bank: this demo's bank check (a SANDBOX penny drop) knows only the sample banks' "
               "accounts. In production the account holder is confirmed with the bank through a payment provider's "
               "account-validation API; here the bank proof document is the evidence.", **ev)
    else:
        b.info("verification:bank_account", "verification", title,
               "Not verified at the bank: the provider was unavailable. A retry checks again.", **ev)


FILING_KEYS = ("filings", "gstr3b_missed", "gstr1_missed")


def _has_filings(obs: dict) -> bool:
    """Does this registry result say anything about return filing?"""
    return bool(isinstance(obs.get("filings"), list) and obs["filings"]) or any(obs.get(k) is not None for k in FILING_KEYS[1:])


def _gst_profile(b: _Builder, gstin: str, obs: dict, src: str) -> None:
    """What the GST taxpayer record says beyond 'active': taxpayer type, and whether returns are being filed."""
    tt = str(obs.get("taxpayer_type") or "").strip()
    if tt:
        if tt.lower().startswith("composition"):
            b.info("verification:gst_taxpayer_type", "verification", "GST taxpayer type",
                   f"Composition taxpayer ({src}): it issues bills of supply, not tax invoices, so the buyer gets no input "
                   "tax credit on its invoices (CGST s.10). A pricing and payment-terms point, not a reason to refuse.",
                   taxpayer_type=tt)
        else:
            b.ok("verification:gst_taxpayer_type", "verification", "GST taxpayer type",
                 f"{tt} taxpayer ({src}): issues tax invoices, so input tax credit is available (subject to CGST s.16)",
                 taxpayer_type=tt)
    filings = obs.get("filings")
    unrecorded: list[str] = []
    if isinstance(filings, list) and filings:
        last12 = filings[-12:]
        m3 = [f.get("period") for f in last12 if not f.get("gstr3b_filed_on")]
        m1 = [f.get("period") for f in last12 if not f.get("gstr1_filed_on")]
        n3, n1, span = len(m3), len(m1), f"{last12[0].get('period')} to {last12[-1].get('period')}"
    elif obs.get("gstr3b_missed") is not None or obs.get("gstr1_missed") is not None:
        # A lookup records both counts now, but a stored one may hold only one: a known gap still counts, and a count
        # that was never recorded is never read as "filed".
        n3, n1, m3, m1 = int(obs.get("gstr3b_missed") or 0), int(obs.get("gstr1_missed") or 0), [], []
        unrecorded = [name for name, k in (("GSTR-3B", "gstr3b_missed"), ("GSTR-1", "gstr1_missed")) if obs.get(k) is None]
        span = "the last 12 months, as recorded by a reviewer"
    else:
        return
    if obs.get("filings_from"):
        src = f"{src}; filing counts from {obs['filings_from']}"
    ev = {"gstin": gstin, "gstr3b_not_filed": m3 or n3, "gstr1_not_filed": m1 or n1, "period": span}
    title = "GST returns filed (last 12 months)"
    composition = str(obs.get("taxpayer_type") or "").strip().lower().startswith("composition")
    if n3 == 0 and n1 == 0 and not unrecorded:
        if composition:
            # GSTR-1 and GSTR-3B don't apply to a Composition taxpayer, so "none missed" proves nothing about filing.
            b.info("verification:gst_returns", "verification", title,
                   f"Filing not assessed: no missed GSTR-1 or GSTR-3B is recorded, but a Composition taxpayer doesn't file "
                   f"those monthly returns; it files CMP-08 quarterly and GSTR-4 yearly. Check those on the GST portal "
                   f"if needed ({src}).", **ev)
            return
        b.ok("verification:gst_returns", "verification", title, f"GSTR-1 and GSTR-3B filed for every month, {span} ({src})", **ev)
        return
    parts = []
    if n3:
        parts.append(f"GSTR-3B (the return that pays the tax) not filed for {n3} month(s)" + (f": {', '.join(m3)}" if m3 else ""))
    if n1:
        parts.append(f"GSTR-1 not filed for {n1} month(s)" + (f": {', '.join(m1)}" if m1 else ""))
    if unrecorded:
        ev["returns_not_recorded"] = unrecorded
        parts.append(f"{' and '.join(unrecorded)} filing not recorded in the lookup")
    if composition:
        # No input tax credit is claimed on a Composition supplier's bills of supply, so there is no credit to lose.
        why = ("This supplier is a Composition taxpayer, so no input tax credit is involved; a Composition taxpayer files "
               "CMP-08 quarterly and GSTR-4 yearly rather than monthly returns, so check what the portal showed. Unfiled "
               "returns are a compliance signal: a registration can be cancelled for them (CGST s.29(2)). Decide "
               "whether to proceed.")
    else:
        why = ("Input tax credit on this supplier's invoices is at risk: credit is allowed only when the supplier reports "
               "the invoice and pays the tax (CGST s.16(2)(aa) and (c)), and Rule 37A makes the buyer reverse credit when "
               "the supplier hasn't filed GSTR-3B. Decide the payment terms (for example, hold the GST portion until the "
               "returns are filed).")
    b.review("verification:gst_returns", "verification", title, f"{'; '.join(parts)} ({span}; {src}). {why}",
             "finance", "finance:gst_returns", fingerprint(gstin, m3 or n3, m1 or n1, *unrecorded), **ev)


def run_checks(inp: EvalInput, cmp: ComparisonResult) -> list[CheckOutcome]:
    b = _Builder(inp)
    form, policy = inp.form, inp.policy
    et = get_field(form, "business.entity_type")
    legal = get_field(form, "business.legal_name")
    gst_registered = get_field(form, "tax.gst_registered")

    # ------------------------------------------------------------------ completeness: form fields
    missing, unknown = [], []
    for req in policy.required_fields:
        if req.when is not None:
            applies = evaluate_condition(req.when, form)
            if applies is None:
                unknown.append(FIELD_LABELS.get(req.when.field, req.when.field))
                continue
            if not applies:
                continue
        v = get_field(form, req.field)
        if v is None or v is False:
            missing.append(FIELD_LABELS.get(req.field, req.field))
    if missing or unknown:
        items = sorted(set(missing)) + [f"answer '{u}'" for u in sorted(set(unknown))]
        b.vendor("required_fields", "completeness", "Required form fields", f"Missing: {', '.join(items)}",
                 "vendor:form_fields", f"Please complete these form fields: {', '.join(items)}.", missing=missing)
    else:
        b.ok("required_fields", "completeness", "Required form fields", "All required fields are filled in")

    # A filled-in field is not enough: the values that aren't identifiers get a shape check too.
    bad = []
    for path, check in (("contact.email", check_email), ("business.registered_address.pincode", check_pincode),
                        ("business.incorporation_date", check_past_date)):
        v = get_field(form, path)
        if v is not None and not (r := check(v)).ok:
            bad.append(f"{FIELD_LABELS[path]}: {r.reason}")
    if bad:
        b.vendor("format:form_values", "format", "Email, PIN code and date formats", "; ".join(bad), "vendor:form_values",
                 "Please correct these details: " + "; ".join(bad) + ".", problems=bad)
    else:
        b.ok("format:form_values", "format", "Email, PIN code and date formats", "No malformed values")

    # ------------------------------------------------------------------ completeness: documents
    for req in policy.documents:
        key = f"document:{req.id}"
        label = DOCUMENT_TYPES[req.document_type]
        applies = True
        if req.when is not None:
            applies = evaluate_condition(req.when, form)
        if applies is None:
            trig = FIELD_LABELS.get(req.when.field, req.when.field)
            if req.level == "required":
                b.vendor(key, "completeness", label, f"Cannot tell whether this is needed: '{trig}' is unanswered",
                         "vendor:form_fields", f"Please answer '{trig}' so we know whether your {label} is needed.")
            continue
        if applies is False:
            b.na(key, "completeness", label, f"Not needed ({FIELD_LABELS.get(req.when.field, req.when.field)} = "
                                             f"{get_field(form, req.when.field)})")
            continue
        required = req.level == "required"
        doc = inp.doc_in_slot(req.document_type)
        if doc is None:
            if required:
                b.vendor(key, "completeness", label, f"Required document not uploaded. Why it's needed: {req.reason}",
                         f"vendor:upload:{req.document_type}", f"Please upload your {label}. {req.reason}.")
            else:
                b.info(key, "completeness", label, "Optional — not provided")
            continue
        ev = {"document_id": doc.document_id, "filename": doc.filename, "layout": doc.layout}
        if doc.status == "failed":
            c = CheckOutcome(key, "completeness", "unable", label,
                             f"The document could not be read automatically ({(doc.error or {}).get('message', 'unknown error')}). "
                             "This is a processing problem, not a problem with the vendor's document.",
                             blocking=required, action="retry", owner="system", task_key="system:reading", evidence=ev)
            b.add(c)
            continue
        if doc.detected_type and doc.detected_type != req.document_type:
            detected = DOCUMENT_TYPES.get(doc.detected_type, "an unrelated document")
            text = (f"The file you uploaded as your {label} ({doc.filename}) appears to be a different document"
                    + (f": {detected}" if doc.detected_type != 'other' else "") + ". "
                    f"Please upload your {label}.")
            if required:
                b.vendor(key, "completeness", label, f"Wrong document: content looks like {detected}",
                         f"vendor:upload:{req.document_type}", text, detected_type=doc.detected_type, **ev)
            else:
                b.info(key, "completeness", label, f"Optional document looks like {detected}", **ev)
            continue
        problems, visual = [], []
        for f in (doc.reading.fields.values() if doc.reading else []):
            if f.critical and f.status in ("unreadable", "not_found", "ambiguous"):
                problems.append((f.key, f.status))
            if f.critical and f.provenance == "visual_unconfirmed":
                visual.append(f.key)
        if problems:
            desc = ", ".join(f"{k.replace('_', ' ')} {s.replace('_', ' ')}" for k, s in problems)
            unreadable = any(s == "unreadable" for _, s in problems)
            text = (f"We couldn't read the {', '.join(k.replace('_', ' ') for k, _ in problems)} on your {label}"
                    + (" — it appears obscured or blurred. Please upload a clearer copy." if unreadable
                       else ". Please upload a complete copy that shows it."))
            if required:
                b.vendor(key, "completeness", label, f"Key information not confirmed: {desc}", f"vendor:upload:{req.document_type}",
                         text, status="unable", problems=problems, quality_issues=doc.reading.quality_issues if doc.reading else [], **ev)
            else:
                b.info(key, "completeness", label, f"Optional document incomplete: {desc}", **ev)
            continue
        note = f" Read from a scanned image ({', '.join(visual)} not text-verified)." if visual else ""
        b.ok(key, "completeness", label, f"Present, correct type, key fields read ({layout_label(doc.layout)}).{note}", **ev)

    # ------------------------------------------------------------------ authenticity: digital signatures
    for d in inp.docs:
        if d.slot in SIGNED_SLOTS and d.signatures is not None:
            _authenticity(b, d)

    # ------------------------------------------------------------------ scope
    if et and et not in policy.scope.supported_entity_types:
        b.review("scope:entity_type", "scope", "Supported vendor type",
                 f"Entity type '{et}' is outside the supported scope (private limited companies and LLPs)",
                 "reviewer", "reviewer:scope", fingerprint(et))
    if gst_registered == "no":
        b.review("scope:gst_unregistered", "scope", "GST registration",
                 "Vendor declares no GST registration; this policy routes unregistered vendors to scope review",
                 "reviewer", "reviewer:scope", fingerprint("gst_no"))

    # ------------------------------------------------------------------ format + cross-identifier checks
    pan = get_field(form, "tax.pan")
    gstin = get_field(form, "tax.gstin") if gst_registered == "yes" else None
    inc_pan = _obs_value(inp, "incorporation_certificate", "pan")
    pan_task = "vendor:pan"
    pan_is_personal = False
    if pan:
        r = check_pan(pan)
        if not r.ok:
            b.vendor("format:pan", "format", "PAN format", r.reason, pan_task, f"The PAN '{pan}' is not valid: {r.reason}.")
        else:
            b.ok("format:pan", "format", "PAN format", r.reason)
            m = check_pan_matches_entity(pan, et)
            if m.facts.get("comparable"):
                if m.ok:
                    b.ok("format:pan_entity", "format", "PAN holder type vs entity type", m.reason, **m.facts)
                else:
                    pan_is_personal = m.facts["actual_code"] == "P"
                    hints = []
                    if inc_pan and inc_pan != clean_identifier(pan):
                        hints.append(f"your certificate of incorporation lists the PAN as {inc_pan}")
                    if gstin and check_gstin(gstin).ok and clean_identifier(gstin)[2:12] != clean_identifier(pan):
                        hints.append(f"your GSTIN contains PAN {clean_identifier(gstin)[2:12]}")
                    holder = PAN_HOLDER_TYPES.get(m.facts["actual_code"], "another kind of holder")
                    text = (f"The PAN you provided ({clean_identifier(pan)}) belongs to {'an individual' if pan_is_personal else 'a ' + holder.lower()}, "
                            f"not a {'company' if et == 'private_limited' else 'LLP'}. Please enter the {'company' if et == 'private_limited' else 'LLP'}'s own PAN "
                            "and upload its PAN card.")
                    if hints:
                        text += " For reference, " + " and ".join(hints) + "."
                    b.vendor("format:pan_entity", "format", "PAN holder type vs entity type", m.reason, pan_task, text,
                             hints=hints, **m.facts)
    if gstin:
        r = check_gstin(gstin)
        if not r.ok:
            b.vendor("format:gstin", "format", "GSTIN format", r.reason, "vendor:gstin", f"The GSTIN '{gstin}' is not valid: {r.reason}.")
        else:
            b.ok("format:gstin", "format", "GSTIN format", r.reason, **r.facts)
            if pan and check_pan(pan).ok:
                link = check_gstin_pan_link(gstin, pan)
                if link.ok:
                    b.ok("cross:gstin_pan", "consistency", "GSTIN ↔ PAN", link.reason, **link.facts)
                else:
                    embedded = link.facts["embedded_pan"]
                    if pan_is_personal and (inc_pan in (None, embedded)):
                        b.vendor("cross:gstin_pan", "consistency", "GSTIN ↔ PAN",
                                 f"GSTIN embeds PAN {embedded}, which fits the entity"
                                 + (" and matches the incorporation certificate" if inc_pan == embedded else "")
                                 + f"; the PAN entered ({clean_identifier(pan)}) is the odd one out.",
                                 pan_task, f"Your GSTIN is registered under PAN {embedded}; please use the business PAN.", **link.facts)
                    else:
                        corroborated = [s for s, v in (("PAN card", _obs_value(inp, "pan_card", "pan")),
                                                       ("incorporation certificate", inc_pan)) if v == clean_identifier(pan)]
                        b.vendor("cross:gstin_pan", "consistency", "GSTIN ↔ PAN",
                                 link.reason + (f". The PAN is corroborated by the {' and '.join(corroborated)}." if corroborated else ""),
                                 "vendor:gst_identity",
                                 f"The GSTIN you provided ({clean_identifier(gstin)}) is registered to a different taxpayer (PAN {embedded}) "
                                 f"than {legal or 'your business'} (PAN {clean_identifier(pan)}). Please provide the GST registration held by "
                                 f"{legal or 'your business'}, or explain the relationship.",
                                 severity="high", corroborated_by=corroborated, **link.facts)
            addr_state = canonical_state(get_field(form, "business.registered_address.state"))
            gst_state = GST_STATE_CODES.get(clean_identifier(gstin)[:2])
            if addr_state and gst_state:
                if addr_state == gst_state or policy.reviews.gst_state_differs_from_address == "ignore":
                    b.ok("cross:gst_state", "consistency", "GST state vs registered office", f"GSTIN state {gst_state} matches the registered office")
                else:
                    b.review("cross:gst_state", "consistency", "GST state vs registered office",
                             f"GSTIN is registered in {gst_state} but the registered office is in {addr_state}. A business can hold "
                             "GST registrations in several states; confirm this is the right one for supplies to us.",
                             "reviewer", "reviewer:gst_state", fingerprint(gstin, addr_state), gst_state=gst_state, address_state=addr_state)
    reg = get_field(form, "business.registration_number")
    if reg and et in ("private_limited", "llp"):
        r = check_registration_number(reg, et)
        if not r.ok:
            b.vendor("format:registration", "format", "CIN / LLPIN format", r.reason, "vendor:registration",
                     f"The registration number '{reg}' doesn't look right: {r.reason}.")
        else:
            b.ok("format:registration", "format", "CIN / LLPIN format", r.reason)
            if r.facts.get("kind") == "CIN":
                addr_state = canonical_state(get_field(form, "business.registered_address.state"))
                cin_state = CIN_STATE_CODES.get(r.facts.get("state_code", ""))
                if addr_state and cin_state and addr_state != cin_state:
                    b.review("cross:cin_state", "consistency", "CIN state vs registered office",
                             f"CIN shows registration in {cin_state} but the registered office is in {addr_state} "
                             "(possible relocation or typo)", "reviewer", "reviewer:cin_state", fingerprint(reg, addr_state))
    for path, fn, key, task in (("bank.ifsc", check_ifsc, "format:ifsc", "vendor:bank_details"),
                                ("bank.account_number", check_account_number, "format:account", "vendor:bank_details")):
        v = get_field(form, path)
        if v:
            r = fn(v)
            if r.ok:
                b.ok(key, "format", FIELD_LABELS[path] + " format", r.reason)
            else:
                b.vendor(key, "format", FIELD_LABELS[path] + " format", r.reason, task, f"{FIELD_LABELS[path]}: {r.reason}.")
    if get_field(form, "tax.msme_registered") == "yes" and get_field(form, "tax.udyam_number"):
        r = check_udyam(get_field(form, "tax.udyam_number"))
        if not r.ok:
            b.vendor("format:udyam", "format", "Udyam number format", r.reason, "vendor:msme", f"{r.reason}.")

    # ------------------------------------------------------------------ consistency: form vs documents
    task_for = {"pan": pan_task, "gstin": "vendor:gstin", "registration_number": "vendor:registration",
                "udyam_number": "vendor:msme", "account_number": "vendor:bank_details", "ifsc": "vendor:bank_details",
                "account_holder": "vendor:bank_details"}
    covered: set[tuple[int, str]] = set()  # (document, field) already sent to a reviewer as a mismatch
    for key in ("pan", "gstin", "registration_number", "udyam_number", "account_number", "ifsc", "account_holder"):
        fc = cmp.fields.get(key)
        if not fc or not fc.form_value:
            continue
        ck = f"match:{key}"
        if fc.status in ("agrees", "equivalent"):
            b.ok(ck, "consistency", f"{fc.label}: form vs documents", fc.detail)
        elif fc.status in ("differs", "similar"):
            bad = [o for o in fc.observations if o.status == "found" and o.relation in ("differs", "similar")]
            if bad and not any(grounded(o.provenance) for o in bad):
                # The disagreeing value is not proven by the text layer: don't blame the vendor for a possible misread.
                why = "; ".join(sorted({UNGROUNDED.get(o.provenance, o.provenance) for o in bad}))
                b.review(ck, "consistency", f"{fc.label}: form vs documents",
                         f"{fc.detail}. The document value is {why}, so it may be a misread; "
                         "compare it with the page image.", "reviewer", "reviewer:visual_read",
                         fingerprint(fc.form_value, _sorted([(o.document_id, o.value, o.provenance) for o in bad])),
                         observations=[o.__dict__ for o in bad])
                covered.update((o.document_id, o.field) for o in bad)
            elif key == "pan" and pan_is_personal:
                b.vendor(ck, "consistency", f"{fc.label}: form vs documents", fc.detail, pan_task,
                         "The PAN on your form and documents must be the business's own PAN.")
            else:
                shown = "; ".join(f"{o.source.split(' (')[0]} shows {mask(o.raw) if 'account' in key and key != 'account_holder' else repr(o.raw)}"
                                  for o in bad)
                form_shown = mask(fc.form_value) if key == "account_number" else repr(fc.form_value)
                b.vendor(ck, "consistency", f"{fc.label}: form vs documents", fc.detail, task_for[key],
                         f"The {fc.label} on your form ({form_shown}) doesn't match your documents ({shown}). "
                         "Please correct whichever is wrong.", observations=[o.__dict__ for o in bad])
        elif fc.status == "unconfirmed":
            b.info(ck, "consistency", f"{fc.label}: form vs documents", fc.detail)

    # legal name across documents
    sha_of = {d.document_id: d.sha256 for d in inp.docs}
    fc = cmp.fields.get("legal_name")
    if fc and fc.form_value:
        by_type: dict[str, list] = {}
        for o in fc.observations:
            if o.status == "found":
                by_type.setdefault(o.doc_type, []).append(o)
        for dtype, obs in by_type.items():
            ck = f"match:legal_name:{dtype}"
            title = f"Legal name vs {obs[0].source.split(' (')[0]}"
            worst = "differs" if any(o.relation == "differs" for o in obs) else ("similar" if any(o.relation == "similar" for o in obs) else "ok")
            if worst == "ok":
                b.ok(ck, "consistency", title, obs[0].note or "Same name")
                continue
            # Every document of this type is assessed, not just the first that disagrees: a proven contradiction
            # (a different name, grounded in the text layer) goes to the vendor even when another document of the
            # same type only needs a judgement, so accepting a close variant on one card can't clear another card.
            bad = [x for x in obs if x.relation in ("differs", "similar")]
            hard = [x for x in bad if x.relation == "differs" and grounded(x.provenance)]
            if not hard:
                def says(x):
                    return (f"document says '{x.raw}' ({x.note})" + ("" if grounded(x.provenance) else
                            f"; the document value is {UNGROUNDED.get(x.provenance, x.provenance)}"))
                # One judgement over exactly these observations: accepting it covers them and nothing else. A value
                # the text layer doesn't prove was also checked against THAT file's image, so the judgement is bound to
                # the file too: a replacement file needs its own look (a text-proven variant stays a reusable judgement
                # that the two names are the same business).
                def item(x):
                    if grounded(x.provenance):
                        return (x.raw, x.provenance)
                    return (x.raw, x.provenance, sha_of.get(x.document_id), x.page)
                fp = (fingerprint(fc.form_value, *item(bad[0])) if len(bad) == 1 else
                      fingerprint(fc.form_value, _sorted([item(x) for x in bad])))
                b.review(ck, "consistency", title, f"Form says '{fc.form_value}', " + "; ".join(says(x) for x in bad),
                         "reviewer", "reviewer:name_variant", fp, observation=bad[0].__dict__,
                         observations=[x.__dict__ for x in bad])
                covered.update((x.document_id, x.field) for x in bad)
                continue
            o = hard[0]
            if dtype == "pan_card":
                text = (f"The PAN card you uploaded is in the name of '{o.raw}', not {fc.form_value}. "
                        "Please upload the business's own PAN card.")
                b.vendor(ck, "consistency", title, f"PAN card holder is '{o.raw}'", pan_task, text, observation=o.__dict__)
            elif dtype == "gst_certificate":
                b.vendor(ck, "consistency", title, f"GST certificate legal name is '{o.raw}', not '{fc.form_value}'",
                         "vendor:gst_identity",
                         f"Your GST certificate is issued to '{o.raw}', not {fc.form_value}. Please provide the GST "
                         "registration held by your business, or explain the relationship.", severity="high", observation=o.__dict__)
            elif dtype == "incorporation_certificate":
                b.vendor(ck, "consistency", title, f"Incorporation certificate is for '{o.raw}'", "vendor:registration",
                         f"Your certificate of incorporation is for '{o.raw}', not {fc.form_value}. Please upload the right certificate.",
                         observation=o.__dict__)
            else:
                b.vendor(ck, "consistency", title, f"Document is for '{o.raw}'", "vendor:msme",
                         f"Your Udyam certificate is for '{o.raw}', not {fc.form_value}.", observation=o.__dict__)

    # ------------------------------------------------------------------ evidence grounding: values only the model vouches for
    # A critical value counts as document evidence only if it is in the page's own text layer. Anything else (a scan,
    # or a value the text layer doesn't contain) needs a person to confirm it against the page image, bound to the
    # exact file and values, even when it happens to match the form.
    for d in inp.docs:
        if not d.reading or d.status == "failed":
            continue
        items = sorted((f for f in d.reading.fields.values()
                        if f.critical and f.status == "found" and f.provenance != "inferred" and not grounded(f.provenance)
                        and (d.document_id, f.key) not in covered), key=lambda f: f.key)
        if not items:
            continue
        label = DOCUMENT_TYPES.get(d.effective_type, DOCUMENT_TYPES.get(d.slot, d.slot))
        desc = "; ".join(f"{f.key.replace('_', ' ')} '{mask(f.value) if f.key == 'account_number' else f.value}' "
                         f"({UNGROUNDED.get(f.provenance, f.provenance)}{f', page {f.page}' if f.page else ''})" for f in items)
        b.review(f"confirm:{d.slot}", "completeness", f"Confirm values read from {d.filename}",
                 f"The model read these key values from the {label}, but our code could not find them in the document's "
                 f"text layer: {desc}. Compare them with the page image before relying on them.",
                 "reviewer", "reviewer:confirm_reading",
                 fingerprint(d.sha256, [(f.key, f.normalized, f.provenance, f.page) for f in items]),
                 document_id=d.document_id, filename=d.filename,
                 unconfirmed_fields=[{"field": f.key, "value": f.value, "page": f.page, "provenance": f.provenance}
                                     for f in items])

    # ------------------------------------------------------------------ credibility: who owns the bank account?
    # The holder is taken from the bank proof only when its text layer proves it. A value read from a scan may be a
    # misread: the form-vs-document comparison above already sends it to a reviewer, so this check uses the vendor's
    # own form entry instead of demanding a new account because of one misread character.
    bank_doc, holder_f = _obs_found(inp, "bank_proof", "account_holder")
    holder_doc = holder_f.normalized if holder_f and grounded(holder_f.provenance) else None
    holder = holder_doc or get_field(form, "bank.account_holder") or (holder_f.normalized if holder_f else None)
    bp_file = inp.doc_in_slot("bank_proof") or bank_doc
    if holder and legal:
        rel = compare_names(holder, legal)
        src = "bank proof" if holder_doc else "form"
        same = ("exact", "equivalent", "suffix_differs")
        if rel.relation in same:
            b.ok("credibility:bank_holder", "credibility", "Bank account holder vs legal name",
                 f"Account holder on the {src} ('{holder}') matches the legal name. This is consistency between the "
                 "vendor's own form and documents, not confirmation from the bank.")
        else:
            gst_doc, gst_f = _obs_found(inp, "gst_certificate", "trade_name")
            gst_trade = gst_f.normalized if gst_f else None
            form_trade = get_field(form, "business.trade_name")
            gst_docs = inp.docs_of_type("gst_certificate")
            gst_unread = gst_trade is None and any(d.status == "failed" for d in gst_docs)
            # "Not on the certificate" is only trustworthy when the certificate has a text layer to read. On a scan,
            # a missed trade name is as likely as an absent one, so a person checks the image.
            gst_unclear = gst_trade is None and any(
                d.reading is not None and (d.obs("trade_name") is None or d.obs("trade_name").status != "found")
                and ((d.obs("trade_name") is not None and d.obs("trade_name").status in ("unreadable", "ambiguous"))
                     or d.layout != "digital")
                for d in gst_docs)
            if gst_trade and compare_names(holder, gst_trade).relation in same:
                # The clearance covers this exact bank proof and GST certificate: a replaced file needs a fresh review.
                b.review("credibility:bank_holder", "credibility", "Bank account holder vs legal name",
                         f"Account holder '{holder}' differs from the legal name '{legal}', but matches the trade name "
                         f"'{gst_trade}' on the GST certificate for this GSTIN. Accounts held in a registered trade name are "
                         "common; confirm before payments are set up. The review covers this bank proof and GST certificate.",
                         "reviewer", "reviewer:bank_holder",
                         fingerprint(holder, legal, gst_trade, bp_file.sha256 if bp_file else None, gst_doc.sha256),
                         holder=holder, legal_name=legal, supporting_trade_name=gst_trade, supporting_document="GST certificate",
                         bank_proof=bp_file.filename if bp_file else None, gst_certificate=gst_doc.filename)
            elif gst_unread:
                b.add(CheckOutcome("credibility:bank_holder", "credibility", "unable", "Bank account holder vs legal name",
                                   f"Account holder '{holder}' differs from the legal name '{legal}'. The GST certificate, which "
                                   "may list it as the registered trade name, could not be read yet, so this can't be judged. "
                                   "Retry reading first; this is not a finding about the vendor.",
                                   blocking=True, action="retry", owner="system", task_key="system:reading",
                                   evidence={"holder": holder, "legal_name": legal}))
            elif gst_unclear or (gst_f is not None and not grounded(gst_f.provenance)):
                b.review("credibility:bank_holder", "credibility", "Bank account holder vs legal name",
                         f"Account holder '{holder}' differs from the legal name '{legal}'. The trade name on the GST "
                         "certificate could not be read with certainty, so check the certificate image: if it lists this "
                         "trade name, accept; otherwise mark it not acceptable and the vendor will be asked for evidence.",
                         "reviewer", "reviewer:bank_holder",
                         fingerprint(holder, legal, "trade_name_unclear", bp_file.sha256 if bp_file else None,
                                     [d.sha256 for d in gst_docs]),
                         holder=holder, legal_name=legal)
            elif form_trade and compare_names(holder, form_trade).relation in same:
                b.vendor("credibility:bank_holder", "credibility", "Bank account holder vs legal name",
                         f"Account holder '{holder}' matches the trade name declared on the form, but no document links "
                         f"that trade name to '{legal}'", "vendor:bank_holder",
                         f"Your bank account is in the name '{holder}', which differs from your legal name {legal}. "
                         "None of your documents links that trade name to your company. Please upload evidence (for example a "
                         "GST certificate that lists the trade name, or a bank letter confirming the account belongs to "
                         f"{legal}), or provide an account in the company's legal name.", holder=holder, legal_name=legal)
            else:
                b.vendor("credibility:bank_holder", "credibility", "Bank account holder vs legal name",
                         f"Account holder '{holder}' does not match the legal name '{legal}' or any evidenced trade name",
                         "vendor:bank_holder",
                         f"Your bank account is held by '{holder}', which doesn't match your legal name {legal}. Payments can "
                         "only be made to an account held by the vendor itself. Please provide an account in your company's "
                         "name, with proof.", severity="high", holder=holder, legal_name=legal)
    acct, ifsc = get_field(form, "bank.account_number"), get_field(form, "bank.ifsc")
    if acct and ifsc:
        others = inp.duplicate_bank_vendors
        ids = [o["id"] for o in others]
        if others:
            # A reviewer's acceptance covers exactly these other records (they are in the fingerprint): if another vendor
            # starts or stops using this account, the finding is asked again rather than inheriting the old judgement.
            b.review("credibility:duplicate_bank", "credibility", "Bank account also on file for another vendor",
                     f"This account and IFSC are already on file for {len(others)} other vendor record(s) "
                     f"({', '.join(o['reference'] for o in others)}). "
                     "Could be a group company or a duplicate registration — or a payment-diversion risk.",
                     "reviewer", "reviewer:duplicate_bank", fingerprint(clean_identifier(acct), clean_identifier(ifsc), ids),
                     other_vendor_records=len(others), other_vendor_ids=ids)
        else:
            b.ok("credibility:duplicate_bank", "credibility", "Bank account not shared with another vendor",
                 "No other vendor record uses this account", other_vendor_records=0, other_vendor_ids=[])
    pan_v, gstin_v = clean_identifier(get_field(form, "tax.pan")), clean_identifier(get_field(form, "tax.gstin"))
    if pan_v or gstin_v:
        others = inp.duplicate_identity_vendors
        ids = [o["id"] for o in others]
        if others:
            # Like the shared account: the judgement covers exactly these other records. Never merged or rejected
            # automatically: one PAN can lawfully hold a GSTIN in each state (CGST s.25).
            listed = ", ".join(f"{o['reference']} (same {' and '.join(o['same'])})" for o in others)
            b.review("credibility:duplicate_identity", "credibility", "Same PAN or GSTIN as another vendor",
                     f"This PAN or GSTIN is already on file for {len(others)} other vendor record(s): {listed}. The same "
                     "business may already be a vendor (a duplicate registration), or this may be a related registration "
                     "(one PAN can hold a GSTIN in each state, CGST s.25). Check before a second vendor record is created.",
                     "reviewer", "reviewer:duplicate_identity", fingerprint(pan_v, gstin_v, ids),
                     other_vendor_records=len(others), other_vendor_ids=ids, other_vendors=others)
        else:
            b.ok("credibility:duplicate_identity", "credibility", "PAN and GSTIN not on file for another vendor",
                 "No other vendor record of this buyer has this PAN or GSTIN", other_vendor_records=0, other_vendor_ids=[])

    # ------------------------------------------------------------------ bank details verified at the source
    _bank_at_source(b, inp, clean_identifier(acct), clean_identifier(ifsc), get_field(form, "bank.bank_name"),
                    get_field(form, "bank.account_holder") or legal)

    # The date printed on the bank letter or statement (a cheque has none, so it isn't checked).
    bp = inp.doc_in_slot("bank_proof")
    dated = bp.obs("document_date") if bp else None
    issued = parse_date(dated.value, DOCUMENT_DATE_FORMATS) if dated and dated.status == "found" else None
    if issued is not None:
        age = (date.today() - issued).days
        ev = {"document": bp.filename, "document_date": issued.isoformat(), "max_age_days": BANK_PROOF_MAX_AGE_DAYS}
        if age < -1:  # one day of slack: the server's date is UTC, a letter dated today in India can be a day ahead
            # A bank doesn't date a letter in the future: the date was misread, mistyped or altered. It can't show the
            # proof is recent, so a reviewer looks at the document.
            b.review("credibility:bank_proof_date", "credibility", "Bank proof is recent",
                     f"The bank proof ({bp.filename}) is dated {dated.value}, which is after today. A future date can't "
                     "show that the proof is recent: check the date on the document (it may be misread or altered) and ask "
                     "the supplier for a correctly dated letter if needed.", "reviewer", "reviewer:bank_proof_date",
                     fingerprint(bp.sha256, issued.isoformat()), **ev)
        elif age > BANK_PROOF_MAX_AGE_DAYS and not grounded(dated.provenance):
            # The date was read from a scan or not found in the page's text: it may be misread. A person confirms it
            # before the supplier is asked to replace a document that may be fine.
            b.review("credibility:bank_proof_date", "credibility", "Bank proof is recent",
                     f"The bank proof ({bp.filename}) was read as dated {dated.value}, more than 6 months ago, but that "
                     f"date is {UNGROUNDED.get(dated.provenance, 'not confirmed from the text')}. Check the date on the "
                     "document; if it really is older than 6 months, ask the supplier for a recent letter.",
                     "reviewer", "reviewer:bank_proof_date", fingerprint(bp.sha256, issued.isoformat(), "unconfirmed"),
                     **ev)
        elif age > BANK_PROOF_MAX_AGE_DAYS:
            b.vendor("credibility:bank_proof_date", "credibility", "Bank proof is recent",
                     f"The bank proof ({bp.filename}) is dated {dated.value}, more than 6 months ago", "vendor:bank_proof_date",
                     f"Your bank proof ({bp.filename}) is dated {dated.value}, more than 6 months ago, so it may no longer "
                     "describe the account. Please upload a bank letter or statement issued in the last 6 months, or a "
                     "cancelled cheque.", **ev)
        else:
            b.ok("credibility:bank_proof_date", "credibility", "Bank proof is recent", f"Dated {dated.value}", **ev)

    # ------------------------------------------------------------------ independent verification
    for v in policy.verification:
        key = f"verification:{v.id}"
        title = "GST registry check" if v.identifier == "GSTIN" else "Company registry check"
        if v.identifier != "GSTIN":
            # Policy validation refuses requirements the engine can't run; if one slips through, block rather than skip.
            b.add(CheckOutcome(key, "verification", "unable", title,
                               f"This policy asks for a {v.identifier} registry check, which this system cannot perform. "
                               "It must not be treated as passed.", blocking=v.required, action="review", owner="reviewer",
                               task_key="reviewer:registry"))
            continue
        if gst_registered != "yes":
            b.na(key, "verification", title, "Vendor is not GST registered")
            continue
        g = clean_identifier(gstin)
        if not g or not check_gstin(g).ok:
            b.add(CheckOutcome(key, "verification", "unable", title, "Cannot look up the registry until a valid GSTIN is provided",
                               blocking=v.required))
            continue
        accepted = set(v.accepted_methods)
        recs = [r for r in inp.verifications if r.identifier_type == "GSTIN" and r.identifier_value == g and r.method in accepted]
        ignored = [r for r in inp.verifications if r.identifier_type == "GSTIN" and (r.identifier_value != g or r.method not in accepted)]
        ev: dict = {"identifier": g, "accepted_methods": sorted(accepted)}
        if ignored:
            ev["ignored_results"] = [{"identifier": r.identifier_value, "method": r.method, "result": r.result,
                                      "why": "different identifier" if r.identifier_value != g else "method not accepted by this policy"}
                                     for r in ignored]
        latest = recs[-1] if recs else None
        if latest is None:
            if "simulated_registry" in accepted:
                b.add(CheckOutcome(key, "verification", "unable", title, "Registry lookup has not run yet", blocking=v.required,
                                   action="retry", owner="system", task_key="system:registry", evidence=ev))
            else:
                b.add(CheckOutcome(key, "verification", "unable", title,
                                   f"Not performed. A reviewer must look up GSTIN {g} on the official GST portal and record "
                                   "what it shows (this policy does not accept simulated results).",
                                   blocking=v.required, action="review", owner="reviewer", task_key="reviewer:registry", evidence=ev))
            continue
        ev.update({"method": latest.method, "source": latest.source, "result": latest.result, "observed": latest.observed})
        if latest.result == "unavailable" and "simulated_registry" in accepted:
            # A retry runs the lookup again (the runner repeats it whenever the latest accepted result is unavailable).
            b.add(CheckOutcome(key, "verification", "unable", title,
                               f"Registry unavailable ({latest.source}). Not a finding about the vendor; retry or record a manual lookup.",
                               blocking=v.required, action="retry", owner="system", task_key="system:registry", evidence=ev))
        elif latest.result == "unavailable":
            # Manual lookups only: a processing retry can't look anything up, so a reviewer records a new lookup.
            b.add(CheckOutcome(key, "verification", "unable", title,
                               f"The GST portal was unavailable ({latest.source}). Not a finding about the vendor; look up "
                               f"GSTIN {g} again and record what it shows.",
                               blocking=v.required, action="review", owner="reviewer", task_key="reviewer:registry", evidence=ev))
        elif latest.result == "not_found" and latest.method == "simulated_registry" and "manual_registry" in accepted:
            # The simulated registry only knows this demo's synthetic vendors, so "no record" says nothing about a real
            # business (a well-formed GSTIN with a valid check character): a reviewer looks it up on the real portal.
            b.add(CheckOutcome(key, "verification", "unable", title,
                               f"The simulated test registry has no record of GSTIN {g}: it only knows this demo's sample "
                               "vendors, so this is not a finding about the vendor. Look the GSTIN up on the official GST "
                               "portal and record what it shows.", blocking=v.required, action="review", owner="reviewer",
                               task_key="reviewer:registry", evidence=ev))
        elif latest.result == "not_found":
            b.vendor(key, "verification", title, f"No registry record for GSTIN {g} ({latest.method})", "vendor:gstin",
                     f"We could not find GSTIN {g} in the registry. Please check the number.", **ev)
        else:
            obs = latest.observed or {}
            if latest.result == "found" and not obs.get("taxpayer_type"):
                # A later lookup that left the optional type blank (e.g. to correct the filing counts) doesn't erase
                # the type recorded earlier for this GSTIN.
                earlier = next((r for r in reversed(recs) if r.result == "found" and (r.observed or {}).get("taxpayer_type")), None)
                if earlier is not None:
                    obs = {**obs, "taxpayer_type": earlier.observed["taxpayer_type"], "taxpayer_type_from": "an earlier lookup"}
                    ev["observed"] = obs
            if latest.result == "found" and not _has_filings(obs):
                # Leaving the optional filing counts blank records nothing about filing, so it never clears returns an
                # earlier lookup found missing: those counts stand until a lookup records new ones (or finance clears them).
                earlier = next((r for r in reversed(recs) if r.result == "found" and _has_filings(r.observed or {})), None)
                if earlier is not None:
                    obs = {**obs, **{k: earlier.observed[k] for k in FILING_KEYS if k in earlier.observed},
                           "filings_from": "an earlier lookup"}
                    ev["observed"] = obs
            status = (obs.get("status") or "").strip().lower()
            reg_name = (obs.get("legal_name") or "").strip() or None
            name_rel = compare_names(reg_name, legal).relation if reg_name and legal else "missing"
            if status not in REGISTRY_STATUSES or not reg_name:
                # An incomplete result proves nothing: unknown status is not "active".
                missing = [x for x, ok in (("registration status", status in REGISTRY_STATUSES), ("legal name", bool(reg_name))) if not ok]
                b.add(CheckOutcome(key, "verification", "unable", title,
                                   f"The recorded registry result for GSTIN {g} is incomplete ({', '.join(missing)} not recorded); "
                                   "record the full lookup result.", blocking=v.required, action="review", owner="reviewer",
                                   task_key="reviewer:registry", evidence=ev))
            elif status != "active":
                b.vendor(key, "verification", title, f"Registry shows status '{obs.get('status')}' for GSTIN {g}",
                         "vendor:gst_identity", f"The registry shows your GST registration {g} as '{obs.get('status')}'. "
                         "Please provide an active registration.", severity="high", **ev)
            elif name_rel not in ("exact", "equivalent", "suffix_differs"):
                b.vendor(key, "verification", title,
                         f"Registry lists GSTIN {g} under '{reg_name}', not '{legal}' ({latest.method})", "vendor:gst_identity",
                         f"The GST registry lists GSTIN {g} under '{reg_name}', not {legal}. Please provide the GST "
                         "registration held by your business, or explain the relationship.", severity="high", **ev)
            else:
                how = ("SIMULATED test registry — not a real government lookup" if latest.method == "simulated_registry"
                       else f"recorded by a reviewer from {latest.source}")
                b.ok(key, "verification", title, f"Registry shows '{reg_name}', status {obs.get('status')} ({how})", **ev)
                _gst_profile(b, g, obs, "SIMULATED test registry" if latest.method == "simulated_registry"
                             else f"recorded by a reviewer from {latest.source}")

    # ------------------------------------------------------------------ declarations
    conduct = get_field(form, "declarations.conduct_acceptance")
    if conduct == "accepted":
        b.ok("declaration:conduct", "declaration", "Supplier code of conduct", "Accepted")
    elif conduct == "declined":
        if policy.declarations.conduct_declined == "auto_reject":
            b.add(CheckOutcome("declaration:conduct", "declaration", "not_satisfied", "Supplier code of conduct",
                               "Vendor explicitly declined the supplier code of conduct; this policy makes acceptance mandatory "
                               "and declares refusal disqualifying.", blocking=True, auto_reject=True, rule="conduct_declined"))
        else:
            b.review("declaration:conduct", "declaration", "Supplier code of conduct", "Vendor declined the code of conduct",
                     "reviewer", "reviewer:conflict", fingerprint("declined"))
    else:
        b.vendor("declaration:conduct", "declaration", "Supplier code of conduct", "Unanswered (not the same as declined)",
                 "vendor:declarations", "Please tell us whether you accept the supplier code of conduct.")
    coi = get_field(form, "declarations.conflict_of_interest")
    if coi == "none":
        b.ok("declaration:conflict", "declaration", "Conflict of interest", "None declared")
    elif coi == "disclosed":
        b.review("declaration:conflict", "declaration", "Conflict of interest",
                 f"Disclosed: {get_field(form, 'declarations.conflict_details') or '(no details given)'}",
                 "reviewer", "reviewer:conflict", fingerprint(get_field(form, "declarations.conflict_details")))
    else:
        b.vendor("declaration:conflict", "declaration", "Conflict of interest", "Unanswered", "vendor:declarations",
                 "Please tell us whether you have any conflict of interest to declare.")

    # ------------------------------------------------------------------ changes since the baseline
    # Compared with the BASELINE: the revision a person last cleared (finance for bank details, a reviewer for identity),
    # else the first submission of this request (or the approved profile for a change request); never just the previous
    # revision, or an unchanged later revision would make an unreviewed change look settled.
    if inp.baselines:
        bank_base = inp.baselines.get("bank") or (None, "the first submission")
        base = bank_base[1]
        bank_changes = [c for c in cmp.baseline_changes if c.area == "bank"]
        bank_doc = next((d for d in cmp.doc_changes if d.slot == "bank_proof"), None)
        bp = inp.doc_in_slot("bank_proof")
        # Finance's clearance covered one exact proof file: a different file is not covered, even with the same details.
        cleared_docs = bank_base[2] if len(bank_base) > 2 else None
        new_proof_after_review = cleared_docs is not None and cleared_docs.get("bank_proof") != (bp.sha256 if bp else None)
        # Finance's clearance also covered whose account it is: the legal identity (name and PAN) it was cleared for. A
        # later identity correction is a reviewer's matter AND makes finance's earlier judgement stale.
        owner_changes = ([c for c in field_changes(bank_base[0], form) if c.field in BANK_REVIEW_IDENTITY]
                         if cleared_docs is not None else [])
        if (bank_changes or new_proof_after_review or owner_changes) and policy.reviews.bank_change_requires_finance_review:
            if owner_changes and not bank_changes and not new_proof_after_review:
                title = "Legal identity changed after finance review"
                reason = (f"Revision {inp.revision_number} keeps the bank details finance cleared in {base}, but changes "
                          "whose account it is: " + "; ".join(f"{c.label} '{c.before}' → '{c.after}'" for c in owner_changes)
                          + ". Finance's review covered the account for that identity, so it needs finance review again.")
            elif bank_changes:
                parts = []
                for c in bank_changes:
                    if c.field == "bank.account_number":
                        parts.append(f"account {mask(c.before)} → {mask(c.after)}")
                    else:
                        parts.append(f"{c.label.lower()} '{c.before}' → '{c.after}'")
                title = "Bank details changed"
                reason = (f"Revision {inp.revision_number} has different bank details from {base} ({'; '.join(parts)}). "
                          "Changed bank details are the most common route to payment fraud, so finance must review the "
                          "current account and its proof (any mismatch with the proof is listed separately).")
            else:
                title = "Bank proof replaced after finance review"
                reason = (f"Revision {inp.revision_number} replaces the bank proof file from {base}; the account, IFSC "
                          "and holder are unchanged. Finance's review covered that exact file, so the new proof needs "
                          "finance review too.")
            b.review("change:bank", "change", title,
                     reason + " The review covers this exact account, IFSC, holder, proof file and legal identity "
                              "(legal name and PAN).",
                     "finance", "finance:bank_change",
                     # The baseline is part of what was confirmed: going back to an account confirmed earlier is a new
                     # change from a later cleared state, and needs its own confirmation.
                     fingerprint(clean_identifier(acct), clean_identifier(ifsc), normalise_name(holder).normalised,
                                 bp.sha256 if bp else None, clean_identifier(get_field(form, "tax.pan")),
                                 normalise_name(get_field(form, "business.legal_name")).normalised, base),
                     changes=[c.__dict__ for c in bank_changes + owner_changes], compared_with=base,
                     bank_proof_change=bank_doc.change if bank_doc else None,
                     proof_replaced_after_review=new_proof_after_review)
        elif cleared_docs is not None and inp.bank_origin is not None and (earlier := [
                c for c in field_changes(inp.bank_origin[0], form) if c.area == "bank"]):
            # Nothing new since finance confirmed the change, but it is still a change from where this request started:
            # it keeps counting for risk (and so for the second approver) and the vendor record keeps the confirmation.
            res = next((r for r in reversed(inp.resolutions) if r.check_key == "change:bank" and r.outcome == "accepted"
                        and r.confirmation), None)
            done = ({"by": res.actor, "note": res.note,
                     "confirmation": {**res.confirmation, "by": res.actor,
                                      "at": res.created_at.isoformat() if res.created_at else None}} if res else None)
            b.ok("change:bank", "change", "Bank details changed earlier, confirmed by finance",
                 f"The bank details differ from {inp.bank_origin[1]} ("
                 + "; ".join(f"account {mask(c.before)} → {mask(c.after)}" if c.field == "bank.account_number"
                             else f"{c.label.lower()} '{c.before}' → '{c.after}'" for c in earlier)
                 + f"). Finance confirmed them in {base}" + (f" by {res.confirmation.get('method_label')}" if res else "")
                 + "; unchanged since.", changes=[c.__dict__ for c in earlier], compared_with=inp.bank_origin[1],
                 **({"resolution": done} if done else {}))
        elif bank_doc and bank_doc.change == "replaced":
            b.info("change:bank", "change", "Bank proof replaced", "New bank proof file; bank details unchanged")
        identity_changes = [c for c in cmp.baseline_changes if c.area == "identity"]
        base = (inp.baselines.get("identity") or (None, "the first submission"))[1]
        if identity_changes:
            b.review("change:identity", "change", "Identity details changed",
                     f"Different from {base}: " + "; ".join(f"{c.label} '{c.before}' → '{c.after}'" for c in identity_changes)
                     + ". Typo fixes are normal; a different entity mid-application is not.",
                     "reviewer", "reviewer:identity_change", fingerprint(_sorted([(c.field, c.after) for c in identity_changes])),
                     changes=[c.__dict__ for c in identity_changes], compared_with=base)
    return b.out
