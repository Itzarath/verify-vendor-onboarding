from __future__ import annotations

import json
from functools import lru_cache
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from fastapi import Request as HttpRequest
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..extraction.service import FIXTURE_DIR, get_reader
from ..india.validators import INDIAN_STATES
from ..models import Assessment, Buyer, Document, Policy, Request, RequestState, Run, User, Vendor, VendorProfile
from ..pdf.inspect import render_page_png
from ..pipeline import registry
from ..schemas.submission import DOCUMENT_TYPES, FIELD_LABELS, SERVICE_CATEGORIES
from ..services import commands, views
from ..services.commands import ApiError
from . import access
from .deps import COOKIE, MAX_AGE, current_user, make_session, require_reviewer, require_vendor

api = APIRouter(prefix="/api")


# ---------------------------------------------------------------- health / auth
@api.get("/health")
def health(request: HttpRequest, db: Session = Depends(get_db)):
    if request.query_params.get("details") != "1" or not access.has_access(request):
        # The platform's health check (every few seconds) and uptime pingers: liveness only. No database query, so
        # a scale-to-zero database can pause while the demo is idle. Details only when asked for (?details=1), and
        # behind the access code when one is set.
        return {"ok": True}
    db.execute(select(1))
    from ..pdf.sandbox import SELF_TEST

    r = get_reader()
    from ..pipeline.worker import worker_alive

    out = {"ok": True, "env": get_settings().app_env, "reader": r.describe(), "reader_configured": r.configured,
           "pdf_sandbox": SELF_TEST or {"status": "not run"},
           "worker": {"enabled": get_settings().worker_enabled, "alive": worker_alive()}}
    chain = getattr(r, "chain", None)
    if chain is not None:
        out["model_keys"] = chain.key_status()  # counts and hold reasons only, never the keys
    return out


@api.get("/access")
def access_status(request: HttpRequest):
    return {"required": access.gate_enabled(), "granted": access.has_access(request)}


@api.post("/access")
def enter_access_code(body: dict, request: HttpRequest, response: Response):
    if not access.gate_enabled():
        return {"granted": True}
    client = request.client.host if request.client else "unknown"
    if access.too_many_failures(client):
        raise ApiError(429, "too_many_attempts", "Too many wrong codes. Wait ten minutes and try again.")
    if not access.code_matches(str(body.get("code") or "")):
        access.record_failure(client)
        raise ApiError(401, "wrong_code", "That access code is not right.")
    response.set_cookie(access.ACCESS_COOKIE, access.access_token(), max_age=access.ACCESS_MAX_AGE, httponly=True,
                        samesite="lax", secure=get_settings().app_env == "prod")
    return {"granted": True}


def _set_cookie(resp: Response, user: User):
    resp.set_cookie(COOKIE, make_session(user.id), max_age=MAX_AGE, httponly=True, samesite="lax",
                    secure=get_settings().app_env == "prod")


def _me(db, user: User) -> dict:
    buyer = db.get(Buyer, user.buyer_id)
    out = {"id": user.id, "name": user.display_name, "email": user.email, "role": user.role,
           "buyer": {"id": buyer.id, "slug": buyer.slug, "name": buyer.name}}
    if user.vendor_id:
        out["vendor"] = {"id": user.vendor_id, "name": db.get(Vendor, user.vendor_id).display_name}
    return out


@api.get("/auth/personas")
def personas(db: Session = Depends(get_db)):
    buyers = {b.id: b for b in db.scalars(select(Buyer))}
    staff = db.scalars(select(User).where(User.role != "vendor").order_by(User.buyer_id, User.id)).all()
    # The 40 most recent sign-ups, plus every supplier whose application is still open, however old: a supplier asked
    # for a correction must always be able to get back to it.
    recent = db.scalars(select(User).where(User.role == "vendor").order_by(User.id.desc()).limit(40)).all()
    waiting = db.scalars(select(User).where(User.role == "vendor", User.vendor_id.in_(
        select(Request.vendor_id).where(Request.state.in_(RequestState.OPEN))))).all()
    vendors = sorted({u.id: u for u in [*recent, *waiting]}.values(), key=lambda u: -u.id)
    return {"buyers": [{"slug": b.slug, "name": b.name} for b in buyers.values()],
            "staff": [{"id": u.id, "name": u.display_name, "role": u.role, "buyer": buyers[u.buyer_id].name} for u in staff],
            "vendors": [_vendor_row(db, u, buyers) for u in vendors]}


