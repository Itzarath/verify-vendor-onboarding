"""Read models for the UI. Every function is scoped by the caller's buyer / vendor."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Assessment,
    AuditEvent,
    Buyer,
    CheckResult,
    Decision,
    Document,
    Extraction,
    Message,
    Policy,
    Request,
    RequestState,
    ReviewResolution,
    Revision,
    Run,
    RunEvent,
    RunStatus,
    Task,
    User,
    Vendor,
    VendorProfile,
    Verification,
)
from ..schemas.submission import DOCUMENT_TYPES
from .commands import ApiError

STATE_LABEL = {
    RequestState.PROCESSING: "Processing",
    RequestState.ACTION_REQUIRED: "Action required",
    RequestState.READY_FOR_DECISION: "Ready for decision",
    RequestState.APPROVED: "Approved",
    RequestState.REJECTED: "Rejected",
}


def iso(dt):
    return dt.isoformat() if dt else None


def _user_names(db: Session, ids) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: u.display_name for u in db.scalars(select(User).where(User.id.in_(ids)))}


def run_dict(run: Run, for_vendor: bool = False) -> dict:
    dur = (run.finished_at - run.started_at).total_seconds() if run.finished_at and run.started_at else None
    error = run.error
    if for_vendor and error:
        error = {"message": "Processing problem on our side; the buyer's team can retry it."}  # no internal traces
    elif error:
        error = {k: v for k, v in error.items() if k != "trace"}  # stack traces stay in the server log
    return {"id": run.id, "revision_id": run.revision_id, "trigger": run.trigger, "status": run.status, "attempt": run.attempt,
            "queued_at": iso(run.queued_at), "started_at": iso(run.started_at), "finished_at": iso(run.finished_at),
            "duration_s": round(dur, 1) if dur is not None else None, "error": error}


def run_events(db: Session, run_id: int, after: int = 0) -> list[dict]:
    rows = db.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.id > after).order_by(RunEvent.id))
    return [{"id": e.id, "stage": e.stage, "status": e.status, "title": e.title, "detail": e.detail, "at": iso(e.at)} for e in rows]


def request_summary(db: Session, req: Request) -> dict:
    tasks = db.scalars(select(Task).where(Task.request_id == req.id, Task.status == "open")).all()
    rev = db.get(Revision, req.current_revision_id) if req.current_revision_id else None
    last_run = db.scalar(select(Run).where(Run.request_id == req.id).order_by(Run.id.desc()).limit(1))
    owners = sorted({t.owner for t in tasks})
    a = db.get(Assessment, req.current_assessment_id) if req.current_assessment_id else None
    risk = (a.summary or {}).get("risk") if a else None
    return {
        "id": req.id, "reference": req.reference, "kind": req.kind, "state": req.state, "state_label": STATE_LABEL[req.state],
        "public_status": RequestState.PUBLIC[req.state], "version": req.version, "vendor_id": req.vendor_id,
        "vendor_name": db.get(Vendor, req.vendor_id).display_name, "revision": rev.number if rev else None,
        "waiting_on": owners, "open_tasks": len(tasks), "created_at": iso(req.created_at), "updated_at": iso(req.updated_at),
        "last_run": run_dict(last_run) if last_run else None,
        "high_severity": any("gst_identity" in t.task_key or "bank_holder" in t.task_key or "duplicate" in t.task_key for t in tasks),
        "risk": {"tier": risk.get("tier"), "score": risk.get("score")} if risk else None,
    }


def dashboard(db: Session, user: User) -> dict:
    reqs = db.scalars(select(Request).where(Request.buyer_id == user.buyer_id).order_by(Request.updated_at.desc())).all()
    items = [request_summary(db, r) for r in reqs]
    counts = {s: 0 for s in STATE_LABEL}
    for r in reqs:
        counts[r.state] += 1
    runs = db.scalars(select(Run).join(Request, Request.id == Run.request_id).where(Request.buyer_id == user.buyer_id)
                      .order_by(Run.id.desc()).limit(40)).all()
    finished = [r for r in runs if r.status == RunStatus.SUCCEEDED and r.started_at and r.finished_at]
    avg = sum((r.finished_at - r.started_at).total_seconds() for r in finished) / len(finished) if finished else None
    ref_by_id = {r.id: (r.reference, db.get(Vendor, r.vendor_id).display_name) for r in reqs}
    run_rows = []
    for r in runs:
        a = db.scalar(select(Assessment).where(Assessment.run_id == r.id))
        run_rows.append({**run_dict(r), "request_id": r.request_id, "reference": ref_by_id.get(r.request_id, ("?", "?"))[0],
                         "vendor_name": ref_by_id.get(r.request_id, ("?", "?"))[1],
                         "outcome": ("auto_approved" if (a.summary or {}).get("auto_approved") else a.outcome) if a else None, "revision": db.get(Revision, r.revision_id).number})
    waiting_me = [i for i in items if i["state"] in (RequestState.ACTION_REQUIRED, RequestState.READY_FOR_DECISION)
                  and (i["state"] == RequestState.READY_FOR_DECISION or {"reviewer", "finance"} & set(i["waiting_on"]))]
    buyer = db.get(Buyer, user.buyer_id)
    pol = db.get(Policy, buyer.active_policy_id)
    return {"buyer": {"name": buyer.name, "policy": f"{pol.document['label']} (v{pol.version})"},
            "counts": counts, "total": len(reqs), "needs_my_attention": len(waiting_me),
            "avg_processing_s": round(avg, 1) if avg else None, "requests": items, "runs": run_rows}


def _assessment_dict(db: Session, a: Assessment | None) -> dict | None:
    if a is None:
        return None
    checks = db.scalars(select(CheckResult).where(CheckResult.assessment_id == a.id).order_by(CheckResult.id)).all()
    from ..pipeline.runner import assessment_is_current

    return {"id": a.id, "revision_id": a.revision_id, "run_id": a.run_id, "outcome": a.outcome, "superseded": a.superseded,
            "engine_current": assessment_is_current(a),
            "summary": a.summary, "comparison": a.comparison, "created_at": iso(a.created_at),
            "checks": [{"key": c.check_key, "group": c.group, "status": c.status, "blocking": c.blocking, "title": c.title,
                        "reason": c.reason, "evidence": c.evidence, "action": c.action, "owner": c.owner,
                        "task_key": c.task_key} for c in checks]}


def _readings_for(db: Session, rev: Revision) -> tuple[Assessment | None, dict[str, tuple[Extraction, bool]]]:
    """Per SLOT, the reading the latest assessment of this revision used (never just 'the newest extraction').

    Returns slot -> (extraction, legacy). Assessments made before readings were linked to them (schema 0001) carry no
    extraction ids. For those, the latest complete reading of the same file made before the assessment is shown,
    flagged as a legacy association, so older evidence stays visible without being presented as an exact link.
    """
    a = db.scalar(select(Assessment).where(Assessment.revision_id == rev.id).order_by(Assessment.id.desc()).limit(1))
    out: dict[str, tuple[Extraction, bool]] = {}
    if a is None:
        return None, out
    docs = {rd.slot: rd.document for rd in rev.documents}
    for slot, entry in ((a.summary or {}).get("documents") or {}).items():
        doc = docs.get(slot)
        if doc is None:
            continue
        ex_id = entry.get("extraction_id")
        if ex_id:
            ex = db.get(Extraction, ex_id)
            if ex is not None and ex.document_id == doc.id:
                out[slot] = (ex, False)
        elif "extraction_id" not in entry:  # legacy summary
            ex = db.scalar(select(Extraction).join(Document, Document.id == Extraction.document_id)
                           .where(Document.sha256 == doc.sha256, Extraction.created_at <= a.created_at,
                                  Extraction.status.in_(["complete", "partial"]))
                           .order_by(Extraction.id.desc()).limit(1))
            if ex is not None:
                out[slot] = (ex, True)
    return a, out


def _revision_dict(db: Session, rev: Revision, with_readings: bool) -> dict:
    docs = []
    assessed, readings = _readings_for(db, rev) if with_readings else (None, {})
    for rd in rev.documents:
        d = rd.document
        item = {"slot": rd.slot, "slot_label": DOCUMENT_TYPES.get(rd.slot, rd.slot), "document_id": d.id,
                "filename": d.filename, "pages": d.page_count, "layout": d.inspection.get("layout"),
                "page_kinds": [p["kind"] for p in d.inspection["pages"]], "sha256": d.sha256[:12], "size_bytes": d.size_bytes,
                "hidden_text_chars": sum(p.get("hidden_chars", 0) for p in d.inspection["pages"])}
        found = readings.get(rd.slot)
        if found:
            ex, legacy = found
            item["reading"] = {"extraction_id": ex.id, "status": ex.status, "detected_type": ex.detected_type,
                               "provider": ex.provider, "model": ex.model, "latency_ms": ex.latency_ms, "result": ex.result,
                               "error": ex.error, "at": iso(ex.created_at), "reused_from_id": ex.reused_from_id,
                               "assessment_id": assessed.id if assessed else None, "legacy_link": legacy}
        docs.append(item)
    return {"id": rev.id, "number": rev.number, "form": rev.form, "submitted_at": iso(rev.submitted_at),
            "vendor_note": rev.vendor_note, "documents": docs,
            "assessed_by": assessed.id if assessed else None}


def case_detail(db: Session, user: User, request_id: int) -> dict:
    req = db.get(Request, request_id)
    if req is None or req.buyer_id != user.buyer_id:
        raise ApiError(404, "not_found", "Request not found")
    revs = db.scalars(select(Revision).where(Revision.request_id == req.id).order_by(Revision.number)).all()
    runs = db.scalars(select(Run).where(Run.request_id == req.id).order_by(Run.id.desc())).all()
    tasks = db.scalars(select(Task).where(Task.request_id == req.id).order_by(Task.id)).all()
    verifs = db.scalars(select(Verification).where(Verification.request_id == req.id).order_by(Verification.id)).all()
    decisions = db.scalars(select(Decision).where(Decision.request_id == req.id).order_by(Decision.id)).all()
    msgs = db.scalars(select(Message).where(Message.request_id == req.id).order_by(Message.id)).all()
    events = db.scalars(select(AuditEvent).where(AuditEvent.request_id == req.id).order_by(AuditEvent.id)).all()
    resols = db.scalars(select(ReviewResolution).where(ReviewResolution.request_id == req.id).order_by(ReviewResolution.id)).all()
    names = _user_names(db, [t.resolved_by for t in tasks] + [d.actor_id for d in decisions] + [e.actor_id for e in events]
                        + [r.actor_id for r in resols] + [v.actor_id for v in verifs])
    vendor = db.get(Vendor, req.vendor_id)
    pol = db.get(Policy, req.policy_id)
    profile = db.get(VendorProfile, vendor.current_profile_id) if vendor.current_profile_id else None
    current = _assessment_dict(db, db.get(Assessment, req.current_assessment_id)) if req.current_assessment_id else None
    first = next((e for e in reversed(events) if e.event_type == "APPROVAL_FIRST_SIGNOFF"), None)
    second_approval = None
    if current and first and (first.data or {}).get("assessment_id") == current["id"]:
        second_approval = {"first_by": names.get(first.actor_id), "first_by_id": first.actor_id,
                           "at": iso(first.created_at), "note": (first.data or {}).get("note")}
    from ..schemas.policy import BuyerPolicy

    rules = BuyerPolicy.model_validate(pol.document).approval
    return {
        "request": request_summary(db, req),
        "vendor": {"id": vendor.id, "reference": vendor.reference, "name": vendor.display_name,
                   "approved_profile_version": profile.version if profile else None},
        "policy": {"id": pol.id, "key": pol.policy_key, "version": pol.version, "label": pol.document["label"],
                   "verification": [{"identifier": v["identifier"], "accepted_methods": v["accepted_methods"],
                                     "required": v.get("required", True)} for v in pol.document.get("verification", [])],
                   "buyer_name": pol.document["buyer_name"], "is_test_policy": pol.document.get("is_test_policy")},
        "approval_rules": {"auto_approve_max_risk": rules.auto_approve_max_risk,
                           "second_approver_from_risk": rules.second_approver_from_risk},
        "second_approval": second_approval,
        "vendor_master": vendor_master_record(db, req, profile, current),
        "revisions": [_revision_dict(db, r, with_readings=True) for r in revs],
        "current_revision_id": req.current_revision_id,
        "assessment": current,
        "runs": [run_dict(r) for r in runs],
        "tasks": [{"id": t.id, "key": t.task_key, "owner": t.owner, "kind": t.kind, "status": t.status, "title": t.title,
                   "detail": t.detail, "created_at": iso(t.created_at), "resolved_at": iso(t.resolved_at),
                   "resolution": t.resolution, "resolved_by": names.get(t.resolved_by)} for t in tasks],
        "verifications": [{"id": v.id, "type": v.identifier_type, "identifier": v.identifier_value, "method": v.method,
                           "result": v.result,
                           "observed": v.observed, "source": v.source, "actor": names.get(v.actor_id, "system"),
                           "revision_id": v.revision_id, "at": iso(v.created_at)} for v in verifs],
        "resolutions": [{"check_key": r.check_key, "outcome": r.outcome, "note": r.note, "vendor_text": r.vendor_text,
                         "confirmation": r.confirmation,
                         "actor": names.get(r.actor_id),
                         "fingerprint": r.fingerprint, "revision_id": r.revision_id, "at": iso(r.created_at)} for r in resols],
        "decisions": [{"outcome": d.outcome, "actor_type": d.actor_type, "actor": names.get(d.actor_id, "system"),
                       "rule": d.rule, "reason": d.reason, "revision_id": d.revision_id, "assessment_id": d.assessment_id,
                       "at": iso(d.created_at)} for d in decisions],
        "messages": [{"kind": m.kind, "subject": m.subject, "body": m.body, "items": m.items, "at": iso(m.created_at)} for m in msgs],
        "audit": [{"type": e.event_type, "actor_type": e.actor_type, "actor": names.get(e.actor_id, e.actor_type),
                   "data": e.data, "at": iso(e.created_at)} for e in events],
    }


def vendor_master_record(db: Session, req: Request, profile, current: dict | None) -> dict | None:
    """The ERP-ready vendor master record (pipeline/master.py): as approved, else the current assessment's draft."""
    if profile is not None and profile.request_id == req.id and (profile.data or {}).get("vendor_master"):
        return profile.data["vendor_master"]
    draft = ((current or {}).get("summary") or {}).get("vendor_master")
    return {**draft, "status": "draft (not approved)"} if draft else None


