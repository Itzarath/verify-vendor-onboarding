"""Persistence model.

Key separations (see docs/ARCHITECTURE.md):
- Vendor (buyer-scoped identity) vs approved VendorProfile snapshots.
- Request (has the workflow state) vs immutable Revision packets.
- Evidence (Extraction) vs CheckResult (a rule's conclusion) vs Task (who must act).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, deferred, mapped_column, relationship, validates

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


TS = DateTime(timezone=True)


class RequestState:
    PROCESSING = "PROCESSING"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    READY_FOR_DECISION = "READY_FOR_DECISION"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    OPEN = (PROCESSING, ACTION_REQUIRED, READY_FOR_DECISION)
    PUBLIC = {PROCESSING: "pending", ACTION_REQUIRED: "pending", READY_FOR_DECISION: "pending",
              APPROVED: "approved", REJECTED: "rejected"}


class RunStatus:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


class Buyer(Base):
    __tablename__ = "buyers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    active_policy_id: Mapped[int | None] = mapped_column(ForeignKey("policies.id", use_alter=True), nullable=True)


class Policy(Base):
    """Immutable buyer policy version."""
    __tablename__ = "policies"
    __table_args__ = (UniqueConstraint("buyer_id", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("buyers.id"))
    policy_key: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # vendor | reviewer
    buyer_id: Mapped[int] = mapped_column(ForeignKey("buyers.id"))
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("buyers.id"), index=True)
    reference: Mapped[str] = mapped_column(String(40), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    current_profile_id: Mapped[int | None] = mapped_column(ForeignKey("vendor_profiles.id", use_alter=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class VendorProfile(Base):
    """Snapshot of approved vendor details. Never edited in place."""
    __tablename__ = "vendor_profiles"
    __table_args__ = (UniqueConstraint("vendor_id", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"))
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSONB)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"))
    revision_id: Mapped[int] = mapped_column(ForeignKey("revisions.id"))
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"), nullable=True)
    approved_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Request(Base):
    __tablename__ = "requests"
    __table_args__ = (
        # At most one open request per vendor.
        Index("uq_one_open_request_per_vendor", "vendor_id", unique=True,
              postgresql_where=text("state IN ('PROCESSING','ACTION_REQUIRED','READY_FOR_DECISION')")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reference: Mapped[str] = mapped_column(String(40), unique=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("buyers.id"), index=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="onboarding")  # onboarding | change
    state: Mapped[str] = mapped_column(String(30), default=RequestState.PROCESSING)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policies.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)  # bumps on any decision-relevant change
    current_revision_id: Mapped[int | None] = mapped_column(ForeignKey("revisions.id", use_alter=True), nullable=True)
    current_assessment_id: Mapped[int | None] = mapped_column(ForeignKey("assessments.id", use_alter=True), nullable=True)
    base_profile_id: Mapped[int | None] = mapped_column(ForeignKey("vendor_profiles.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)

    vendor: Mapped[Vendor] = relationship()
    policy: Mapped[Policy] = relationship()


class Revision(Base):
    """One immutable submitted packet within a request."""
    __tablename__ = "revisions"
    __table_args__ = (UniqueConstraint("request_id", "number"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    form: Mapped[dict] = mapped_column(JSONB)
    previous_revision_id: Mapped[int | None] = mapped_column(ForeignKey("revisions.id"), nullable=True)
    vendor_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    submitted_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    # Canonical 'ACCOUNT|IFSC' of the form's bank account (crossfacts.bank_key), set whenever the row is written: other
    # requests are matched on this stored key, never by re-cleaning raw input in SQL.
    bank_account_key: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)

    documents: Mapped[list["RevisionDocument"]] = relationship(order_by="RevisionDocument.id")


@event.listens_for(Revision, "before_insert")
@event.listens_for(Revision, "before_update")
def _set_bank_account_key(_mapper, _conn, rev: Revision) -> None:
    from .pipeline.crossfacts import bank_key  # imported here: crossfacts imports these models

    rev.bank_account_key = bank_key(rev.form)


class Document(Base):
    """Uploaded file, content-addressed per vendor. Bytes are private and served via authorised endpoints."""
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("vendor_id", "sha256"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int] = mapped_column(Integer)
    inspection: Mapped[dict] = mapped_column(JSONB)  # per-page text/scan classification
    content: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class RevisionDocument(Base):
    __tablename__ = "revision_documents"
    __table_args__ = (UniqueConstraint("revision_id", "slot"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("revisions.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    slot: Mapped[str] = mapped_column(String(40))  # vendor's label for the upload

    document: Mapped[Document] = relationship()


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_status_queued", "status", "queued_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("revisions.id"))
    policy_id: Mapped[int] = mapped_column(ForeignKey("policies.id"))
    trigger: Mapped[str] = mapped_column(String(30))  # submission | correction | retry | review_evidence | verification
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.QUEUED)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class RunEvent(Base):
    """Live progress of a run; drives the stage-by-stage run view."""
    __tablename__ = "run_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(30))  # receive | read | compare | check | decide | communicate
    status: Mapped[str] = mapped_column(String(20))  # started | info | completed | warning | failed
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    @validates("title")
    def _fits(self, _key, value: str) -> str:
        """A log line longer than the column must never fail a run: shorten it instead."""
        return value if value is None or len(value) <= 300 else value[:299] + "…"


class Extraction(Base):
    """What one document says, read independently of the form. Cached by (document hash, config)."""
    __tablename__ = "extractions"
    __table_args__ = (Index("ix_extraction_cache", "document_id", "config_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    config_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))  # complete | partial | failed
    detected_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    result: Mapped[dict] = mapped_column(JSONB)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Set when this row reuses an earlier reading of identical bytes (another upload of the same file).
    reused_from_id: Mapped[int | None] = mapped_column(ForeignKey("extractions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Assessment(Base):
    """One evaluation of one revision under one policy, with the inputs it considered."""
    __tablename__ = "assessments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("revisions.id"))
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    policy_id: Mapped[int] = mapped_column(ForeignKey("policies.id"))
    outcome: Mapped[str] = mapped_column(String(30))  # ready | action_required | auto_reject
    comparison: Mapped[dict] = mapped_column(JSONB)
    summary: Mapped[dict] = mapped_column(JSONB)
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    checks: Mapped[list["CheckResult"]] = relationship(order_by="CheckResult.id")


class CheckResult(Base):
    __tablename__ = "check_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id"), index=True)
    check_key: Mapped[str] = mapped_column(String(80))
    group: Mapped[str] = mapped_column(String(30))  # completeness | format | consistency | verification | change | declaration
    status: Mapped[str] = mapped_column(String(20))  # satisfied | not_satisfied | needs_review | unable | not_applicable | info
    blocking: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str] = mapped_column(String(300))
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    action: Mapped[str | None] = mapped_column(String(40), nullable=True)  # request_information | review | retry | reject
    owner: Mapped[str | None] = mapped_column(String(20), nullable=True)  # vendor | reviewer | finance | system
    task_key: Mapped[str | None] = mapped_column(String(120), nullable=True)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    assessment_id: Mapped[int | None] = mapped_column(ForeignKey("assessments.id"), nullable=True)
    task_key: Mapped[str] = mapped_column(String(120))
    owner: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | resolved | superseded
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolution: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class ReviewResolution(Base):
    """A reviewer's judgement on a needs-review finding, bound to the evidence it concerned.

    A resolution only counts while its fingerprint still matches the current evidence
    (e.g. a bank review stops counting once account/IFSC/holder/proof change).
    """
    __tablename__ = "review_resolutions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("revisions.id"))
    check_key: Mapped[str] = mapped_column(String(80))
    fingerprint: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(20))  # accepted | not_accepted
    note: Mapped[str] = mapped_column(Text)  # the reviewer's reasoning: internal, never shown to the vendor
    vendor_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # not_accepted: what the vendor is asked to fix
    # change:bank accepted: how finance confirmed the change independently (commands.bank_change_confirmation)
    confirmation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Verification(Base):
    """Independent (registry) verification result, bound to the exact identifier checked."""
    __tablename__ = "verifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("revisions.id"))
    identifier_type: Mapped[str] = mapped_column(String(20))  # GSTIN | CIN | LLPIN
    identifier_value: Mapped[str] = mapped_column(String(40))
    method: Mapped[str] = mapped_column(String(30))  # simulated_registry | manual_registry
    result: Mapped[str] = mapped_column(String(20))  # matched | mismatch | not_found | unavailable
    observed: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[str] = mapped_column(String(200))
    actor_type: Mapped[str] = mapped_column(String(20))  # system | reviewer
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("revisions.id"))
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id"))
    policy_id: Mapped[int] = mapped_column(ForeignKey("policies.id"))
    request_version: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(20))  # approved | rejected
    actor_type: Mapped[str] = mapped_column(String(20))  # system | reviewer
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    rule: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Message(Base):
    """Portal message actually delivered to the vendor (or an internal note)."""
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id"), index=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    audience: Mapped[str] = mapped_column(String(20), default="vendor")
    kind: Mapped[str] = mapped_column(String(40))  # received | action_required | under_review | approved | rejected
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    items: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    assessment_id: Mapped[int | None] = mapped_column(ForeignKey("assessments.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class AuditEvent(Base):
    """Append-only record of committed events (REQUEST_SUBMITTED, REQUEST_APPROVED, ...)."""
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[int | None] = mapped_column(ForeignKey("requests.id"), nullable=True, index=True)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(60))
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Operation(Base):
    """Idempotency record: same actor + action + operation id returns the first result."""
    __tablename__ = "operations"
    __table_args__ = (UniqueConstraint("actor_id", "action", "operation_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(40))
    operation_id: Mapped[str] = mapped_column(String(80))
    payload_sha: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