def _vendor_row(db: Session, u: User, buyers: dict) -> dict:
    """Enough to tell many suppliers with the same company name apart: the name typed at sign-up (the form's legal
    name replaces the displayed name), the supplier reference, and the latest application and its state."""
    v = db.get(Vendor, u.vendor_id)
    last = db.scalar(select(Request).where(Request.vendor_id == v.id).order_by(Request.id.desc()).limit(1))
    return {"id": u.id, "name": v.display_name, "buyer": buyers[u.buyer_id].name, "reference": v.reference,
            "signed_up_as": u.display_name if u.display_name != v.display_name else None,
            "application": last.reference if last else None, "state": last.state if last else None}


@api.post("/auth/login")
def login(body: dict, response: Response, db: Session = Depends(get_db)):
    user = db.get(User, int(body.get("user_id") or 0))
    if user is None:
        raise ApiError(404, "unknown_user", "Unknown account")
    _set_cookie(response, user)
    return _me(db, user)


@api.post("/auth/new-vendor")
def new_vendor(body: dict, response: Response, db: Session = Depends(get_db)):
    name = (body.get("company_name") or "").strip()
    if len(name) < 3:
        raise ApiError(400, "name_required", "Enter your company name")
    buyer = db.scalar(select(Buyer).where(Buyer.slug == (body.get("buyer") or "meridian")))
    if buyer is None:
        raise ApiError(404, "unknown_buyer", "Unknown buyer")
    v = Vendor(buyer_id=buyer.id, reference="pending", display_name=name[:200])
    db.add(v)
    db.flush()
    v.reference = f"VEN-{v.id:05d}"
    u = User(email=f"vendor{v.id}@vendors.demo", display_name=name[:200], role="vendor", buyer_id=buyer.id, vendor_id=v.id)
    db.add(u)
    db.commit()
    _set_cookie(response, u)
    return _me(db, u)


