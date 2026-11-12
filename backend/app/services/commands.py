"""Commands (requests to act) with their guards. Events are recorded only after a command succeeds.

Every command: server-derived actor, buyer/vendor scoping, expected-version check where
the action depends on what the actor saw, and an operation id for idempotency.

Lock order (everywhere, including the worker): vendor -> request -> run. The idempotency
record is checked again after the locks are held, so two simultaneous deliveries of the same
operation return the same receipt instead of a server error.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..india.validators import clean_identifier
from ..models import (
    Assessment,
    AuditEvent,
    Buyer,
    CheckResult,
    Decision,
    Document,
    Extraction,
    Message,
    Operation,
    Policy,
    Request,
    RequestState,
    ReviewResolution,
    Revision,
    RevisionDocument,
    Run,
    RunStatus,
    Task,
    User,
    Vendor,
    VendorProfile,
    Verification,
    utcnow,
)
from ..pdf.inspect import IntakeError, inspect_pdf
from ..extraction.service import get_reader
from ..pipeline.finalise import CannotApprove, OutOfDate, approve_request, assert_cross_facts_current
from ..pipeline.crossfacts import (DUPLICATE_BANK_CHECK, DUPLICATE_IDENTITY_CHECK, duplicate_bank_vendors,
                                   duplicate_identity_vendors, lock_bank_account, lock_identity)
from ..pipeline import risk as risk_mod
from ..pipeline.runner import REREAD_REQUESTED, assessment_is_current, enqueue_run
from ..schemas.policy import BuyerPolicy
from ..pipeline.worker import nudge
from ..schemas.submission import DOCUMENT_TYPES, SubmissionForm


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}


# ---------------------------------------------------------------- idempotency

def _sha(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def replay(db: Session, user: User, action: str, operation_id: str | None, payload) -> dict | None:
    if not operation_id:
        raise ApiError(400, "operation_id_required", "operation_id is required")
    op = db.scalar(select(Operation).where(Operation.actor_id == user.id, Operation.action == action,
                                           Operation.operation_id == operation_id))
    if op is None:
        return None
    if op.payload_sha != _sha(payload):
        raise ApiError(409, "operation_reused", "This operation id was already used for a different request")
    return {**op.response, "replayed": True}


def remember(db: Session, user: User, action: str, operation_id: str, payload, response: dict) -> dict:
    db.add(Operation(actor_id=user.id, action=action, operation_id=operation_id, payload_sha=_sha(payload), response=response))
    return response


def audit(db, req: Request | None, event: str, user: User | None, **data):
    db.add(AuditEvent(request_id=req.id if req else None, vendor_id=req.vendor_id if req else (user.vendor_id if user else None),
                      event_type=event, actor_type="system" if user is None else user.role,
                      actor_id=user.id if user else None, data=data or None))


# ---------------------------------------------------------------- vendor: upload + submit

def upload_document(db: Session, user: User, filename: str, data: bytes) -> Document:
    s = get_settings()
    try:
        info = inspect_pdf(data, s.max_file_bytes, s.max_pages_per_file)
    except IntakeError as e:
        raise ApiError(400, e.code, e.message)
    existing = db.scalar(select(Document).where(Document.vendor_id == user.vendor_id, Document.sha256 == info.sha256))
    if existing:
        return existing
    stored = db.scalar(select(func.coalesce(func.sum(Document.size_bytes), 0))) or 0
    if stored + len(data) > s.max_total_upload_mb * 1024 * 1024:
        raise ApiError(507, "storage_budget", "This demo has reached its document storage budget. Please contact the "
                                              "demo owner.")
    doc = Document(vendor_id=user.vendor_id, sha256=info.sha256, filename=(filename or "document.pdf")[:255],
                   content_type="application/pdf", size_bytes=len(data), page_count=info.page_count,
                   inspection=info.to_dict(), content=data, uploaded_by=user.id)
    try:
        with db.begin_nested():  # a savepoint: losing the race below undoes only this insert
            db.add(doc)
            db.flush()
    except IntegrityError:
        # The same vendor uploaded the same bytes at the same moment (e.g. a sample loaded in two tabs): the other
        # upload won. Reuse its document, as the lookup above would have a moment later.
        existing = db.scalar(select(Document).where(Document.vendor_id == user.vendor_id, Document.sha256 == info.sha256))
        if existing is None:
            raise
        return existing
    return doc


def _reference(db, req: Request) -> str:
    return f"VOB-{datetime.now(timezone.utc):%Y}-{req.id:04d}"


def submit(db: Session, user: User, body: dict) -> dict:
    """Create a request (first submission) or a new immutable revision (correction)."""
    payload = {k: body.get(k) for k in ("form", "documents", "based_on_revision", "vendor_note")}
    if (r := replay(db, user, "submit", body.get("operation_id"), payload)) is not None:
        return r
    try:
        form = SubmissionForm.model_validate(body.get("form") or {}).model_dump()
    except ValidationError as exc:
        raise ApiError(422, "invalid_form", "The form has invalid values", {"errors": json.loads(exc.json())[:10]})
    s = get_settings()
    slots: dict = body.get("documents") or {}
    if len(slots) > s.max_files_per_revision:
        raise ApiError(400, "too_many_files", f"At most {s.max_files_per_revision} files per submission")
    docs: dict[str, Document] = {}
    for slot, doc_id in slots.items():
        if slot not in DOCUMENT_TYPES:
            raise ApiError(400, "unknown_slot", f"Unknown document slot '{slot}'")
        d = db.get(Document, int(doc_id))
        if d is None or d.vendor_id != user.vendor_id:  # never reveal whether another vendor's id exists
            raise ApiError(404, "document_not_found", "Document not found")
        docs[slot] = d
    if sum(d.page_count for d in docs.values()) > s.max_pages_per_revision:
        raise ApiError(400, "too_many_pages", f"At most {s.max_pages_per_revision} pages per submission")

    vendor = db.execute(select(Vendor).where(Vendor.id == user.vendor_id).with_for_update(key_share=True)).scalar_one()
    if (r := replay(db, user, "submit", body.get("operation_id"), payload)) is not None:
        return r  # a simultaneous delivery of the same operation committed while we waited for the lock
    open_req = db.scalar(select(Request).where(Request.vendor_id == vendor.id, Request.state.in_(RequestState.OPEN))
                         .with_for_update(key_share=True))
    if open_req:
        cur = db.get(Revision, open_req.current_revision_id)
        based_on = body.get("based_on_revision")
        if based_on is None:
            raise ApiError(409, "correction_needs_base",
                           f"You already have an open application ({open_req.reference}, revision {cur.number}). "
                           "Open it and submit a correction from there.", {"current_revision": cur.number})
        if int(based_on) != cur.number:
            raise ApiError(409, "stale_revision",
                           f"Your application was updated (now revision {cur.number}) since you started this correction. "
                           "Reload to see the latest version; your edits are kept in this form.",
                           {"current_revision": cur.number})
        rev = Revision(request_id=open_req.id, number=cur.number + 1, form=form, previous_revision_id=cur.id,
                       vendor_note=body.get("vendor_note"), submitted_by=user.id)
        req, trigger, event = open_req, "correction", "REVISION_SUBMITTED"
    else:
        last = db.scalar(select(Request).where(Request.vendor_id == vendor.id).order_by(Request.id.desc()).limit(1))
        if last and last.state == RequestState.REJECTED:
            raise ApiError(409, "request_closed", "This application was rejected. If you believe it should be "
                                                  "reconsidered, please contact the buyer's procurement team.")
        if body.get("based_on_revision") is not None:
            raise ApiError(409, "nothing_to_correct", "There is no open application to correct.")
        if vendor.current_profile_id:
            # Post-approval change requests are not enabled in this prototype (their path is not yet tested end to end).
            raise ApiError(409, "changes_not_available", "Your supplier profile is already approved. Changes to an approved "
                                                         "profile aren't available in this demo; please contact the buyer's "
                                                         "procurement team.")
        buyer = db.get(Buyer, vendor.buyer_id)
        kind = "onboarding"
        req = Request(reference="pending", buyer_id=vendor.buyer_id, vendor_id=vendor.id, kind=kind,
                      policy_id=buyer.active_policy_id, base_profile_id=vendor.current_profile_id)
        db.add(req)
        db.flush()
        req.reference = _reference(db, req)
        rev = Revision(request_id=req.id, number=1, form=form, vendor_note=body.get("vendor_note"), submitted_by=user.id)
        trigger, event = ("change" if kind == "change" else "submission"), ("CHANGE_REQUESTED" if kind == "change" else "REQUEST_SUBMITTED")
    # Serialise with any publish or approval that relies on how many vendors use this bank account, PAN or GSTIN
    # (crossfacts).
    lock_bank_account(db, req.buyer_id, form)
    lock_identity(db, req.buyer_id, form)
    db.add(rev)
    db.flush()
    for slot, d in docs.items():
        db.add(RevisionDocument(revision_id=rev.id, document_id=d.id, slot=slot))
    req.current_revision_id = rev.id
    if form["business"].get("legal_name"):
        vendor.display_name = form["business"]["legal_name"]
    run = enqueue_run(db, req, rev.id, trigger)
    db.add(Message(request_id=req.id, vendor_id=vendor.id, kind="received",
                   subject=f"Received — revision {rev.number} of {req.reference}",
                   body=f"We've received your {'correction' if rev.number > 1 else 'application'} with {len(docs)} document(s). "
                        "Automated checks are running now; you can watch progress on this page."))
    audit(db, req, event, user, revision=rev.number, run_id=run.id, documents=sorted(docs))
    resp = {"request_id": req.id, "reference": req.reference, "revision": rev.number, "run_id": run.id}
    remember(db, user, "submit", body["operation_id"], payload, resp)
    db.commit()
    nudge()
    return resp


# ---------------------------------------------------------------- reviewer actions

def _load_for_review(db: Session, user: User, request_id: int) -> Request:
    """Lock vendor then request (the global lock order), scoped to the reviewer's buyer."""
    head = db.execute(select(Request.vendor_id, Request.buyer_id).where(Request.id == request_id)).first()
    if head is None or head.buyer_id != user.buyer_id:
        raise ApiError(404, "not_found", "Request not found")
    db.execute(select(Vendor.id).where(Vendor.id == head.vendor_id).with_for_update(key_share=True))
    return db.execute(select(Request).where(Request.id == request_id).with_for_update(key_share=True)).scalar_one()


