"""Supplier risk: a 0-100 score from explained red flags, read from the evaluation's findings.

Production KYB scores work this way (e.g. FastKYC: weighted red flags such as an inactive GST registration or unfiled
returns, capped at 100, read as low / medium / high), and onboarding tools route by the tier: a low-risk supplier may
be approved automatically, a high-risk one needs more scrutiny and a second approver. Each flag names the finding it
comes from, so the score is never a black box. Pure: no database, no model.
"""

from __future__ import annotations

from datetime import date

from ..india.validators import parse_date

TIERS = (("low", 0), ("medium", 25), ("high", 50))
ORDER = {"low": 0, "medium": 1, "high": 2}


def tier(score: int) -> str:
    return [name for name, floor in TIERS if score >= floor][-1]


def assess(checks, form: dict, today: date | None = None) -> dict:
    by = {c.key: c for c in checks}
    flags: list[dict] = []

    def add(weight: int, text: str, check: str | None = None):
        flags.append({"weight": weight, "flag": text, "check": check})

    def ev(key):
        c = by.get(key)
        return (c.evidence or {}) if c else {}

    for c in checks:
        e = c.evidence or {}
        if c.key.startswith("authenticity:") and c.status != "info" and (
                e.get("changed_after_signing") or any(s.get("status") == "broken" for s in e.get("signatures") or [])):
            add(50, f"A certificate was changed after it was signed ({e.get('document')})", c.key)
        if c.key.startswith("authenticity:") and e.get("expected_signer") and c.status == "needs_review":
            add(25, f"A certificate is signed by an unexpected party ({e.get('document')})", c.key)
    bank = ev("verification:bank_account")
    if bank.get("name_match_result") == "NO_MATCH":
        add(50, "The bank names someone else as the account holder", "verification:bank_account")
    elif bank.get("name_match_result") == "PARTIAL_MATCH":
        add(15, "The bank's name for the account only partly matches", "verification:bank_account")
    elif bank.get("result") == "invalid":
        add(25, "The bank reports no such account", "verification:bank_account")
    if by.get("verification:bank_branch") and by["verification:bank_branch"].status == "not_satisfied":
        add(15, "The IFSC is not a branch of the bank named", "verification:bank_branch")
    reg = by.get("verification:gst_registry_check")
    if reg is not None:
        status = str(((reg.evidence or {}).get("observed") or {}).get("status") or "").lower()
        if status and status != "active":
            add(30, f"GST registration is {status}", reg.key)
        elif reg.status == "not_satisfied" and (reg.evidence or {}).get("result") == "found":
            add(25, "The GST registry lists a different business for this GSTIN", reg.key)
    returns = ev("verification:gst_returns")
    missed = returns.get("gstr3b_not_filed")
    n = len(missed) if isinstance(missed, list) else int(missed or 0)
    if n:
        add(min(30, 10 + 5 * n), f"GSTR-3B not filed for {n} of the last 12 months", "verification:gst_returns")
    if by.get("cross:gstin_pan") and by["cross:gstin_pan"].status == "not_satisfied":
        add(25, "The PAN and the GSTIN belong to different taxpayers", "cross:gstin_pan")
    dup = by.get("credibility:duplicate_bank")
    if dup is not None and (dup.evidence or {}).get("other_vendor_records"):
        add(15 if (dup.evidence or {}).get("resolution") else 30,
            "The bank account is also on file for another vendor" + (" (accepted by a reviewer)" if (dup.evidence or {}).get("resolution") else ""),
            dup.key)
    same_id = by.get("credibility:duplicate_identity")
    if same_id is not None and (same_id.evidence or {}).get("other_vendor_records"):
        accepted = bool((same_id.evidence or {}).get("resolution"))
        add(10 if accepted else 20, "The PAN or GSTIN is also on file for another vendor"
            + (" (accepted by a reviewer)" if accepted else ""), same_id.key)
    holder = by.get("credibility:bank_holder")
    if holder is not None and (holder.status != "satisfied" or (holder.evidence or {}).get("resolution")):
        add(10, "The bank account is not held in the legal name (for example a trade name)", holder.key)
    if ((form.get("declarations") or {}).get("conflict_of_interest")) == "disclosed":
        add(15, "A conflict of interest was declared", "declaration:conflict")
    # Only a real change of account, IFSC, holder or bank counts: a renewed letter for the same details is not one.
    if any(str(x.get("field") or "").startswith("bank.") for x in ev("change:bank").get("changes") or []):
        add(15, "Bank details changed during onboarding", "change:bank")
    if "change:identity" in by:
        add(10, "Identity details changed during onboarding", "change:identity")
    if any(k.startswith("confirm:") for k in by):
        add(5, "Some values were read only from scanned images", None)
    if by.get("credibility:bank_proof_date") and by["credibility:bank_proof_date"].status == "not_satisfied":
        # Not satisfied because the letter is old, or because a reviewer refused a date (e.g. one in the future): say which.
        try:
            dated = date.fromisoformat(str(ev("credibility:bank_proof_date").get("document_date")))
        except ValueError:
            dated = None
        future = dated is not None and dated > (today or date.today())
        add(5, "The bank proof is dated in the future" if future else "The bank proof is more than 6 months old",
            "credibility:bank_proof_date")
    if any(k.startswith("scope:") for k in by):
        add(10, "Outside the standard scope (entity type or GST registration)", None)
    inc = parse_date((form.get("business") or {}).get("incorporation_date"))
    if inc is not None:
        age = ((today or date.today()) - inc).days
        if 0 <= age < 365:
            add(20 if age < 180 else 15, f"Incorporated {age // 30} month(s) ago (less than a year)", None)
    score = min(100, sum(f["weight"] for f in flags))
    return {"score": score, "tier": tier(score), "flags": sorted(flags, key=lambda f: -f["weight"])}
