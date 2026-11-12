"""The common vendor submission (what the vendor declares in the portal form).

Every field is optional at the schema level on purpose: an incomplete
submission is accepted and then *flagged* by policy checks, rather than being
bounced at the door.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

YesNo = Literal["yes", "no"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Address(_M):
    line1: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None


class Business(_M):
    legal_name: str | None = None
    trade_name: str | None = None
    entity_type: Literal["private_limited", "llp", "other"] | None = None
    registration_number: str | None = None  # CIN or LLPIN
    incorporation_date: str | None = None
    registered_address: Address = Address()


class Service(_M):
    category: str | None = None
    description: str | None = None
    onsite_work: YesNo | None = None
    buyer_contact: str | None = None


class Contact(_M):
    name: str | None = None
    role: str | None = None
    email: str | None = None
    phone: str | None = None
    signatory_name: str | None = None
    signatory_designation: str | None = None


class Tax(_M):
    pan: str | None = None
    gst_registered: YesNo | None = None
    gstin: str | None = None
    msme_registered: YesNo | None = None
    udyam_number: str | None = None


class Bank(_M):
    account_holder: str | None = None
    account_number: str | None = None
    ifsc: str | None = None
    bank_name: str | None = None
    branch: str | None = None


class Declarations(_M):
    information_correct: bool | None = None
    conduct_acceptance: Literal["accepted", "declined"] | None = None  # None = unanswered
    conflict_of_interest: Literal["none", "disclosed"] | None = None
    conflict_details: str | None = None


class SubmissionForm(_M):
    business: Business = Business()
    service: Service = Service()
    contact: Contact = Contact()
    tax: Tax = Tax()
    bank: Bank = Bank()
    declarations: Declarations = Declarations()


DOCUMENT_TYPES: dict[str, str] = {
    "pan_card": "PAN card / e-PAN (business)",
    "gst_certificate": "GST registration certificate (REG-06)",
    "incorporation_certificate": "Certificate of incorporation (company / LLP)",
    "bank_proof": "Bank proof (cancelled cheque or bank letter)",
    "msme_certificate": "Udyam (MSME) registration certificate",
    "supporting_document": "Other supporting document",
}

SERVICE_CATEGORIES = [
    "IT services & software", "Facility management", "Logistics & transport", "Marketing & advertising",
    "Professional services (legal, audit, consulting)", "Staffing & manpower", "Other services",
]


def _paths(model: type[BaseModel], prefix: str = "") -> list[str]:
    out: list[str] = []
    for name, f in model.model_fields.items():
        ann = f.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            out.extend(_paths(ann, f"{prefix}{name}."))
        else:
            out.append(f"{prefix}{name}")
    return out


KNOWN_FIELDS: list[str] = _paths(SubmissionForm)


def field_type(path: str) -> tuple[type, set | None]:
    """(base type, allowed values or None) for a form field path, e.g. tax.msme_registered -> (str, {'yes', 'no'})."""
    import types
    import typing

    model: type[BaseModel] = SubmissionForm
    ann: Any = None
    for part in path.split("."):
        ann = model.model_fields[part].annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            model = ann
    args = [a for a in typing.get_args(ann) if a is not type(None)] if typing.get_origin(ann) in (typing.Union, types.UnionType) else [ann]
    base = args[0] if args else ann
    if typing.get_origin(base) is Literal:
        vals = set(typing.get_args(base))
        return type(next(iter(vals))), vals
    return base, None


def get_field(form: dict, path: str) -> Any:
    cur: Any = form
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    if isinstance(cur, str) and not cur.strip():
        return None
    return cur


FIELD_LABELS: dict[str, str] = {
    "business.legal_name": "Legal name", "business.trade_name": "Trade name",
    "business.entity_type": "Entity type", "business.registration_number": "CIN / LLPIN",
    "business.incorporation_date": "Date of incorporation",
    "business.registered_address.line1": "Registered address", "business.registered_address.city": "City",
    "business.registered_address.state": "State", "business.registered_address.pincode": "PIN code",
    "service.category": "Service category", "service.description": "Service description",
    "service.onsite_work": "On-site work", "service.buyer_contact": "Buyer contact",
    "contact.name": "Contact name", "contact.role": "Contact role", "contact.email": "Contact email",
    "contact.phone": "Contact phone", "contact.signatory_name": "Authorised signatory",
    "contact.signatory_designation": "Signatory designation",
    "tax.pan": "PAN", "tax.gst_registered": "GST registered?", "tax.gstin": "GSTIN",
    "tax.msme_registered": "MSME registered?", "tax.udyam_number": "Udyam number",
    "bank.account_holder": "Account holder name", "bank.account_number": "Account number",
    "bank.ifsc": "IFSC", "bank.bank_name": "Bank name", "bank.branch": "Branch",
    "declarations.information_correct": "Information-correct declaration",
    "declarations.conduct_acceptance": "Supplier code of conduct",
    "declarations.conflict_of_interest": "Conflict of interest", "declarations.conflict_details": "Conflict details",
}