def _locked_replay(db: Session, user: User, action: str, body: dict, payload) -> dict | None:
    """Second idempotency check, made while holding the locks."""
    return replay(db, user, action, body.get("operation_id"), payload)


def _guard_version(req: Request, expected) -> None:
    if expected is None or int(expected) != req.version:
        raise ApiError(409, "stale_view",
                       "This application changed while you were looking at it. Reload to review the latest evidence "
                       "before acting.", {"current_version": req.version, "you_saw": expected})


def approve(db: Session, user: User, request_id: int, body: dict) -> dict:
    payload = {k: body.get(k) for k in ("expected_version", "assessment_id", "note")}
    if (r := replay(db, user, f"approve:{request_id}", body.get("operation_id"), payload)) is not None:
        return r
    req = _load_for_review(db, user, request_id)
    if (r := _locked_replay(db, user, f"approve:{request_id}", body, payload)) is not None:
        return r
    _guard_version(req, body.get("expected_version"))
    if req.state != RequestState.READY_FOR_DECISION:
        raise ApiError(409, "not_ready", f"Cannot approve: the application is {req.state.replace('_', ' ').lower()}")
    a = db.get(Assessment, req.current_assessment_id) if req.current_assessment_id else None
    if a is None or a.id != body.get("assessment_id") or a.superseded or a.revision_id != req.current_revision_id \
            or a.outcome != "ready":
        raise ApiError(409, "stale_assessment", "The assessment you reviewed is no longer current. Reload first.")
    if not assessment_is_current(a):
        raise ApiError(409, "outdated_assessment", "This assessment was made under an earlier version of the checks. "
                                                   "Re-evaluate it with the current checks before deciding.")
    blocking = db.scalar(select(func.count()).select_from(CheckResult).where(
        CheckResult.assessment_id == a.id, CheckResult.blocking.is_(True), CheckResult.status != "satisfied"))
    open_tasks = db.scalar(select(func.count()).select_from(Task).where(Task.request_id == req.id, Task.status == "open"))
    if blocking or open_tasks:
        raise ApiError(409, "blocked", "Required checks are not all satisfied")
    # Segregation of duties: whoever confirmed changed bank details doesn't also approve the vendor who changed them.
    if db.scalar(select(ReviewResolution.id).where(ReviewResolution.request_id == req.id,
                                                   ReviewResolution.check_key == BANK_CHANGE_CHECK,
                                                   ReviewResolution.outcome == "accepted",
                                                   ReviewResolution.actor_id == user.id).limit(1)):
        raise ApiError(409, "bank_change_same_person", "You confirmed the bank change on this application, so a "
                                                       "different person must give the final approval.")
    rev = db.get(Revision, req.current_revision_id)
    vendor = db.get(Vendor, req.vendor_id)  # already locked by _load_for_review
    if req.kind == "change" and vendor.current_profile_id != req.base_profile_id:
        raise ApiError(409, "stale_profile", "The vendor's approved profile changed since this update was started; re-evaluate first.")
    # Four eyes on risky suppliers: from the policy's risk tier on, two different people must approve this assessment.
    risk = (a.summary or {}).get("risk") or {}
    need = BuyerPolicy.model_validate(db.get(Policy, req.policy_id).document).approval.second_approver_from_risk
    second = ""
    try:
        if need != "never" and risk.get("tier") and risk_mod.ORDER[risk["tier"]] >= risk_mod.ORDER[need]:
            first = db.scalar(select(AuditEvent).where(AuditEvent.request_id == req.id,
                                                       AuditEvent.event_type == "APPROVAL_FIRST_SIGNOFF")
                              .order_by(AuditEvent.id.desc()).limit(1))
            if first is None or (first.data or {}).get("assessment_id") != a.id:
                # Same freshness check as the final approval: no one signs off on facts that changed since.
                assert_cross_facts_current(db, req, rev, a.id)
                audit(db, req, "APPROVAL_FIRST_SIGNOFF", user, assessment_id=a.id, risk_tier=risk["tier"],
                      risk_score=risk.get("score"), note=body.get("note"))
                req.version += 1
                resp = {"state": req.state, "version": req.version, "awaiting_second_approval": True}
                remember(db, user, f"approve:{request_id}", body["operation_id"], payload, resp)
                db.commit()
                return resp
            if first.actor_id == user.id:
                raise ApiError(409, "second_approver_required", f"This supplier's risk is {risk['tier']} "
                                                                f"({risk.get('score')}/100): a second, different person "
                                                                "must approve. You already gave the first approval.")
            first_name = db.get(User, first.actor_id).display_name if first.actor_id else "a reviewer"
            second = (f" Second approval (four eyes, {risk['tier']} risk {risk.get('score')}/100); first approval by "
                      f"{first_name}" + (f": {(first.data or {}).get('note')}" if (first.data or {}).get("note") else "")
                      + ".")
        decision = approve_request(
            db, req, vendor, rev, a.id, actor_type="reviewer", actor_id=user.id, audit_actor_type=user.role, rule=None,
            reason=(body.get("note") or "All required checks satisfied") + second,
            message_body=f"Your application {req.reference} (revision {rev.number}) has been approved under "
                         f"{db.get(Buyer, req.buyer_id).name}'s onboarding policy. This approval covers vendor registration; "
                         "it is not bank verification, a purchase order or payment authorisation.")
    except OutOfDate as exc:
        # Nothing was written yet. Evaluate again with the current records, and tell the reviewer to look again.
        run = enqueue_run(db, req, req.current_revision_id, "recheck")
        audit(db, req, "REEVALUATION_QUEUED", user, run_id=run.id, reason=str(exc))
        db.commit()
        nudge()
        raise ApiError(409, "re_evaluating", f"Not approved: {exc}. The application is being checked again with the "
                                             "current records; review the new result before deciding.")
    except CannotApprove as exc:
        raise ApiError(409, "not_approvable", f"Cannot approve: {exc}")
    resp = {"state": req.state, "version": req.version, "decision_id": decision.id}
    remember(db, user, f"approve:{request_id}", body["operation_id"], payload, resp)
    db.commit()
    return resp


