"""Final approval of a request: one implementation for a reviewer's approval and for the system's automatic approval
of a clean request (policy setting approval.auto_approve_when_clean).

Runs inside the caller's transaction, with the locks held in the global order vendor -> request (-> run).

Who checks what:
- The caller: who may act (role and buyer scope), that the actor saw the current version, and, for the runner, that
  its run still holds the lease (status RUNNING and the same lease token). For an automatic approval the runner also
  applies the eligibility rule (runner.auto_approval_block).
- This function: everything about the request itself, checked again here so that no caller (a retried, racing or
  future one) can approve from a stale state: the current revision and vendor, the request is ready for a decision,
  the assessment is its current, ready, current-engine assessment under its pinned policy, nothing blocks or is open,
  the vendor's profile is as expected, there is no earlier approval, and the facts read from other requests
  (which other vendor records use the same bank account) are unchanged since that assessment.
"""

from __future__ import annotations

from sqlalchemy import func, select

from ..models import (AuditEvent, Assessment, CheckResult, Decision, Message, Request, RequestState, Revision, Task,
                      Vendor, VendorProfile, utcnow)
from .checks import ENGINE_VERSION
from .crossfacts import (DUPLICATE_IDENTITY_CHECK, duplicate_bank_vendors, duplicate_identity_vendors,
                         lock_bank_account, lock_identity, recorded_duplicate_bank_vendors)


SIMULATED = "simulated_registry"


def verification_methods(db, assessment_id: int) -> list[str]:
    """How the assessment's passed registry checks were made (e.g. simulated_registry, manual_registry)."""
    return sorted({(c.evidence or {}).get("method") for c in db.scalars(select(CheckResult).where(
        CheckResult.assessment_id == assessment_id, CheckResult.check_key.like("verification:%"),
        CheckResult.status == "satisfied")) if (c.evidence or {}).get("method")})


class CannotApprove(Exception):
    pass


class OutOfDate(CannotApprove):
    """Something the assessment relied on has changed since it was made: evaluate again before deciding."""


def approve_request(db, req: Request, vendor: Vendor, rev: Revision, assessment_id: int, *, actor_type: str,
                    actor_id: int | None, audit_actor_type: str, rule: str | None, reason: str,
                    message_body: str, message_items: list | None = None, audit_data: dict | None = None) -> Decision:
    _recheck(db, req, vendor, rev, assessment_id)
    # Whoever approves, the final decision and the vendor's message say when a registry check was only simulated, so
    # neither can be read as a real government verification (independent of any note the approver typed).
    if SIMULATED in verification_methods(db, assessment_id):
        if "SIMULATED" not in reason:
            reason += ". Registry lookup: SIMULATED test registry, not a real government check"
        if "simulated" not in message_body.lower():
            message_body += " In this demo the registry lookup is simulated, not a real government check."
    req.version += 1
    decision = Decision(request_id=req.id, revision_id=rev.id, assessment_id=assessment_id, policy_id=req.policy_id,
                        request_version=req.version, outcome="approved", actor_type=actor_type, actor_id=actor_id,
                        rule=rule, reason=reason)
    db.add(decision)
    db.flush()
    version = (db.scalar(select(func.max(VendorProfile.version)).where(VendorProfile.vendor_id == vendor.id)) or 0) + 1
    docs = {rd.slot: rd.document.sha256 for rd in rev.documents}
    a = db.get(Assessment, assessment_id)
    record = (a.summary or {}).get("vendor_master") if a else None
    data = {"form": rev.form, "documents": docs}
    if record:  # the ERP-ready vendor master record (pipeline/master.py), as approved
        data["vendor_master"] = {**record, "status": "approved", "approved_at": utcnow().isoformat(),
                                 "approved_by": "policy rule " + rule if rule else audit_actor_type}
    profile = VendorProfile(vendor_id=vendor.id, version=version, data=data,
                            request_id=req.id, revision_id=rev.id, decision_id=decision.id)
    db.add(profile)
    db.flush()
    vendor.current_profile_id = profile.id
    req.state, req.closed_at = RequestState.APPROVED, utcnow()
    db.add(Message(request_id=req.id, vendor_id=vendor.id, kind="approved", subject="Approved — welcome aboard",
                   body=message_body, items=message_items or None, assessment_id=assessment_id))
    db.add(AuditEvent(request_id=req.id, vendor_id=vendor.id, event_type="REQUEST_APPROVED", actor_type=audit_actor_type,
                      actor_id=actor_id, data={"revision": rev.number, "assessment_id": assessment_id,
                                               "profile_version": version, "rule": rule, **(audit_data or {})}))
    return decision


def _recheck(db, req: Request, vendor: Vendor, rev: Revision, assessment_id: int) -> None:
    if vendor.id != req.vendor_id or req.current_revision_id != rev.id:
        raise CannotApprove("not the request's current revision")
    if req.state != RequestState.READY_FOR_DECISION:
        raise CannotApprove(f"the application is {req.state.replace('_', ' ').lower()}")
    a = db.get(Assessment, assessment_id)
    if a is None or a.id != req.current_assessment_id or a.superseded or a.revision_id != rev.id \
            or a.outcome != "ready" or a.policy_id != req.policy_id:
        raise CannotApprove("that is not the request's current, ready assessment")
    if (a.summary or {}).get("engine_version") != ENGINE_VERSION:
        raise CannotApprove("the assessment was made under an earlier version of the checks")
    if db.scalar(select(func.count()).select_from(CheckResult).where(
            CheckResult.assessment_id == a.id, CheckResult.blocking.is_(True), CheckResult.status != "satisfied")) \
            or db.scalar(select(func.count()).select_from(Task).where(Task.request_id == req.id, Task.status == "open")):
        raise CannotApprove("required checks are not all satisfied")
    expected_profile = req.base_profile_id if req.kind == "change" else None
    if vendor.current_profile_id != expected_profile:
        raise CannotApprove("the vendor's approved profile is not what this request was based on")
    if db.scalar(select(Decision.id).where(Decision.request_id == req.id, Decision.outcome == "approved").limit(1)):
        raise CannotApprove("this request already has an approval")
    assert_cross_facts_current(db, req, rev, a.id)


def assert_cross_facts_current(db, req: Request, rev: Revision, assessment_id: int) -> None:
    """The facts read from other requests (vendor records using the same bank account) are unchanged since the
    assessment; else OutOfDate. Also run before a first sign-off, so no person signs off on stale facts."""
    recorded = recorded_duplicate_bank_vendors(db, assessment_id)
    if recorded is not None:
        lock_bank_account(db, req.buyer_id, rev.form)
        if [o["id"] for o in duplicate_bank_vendors(db, req.buyer_id, req.vendor_id, rev.form)] != recorded:
            raise OutOfDate("the other vendor records using this bank account changed since this assessment (another "
                            "application with the same account was submitted, corrected or closed)")
    recorded = recorded_duplicate_bank_vendors(db, assessment_id, DUPLICATE_IDENTITY_CHECK)
    if recorded is not None:
        lock_identity(db, req.buyer_id, rev.form)
        if [o["id"] for o in duplicate_identity_vendors(db, req.buyer_id, req.vendor_id, rev.form)] != recorded:
            raise OutOfDate("the other vendor records with this PAN or GSTIN changed since this assessment (another "
                            "application with the same PAN or GSTIN was submitted, corrected or closed)")
