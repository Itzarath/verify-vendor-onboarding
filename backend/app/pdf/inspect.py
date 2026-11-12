"""Bounded PDF intake inspection: is this a PDF we can process, and how should each page be read?

Pages are classified as:
- text:    a usable VISIBLE text layer (we can verify quotes against it)
- scanned: little/no visible text but an image covering the page (needs vision reading)
- blank:   neither

Untrusted PDFs are parsed in a separate, killable process (see sandbox.py): file size and page
count alone do not bound how much work a page's content streams demand. The `_*_impl` functions
below run inside that process; the public functions are what the app calls.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field

MIN_TEXT_CHARS = 40
MAX_VISIBLE_CHARS_PER_PAGE = 20_000  # a dense A4 page has ~6,000; beyond this the page is not a normal document
MAX_GLYPHS_PER_PAGE = 60_000  # including invisible text
VIS_ZOOM = 100 / 72  # resolution of the visibility check (100 dpi)
VIS_MAX_SIDE = 3000  # ...but never more than this many pixels on a side (an oversized page is not rendered huge)
MAX_IMAGE_PIXELS = 50_000_000  # a 600 dpi A4 scan is ~35 million; a bigger image is refused before it is decoded
INK_CONTRAST = 10  # grey levels (of 255, ~4%) the text must change a pixel by to count as ink
MIN_SHARE = 0.3  # ...and somewhere it must reach this share of its own full strength (thin, small strokes stay above it)


def layout_label(layout: str) -> str:
    """How a layout is named to people. "blank" only means no searchable text and no full-page image: a certificate
    drawn as vector shapes is not blank to a reader (every page is still sent to the model as an image)."""
    return "no text layer" if layout == "blank" else layout


class IntakeError(Exception):
    """File cannot be accepted (corrupt, encrypted, wrong type, over limits, too complex)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PageInfo:
    number: int  # 1-based
    kind: str  # text | scanned | blank
    text_chars: int
    image_coverage: float
    width: float
    height: float
    hidden_chars: int = 0  # text present in the file but not visible (invisible render mode, transparent, white, tiny)


@dataclass
class Inspection:
    sha256: str
    page_count: int
    pages: list[PageInfo] = field(default_factory=list)
    # Embedded digital signatures (pdf/signature.py); None for documents inspected before signatures were checked.
    signatures: dict | None = None

    @property
    def layout(self) -> str:
        kinds = {p.kind for p in self.pages if p.kind != "blank"}
        if kinds == {"text"}:
            return "digital"
        if kinds == {"scanned"}:
            return "scanned"
        if not kinds:
            return "blank"
        return "mixed"

    def to_dict(self) -> dict:
        out = {"sha256": self.sha256, "page_count": self.page_count, "layout": self.layout,
               "pages": [asdict(p) for p in self.pages]}
        if self.signatures is not None:
            out["signatures"] = self.signatures
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Inspection":
        return cls(d["sha256"], d["page_count"], [PageInfo(**p) for p in d["pages"]], d.get("signatures"))


# ---------------------------------------------------------------------------- run inside the child process

def _span_hidden(span: dict) -> bool:
    """Text hidden by how it is drawn, whatever is around it."""
    if span.get("type") == 3:  # text render mode 3: invisible (typical of OCR layers and hidden instructions)
        return True
    if (span.get("opacity") if span.get("opacity") is not None else 1.0) <= 0.05:
        return True
    return (span.get("size") or 10) < 2.0


def _gray(color) -> float:
    """A text colour (grey, RGB or CMYK components 0..1) as a grey level 0..255, as the renderer would show it."""
    c = tuple(color or (0,))
    if len(c) == 1:
        g = c[0]
    elif len(c) == 3:
        g = 0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2]
    elif len(c) == 4:
        g = 1 - min(1.0, 0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2] + c[3])
    else:
        g = 0.0
    return 255 * max(0.0, min(1.0, g))