def reject(db: Session, user: User, request_id: int, body: dict) -> dict:
    payload = {k: body.get(k) for k in ("expected_version", "reason", "vendor_explanation")}
    if (r := replay(db, user, f"reject:{request_id}", body.get("operation_id"), payload)) is not None:
        return r
    req = _load_for_review(db, user, request_id)
    if (r := _locked_replay(db, user, f"reject:{request_id}", body, payload)) is not None:
        return r
    _guard_version(req, body.get("expected_version"))
    if req.state not in (RequestState.ACTION_REQUIRED, RequestState.READY_FOR_DECISION):
        raise ApiError(409, "not_rejectable", f"Cannot reject while the application is {req.state.replace('_', ' ').lower()}")
    a = db.get(Assessment, req.current_assessment_id) if req.current_assessment_id else None
    if a is None or a.revision_id != req.current_revision_id:
        raise ApiError(409, "no_assessment", "This revision has no completed evaluation yet, so there is no evidence to "
                                             "base a decision on. Retry the evaluation first.")
    reason, explanation = (body.get("reason") or "").strip(), (body.get("vendor_explanation") or "").strip()
    if len(reason) < 10 or len(explanation) < 10:
        raise ApiError(400, "reason_required", "A recorded reason and a vendor-facing explanation are required")
    req.version += 1
    db.add(Decision(request_id=req.id, revision_id=req.current_revision_id, assessment_id=a.id,
                    policy_id=req.policy_id, request_version=req.version, outcome="rejected", actor_type="reviewer",
                    actor_id=user.id, reason=reason))
    for t in db.scalars(select(Task).where(Task.request_id == req.id, Task.status == "open")):
        t.status, t.resolved_at = "superseded", utcnow()
    req.state, req.closed_at = RequestState.REJECTED, utcnow()
    db.add(Message(request_id=req.id, vendor_id=req.vendor_id, kind="rejected", subject="Your application was not accepted",
                   body=explanation))
    audit(db, req, "REQUEST_REJECTED", user, reason=reason)
    resp = {"state": req.state, "version": req.version}
    remember(db, user, f"reject:{request_id}", body["operation_id"], payload, resp)
    db.commit()
    return resp


