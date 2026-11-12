"""Stage 3 — assemble and compare the evidence.

Three directions: form vs documents, documents vs each other (via the same
field), and this revision vs the previous one. Nothing here decides
consequences; it only reports agreement, disagreement, gaps and changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..india.names import compare_names
from ..india.validators import clean_identifier
from ..schemas.submission import FIELD_LABELS, get_field
from .evidence import EvalInput

# key, label, form path, [(document type, template field)], kind
COMPARE_SPECS = [
    ("legal_name", "Legal name", "business.legal_name",
     [("pan_card", "holder_name"), ("incorporation_certificate", "entity_name"), ("gst_certificate", "legal_name"),
      ("msme_certificate", "enterprise_name")], "name"),
    ("trade_name", "Trade name", "business.trade_name", [("gst_certificate", "trade_name")], "name"),
    ("pan", "PAN", "tax.pan", [("pan_card", "pan"), ("incorporation_certificate", "pan")], "id"),
    ("gstin", "GSTIN", "tax.gstin", [("gst_certificate", "gstin")], "id"),
    ("registration_number", "CIN / LLPIN", "business.registration_number",
     [("incorporation_certificate", "registration_number")], "id"),
    ("udyam_number", "Udyam number", "tax.udyam_number", [("msme_certificate", "udyam_number")], "id"),
    ("account_number", "Account number", "bank.account_number", [("bank_proof", "account_number")], "id"),
    ("ifsc", "IFSC", "bank.ifsc", [("bank_proof", "ifsc")], "id"),
    ("account_holder", "Account holder", "bank.account_holder", [("bank_proof", "account_holder")], "name"),
]

DOC_TYPE_SHORT = {"pan_card": "PAN card", "gst_certificate": "GST certificate", "incorporation_certificate": "Incorporation certificate",
                  "bank_proof": "Bank proof", "msme_certificate": "Udyam certificate"}

MATERIAL_FIELDS = {
    "bank.account_number": "bank", "bank.ifsc": "bank", "bank.account_holder": "bank",
    "business.legal_name": "identity", "business.entity_type": "identity", "business.registration_number": "identity",
    "tax.pan": "identity", "tax.gstin": "identity",
}

# Identity fields that a finance clearance of a bank account also covers: whose account it is (checks.py, change:bank).
BANK_REVIEW_IDENTITY = ("business.legal_name", "tax.pan")


@dataclass
class Observation:
    source: str
    doc_type: str
    document_id: int
    field: str
    status: str  # found | not_found | unreadable | ambiguous
    value: str | None
    raw: str | None
    page: int | None
    provenance: str
    relation: str | None = None  # agrees | equivalent | similar | differs (vs the form value)
    note: str | None = None
    slot: str | None = None  # which upload slot the document is in (one file can fill two slots)


@dataclass
class FieldComparison:
    key: str
    label: str
    form_value: str | None
    status: str  # agrees | equivalent | similar | differs | unconfirmed | not_provided | no_evidence | not_read
    detail: str
    observations: list[Observation] = field(default_factory=list)


@dataclass
class Change:
    field: str
    label: str
    before: str | None
    after: str | None
    area: str  # bank | identity | other
    material: bool


@dataclass
class DocChange:
    slot: str
    change: str  # added | replaced | removed | unchanged


@dataclass
class ComparisonResult:
    fields: dict[str, FieldComparison]
    changes: list[Change]  # vs the previous revision (what this correction changed)
    doc_changes: list[DocChange]
    baseline_changes: list[Change] = field(default_factory=list)  # vs the last cleared state (what still needs review)
    baseline_labels: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"fields": {k: asdict(v) for k, v in self.fields.items()},
                "changes": [asdict(c) for c in self.changes], "doc_changes": [asdict(c) for c in self.doc_changes],
                "baseline_changes": [asdict(c) for c in self.baseline_changes], "baseline_labels": self.baseline_labels}


def field_changes(before_form: dict, after_form: dict) -> list[Change]:
    out: list[Change] = []
    for path, label in FIELD_LABELS.items():
        before, after = form_claim(before_form, path), form_claim(after_form, path)
        nb = clean_identifier(str(before)) if before is not None else None
        na = clean_identifier(str(after)) if after is not None else None
        if nb != na:
            area = MATERIAL_FIELDS.get(path, "other")
            out.append(Change(path, label, None if before is None else str(before),
                              None if after is None else str(after), area, area != "other"))
    return out


def _relate(kind: str, form_value: str | None, doc_value: str | None) -> tuple[str, str | None]:
    if kind == "id":
        return ("agrees", None) if clean_identifier(form_value) == clean_identifier(doc_value) else ("differs", None)
    cmp = compare_names(form_value, doc_value)
    if cmp.relation == "exact":
        return "agrees", None
    if cmp.relation in ("equivalent", "suffix_differs"):
        return "equivalent", cmp.explanation
    if cmp.relation == "similar":
        return "similar", cmp.explanation
    return "differs", cmp.explanation


# Form values that only apply after a "yes": a GSTIN left over after answering "not GST registered" is not a claim.
CONDITIONAL_FIELDS = {"tax.gstin": ("tax.gst_registered", "yes"), "tax.udyam_number": ("tax.msme_registered", "yes")}


def form_claim(form: dict, path: str):
    """The form's value for this field, or None when the vendor's own answers make it not applicable."""
    cond = CONDITIONAL_FIELDS.get(path)
    if cond and get_field(form, cond[0]) != cond[1]:
        return None
    return get_field(form, path)


def compare(inp: EvalInput) -> ComparisonResult:
    out: dict[str, FieldComparison] = {}
    for key, label, path, sources, kind in COMPARE_SPECS:
        form_value = form_claim(inp.form, path)
        form_value = str(form_value) if form_value is not None else None
        obs: list[Observation] = []
        for dtype, fkey in sources:
            for d in inp.docs_of_type(dtype):
                f = d.obs(fkey)
                if f is None:
                    continue
                o = Observation(source=f"{DOC_TYPE_SHORT.get(dtype, dtype)} ({d.filename})", doc_type=dtype,
                                document_id=d.document_id, field=fkey, status=f.status, value=f.normalized, raw=f.value,
                                page=f.page, provenance=f.provenance, slot=d.slot)
                if f.status == "found" and form_value:
                    o.relation, o.note = _relate(kind, form_value, f.normalized)
                obs.append(o)
        found = [o for o in obs if o.status == "found"]
        if not form_value:
            status, detail = ("not_provided", "Not entered on the form") if not found else ("not_provided", "Not entered on the form; documents show a value")
        elif not obs:
            # A document that would carry this field but couldn't be read is not the same as no such document.
            unread = [d.filename for d in inp.docs if d.status == "failed" and d.effective_type in {t for t, _ in sources}]
            status, detail = (("not_read", f"Not checked yet: {', '.join(unread)} could not be read; a retry reads it again")
                              if unread else ("no_evidence", "No uploaded document carries this field"))
        elif not found:
            states = sorted({o.status.replace("_", " ") for o in obs})
            status, detail = "unconfirmed", f"Documents could not confirm it ({', '.join(states)})"
        else:
            rels = {o.relation for o in found}
            if "differs" in rels:
                status = "differs"
                bad = [o for o in found if o.relation == "differs"]
                detail = "; ".join(f"{o.source} shows '{o.raw}'" for o in bad)
            elif "similar" in rels:
                status, detail = "similar", "Close but not identical: " + "; ".join(f"{o.source} shows '{o.raw}'" for o in found if o.relation == "similar")
            elif "equivalent" in rels:
                status, detail = "equivalent", "Matches after normalisation"
            else:
                status, detail = "agrees", f"Matches {len(found)} document value(s)"
        out[key] = FieldComparison(key, label, form_value, status, detail, obs)

    changes: list[Change] = []
    doc_changes: list[DocChange] = []
    if inp.previous_form is not None:
        changes = field_changes(inp.previous_form, inp.form)
        prev = inp.previous_docs or {}
        now = {d.slot: d.sha256 for d in inp.docs}
        for slot in sorted(set(prev) | set(now)):
            if slot not in prev:
                doc_changes.append(DocChange(slot, "added"))
            elif slot not in now:
                doc_changes.append(DocChange(slot, "removed"))
            elif prev[slot] != now[slot]:
                doc_changes.append(DocChange(slot, "replaced"))
            else:
                doc_changes.append(DocChange(slot, "unchanged"))
    baseline_changes: list[Change] = []
    for area, base in inp.baselines.items():
        baseline_changes += [c for c in field_changes(base[0], inp.form) if c.area == area]
    return ComparisonResult(out, changes, doc_changes, baseline_changes,
                            {area: base[1] for area, base in inp.baselines.items()})
