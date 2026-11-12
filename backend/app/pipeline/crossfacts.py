"""Facts a request's checks read from OTHER requests.

Everything else a check reads belongs to the request itself, and any change to it (a correction, a review, a lookup)
queues a new run that supersedes the old one. A fact read from other requests can change without that: another
vendor can submit the same bank account while this request is between its checks and its publish. So such a fact
is read once for the checks, and read again under a lock when a result or a decision is published:

- the publish (and the final approval) take a transaction lock on the bank account, then read again;
- a submission that writes a bank account takes the same lock before it commits.

Either the submission is visible to the second read, or it waits until the decision has been committed. A decision
never rests on a fact that a committed submission has already made wrong.

What is compared is one canonical key per account, made by `bank_key` (the same cleaning the validators, the
comparison with documents and the reviewer's fingerprint use). Each revision stores it when it is written
(models.Revision.bank_account_key), so the database compares stored keys and never re-cleans raw input itself: a
database's whitespace rules (e.g. for a non-breaking space pasted with an account number) need not match Python's.
"""

from __future__ import annotations

from sqlalchemy import select, text

from ..india.validators import clean_identifier
from ..models import CheckResult, Request, RequestState, Revision, Vendor

DUPLICATE_BANK_CHECK = "credibility:duplicate_bank"
DUPLICATE_IDENTITY_CHECK = "credibility:duplicate_identity"


def bank_key(form: dict | None) -> str | None:
    """The canonical key of a form's bank account ('ACCOUNT|IFSC', cleaned), or None when either part is missing."""
    bank = (form or {}).get("bank") or {}
    acct, ifsc = clean_identifier(bank.get("account_number")), clean_identifier(bank.get("ifsc"))
    return f"{acct}|{ifsc}" if acct and ifsc else None


def lock_bank_account(db, buyer_id: int, form: dict | None) -> None:
    """Serialise everything that writes or relies on this buyer's use of this account (held until commit)."""
    key = bank_key(form)
    if key:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"bank-account|{buyer_id}|{key}"})


def duplicate_bank_vendors(db, buyer_id: int, vendor_id: int, form: dict | None) -> list[dict]:
    """The OTHER vendors of this buyer with an open or decided (not rejected) request on this account, by vendor id:
    [{"id", "reference"}]. A reviewer's judgement on a shared account is about exactly these records."""
    key = bank_key(form)
    if not key:
        return []
    users = (select(Request.vendor_id).join(Revision, Revision.id == Request.current_revision_id)
             .where(Request.buyer_id == buyer_id, Request.vendor_id != vendor_id, Request.state != RequestState.REJECTED,
                    Revision.bank_account_key == key))
    return [{"id": i, "reference": ref} for i, ref in
            db.execute(select(Vendor.id, Vendor.reference).where(Vendor.id.in_(users)).order_by(Vendor.id)).all()]


def recorded_duplicate_bank_vendors(db, assessment_id: int, check_key: str = DUPLICATE_BANK_CHECK) -> list[int] | None:
    """The other vendor records an assessment's duplicate check was based on (None: the check didn't run)."""
    c = db.scalar(select(CheckResult).where(CheckResult.assessment_id == assessment_id,
                                            CheckResult.check_key == check_key))
    if c is None:
        return None
    return sorted((c.evidence or {}).get("other_vendor_ids") or [])


# ---------------------------------------------------------------------------- the same legal identity (PAN / GSTIN)

def identity_keys(form: dict | None) -> tuple[str | None, str | None]:
    """The form's PAN and GSTIN, cleaned (None when absent)."""
    tax = (form or {}).get("tax") or {}
    return clean_identifier(tax.get("pan")) or None, clean_identifier(tax.get("gstin")) or None


def lock_identity(db, buyer_id: int, form: dict | None) -> None:
    """Serialise submissions and decisions that rely on this buyer's use of this PAN and GSTIN (held until commit).
    Always taken after the bank-account lock, PAN before GSTIN, so two transactions never wait on each other."""
    for kind, key in zip(("pan", "gstin"), identity_keys(form)):
        if key:
            db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"{kind}|{buyer_id}|{key}"})


def duplicate_identity_vendors(db, buyer_id: int, vendor_id: int, form: dict | None) -> list[dict]:
    """The OTHER vendors of this buyer with an open or decided (not rejected) request with this PAN or GSTIN:
    [{"id", "reference", "same": ["PAN", "GSTIN"]}]. Compared in Python with the validators' own cleaning (a buyer has
    few vendors in this demo; production would store the keys like Revision.bank_account_key)."""
    pan, gstin = identity_keys(form)
    if not pan and not gstin:
        return []
    rows = db.execute(select(Request.vendor_id, Vendor.reference, Revision.form)
                      .join(Revision, Revision.id == Request.current_revision_id)
                      .join(Vendor, Vendor.id == Request.vendor_id)
                      .where(Request.buyer_id == buyer_id, Request.vendor_id != vendor_id,
                             Request.state != RequestState.REJECTED)).all()
    found: dict[int, dict] = {}
    for vid, ref, other in rows:
        p, g = identity_keys(other)
        same = [name for name, hit in (("PAN", pan and p == pan), ("GSTIN", gstin and g == gstin)) if hit]
        if same:
            found.setdefault(vid, {"id": vid, "reference": ref, "same": set()})["same"].update(same)
    return [{"id": v["id"], "reference": v["reference"], "same": [x for x in ("PAN", "GSTIN") if x in v["same"]]}
            for _, v in sorted(found.items())]