BANK_CHANGE_CHECK = "change:bank"
# How finance confirmed a bank change, and where the contact came from. Only contacts the buyer already held before
# this change count: a number or address taken from the request itself could belong to whoever sent it.
CONFIRM_METHODS = {"call_back": "call-back", "email_on_file": "e-mail to an address on file", "in_person": "in person"}
CONTACT_SOURCES = {
    "approved_record": "contact on the vendor's approved record, from before this change",
    "contract": "contact in the signed contract or earlier purchase orders",
    "independent_lookup": "the company's number, looked up independently (not from this application)",
}


def bank_change_confirmation(c) -> dict:
    """Finance's record of an independent confirmation of changed bank details, or 400."""
    c = c if isinstance(c, dict) else {}
    method, source = c.get("method"), c.get("contact_source")
    person = str(c.get("contact_person") or "").strip()
    if method not in CONFIRM_METHODS or source not in CONTACT_SOURCES or len(person) < 2 or c.get("confirmed") is not True:
        raise ApiError(400, "confirmation_required",
                       "Before accepting a bank change, record how you confirmed it with the vendor: the method, a contact "
                       "the buyer already held (never one from this application), who confirmed it, and that they "
                       "confirmed the account number and IFSC.")
    return {"method": method, "method_label": CONFIRM_METHODS[method], "contact_source": source,
            "contact_source_label": CONTACT_SOURCES[source], "contact_person": person, "confirmed": True}


