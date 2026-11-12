"""Document templates: which fields each document type can contain.

Templates describe documents, not buyer requirements. Whether a document is
*required* is decided by the buyer policy.
"""

from __future__ import annotations

TEMPLATES: dict[str, dict] = {
    "pan_card": {
        "label": "PAN card / e-PAN",
        "fields": {
            "pan": "10-character Permanent Account Number exactly as printed",
            "holder_name": "Name of the PAN holder (company, LLP or person) exactly as printed",
            "father_name": "Father's name, only printed on an individual's PAN",
            "date": "Date of birth (individual) or date of incorporation/formation (entity), as printed",
        },
        "critical": ["pan", "holder_name"],
    },
    "gst_certificate": {
        "label": "GST registration certificate",
        "fields": {
            "gstin": "15-character GSTIN / registration number",
            "legal_name": "Legal name of the business",
            "trade_name": "Trade name, if any (null if blank or a dash)",
            "constitution": "Constitution of business (e.g. Private Limited Company, LLP)",
            "principal_address": "Address of principal place of business",
            "address_state": "State named in the principal place of business address",
            "date_of_liability": "Date of liability / validity from",
            "registration_type": "Type of registration (e.g. Regular)",
        },
        "critical": ["gstin", "legal_name"],
    },
    "incorporation_certificate": {
        "label": "Certificate of incorporation",
        "fields": {
            "entity_name": "Name of the company or LLP being incorporated",
            "registration_number": "CIN (21 characters) or LLPIN (e.g. AAB-1234)",
            "incorporation_date": "Date of incorporation as stated",
            "pan": "PAN of the entity, if printed on the certificate",
            "entity_kind": "'company' or 'llp'",
        },
        "critical": ["entity_name", "registration_number"],
    },
    "bank_proof": {
        "label": "Bank proof (cancelled cheque / bank letter)",
        "fields": {
            "account_holder": "Account holder name as printed (on a cheque: the name under 'For ...' / signatory line)",
            "account_number": "Full bank account number exactly as printed, keeping leading zeros",
            "ifsc": "11-character IFSC",
            "bank_name": "Name of the bank",
            "branch": "Branch name",
            "proof_kind": "'cancelled_cheque', 'bank_letter' or 'bank_statement'",
            "document_date": "Date on the letter/statement, if any",
        },
        "critical": ["account_holder", "account_number", "ifsc"],
    },
    "msme_certificate": {
        "label": "Udyam (MSME) registration certificate",
        "fields": {
            "udyam_number": "Udyam registration number (UDYAM-XX-00-0000000)",
            "enterprise_name": "Name of enterprise",
            "enterprise_category": "Micro, Small or Medium",
            "organisation_type": "Type of organisation",
            "registration_date": "Date of Udyam registration",
        },
        "critical": ["udyam_number", "enterprise_name"],
    },
}

DETECTABLE_TYPES = list(TEMPLATES) + ["other"]

IDENTIFIER_FIELDS = {"pan", "gstin", "registration_number", "account_number", "ifsc", "udyam_number"}


def template_fields(doc_type: str) -> dict[str, str]:
    return TEMPLATES.get(doc_type, {}).get("fields", {})
