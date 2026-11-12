"""Generate synthetic Indian vendor-onboarding fixtures.

Every company, bank, person and identifier here is fictional. Identifiers are
format-valid (GSTIN check characters are computed) so the deterministic checks
are meaningful. Every page carries a SYNTHETIC TEST DOCUMENT watermark and no
government emblems or real bank branding.

Gold labels (expected extraction values) come from this script's own data,
never from model output.

Usage:  python fixtures/generate.py
"""

from __future__ import annotations

import io
import json
import random
import shutil
import sys
from pathlib import Path

import pymupdf
from PIL import Image, ImageFilter
from reportlab.lib.colors import Color, black, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.india.validators import gstin_check_char  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
W, H = A4
GREY = Color(0.35, 0.35, 0.35)
LIGHT = Color(0.93, 0.94, 0.96)
ACCENT = Color(0.12, 0.25, 0.55)


def gstin(state: str, pan: str, entity: str = "1") -> str:
    first14 = f"{state}{pan}{entity}Z"
    return first14 + gstin_check_char(first14)


# ----------------------------------------------------------------------------- drawing helpers

def _canvas(path: Path, title: str) -> canvas.Canvas:
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(title)
    c.setAuthor("Synthetic test fixture")
    c.setSubject("Synthetic document for software testing; not issued by any authority")
    return c


def _chrome(c: canvas.Canvas):
    c.saveState()
    c.setFillColor(Color(0.8, 0.1, 0.1, alpha=0.10))
    c.setFont("Helvetica-Bold", 46)
    c.translate(W / 2, H / 2)
    c.rotate(35)
    c.drawCentredString(0, 0, "SYNTHETIC TEST DOCUMENT")
    c.restoreState()
    c.setFont("Helvetica", 7)
    c.setFillColor(GREY)
    c.drawCentredString(W / 2, 12 * mm, "Synthetic specimen generated for software testing. Not issued by any authority or bank.")
    c.setFillColor(black)


def _rows(c: canvas.Canvas, x: float, y: float, rows: list[tuple[str, str]], label_w: float = 70 * mm,
          size: int = 10, gap: float = 8 * mm) -> float:
    for label, value in rows:
        c.setFont("Helvetica", size - 1)
        c.setFillColor(GREY)
        c.drawString(x, y, label)
        c.setFillColor(black)
        c.setFont("Helvetica-Bold", size)
        for i, line in enumerate(_wrap(value, 58)):
            c.drawString(x + label_w, y - i * 4.6 * mm, line)
        y -= gap + max(0, len(_wrap(value, 58)) - 1) * 4.6 * mm
    return y


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines.append(cur)
    return lines or [""]


def _para(c: canvas.Canvas, x: float, y: float, text: str, size: int = 11, width: int = 88, leading: float = 6 * mm):
    c.setFont("Helvetica", size)
    for line in _wrap(text, width):
        c.drawString(x, y, line)
        y -= leading
    return y


# ----------------------------------------------------------------------------- documents

