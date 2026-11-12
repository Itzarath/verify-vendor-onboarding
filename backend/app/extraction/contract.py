"""Extraction contract shared by every model provider.

The model returns `ModelReading` (portable, flat JSON). Our code then
validates it and produces `DocumentReading`, adding what the model must not
decide for itself: normalised values, page-range checks and provenance
(whether the quoted text actually appears in the page's native text layer).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..pdf.inspect import HIDDEN_MARK
from .templates import DETECTABLE_TYPES, IDENTIFIER_FIELDS, TEMPLATES

FieldStatus = Literal["found", "not_found", "unreadable", "ambiguous"]


# ---- what the model returns (keep it simple so any OpenAI-compatible provider can produce it)

class ModelField(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str
    status: FieldStatus
    value: str | None = None
    alternatives: list[str] = Field(default_factory=list)
    page: int | None = None
    quote: str | None = None


class ModelReading(BaseModel):
    model_config = ConfigDict(extra="ignore")
    document_type: str
    document_type_reason: str = ""
    fields: list[ModelField] = Field(default_factory=list)
    quality_issues: list[str] = Field(default_factory=list)


# ---- what we store after validation

Provenance = Literal["text_layer_verified", "visual_unconfirmed", "quote_not_in_text", "inferred", "none"]

# Classifications the model infers rather than copies (not checkable against the text layer).
INFERRED_FIELDS = {"proof_kind", "entity_kind"}


class FieldObservation(BaseModel):
    key: str
    status: FieldStatus
    value: str | None = None  # raw, as printed
    normalized: str | None = None  # computed by our code
    alternatives: list[str] = Field(default_factory=list)
    page: int | None = None
    quote: str | None = None
    provenance: Provenance = "none"
    critical: bool = False


class DocumentReading(BaseModel):
    detected_type: str
    type_reason: str = ""
    fields: dict[str, FieldObservation] = Field(default_factory=dict)
    quality_issues: list[str] = Field(default_factory=list)
    validation_notes: list[str] = Field(default_factory=list)

    def value(self, key: str) -> str | None:
        f = self.fields.get(key)
        return f.normalized if f and f.status == "found" else None

    @model_validator(mode="after")
    def _conflicting_alternatives_are_ambiguous(self):
        """A value marked found while the reply also offers a DIFFERENT candidate is not settled: keep it ambiguous
        with every candidate, never pick one. Runs on new readings and on cached ones when they are loaded, so a
        stored reading gets the same treatment without being read again."""
        for key, f in list(self.fields.items()):
            if f.status != "found" or not f.alternatives:
                continue
            seen, cands = set(), []
            for raw in [f.value, *f.alternatives]:
                n = normalise_value(key, raw)
                if n and n not in seen:
                    seen.add(n)
                    cands.append(raw.strip())
            if len(cands) > 1:
                self.fields[key] = f.model_copy(update={"status": "ambiguous", "value": None, "normalized": None,
                                                        "provenance": "none", "alternatives": cands[:4]})
                self.validation_notes = [*self.validation_notes,
                                         f"field '{key}' was marked found but the reply also offered a different value; "
                                         "marked ambiguous"]
        return self


# Bump when validation rules change: stored readings are cached per contract version.
CONTRACT_VERSION = "contract-v5"


def _squash(s: str) -> str:
    return re.sub(r"[\s\-]+", "", s or "").upper()


def _in_text(value: str | None, page_text: str) -> bool:
    """Does the page's visible text contain this value as a whole token sequence?

    Spaces and hyphens may differ ('ABCDE 1234 F' == 'ABCDE1234F'), but the value may not be a fragment of a longer
    identifier or word: '1234' is not found inside '512345', and '0012345678' is not found inside '9900-0012345678-1122'
    (hyphen-joined groups are one identifier).
    """
    chars = [c for c in (value or "").upper() if not c.isspace() and c != "-"]
    if not chars or HIDDEN_MARK in chars:
        return False
    # HIDDEN_MARK (letters or digits present in the file but not visible) counts like a letter or digit at the edges
    # and is never skipped between characters: no value is grounded across hidden characters or right next to them.
    edge = "A-Z0-9" + HIDDEN_MARK
    pattern = (rf"(?<![{edge}])(?<![{edge}]-)" + r"[\s\-]*".join(re.escape(c) for c in chars)
               + rf"(?![{edge}])(?!-[{edge}])")
    return re.search(pattern, page_text.upper()) is not None


def reground(reading: DocumentReading, page_texts: list[str]) -> tuple[DocumentReading, list[str]]:
    """Check a stored reading's text-layer claims against the page text as it is extracted today, without a model
    call (e.g. a reading stored before hidden characters were marked). A value that its page no longer contains loses
    'text_layer_verified'; nothing is ever upgraded. Returns (reading, keys downgraded)."""
    changed = [k for k, f in reading.fields.items()
               if f.provenance == "text_layer_verified" and f.page and 1 <= f.page <= len(page_texts)
               and not _in_text(f.value, page_texts[f.page - 1])]
    if not changed:
        return reading, []
    fields = {k: (f.model_copy(update={"provenance": "quote_not_in_text"}) if k in changed else f)
              for k, f in reading.fields.items()}
    note = f"re-checked against the visible text: {', '.join(changed)} no longer found there, so not verified"
    return reading.model_copy(update={"fields": fields, "validation_notes": [*reading.validation_notes, note]}), changed


def normalise_value(key: str, raw: str | None) -> str | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw or raw in {"-", "—", "NA", "N/A", "null"}:
        return None
    if key in IDENTIFIER_FIELDS:
        v = re.sub(r"\s+", "", raw).upper()
        if key == "account_number":
            v = v.replace("-", "")
        return v  # strings: leading zeros are preserved
    return re.sub(r"\s+", " ", raw)


def _distinct_alternatives(key: str, value: str | None, alternatives: list[str]) -> list[str]:
    """Alternatives that differ from the value and from each other (after normalisation), THEN at most 4 kept.

    Trimming first could drop the one candidate that conflicts (e.g. four spellings of the same IFSC, then a different
    one), and the reading would look settled."""
    seen = {n for n in [normalise_value(key, value)] if n}
    out = []
    for raw in alternatives:
        n = normalise_value(key, raw)
        if n and n not in seen:
            seen.add(n)
            out.append(raw.strip())
    return out[:4]


def validate_reading(model: ModelReading, page_kinds: list[str], page_texts: list[str]) -> DocumentReading:
    """Turn an untrusted model reading into a validated DocumentReading."""
    notes: list[str] = []
    dtype = model.document_type if model.document_type in DETECTABLE_TYPES else "other"
    if dtype != model.document_type:
        notes.append(f"model returned unknown document type '{model.document_type}', treated as 'other'")
    template = TEMPLATES.get(dtype, {"fields": {}, "critical": []})
    out: dict[str, FieldObservation] = {}
    for f in model.fields:
        if f.key not in template["fields"]:
            notes.append(f"ignored field '{f.key}' (not in the {dtype} template)")
            continue
        if f.key in out:
            prev = out[f.key]
            # Every candidate value either observation offered, deduplicated after normalisation, in order.
            seen, cands = set(), []
            for raw in [prev.value, *prev.alternatives, f.value if f.status in ("found", "ambiguous") else None,
                        *f.alternatives]:
                n = normalise_value(f.key, raw)
                if n and n not in seen:
                    seen.add(n)
                    cands.append(raw.strip())
            if prev.status == f.status == "found" and len(cands) <= 1:
                notes.append(f"duplicate field '{f.key}' ignored (same value)")
            elif prev.status == f.status == "not_found":
                notes.append(f"duplicate field '{f.key}' ignored (not found both times)")
            else:
                # Two observations disagree (different values, or one says ambiguous / not found / unreadable): keep
                # it ambiguous with every candidate, never pick one.
                out[f.key] = prev.model_copy(update={"status": "ambiguous", "value": None, "normalized": None,
                                                     "provenance": "none", "alternatives": cands[:4]})
                notes.append(f"field '{f.key}' returned more than once and the observations disagree; marked ambiguous")
            continue
        page = f.page if f.page and 1 <= f.page <= len(page_kinds) else None
        if f.page and page is None:
            notes.append(f"field '{f.key}' cited page {f.page}, which does not exist")
        status = f.status
        value = f.value if status in ("found", "ambiguous") else None
        if status == "found" and not normalise_value(f.key, value):
            status = "not_found"
            notes.append(f"field '{f.key}' marked found but empty")
        provenance: Provenance = "none"
        if status == "found" and f.key in INFERRED_FIELDS:
            provenance = "inferred"
        elif status == "found":
            if page is None:
                # find the page whose visible text contains the value
                for i, t in enumerate(page_texts):
                    if page_kinds[i] == "text" and _in_text(value, t):
                        page = i + 1
                        break
                if page is None and len(page_kinds) == 1 and page_kinds[0] != "text":
                    page = 1  # a one-page scan: the value can only come from its image
            if page is not None and page_kinds[page - 1] == "text":
                provenance = "text_layer_verified" if _in_text(value, page_texts[page - 1]) else "quote_not_in_text"
            elif page is not None:
                provenance = "visual_unconfirmed"
        out[f.key] = FieldObservation(
            key=f.key, status=status, value=value, normalized=normalise_value(f.key, value) if status == "found" else None,
            alternatives=_distinct_alternatives(f.key, value, f.alternatives), page=page, quote=(f.quote or None),
            provenance=provenance, critical=f.key in template["critical"],
        )
    for key in template["fields"]:
        if key not in out:
            out[key] = FieldObservation(key=key, status="not_found", critical=key in template["critical"])
    return DocumentReading(detected_type=dtype, type_reason=model.document_type_reason, fields=out,
                           quality_issues=model.quality_issues, validation_notes=notes)
