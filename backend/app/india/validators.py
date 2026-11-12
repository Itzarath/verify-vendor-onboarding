"""Deterministic Indian identifier checks.

These checks establish *shape* and *cross-field consistency* only. A well-formed
PAN or GSTIN is not proof that the identifier exists or belongs to the
submitter; that is the job of independent (registry) verification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

_ALNUM36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
CIN_RE = re.compile(r"^([LU])([0-9]{5})([A-Z]{2})([0-9]{4})([A-Z]{3})([0-9]{6})$")
LLPIN_RE = re.compile(r"^[A-Z]{3}-[0-9]{4}$")
UDYAM_RE = re.compile(r"^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$")
ACCOUNT_RE = re.compile(r"^[0-9]{9,18}$")
PINCODE_RE = re.compile(r"^[1-9][0-9]{5}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d")
# Dates as printed on documents (Indian order: day first).
DOCUMENT_DATE_FORMATS = DATE_FORMATS + ("%d %B %Y", "%d %b %Y", "%d-%b-%Y", "%d %b, %Y", "%B %d, %Y", "%b %d, %Y")

# 4th character of a PAN identifies the holder category (Income Tax Department).
PAN_HOLDER_TYPES = {
    "P": "Individual",
    "C": "Company",
    "H": "Hindu Undivided Family",
    "F": "Firm / LLP",
    "A": "Association of Persons",
    "T": "Trust",
    "B": "Body of Individuals",
    "L": "Local Authority",
    "J": "Artificial Juridical Person",
    "G": "Government",
}

# Entity types we support in v1 and the PAN holder category each should carry.
ENTITY_EXPECTED_PAN_TYPE = {"private_limited": "C", "llp": "F"}
ENTITY_LABELS = {"private_limited": "Private limited company", "llp": "Limited liability partnership"}

GST_STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
}

# State segment of a CIN (Registrar of Companies state code).
CIN_STATE_CODES = {
    "AP": "Andhra Pradesh", "AR": "Arunachal Pradesh", "AS": "Assam", "BR": "Bihar", "CH": "Chandigarh",
    "CT": "Chhattisgarh", "DL": "Delhi", "GA": "Goa", "GJ": "Gujarat", "HR": "Haryana",
    "HP": "Himachal Pradesh", "JK": "Jammu and Kashmir", "JH": "Jharkhand", "KA": "Karnataka",
    "KL": "Kerala", "MP": "Madhya Pradesh", "MH": "Maharashtra", "MN": "Manipur", "ML": "Meghalaya",
    "MZ": "Mizoram", "NL": "Nagaland", "OR": "Odisha", "PB": "Punjab", "PY": "Puducherry",
    "RJ": "Rajasthan", "SK": "Sikkim", "TN": "Tamil Nadu", "TG": "Telangana", "TR": "Tripura",
    "UP": "Uttar Pradesh", "UR": "Uttarakhand", "WB": "West Bengal",
}

_STATE_ALIASES = {
    "orissa": "odisha", "pondicherry": "puducherry", "new delhi": "delhi", "nct of delhi": "delhi",
    "uttaranchal": "uttarakhand", "j&k": "jammu and kashmir", "tamilnadu": "tamil nadu",
}

INDIAN_STATES = sorted(set(GST_STATE_CODES.values()))


def clean_identifier(value: str | None) -> str:
    """Uppercase and strip whitespace. Identifiers stay strings (leading zeros matter)."""
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value)).upper()


def canonical_state(name: str | None) -> str | None:
    if not name:
        return None
    key = re.sub(r"\s+", " ", name.strip().lower().replace("&", "and"))
    key = _STATE_ALIASES.get(key, key)
    for state in GST_STATE_CODES.values():
        if state.lower() == key:
            return state
    return None


def gstin_check_char(first14: str) -> str:
    """GSTIN check character (mod-36 weighted sum over the first 14 characters)."""
    total = 0
    for i, ch in enumerate(first14):
        product = _ALNUM36.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return _ALNUM36[(36 - total % 36) % 36]


@dataclass
class IdCheck:
    ok: bool
    reason: str
    facts: dict = field(default_factory=dict)


def check_pan(pan: str | None) -> IdCheck:
    v = clean_identifier(pan)
    if not v:
        return IdCheck(False, "PAN is missing")
    if not PAN_RE.match(v):
        return IdCheck(False, f"'{v}' is not a valid PAN shape (5 letters, 4 digits, 1 letter)")
    holder = PAN_HOLDER_TYPES.get(v[3])
    if holder is None:
        return IdCheck(False, f"PAN 4th character '{v[3]}' is not a recognised holder category")
    return IdCheck(True, f"Valid PAN shape; holder category '{v[3]}' = {holder}", {"holder_code": v[3], "holder_type": holder})


def check_pan_matches_entity(pan: str | None, entity_type: str | None) -> IdCheck:
    v = clean_identifier(pan)
    expected = ENTITY_EXPECTED_PAN_TYPE.get(entity_type or "")
    if not v or not PAN_RE.match(v) or expected is None:
        return IdCheck(False, "Cannot compare PAN holder category with entity type", {"comparable": False})
    actual = v[3]
    facts = {"comparable": True, "expected_code": expected, "actual_code": actual,
             "actual_type": PAN_HOLDER_TYPES.get(actual, "Unknown")}
    if actual == expected:
        return IdCheck(True, f"PAN holder category '{actual}' fits a {ENTITY_LABELS[entity_type]}", facts)
    return IdCheck(
        False,
        f"PAN holder category '{actual}' ({facts['actual_type']}) does not fit a "
        f"{ENTITY_LABELS[entity_type]} (expected '{expected}')",
        facts,
    )


def check_gstin(gstin: str | None) -> IdCheck:
    v = clean_identifier(gstin)
    if not v:
        return IdCheck(False, "GSTIN is missing")
    if not GSTIN_RE.match(v):
        return IdCheck(False, f"'{v}' is not a valid GSTIN shape (15 characters)")
    state = GST_STATE_CODES.get(v[:2])
    if state is None:
        return IdCheck(False, f"GSTIN state code '{v[:2]}' is not a recognised state/UT code")
    expected = gstin_check_char(v[:14])
    if v[14] != expected:
        return IdCheck(False, f"GSTIN check character is '{v[14]}' but should be '{expected}' (likely mistyped)",
                       {"state_code": v[:2], "state": state})
    return IdCheck(True, f"Valid GSTIN; state {v[:2]} = {state}; embedded PAN {v[2:12]}",
                   {"state_code": v[:2], "state": state, "embedded_pan": v[2:12]})


def check_gstin_pan_link(gstin: str | None, pan: str | None) -> IdCheck:
    g, p = clean_identifier(gstin), clean_identifier(pan)
    if not (GSTIN_RE.match(g) and PAN_RE.match(p)):
        return IdCheck(False, "Cannot cross-check GSTIN and PAN", {"comparable": False})
    embedded = g[2:12]
    facts = {"comparable": True, "embedded_pan": embedded, "pan": p}
    if embedded == p:
        return IdCheck(True, f"GSTIN characters 3–12 ({embedded}) match the PAN", facts)
    return IdCheck(False, f"GSTIN embeds PAN {embedded}, but the PAN given is {p}: the GST registration "
                          f"belongs to a different taxpayer", facts)


def check_ifsc(ifsc: str | None) -> IdCheck:
    v = clean_identifier(ifsc)
    if not v:
        return IdCheck(False, "IFSC is missing")
    if not IFSC_RE.match(v):
        return IdCheck(False, f"'{v}' is not a valid IFSC shape (4 letters, 0, 6 characters)")
    return IdCheck(True, f"Valid IFSC shape; bank code {v[:4]}, branch {v[5:]}", {"bank_code": v[:4]})


def check_account_number(acct: str | None) -> IdCheck:
    v = clean_identifier(acct)
    if not v:
        return IdCheck(False, "Account number is missing")
    if not ACCOUNT_RE.match(v):
        return IdCheck(False, "Account number should be 9–18 digits")
    return IdCheck(True, "Account number shape is valid")


def check_registration_number(reg: str | None, entity_type: str | None) -> IdCheck:
    v = clean_identifier(reg)
    if not v:
        return IdCheck(False, "Company/LLP registration number is missing")
    if entity_type == "llp":
        if LLPIN_RE.match(v):
            return IdCheck(True, "Valid LLPIN shape", {"kind": "LLPIN"})
        if CIN_RE.match(v):
            return IdCheck(False, "A company CIN was given, but the entity type is LLP (expected an LLPIN like AAB-1234)")
        return IdCheck(False, f"'{v}' is not a valid LLPIN shape (e.g. AAB-1234)")
    m = CIN_RE.match(v)
    if not m:
        if LLPIN_RE.match(v):
            return IdCheck(False, "An LLPIN was given, but the entity type is a company (expected a 21-character CIN)")
        return IdCheck(False, f"'{v}' is not a valid CIN shape (21 characters)")
    listing, _industry, state_code, year, ownership, _num = m.groups()
    facts = {"kind": "CIN", "state_code": state_code, "state": CIN_STATE_CODES.get(state_code),
             "year": year, "ownership": ownership, "listing": listing}
    if entity_type == "private_limited" and ownership not in {"PTC", "OPC"}:
        return IdCheck(False, f"CIN ownership code '{ownership}' does not indicate a private limited company (PTC)", facts)
    return IdCheck(True, f"Valid CIN shape; ROC state {state_code}, incorporated {year}, class {ownership}", facts)


def check_udyam(num: str | None) -> IdCheck:
    v = clean_identifier(num)
    if not v:
        return IdCheck(False, "Udyam registration number is missing")
    if not UDYAM_RE.match(v):
        return IdCheck(False, f"'{v}' is not a valid Udyam number shape (UDYAM-XX-00-0000000)")
    return IdCheck(True, "Valid Udyam number shape")

# Non-identifier form values: shape only, like the identifier checks above.

def check_email(v: str | None) -> IdCheck:
    v = (v or "").strip()
    if not EMAIL_RE.match(v):
        return IdCheck(False, f"'{v}' is not an email address")
    return IdCheck(True, "Email address shape is valid")


def check_pincode(v: str | None) -> IdCheck:
    c = re.sub(r"\s+", "", v or "")
    if not PINCODE_RE.match(c):
        return IdCheck(False, f"'{(v or '').strip()}' is not an Indian PIN code (6 digits, not starting with 0)")
    return IdCheck(True, "PIN code shape is valid")


def parse_date(v: str | None, formats: tuple[str, ...] = DATE_FORMATS) -> date | None:
    v = " ".join((v or "").replace(",", ", ").split()).replace(" ,", ",")
    for fmt in formats:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def check_past_date(v: str | None, today: date | None = None) -> IdCheck:
    """A real calendar date (DD/MM/YYYY; also DD-MM-YYYY, DD.MM.YYYY or YYYY-MM-DD) that is not in the future."""
    v = (v or "").strip()
    d = parse_date(v)
    if d is None:
        return IdCheck(False, f"'{v}' is not a real date in the form DD/MM/YYYY")
    if d > (today or date.today()):
        return IdCheck(False, f"'{v}' is in the future")
    if d.year < 1850:
        return IdCheck(False, f"'{v}' is too far in the past")
    return IdCheck(True, f"Valid date ({d.isoformat()})", {"iso": d.isoformat()})
