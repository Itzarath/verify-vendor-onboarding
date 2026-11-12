"""Visibility of text-layer values: tightly fitted covers, hidden text elsewhere on a line, and per-file confirmations.
No model calls; the fixture reader and real PDF rendering only.
"""

import pymupdf
import pytest

from app.extraction.contract import ModelField, ModelReading, validate_reading
from app.extraction.service import FIXTURE_DIR
from app.pdf.inspect import HIDDEN_MARK, inspect_pdf, page_texts
from app.pipeline.checks import run_checks
from app.pipeline.compare import compare
from app.pipeline.evidence import DocEvidence, EvalInput, ResolutionRec

from .test_scenarios_logic import SCEN, evidence, policy, registry

ACCOUNT = "000412300045821"
LETTER = (FIXTURE_DIR / "documents" / "nimbus_bank_letter.pdf").read_bytes()


def _verified(data: bytes, value: str) -> bool:
    info = inspect_pdf(data, 10 * 2**20, 10)
    reading = validate_reading(ModelReading(document_type="bank_proof", fields=[
        ModelField(key="account_number", status="found", value=value, page=1)]), [p.kind for p in info.pages],
        page_texts(data))
    return reading.fields["account_number"].provenance == "text_layer_verified"


# ---------------------------------------------------------------------------- a tightly fitted cover

def _covered(colour, box: str) -> bytes:
    """The Nimbus bank letter with its account number's 7th digit under a box drawn exactly on that character's box
    (as the text trace or as rawdict reports it)."""
    doc = pymupdf.open(stream=LETTER, filetype="pdf")
    page = doc[0]
    if box == "trace":
        span = next(s for s in page.get_texttrace() if "".join(chr(c[0]) for c in s["chars"]) == ACCOUNT)
        rect = pymupdf.Rect(span["chars"][6][3])
    else:
        chars = [c for b in page.get_text("rawdict")["blocks"] for line in b.get("lines", []) for s in line["spans"]
                 for c in s["chars"]]
        rect = pymupdf.Rect(chars["".join(c["c"] for c in chars).index(ACCOUNT) + 6]["bbox"])
    page.draw_rect(rect, color=colour, fill=colour, overlay=True)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.parametrize("box", ["trace", "rawdict"])
@pytest.mark.parametrize("colour", [(0, 0, 0), (1, 1, 1), (0.5, 0.5, 0.5)])
def test_a_digit_under_a_tightly_fitted_box_is_hidden(colour, box):
    data = _covered(colour, box)
    assert inspect_pdf(data, 10 * 2**20, 10).pages[0].hidden_chars == 1
    assert not _verified(data, ACCOUNT)  # the full number from the text layer
    assert not _verified(data, ACCOUNT[:6] + ACCOUNT[7:])
    assert _verified(LETTER, ACCOUNT)  # control: the same letter without the box


def test_clean_fixture_documents_keep_their_visibility():
    """The stricter rule finds no new hidden characters in any clean document (the known faint watermarks aside)."""
    for f in sorted((FIXTURE_DIR / "documents").glob("*.pdf")):
        try:
            pages = inspect_pdf(f.read_bytes(), 10 * 2**20, 10).pages
        except Exception:  # noqa: BLE001 - the deliberately damaged fixtures
            continue
        for p, text in zip(pages, page_texts(f.read_bytes())):
            visible_lines = [line for line in text.splitlines() if HIDDEN_MARK in line]
            assert all(line.replace(HIDDEN_MARK, "").startswith("SYNTHETIC TEST DO") for line in visible_lines), (f.name, visible_lines)


# ---------------------------------------------------------------------------- hidden text elsewhere

def _letter_with_hidden(kind: str) -> bytes:
    """A bank letter whose fully visible account number is followed, in drawing order and on the same baseline, by
    invisible text: OCR/reference text in another column, or an invisible copy laid exactly over the number."""
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=300)
    page.insert_text((30, 40), "SYNTHETIC TEST DOCUMENT - bank letter", fontsize=12)
    page.insert_text((30, 75), "Account holder: NIMBUS LEDGER SOLUTIONS PRIVATE LIMITED", fontsize=12)
    label, size = "Account number: ", 14
    page.insert_text((30, 110), label + ACCOUNT, fontsize=size)
    if kind == "column":
        page.insert_text((420, 110), "REF 7788", fontsize=size, render_mode=3)
    else:
        page.insert_text((30 + pymupdf.get_text_length(label, fontsize=size), 110), ACCOUNT, fontsize=size, render_mode=3)
    page.insert_text((30, 145), "IFSC: DECB0000412", fontsize=size)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.parametrize("kind", ["column", "duplicate_layer"])
def test_hidden_text_elsewhere_on_the_line_does_not_unverify_a_visible_account(kind):
    data = _letter_with_hidden(kind)
    assert inspect_pdf(data, 10 * 2**20, 10).pages[0].hidden_chars >= 1
    assert _verified(data, ACCOUNT)
    assert not _verified(data, ACCOUNT[:6] + ACCOUNT[7:])  # and nothing new becomes verifiable


# ---------------------------------------------------------------------------- per-file look

def _pan_reading(name: str, provenance: str, sha: str):
    s = SCEN["happy_nimbus"]
    docs = evidence(s["documents"])
    pan = next(d for d in docs if d.slot == "pan_card")
    f = pan.reading.fields["holder_name"].model_copy(update={"value": name, "normalized": name, "provenance": provenance})
    reading = pan.reading.model_copy(update={"fields": {**pan.reading.fields, "holder_name": f}})
    others = [d for d in docs if d.slot != "pan_card"]
    new = DocEvidence("pan_card", int(sha[:6], 16), "pan.pdf", sha, pan.layout, pan.page_count, "complete", reading)
    return EvalInput(form=s["form"], docs=others + [new], policy=policy(), verifications=registry(s["form"]))


def _name(inp):
    return next(c for c in run_checks(inp, compare(inp)) if c.key == "match:legal_name:pan_card")


@pytest.mark.parametrize("provenance,carries", [("visual_unconfirmed", False), ("quote_not_in_text", False),
                                                ("text_layer_verified", True)])
def test_a_confirmation_of_an_unproven_name_covers_only_that_file(provenance, carries):
    variant = "NIMBUS LEDGER SOLUTIONS INDIA PRIVATE LIMITED"  # a close variant ("similar"), so a judgement call
    first = _pan_reading(variant, provenance, "a" * 64)
    c = _name(first)
    assert c.status == "needs_review"
    accepted = [ResolutionRec(c.key, c.fingerprint, "accepted", "checked against the scan", "Priya")]
    first.resolutions = accepted
    assert _name(first).status == "satisfied"
    replaced = _pan_reading(variant, provenance, "b" * 64)  # another file, the same reading
    replaced.resolutions = accepted
    # Unproven by the text layer: the earlier look at the old file doesn't cover the new one. Proven: the judgement
    # that the two names are the same business carries over.
    assert (_name(replaced).status == "satisfied") is carries
