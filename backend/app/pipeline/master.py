"""The vendor master record an approval hands to the ERP: identity, tax, payment and bank data in one shape.

Onboarding ends with "master data creation" (Zamp's step 5; Gatekeeper/Opstream sync the record to the ERP). Beyond
the form, the record carries what finance must set up before paying:
- MSME: a Micro or Small enterprise (not a trader) must be paid within 45 days of acceptance, 15 without a written
  agreement (MSMED Act s.15); paying later defers the buyer's tax deduction to the year of payment (Income-tax s.43B(h)).
  Medium enterprises and traders are outside s.15 (MSME Ministry OM of 28 June 2024).
- GST: the taxpayer type (a Composition supplier issues bills of supply; no input tax credit) and the filing record.
- TDS: from 1 April 2026 under the Income-tax Act, 2025, s.393(1) (the old 194C/194J numbers fail validation);
  suggested from the service category, for the tax team to confirm.
Pure: built from the evaluation's inputs and findings.
"""

from __future__ import annotations

import csv
import io

from ..india.validators import GST_STATE_CODES, clean_identifier
from ..schemas.submission import get_field

# Service category -> TDS under the Income-tax Act, 2025, s.393(1) Table (payee: a company or LLP).
TDS_BY_CATEGORY = {
    "IT services & software": ("Sl. 6(iii)", "Fees for technical services", "2%", "194J(b)",
                               "Software development can be professional services (10%); confirm the contract's nature"),
    "Facility management": ("Sl. 6(i)", "Contract work", "2%", "194C", None),
    "Logistics & transport": ("Sl. 6(i)", "Contract work (carriage of goods)", "2%", "194C",
                              "A transporter owning up to 10 goods carriages is exempt with a PAN declaration"),
    "Marketing & advertising": ("Sl. 6(i)", "Contract work (advertising)", "2%", "194C", None),
    "Professional services (legal, audit, consulting)": ("Sl. 6(iii)", "Fees for professional services", "10%",
                                                         "194J(a)", None),
    "Staffing & manpower": ("Sl. 6(i)", "Contract work (supply of labour)", "2%", "194C", None),
}
ENTITY = {"private_limited": "Private limited company", "llp": "Limited liability partnership", "other": "Other"}


def _reading(inp, dtype: str, key: str):
    for d in inp.docs_of_type(dtype):
        f = d.obs(key)
        if f and f.status == "found":
            return f.normalized or f.value
    return None