def resolve_review(db: Session, user: User, request_id: int, body: dict) -> dict:
    """Record a reviewer's judgement on a needs-review finding, bound to its evidence fingerprint, then re-evaluate."""
    payload = {k: body.get(k) for k in ("expected_version", "check_key", "fingerprint", "outcome", "note", "vendor_text",
                                        "confirmation")}
    if (r := replay(db, user, f"resolve:{request_id}", body.get("operation_id"), payload)) is not None:
        return r
    req = _load_for_review(db, user, request_id)
    if (r := _locked_replay(db, user, f"resolve:{request_id}", body, payload)) is not None:
        return r
    _guard_version(req, body.get("expected_version"))
    if req.state != RequestState.ACTION_REQUIRED:
        raise ApiError(409, "not_open", "Nothing to review in the current state")
    check = db.scalar(select(CheckResult).where(CheckResult.assessment_id == req.current_assessment_id,
                                                CheckResult.check_key == body.get("check_key")))
    if check is None or check.status != "needs_review":
        raise ApiError(409, "not_reviewable", "That finding is not awaiting review in the current assessment")
    if check.owner == "finance" and user.role != "finance":
        raise ApiError(403, "finance_only", "Bank-change reviews must be completed by a finance reviewer")
    if check.owner != "finance" and user.role not in ("reviewer", "finance"):
        raise ApiError(403, "reviewer_only", "Only reviewers can record review decisions")
    fp = (check.evidence or {}).get("fingerprint")
    if fp != body.get("fingerprint"):
        raise ApiError(409, "stale_evidence", "The evidence for this finding changed; reload before deciding")
    if check.check_key in (DUPLICATE_BANK_CHECK, DUPLICATE_IDENTITY_CHECK):
        # A judgement on shared details covers exactly the other records shown. If another application with this
        # account (or PAN / GSTIN) arrived or closed since, say so now instead of recording a decision the next check
        # can't use.
        rev = db.get(Revision, req.current_revision_id)
        bank = check.check_key == DUPLICATE_BANK_CHECK
        (lock_bank_account if bank else lock_identity)(db, req.buyer_id, rev.form)
        now = [o["id"] for o in (duplicate_bank_vendors if bank else duplicate_identity_vendors)(
            db, req.buyer_id, req.vendor_id, rev.form)]
        if now != sorted((check.evidence or {}).get("other_vendor_ids") or []):
            what = "this bank account" if bank else "this PAN or GSTIN"
            run = enqueue_run(db, req, req.current_revision_id, "recheck")
            audit(db, req, "REEVALUATION_QUEUED", user, run_id=run.id, reason=f"vendor records with {what} changed")
            db.commit()
            nudge()
            raise ApiError(409, "re_evaluating", f"Not recorded: the other vendor records with {what} changed "
                                                 "since this page was loaded. The application is being checked again; "
                                                 "decide on the updated list.")
    outcome = body.get("outcome")
    note = (body.get("note") or "").strip()
    if outcome not in ("accepted", "not_accepted") or len(note) < 5:
        raise ApiError(400, "invalid_resolution", "Choose accept / not accept and give a short reason")
    # The note is the reviewer's reasoning and stays internal; only vendor_text is shown to the vendor.
    vendor_text = (body.get("vendor_text") or "").strip() if outcome == "not_accepted" else ""
    confirmation = None
    if check.check_key == BANK_CHANGE_CHECK and outcome == "accepted":
        confirmation = bank_change_confirmation(body.get("confirmation"))
    db.add(ReviewResolution(request_id=req.id, revision_id=req.current_revision_id, check_key=check.check_key,
                            fingerprint=fp, outcome=outcome, note=note, vendor_text=vendor_text or None,
                            confirmation=confirmation, actor_id=user.id))
    run = enqueue_run(db, req, req.current_revision_id, "review_evidence")
    audit(db, req, "REVIEW_RECORDED", user, check=check.check_key, outcome=outcome)
    resp = {"state": req.state, "version": req.version, "run_id": run.id}
    remember(db, user, f"resolve:{request_id}", body["operation_id"], payload, resp)
    db.commit()
    nudge()
    return resp


