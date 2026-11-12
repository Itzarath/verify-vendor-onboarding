"""Executes one evaluation run through the six stages and publishes the result.

Transaction discipline:
- Model calls and PDF work happen OUTSIDE database transactions.
- Progress events are committed as they happen (the live run view polls them).
- The result is published in ONE short transaction that locks the request and
  verifies the run is still current (same revision, same lease token). A run
  that lost that race is marked SUPERSEDED and its result is kept only as history.
"""

from __future__ import annotations

import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

from sqlalchemy import func, select, update

from ..config import get_settings
from ..db import session_scope
from ..extraction.contract import DocumentReading, reground
from ..extraction.service import get_reader
from ..india.validators import check_account_number, check_gstin, check_ifsc, clean_identifier
from ..pdf.inspect import IntakeError, layout_label, page_texts, pdf_signatures
from ..pdf.signature import READER_VERSION as SIGNATURE_READER
from ..models import (
    Assessment,
    Buyer,
    AuditEvent,
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
    Verification,
    VendorProfile,
    utcnow,
)
from ..schemas.policy import BuyerPolicy
from ..schemas.submission import DOCUMENT_TYPES
from ..verification import bank as bank_sources
from . import master as vendor_master
from . import registry
from . import risk as risk_mod
from .checks import ENGINE_VERSION, run_checks
from .compare import compare
from .crossfacts import duplicate_bank_vendors, duplicate_identity_vendors, lock_bank_account, lock_identity
from .decide import compose_vendor_message, route
from .evidence import DocEvidence, EvalInput, ResolutionRec, VerificationRec
from .finalise import approve_request

log = logging.getLogger("runner")
# Extraction status of a marker row: a reviewer asked for this file to be read again instead of reusing its reading.
REREAD_REQUESTED = "reread_requested"
STAGES = ["receive", "read", "compare", "check", "decide", "communicate"]


class Superseded(Exception):
    pass


def emit(run_id: int, stage: str, status: str, title: str, detail: dict | None = None) -> None:
    with session_scope() as db:
        db.add(RunEvent(run_id=run_id, stage=stage, status=status, title=title[:300], detail=detail))


def assessment_is_current(a: Assessment | None) -> bool:
    """Was this assessment made under the eligibility rules this code enforces?"""
    return a is not None and (a.summary or {}).get("engine_version") == ENGINE_VERSION


def reassess_outdated() -> int:
    """At startup: open cases whose latest assessment was made under older rules are evaluated again, so an
    eligibility granted by older (possibly weaker) checks never carries over an upgrade. History is kept."""
    with session_scope() as db:
        ids = db.scalars(select(Request.id).where(Request.state.in_([RequestState.ACTION_REQUIRED,
                                                                      RequestState.READY_FOR_DECISION]))).all()
    n = 0
    for request_id in ids:
        with session_scope() as db:
            vendor_id = db.scalar(select(Request.vendor_id).where(Request.id == request_id))
            db.execute(select(Vendor.id).where(Vendor.id == vendor_id).with_for_update(key_share=True))  # lock order: vendor -> request
            req = db.execute(select(Request).where(Request.id == request_id).with_for_update(key_share=True)).scalar_one()
            if req.state not in (RequestState.ACTION_REQUIRED, RequestState.READY_FOR_DECISION):
                continue
            a = db.get(Assessment, req.current_assessment_id) if req.current_assessment_id else None
            if a is None or a.revision_id != req.current_revision_id or assessment_is_current(a):
                continue  # current, or a failed evaluation (the retry task already covers it)
            run = enqueue_run(db, req, req.current_revision_id, "upgrade")
            db.add(AuditEvent(request_id=req.id, vendor_id=req.vendor_id, event_type="REEVALUATION_QUEUED",
                              actor_type="system", data={"run_id": run.id, "reason": "checks updated",
                                                         "from": (a.summary or {}).get("engine_version"),
                                                         "to": ENGINE_VERSION}))
            n += 1
    if n:
        log.info("queued %d re-evaluation(s) under %s", n, ENGINE_VERSION)
    return n