def build(inp, checks, risk: dict, vendor_ref: str | None, buyer_name: str | None, request_ref: str | None) -> dict:
    form = inp.form
    by = {c.key: c for c in checks}
    g = clean_identifier(get_field(form, "tax.gstin")) if get_field(form, "tax.gst_registered") == "yes" else ""
    reg = (by.get("verification:gst_registry_check").evidence or {}) if by.get("verification:gst_registry_check") else {}
    obs = reg.get("observed") or {}
    gst_type = obs.get("taxpayer_type")
    type_src = ("SIMULATED test registry" if reg.get("method") == "simulated_registry"
                else "GST portal, recorded by a reviewer") if gst_type else None
    if not gst_type:
        gst_type = _reading(inp, "gst_certificate", "registration_type")
        type_src = "read from the GST certificate; not confirmed at the registry" if gst_type else None
    composition = str(gst_type or "").lower().startswith("composition")
    # Onboarding proves the registration and the filing record, not any invoice: credit is decided per invoice.
    itc = (None if not g else "Not available: Composition supplier (bills of supply)" if composition
           else "Not confirmed: the taxpayer type is unknown; check it on the GST portal" if not gst_type
           else "Depends on each invoice: a Regular taxpayer issues tax invoices, but credit is claimed per invoice "
                "under the CGST s.16 conditions; not assessed at onboarding"
                + ("" if obs.get("taxpayer_type") else "; type from the certificate, confirm it"))
    returns = by.get("verification:gst_returns")
    msme = get_field(form, "tax.msme_registered") == "yes"
    category = str(_reading(inp, "msme_certificate", "enterprise_category") or "").strip().title() if msme else ""
    mse = category in ("Micro", "Small")
    service = get_field(form, "service.category")
    tds = TDS_BY_CATEGORY.get(service)
    bank_v = (by.get("verification:bank_account").evidence or {}) if by.get("verification:bank_account") else {}
    branch = (by.get("verification:bank_branch").evidence or {}) if by.get("verification:bank_branch") else {}
    # Payment set-up: an instruction to the payment system. Only a live bank confirmation releases it; a sandbox
    # result is a demonstration, not a reason to pay.
    hold = []
    if not (bank_v.get("name_match_result") == "MATCH" and "sandbox" not in str(bank_v.get("method") or "")):
        hold.append("the account holder has not been confirmed by the bank through a live provider"
                    + (" (this demo's bank check is a SANDBOX)" if "sandbox" in str(bank_v.get("method") or "") else ""))
    change = by.get("change:bank")
    confirmation = ((change.evidence or {}).get("resolution") or {}).get("confirmation") if change else None
    if change is not None and change.status != "satisfied" and change.status != "info":
        hold.append("the bank-detail change has not been confirmed by finance")
    return {
        "record_type": "vendor_master",
        "vendor_code": vendor_ref, "buyer": buyer_name, "source_request": request_ref,
        "legal_name": get_field(form, "business.legal_name"), "trade_name": get_field(form, "business.trade_name"),
        "constitution": ENTITY.get(get_field(form, "business.entity_type") or "", None),
        "pan": clean_identifier(get_field(form, "tax.pan")) or None,
        "cin_llpin": clean_identifier(get_field(form, "business.registration_number")) or None,
        "incorporation_date": get_field(form, "business.incorporation_date"),
        "registered_address": {"line1": get_field(form, "business.registered_address.line1"),
                               "city": get_field(form, "business.registered_address.city"),
                               "state": get_field(form, "business.registered_address.state"),
                               "pincode": get_field(form, "business.registered_address.pincode")},
        "contact": {"name": get_field(form, "contact.name"), "email": get_field(form, "contact.email"),
                    "phone": get_field(form, "contact.phone")},
        "gst": {"registered": bool(g), "gstin": g or None,
                "state": GST_STATE_CODES.get(g[:2]) if g else None, "state_code": g[:2] if g else None,
                "taxpayer_type": gst_type, "taxpayer_type_source": type_src if g else None,
                "input_tax_credit": itc,
                "returns_last_12_months": (returns.reason if returns else None),
                "e_invoicing": "Required on invoices if the supplier's aggregate turnover exceeds ₹5 crore (IRN and QR "
                               "code); confirm on the first invoice" if g else None},
        "msme": {"registered": msme, "udyam_number": get_field(form, "tax.udyam_number") if msme else None,
                 "category": category or None,
                 "payment_rule": ("Pay within 45 days of acceptance, or 15 days without a written agreement (MSMED Act "
                                  "s.15); a later payment defers the deduction to the year of payment (Income-tax s.43B(h))"
                                  if mse else "Outside MSMED s.15 (Medium enterprise): the buyer's standard terms apply"
                                  if msme and category == "Medium" else None),
                 # The law sets ceilings; the term that applies is whatever the parties agreed in writing, which
                 # onboarding does not record.
                 "max_days_with_written_agreement": 45 if mse else None,
                 "max_days_without_agreement": 15 if mse else None,
                 "agreed_payment_terms": "not recorded at onboarding; set from the signed agreement" if mse else None},
        "tds": ({"law": "Income-tax Act, 2025, s.393(1)", "table_entry": tds[0], "nature": tds[1], "rate": tds[2],
                 "old_reference": tds[3], "note": tds[4], "status": "suggested from the service category; the tax team "
                 "confirms"} if tds else {"status": "to be classified by the tax team", "service_category": service}),
        "bank": {"account_holder": get_field(form, "bank.account_holder"),
                 "account_number": clean_identifier(get_field(form, "bank.account_number")) or None,
                 "ifsc": clean_identifier(get_field(form, "bank.ifsc")) or None,
                 "bank_name": get_field(form, "bank.bank_name"),
                 "branch": (branch.get("observed") or {}).get("BRANCH") or get_field(form, "bank.branch"),
                 "verified_at_bank": ({"result": bank_v.get("name_match_result") or bank_v.get("result"),
                                       "name_at_bank": bank_v.get("name_at_bank"), "method": bank_v.get("method")}
                                      if bank_v else None)},
        "payment_setup": {"status": "hold" if hold else "ready", "hold_reasons": "; ".join(hold) or None,
                          "bank_change_confirmation": confirmation,
                          "note": "An instruction to the payment system; it does not stop a payment by itself."},
        "risk": {"score": risk.get("score"), "tier": risk.get("tier")},
    }


def to_csv(rec: dict) -> str:
    """One row of flattened fields (nested sections joined with '.'), the shape ERP import templates expect."""
    flat: dict[str, object] = {}

    def walk(prefix: str, v):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(f"{prefix}.{k}" if prefix else k, x)
        else:
            flat[prefix] = "" if v is None else v
    walk("", rec)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(list(flat))
    w.writerow(list(flat.values()))
    return buf.getvalue()