@api.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@api.get("/auth/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _me(db, user)


# ---------------------------------------------------------------- config for the form
@api.get("/config/form")
def form_config(user: User = Depends(current_user), db: Session = Depends(get_db)):
    buyer = db.get(Buyer, user.buyer_id)
    pol_id = buyer.active_policy_id
    if user.vendor_id is not None:
        # A correction is checked against the policy its request is pinned to, so the form asks for exactly that.
        # The buyer's active policy is for new applications.
        open_req = db.scalar(select(Request).where(Request.vendor_id == user.vendor_id,
                                                   Request.state.in_(RequestState.OPEN)))
        if open_req is not None:
            pol_id = open_req.policy_id
    pol = db.get(Policy, pol_id)
    return {"document_types": DOCUMENT_TYPES, "field_labels": FIELD_LABELS, "service_categories": SERVICE_CATEGORIES,
            "states": INDIAN_STATES, "policy": pol.document,
            "reader": {"mode": "fixture" if getattr(get_reader(), "provider", None) == "test-fixture" else "live",
                       "describe": get_reader().describe()},
            "limits": {"max_file_mb": get_settings().max_file_bytes // 2**20, "max_pages_per_file": get_settings().max_pages_per_file,
                       "max_files": get_settings().max_files_per_revision}}


# ---------------------------------------------------------------- vendor
@api.post("/vendor/documents")
async def upload(file: UploadFile = File(...), user: User = Depends(require_vendor), db: Session = Depends(get_db)):
    data = await file.read(get_settings().max_file_bytes + 1)
    doc = commands.upload_document(db, user, file.filename or "document.pdf", data)
    db.commit()
    return {"document_id": doc.id, "filename": doc.filename, "pages": doc.page_count, "layout": doc.inspection.get("layout"),
            "size_bytes": doc.size_bytes}


@api.post("/vendor/submit")
def submit(body: dict, user: User = Depends(require_vendor), db: Session = Depends(get_db)):
    return commands.submit(db, user, body)


@api.get("/vendor/overview")
def vendor_overview(user: User = Depends(require_vendor), db: Session = Depends(get_db)):
    return views.vendor_overview(db, user)


@lru_cache
def _scenarios() -> dict:
    p = FIXTURE_DIR / "scenarios.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


@api.get("/samples")
def samples(user: User = Depends(current_user)):
    return [{"key": k, "title": v["title"], "expect": v["expect"], "documents": v["documents"]} for k, v in _scenarios().items()]


@api.post("/vendor/samples/{key}/load")
def load_sample(key: str, user: User = Depends(require_vendor), db: Session = Depends(get_db)):
    s = _scenarios().get(key)
    if s is None:
        raise ApiError(404, "unknown_sample", "Unknown sample")
    docs = {}
    for slot, fname in s["documents"].items():
        data = (FIXTURE_DIR / "documents" / fname).read_bytes()
        d = commands.upload_document(db, user, fname, data)
        docs[slot] = {"document_id": d.id, "filename": d.filename, "pages": d.page_count, "layout": d.inspection.get("layout")}
    db.commit()
    return {"form": s["form"], "documents": docs, "title": s["title"]}


@api.get("/vendor/samples/files/{name}")
def sample_file(name: str, user: User = Depends(current_user)):
    p = FIXTURE_DIR / "documents" / name
    if "/" in name or "\\" in name or not p.exists() or p.suffix != ".pdf":
        raise ApiError(404, "not_found", "Not found")
    return Response(p.read_bytes(), media_type="application/pdf")


# ---------------------------------------------------------------- shared: runs + documents (authorised)
def _authorise_request(db, user: User, req: Request):
    if user.role == "vendor":
        if req.vendor_id != user.vendor_id:
            raise ApiError(404, "not_found", "Not found")
    elif req.buyer_id != user.buyer_id:
        raise ApiError(404, "not_found", "Not found")


_INTERNAL_OWNERS = {"reviewer", "finance", "system"}


@api.get("/runs/{run_id}/events")
def events(run_id: int, after: int = 0, user: User = Depends(current_user), db: Session = Depends(get_db)):
    run = db.get(Run, run_id)
    if run is None:
        raise ApiError(404, "not_found", "Not found")
    _authorise_request(db, user, db.get(Request, run.request_id))
    evs = views.run_events(db, run_id, after)
    if user.role == "vendor":  # vendors see progress, not internal review signals or provider traces
        for e in evs:
            d = e.get("detail") or {}
            if d.get("vendor_title"):  # the event's supplier-safe wording (e.g. without the bank's holder name)
                e["title"] = d["vendor_title"]
            elif d.get("owner") in _INTERNAL_OWNERS:
                e["title"] = "An item was passed to the buyer's team for review"
            # Keep only whether the event is about one document (the progress view needs it), not its details.
            e["detail"] = {"scope": "document"} if "document_id" in (e.get("detail") or {}) else None
    out = views.run_dict(run, for_vendor=user.role == "vendor")
    if run.status == "QUEUED":  # runs go one at a time: say how many are ahead (a count only, nothing about them)
        out["ahead"] = db.scalar(select(func.count()).select_from(Run).where(or_(
            Run.status == "RUNNING", and_(Run.status == "QUEUED", Run.queued_at < run.queued_at)))) or 0
    return {"run": out, "events": evs}


def _authorise_document(db, user: User, doc_id: int) -> Document:
    doc = db.get(Document, doc_id)
    if doc is None:
        raise ApiError(404, "not_found", "Not found")
    if user.role == "vendor":
        if doc.vendor_id != user.vendor_id:
            raise ApiError(404, "not_found", "Not found")
    else:
        if db.get(Vendor, doc.vendor_id).buyer_id != user.buyer_id:
            raise ApiError(404, "not_found", "Not found")
    return doc


@lru_cache(maxsize=64)
def _page_png(doc_id: int, sha: str, page: int) -> bytes:
    from ..db import session_scope
    with session_scope() as db:
        content = db.get(Document, doc_id).content
    return render_page_png(content, page, dpi=110)


@api.get("/documents/{doc_id}/page/{page}")
def page_image(doc_id: int, page: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _authorise_document(db, user, doc_id)
    if not 1 <= page <= doc.page_count:
        raise ApiError(404, "not_found", "No such page")
    return Response(_page_png(doc.id, doc.sha256, page), media_type="image/png",
                    headers={"Cache-Control": "private, max-age=3600"})


@api.get("/documents/{doc_id}/file")
def document_file(doc_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _authorise_document(db, user, doc_id)
    return Response(doc.content, media_type="application/pdf",
                    headers={"Content-Disposition": content_disposition(doc.filename)})


def content_disposition(filename: str | None) -> str:
    """inline, with the real name as UTF-8 (RFC 6266 filename*) and a plain-ASCII fallback.

    HTTP headers are Latin-1, so a name like 'PAN – final.pdf' (en dash) or one in an Indian script can't go into
    the header as it is."""
    name = filename or "document.pdf"
    fallback = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name) or "document.pdf"
    return f"inline; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


# ---------------------------------------------------------------- reviewer
@api.get("/review/dashboard")
def dashboard(user: User = Depends(require_reviewer), db: Session = Depends(get_db)):
    pol = db.get(Policy, db.get(Buyer, user.buyer_id).active_policy_id)
    # The outage switch only affects automated (simulated) lookups; a buyer that accepts manual lookups only has none.
    simulated = any("simulated_registry" in (v.get("accepted_methods") or []) for v in pol.document.get("verification") or [])
    return {**views.dashboard(db, user), "registry_outage": registry.state.is_out(user.buyer_id),
            "registry_simulated": simulated}


@api.get("/review/requests/{request_id}/vendor-master")
def vendor_master_export(request_id: int, format: str = "json", version: int | None = None,
                         user: User = Depends(require_reviewer), db: Session = Depends(get_db)):
    from ..models import Revision
    from ..pipeline.master import to_csv
    from ..services.views import vendor_master_record

    req = db.get(Request, request_id)
    if req is None or req.buyer_id != user.buyer_id:
        raise ApiError(404, "not_found", "Request not found")
    # The screen sends the case version it shows: a newer one would put different data in the file than on the screen.
    if version is not None and version != req.version:
        raise ApiError(409, "stale_view", "This case changed after your page loaded, so the file would not match what you "
                       "see. Go back, reload the page and download again.", {"current_version": req.version, "you_saw": version})
    vendor = db.get(Vendor, req.vendor_id)
    profile = db.get(VendorProfile, vendor.current_profile_id) if vendor.current_profile_id else None
    a = db.get(Assessment, req.current_assessment_id) if req.current_assessment_id else None
    rec = vendor_master_record(db, req, profile, {"summary": a.summary} if a else None)
    if rec is None:
        raise ApiError(404, "not_found", "No evaluation yet, so no vendor master record")
    rev = db.get(Revision, a.revision_id) if a else None
    rec = {**rec, "snapshot": {"case_version": req.version, "revision": rev.number if rev else None,
                               "assessment_id": a.id if a else None}}
    name = f"{vendor.reference}-vendor-master"
    if format == "csv":
        return Response(to_csv(rec), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    return Response(json.dumps(rec, indent=2, ensure_ascii=False), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}.json"'})


@api.get("/review/requests/{request_id}")
def case(request_id: int, user: User = Depends(require_reviewer), db: Session = Depends(get_db)):
    return views.case_detail(db, user, request_id)


@api.post("/review/requests/{request_id}/{action}")
def review_action(request_id: int, action: str, body: dict, user: User = Depends(require_reviewer), db: Session = Depends(get_db)):
    fn = {"approve": commands.approve, "reject": commands.reject, "resolve": commands.resolve_review,
          "verification": commands.record_verification, "retry": commands.retry, "reread": commands.reread}.get(action)
    if fn is None:
        raise ApiError(404, "unknown_action", "Unknown action")
    return fn(db, user, request_id, body)


@api.post("/review/demo/registry-outage")
def toggle_outage(body: dict, user: User = Depends(require_reviewer)):
    registry.state.set_outage(user.buyer_id, bool(body.get("on")))  # only this reviewer's buyer
    return {"registry_outage": registry.state.is_out(user.buyer_id)}