def record_verification(db: Session, user: User, request_id: int, body: dict) -> dict:
    """Reviewer records what the official registry showed (method: manual_registry)."""
    payload = {k: body.get(k) for k in ("expected_version", "identifier", "result", "legal_name", "status", "source",
                                        "taxpayer_type", "gstr3b_missed", "gstr1_missed")}
    if (r := replay(db, user, f"verify:{request_id}", body.get("operation_id"), payload)) is not None:
        return r
    req = _load_for_review(db, user, request_id)
    if (r := _locked_replay(db, user, f"verify:{request_id}", body, payload)) is not None:
        return r
    _guard_version(req, body.get("expected_version"))
    if req.state not in (RequestState.ACTION_REQUIRED, RequestState.READY_FOR_DECISION):
        raise ApiError(409, "not_open", "The application is not open for verification")
    rev = db.get(Revision, req.current_revision_id)
    gstin = clean_identifier((rev.form.get("tax") or {}).get("gstin"))
    if clean_identifier(body.get("identifier")) != gstin:
        raise ApiError(409, "identifier_mismatch", f"The lookup must be for the current GSTIN ({gstin})")
    result = body.get("result")
    if result not in ("found", "not_found", "unavailable"):
        raise ApiError(400, "invalid_result", "result must be found, not_found or unavailable")
    observed = None
    if result == "found":
        legal_name = (body.get("legal_name") or "").strip()
        status = (body.get("status") or "").strip().lower()
        if len(legal_name) < 3 or status not in ("active", "cancelled", "suspended", "inactive"):
            raise ApiError(400, "incomplete_result", "For a found registration, record the legal name and the status "
                                                     "exactly as the registry shows them (Active, Cancelled, Suspended or "
                                                     "Inactive).")
        observed = {"legal_name": legal_name, "status": status.capitalize()}
        # Optional, as the portal's taxpayer search shows them: the taxpayer type and the return-filing table.
        tt = (body.get("taxpayer_type") or "").strip()
        if tt:
            if tt.lower() not in ("regular", "composition", "casual", "sez unit", "sez developer", "isd", "tds", "tcs"):
                raise ApiError(400, "invalid_taxpayer_type", "Record the taxpayer type as the portal shows it (for example "
                                                             "Regular or Composition).")
            observed["taxpayer_type"] = tt.title() if tt.lower() not in ("isd", "tds", "tcs") else tt.upper()
        for k in ("gstr3b_missed", "gstr1_missed"):
            if body.get(k) not in (None, ""):
                try:
                    n = int(body[k])
                except (TypeError, ValueError):
                    n = -1
                if not 0 <= n <= 12:
                    raise ApiError(400, "invalid_filing_count", "Months not filed must be a number from 0 to 12.")
                observed[k] = n
        # The two counts come from the same filing table: one alone would either hide known gaps or claim "all filed".
        if ("gstr3b_missed" in observed) != ("gstr1_missed" in observed):
            raise ApiError(400, "incomplete_filing_counts", "Record the months not filed for both GSTR-3B and GSTR-1 "
                                                            "(0 if all were filed), or leave both blank.")
    db.add(Verification(request_id=req.id, revision_id=rev.id, identifier_type="GSTIN", identifier_value=gstin,
                        method="manual_registry", result=result, observed=observed,
                        source=(body.get("source") or "Official GST portal — taxpayer search")[:200],
                        actor_type="reviewer", actor_id=user.id))
    run = enqueue_run(db, req, rev.id, "verification")
    audit(db, req, "VERIFICATION_RECORDED", user, identifier=gstin, result=result, method="manual_registry")
    resp = {"state": req.state, "version": req.version, "run_id": run.id}
    remember(db, user, f"verify:{request_id}", body["operation_id"], payload, resp)
    db.commit()
    nudge()
    return resp


