"""SANDBOX data for the external verification adapters, built from the synthetic scenarios.

The demo's sample companies and banks are fictional, so real verification sources can't know them. Like the
simulated GST registry, these sandbox records answer for the fictional samples only, in the response shape of the
real services (the Razorpay/RBI IFSC directory; a payment provider's bank-account validation, "penny drop"; the GST
taxpayer search with its return-filing table). Every answer is labelled SIMULATED wherever it is shown. Real
identifiers go to the real free sources (IFSC directory), or are reported as not verifiable in this demo.

Run: python -m fixtures.sandbox   (rewrites fixtures/out/simulated_sources.json and adds the GST taxpayer type and
filing history to fixtures/out/simulated_registry.json; never touches the sample PDFs or their stored readings).
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"

# Periods the simulated GST return-filing table covers: the 12 latest complete months as of AS_OF.
AS_OF = "2026-11-01"
PERIODS = [f"{y}-{m:02d}" for y, m in [(2025, 10), (2025, 11), (2025, 12)] + [(2026, m) for m in range(1, 10)]]
# One sample company with gaps (an edge case): GSTR-1 filed, but GSTR-3B (the return that pays the tax) missed for
# three months. The buyer's input tax credit on those invoices is at risk (CGST s.16(2)(c), Rule 37A).
MISSED_3B = {"24AAPCS2291H1Z4": {"2026-04", "2026-05", "2026-07"}}

# Demo test accounts, like a provider sandbox's published test data: a letter can claim any holder, the bank's own
# record can't be forged by the applicant.
TEST_ACCOUNTS = {
    "000412300099999|DECB0000412": {"status": "VALID", "name_at_bank": "RAVI KUMAR SHARMA"},
    "000412300000000|DECB0000412": {"status": "INVALID"},
}


def _due(period: str, day: int) -> str:
    y, m = map(int, period.split("-"))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y}-{m:02d}-{day:02d}"


def filings(gstin: str) -> list[dict]:
    missed = MISSED_3B.get(gstin, set())
    return [{"period": p, "gstr1_filed_on": _due(p, 11), "gstr3b_filed_on": None if p in missed else _due(p, 20)}
            for p in PERIODS]


def build(scenarios: dict) -> dict:
    banks: dict[str, dict] = {}
    accounts: dict[str, dict] = dict(TEST_ACCOUNTS)
    for s in scenarios.values():
        f = s["form"]
        b = f.get("bank") or {}
        addr = (f.get("business") or {}).get("registered_address") or {}
        ifsc, acct = (b.get("ifsc") or "").upper(), (b.get("account_number") or "").replace(" ", "")
        if not ifsc:
            continue
        banks.setdefault(ifsc, {"IFSC": ifsc, "BANK": b.get("bank_name"), "BRANCH": b.get("branch"),
                                "CITY": (addr.get("city") or "").upper() or None,
                                "STATE": (addr.get("state") or "").upper() or None, "BANKCODE": ifsc[:4],
                                "NEFT": True, "RTGS": True, "IMPS": True, "UPI": True})
        if acct and b.get("account_holder"):
            accounts.setdefault(f"{acct}|{ifsc}", {"status": "VALID", "name_at_bank": b["account_holder"].upper()})
    return {"_note": "SANDBOX verification sources for the fictional samples only (SIMULATED; see fixtures/sandbox.py)",
            "bank_codes": sorted({i[:4] for i in banks}), "banks": banks, "accounts": accounts}


def main() -> None:
    scenarios = json.loads((OUT / "scenarios.json").read_text(encoding="utf-8"))
    (OUT / "simulated_sources.json").write_text(json.dumps(build(scenarios), indent=2), encoding="utf-8")
    reg_path = OUT / "simulated_registry.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    for gstin, rec in reg["gstin"].items():
        rec["taxpayer_type"] = "Regular"
        rec["filings_as_of"] = AS_OF
        rec["filings"] = filings(gstin)
    reg_path.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    print("wrote", OUT / "simulated_sources.json", "and updated", reg_path)


if __name__ == "__main__":
    main()
