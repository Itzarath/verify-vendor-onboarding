"""In-memory evaluation inputs. Pure data so the compare/check/decide logic is testable without a database."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from ..extraction.contract import DocumentReading, FieldObservation
from ..schemas.policy import BuyerPolicy
from ..schemas.submission import DOCUMENT_TYPES


@dataclass
class DocEvidence:
    slot: str  # what the vendor said it is
    document_id: int
    filename: str
    sha256: str
    layout: str  # digital | scanned | mixed | blank
    page_count: int
    status: str  # complete | partial | failed
    reading: DocumentReading | None = None
    error: dict | None = None
    provider: str | None = None
    model: str | None = None
    reused: bool = False
    extraction_id: int | None = None  # the exact stored reading this evaluation used
    # Embedded digital signatures (pdf/signature.py): {"signatures": [...], "changed_after_signing": bool}; None when
    # the file was not checked.
    signatures: dict | None = None

    @property
    def detected_type(self) -> str | None:
        return self.reading.detected_type if self.reading else None

    @property
    def effective_type(self) -> str:
        """Type used for comparisons: what the content says, falling back to the vendor's label if unread."""
        return self.detected_type or self.slot

    def obs(self, key: str) -> FieldObservation | None:
        return self.reading.fields.get(key) if self.reading else None

    def label(self) -> str:
        return DOCUMENT_TYPES.get(self.slot, self.slot)


@dataclass
class ResolutionRec:
    check_key: str
    fingerprint: str
    outcome: str  # accepted | not_accepted
    note: str
    actor: str
    created_at: datetime | None = None
    revision_id: int | None = None  # the revision the judgement was made on
    vendor_text: str | None = None  # not_accepted: what the vendor is asked to fix (the note stays internal)
    confirmation: dict | None = None  # change:bank accepted: how finance confirmed the change independently


@dataclass
class VerificationRec:
    id: int
    identifier_type: str
    identifier_value: str
    method: str  # simulated_registry | manual_registry
    result: str  # found | not_found | unavailable
    observed: dict | None
    source: str
    actor_type: str
    created_at: datetime | None = None


@dataclass
class EvalInput:
    form: dict
    docs: list[DocEvidence]
    policy: BuyerPolicy
    revision_number: int = 1
    revision_id: int | None = None
    previous_form: dict | None = None
    previous_docs: dict[str, str] | None = None  # slot -> sha256 of the previous revision
    # What material changes are judged against, per area ("bank", "identity"): (form, label). The last state a
    # person cleared (finance for bank, a reviewer for identity), else the first submission / approved profile.
    # area -> (form, label[, documents {slot: sha256} when a person cleared that revision]): what change reviews compare with
    baselines: dict[str, tuple] = field(default_factory=dict)
    # (form, label) this request started from (first submission, or the approved profile for a change request): a bank
    # change finance already confirmed still counts for risk and the vendor record after unrelated corrections.
    bank_origin: tuple | None = None
    resolutions: list[ResolutionRec] = field(default_factory=list)
    verifications: list[VerificationRec] = field(default_factory=list)
    # Other vendor records of this buyer using the same bank account: [{"id", "reference"}] (crossfacts).
    duplicate_bank_vendors: list[dict] = field(default_factory=list)
    duplicate_identity_vendors: list[dict] = field(default_factory=list)  # same PAN or GSTIN (crossfacts)

    def docs_of_type(self, dtype: str) -> list[DocEvidence]:
        """Documents whose content is this type; the one uploaded in the matching slot first."""
        found = [d for d in self.docs if d.effective_type == dtype]
        return sorted(found, key=lambda d: 0 if d.slot == dtype else 1)

    def doc_in_slot(self, slot: str) -> DocEvidence | None:
        return next((d for d in self.docs if d.slot == slot), None)

    def resolution_for(self, check_key: str, fingerprint: str) -> ResolutionRec | None:
        matches = [r for r in self.resolutions if r.check_key == check_key and r.fingerprint == fingerprint]
        return matches[-1] if matches else None


def fingerprint(*values) -> str:
    raw = json.dumps(values, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