def retry(db: Session, user: User, request_id: int, body: dict) -> dict:
    payload = {"expected_version": body.get("expected_version")}
    if (r := replay(db, user, f"retry:{request_id}", body.get("operation_id"), payload)) is not None:
        return r
    req = _load_for_review(db, user, request_id)
    if (r := _locked_replay(db, user, f"retry:{request_id}", body, payload)) is not None:
        return r
    _guard_version(req, body.get("expected_version"))
    a = db.get(Assessment, req.current_assessment_id) if req.current_assessment_id else None
    outdated = (req.state in (RequestState.ACTION_REQUIRED, RequestState.READY_FOR_DECISION) and a is not None
                and a.revision_id == req.current_revision_id and not assessment_is_current(a))
    sys_tasks = db.scalars(select(Task).where(Task.request_id == req.id, Task.status == "open", Task.owner == "system")).all()
    if not outdated:
        if req.state != RequestState.ACTION_REQUIRED:
            raise ApiError(409, "not_retryable", "Retry is only available when processing needs attention")
        if not sys_tasks:
            raise ApiError(409, "not_retryable", "There is no processing problem to retry")
    for t in sys_tasks:
        if t.task_key == "system:evaluation":
            t.status, t.resolved_at = "resolved", utcnow()
    run = enqueue_run(db, req, req.current_revision_id, "upgrade" if outdated else "retry")
    audit(db, req, "RETRY_REQUESTED", user, run_id=run.id)
    resp = {"state": req.state, "version": req.version, "run_id": run.id}
    remember(db, user, f"retry:{request_id}", body["operation_id"], payload, resp)
    db.commit()
    nudge()
    return resp