def vendor_overview(db: Session, user: User) -> dict:
    vendor = db.get(Vendor, user.vendor_id)
    reqs = db.scalars(select(Request).where(Request.vendor_id == vendor.id).order_by(Request.id.desc())).all()
    current = reqs[0] if reqs else None
    out = {"vendor": {"id": vendor.id, "reference": vendor.reference, "name": vendor.display_name,
                      "has_approved_profile": bool(vendor.current_profile_id)},
           "buyer": db.get(Buyer, vendor.buyer_id).name, "requests": [], "current": None}
    for r in reqs:
        rev = db.get(Revision, r.current_revision_id) if r.current_revision_id else None
        out["requests"].append({"id": r.id, "reference": r.reference, "kind": r.kind, "public_status": RequestState.PUBLIC[r.state],
                                "state": r.state, "revision": rev.number if rev else None, "created_at": iso(r.created_at)})
    if current:
        revs = db.scalars(select(Revision).where(Revision.request_id == current.id).order_by(Revision.number)).all()
        tasks = db.scalars(select(Task).where(Task.request_id == current.id, Task.status == "open")).all()
        msgs = db.scalars(select(Message).where(Message.request_id == current.id, Message.audience == "vendor")
                          .order_by(Message.id.desc())).all()
        last_run = db.scalar(select(Run).where(Run.request_id == current.id).order_by(Run.id.desc()).limit(1))
        out["current"] = {
            "id": current.id, "reference": current.reference, "state": current.state,
            "public_status": RequestState.PUBLIC[current.state], "state_label": STATE_LABEL[current.state],
            "revision": revs[-1].number if revs else None, "can_correct": current.state in RequestState.OPEN,
            "vendor_tasks": [{"key": t.task_key, "title": t.title, "lines": (t.resolution or {}).get("vendor_lines", [])}
                             for t in tasks if t.owner == "vendor"],
            "internal_pending": sorted({t.owner for t in tasks if t.owner != "vendor"}),
            "messages": [{"kind": m.kind, "subject": m.subject, "body": m.body, "items": m.items, "at": iso(m.created_at)} for m in msgs],
            "revisions": [{"number": r.number, "submitted_at": iso(r.submitted_at),
                           "documents": [{"slot": rd.slot, "filename": rd.document.filename} for rd in r.documents]} for r in revs],
            "latest": _revision_dict(db, revs[-1], with_readings=False) if revs else None,
            "last_run": run_dict(last_run, for_vendor=True) if last_run else None,
        }
    return out