class _Visibility:
    """Does a letter or digit actually show on the rendered page?

    The page is rendered twice: as it is (F) and with all its text removed but every image, vector path, shading
    and annotation kept (B). Only pixels that differ between the two were painted by text, so a glyph counts as
    visible only if, somewhere in its own box, the text moved a pixel strongly towards that glyph's colour, and no
    other glyph covering that pixel could account for the change. That one rule covers text painted over (by a box,
    a bar, an image, shading or an annotation), clipped, off the page, in the background colour, on a transparent
    band, or overprinted by other text. Whatever can't be attributed counts as hidden; a person then confirms it.
    """

    def __init__(self, page, spans: list[dict]):
        import pymupdf

        zoom = min(VIS_ZOOM, VIS_MAX_SIDE / max(page.rect.width, page.rect.height, 1))
        m = pymupdf.Matrix(zoom, zoom)
        self.m = page.rotation_matrix * m  # text coordinates are unrotated page space; renderings are rotated and scaled
        f = page.get_pixmap(matrix=m, colorspace=pymupdf.csGRAY, alpha=False)
        self.f, self.stride, self.w, self.h = f.samples, f.stride, f.width, f.height
        self.b = self._without_text(page, m)
        self.glyphs: dict[tuple[int, int], tuple] = {}  # (span, char) -> (pixel rect, char, grey, opacity)
        grid: dict[tuple[int, int], list] = {}
        for si, sp in enumerate(spans):
            grey = _gray(sp.get("color"))
            opacity = 0.0 if sp.get("type") == 3 else (sp.get("opacity") if sp.get("opacity") is not None else 1.0)
            for ci, ch in enumerate(sp.get("chars") or []):
                r = self._region(ch[3])
                g = (r, chr(ch[0]), grey, opacity)
                self.glyphs[(si, ci)] = g
                if r[2] > r[0] and r[3] > r[1]:
                    for gx in range(r[0] // 32, (r[2] - 1) // 32 + 1):
                        for gy in range(r[1] // 32, (r[3] - 1) // 32 + 1):
                            grid.setdefault((gx, gy), []).append((si, ci))
        self.grid = grid

    def _without_text(self, page, m):
        """The same page rendered with its text removed and everything else kept (None if that fails)."""
        import pymupdf

        try:
            copy = pymupdf.open()
            copy.insert_pdf(page.parent, from_page=page.number, to_page=page.number, annots=True)
            q = copy[0]
            q.add_redact_annot(q.rect + (-10_000, -10_000, 10_000, 10_000), fill=False)
            q.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                               text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            b = q.get_pixmap(matrix=m, colorspace=pymupdf.csGRAY, alpha=False)
            if (b.width, b.height, b.stride) != (self.w, self.h, self.stride):
                return None
            return b.samples
        except Exception:  # noqa: BLE001 - without the comparison nothing can be proven visible
            return None

    def _region(self, bbox):
        import pymupdf

        r = pymupdf.Rect(bbox) * self.m
        return (max(0, int(r.x0)), max(0, int(r.y0)), min(self.w, math.ceil(r.x1)), min(self.h, math.ceil(r.y1)))

    def _others(self, key) -> list[tuple]:
        """Other glyphs whose boxes overlap this one and so could have painted its pixels. A neighbouring letter of
        the same run only counts when it genuinely overlaps (not just touching boxes or kerning), and a duplicate of
        the same character (simulated bold) is treated as part of this glyph."""
        (x0, y0, x1, y1), c, _, _ = self.glyphs[key]
        area = max(1, (x1 - x0) * (y1 - y0))
        seen, out = {key}, []
        for gx in range(x0 // 32, (x1 - 1) // 32 + 1):
            for gy in range(y0 // 32, (y1 - 1) // 32 + 1):
                for k in self.grid.get((gx, gy), ()):
                    if k in seen:
                        continue
                    seen.add(k)
                    (a0, b0, a1, b1), c2, grey, opacity = self.glyphs[k]
                    ix, iy = min(x1, a1) - max(x0, a0), min(y1, b1) - max(y0, b0)
                    if ix <= 0 or iy <= 0:
                        continue
                    overlap = ix * iy / min(area, max(1, (a1 - a0) * (b1 - b0)))
                    if k[0] == key[0] and overlap <= 0.3:
                        continue  # the next letter of the same run
                    if c2 == c and overlap > 0.5:
                        continue  # the same character drawn again on top of itself
                    out.append(((a0, b0, a1, b1), grey, opacity))
        return out

    def visible(self, key) -> bool:
        if self.b is None:
            return False
        (x0, y0, x1, y1), _, grey, opacity = self.glyphs[key]
        if x1 <= x0 or y1 <= y0:
            return False  # off the page
        # Only the inside of the box counts: its outermost pixels also hold a neighbouring letter's ink and the soft
        # edge of anything drawn over this one, so a digit under a tightly fitted box would otherwise "show".
        if x1 - x0 >= 3:
            x0, x1 = x0 + 1, x1 - 1
        if y1 - y0 >= 3:
            y0, y1 = y0 + 1, y1 - 1
        others = None
        f, b, stride = self.f, self.b, self.stride
        for y in range(y0, y1):
            i0 = y * stride + x0
            if f[i0:i0 + x1 - x0] == b[i0:i0 + x1 - x0]:
                continue  # text changed nothing on this row of the glyph's box
            for x in range(x0, x1):
                fp, bp = f[i0 + x - x0], b[i0 + x - x0]
                gap = bp - grey  # how far the background is from this glyph's colour, and in which direction
                moved = (bp - fp) if gap > 0 else (fp - bp)  # movement towards the glyph's colour
                if abs(gap) <= INK_CONTRAST or moved < max(INK_CONTRAST, MIN_SHARE * opacity * abs(gap)):
                    continue
                if others is None:
                    others = self._others(key)
                explained = False
                for (a0, b0, a1, b1), g2, op2 in others:
                    if a0 <= x < a1 and b0 <= y < b1:
                        gap2 = bp - g2
                        if gap2 * gap > 0 and op2 * abs(gap2) + INK_CONTRAST >= moved:
                            explained = True  # another glyph here could have made this change
                            break
                if not explained:
                    return True
        return False


# Stands in the visible text where letters or digits are present in the file but not visible. It is not a letter, a
# digit, a space or a hyphen, so a value can neither be grounded across it nor end right next to it: with one digit of
# an account number hidden, neither the shortened number nor a part of it counts as found in the text
# (extraction.contract._in_text). A real space between visible characters stays a space.
HIDDEN_MARK = "░"


def _visible_text(page) -> tuple[str, int, int]:
    """(visible text, hidden glyph count, total glyph count). Only what a person can see counts as the text layer;
    each hidden run of letters or digits is marked with HIDDEN_MARK, never turned into a space."""
    spans = page.get_texttrace()
    total = sum(len(s.get("chars") or []) for s in spans)
    if total > MAX_GLYPHS_PER_PAGE:
        raise IntakeError("too_complex", "A page contains far more text than a normal document. Please upload a "
                                         "simpler copy (for example a scan or a 'print to PDF' version).")
    vis = _Visibility(page, spans) if total else None
    parts: list[str] = []
    last_y = None
    last_x1 = None
    glue = False  # the previous part ended in a hidden run
    hidden = 0

    def add(text: str, chars: list, size: float | None, mark_only: bool = False) -> None:
        """Append a run. A hidden run is joined only to text it touches (no space between them), so a hidden digit
        splits the number it sits in, but hidden text elsewhere on the line (another column, an invisible copy laid
        over the page) never sticks to a visible value."""
        nonlocal last_y, last_x1, glue
        y, x0, x1 = chars[0][2][1], chars[0][3][0], chars[-1][3][2]
        same_line = last_y is not None and abs(y - last_y) <= 2
        touching = same_line and last_x1 is not None and abs(x0 - last_x1) <= 0.3 * (size or 10)
        sep = "" if not parts else ("\n" if not same_line else ("" if (glue or mark_only) and touching else " "))
        parts.append(sep + text)
        last_y, last_x1, glue = y, x1, text.endswith(HIDDEN_MARK)

    for si, s in enumerate(spans):
        chars = s.get("chars") or []
        if not chars:
            continue
        has_alnum = any(chr(ch[0]).isalnum() for ch in chars)
        if _span_hidden(s):
            hidden += len(chars)
            if has_alnum:
                add(HIDDEN_MARK, chars, s.get("size"), mark_only=True)
            continue
        text, gap, shown = [], False, 0
        for ci, ch in enumerate(chars):
            c = chr(ch[0])
            if c.isalnum() and not vis.visible((si, ci)):
                hidden += 1
                gap = True
                continue
            if gap:
                text.append(HIDDEN_MARK)
                gap = False
            text.append(c)
            shown += c.isalnum()
        if gap:
            text.append(HIDDEN_MARK)  # hidden characters at the end of the run
        if not shown and has_alnum:
            add(HIDDEN_MARK, chars, s.get("size"), mark_only=True)  # every letter and digit of this run is hidden
            continue
        add("".join(text), chars, s.get("size"))
    return "".join(parts).strip(), hidden, total


def _check_images(page, number: int) -> None:
    """Refuse images whose declared size would need huge memory to decode (decompression bombs), before decoding."""
    for img in page.get_image_info():
        w, h = int(img.get("width") or 0), int(img.get("height") or 0)
        if w * h > MAX_IMAGE_PIXELS:
            raise IntakeError("too_complex", f"Page {number} contains an image too large to process safely "
                                             f"({w:,} × {h:,} pixels). Please upload a smaller scan.")


def _open(data: bytes):
    import pymupdf

    try:
        return pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # corrupted structure
        raise IntakeError("corrupt_pdf", "The PDF could not be opened; it may be damaged.") from exc


def _inspect_impl(data: bytes, max_pages: int) -> dict:
    doc = _open(data)
    try:
        if doc.needs_pass or doc.is_encrypted:
            raise IntakeError("encrypted_pdf", "The PDF is password-protected. Please upload an unlocked copy.")
        if doc.page_count == 0:
            raise IntakeError("no_pages", "The PDF has no pages.")
        if doc.page_count > max_pages:
            raise IntakeError("too_many_pages", f"The PDF has {doc.page_count} pages; the limit is {max_pages}.")
        info = Inspection(sha256=hashlib.sha256(data).hexdigest(), page_count=doc.page_count)
        for i, page in enumerate(doc):
            _check_images(page, i + 1)
            try:
                text, hidden, _ = _visible_text(page)
            except IntakeError:
                raise
            except Exception as exc:
                raise IntakeError("corrupt_pdf", f"Page {i + 1} could not be read; the PDF may be damaged.") from exc
            chars = len("".join(text.split()).replace(HIDDEN_MARK, ""))
            if chars > MAX_VISIBLE_CHARS_PER_PAGE:
                raise IntakeError("too_complex", f"Page {i + 1} contains far more text than a normal document. Please "
                                                 "upload a simpler copy (for example a scan or a 'print to PDF' version).")
            area = page.rect.width * page.rect.height or 1.0
            covered = 0.0
            for img in page.get_image_info():
                x0, y0, x1, y1 = img["bbox"]
                covered += max(0.0, x1 - x0) * max(0.0, y1 - y0)
            coverage = min(1.0, covered / area)
            kind = "text" if chars >= MIN_TEXT_CHARS else ("scanned" if coverage > 0.3 else "blank")
            info.pages.append(PageInfo(i + 1, kind, chars, round(coverage, 2),
                                       round(page.rect.width, 1), round(page.rect.height, 1), hidden))
        info.signatures = _signatures_impl(data)
        return info.to_dict()
    finally:
        doc.close()


def _signatures_impl(data: bytes) -> dict:
    from .signature import read_signatures

    return read_signatures(data)


def _texts_impl(data: bytes) -> list[str]:
    doc = _open(data)
    try:
        for i, p in enumerate(doc):
            _check_images(p, i + 1)
        return [_visible_text(p)[0] for p in doc]
    finally:
        doc.close()


def _render(doc, page_number: int, dpi: int, max_side: int) -> bytes:
    import pymupdf

    page = doc[page_number - 1]
    _check_images(page, page_number)
    zoom = dpi / 72
    longest = max(page.rect.width, page.rect.height) * zoom
    if longest > max_side:
        zoom *= max_side / longest
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png")


def _render_impl(data: bytes, page_number: int, dpi: int, max_side: int) -> bytes:
    doc = _open(data)
    try:
        return _render(doc, page_number, dpi, max_side)
    finally:
        doc.close()


def _prepare_impl(data: bytes, dpi: int, max_side: int) -> dict:
    """Everything the model reader needs, in one pass: visible text and an image per page."""
    doc = _open(data)
    try:
        for i, p in enumerate(doc):
            _check_images(p, i + 1)
        return {"texts": [_visible_text(p)[0] for p in doc],
                "images": [_render(doc, i + 1, dpi, max_side) for i in range(doc.page_count)]}
    finally:
        doc.close()


# ---------------------------------------------------------------------------- what the app calls

def inspect_pdf(data: bytes, max_bytes: int, max_pages: int) -> Inspection:
    if len(data) == 0:
        raise IntakeError("empty_file", "The file is empty.")
    if len(data) > max_bytes:
        raise IntakeError("file_too_large", f"The file is larger than {max_bytes // (1024 * 1024)} MB.")
    if not data[:1024].lstrip().startswith(b"%PDF-"):
        raise IntakeError("not_pdf", "The file is not a PDF.")
    from .sandbox import run

    return Inspection.from_dict(run("inspect", data, max_pages=max_pages))


def pdf_signatures(data: bytes) -> dict:
    """Embedded digital signatures and whether the file changed after signing (see pdf/signature.py)."""
    from .sandbox import run

    return run("signatures", data)


def page_texts(data: bytes) -> list[str]:
    from .sandbox import run

    return run("texts", data)


def prepare_pages(data: bytes, dpi: int = 150, max_side: int = 2000) -> tuple[list[str], list[bytes]]:
    from .sandbox import run

    out = run("prepare", data, dpi=dpi, max_side=max_side)
    return out["texts"], out["images"]


def render_page_png(data: bytes, page_number: int, dpi: int = 150, max_side: int = 2000) -> bytes:
    """Render one page (1-based) to PNG for vision reading or the evidence viewer."""
    from .sandbox import run

    return run("render", data, page_number=page_number, dpi=dpi, max_side=max_side)
