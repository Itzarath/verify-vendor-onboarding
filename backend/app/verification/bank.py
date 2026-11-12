"""Bank details verified at the source: the branch (IFSC directory) and the account holder (penny drop).

- IFSC directory: live lookup in Razorpay's free IFSC directory, built from RBI's branch list. The demo's sample banks
  are fictional, so their codes are answered by a labelled demo directory instead.
- Penny drop: in production a payment provider credits ₹1 to the account (or validates it with the bank) and returns
  the name registered at the bank, which is compared with the declared holder (e.g. Cashfree / Razorpay account
  validation: name_at_bank, name_match_score, name_match_result). That needs a paid provider account, so it runs
  here against a SANDBOX that knows the sample accounts plus published-style test accounts. Real accounts are reported
  as not verifiable in this demo, never as verified.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache

import httpx

from ..config import BACKEND_ROOT, get_settings
from ..india.names import compare_names

IFSC_URL = "https://ifsc.razorpay.com/{}"
IFSC_LIVE = "RBI branch directory via Razorpay's IFSC API (live lookup)"
IFSC_DEMO = "Demo bank directory: the sample banks are fictional (SIMULATED)"
PENNY_SANDBOX = "Bank account validation SANDBOX (penny drop, SIMULATED): knows the demo's sample accounts only"
IFSC_FIELDS = ("BANK", "BRANCH", "CITY", "DISTRICT", "STATE", "ADDRESS", "BANKCODE", "MICR", "NEFT", "RTGS", "IMPS", "UPI")
IFSC_FLAGS = {"NEFT", "RTGS", "IMPS", "UPI"}  # true/false; every other field is text


@dataclass
class Answer:
    result: str  # IFSC: found | not_found | unavailable;  account: valid | invalid | no_record | unavailable
    observed: dict | None
    method: str
    source: str


@lru_cache
def _sandbox() -> dict:
    p = BACKEND_ROOT / "fixtures" / "out" / "simulated_sources.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"bank_codes": [], "banks": {}, "accounts": {}}


def lookup_ifsc(ifsc: str) -> Answer:
    sb = _sandbox()
    if ifsc[:4] in sb["bank_codes"]:
        rec = sb["banks"].get(ifsc)
        return Answer("found" if rec else "not_found", dict(rec) if rec else None, "demo_bank_directory", IFSC_DEMO)
    if not get_settings().live_lookups:
        return Answer("unavailable", None, "ifsc_directory", IFSC_LIVE + " — live lookups are switched off")
    try:
        r = httpx.get(IFSC_URL.format(ifsc), timeout=8.0, headers={"User-Agent": "verify-onboarding-demo"})
    except httpx.HTTPError:
        return Answer("unavailable", None, "ifsc_directory", IFSC_LIVE)
    if r.status_code == 404:
        return Answer("not_found", None, "ifsc_directory", IFSC_LIVE)
    if r.status_code != 200:
        return Answer("unavailable", None, "ifsc_directory", IFSC_LIVE)
    try:
        data = r.json()
    except ValueError:
        return Answer("unavailable", None, "ifsc_directory", IFSC_LIVE)
    # A reply without a bank name is not a directory record: treat it as an outage, never as a confirmed branch.
    if not isinstance(data, dict) or not isinstance(data.get("BANK"), str) or not data["BANK"].strip():
        return Answer("unavailable", None, "ifsc_directory", IFSC_LIVE + " — the reply had no usable branch record")
    return Answer("found", {k: data[k] for k in IFSC_FIELDS if isinstance(data.get(k), bool if k in IFSC_FLAGS else str)},
                  "ifsc_directory", IFSC_LIVE)


def penny_drop(account: str, ifsc: str) -> Answer:
    """What the bank says about this account (sandbox). The name match is judged by the checks, against the form."""
    rec = _sandbox()["accounts"].get(f"{account}|{ifsc}")
    if rec is None:
        return Answer("no_record", None, "penny_drop_sandbox", PENNY_SANDBOX)
    if rec.get("status") != "VALID":
        return Answer("invalid", {"status": "INVALID"}, "penny_drop_sandbox", PENNY_SANDBOX)
    return Answer("valid", {"status": "VALID", "name_at_bank": rec["name_at_bank"]}, "penny_drop_sandbox", PENNY_SANDBOX)


def name_match(name_at_bank: str | None, declared: str | None) -> tuple[int, str]:
    """(score 0-100, MATCH | PARTIAL_MATCH | NO_MATCH), the way account-validation APIs grade the bank's name."""
    c = compare_names(name_at_bank, declared)
    if c.relation in ("exact", "equivalent"):
        return 100, "MATCH"
    if c.relation == "suffix_differs":
        return 90, "MATCH"
    if c.relation in ("similar", "suffix_conflict"):
        return int(round(c.similarity * 80)), "PARTIAL_MATCH"
    return int(round(c.similarity * 50)), "NO_MATCH"


_BANK_NOISE = {"BANK", "LTD", "LIMITED", "THE", "OF", "CO", "OPERATIVE", "COOPERATIVE", "AND"}
# Names people commonly write for the larger banks, by the IFSC's bank code (its first four letters), after dropping
# the words above: "SBI" and "State Bank of India" are the same bank; "Bank of India" (BKID) is a different one.
BANK_ALIASES = {
    "SBIN": {"SBI", "STATE INDIA"}, "PUNB": {"PNB", "PUNJAB NATIONAL"}, "BARB": {"BOB", "BARODA"},
    "BKID": {"BOI", "INDIA"}, "UBIN": {"UBI", "UNION", "UNION INDIA"}, "CBIN": {"CBI", "CENTRAL", "CENTRAL INDIA"},
    "IOBA": {"IOB", "INDIAN OVERSEAS"}, "IDIB": {"INDIAN"}, "CNRB": {"CANARA"}, "MAHB": {"BOM", "MAHARASHTRA"},
    "HDFC": {"HDFC"}, "ICIC": {"ICICI"}, "UTIB": {"AXIS"}, "KKBK": {"KOTAK", "KOTAK MAHINDRA"}, "YESB": {"YES"},
    "IDFB": {"IDFC", "IDFC FIRST"}, "INDB": {"INDUSIND"},
}


def branch_place(o: dict) -> str:
    """'Residency Road, Bengaluru, Karnataka' from a directory record, without repeating the city."""
    branch, city, state = o.get("BRANCH") or "", (o.get("CITY") or "").title(), (o.get("STATE") or "").title()
    parts = [branch] + [x for x in (city, state) if x and x.lower() not in branch.lower()]
    return ", ".join(p for p in parts if p)


def same_bank(a: str | None, b: str | None, code: str | None = None) -> bool:
    """Is the bank written on the form (a) the directory's bank (b) for an IFSC with this bank code?
    'HDFC Bank' = 'HDFC BANK LIMITED'; 'SBI' = 'State Bank of India'; 'Bank of India' != 'State Bank of India'."""
    def toks(s):
        return [t for t in "".join(ch if ch.isalnum() else " " for ch in (s or "").upper()).split() if t not in _BANK_NOISE]
    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return False
    known = BANK_ALIASES.get((code or "").upper()[:4])
    if known is not None:
        return " ".join(ta) in known or ta == tb
    # Other banks: the same words, or one name is the other's leading words ('Saraswat' = 'Saraswat Co-operative Bank').
    n = min(len(ta), len(tb))
    return ta[:n] == tb[:n]