def reread(db: Session, user: User, request_id: int, body: dict) -> dict:
    """A reviewer asks for ONE document to be read again instead of reusing its stored reading.

    Only for an incomplete reading (key information not found), e.g. one stored before replies had to answer every key
    field: re-uploading the same file would reuse the same reading forever. Costs one model call; never automatic.
    The request is kept as a marker in the reading history and in the audit trail."""
    payload = {k: body.get(k) for k in ("expected_version", "slot")}
    if (r := replay(db, user, f"reread:{request_id}", body.get("operation_id"), payload)) is not None:
        return r
    req = _load_for_review(db, user, request_id)
    if (r := _locked_replay(db, user, f"reread:{request_id}", body, payload)) is not None:
        return r
    _guard_version(req, body.get("expected_version"))
    if req.state not in (RequestState.ACTION_REQUIRED, RequestState.READY_FOR_DECISION):
        raise ApiError(409, "not_open", "Documents can only be re-read while the application is open and not processing")
    a = db.get(Assessment, req.current_assessment_id) if req.current_assessment_id else None
    if a is None or a.revision_id != req.current_revision_id:
        raise ApiError(409, "no_assessment", "This revision has no completed evaluation yet; retry the evaluation first")
    slot = body.get("slot")
    entry = ((a.summary or {}).get("documents") or {}).get(slot)
    rev = db.get(Revision, req.current_revision_id)
    rd = next((x for x in rev.documents if x.slot == slot), None)
    if entry is None or rd is None:
        raise ApiError(404, "unknown_slot", "No such document in the current revision")
    if entry.get("status") != "partial":
        raise ApiError(409, "not_incomplete", "Only a reading with key information missing can be re-read")
    reader = get_reader()
    db.add(Extraction(document_id=rd.document_id, config_key=reader.config_key, status=REREAD_REQUESTED, result={},
                      error={"kind": "reread_requested", "by": user.display_name,
                             "replaces_extraction_id": entry.get("extraction_id")}))
    run = enqueue_run(db, req, rev.id, "reread")
    audit(db, req, "READING_REREAD_REQUESTED", user, slot=slot, document_id=rd.document_id,
          previous_extraction_id=entry.get("extraction_id"), run_id=run.id)
    resp = {"state": req.state, "version": req.version, "run_id": run.id}
    remember(db, user, f"reread:{request_id}", body["operation_id"], payload, resp)
    db.commit()
    nudge()
    return resp