def enqueue_run(db, req: Request, revision_id: int, trigger: str) -> Run:
    """Queue a new evaluation; any queued/running run for this request becomes SUPERSEDED."""
    db.execute(update(Run).where(Run.request_id == req.id, Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
               .values(status=RunStatus.SUPERSEDED, finished_at=utcnow(), lease_token=None))
    run = Run(request_id=req.id, revision_id=revision_id, policy_id=req.policy_id, trigger=trigger)
    db.add(run)
    db.flush()
    req.state = RequestState.PROCESSING
    req.version += 1
    db.add(RunEvent(run_id=run.id, stage="receive", status="info", title="Queued for processing"))
    return run


def _is_current(db, run_id: int, token: str) -> bool:
    run = db.get(Run, run_id)
    return bool(run and run.status == RunStatus.RUNNING and run.lease_token == token)


def _check_current(run_id: int, token: str):
    with session_scope() as db:
        if not _is_current(db, run_id, token):
            raise Superseded()


class _Heartbeat(threading.Thread):
    """Extends the run lease while work is in progress, so a crashed worker's run is recoverable."""

    def __init__(self, run_id: int, token: str, lease_s: int):
        super().__init__(daemon=True)
        self.run_id, self.token, self.lease_s = run_id, token, lease_s
        self.stop = threading.Event()

    def run(self):
        while not self.stop.wait(max(5, self.lease_s // 3)):
            try:
                with session_scope() as db:
                    db.execute(update(Run).where(Run.id == self.run_id, Run.lease_token == self.token,
                                                 Run.status == RunStatus.RUNNING)
                               .values(lease_expires_at=func.now() + timedelta(seconds=self.lease_s)))
            except Exception:  # noqa: BLE001
                log.exception("heartbeat failed")


def _stored_reading(db, sha256: str, cfg: str, trigger: str) -> Extraction | None:
    """The stored reading a run uses for a file, or None when the file must be read now (a model call).

    Same bytes + same extraction settings => same reading (keyed by content hash, not by upload). A reviewer's re-read
    request is a marker row, kept for the audit trail, and it authorises ONE new reading: once any reading attempt
    follows the marker, the request has been carried out, whether that attempt succeeded or failed.
    - A usable reading made after the latest marker (or with no marker at all) is reused.
    - A marker with no attempt after it is carried out now: the file is read.
    - Otherwise the latest attempt failed. A failed reading is never treated as a result, so an ordinary run reads the
      file again. A re-read run covers only the files a reviewer asked for (one model call each, as the button says):
      it keeps the failed reading, and the ordinary retry reads that file.
    """
    same = (select(Extraction).join(Document, Document.id == Extraction.document_id)
            .where(Document.sha256 == sha256, Extraction.config_key == cfg))
    marker = db.scalar(same.where(Extraction.status == REREAD_REQUESTED).order_by(Extraction.id.desc()).limit(1))
    since = same.where(Extraction.id > marker.id) if marker else same
    usable = db.scalar(since.where(Extraction.status.in_(["complete", "partial"])).order_by(Extraction.id.desc()).limit(1))
    if usable is not None:
        return usable
    failed = db.scalar(since.where(Extraction.status == "failed").order_by(Extraction.id.desc()).limit(1))
    return failed if failed is not None and trigger == "reread" else None


def _doc_summary(slot: str, reading: DocumentReading | None, status: str, error: dict | None, layout: str) -> tuple[str, str]:
    label = DOCUMENT_TYPES.get(slot, slot)
    if status == "failed" or reading is None:
        return "failed", f"{label}: could not be read — {(error or {}).get('message', 'unknown error')}"
    found = [f for f in reading.fields.values() if f.status == "found"]
    issues = [f for f in reading.fields.values() if f.critical and f.status != "found"]
    visual = [f for f in found if f.provenance == "visual_unconfirmed"]
    kind = DOCUMENT_TYPES.get(reading.detected_type, "unrecognised document")
    mismatch = reading.detected_type != slot
    title = f"{label}: read as {kind.split(' (')[0]}; {len(found)}/{len(reading.fields)} fields"
    if visual:
        title += f", {len(visual)} from scanned image"
    if issues:
        title += f"; {', '.join(f.key.replace('_', ' ') + ' ' + f.status.replace('_', ' ') for f in issues)}"
    return ("warning" if (issues or mismatch) else "completed"), title


def _bank_lookups(req_id: int, form: dict) -> list[dict]:
    """Bank details verified at the source (verification/bank.py), once per branch and per account on a request.
    Looked up again only after an "unavailable" answer. Network calls: run outside any database transaction."""
    b = form.get("bank") or {}
    acct, ifsc = clean_identifier(b.get("account_number")), clean_identifier(b.get("ifsc"))
    if not check_ifsc(ifsc).ok:
        return []
    with session_scope() as db:
        latest = {(v.identifier_type, v.identifier_value): v.result for v in db.scalars(
            select(Verification).where(Verification.request_id == req_id,
                                       Verification.identifier_type.in_(["IFSC", "BANK_ACCOUNT"])).order_by(Verification.id))}
    out = []
    if latest.get(("IFSC", ifsc)) in (None, "unavailable"):
        a = bank_sources.lookup_ifsc(ifsc)
        out.append(dict(identifier_type="IFSC", identifier_value=ifsc, method=a.method, result=a.result,
                        observed=a.observed, source=a.source))
    key = f"{acct}|{ifsc}"
    if check_account_number(acct).ok and latest.get(("BANK_ACCOUNT", key)) in (None, "unavailable"):
        a = bank_sources.penny_drop(acct, ifsc)
        out.append(dict(identifier_type="BANK_ACCOUNT", identifier_value=key, method=a.method, result=a.result,
                        observed=a.observed, source=a.source))
    return out


def _bank_note(r: dict) -> str:
    o = r["observed"] or {}
    if r["identifier_type"] == "IFSC":
        where = ", ".join(x for x in (o.get("BANK"), bank_sources.branch_place(o)) if x)
        text = {"found": f"{where}", "not_found": "not in the directory", "unavailable": "directory unreachable"}[r["result"]]
        label = "SIMULATED demo directory" if r["method"] == "demo_bank_directory" else "live RBI directory"
        return f"Bank branch lookup ({label}) for {r['identifier_value']}: {text}"
    text = {"valid": f"the bank names the holder '{o.get('name_at_bank')}'", "invalid": "no such account at this branch",
            "no_record": "not in the sandbox (real accounts can't be verified in this demo)",
            "unavailable": "provider unavailable"}[r["result"]]
    return f"Bank account verification (SANDBOX penny drop, SIMULATED) for ••{r['identifier_value'].split('|')[0][-4:]}: {text}"


def execute_run(run_id: int, token: str) -> None:
    settings = get_settings()
    hb = _Heartbeat(run_id, token, settings.run_lease_s)
    hb.start()
    try:
        _execute(run_id, token)
    except Superseded:
        emit(run_id, "communicate", "info", "Stopped: a newer revision or re-evaluation superseded this run")
    except Exception as exc:  # noqa: BLE001 — a crash must never leave a spinner or reject a vendor
        log.exception("run %s failed", run_id)
        try:
            _fail_run(run_id, token, exc)
        except Exception:  # noqa: BLE001 - e.g. the database is down too: the run stays RUNNING, its lease expires
            log.exception("run %s: could not record the failure; it will be retried after its lease expires", run_id)
    finally:
        hb.stop.set()


def _fail_run(run_id: int, token: str, exc: Exception) -> None:
    with session_scope() as db:
        # Lock order everywhere: vendor -> request -> run. Read the run's request first without locking the run.
        request_id = db.scalar(select(Run.request_id).where(Run.id == run_id))
        if request_id is None:
            return
        req = db.execute(select(Request).where(Request.id == request_id).with_for_update(key_share=True)).scalar_one()
        run = db.execute(select(Run).where(Run.id == run_id).with_for_update(key_share=True)).scalar_one()
        if run.lease_token != token or run.status != RunStatus.RUNNING:
            return
        run.status, run.finished_at = RunStatus.FAILED, utcnow()
        run.error = {"type": type(exc).__name__, "message": str(exc)[:500], "trace": traceback.format_exc()[-1500:]}
        if req.current_revision_id == run.revision_id and req.state == RequestState.PROCESSING:
            req.state = RequestState.ACTION_REQUIRED
            req.version += 1
            existing = db.scalar(select(Task).where(Task.request_id == req.id, Task.task_key == "system:evaluation",
                                                    Task.status == "open"))
            if not existing:
                db.add(Task(request_id=req.id, task_key="system:evaluation", owner="system", kind="system_retry",
                            title="Evaluation failed — retry", detail=f"{type(exc).__name__}: {str(exc)[:300]}"))
            db.add(AuditEvent(request_id=req.id, vendor_id=req.vendor_id, event_type="EVALUATION_FAILED", actor_type="system",
                              data={"run_id": run_id, "error": type(exc).__name__}))
            # The vendor's last message said checks were running: replace that impression, in the same transaction
            # as the state change (once per failure: the request leaves PROCESSING only here or at a publish).
            rev_no = db.scalar(select(Revision.number).where(Revision.id == run.revision_id))
            db.add(Message(request_id=req.id, vendor_id=req.vendor_id, kind="under_review",
                           subject="Checks paused — our team will retry",
                           body=f"The automated checks on revision {rev_no} of {req.reference} stopped because of a "
                                "technical problem on our side. Nothing is wrong with your application because of this, "
                                "and no action is needed from you: our team has been asked to run the checks again."))
        db.add(RunEvent(run_id=run_id, stage="communicate", status="failed",
                        title=f"Processing error ({type(exc).__name__}). The application is not rejected; a retry is available."))


def _execute(run_id: int, token: str) -> None:
    settings = get_settings()
    reader = get_reader()
    # ------------------------------------------------------------------ Stage 1: receive
    with session_scope() as db:
        run = db.get(Run, run_id)
        req = db.get(Request, run.request_id)
        rev = db.get(Revision, run.revision_id)
        pol = db.get(Policy, run.policy_id)
        policy = BuyerPolicy.model_validate(pol.document)
        prev_rev = db.get(Revision, rev.previous_revision_id) if rev.previous_revision_id else None
        docs = [dict(slot=rd.slot, id=rd.document.id, filename=rd.document.filename, sha256=rd.document.sha256,
                     pages=rd.document.page_count, layout=rd.document.inspection.get("layout", "?"),
                     kinds=[p["kind"] for p in rd.document.inspection["pages"]],
                     hidden=sum(p.get("hidden_chars") or 0 for p in rd.document.inspection["pages"]),
                     signatures=rd.document.inspection.get("signatures")) for rd in rev.documents]
        prev_docs = {rd.slot: rd.document.sha256 for rd in prev_rev.documents} if prev_rev else None
        prev_form = prev_rev.form if prev_rev else None
        # Baselines for change reviews: what was last CLEARED by a person (finance for bank details, a reviewer for
        # identity), else the approved profile (change request) or this request's first submission. Comparing only
        # with the previous revision would let an unchanged later revision make an unreviewed change disappear.
        baselines: dict[str, tuple] = {}
        start = None
        if req.kind == "change" and req.base_profile_id:
            base = db.get(VendorProfile, req.base_profile_id)
            start = (base.data.get("form"), f"the approved profile (v{base.version})")
            if prev_rev is None:
                prev_form, prev_docs = base.data.get("form"), base.data.get("documents")
        elif rev.number > 1:
            first = db.scalar(select(Revision).where(Revision.request_id == req.id, Revision.number == 1))
            start = (first.form, "the first submission (revision 1)")
        if start is not None:
            for area, check_key, who in (("bank", "change:bank", "finance"), ("identity", "change:identity", "a reviewer")):
                q = select(ReviewResolution).where(
                    ReviewResolution.request_id == req.id, ReviewResolution.check_key == check_key,
                    ReviewResolution.outcome == "accepted")
                if area == "bank":
                    # Only a bank change finance confirmed independently counts as cleared. An acceptance from before
                    # that was required (migration 0005) leaves an open case to be confirmed again. And the baseline is
                    # the last state cleared on an EARLIER revision: a clearance of this revision was made against that
                    # same baseline (it is part of the confirmed fingerprint), so re-evaluating keeps matching it.
                    q = q.where(ReviewResolution.confirmation.is_not(None), ReviewResolution.revision_id != rev.id)
                cleared = db.scalar(q.order_by(ReviewResolution.id.desc()).limit(1))
                cleared_rev = db.get(Revision, cleared.revision_id) if cleared else None
                # A clearance covers that revision's form AND its files (finance reviewed that exact bank proof).
                baselines[area] = ((cleared_rev.form, f"revision {cleared_rev.number}, as cleared by {who}",
                                    {rd.slot: rd.document.sha256 for rd in cleared_rev.documents})
                                   if cleared_rev and cleared_rev.id != rev.id else start)
        form, rev_no, rev_id, ref, req_id = rev.form, rev.number, rev.id, req.reference, req.id
        trigger = run.trigger
        buyer_id, vendor_id = req.buyer_id, req.vendor_id
    what = {"submission": "first submission", "correction": f"correction (revision {rev_no})",
            "retry": "retry of the same revision", "review_evidence": "re-evaluation after a review decision",
            "verification": "re-evaluation after a registry result", "change": "change request",
            "upgrade": "re-evaluation under updated checks",
            "reread": "re-read of a document, requested by a reviewer",
            "recheck": "re-evaluation: another application changed a fact the checks use"}.get(trigger, trigger)
    emit(run_id, "receive", "started", f"{ref} revision {rev_no}: {what}",
         {"documents": [{k: d[k] for k in ('slot', 'filename', 'layout', 'pages')} for d in docs],
          "policy": f"{policy.policy_key} v{policy.version}"})
    if prev_docs is not None:
        now = {d["slot"]: d["sha256"] for d in docs}
        changed = [s for s in now if prev_docs.get(s) != now[s]]
        emit(run_id, "receive", "info", f"Compared with the previous version: {len(changed)} document(s) new or replaced, "
                                        f"{len(now) - len(changed)} unchanged", {"changed": changed})
    emit(run_id, "receive", "completed", f"Received {len(docs)} document(s) under policy '{policy.label}'")
    _check_current(run_id, token)

    # ------------------------------------------------------------------ Stage 2: read documents
    emit(run_id, "read", "started", f"Reading {len(docs)} document(s) — model: {reader.describe()}")
    cfg = reader.config_key
    evidence: dict[str, DocEvidence] = {}
    downgraded: dict[str, list[str]] = {}  # slot -> fields of a stored reading no longer verified in the visible text
    to_read = []
    for d in docs:
        if (d["signatures"] or {}).get("reader") != SIGNATURE_READER:
            # Uploaded before digital signatures were checked, or checked by an older reader: check once (in the PDF
            # sandbox) and keep the result.
            with session_scope() as db:
                doc = db.get(Document, d["id"])
                content = doc.content
            try:
                d["signatures"] = pdf_signatures(content)
            except IntakeError:
                continue  # not checkable within the safety limits: the authenticity check says it wasn't checked
            with session_scope() as db:
                doc = db.get(Document, d["id"])
                doc.inspection = {**doc.inspection, "signatures": d["signatures"]}
    with session_scope() as db:
        for d in docs:
            cached = _stored_reading(db, d["sha256"], cfg, trigger)
            if cached:
                failed = cached.status == "failed"
                reading = None if failed else DocumentReading.model_validate(cached.result)
                if reading is not None and d["hidden"]:
                    # The file has characters that aren't visible: check the stored reading's text-layer claims
                    # against today's visible text (no model call), so a reading stored under older rules can't
                    # keep a value verified across hidden characters.
                    reading, downgraded[d["slot"]] = reground(reading, page_texts(db.get(Document, d["id"]).content))
                ex_id = cached.id
                if cached.document_id != d["id"] or downgraded.get(d["slot"]):
                    # Same bytes uploaded as another document (e.g. by another vendor), or the reading as re-checked:
                    # record what was used against THIS document, linked to the original reading, so its evidence view
                    # and history show exactly what the checks used.
                    copy = Extraction(document_id=d["id"], config_key=cfg, status=cached.status,
                                      detected_type=cached.detected_type,
                                      result=reading.model_dump() if downgraded.get(d["slot"]) else cached.result,
                                      provider=cached.provider, model=cached.model, latency_ms=0, usage={},
                                      reused_from_id=cached.reused_from_id or cached.id)
                    db.add(copy)
                    db.flush()
                    ex_id = copy.id
                evidence[d["slot"]] = DocEvidence(d["slot"], d["id"], d["filename"], d["sha256"], d["layout"], d["pages"],
                                                  cached.status, reading, cached.error if failed else None, cached.provider,
                                                  cached.model, reused=True, extraction_id=ex_id,
                                                  signatures=d["signatures"])
            else:
                to_read.append(d)
    for slot, ev in evidence.items():
        st, title = _doc_summary(slot, ev.reading, ev.status, ev.error, ev.layout)
        if downgraded.get(slot):
            title += (f"; re-checked against the visible text: {', '.join(k.replace('_', ' ') for k in downgraded[slot])} "
                      "not verified")
        emit(run_id, "read", st, title + (" (not read again: this re-read covers only the selected file; a retry reads "
                                          "this one)" if ev.status == "failed" else
                                          " (reused: same file already read with the same settings)"),
             {"document_id": ev.document_id, "reused": True})

    def _read(d):
        with session_scope() as db:
            content = db.get(Document, d["id"]).content
        return d, reader.read(content, d["kinds"])

    if to_read:
        for d in to_read:
            emit(run_id, "read", "info", f"Reading {DOCUMENT_TYPES.get(d['slot'], d['slot'])} — {d['filename']} "
                                         f"({layout_label(d['layout'])}, {d['pages']} page(s))", {"document_id": d["id"]})
        with ThreadPoolExecutor(max_workers=min(4, len(to_read))) as pool:
            futures = [pool.submit(_read, d) for d in to_read]
            for fut in as_completed(futures):
                d, out = fut.result()
                with session_scope() as db:
                    ex = Extraction(document_id=d["id"], config_key=cfg, status=out.status,
                                    detected_type=out.reading.detected_type if out.reading else None,
                                    result=out.reading.model_dump() if out.reading else {}, provider=out.provider,
                                    model=out.model, latency_ms=out.latency_ms, usage=out.usage,
                                    error={**(out.error or {}), "attempts": [a.__dict__ for a in out.attempts]}
                                    if out.error or len(out.attempts) > 1 else None)
                    db.add(ex)
                    db.flush()
                    ex_id = ex.id
                evidence[d["slot"]] = DocEvidence(d["slot"], d["id"], d["filename"], d["sha256"], d["layout"], d["pages"],
                                                  out.status, out.reading, out.error, out.provider, out.model,
                                                  extraction_id=ex_id, signatures=d["signatures"])
                st, title = _doc_summary(d["slot"], out.reading, out.status, out.error, d["layout"])
                retries = [a for a in out.attempts if a.outcome != "ok"]
                if retries:
                    title += f" — after {len(retries)} provider retr{'y' if len(retries) == 1 else 'ies'} ({retries[-1].outcome})"
                emit(run_id, "read", st, title, {"document_id": d["id"], "latency_ms": out.latency_ms,
                                                 "attempts": [a.__dict__ for a in out.attempts]})
    failed = [e for e in evidence.values() if e.status == "failed"]
    emit(run_id, "read", "warning" if failed else "completed",
         f"Read {len(evidence) - len(failed)}/{len(evidence)} document(s)" + (f"; {len(failed)} need a retry" if failed else ""))
    _check_current(run_id, token)

    # ------------------------------------------------------------------ Stage 3: compare
    # A fixed order (by slot): reads finish in any order, and cached readings come first; neither may change a result.
    inp = EvalInput(form=form, docs=[evidence[s] for s in sorted(evidence)], policy=policy, revision_number=rev_no,
                    revision_id=rev_id, previous_form=prev_form, previous_docs=prev_docs, baselines=baselines,
                    bank_origin=start)
    emit(run_id, "compare", "started", "Comparing the form with the documents, and with the previous version")
    cmp = compare(inp)
    counts: dict[str, int] = {}
    for f in cmp.fields.values():
        counts[f.status] = counts.get(f.status, 0) + 1
    for f in cmp.fields.values():
        if f.status in ("differs", "similar"):
            emit(run_id, "compare", "warning", f"{f.label}: {f.detail}"[:300])
    material = [c for c in cmp.changes if c.material]
    if material:
        emit(run_id, "compare", "warning", "Changed since the previous version: " + ", ".join(c.label for c in material),
             {"changes": [c.__dict__ for c in material]})
    emit(run_id, "compare", "completed", "Fields: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(counts.items())),
         {"counts": counts, "changes": len(cmp.changes)})
    _check_current(run_id, token)

    # ------------------------------------------------------------------ Stage 4: check (incl. independent verification)
    emit(run_id, "check", "started", f"Applying {policy.buyer_name} policy v{policy.version}")
    bank_records = _bank_lookups(req_id, form)
    for r in bank_records:
        # The account result names the holder the bank holds: reviewers only (the supplier sees a neutral line).
        acct_ev = r["identifier_type"] == "BANK_ACCOUNT"
        emit(run_id, "check", "info", _bank_note(r), {"method": r["method"], "result": r["result"]} | (
            {"owner": "system", "vendor_title": "Bank account checked at the bank (SANDBOX penny drop)"} if acct_ev else {}))
    with session_scope() as db:
        for r in bank_records:
            db.add(Verification(request_id=req_id, revision_id=rev_id, actor_type="system", **r))
        g = clean_identifier((form.get("tax") or {}).get("gstin"))
        accepted = {m for v in policy.verification if v.identifier == "GSTIN" for m in v.accepted_methods}
        if "simulated_registry" in accepted and (form.get("tax") or {}).get("gst_registered") == "yes" and check_gstin(g).ok:
            recs = db.scalars(select(Verification).where(Verification.request_id == req_id, Verification.identifier_value == g,
                                                         Verification.method.in_(sorted(accepted)))
                              .order_by(Verification.id)).all()
            # Look up when nothing has been recorded yet, or when the LATEST accepted result says the registry was
            # unavailable (that is what an offered retry is for). A conclusive latest result from any accepted method
            # stands: e.g. a reviewer's manual lookup recorded during an outage is not overwritten by another failure.
            if not recs or recs[-1].result == "unavailable":
                ans = registry.lookup_gstin(g, buyer_id)
                db.add(Verification(request_id=req_id, revision_id=rev.id, identifier_type="GSTIN", identifier_value=g,
                                    method="simulated_registry", result=ans.result, observed=ans.observed,
                                    source=registry.SOURCE, actor_type="system"))
                note = {"found": f"found '{(ans.observed or {}).get('legal_name')}', status {(ans.observed or {}).get('status')}",
                        "not_found": "no record for this GSTIN", "unavailable": "registry unavailable"}[ans.result]
                emit(run_id, "check", "info", f"GST registry lookup (SIMULATED test registry) for {g}: {note}",
                     {"method": "simulated_registry", "result": ans.result})
        resolutions = [ResolutionRec(r.check_key, r.fingerprint, r.outcome, r.note,
                                     (db.get(User, r.actor_id).display_name if r.actor_id else "reviewer"), r.created_at,
                                     r.revision_id, r.vendor_text, r.confirmation)
                       for r in db.scalars(select(ReviewResolution).where(ReviewResolution.request_id == req_id)
                                           .order_by(ReviewResolution.id))
                       # A bank-change acceptance without an independent confirmation (recorded before one was required)
                       # no longer clears the change; it stays in the history.
                       if not (r.check_key == "change:bank" and r.outcome == "accepted" and r.confirmation is None)]
        verifications = [VerificationRec(v.id, v.identifier_type, v.identifier_value, v.method, v.result, v.observed,
                                         v.source, v.actor_type, v.created_at)
                         for v in db.scalars(select(Verification).where(Verification.request_id == req_id).order_by(Verification.id))]
        # Read from OTHER requests, so it is counted again under a lock at publication (see crossfacts).
        dup = duplicate_bank_vendors(db, buyer_id, vendor_id, form)
        dup_id = duplicate_identity_vendors(db, buyer_id, vendor_id, form)

    inp.resolutions, inp.verifications, inp.duplicate_bank_vendors = resolutions, verifications, dup
    inp.duplicate_identity_vendors = dup_id
    checks, by_status, routing, auto_policy = _evaluate(inp, cmp, policy)
    risk = risk_mod.assess(checks, form)
    for c in checks:
        if c.blocking and c.status != "satisfied":
            emit(run_id, "check", "warning", f"{c.title}: {c.reason}"[:300], {"check": c.key, "owner": c.owner} | (
                {"vendor_title": f"{c.title}: {c.vendor_text}"[:300]} if c.owner == "vendor" and c.vendor_text else {}))
    emit(run_id, "check", "completed", f"{len(checks)} checks — " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in sorted(by_status.items())),
         {"by_status": by_status})
    emit(run_id, "check", "info", f"Risk: {risk['tier']} ({risk['score']}/100)"
         + (": " + "; ".join(f"{f['flag']} (+{f['weight']})" for f in risk["flags"][:4]) if risk["flags"] else ": no red flags"),
         {"risk": risk, "owner": "system", "vendor_title": "Checks summarised for the buyer's team"})

    # ------------------------------------------------------------------ Stage 5: decide
    emit(run_id, "decide", "completed" if routing.outcome == "ready" else "warning", _decide_title(routing, auto_policy),
         {"outcome": routing.outcome, "state": routing.state})

    # ------------------------------------------------------------------ Stage 6: communicate (atomic publish)
    with session_scope() as db:
        # Global lock order: vendor -> request -> run, taken by every publish. An automatic approval updates the vendor's
        # approved profile, so the publish must lock the vendor first, like a submission (vendor -> request) does.
        vendor = db.execute(select(Vendor).where(Vendor.id == vendor_id).with_for_update(key_share=True)).scalar_one()
        req = db.execute(select(Request).where(Request.id == req_id).with_for_update(key_share=True)).scalar_one()
        run = db.execute(select(Run).where(Run.id == run_id).with_for_update(key_share=True)).scalar_one()
        if run.status != RunStatus.RUNNING or run.lease_token != token or req.current_revision_id != run.revision_id \
                or req.state != RequestState.PROCESSING:
            if run.status == RunStatus.RUNNING:
                run.status, run.finished_at = RunStatus.SUPERSEDED, utcnow()
            raise Superseded()
        # A fact read from other requests may have changed since the checks (another vendor submitted the same bank
        # account meanwhile). Read it again under the account lock; if it changed, decide on the current records.
        lock_bank_account(db, buyer_id, form)
        lock_identity(db, buyer_id, form)
        dup_now = duplicate_bank_vendors(db, buyer_id, vendor_id, form)
        id_now = duplicate_identity_vendors(db, buyer_id, vendor_id, form)
        bank_moved = [o["id"] for o in dup_now] != [o["id"] for o in inp.duplicate_bank_vendors]
        if bank_moved or id_now != inp.duplicate_identity_vendors:
            inp.duplicate_bank_vendors, inp.duplicate_identity_vendors = dup_now, id_now
            checks, by_status, routing, auto_policy = _evaluate(inp, cmp, policy)
            risk = risk_mod.assess(checks, form)
            db.add(RunEvent(run_id=run_id, stage="check", status="warning",
                            title=(f"Checked again before publishing: this bank account is now on file for {len(dup_now)} "
                                   "other vendor record(s), so the checks above were re-applied with the current records"
                                   if dup_now else "Checked again before publishing: no other active vendor record uses this "
                                                   "bank account any more, so the checks above were re-applied")
                            if bank_moved else
                            "Checked again before publishing: the other vendor records with this PAN or GSTIN changed, "
                            "so the checks above were re-applied with the current records",
                            detail={"owner": "system", "vendor_title": "Checked again with the latest records"}))
            db.add(RunEvent(run_id=run_id, stage="decide", status="completed" if routing.outcome == "ready" else "warning",
                            title=_decide_title(routing, auto_policy), detail={"outcome": routing.outcome, "state": routing.state}))
        db.execute(update(Assessment).where(Assessment.request_id == req_id).values(superseded=True))
        master = vendor_master.build(inp, checks, risk, vendor.reference, db.get(Buyer, buyer_id).name, ref)
        assessment = Assessment(request_id=req_id, revision_id=run.revision_id, run_id=run_id, policy_id=run.policy_id,
                                outcome=routing.outcome, comparison=cmp.to_dict(),
                                summary={"headline": routing.headline, "by_status": by_status,
                                         "engine_version": ENGINE_VERSION, "risk": risk, "vendor_master": master,
                                         "documents": {s: {"status": e.status, "detected_type": e.detected_type,
                                                           "reused": e.reused, "provider": e.provider, "model": e.model,
                                                           "error": e.error, "document_id": e.document_id,
                                                           "extraction_id": e.extraction_id}
                                                       for s, e in evidence.items()}})
        db.add(assessment)
        db.flush()
        for c in checks:
            db.add(CheckResult(assessment_id=assessment.id, check_key=c.key, group=c.group, status=c.status, blocking=c.blocking,
                               title=c.title, reason=c.reason, evidence={**c.evidence, "fingerprint": c.fingerprint,
                                                                         "vendor_text": c.vendor_text, "severity": c.severity},
                               action=c.action, owner=c.owner, task_key=c.task_key))
        open_tasks = {t.task_key: t for t in db.scalars(select(Task).where(Task.request_id == req_id, Task.status == "open"))}
        newly_resolved = []
        for key, spec in routing.tasks.items():
            t = open_tasks.pop(key, None)
            if t:
                t.title, t.detail, t.fingerprint, t.assessment_id, t.owner = spec.title, spec.detail, spec.fingerprint, assessment.id, spec.owner
                t.resolution = {"checks": spec.check_keys, "vendor_lines": spec.vendor_lines}
            else:
                db.add(Task(request_id=req_id, assessment_id=assessment.id, task_key=key, owner=spec.owner, kind=spec.kind,
                            title=spec.title, detail=spec.detail, fingerprint=spec.fingerprint,
                            resolution={"checks": spec.check_keys, "vendor_lines": spec.vendor_lines}))
        for t in open_tasks.values():  # no longer blocking
            t.status, t.resolved_at = "resolved", utcnow()
            t.resolution = {**(t.resolution or {}), "resolved_by": "evaluation", "assessment_id": assessment.id,
                            "note": f"No longer blocking after evaluating revision {rev_no}"}
            if t.owner == "vendor":
                newly_resolved.append(f"Done: {t.title}")
        req.state = routing.state
        req.version += 1
        req.current_assessment_id = assessment.id
        if routing.outcome == "auto_reject":
            a = routing.auto_reject
            req.closed_at = utcnow()
            db.add(Decision(request_id=req_id, revision_id=run.revision_id, assessment_id=assessment.id, policy_id=run.policy_id,
                            request_version=req.version, outcome="rejected", actor_type="system", rule=a.rule, reason=a.reason))
            db.add(AuditEvent(request_id=req_id, vendor_id=req.vendor_id, event_type="REQUEST_REJECTED", actor_type="system",
                              data={"rule": a.rule, "assessment_id": assessment.id}))

        # Automatic approval of a clean request: a policy setting, allowed only under the conditions in
        # auto_approval_block. Generic: nothing here depends on a buyer, vendor or sample.
        approved = None
        if auto_policy:
            blocked = auto_approval_block(db, req, run, vendor, checks, policy, risk)
            if blocked:
                db.add(RunEvent(run_id=run_id, stage="decide", status="info",
                                title=f"Not approved automatically: {blocked}. A reviewer makes the final decision.",
                                detail={"owner": "system", "vendor_title": "A reviewer at the buyer makes the final decision"}))
            else:
                approved = _auto_approve(db, req, vendor, run, assessment, checks, policy, rev_no, newly_resolved)
                assessment.summary = {**assessment.summary, "headline": "All required checks satisfied — approved "
                                      "automatically under the buyer's policy", "auto_approved": True}
                db.add(RunEvent(run_id=run_id, stage="decide", status="completed",
                                title="Approved automatically (policy rule auto_approve_clean): no person's judgement was "
                                      "needed on this application. Supplier registration only; not bank verification or "
                                      "payment authorisation"))
        if approved is None:
            msg = compose_vendor_message(routing, (form.get("business") or {}).get("legal_name"), rev_no, newly_resolved)
            db.add(Message(request_id=req_id, vendor_id=req.vendor_id, kind=msg.kind, subject=msg.subject, body=msg.body,
                           items=msg.items, assessment_id=assessment.id))
            subject, kind = msg.subject, msg.kind
        else:
            subject, kind = "Approved — welcome aboard", "approved"
        db.add(AuditEvent(request_id=req_id, vendor_id=req.vendor_id, event_type="EVALUATION_COMPLETED", actor_type="system",
                          data={"run_id": run_id, "assessment_id": assessment.id, "outcome": routing.outcome,
                                "state": req.state, "revision": rev_no, "auto_approved": approved is not None}))
        run.status, run.finished_at = RunStatus.SUCCEEDED, utcnow()
        db.add(RunEvent(run_id=run_id, stage="communicate", status="completed",
                        title=f"Vendor notified: “{subject}”. Status: {RequestState.PUBLIC[req.state]} ({req.state.replace('_', ' ').lower()})",
                        detail={"message_kind": kind, "tasks": len(routing.tasks)}))


def _evaluate(inp: EvalInput, cmp, policy: BuyerPolicy):
    """Checks and routing from the evidence (pure: no database, no model)."""
    checks = run_checks(inp, cmp)
    by_status: dict[str, int] = {}
    for c in checks:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    routing = route(checks)
    return checks, by_status, routing, routing.outcome == "ready" and policy.approval.auto_approve_when_clean


def _decide_title(routing, auto_policy: bool) -> str:
    return ("All required checks satisfied; this buyer's policy approves clean applications automatically" if auto_policy
            else routing.headline)


# Technical steps on a request: not business judgement, so they don't stop an automatic approval, but the decision
# names them (audit event type -> how the decision reason counts it).
TECHNICAL_STEPS = {
    "EVALUATION_FAILED": "evaluation failed",
    "RUN_RECOVERED": "interrupted run recovered",
    "RETRY_REQUESTED": "retry requested",
    "READING_REREAD_REQUESTED": "document re-read requested",
    "REEVALUATION_QUEUED": "re-evaluation under updated checks",
}

# Check statuses a clean request may have: satisfied, plus optional notes and requirements that don't apply.
ACCEPTABLE_FOR_AUTO = {"satisfied", "not_applicable", "info"}


def auto_approval_block(db, req: Request, run: Run, vendor: Vendor, checks, policy: BuyerPolicy,
                        risk: dict | None = None) -> str | None:
    """Why this request may NOT be approved automatically, or None when it may.

    Clean means: every check is satisfied (optional notes and not-applicable requirements allowed), every required
    verification passed, and no person's business judgement was ever required or recorded on this request. That is
    read from durable history, so a review that was later resolved or superseded still counts. System retries and
    re-reads are technical steps, not judgement; they don't disqualify but are named in the decision.
    """
    if run.policy_id != req.policy_id or not policy.approval.auto_approve_when_clean:
        return "the request's policy does not approve automatically"
    if req.kind != "onboarding" or vendor.current_profile_id is not None:
        return "only a first onboarding without an approved profile is approved automatically"
    if any(c.status not in ACCEPTABLE_FOR_AUTO for c in checks):
        return "a check is not fully satisfied"
    if risk and risk_mod.ORDER[risk["tier"]] > risk_mod.ORDER[policy.approval.auto_approve_max_risk]:
        top = risk["flags"][0]["flag"] if risk["flags"] else "see the risk panel"
        return (f"the supplier's risk is {risk['tier']} ({risk['score']}/100, mainly: {top}); automatic approval is "
                f"only up to {policy.approval.auto_approve_max_risk} risk")
    need = policy.approval.second_approver_from_risk
    if risk and need != "never" and risk_mod.ORDER[risk["tier"]] >= risk_mod.ORDER[need]:
        return f"the supplier's risk is {risk['tier']} ({risk['score']}/100): the policy requires two people to approve"
    for v in policy.verification:
        c = next((x for x in checks if x.key == f"verification:{v.id}"), None)
        if v.required and (c is None or c.status not in ("satisfied", "not_applicable")):
            return "a required verification has not passed"
    if db.scalar(select(func.count()).select_from(ReviewResolution).where(ReviewResolution.request_id == req.id)):
        return "a person reviewed a finding on this application"
    if db.scalar(select(func.count()).select_from(Task).where(Task.request_id == req.id,
                                                              Task.owner.in_(["reviewer", "finance"]))):
        return "an item on this application needed a reviewer or finance"
    if db.scalar(select(func.count()).select_from(Verification).where(Verification.request_id == req.id,
                                                                      Verification.method == "manual_registry")):
        return "a registry lookup was recorded by a person"
    if db.scalar(select(func.count()).select_from(Decision).where(Decision.request_id == req.id,
                                                                  Decision.outcome == "approved")):
        return "this application already has an approval"
    return None


def _auto_approve(db, req: Request, vendor: Vendor, run: Run, assessment: Assessment, checks, policy: BuyerPolicy,
                  rev_no: int, newly_resolved: list[str]):
    passed = [c for c in checks if c.status == "satisfied"]
    optional = [c for c in checks if c.status in ("info", "not_applicable")]
    methods = sorted({(c.evidence or {}).get("method") for c in checks
                      if c.key.startswith("verification:") and c.status == "satisfied" and (c.evidence or {}).get("method")})
    simulated = "simulated_registry" in methods
    technical = dict(db.execute(select(AuditEvent.event_type, func.count()).where(
        AuditEvent.request_id == req.id, AuditEvent.event_type.in_(list(TECHNICAL_STEPS)))
        .group_by(AuditEvent.event_type)).all())
    tech_text = ", ".join(f"{technical[k]} {label}" for k, label in TECHNICAL_STEPS.items() if technical.get(k))
    reason = (f"Approved automatically under {policy.label} (v{policy.version}): all {len(passed)} applicable checks passed "
              "from the documents and the form"
              + ("; registry lookup: SIMULATED test registry, not a real government check" if simulated else "")
              + (f". Not counted as passed: {len(optional)} optional or not-applicable item(s)" if optional else "")
              + (f". Technical steps before this decision: {tech_text}" if tech_text else "")
              + ". No person's judgement was needed. This approves supplier registration only; it is not bank "
                "verification or payment authorisation.")
    buyer = db.get(Buyer, req.buyer_id)
    rev = db.get(Revision, run.revision_id)
    body = (f"Your application {req.reference} (revision {rev_no}) was approved automatically under {buyer.name}'s "
            "onboarding policy: every required check passed."
            + (" In this demo the registry lookup is simulated." if simulated else "")
            + " This covers supplier registration only; it is not bank verification, a purchase order or payment "
              "authorisation.")
    return approve_request(db, req, vendor, rev, assessment.id, actor_type="system", actor_id=None,
                           audit_actor_type="system", rule="auto_approve_clean", reason=reason, message_body=body,
                           message_items=[{"type": "accepted", "text": t} for t in newly_resolved] or None,
                           audit_data={"run_id": run.id, "policy_version": policy.version,
                                       "verification_methods": methods})