def pan_card(path: Path, *, pan: str, name: str, date_label: str, date: str, father: str | None = None):
    c = _canvas(path, "PAN card (synthetic)")
    _chrome(c)
    x0, y0, cw, ch = 30 * mm, H - 120 * mm, 150 * mm, 92 * mm
    c.setFillColor(LIGHT)
    c.roundRect(x0, y0, cw, ch, 6 * mm, stroke=1, fill=1)
    c.setFillColor(ACCENT)
    c.rect(x0, y0 + ch - 16 * mm, cw, 16 * mm, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(x0 + 8 * mm, y0 + ch - 10.5 * mm, "PERMANENT ACCOUNT NUMBER CARD  (synthetic specimen)")
    c.setFillColor(black)
    y = y0 + ch - 28 * mm
    rows = [("Permanent Account Number", pan), ("Name", name)]
    if father:
        rows.append(("Father's Name", father))
    rows.append((date_label, date))
    _rows(c, x0 + 8 * mm, y, rows, label_w=58 * mm, size=11, gap=11 * mm)
    c.setFont("Helvetica", 8)
    c.setFillColor(GREY)
    c.drawString(x0 + 8 * mm, y0 + 6 * mm, "Signature / seal intentionally omitted on synthetic specimen")
    c.save()


def gst_certificate(path: Path, *, gstin_value: str, legal_name: str, trade_name: str, constitution: str,
                    address: str, liability_date: str):
    c = _canvas(path, "GST registration certificate (synthetic)")
    _chrome(c)
    c.setFont("Helvetica-Bold", 15)
    c.drawCentredString(W / 2, H - 30 * mm, "Form GST REG-06  —  Registration Certificate")
    c.setFont("Helvetica", 9)
    c.setFillColor(GREY)
    c.drawCentredString(W / 2, H - 36 * mm, "Synthetic specimen in the layout of a goods and services tax registration certificate")
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(25 * mm, H - 52 * mm, f"Registration Number: {gstin_value}")
    rows = [
        ("1. Legal Name", legal_name),
        ("2. Trade Name, if any", trade_name or "—"),
        ("3. Constitution of Business", constitution),
        ("4. Address of Principal Place of Business", address),
        ("5. Date of Liability", liability_date),
        ("6. Period of Validity", f"From {liability_date}  To  Not Applicable"),
        ("7. Type of Registration", "Regular"),
        ("8. Particulars of Approving Authority", "Synthetic Range Office (fictional)"),
    ]
    _rows(c, 25 * mm, H - 66 * mm, rows, label_w=72 * mm, size=10, gap=10 * mm)
    c.setFont("Helvetica", 9)
    c.drawString(25 * mm, 60 * mm, "Date of issue of certificate: " + liability_date)
    c.save()


def incorporation_certificate(path: Path, *, name: str, kind: str, reg_no: str, date_words: str, pan: str | None,
                              state: str):
    c = _canvas(path, "Certificate of incorporation (synthetic)")
    _chrome(c)
    c.setFont("Helvetica-Bold", 16)
    title = "Certificate of Incorporation" if kind == "company" else "Certificate of Incorporation of Limited Liability Partnership"
    c.drawCentredString(W / 2, H - 40 * mm, title)
    c.setFont("Helvetica", 9)
    c.setFillColor(GREY)
    c.drawCentredString(W / 2, H - 47 * mm, f"Registrar of Companies, {state} (fictional synthetic registry)")
    c.setFillColor(black)
    if kind == "company":
        text = (f"I hereby certify that {name} is incorporated on this {date_words} under the Companies Act, 2013 "
                f"and that the company is limited by shares. The Corporate Identity Number of the company is {reg_no}.")
        if pan:
            text += f" The Permanent Account Number (PAN) of the company is {pan}."
    else:
        text = (f"This is to certify that {name} is incorporated on this {date_words} under the Limited Liability "
                f"Partnership Act, 2008 and that the LLP Identification Number is {reg_no}.")
        if pan:
            text += f" The Permanent Account Number (PAN) of the LLP is {pan}."
    y = _para(c, 25 * mm, H - 70 * mm, text, size=12, width=80, leading=7 * mm)
    c.setFont("Helvetica", 10)
    c.drawString(25 * mm, y - 20 * mm, "Given under my hand (synthetic specimen — no signature).")
    c.save()


def bank_letter(path: Path, *, bank: str, branch: str, holder: str, account: str, ifsc: str, acct_type: str = "Current",
                date: str = "02/11/2026"):
    c = _canvas(path, "Bank account confirmation letter (synthetic)")
    _chrome(c)
    _bank_letter_body(c, bank=bank, branch=branch, holder=holder, account=account, ifsc=ifsc, acct_type=acct_type, date=date)
    c.save()


def _bank_letter_body(c, *, bank, branch, holder, account, ifsc, acct_type, date):
    c.setFillColor(ACCENT)
    c.rect(0, H - 28 * mm, W, 28 * mm, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(25 * mm, H - 17 * mm, bank)
    c.setFont("Helvetica", 9)
    c.drawString(25 * mm, H - 23 * mm, f"{branch} Branch  ·  fictional bank for software testing")
    c.setFillColor(black)
    c.setFont("Helvetica", 10)
    c.drawRightString(W - 25 * mm, H - 40 * mm, f"Date: {date}")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(25 * mm, H - 55 * mm, "TO WHOMSOEVER IT MAY CONCERN")
    y = _para(c, 25 * mm, H - 68 * mm,
              f"This is to confirm that {holder} maintains a {acct_type.lower()} account with our {branch} branch. "
              "The account details are given below for the purpose of vendor registration.", size=11)
    _rows(c, 25 * mm, y - 6 * mm, [("Account holder name", holder), ("Account number", account),
                                   ("Account type", acct_type), ("IFSC", ifsc), ("Branch", branch)],
          label_w=55 * mm, size=11, gap=9 * mm)
    c.setFont("Helvetica", 10)
    c.drawString(25 * mm, 70 * mm, "Branch Manager (synthetic specimen — no signature)")


def cancelled_cheque(path: Path, *, bank: str, branch: str, holder: str, account: str, ifsc: str,
                     blot_digits: bool = False):
    c = _canvas(path, "Cancelled cheque (synthetic)")
    _chrome(c)
    _cheque(c, bank=bank, branch=branch, holder=holder, account=account, ifsc=ifsc, blot_digits=blot_digits)
    c.save()


def _cheque(c, *, bank, branch, holder, account, ifsc, blot_digits=False, y0=None):
    x0, cw, ch = 15 * mm, W - 30 * mm, 80 * mm
    y0 = y0 if y0 is not None else H - 115 * mm
    c.setFillColor(Color(0.94, 0.97, 0.94))
    c.rect(x0, y0, cw, ch, stroke=1, fill=1)
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(x0 + 6 * mm, y0 + ch - 10 * mm, bank)
    c.setFont("Helvetica", 9)
    c.drawString(x0 + 6 * mm, y0 + ch - 15 * mm, f"{branch} Branch    IFSC: {ifsc}")
    c.drawRightString(x0 + cw - 6 * mm, y0 + ch - 10 * mm, "Date  __ __ / __ __ / __ __ __ __")
    c.drawString(x0 + 6 * mm, y0 + ch - 27 * mm, "Pay ____________________________________________________  or Bearer")
    c.drawString(x0 + 6 * mm, y0 + ch - 36 * mm, "Rupees __________________________________________   ₹ [__________]".replace("₹", "Rs"))
    c.setFont("Helvetica", 9)
    c.drawString(x0 + 6 * mm, y0 + ch - 47 * mm, "A/c No.")
    c.setFont("Courier-Bold", 14)
    c.drawString(x0 + 22 * mm, y0 + ch - 47.5 * mm, account)
    if blot_digits:  # ink blot over digits 6-11 of the account number (realistic unreadable scan)
        c.setFillColor(Color(0.12, 0.12, 0.16))
        char_w = 0.6 * 14  # Courier advance width at 14pt
        bx = x0 + 22 * mm + char_w * 8.5
        for dx, dy, r in [(0, 0, 3.4), (3.0, 1.0, 2.8), (-3.2, -0.6, 2.8), (6.0, -0.8, 2.4), (-6.0, 0.8, 2.2), (1.0, 2.4, 2.0)]:
            c.circle(bx + dx * mm, y0 + ch - 46.5 * mm + dy * mm, r * mm, stroke=0, fill=1)
        c.setFillColor(black)
    c.setFont("Helvetica", 9)
    c.drawRightString(x0 + cw - 6 * mm, y0 + 16 * mm, f"For {holder}")
    c.drawRightString(x0 + cw - 6 * mm, y0 + 8 * mm, "Authorised Signatory")
    c.setFont("Courier", 11)
    c.drawString(x0 + 30 * mm, y0 + 3 * mm, '"000117"  560240012:  ' + account[-6:] + '"  29')
    c.saveState()
    c.setStrokeColor(Color(0.7, 0.05, 0.05))
    c.setLineWidth(2)
    # Crossing lines and stamp kept clear of the A/c No. line so the clean cheque stays readable.
    c.line(x0 + 95 * mm, y0 + 20 * mm, x0 + cw - 25 * mm, y0 + ch - 18 * mm)
    c.line(x0 + 103 * mm, y0 + 20 * mm, x0 + cw - 17 * mm, y0 + ch - 18 * mm)
    c.setFillColor(Color(0.7, 0.05, 0.05))
    c.setFont("Helvetica-Bold", 26)
    c.translate(x0 + cw * 0.72, y0 + ch * 0.50)
    c.rotate(28)
    c.drawCentredString(0, 0, "CANCELLED")
    c.restoreState()


def udyam_certificate(path: Path, *, number: str, name: str, org_type: str, category: str, activity: str,
                      reg_date: str, state: str):
    c = _canvas(path, "Udyam registration certificate (synthetic)")
    _chrome(c)
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(W / 2, H - 35 * mm, "UDYAM REGISTRATION CERTIFICATE")
    c.setFont("Helvetica", 9)
    c.setFillColor(GREY)
    c.drawCentredString(W / 2, H - 41 * mm, "Synthetic specimen in the layout of an MSME (Udyam) registration certificate")
    c.setFillColor(black)
    _rows(c, 25 * mm, H - 60 * mm, [
        ("Udyam Registration Number", number), ("Name of Enterprise", name), ("Type of Organisation", org_type),
        ("Type of Enterprise", category), ("Major Activity", activity), ("State", state),
        ("Date of Udyam Registration", reg_date)], label_w=70 * mm, size=11, gap=10 * mm)
    c.save()


# ----------------------------------------------------------------------------- scan simulation

def scanify(src: Path, dst: Path, *, seed: int = 7, dpi: int = 110, angle: float = 0.9, blur: float = 0.6):
    """Turn a digital PDF into an image-only 'scanned' PDF (no text layer)."""
    rnd = random.Random(seed)
    out = pymupdf.open()
    with pymupdf.open(src) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
            img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            img = img.rotate(angle * (1 if rnd.random() > 0.5 else -1), fillcolor=255, expand=False)
            noise = Image.effect_noise(img.size, 28).point(lambda v: 255 if v > 120 else 225)
            img = Image.blend(img, noise, 0.12).filter(ImageFilter.GaussianBlur(blur))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=62)
            p = out.new_page(width=page.rect.width, height=page.rect.height)
            p.insert_image(p.rect, stream=buf.getvalue())
    out.save(dst, garbage=3, deflate=True)
    out.close()


def merge(paths: list[Path], dst: Path):
    out = pymupdf.open()
    for p in paths:
        with pymupdf.open(p) as d:
            out.insert_pdf(d)
    out.save(dst)
    out.close()


# ----------------------------------------------------------------------------- vendors

def vendors() -> dict:
    v = {}
    v["nimbus"] = dict(
        legal_name="Nimbus Ledger Solutions Private Limited", trade_name="", entity_type="private_limited",
        cin="U72200KA2019PTC128734", inc_date="14/03/2019", inc_words="14th day of March 2019",
        pan="AAHCN7316M", state_code="29", state="Karnataka",
        address=dict(line1="4th Floor, 18 Residency Road", city="Bengaluru", state="Karnataka", pincode="560025"),
        bank=dict(bank="Deccan Commercial Bank", branch="Residency Road, Bengaluru", holder="NIMBUS LEDGER SOLUTIONS PRIVATE LIMITED",
                  account="000412300045821", ifsc="DECB0000412"),
        udyam="UDYAM-KA-03-0045127", category="IT services & software",
        contact=dict(name="Ananya Rao", role="Finance Manager", email="ananya.rao@nimbusledger.example", phone="+91 80 4000 1122",
                     signatory_name="Vikram Shetty", signatory_designation="Director"),
    )
    v["tejas"] = dict(
        legal_name="Tejas Facility Services Private Limited", trade_name="Tejas Cleanpro", entity_type="private_limited",
        cin="U81210MH2017PTC294518", inc_date="09/06/2017", inc_words="9th day of June 2017",
        pan="AACCT5520R", state_code="27", state="Maharashtra",
        address=dict(line1="Unit 7, Lakeview Industrial Estate, Baner", city="Pune", state="Maharashtra", pincode="411045"),
        bank=dict(bank="Konkan Union Bank", branch="Baner, Pune", holder="TEJAS CLEANPRO", account="50200011223344",
                  ifsc="KONB0001187"),
        category="Facility management",
        contact=dict(name="Meera Kulkarni", role="Operations Head", email="meera@tejascleanpro.example", phone="+91 20 6600 4411",
                     signatory_name="Prakash Joshi", signatory_designation="Managing Director"),
    )
    v["saffron"] = dict(
        legal_name="Saffron Route Logistics Private Limited", trade_name="", entity_type="private_limited",
        cin="U60200GJ2020PTC115263", inc_date="21/09/2020", inc_words="21st day of September 2020",
        pan="AAPCS2291H", director_pan="BQRPM4471K", director_name="ROHAN MEHTA", director_father="SURESH MEHTA",
        director_dob="12/08/1986", state_code="24", state="Gujarat",
        address=dict(line1="Plot 41, Sanand GIDC Phase II", city="Ahmedabad", state="Gujarat", pincode="382110"),
        bank=dict(bank="Sabarmati Mercantile Bank", branch="Sanand, Ahmedabad", holder="SAFFRON ROUTE LOGISTICS PRIVATE LIMITED",
                  account="1204005566778", ifsc="SBMB0000318"),
        category="Logistics & transport",
        contact=dict(name="Rohan Mehta", role="Director", email="rohan@saffronroute.example", phone="+91 79 2900 7788",
                     signatory_name="Rohan Mehta", signatory_designation="Director"),
    )
    v["indigo"] = dict(
        legal_name="Indigo Pixel Studios LLP", trade_name="", entity_type="llp",
        cin="AAQ-9921", inc_date="03/02/2021", inc_words="3rd day of February 2021",
        pan="AAKFI6604B", state_code="27", state="Maharashtra",
        address=dict(line1="2nd Floor, Kamala Mills Compound, Lower Parel", city="Mumbai", state="Maharashtra", pincode="400013"),
        bank=dict(bank="Sahyadri Commercial Bank", branch="Lower Parel, Mumbai", holder="INDIGO PIXEL STUDIOS LLP",
                  account="091100078214821", ifsc="SAHB0000231"),
        bank_new=dict(bank="Konkan Union Bank", branch="Worli, Mumbai", holder="INDIGO PIXEL STUDIOS LLP",
                      account="337700019027390", ifsc="KONB0000562"),
        category="Marketing & advertising",
        contact=dict(name="Kabir Fernandes", role="Partner", email="kabir@indigopixel.example", phone="+91 22 6100 3030",
                     signatory_name="Kabir Fernandes", signatory_designation="Designated Partner"),
    )
    v["harbourline"] = dict(
        legal_name="Harbourline Analytics Private Limited", trade_name="", entity_type="private_limited",
        cin="U72900TG2018PTC127781", inc_date="17/07/2018", inc_words="17th day of July 2018",
        pan="AADCH3349P", other_pan="AAKCS8812D", other_legal_name="SEAVIEW DATA CONSULTANTS PRIVATE LIMITED",
        state_code="36", state="Telangana",
        address=dict(line1="Plot 12, Cyber Towers Road, Madhapur", city="Hyderabad", state="Telangana", pincode="500081"),
        bank=dict(bank="Deccan Commercial Bank", branch="Madhapur, Hyderabad", holder="HARBOURLINE ANALYTICS PRIVATE LIMITED",
                  account="000735100082214", ifsc="DECB0000735"),
        category="IT services & software",
        contact=dict(name="Sana Qureshi", role="Accounts Lead", email="sana@harbourline.example", phone="+91 40 4455 9900",
                     signatory_name="Arjun Reddy", signatory_designation="Director"),
    )
    v["kaveri"] = dict(
        legal_name="Kaveri Cloudworks LLP", trade_name="", entity_type="llp",
        cin="AAK-4417", inc_date="28/11/2019", inc_words="28th day of November 2019",
        pan="AAPFK9035C", state_code="33", state="Tamil Nadu",
        address=dict(line1="No. 88, Rajiv Gandhi Salai, Taramani", city="Chennai", state="Tamil Nadu", pincode="600113"),
        bank=dict(bank="Coromandel Trust Bank", branch="Taramani, Chennai", holder="KAVERI CLOUDWORKS LLP",
                  account="004420019935107", ifsc="CTBK0000442"),
        udyam="UDYAM-TN-02-0078812", category="IT services & software",
        contact=dict(name="Divya Natarajan", role="Partner", email="divya@kavericloud.example", phone="+91 44 4800 2200",
                     signatory_name="Divya Natarajan", signatory_designation="Designated Partner"),
    )
    for key, d in v.items():
        d["gstin"] = gstin(d["state_code"], d.get("other_pan") or d["pan"])
    return v


def form_for(d: dict, *, pan: str | None = None, bank: dict | None = None, msme: str = "no", conduct: str | None = "accepted",
             gst_registered: str = "yes") -> dict:
    b = bank or d["bank"]
    return {
        "business": {"legal_name": d["legal_name"], "trade_name": d.get("trade_name") or None,
                     "entity_type": d["entity_type"], "registration_number": d["cin"],
                     "incorporation_date": d["inc_date"], "registered_address": d["address"]},
        "service": {"category": d["category"], "description": f"{d['category']} for buyer sites and teams",
                    "onsite_work": "yes" if d["category"] == "Facility management" else "no",
                    "buyer_contact": "Procurement — Services"},
        "contact": d["contact"],
        "tax": {"pan": pan or d["pan"], "gst_registered": gst_registered,
                "gstin": d["gstin"] if gst_registered == "yes" else None,
                "msme_registered": msme, "udyam_number": d.get("udyam") if msme == "yes" else None},
        "bank": {"account_holder": b["holder"], "account_number": b["account"], "ifsc": b["ifsc"],
                 "bank_name": b["bank"], "branch": b["branch"]},
        "declarations": {"information_correct": True, "conduct_acceptance": conduct,
                         "conflict_of_interest": "none", "conflict_details": None},
    }


def constitution(d):
    return "Private Limited Company" if d["entity_type"] == "private_limited" else "Limited Liability Partnership"


def addr_line(d):
    a = d["address"]
    return f"{a['line1']}, {a['city']}, {a['state']} - {a['pincode']}"


# ----------------------------------------------------------------------------- build

def build():
    if OUT.exists():
        shutil.rmtree(OUT)
    docs = OUT / "documents"
    docs.mkdir(parents=True)
    V = vendors()
    gold: dict[str, dict] = {}
    tmp = OUT / "_tmp"
    tmp.mkdir()

    def rec(name: str, dtype: str, fields: dict, unreadable: tuple = (), page: int = 1):
        """Gold label for one document, keyed by the extraction template's field names."""
        gold[name] = {"type": dtype, "page": page, "unreadable": list(unreadable),
                      "fields": {k: (v if v not in ("", None) else None) for k, v in fields.items()}}

    def pan_fields(pan, holder, date, father=None):
        return {"pan": pan, "holder_name": holder, "father_name": father, "date": date}

    def bank_fields(b, kind, date=None):
        return {"account_holder": b["holder"], "account_number": b["account"], "ifsc": b["ifsc"], "bank_name": b["bank"],
                "branch": b["branch"], "proof_kind": kind, "document_date": date}

    def std_docs(key: str, d: dict, *, gst_legal=None, gst_trade=None):
        pan_card(docs / f"{key}_pan.pdf", pan=d["pan"], name=d["legal_name"].upper(),
                 date_label="Date of Incorporation/Formation", date=d["inc_date"])
        rec(f"{key}_pan.pdf", "pan_card", pan_fields(d["pan"], d["legal_name"].upper(), d["inc_date"]))
        legal = (gst_legal or d["legal_name"]).upper()
        trade = (gst_trade if gst_trade is not None else d.get("trade_name") or "").upper()
        gst_certificate(docs / f"{key}_gst.pdf", gstin_value=d["gstin"], legal_name=legal, trade_name=trade,
                        constitution=constitution(d), address=addr_line(d), liability_date="01/07/2021")
        rec(f"{key}_gst.pdf", "gst_certificate", {
            "gstin": d["gstin"], "legal_name": legal, "trade_name": trade, "constitution": constitution(d),
            "principal_address": addr_line(d), "address_state": d["state"], "date_of_liability": "01/07/2021",
            "registration_type": "Regular"})
        kind = "company" if d["entity_type"] == "private_limited" else "llp"
        incorporation_certificate(docs / f"{key}_incorporation.pdf", name=d["legal_name"].upper(), kind=kind,
                                  reg_no=d["cin"], date_words=d["inc_words"], pan=d["pan"], state=d["state"])
        rec(f"{key}_incorporation.pdf", "incorporation_certificate", {
            "entity_name": d["legal_name"].upper(), "registration_number": d["cin"], "incorporation_date": d["inc_words"],
            "pan": d["pan"], "entity_kind": kind})

    # 1 nimbus — happy path (digital docs, MSME claimed with certificate)
    d = V["nimbus"]
    std_docs("nimbus", d)
    bank_letter(docs / "nimbus_bank_letter.pdf", **d["bank"])
    rec("nimbus_bank_letter.pdf", "bank_proof", bank_fields(d["bank"], "bank_letter", "02/11/2026"))
    udyam_certificate(docs / "nimbus_udyam.pdf", number=d["udyam"], name=d["legal_name"].upper(), org_type="Private Limited Company",
                      category="Small", activity="Services", reg_date="05/01/2022", state=d["state"])
    rec("nimbus_udyam.pdf", "msme_certificate", {"udyam_number": d["udyam"], "enterprise_name": d["legal_name"].upper(),
                                                 "enterprise_category": "Small", "organisation_type": "Private Limited Company",
                                                 "registration_date": "05/01/2022"})

    # 2 tejas — bank account held in the trade name; GST certificate lists that trade name
    d = V["tejas"]
    std_docs("tejas", d)
    gst_certificate(docs / "tejas_gst_no_trade_name.pdf", gstin_value=d["gstin"], legal_name=d["legal_name"].upper(), trade_name="",
                    constitution=constitution(d), address=addr_line(d), liability_date="01/07/2021")
    rec("tejas_gst_no_trade_name.pdf", "gst_certificate", {**gold["tejas_gst.pdf"]["fields"], "trade_name": None})
    cancelled_cheque(docs / "tejas_cheque.pdf", **d["bank"])
    rec("tejas_cheque.pdf", "bank_proof", bank_fields(d["bank"], "cancelled_cheque"))

    # 3 saffron — director's personal PAN uploaded instead of the company PAN
    d = V["saffron"]
    std_docs("saffron", d)
    pan_card(docs / "saffron_director_pan.pdf", pan=d["director_pan"], name=d["director_name"], father=d["director_father"],
             date_label="Date of Birth", date=d["director_dob"])
    rec("saffron_director_pan.pdf", "pan_card", pan_fields(d["director_pan"], d["director_name"], d["director_dob"],
                                                           d["director_father"]))
    bank_letter(docs / "saffron_bank_letter.pdf", **d["bank"])
    rec("saffron_bank_letter.pdf", "bank_proof", bank_fields(d["bank"], "bank_letter", "02/11/2026"))

    # 4 indigo — correction (missing LLP certificate) + silent bank change
    d = V["indigo"]
    std_docs("indigo", d)
    cancelled_cheque(docs / "indigo_cheque_old.pdf", **d["bank"])
    cancelled_cheque(docs / "indigo_cheque_new.pdf", **d["bank_new"])
    rec("indigo_cheque_old.pdf", "bank_proof", bank_fields(d["bank"], "cancelled_cheque"))
    rec("indigo_cheque_new.pdf", "bank_proof", bank_fields(d["bank_new"], "cancelled_cheque"))

    # 5 harbourline — GSTIN belongs to a different legal entity
    d = V["harbourline"]
    std_docs("harbourline", d, gst_legal=d["other_legal_name"], gst_trade="Harbourline Analytics")
    bank_letter(docs / "harbourline_bank_letter.pdf", **d["bank"])
    rec("harbourline_bank_letter.pdf", "bank_proof", bank_fields(d["bank"], "bank_letter", "02/11/2026"))

    # 6 kaveri — scanned PAN, mixed bank proof (letter + scanned cheque), blotted variant
    d = V["kaveri"]
    std_docs("kaveri", d)
    scanify(docs / "kaveri_pan.pdf", docs / "kaveri_pan_scanned.pdf", seed=3)
    rec("kaveri_pan_scanned.pdf", "pan_card", gold["kaveri_pan.pdf"]["fields"])
    bank_letter(tmp / "kaveri_letter.pdf", **d["bank"])
    cancelled_cheque(tmp / "kaveri_cheque.pdf", **d["bank"])
    scanify(tmp / "kaveri_cheque.pdf", tmp / "kaveri_cheque_scan.pdf", seed=5)
    merge([tmp / "kaveri_letter.pdf", tmp / "kaveri_cheque_scan.pdf"], docs / "kaveri_bank_mixed.pdf")
    rec("kaveri_bank_mixed.pdf", "bank_proof", bank_fields(d["bank"], "bank_letter", "02/11/2026"))
    cancelled_cheque(tmp / "kaveri_cheque_blot.pdf", **d["bank"], blot_digits=True)
    scanify(tmp / "kaveri_cheque_blot.pdf", docs / "kaveri_cheque_unreadable_scan.pdf", seed=9)
    rec("kaveri_cheque_unreadable_scan.pdf", "bank_proof", {**bank_fields(d["bank"], "cancelled_cheque"), "account_number": None},
        unreadable=("account_number",))

    # Intake failure fixtures
    raw = (docs / "nimbus_pan.pdf").read_bytes()
    (docs / "broken_truncated.pdf").write_bytes(raw[: len(raw) // 3])
    with pymupdf.open(docs / "nimbus_pan.pdf") as locked:
        locked.save(docs / "locked_password.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="vendor123")
    (docs / "not_a_pdf.pdf").write_bytes(b"PK\x03\x04 this is actually a zip/docx, renamed to .pdf")
    shutil.rmtree(tmp)

    # ------------------------------------------------------------------ scenarios (form + slot -> file)
    S = {}
    n, t, s, i, h, k = (V[x] for x in ("nimbus", "tejas", "saffron", "indigo", "harbourline", "kaveri"))
    S["happy_nimbus"] = dict(
        title="Happy path — Nimbus Ledger Solutions Pvt Ltd", vendor="nimbus",
        form=form_for(n, msme="yes"),
        documents={"pan_card": "nimbus_pan.pdf", "gst_certificate": "nimbus_gst.pdf",
                   "incorporation_certificate": "nimbus_incorporation.pdf", "bank_proof": "nimbus_bank_letter.pdf",
                   "msme_certificate": "nimbus_udyam.pdf"},
        expect="All checks pass. Meridian (automatic approval of clean applications): APPROVED automatically. "
               "Kestrel (manual): READY_FOR_DECISION → reviewer approves")
    S["trade_name_supported"] = dict(
        title="Edge 1 — Bank account in trade name (GST certificate supports it)", vendor="tejas",
        form=form_for(t),
        documents={"pan_card": "tejas_pan.pdf", "gst_certificate": "tejas_gst.pdf",
                   "incorporation_certificate": "tejas_incorporation.pdf", "bank_proof": "tejas_cheque.pdf"},
        expect="Needs reviewer judgement with GST trade-name evidence shown → reviewer accepts → READY")
    S["trade_name_unsupported"] = dict(
        title="Edge 1b — Bank account in trade name, no supporting evidence", vendor="tejas",
        form={**form_for(t), "business": {**form_for(t)["business"], "trade_name": None}},
        documents={"pan_card": "tejas_pan.pdf", "gst_certificate": "tejas_gst_no_trade_name.pdf",
                   "incorporation_certificate": "tejas_incorporation.pdf", "bank_proof": "tejas_cheque.pdf"},
        expect="ACTION_REQUIRED: vendor asked to evidence the account-holder name")
    S["director_pan"] = dict(
        title="Edge 2 — Director's personal PAN instead of company PAN", vendor="saffron",
        form=form_for(s, pan=s["director_pan"]),
        documents={"pan_card": "saffron_director_pan.pdf", "gst_certificate": "saffron_gst.pdf",
                   "incorporation_certificate": "saffron_incorporation.pdf", "bank_proof": "saffron_bank_letter.pdf"},
        expect="ACTION_REQUIRED: provide company PAN (system points to PAN on incorporation certificate and GSTIN)")
    S["bank_change_rev1"] = dict(
        title="Edge 3 (rev 1) — Indigo Pixel Studios LLP, LLP certificate missing", vendor="indigo",
        form=form_for(i),
        documents={"pan_card": "indigo_pan.pdf", "gst_certificate": "indigo_gst.pdf", "bank_proof": "indigo_cheque_old.pdf"},
        expect="ACTION_REQUIRED: upload certificate of incorporation")
    S["bank_change_rev2"] = dict(
        title="Edge 3 (rev 2) — certificate supplied AND bank account changed", vendor="indigo",
        form=form_for(i, bank=i["bank_new"]),
        documents={"pan_card": "indigo_pan.pdf", "gst_certificate": "indigo_gst.pdf",
                   "incorporation_certificate": "indigo_incorporation.pdf", "bank_proof": "indigo_cheque_new.pdf"},
        expect="Certificate satisfied; bank change detected → finance review; stale approval blocked")
    S["identity_conflict"] = dict(
        title="Edge 4 — GSTIN belongs to a different legal entity", vendor="harbourline",
        form=form_for(h),
        documents={"pan_card": "harbourline_pan.pdf", "gst_certificate": "harbourline_gst.pdf",
                   "incorporation_certificate": "harbourline_incorporation.pdf", "bank_proof": "harbourline_bank_letter.pdf"},
        expect="ACTION_REQUIRED clarification → unresolved → reviewer rejects with reason")
    S["scanned_llp_buyer_b"] = dict(
        title="Scanned + mixed documents, MSME claimed without certificate (Buyer A blocks, Buyer B doesn't)", vendor="kaveri",
        form=form_for(k, msme="yes"),
        documents={"pan_card": "kaveri_pan_scanned.pdf", "gst_certificate": "kaveri_gst.pdf",
                   "incorporation_certificate": "kaveri_incorporation.pdf", "bank_proof": "kaveri_bank_mixed.pdf"},
        expect="Buyer A: ACTION_REQUIRED (MSME proof). Buyer B: not blocked by MSME")
    S["unreadable_account"] = dict(
        title="Scanned cheque with obscured account digits", vendor="kaveri",
        form=form_for(k),
        documents={"pan_card": "kaveri_pan.pdf", "gst_certificate": "kaveri_gst.pdf",
                   "incorporation_certificate": "kaveri_incorporation.pdf", "bank_proof": "kaveri_cheque_unreadable_scan.pdf"},
        expect="Bank proof account number unreadable → unable to confirm → vendor asked for a clearer copy")
    S["conduct_declined"] = dict(
        title="Explicit refusal of supplier code of conduct", vendor="nimbus",
        form=form_for(n, msme="yes", conduct="declined"),
        documents=S["happy_nimbus"]["documents"], expect="REJECTED automatically by policy rule")
    S["conduct_unanswered"] = dict(
        title="Code of conduct left unanswered", vendor="nimbus",
        form=form_for(n, msme="yes", conduct=None),
        documents=S["happy_nimbus"]["documents"], expect="ACTION_REQUIRED: answer the code-of-conduct question (not a rejection)")

    (OUT / "scenarios.json").write_text(json.dumps(S, indent=2), encoding="utf-8")
    (OUT / "gold_labels.json").write_text(json.dumps(gold, indent=2), encoding="utf-8")

    # Simulated registry (exact synthetic identifiers only). Unknown GSTINs => not_found.
    registry = {}
    for key, d in V.items():
        legal = d.get("other_legal_name") or d["legal_name"].upper()
        registry[d["gstin"]] = {"legal_name": legal.upper(), "trade_name": (d.get("trade_name") or "").upper() or None,
                                "status": "Active", "state": d["state"], "constitution": constitution(d),
                                "registered_on": "01/07/2021"}
    registry[V["harbourline"]["gstin"]]["trade_name"] = "HARBOURLINE ANALYTICS"
    (OUT / "simulated_registry.json").write_text(json.dumps({"_note": "SIMULATED registry for synthetic fixtures only",
                                                             "gstin": registry}, indent=2), encoding="utf-8")
    try:  # sandbox sources for the verification adapters, and the GST filing table (fixtures/sandbox.py)
        from fixtures import sandbox
    except ImportError:  # run as a script from this folder
        import sandbox
    sandbox.main()
    print(f"wrote {len(list(docs.glob('*.pdf')))} PDFs, {len(S)} scenarios -> {OUT}")


if __name__ == "__main__":
    build()
