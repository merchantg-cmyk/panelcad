"""Read embedded text from a vector PDF or a DXF. Raster pages have no tokens."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, NamedTuple

from panelcad.importer.models import CONVENTIONAL_WIRES, FURNITURE

PT_TO_MM = 25.4 / 72.0
# Letter-first IEC tags (K1, CR12), number-first JIC/NEMA tags (1M, 10M-G, 10OL-G, 2CR),
# and free-form NEMA tags (FU1-7, FU-1A, FU1A1, CR410, DRIVE1, S-204, LS-300, TB2-24).
# A motor-lead wire such as 10T1 does not match: the letter run cannot swallow the trailing digit.
_LETTER_TAG = r"[A-Z]{1,6}(?:-?\d{1,4}){1,2}[A-Z]?\d{0,4}"
_NUMBER_TAG = r"\d{1,4}[A-Z]{1,4}(?:-[A-Z0-9]{1,4})?"
# Siemens-style PLC addresses. The x in I0.x is a bit placeholder; Q40.2L keeps the latch letter.
_PLC_ADDR = r"[IQ]\d{1,3}\.[0-9X]\d{0,2}L?"
# Names with no digits that are still devices on a free-form NEMA sheet.
_BARE_TAG = r"PWS|HMI"
TAG_RE = re.compile(rf"^(?:{_LETTER_TAG}|{_NUMBER_TAG}|{_PLC_ADDR}|{_BARE_TAG})$")
PLC_ADDR_RE = re.compile(rf"^(?:{_PLC_ADDR})$")
_BARE_DEVICE_TAGS = {"PWS", "HMI"}
# A JIC line reference: (410) or sheet-line 6-410. Not a device and not a wire number.
_NEMA_LINE_REF = re.compile(r"^(?:\(\d{2,4}\)|\d{1,2}-\d{3,4})$")
# Wire numbers are digits of any ordinary length, including a single digit and a
# 5-digit number. The old 2–4 digit window dropped both.
WIRE_RE = re.compile(r"^\d{1,5}$")
# A number printed next to one of these is a rating (60 A, 480 V, 15 HP), not a wire.
_RATING_UNIT = re.compile(r"^(?:A|V|VAC|VDC|HP|KW|KVA|HZ|VA|W|AMP|AMPS|MA|KV)$")
# 2-15 is a wire number. 6-410 is a sheet-line reference and is not this pattern.
HYPHEN_WIRE_RE = re.compile(r"^\d{1,2}-\d{1,2}$")
# W101 and 101A. A one-letter tag with one or two digits (K1, M12, 10M) is not this.
LETTERED_WIRE_RE = re.compile(r"^(?:[A-Z]\d{3,5}|\d{3,5}[A-Z])$")
# IEC 81346 aspect marks: =function, +location, -product. -K1, -X2-4, =A1-K1.
_IEC_PRODUCT = re.compile(
    r"^(?:=[A-Z]{1,4}\d{0,4})?(?:\+[A-Z0-9]{1,8})*"
    r"-(?P<product>[A-Z]{1,6}\d{0,4}(?:[-:][A-Z0-9]{1,4})*)$"
)
# Printed motor-circuit numbers (10T1) and bus labels (9B16B). Not device tags.
LEAD_WIRE_RE = re.compile(r"^\d{1,4}T\d{1,2}$")
BUS_WIRE_RE = re.compile(r"^\d{1,4}B\d{1,4}[A-Z]$")
# Coil cross-references such as [9B34] or 9B34. A bus label has a trailing column letter.
XREF_RE = re.compile(r"^\[?\d{1,2}[A-Z]\d{2,4}\]?$")
# LS-2041-E1 keeps every dash group. A one-letter tail on the first group used to
# stop at LS-2041. 380L3503 is the number-letter-number form.
DWG_RE = re.compile(r"\b(?:[A-Z]{1,6}(?:-[A-Z0-9]{1,6}){1,3}|\d{2,5}[A-Z]\d{3,6})\b")
_COMPANY_SUFFIX = re.compile(
    r"\b(?:INC|INCORPORATED|L\.?L\.?C|LLC|LTD|LIMITED|CORP|CORPORATION|GMBH|CO|COMPANY)\.?$",
    re.IGNORECASE,
)
JOB_RE = re.compile(r"J[O0][BE8]\s*N[O0]\.?\s*([A-Z0-9][A-Z0-9\-]{3,})")
JOB_TIGHT_RE = re.compile(r"J[O0][BE8]N[O0]\.?\s*([A-Z0-9][A-Z0-9\-]{3,})")
FILE_RE = re.compile(r"FILE(?:\s*NAME)?\s*[:.]?\s*(?!NAME\b)([A-Z0-9][A-Z0-9.\-]{2,}(?:\.[A-Z0-9]{2,4})?)")
SHEET_NO_RE = re.compile(r"S[H5]\.?\s*N[O0]\.?\s*([0-9]{1,3}[A-Z]?|[A-Z][0-9]{1,3})")
# A sheet number read below this OCR confidence is needs-info. It is not stored.
SHEET_OCR_MIN_CONFIDENCE = 0.35
# A bare "N OF M" has no SHEET label to anchor it. Linework scores under this.
SHEET_UNLABELED_MIN_CONFIDENCE = 0.70
_SHEET_OF_RE = re.compile(
    r"\b(?:(SHEET|SHT|PAGE|PG)\.?\s*(?:NO\.?|NUMBER|#)?\s*[:.]?\s*)?(\d{1,3})\s+OF\s+(\d{1,3})\b",
    re.IGNORECASE,
)
_SHEET_NAMED_RE = re.compile(
    r"\b(SHEET|SHT|PAGE|PG)\.?\s*(?:(?:NO\.?|NUMBER|#)\s*)?[:.]?\s*(\d{1,3}[A-Z]?)\b",
    re.IGNORECASE,
)
_SHEET_DWG_SH_RE = re.compile(
    r"\bDWG\b.{0,60}?\b(SHT|SH)\.?\s*(\d{1,3}[A-Z]?)\b",
    re.IGNORECASE,
)
# OCR often glues the short form: SH3, SH.3, SHT7.
_SHEET_SHORT_SH_RE = re.compile(r"\b(SHT|SH)\.?\s*(\d{1,3}[A-Z]?)\b", re.IGNORECASE)
_SHEET_FRAC_RE = re.compile(
    r"\b(?:(SHEET|SHT|PAGE|PG)\.?\s*)?(\d{1,3})\s*/\s*(\d{1,3})\b",
    re.IGNORECASE,
)
_SHEET_BROKEN_RE = re.compile(
    r"\b(?:SHEET|SHT|PAGE|PG)\.?\s*(?:NO\.?|NUMBER|#)?\s*[:.]?\s*([Il|?])\b"
    r"|\bS[H5]\.?\s*N[O0]\.?\s*([Il|?])\b"
    r"|\b(?:SHEET|SHT|PAGE|PG)\.?\s*(?:NO\.?|NUMBER|#)\s*[:.]?\s*$",
    re.IGNORECASE,
)
_SHEET_CROSS_RE = re.compile(
    r"\b(?:SEE|FROM|REFER(?:ENCE)?|REF|TO|CONT(?:INUED)?(?:\s+ON)?)\.?\s+(?:TO\s+)?"
    r"(?:SHEET|SHT|SH|PAGE|PG)\.?\s*(?:NO\.?|NUMBER|#)?\s*[:.]?\s*\d{1,3}[A-Z]?(?:\s+OF\s+\d{1,3})?",
    re.IGNORECASE,
)
_SHEET_OF_NOTES_RE = re.compile(r"\b\d{1,3}\s+OF\s+\d{1,3}\s+NOTES\b", re.IGNORECASE)
_SHEET_ZONE_AFTER_RE = re.compile(r"[A-Z]\s*[-:]?\s*\d", re.IGNORECASE)
# A bare 1/2 or 3/8 is a drawing scale. 12/12 and 2/5 are not.
_SCALE_BEFORE_RE = re.compile(r"(?:SCALE|RATIO)\s*[:=]?\s*$", re.IGNORECASE)
# A DATE label and its value on the same line are one field. The colon is
# optional, and the value may be numeric or a month name. The strip stays on
# that line so a sheet fraction on the next line is left alone.
_MONTH_NAME = (
    r"JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|"
    r"NOVEMBER|DECEMBER|SEPT|JAN|FEB|MAR|APR|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
)
_DATE_GAP = r"[^\S\n]*"
_DATE_SP = r"[^\S\n]+"
_NUMERIC_DATE_VALUE = (
    rf"(?:\d{{1,2}}{_DATE_GAP}[./-]{_DATE_GAP}\d{{1,2}}"
    rf"(?:{_DATE_GAP}[./-]{_DATE_GAP}\d{{2,4}})?"
    rf"|\d{{4}}{_DATE_GAP}[./-]{_DATE_GAP}\d{{1,2}}{_DATE_GAP}[./-]{_DATE_GAP}\d{{1,2}})"
)
_MONTH_DATE_VALUE = (
    rf"(?:(?:{_MONTH_NAME})\.?{_DATE_SP}\d{{1,2}}(?:st|nd|rd|th)?"
    rf"(?:{_DATE_SP}|,[^\S\n]*)\d{{2,4}}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?{_DATE_SP}(?:{_MONTH_NAME})\.?"
    rf"(?:,[^\S\n]*)?{_DATE_SP}\d{{2,4}})"
)
_LABELED_DATE_RE = re.compile(
    rf"\b(?:DATED|DATE)\b{_DATE_GAP}[:.]?{_DATE_GAP}"
    rf"(?:{_NUMERIC_DATE_VALUE}|{_MONTH_DATE_VALUE})\b",
    re.IGNORECASE,
)
# A full date may have spaces around the separators. A year is required, so
# a bare sheet fraction such as 12/12 is not a date.
_FULL_DATE_RE = re.compile(
    rf"\b(?:\d{{4}}{_DATE_GAP}[./-]{_DATE_GAP}\d{{1,2}}{_DATE_GAP}[./-]{_DATE_GAP}\d{{1,2}}"
    rf"|\d{{1,2}}{_DATE_GAP}[./-]{_DATE_GAP}\d{{1,2}}{_DATE_GAP}[./-]{_DATE_GAP}\d{{2,4}})\b"
)
_MONTH_SEP = rf"(?:{_DATE_GAP}[./-]{_DATE_GAP}|{_DATE_SP})"
_MONTH_DATE_LOOSE_RE = re.compile(
    rf"\b(?:(?:{_MONTH_NAME})\.?{_MONTH_SEP}\d{{1,2}}(?:st|nd|rd|th)?"
    rf"(?:{_MONTH_SEP}|,[^\S\n]*)\d{{2,4}}"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?{_MONTH_SEP}(?:{_MONTH_NAME})\.?"
    rf"(?:,[^\S\n]*)?{_MONTH_SEP}\d{{2,4}})\b",
    re.IGNORECASE,
)
# Qualifiers that can sit with DATE and still leave the line a date label.
# A person's name, or any other word, means the line is not a label.
_DATE_LABEL_WORDS = frozenset(
    {
        "ISSUE",
        "ISSUED",
        "REV",
        "REVISION",
        "DRAWN",
        "PLOT",
        "PRINT",
        "PRINTED",
        "CHECKED",
        "APPROVED",
        "ORIGINAL",
        "LAST",
        "MODIFIED",
        "CREATED",
    }
)
_FRACTION_NEIGHBOR = re.compile(r"[A-Za-z]+")
_FRACTION_CONTEXT = frozenset(
    {
        "REV",
        "DATE",
        "DATED",
        "JOB",
        "DWG",
        "SCALE",
        "RATIO",
        "HP",
        "HZ",
        "VAC",
        "VDC",
        "AMP",
        "AMPS",
        "KW",
        "KVA",
        "FLA",
        "A",
        "V",
        "W",
    }
)
_DRAWING_SCALES = {
    (1, 1),
    (1, 2),
    (1, 4),
    (1, 5),
    (1, 8),
    (1, 10),
    (1, 16),
    (1, 20),
    (1, 25),
    (1, 30),
    (1, 32),
    (1, 40),
    (1, 50),
    (1, 64),
    (1, 75),
    (1, 100),
    (3, 4),
    (3, 8),
    (3, 16),
    (3, 32),
    (3, 64),
}
DRAWN_RE = re.compile(r"DRAWN\s+BY\s+([A-Z][A-Z .'-]{2,40}?)(?:\s{2,}|DATE|APPROVED|MADE|$)")
_DRAWN_STOP = {
    "MADE",
    "FOR",
    "INDUSTRIES",
    "INDUSTRY",
    "COMPANY",
    "APPROVED",
    "DATE",
    "REV",
    "CHECKED",
    "SCALE",
    "WEAN",
}
_DRAWING_REJECT = {
    "DRAWN",
    "BY",
    "NAME",
    "NO",
    "FILE",
    "DATE",
    "REV",
    "SHEET",
    "TITLE",
    "APPROVED",
    "SCALE",
    "JOB",
    "DWG",
    "SH",
    "MADE",
    "FOR",
    "WEAN",
}
_DATE_SEP = r"[-./]"
_DATE_BODY = rf"(?:\d{{4}}{_DATE_SEP}\d{{1,2}}{_DATE_SEP}\d{{1,2}}|\d{{1,2}}{_DATE_SEP}\d{{1,2}}{_DATE_SEP}\d{{2,4}})"
DATE_RE = re.compile(rf"\b({_DATE_BODY})\b")


def tokens_from_pdf(path: Path | str, page_index: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    import pymupdf

    doc = pymupdf.open(str(path))
    try:
        return tokens_from_page(doc[page_index])
    finally:
        doc.close()
        release_mupdf_store()


def tokens_from_page(page: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Words and page size from an already-open page. The caller closes the document."""

    rect = page.rect
    width_mm = float(rect.width) * PT_TO_MM
    height_mm = float(rect.height) * PT_TO_MM
    tokens: list[dict[str, Any]] = []
    for word in page.get_text("words"):
        x0, y0, x1, y1, text = word[:5]
        label = str(text or "").strip()
        if not label:
            continue
        tokens.append(
            {
                "text": label,
                "x": (float(x0) + float(x1)) * 0.5 * PT_TO_MM,
                "y": (float(y0) + float(y1)) * 0.5 * PT_TO_MM,
                "w": max(0.2, (float(x1) - float(x0)) * PT_TO_MM),
                "h": max(0.2, (float(y1) - float(y0)) * PT_TO_MM),
            }
        )
    meta = {
        "width_mm": round(width_mm, 3),
        "height_mm": round(height_mm, 3),
        "rotation": int(page.rotation or 0),
        "vector": _has_drawing_text(tokens, width_mm, height_mm),
        "text": page.get_text("text") or "",
    }
    return tokens, meta


def release_mupdf_store() -> None:
    """Drop MuPDF's decoded-page cache after a document is closed."""

    try:
        import pymupdf
    except ImportError:
        return
    tools = getattr(pymupdf, "TOOLS", None)
    shrink = getattr(tools, "store_shrink", None)
    if not callable(shrink):
        return
    try:
        shrink(100)
    except Exception:  # noqa: BLE001 — a cache clear must not fail the import
        return


def tokens_from_dxf(path: Path | str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Text from the first layout that has anything on it."""

    page = dxf_pages(path)[0]
    return list(page["tokens"]), dict(page["meta"])


def dxf_pages(path: Path | str) -> list[dict[str, Any]]:
    """One page per DXF layout that has text or geometry.

    Model space and each paper-space layout are separate sheets. An empty
    layout is not a sheet. Text inside a block is read in place.
    """

    import ezdxf

    doc = ezdxf.readfile(str(path))
    pages: list[dict[str, Any]] = []
    for layout in doc.layouts:
        texts, strokes = _dxf_layout_geometry(layout, doc)
        if not texts and not strokes:
            continue
        pages.append(_dxf_page(texts, strokes))
    if not pages:
        return [_dxf_page([], [])]
    return pages


def _dxf_layout_geometry(
    layout: Any, doc: Any
) -> tuple[list[tuple[str, float, float, float]], list[tuple]]:
    texts: list[tuple[str, float, float, float]] = []
    strokes: list[tuple] = []
    _collect_dxf_entities(layout, texts, strokes, depth=0, doc=doc)
    return texts, strokes


def _collect_dxf_entities(
    container: Any,
    texts: list,
    strokes: list,
    depth: int,
    doc: Any = None,
    insert_weight: float | None = None,
) -> None:
    if isinstance(container, list):
        entities = container
    else:
        try:
            entities = list(container)
        except Exception:  # noqa: BLE001 — a layout that cannot be walked is empty
            return
    for entity in entities:
        try:
            kind = entity.dxftype()
        except Exception:  # noqa: BLE001
            continue
        if kind == "INSERT" and depth < 3:
            from panelcad.importer.vectors import resolve_lineweight_mm

            weight = resolve_lineweight_mm(entity, doc=doc, insert_weight=insert_weight)
            try:
                children = list(entity.virtual_entities())
            except Exception:  # noqa: BLE001 — a bad block does not drop the rest of the sheet
                continue
            _collect_dxf_entities(children, texts, strokes, depth + 1, doc=doc, insert_weight=weight)
            continue
        try:
            _dxf_entity(entity, texts, strokes, doc=doc, insert_weight=insert_weight)
        except Exception:  # noqa: BLE001 — one bad entity does not drop the sheet
            continue


def _dxf_plain_text(entity: Any) -> str:
    plain = getattr(entity, "plain_text", None)
    if callable(plain):
        try:
            return str(plain() or "")
        except Exception:  # noqa: BLE001
            pass
    return str(getattr(entity, "text", "") or getattr(getattr(entity, "dxf", None), "text", "") or "")


def _dxf_add_text(texts: list, text: str, x: float, y: float, height: float) -> None:
    size = float(height or 2.5) or 2.5
    for index, line in enumerate(str(text or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        texts.append((line, float(x), float(y) - index * size, size))


def _dxf_layer_role(entity: Any) -> str:
    layer = str(getattr(getattr(entity, "dxf", None), "layer", "") or "").upper()
    if "WIRE" in layer:
        return "wire"
    if layer in {"TITLEBLOCK", "REVCLOUD", "DIM"}:
        return "frame"
    return "ink"


def _dxf_add_line(
    strokes: list,
    entity: Any,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    width_mm: float = 0.25,
) -> None:
    role = _dxf_layer_role(entity)
    strokes.append(("line", role, (float(x1), float(y1)), (float(x2), float(y2)), float(width_mm)))


def _dxf_line_width(entity: Any, doc: Any, insert_weight: float | None) -> float:
    from panelcad.importer.vectors import resolve_lineweight_mm

    return resolve_lineweight_mm(entity, doc=doc, insert_weight=insert_weight)


def _dxf_entity(
    entity: Any,
    texts: list,
    strokes: list,
    doc: Any = None,
    insert_weight: float | None = None,
) -> None:
    kind = entity.dxftype()
    if kind == "TEXT":
        text = str(entity.dxf.text or "").strip()
        if not text:
            return
        x, y, _z = entity.dxf.insert
        _dxf_add_text(texts, text, float(x), float(y), float(entity.dxf.height or 2.5))
        return
    if kind == "MTEXT":
        text = _dxf_plain_text(entity).strip()
        if not text:
            return
        x, y, _z = entity.dxf.insert
        height = float(getattr(entity.dxf, "char_height", 2.5) or 2.5)
        _dxf_add_text(texts, text, float(x), float(y), height)
        return
    if kind in {"ATTRIB", "ATTDEF"}:
        text = str(getattr(entity.dxf, "text", "") or "").strip()
        if not text:
            return
        x, y, _z = entity.dxf.insert
        _dxf_add_text(texts, text, float(x), float(y), float(getattr(entity.dxf, "height", 2.5) or 2.5))
        return
    if kind == "LINE":
        start, end = entity.dxf.start, entity.dxf.end
        width = _dxf_line_width(entity, doc, insert_weight)
        _dxf_add_line(strokes, entity, float(start.x), float(start.y), float(end.x), float(end.y), width)
        return
    if kind == "LWPOLYLINE":
        points = [(float(point[0]), float(point[1])) for point in entity.get_points("xy")]
        pairs = list(zip(points, points[1:], strict=False))
        if getattr(entity, "closed", False) and len(points) > 2:
            pairs.append((points[-1], points[0]))
        width = _dxf_line_width(entity, doc, insert_weight)
        for (x1, y1), (x2, y2) in pairs:
            _dxf_add_line(strokes, entity, x1, y1, x2, y2, width)
        return
    if kind == "POLYLINE":
        points = []
        for vertex in entity.vertices:
            loc = vertex.dxf.location
            points.append((float(loc.x), float(loc.y)))
        pairs = list(zip(points, points[1:], strict=False))
        if bool(getattr(entity, "is_closed", False)) and len(points) > 2:
            pairs.append((points[-1], points[0]))
        width = _dxf_line_width(entity, doc, insert_weight)
        for (x1, y1), (x2, y2) in pairs:
            _dxf_add_line(strokes, entity, x1, y1, x2, y2, width)
        return
    if kind == "CIRCLE":
        center = entity.dxf.center
        radius = float(entity.dxf.radius or 0)
        if radius <= 0:
            return
        points = []
        for step in range(12):
            angle = 2.0 * math.pi * step / 12.0
            points.append((float(center.x) + radius * math.cos(angle), float(center.y) + radius * math.sin(angle)))
        strokes.append(("curve", _dxf_layer_role(entity), points))
        return
    if kind == "ARC":
        center = entity.dxf.center
        radius = float(entity.dxf.radius or 0)
        if radius <= 0:
            return
        start = math.radians(float(entity.dxf.start_angle or 0))
        end = math.radians(float(entity.dxf.end_angle or 0))
        if end <= start:
            end += 2.0 * math.pi
        points = []
        sweep = end - start
        for step in range(8):
            angle = start + sweep * step / 7.0
            points.append((float(center.x) + radius * math.cos(angle), float(center.y) + radius * math.sin(angle)))
        strokes.append(("curve", _dxf_layer_role(entity), points))
        # A shallow arc is a hop in a conductor. The chord keeps the run connected.
        if sweep <= math.radians(50) and len(points) >= 2:
            width = _dxf_line_width(entity, doc, insert_weight)
            _dxf_add_line(
                strokes,
                entity,
                points[0][0],
                points[0][1],
                points[-1][0],
                points[-1][1],
                width,
            )


def _dxf_page(
    raw_text: list[tuple[str, float, float, float]],
    raw_strokes: list[tuple],
) -> dict[str, Any]:
    """Sheet coordinates are Y down. DXF export writes Y up."""

    ys = [item[2] for item in raw_text]
    xs = [item[1] for item in raw_text]
    for stroke in raw_strokes:
        if stroke[0] == "line":
            xs.extend((stroke[2][0], stroke[3][0]))
            ys.extend((stroke[2][1], stroke[3][1]))
        else:
            for point in stroke[2]:
                xs.append(point[0])
                ys.append(point[1])
    if not ys:
        meta = {"width_mm": 420.0, "height_mm": 297.0, "rotation": 0, "vector": False, "text": ""}
        return {"tokens": [], "strokes": [], "meta": meta}
    max_y = max(ys)
    min_y = min(ys)
    height_mm = max(297.0, max_y - min_y + 20.0)

    def _y(value: float) -> float:
        return height_mm - value

    tokens = []
    for text, x, y, height in raw_text:
        tokens.append(
            {
                "text": text,
                "x": x,
                "y": _y(y),
                "w": max(1.0, len(text) * height * 0.55),
                "h": max(0.8, height),
            }
        )
    from panelcad.importer.vectors import _promote_conductors, _promote_orthogonal, omit_border_line

    lines: list[dict[str, Any]] = []
    curves: list[dict[str, Any]] = []
    for stroke in raw_strokes:
        if stroke[0] == "line":
            width_mm = float(stroke[4]) if len(stroke) > 4 else 0.25
            start = [stroke[2][0], _y(stroke[2][1])]
            end = [stroke[3][0], _y(stroke[3][1])]
            # A layer named WIRE is not a conductor. Lineweight promotion decides.
            role = "ink" if stroke[1] == "wire" else stroke[1]
            lines.append(
                {
                    "kind": "line",
                    "role": role,
                    "a": start,
                    "b": end,
                    "pt": width_mm * 72.0 / 25.4,
                    "color": (0.1, 0.1, 0.1),
                    "span": math.hypot(end[0] - start[0], end[1] - start[1]),
                }
            )
        else:
            curves.append(
                {
                    "kind": "curve",
                    "role": stroke[1],
                    "points": [[point[0], _y(point[1])] for point in stroke[2]],
                }
            )
    _promote_conductors(lines)
    _promote_orthogonal(lines)
    strokes: list[dict[str, Any]] = []
    for line in lines:
        if omit_border_line(line["role"], line["a"], line["b"]):
            continue
        item = {"kind": "line", "role": line["role"], "a": line["a"], "b": line["b"]}
        if line.get("bus"):
            item["bus"] = True
        if line.get("geom"):
            item["geom"] = True
        strokes.append(item)
    strokes.extend(curves)
    max_x = max(xs) if xs else 0.0
    width_mm = max(420.0, max_x + 20.0)
    meta = {
        "width_mm": round(width_mm, 3),
        "height_mm": round(height_mm, 3),
        "rotation": 0,
        "vector": _has_drawing_text(tokens, width_mm, height_mm) or len(strokes) >= 4,
        "text": " ".join(item["text"] for item in tokens),
    }
    return {"tokens": tokens, "strokes": strokes, "meta": meta}


def normalize_device_tag(text: str) -> str:
    """Uppercase a tag. A PLC address loses the spaces a title block or OCR leaves in."""

    token = str(text or "").strip().upper()
    compact = re.sub(r"\s+", "", token)
    if PLC_ADDR_RE.match(compact):
        return compact
    return token


def is_plc_address(text: str) -> bool:
    """True for I0.0, I17.3, Q0.1, Q40.2L, and the same addresses with the spaces removed."""

    return PLC_ADDR_RE.match(normalize_device_tag(text)) is not None


def component_ref(tag: str) -> str:
    """Component id for a printed tag.

    A PLC address contains a dot, and a pin reference is ``component.pin``.
    The stored id uses an underscore so ``I0.0`` can still own a pin.
    """

    text = normalize_device_tag(tag)
    if is_plc_address(text):
        return text.replace(".", "_")
    return str(tag or "").strip()


def is_nema_line_ref(text: str) -> bool:
    """A NEMA/JIC line reference such as (410) or 6-410. Not a device tag."""

    token = str(text or "").strip().upper()
    return _NEMA_LINE_REF.match(token) is not None


# Parent, then a terminal. The separator is a hyphen or the IEC colon (K1:14).
# A leading dash (-K1:14) is the IEC tag mark, not part of the name.
_CONTACT_TAG = re.compile(r"^-?([A-Z]{1,6}\d{1,4})[-:](\d{1,3})$")
# Categories that own a separate contact symbol. A fuse or a terminal block does not.
_CONTACT_CATEGORIES = (
    "switching.contactor",
    "switching.relay",
    "switching.aux",
    "timing.",
    "protection.overload",
    "protection.breaker",
    "protection.mpcb",
)


def _prefix_owns_contacts(prefix: str, catalog: Any) -> bool | None:
    """Whether this tag family has aux or overload contacts in the catalog.

    True when a listed device owns aux contacts. False when the prefix is
    listed and none of those devices do (a fuse, a terminal, a motor). None
    when the catalog has no device with that prefix.
    """

    seen = False
    for device in catalog.devices.values():
        if str(getattr(device, "ref_prefix", "") or "").upper() != prefix:
            continue
        seen = True
        category = str(getattr(device, "category", "") or "").lower()
        library = str(getattr(device, "library_id", "") or "").lower()
        if category.startswith(_CONTACT_CATEGORIES) or "aux" in library or "ol_contact" in library:
            return True
    if not seen:
        return None
    return False


def sheet_tag_names(tokens: list[dict[str, Any]] | None) -> set[str]:
    """Device names printed on the sheet, before a suffixed contact is split."""

    names: set[str] = set()
    for token in tokens or []:
        raw = str(token.get("text") or token.get("tag") or "")
        text = iec_product_tag(raw) or normalize_device_tag(raw)
        if text and TAG_RE.match(text):
            names.add(text)
    return names


def contact_tag_parts(
    text: str,
    catalog: Any = None,
    parents: set[str] | None = None,
) -> tuple[str, str] | None:
    """Parent device and terminal for a suffixed contact tag.

    ``M1-13`` is device M1, terminal 13. ``K1:14`` and ``-K1:14`` are the same
    shape with the IEC separator. ``FU1-7``, ``TB2-24``, and ``X1:5`` stay
    whole devices: a terminal strip point is not a contact. A prefix the
    catalog does not list (``XT1-4``, ``SOL1-2``) is a contact only when that
    parent tag is also printed on the sheet.
    """

    from panelcad.importer.symbols import tag_prefix
    from panelcad.library.catalog import default_catalog

    token = normalize_device_tag(text).lstrip("-")
    match = _CONTACT_TAG.fullmatch(str(text or "").strip().upper())
    if match is None:
        match = _CONTACT_TAG.fullmatch(token)
    if match is None:
        return None
    parent, terminal = match.group(1), match.group(2)
    owns = _prefix_owns_contacts(tag_prefix(parent), catalog or default_catalog())
    if owns is False:
        return None
    if owns is None:
        present = {str(name).upper() for name in (parents or set())}
        if parent.upper() not in present:
            return None
    return parent, terminal


def _aspect_token(text: str) -> str:
    token = normalize_device_tag(text).replace(" ", "")
    return token.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")


def iec_product_tag(text: str) -> str:
    """Product designation from an IEC 81346 tag.

    ``-K1`` is K1, ``-S1`` is S1, ``-X2-4`` is X2-4, ``=A1-K1`` is K1.
    A tag with no aspect mark returns empty; the caller keeps the original text.
    """

    match = _IEC_PRODUCT.fullmatch(_aspect_token(text))
    if match is None:
        return ""
    return match.group("product")


def printed_reference_tag(text: str) -> str:
    """The reference as printed. ``-K1`` stays -K1; ``=A1-K1`` stays =A1-K1.

    A tag with no aspect mark returns empty. Symbol lookup still uses the
    product designation; the stored component id keeps the mark that was read.
    """

    token = _aspect_token(text)
    if not iec_product_tag(token):
        return ""
    return token


def lettered_wire_text(text: str) -> bool:
    """W101 or 101A. A device tag of this shape is a wire only when it sits on a conductor."""

    token = str(text or "").strip().upper()
    if not token or is_nema_line_ref(token) or is_cross_ref(token):
        return False
    return LETTERED_WIRE_RE.fullmatch(token) is not None


def is_device_tag(text: str, catalog: Any = None) -> bool:
    """A printed device tag. Conventional wire names and cross-references are not tags.

    Pass the page's catalog. Without it, a suffixed tag loads the library from disk.
    A leading IEC aspect mark (``-K1``, ``=A1-K1``) is stripped for the test and
    the stored tag is the product designation. ``W101`` and ``101A`` are wire numbers.
    """

    product = iec_product_tag(text)
    token = product or normalize_device_tag(text)
    if not token or token in FURNITURE or token in CONVENTIONAL_WIRES:
        return False
    if lettered_wire_text(token):
        return False
    if is_nema_line_ref(token) or is_cross_ref(token) or LEAD_WIRE_RE.match(token) or BUS_WIRE_RE.match(token):
        return False
    if token in _BARE_DEVICE_TAGS or is_plc_address(token):
        return True
    # 221MM is a dimension, not a device.
    if re.fullmatch(r"\d+(?:\.\d+)?MM", token):
        return False
    if contact_tag_parts(text, catalog) is not None or contact_tag_parts(token, catalog) is not None:
        return True
    return TAG_RE.match(token) is not None


def is_cross_ref(text: str) -> bool:
    """A bracketed or bare sheet cross-reference such as [9B34]."""

    token = str(text or "").strip().upper()
    if not token:
        return False
    if "[" in token or "]" in token:
        return True
    if BUS_WIRE_RE.match(token):
        return False
    return XREF_RE.match(token) is not None


def is_rating(token: dict[str, Any], tokens: list[dict[str, Any]]) -> bool:
    """A number with a unit word beside it. ``60 A``, ``480 V``, and ``15 HP`` are ratings.

    The unit may be the next word or glued on (``60A``). A rating is not a wire
    number and not a wire question.
    """

    text = str(token.get("text") or "").strip().upper().replace(" ", "")
    # 24V, 0V, and 480V are rail names. A spaced ``480 V`` beside a device is a rating.
    if text in CONVENTIONAL_WIRES:
        return False
    if re.fullmatch(r"\d{1,4}(?:A|V|VAC|VDC|HP|KW|KVA|HZ|VA|W|AMP|AMPS|MA|KV)", text):
        return True
    if not re.fullmatch(r"\d{1,4}", text):
        return False
    x = float(token["x"])
    y = float(token["y"])
    height = float(token.get("h") or 2.5)
    for other in tokens:
        if other is token:
            continue
        unit = str(other.get("text") or "").strip().upper()
        if not _RATING_UNIT.fullmatch(unit):
            continue
        if abs(float(other["y"]) - y) > max(1.4, height * 0.6):
            continue
        gap = float(other["x"]) - x
        if 0.4 < gap <= max(8.0, height * 2.4):
            return True
    return False


def is_wire_label(text: str, catalog: Any = None) -> bool:
    """A wire number, motor lead, bus label, or conventional power name. Not a device tag.

    Single-digit, 5-digit, and short hyphen numbers (``2-15``) count. ``6-410``
    stays a line reference. ``W101`` and ``101A`` are wire numbers. When they
    are not on a conductor they stay a wire question, not a device row.
    """

    token = str(text or "").strip().upper()
    if not token or is_cross_ref(token) or is_nema_line_ref(token):
        return False
    if lettered_wire_text(token):
        return True
    if HYPHEN_WIRE_RE.fullmatch(token):
        return True
    if is_device_tag(token, catalog):
        return False
    if token in CONVENTIONAL_WIRES:
        return True
    return bool(WIRE_RE.fullmatch(token) or LEAD_WIRE_RE.fullmatch(token) or BUS_WIRE_RE.fullmatch(token))


# Sheet frame. Row digits and column letters sit inside this band.
# A short page keeps a fraction so the working area does not disappear.
SHEET_BORDER_X_MM = 18.0
SHEET_BORDER_TOP_MM = 20.0
SHEET_BORDER_BOTTOM_MM = 52.0


def in_sheet_border(
    x: float,
    y: float,
    width_mm: float,
    height_mm: float,
    insets: tuple[float, float, float, float] | None = None,
) -> bool:
    """Zone grid and title strip. Row digits and column letters live here.

    ``insets`` is (left, top, right, bottom) in millimetres from the drawing frame.
    Without a frame, the band is the fixed 18 mm margin.
    """

    if width_mm <= 0 or height_mm <= 0:
        return False
    if insets is None:
        margin_x = min(SHEET_BORDER_X_MM, width_mm * 0.12)
        margin_top = min(SHEET_BORDER_TOP_MM, height_mm * 0.12)
        margin_bottom = min(SHEET_BORDER_BOTTOM_MM, height_mm * 0.22)
        right = margin_x
    else:
        margin_x, margin_top, right, margin_bottom = insets
    if x < margin_x or x > width_mm - right:
        return True
    if y < margin_top or y > height_mm - margin_bottom:
        return True
    return False


def _median_height(tokens: list[dict[str, Any]]) -> float:
    heights = sorted(float(tok.get("h") or 0) for tok in tokens)
    if not heights:
        return 0.0
    return heights[len(heights) // 2]


def _height_clusters(tokens: list[dict[str, Any]], ratio: float = 1.10) -> list[list[dict[str, Any]]]:
    """Group tag text whose heights are the same point size.

    ``ratio`` is how much taller the next token can be and still share the
    cluster. 10% keeps 7 pt and 9 pt apart and keeps a contact drawn a hair
    smaller than its coil with the coil.
    """

    ordered = sorted(tokens, key=lambda tok: float(tok.get("h") or 0))
    clusters: list[list[dict[str, Any]]] = [[ordered[0]]]
    for tok in ordered[1:]:
        floor = float(clusters[-1][0].get("h") or 0)
        height = float(tok.get("h") or 0)
        if floor > 0 and height <= floor * ratio:
            clusters[-1].append(tok)
        else:
            clusters.append([tok])
    return clusters


def _dominant_tags(
    tagish: list[dict[str, Any]],
    parents: set[str],
    catalog: Any = None,
) -> list[dict[str, Any]]:
    """Tags at the size actually used on the page.

    The floor is the tallest text size that more than a couple of tokens share.
    A few larger rail captions do not raise it, so they cannot drop the tags.
    A smaller cluster, such as terminal marks under the tag size, is not a tag.
    A contact suffix is kept even when it is drawn a little smaller. The stored
    text drops an IEC aspect mark so ``-K1`` and ``=A1-K1`` are the product ``K1``.
    """

    if not tagish:
        return []
    clusters = _height_clusters(tagish)
    total = len(tagish)
    substantial = [
        cluster
        for cluster in clusters
        if len(cluster) >= 2 and len(cluster) >= 0.25 * total
    ]
    # The tallest size that is actually used for tags. A couple of larger rail
    # captions do not set the floor. A smaller cluster (terminal marks under
    # the tag size) is not a tag.
    reference = max(substantial or clusters, key=_median_height)
    ref_h = _median_height(reference)
    kept: list[dict[str, Any]] = []
    for cluster in clusters:
        med = _median_height(cluster)
        # Several tags at a nearby point size stay. 4.3 mm and 5.8 mm text on
        # one sheet are both tags. One or two much shorter words are not a size.
        near_size = len(cluster) >= 3 and med >= ref_h * 0.70
        lone = len(cluster) == 1 and med < ref_h * 0.91
        pair = len(cluster) == 2 and med < ref_h * 0.85
        much_smaller = med < ref_h * 0.70
        drop = cluster is not reference and med < ref_h and (lone or pair or much_smaller) and not near_size
        for tok in cluster:
            if drop and contact_tag_parts(tok["text"], catalog, parents=parents) is None:
                continue
            product = iec_product_tag(tok["text"])
            kept.append({**tok, "text": product or str(tok["text"]), "role": "tag", "confidence": 0.93})
    return kept


def _best_drawing(text: str) -> str:
    """Longest drawing number in ``text`` that actually contains a digit."""

    best = ""
    for match in DWG_RE.finditer(str(text or "").upper()):
        token = match.group(0)
        if not any(ch.isdigit() for ch in token):
            continue
        if len(token) > len(best):
            best = token
    return best


def _field_word(text: str) -> str:
    return re.sub(r"[^A-Z]", "", str(text or "").upper())


def _same_line_value(tokens: list[dict[str, Any]], label: str, zone_ys: set[int]) -> str:
    """The value to the right of a label on the same baseline.

    ``REV: B`` and ``DWG NO: LS-2041-E1`` are one line. A value under the label
    is ``_join_under``.
    """

    wanted = label.upper()
    labels = [
        tok
        for tok in tokens
        if _field_word(tok["text"]) == wanted or str(tok["text"]).upper().rstrip(":.") == wanted
    ]
    if not labels:
        return ""
    lab = sorted(labels, key=lambda tok: (float(tok["x"]), float(tok["y"])))[0]
    lx, ly = float(lab["x"]), float(lab["y"])
    stop = _next_label_x(tokens, lab)
    words: list[tuple[float, str]] = []
    for tok in tokens:
        if tok is lab or round(float(tok["y"])) in zone_ys:
            continue
        if abs(float(tok["y"]) - ly) > 1.4:
            continue
        if float(tok["x"]) <= lx + 1.0 or float(tok["x"]) > stop:
            continue
        word = str(tok["text"]).strip()
        if _field_word(word) in {"NO", "NUMBER", "NUM"}:
            continue
        if word.strip(":.") == "":
            continue
        words.append((float(tok["x"]), word))
    words.sort()
    return " ".join(word for _x, word in words).strip()


def _company_caption(tokens: list[dict[str, Any]], zone_ys: set[int]) -> str:
    """A company line that has no COMPANY label, such as ``ACME CONTROLS INC.``"""

    rows: list[list[dict[str, Any]]] = []
    for tok in sorted(tokens, key=lambda item: (float(item["y"]), float(item["x"]))):
        if round(float(tok["y"])) in zone_ys:
            continue
        if rows and abs(float(tok["y"]) - float(rows[-1][0]["y"])) < 1.4:
            rows[-1].append(tok)
        else:
            rows.append([tok])
    for row in rows:
        text = " ".join(str(tok["text"]) for tok in row).strip()
        if not _COMPANY_SUFFIX.search(text):
            continue
        words = text.split()
        if len(words) < 2:
            continue
        if text.upper().startswith("PANELCAD") and any(ch.isdigit() for ch in text):
            continue
        return text
    return ""


def label_tokens(
    tokens: list[dict[str, Any]],
    width_mm: float,
    height_mm: float,
    *,
    insets: tuple[float, float, float, float] | None = None,
    catalog: Any = None,
) -> dict[str, list[dict[str, Any]]]:
    """Split working-area text into device tags and wire numbers.

    The dominant tag height is the size most tags share. A taller rail caption
    does not set the floor, so a 9 pt caption does not drop 7 pt tags. A token
    much smaller than that dominant size is not a tag, except a contact suffix.
    Wire numbers are kept from the text pattern, including when they are the
    same height as the tags. A lettered number such as W101 is a wire number;
    if it never lands on a conductor it stays a wire question.
    Conventional power names (L1, 24V, 0V) stay wire numbers. Number-first
    device tags stay tags. Border grid numbers and bracketed cross-references
    are neither.
    """

    if catalog is None:
        from panelcad.library.catalog import default_catalog

        catalog = default_catalog()
    insets = effective_insets(insets, tokens, width_mm, height_mm)
    # A pushbutton tag drawn into the zone band is still a device. Column
    # letters and the title strip are not. Re-export seats the tag above the
    # symbol, and that seat can cross the old working-area line.
    bottom = insets[3] if insets is not None else 54.0
    working = []
    for tok in tokens:
        if _in_working(tok, width_mm, height_mm, insets) or _margin_device_token(
            tok, width_mm, height_mm, insets
        ):
            working.append(dict(tok))
            continue
        # The title band is not a wire. A lettered or 4-digit number in the
        # top or side margin was still read and must not disappear.
        if float(tok["y"]) > float(height_mm) - bottom:
            continue
        text = str(tok.get("text") or "").strip()
        if lettered_wire_text(text) or re.fullmatch(r"\d{4,5}", text):
            working.append(dict(tok))
    parents = sheet_tag_names(working)
    tagish = [
        tok
        for tok in working
        if is_device_tag(tok["text"], catalog) and not _grid_junk_tag(tok, width_mm, height_mm, insets)
    ]
    tags: list[dict[str, Any]] = []
    wires: list[dict[str, Any]] = []
    if tagish:
        cutoff = _tag_height_cutoff([float(tok["h"]) for tok in tagish])
        passed: list[dict[str, Any]] = []
        below: list[dict[str, Any]] = []
        for tok in tagish:
            # A contact tag is drawn a little smaller than the coil tag above it.
            # The height gate is there to drop a title-block word, not that contact.
            if float(tok["h"]) >= cutoff or contact_tag_parts(tok["text"], catalog, parents=parents):
                passed.append(tok)
            else:
                below.append(tok)
        # The coil and the contact of one device are not the same height. The
        # shorter one stays when it is another printing of a tag that passed,
        # not a second glyph of the same label.
        named = {iec_product_tag(tok["text"]) or normalize_device_tag(tok["text"]) for tok in passed}
        for tok in below:
            product = iec_product_tag(tok["text"]) or normalize_device_tag(tok["text"])
            if not product or product not in named:
                continue
            height = float(tok.get("h") or 2.5)
            limit = max(12.0, height * 3.5)
            if any(
                (iec_product_tag(other["text"]) or normalize_device_tag(other["text"])) == product
                and math.hypot(float(tok["x"]) - float(other["x"]), float(tok["y"]) - float(other["y"])) <= limit
                for other in passed
            ):
                continue
            passed.append(tok)
        for tok in passed:
            # Keep the aspect mark that was printed. -K1 and K1 are different
            # spellings of one product; the sheet showed one of them.
            shown = printed_reference_tag(tok["text"]) or str(tok["text"])
            tags.append({**tok, "text": shown, "role": "tag", "confidence": 0.93})
    tag_keys = {(round(tok["x"], 1), round(tok["y"], 1), tok["text"]) for tok in tags}
    # The product spelling too, so a stored -K1 still hides a bare K1 on the same glyph.
    for tok in tagish:
        product = iec_product_tag(tok["text"])
        if product:
            tag_keys.add((round(tok["x"], 1), round(tok["y"], 1), product))
            tag_keys.add((round(tok["x"], 1), round(tok["y"], 1), str(tok["text"])))
    for tok in working:
        key = (round(tok["x"], 1), round(tok["y"], 1), tok["text"])
        if key in tag_keys:
            continue
        if is_cross_ref(tok["text"]) or _border_grid_token(tok, width_mm, height_mm, insets):
            continue
        if _near_damaged_xref(tok, tokens):
            continue
        if _damaged_xref_number(tok["text"]) and _outer_band(
            float(tok["x"]), float(tok["y"]), width_mm, height_mm
        ):
            continue
        if is_wire_label(tok["text"], catalog) and not is_rating(tok, working):
            wires.append({**tok, "role": "wire", "confidence": 0.9})
    return {"tags": _dedupe_tags(tags), "wires": wires, "title": _title_tokens(tokens, height_mm)}


def _tag_height_cutoff(heights: list[float]) -> float:
    """Height gate from the tags that share a height, not from one extreme.

    A few labels drawn larger than the device tags must not hide those tags.
    A crowd of shorter point labels must not pull the gate down to their size.
    The reference is the tallest height that at least three tags share. A short
    title-block word stays under the cutoff. A page with too few tags keeps the
    old rule and uses its tallest tag.
    """

    ordered = sorted(float(height) for height in heights if height > 0)
    if not ordered:
        return 0.0
    if len(ordered) < 8:
        return ordered[-1] * 0.82
    clusters: list[list[float]] = [[ordered[0]]]
    for height in ordered[1:]:
        previous = clusters[-1][-1]
        # A gap larger than the small labels, or a step of about a fifth, is a new size.
        if height - previous > max(0.6, previous * 0.18):
            clusters.append([height])
        else:
            clusters[-1].append(height)
    shared = [cluster for cluster in clusters if len(cluster) >= 3]
    if not shared:
        index = int(round(0.80 * (len(ordered) - 1)))
        reference = ordered[index]
    else:
        group = max(shared, key=lambda cluster: cluster[-1])
        middle = len(group) // 2
        reference = group[middle]
    return reference * 0.82


def read_title_block(tokens: list[dict[str, Any]], height_mm: float) -> dict[str, Any]:
    """Title-block fields from text that sits in the bottom band.

    Values sit on the line under the label and run to the right, up to the next label.
    Zone digits along the top of the band are not field values.
    """

    # A D-size title block is taller than the 58 mm strip a B-size sheet uses.
    band_mm = max(58.0, min(110.0, float(height_mm) * 0.20))
    band = [tok for tok in tokens if float(tok["y"]) >= float(height_mm) - band_mm]
    text = " ".join(tok["text"] for tok in band)
    zone_ys = _zone_rows(band)
    company_lines = _lines_under(band, "COMPANY", zone_ys=zone_ys, max_lines=2)
    company = company_lines[0] if company_lines else ""
    if not company:
        company = _company_caption(band, zone_ys)
    address = company_lines[1] if len(company_lines) > 1 else ""
    title = _join_under(band, "TITLE", zone_ys) or _join_under(band, "Title", zone_ys)
    if not title:
        # DRAWING TITLE, SHEET TITLE, DWG TITLE. The value is the sheet name.
        title = _title_under_extended_label(band, zone_ys)
    upper = text.upper()
    beside_dwg = _same_line_value(band, "DWG", zone_ys)
    under_dwg = _join_under(band, "DWG", zone_ys)
    drawing = _best_drawing(beside_dwg) or _best_drawing(under_dwg) or _best_drawing(upper)
    job = JOB_RE.search(upper) or JOB_TIGHT_RE.search(upper)
    if job:
        drawing = job.group(1).strip(" .-")
    elif not _accept_drawing(drawing):
        file_name = FILE_RE.search(upper)
        if file_name:
            drawing = _accept_drawing(file_name.group(1).split(".")[0])
        else:
            drawing = ""
    if not _accept_drawing(drawing):
        bare = re.search(r"\b(\d{2,4}[A-Z]\d{3,6})\b", upper)
        drawing = bare.group(1) if bare else ""
    drawing = _accept_drawing(drawing)
    if not title:
        # No TITLE label. A short layout name in the title band can be the sheet title.
        # A cross-reference or a revision note cannot. The words in front of
        # "layout" are not a fixed list.
        titled = _layout_line_in_band(band)
        if titled:
            title = titled
    if not title and "MOTOR STARTER" in upper:
        title = "MOTOR STARTERS" if "MOTOR STARTERS" in upper else "MOTOR STARTER"
    elif not title and "ELEMENTARY DIAGRAM" in upper:
        title = "ELEMENTARY DIAGRAM"
    rev = _same_line_value(band, "REV", zone_ys) or _join_under(band, "REV", zone_ys)
    rev = rev.split()[0].strip(":.") if rev else ""
    if rev.upper() in {"DATE", "DESCRIPTION", "DRAWN"}:
        rev = ""
    standard = ""
    under_std = _join_under(band, "STANDARD", zone_ys).upper()
    explicit_iec = bool(re.search(r"\bIEC\b", under_std) or re.search(r"\bIEC\b", upper))
    # An IEC sheet that also says ELEMENTARY stays IEC. A US job number does not.
    if "NEMA" in under_std or "ANSI" in under_std or "NEMA" in upper or "ANSI" in upper or "JIC" in upper:
        standard = "NEMA/ANSI"
    elif explicit_iec:
        standard = "IEC"
    elif re.search(r"\b(JOB\s*NO|SH\.?\s*NO|ELEMENTARY)\b", upper):
        standard = "NEMA/ANSI"
    labeled = [
        value
        for value in (
            _join_under(band, "SHEET", zone_ys),
            _join_under(band, "PAGE", zone_ys),
            _join_under(band, "SHT", zone_ys),
        )
        if value
    ]
    # A bare "N OF M" in this band uses the same box rule as the title-crop OCR.
    # A failed check is needs-info and does not fall through to the page text.
    sheet = sheet_from_glyphs(
        glyphs_from_title_tokens(_reading_order(band)),
        in_title_block=True,
        labeled_values=labeled,
    )
    if sheet["sheet_number"]:
        sheet = _drop_uncertain_sheet(sheet, _sheet_number_confidence(band, sheet["sheet_number"]))
    if not sheet["sheet_number"] and not sheet["sheet_number_needs_info"]:
        # A rotated photo can leave the title block outside the bottom band.
        # Explicit SHEET / SH.NO / DWG … SH text anywhere on the page still counts.
        # A bare "6 OF 6" does not, so a note cannot become the sheet number.
        page_text = " ".join(str(tok.get("text") or "") for tok in _reading_order(tokens))
        page_sheet = sheet_fields_from_text(page_text, in_title_block=False)
        if page_sheet["sheet_number"]:
            page_sheet = _drop_uncertain_sheet(
                page_sheet, _sheet_number_confidence(tokens, page_sheet["sheet_number"])
            )
        sheet = page_sheet
    sheet_no = sheet["sheet_number"]
    sheet_count = sheet["sheet_count"]
    sheet_needs_info = sheet["sheet_number_needs_info"]
    descriptions = _label_tokens(band, "DESCRIPTION")
    description = _join_under(band, "DESCRIPTION", zone_ys, index=0)
    rev_description = _join_under(band, "DESCRIPTION", zone_ys, index=1) if len(descriptions) > 1 else ""
    return {
        "title": title,
        "project": _join_under(band, "PROJECT", zone_ys),
        "description": description,
        "company": company,
        "address": address,
        "customer": _join_under(band, "CUSTOMER", zone_ys),
        "location": _join_under(band, "LOCATION", zone_ys),
        "drawn_by": _drawn_name(_join_under(band, "DRAWN", zone_ys)) or _drawn_by(upper),
        "scale": _join_under(band, "SCALE", zone_ys),
        "date": _drawn_date(upper) or _accepted_date(_join_under(band, "DATE", zone_ys)) or _first_date(text),
        "rev_description": rev_description,
        "drawing_number": drawing,
        "rev": rev if len(rev) <= 4 else "",
        "standard": standard,
        "sheet_number": sheet_no,
        "sheet_count": sheet_count,
        "sheet_number_needs_info": sheet_needs_info,
        "text": text,
        "layout_kind": _kind_token_in_band(band),
    }


def _reading_order(tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Top to bottom, then left to right. OCR tokens are not already in that order."""

    def key(tok: dict[str, Any]) -> tuple[float, float]:
        try:
            y = float(tok.get("y") or 0)
            x = float(tok.get("x") or 0)
        except (TypeError, ValueError):
            return (0.0, 0.0)
        return (round(y / 2.2) * 2.2, x)

    return sorted(tokens, key=key)


def _norm_sheet(value: str) -> str:
    """Sheet id as printed. Leading zeros drop so 03 and 3 are the same sheet."""

    text = str(value or "").strip().upper()
    digit = re.fullmatch(r"0*(\d{1,3})([A-Z]?)", text)
    if digit and digit.group(1) and int(digit.group(1)) != 0:
        return f"{int(digit.group(1))}{digit.group(2)}"
    letter = re.fullmatch(r"([A-Z])0*(\d{1,3})", text)
    if letter and int(letter.group(2)) != 0:
        return f"{letter.group(1)}{int(letter.group(2))}"
    return ""


def _empty_sheet_fields() -> dict[str, Any]:
    return {"sheet_number": "", "sheet_count": "", "sheet_number_needs_info": False}


class SheetGlyph(NamedTuple):
    """One word and its box. ``score`` is None for vector text, which has no OCR confidence."""

    text: str
    score: float | None
    left: float
    top: float
    width: float
    height: float


# A bare number has to share the OF line. The tolerances are glyph size, not one drawing.
_OF_HEIGHT_TOLERANCE = 0.35
_OF_GAP_HEIGHTS = 1.5
_OF_BASELINE_HEIGHTS = 0.50


def glyphs_from_title_tokens(tokens: list[dict[str, Any]]) -> list[SheetGlyph]:
    """Title-band tokens use a center point. The box is that center plus width and height."""

    glyphs: list[SheetGlyph] = []
    for tok in tokens:
        text = str(tok.get("text") or "").strip()
        if not text:
            continue
        try:
            x = float(tok.get("x") or 0)
            y = float(tok.get("y") or 0)
            width = float(tok.get("w") or 0)
            height = float(tok.get("h") or 0)
        except (TypeError, ValueError):
            continue
        raw_score = tok.get("confidence")
        score: float | None
        if raw_score is None:
            score = None
        else:
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                score = None
        glyphs.append(SheetGlyph(text, score, x - width / 2.0, y - height / 2.0, width, height))
    return _expand_of_glyphs(glyphs)


def sheet_from_glyphs(
    glyphs: list[SheetGlyph],
    *,
    in_title_block: bool,
    labeled_values: list[str] | None = None,
    parse: Any = None,
) -> dict[str, Any]:
    """Sheet fields from one line of boxes.

    A bare digit stays only when it sits on OF. A SHEET label beside the digit
    keeps a wide gap before OF. A clear OF M with no usable number is needs-info.
    """

    if parse is None:
        parse = sheet_fields_from_text
    glyphs = _expand_of_glyphs(list(glyphs))
    pairs = _of_count_pairs(glyphs)
    kept = _words_for_sheet_parse(glyphs, pairs)
    text = _glyph_text(kept)
    found = parse(text, in_title_block=in_title_block, labeled_values=labeled_values)
    if found.get("sheet_number"):
        target = _norm_sheet(str(found["sheet_number"]))
        scored = [glyph for glyph in kept if glyph.score is not None]
        if scored:
            # A glued "SHT1" or "3OF7" carries the sheet number. If every OCR
            # word has a score and none of them is that number, do not store it.
            scores = [
                float(glyph.score)
                for glyph in scored
                if glyph_supports_sheet(glyph.text, target)
            ]
            if not scores:
                return {**_empty_sheet_fields(), "sheet_number_needs_info": True}
            found = parse(
                text,
                in_title_block=in_title_block,
                labeled_values=labeled_values,
                confidence=max(scores),
            )
        return found
    if found.get("sheet_number_needs_info"):
        return found
    clear = _clearest_of_count(pairs)
    if clear is None:
        return _empty_sheet_fields()
    _of_word, count_word = clear
    return {
        "sheet_number": "",
        "sheet_count": _norm_sheet(count_word.text),
        "sheet_number_needs_info": True,
    }


def _expand_of_glyphs(glyphs: list[SheetGlyph]) -> list[SheetGlyph]:
    expanded: list[SheetGlyph] = []
    for glyph in glyphs:
        expanded.extend(_split_of_count(glyph))
    return expanded


def _split_of_count(glyph: SheetGlyph) -> list[SheetGlyph]:
    """``OF12`` is the count with no space. Split it so the box rule can see both parts."""

    match = re.fullmatch(r"(OF|0F)(\d{1,3})", glyph.text.strip(), re.IGNORECASE)
    if match is None or glyph.width <= 0:
        return [glyph]
    of_text = match.group(1).upper()
    count_text = match.group(2)
    of_width = glyph.width * len(of_text) / (len(of_text) + len(count_text))
    return [
        SheetGlyph(of_text, glyph.score, glyph.left, glyph.top, of_width, glyph.height),
        SheetGlyph(
            count_text,
            glyph.score,
            glyph.left + of_width,
            glyph.top,
            glyph.width - of_width,
            glyph.height,
        ),
    ]


def _words_for_sheet_parse(
    words: list[SheetGlyph],
    pairs: list[tuple[SheetGlyph, SheetGlyph]],
) -> list[SheetGlyph]:
    """Drop a digit that cannot be the N in ``N OF M``. A count, or a digit by its label, stays.

    The digit has to share OF's baseline. A digit on the line above, even when
    it sits over OF's right side, is not the sheet number.
    """

    if not pairs:
        return list(words)
    chosen: set[int] = set()
    for of_word, count_word in pairs:
        sitting = [
            word for word in words if _number_sits_with_of(word, of_word, count_word, words)
        ]
        if not sitting:
            continue
        best = min(sitting, key=lambda word: _gap_before(of_word, word))
        chosen.add(id(best))
    counts = {id(count) for _of_word, count in pairs}
    dropped: set[int] = set()
    for word in words:
        if not _pure_digits(word) or id(word) in chosen or id(word) in counts or _label_beside(word, words):
            continue
        dropped.add(id(word))
    return [word for word in words if id(word) not in dropped]


def glyph_supports_sheet(word: str, target: str) -> bool:
    """True when this word is the sheet id, including a label or OF glued to it.

    ``1`` does not borrow the confidence of ``10`` or ``E-1042``. ``SHT1`` and
    ``3OF7`` do carry the confidence of that whole word.
    """

    if not target:
        return False
    if _norm_sheet(word) == target:
        return True
    # A slash or a glued OF is a fraction. Stripping punctuation first turns 2/5 into 25.
    raw = str(word).strip().upper()
    label = r"(?:SHEET|SHT|SH|PAGE|PG|NO|NUMBER|DWG|JOB)"
    fraction = re.fullmatch(rf"(?:{label}\.?\s*)?(\d{{1,3}})\s*/\s*(\d{{1,3}})", raw)
    if fraction and _norm_sheet(fraction.group(1)) == target:
        return True
    glued_of = re.fullmatch(rf"(?:{label}\.?\s*)?(\d{{1,3}})\s*OF\s*(\d{{1,3}})", raw)
    if glued_of and _norm_sheet(glued_of.group(1)) == target:
        return True
    compact = re.sub(r"[^A-Z0-9]", "", raw)
    if target not in compact:
        return False
    if re.fullmatch(r"\d+[A-Z]?", compact) and compact != target:
        return False
    prefix = r"(?:SHEET|SHT|SH|PAGE|PG|NO|NUMBER|DWG|JOB)*"
    if re.fullmatch(prefix + re.escape(target), compact):
        return True
    return re.fullmatch(re.escape(target) + r"OF\d{1,3}", compact) is not None


def _of_count_pairs(words: list[SheetGlyph]) -> list[tuple[SheetGlyph, SheetGlyph]]:
    """``OF`` plus a count on the same line. A low OCR score does not skip the check."""

    pairs: list[tuple[SheetGlyph, SheetGlyph]] = []
    counts = [word for word in words if _pure_digits(word)]
    for of_word in words:
        if not _is_of(of_word):
            continue
        sitting = [count for count in counts if _count_sits_with_of(count, of_word)]
        if not sitting:
            continue
        count_word = min(sitting, key=lambda count: _gap_after(of_word, count))
        if _notes_follow(count_word, words):
            continue
        pairs.append((of_word, count_word))
    return pairs


def _clearest_of_count(
    pairs: list[tuple[SheetGlyph, SheetGlyph]],
) -> tuple[SheetGlyph, SheetGlyph] | None:
    if not pairs:
        return None

    def rank(pair: tuple[SheetGlyph, SheetGlyph]) -> float:
        scores = [score for score in (pair[0].score, pair[1].score) if score is not None]
        return min(scores) if scores else 1.0

    return max(pairs, key=rank)


def _number_sits_with_of(
    number: SheetGlyph,
    of_word: SheetGlyph,
    count_word: SheetGlyph,
    words: list[SheetGlyph],
) -> bool:
    """Bare N is on OF's baseline, a similar height, and within 1.5 OF-heights."""

    if not _pure_digits(number) or not _same_baseline(number, of_word):
        return False
    height_ok = _height_close(number.height, of_word.height) or _height_close(
        number.height, count_word.height
    )
    if not height_ok:
        return False
    gap = _gap_before(of_word, number)
    height = max(of_word.height, 1.0)
    if gap > _OF_GAP_HEIGHTS * height or gap < -0.45 * height:
        return False
    if _label_beside(number, words):
        floor = SHEET_OCR_MIN_CONFIDENCE
    else:
        floor = SHEET_UNLABELED_MIN_CONFIDENCE
    return _score_meets(number.score, floor)


def _count_sits_with_of(count: SheetGlyph, of_word: SheetGlyph) -> bool:
    if not _same_baseline(count, of_word):
        return False
    if not _height_close(count.height, of_word.height):
        return False
    gap = _gap_after(of_word, count)
    height = max(of_word.height, 1.0)
    return -0.45 * height <= gap <= _OF_GAP_HEIGHTS * height


def _label_beside(number: SheetGlyph, words: list[SheetGlyph]) -> bool:
    """A SHEET / SHT / PAGE / SH label on this number's line, not a label across the band."""

    height = max(number.height, 1.0)
    for word in words:
        if word is number or not _sheet_label(word.text):
            continue
        if not _same_baseline(word, number):
            continue
        gap = number.left - (word.left + word.width)
        if -0.3 * height <= gap <= 4.0 * height:
            return True
    return False


def _sheet_label(text: str) -> bool:
    token = re.sub(r"[^A-Z0-9]", "", str(text).upper())
    if token in {"SHEET", "SHT", "SH", "PAGE", "PG", "NO"}:
        return True
    return bool(re.fullmatch(r"S[H5]N[O0]|SHTNO|SHEETNO", token))


def _notes_follow(count: SheetGlyph, words: list[SheetGlyph]) -> bool:
    height = max(count.height, 1.0)
    for word in words:
        if not re.match(r"NOTES?", word.text.strip(), re.IGNORECASE):
            continue
        gap = word.left - (count.left + count.width)
        if _same_baseline(word, count) and -0.3 * height <= gap <= 2.0 * height:
            return True
    return False


def _same_baseline(one: SheetGlyph, other: SheetGlyph) -> bool:
    ref = max(one.height, other.height, 1.0)
    return abs((one.top + one.height) - (other.top + other.height)) <= _OF_BASELINE_HEIGHTS * ref


def _height_close(height: float, reference: float) -> bool:
    if height <= 0 or reference <= 0:
        return False
    return abs(height - reference) <= _OF_HEIGHT_TOLERANCE * reference


def _gap_before(of_word: SheetGlyph, number: SheetGlyph) -> float:
    return of_word.left - (number.left + number.width)


def _gap_after(of_word: SheetGlyph, count: SheetGlyph) -> float:
    return count.left - (of_word.left + of_word.width)


def _pure_digits(word: SheetGlyph) -> bool:
    return re.fullmatch(r"\d{1,3}", word.text.strip()) is not None


def _is_of(word: SheetGlyph) -> bool:
    return re.fullmatch(r"OF|0F", word.text.strip(), re.IGNORECASE) is not None


def _score_meets(score: float | None, floor: float) -> bool:
    """Vector text has no OCR score. A present score has to clear the floor."""

    if score is None:
        return True
    return score >= floor


def _baseline_pitch(words: list[SheetGlyph]) -> float:
    heights = [word.height for word in words if word.height > 0]
    line = (sum(heights) / len(heights)) * 0.6 if heights else 1.0
    if line < 0.4:
        line = 0.4
    return line


def _glyph_order(words: list[SheetGlyph]) -> list[SheetGlyph]:
    line = _baseline_pitch(words)
    return sorted(words, key=lambda word: (round((word.top + word.height) / line), word.left))


def _glyph_lines(words: list[SheetGlyph]) -> list[list[SheetGlyph]]:
    """Group words that share a baseline. A real next line starts a new group.

    Rounding the baseline onto a grid splits ``REV A`` from ``3/12`` when their
    boxes differ by a fraction of a millimetre. A gap of about half a glyph
    is still the same line.
    """

    if not words:
        return []

    def baseline(word: SheetGlyph) -> float:
        return word.top + word.height

    ordered = sorted(words, key=lambda word: (baseline(word), word.left))
    heights = [word.height for word in ordered if word.height > 0]
    typical = (sum(heights) / len(heights)) if heights else 1.0
    tolerance = max(0.55 * typical, 0.8)
    lines: list[list[SheetGlyph]] = []
    anchors: list[float] = []
    for word in ordered:
        base = baseline(word)
        if lines and abs(base - anchors[-1]) <= tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])
            anchors.append(base)
    for line in lines:
        line.sort(key=lambda word: word.left)
    return lines


def _glyph_text(words: list[SheetGlyph]) -> str:
    """One baseline is one line. The title above ``12/12`` is not on that line."""

    return "\n".join(" ".join(word.text for word in line) for line in _glyph_lines(words))


def _prepare_sheet_text(text: str) -> str:
    """Drop dates, cross-references, and note counts before looking for this sheet.

    Newlines stay. They are the line breaks a title block already had.
    """

    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _LABELED_DATE_RE.sub(" ", cleaned)
    cleaned = _FULL_DATE_RE.sub(" ", cleaned)
    cleaned = _MONTH_DATE_LOOSE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\b0F\b", "OF", cleaned, flags=re.IGNORECASE)
    cleaned = _SHEET_CROSS_RE.sub(" ", cleaned)
    cleaned = _SHEET_OF_NOTES_RE.sub(" ", cleaned)
    return cleaned


def _zone_follows(text: str, end: int) -> bool:
    """PAGE 4 B-6 is an off-page arrow, not this sheet's number."""

    return _SHEET_ZONE_AFTER_RE.match(text[end:].lstrip()) is not None


def _add_sheet_hit(
    hits: list[dict[str, Any]],
    number: str,
    count: str,
    *,
    strong: bool,
) -> None:
    raw = str(number or "").strip()
    if re.fullmatch(r"0+", raw):
        hits.append({"broken": True})
        return
    num = _norm_sheet(raw)
    if not num:
        return
    cnt = _norm_sheet(count) if count else ""
    if cnt and num.isdigit() and cnt.isdigit() and int(num) > int(cnt):
        hits.append({"broken": True})
        return
    hits.append({"number": num, "count": cnt, "strong": strong, "broken": False})


def _collect_sheet_hits(text: str, *, in_title_block: bool, promote: bool) -> list[dict[str, Any]]:
    """Explicit sheet marks only. A bare digit is not a hit."""

    source = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    original_lines = source.split("\n")
    raw = _prepare_sheet_text(source)
    hits: list[dict[str, Any]] = []
    for match in _SHEET_OF_RE.finditer(raw):
        label = (match.group(1) or "").upper()
        if label in {"PAGE", "PG"} and not in_title_block:
            continue
        if label in {"PAGE", "PG"} and _zone_follows(raw, match.end()):
            continue
        if not label and not in_title_block:
            continue
        _add_sheet_hit(hits, match.group(2), match.group(3), strong=bool(label) or promote)
    for match in SHEET_NO_RE.finditer(raw.upper()):
        _add_sheet_hit(hits, match.group(1), "", strong=True)
    for match in _SHEET_DWG_SH_RE.finditer(raw):
        _add_sheet_hit(hits, match.group(2), "", strong=True)
    for match in _SHEET_NAMED_RE.finditer(raw):
        label = match.group(1).upper()
        if label in {"PAGE", "PG"} and not in_title_block:
            continue
        if label in {"PAGE", "PG"} and _zone_follows(raw, match.end()):
            continue
        _add_sheet_hit(hits, match.group(2), "", strong=True)
    if in_title_block:
        for match in _SHEET_SHORT_SH_RE.finditer(raw):
            _add_sheet_hit(hits, match.group(2), "", strong=True)
    explicit = any(hit.get("number") and hit.get("strong") for hit in hits)
    for match in _SHEET_FRAC_RE.finditer(raw):
        label = (match.group(1) or "").upper()
        # A labeled PAGE 12/12 is the sheet. A bare 12/12 is the sheet only
        # when that fraction is the whole line. REV, HP, JOB, and DATE on
        # that same line are not, and they must not cancel SHEET 2 OF 5.
        # A date label claims the line under it only when that line, before
        # any date is removed, is nothing but one N/M.
        if label in {"PAGE", "PG"} and not in_title_block:
            continue
        if not label and not promote:
            if not in_title_block or _fraction_is_scale(raw, match):
                continue
            if _fraction_beside_context(raw, match):
                continue
            if explicit:
                continue
            if not _fraction_is_only_token(raw, match):
                hits.append({"broken": True})
                continue
            if _fraction_under_bare_date(raw, match, original_lines):
                hits.append({"broken": True})
                continue
        _add_sheet_hit(hits, match.group(2), match.group(3), strong=True)
    if _SHEET_BROKEN_RE.search(raw):
        hits.append({"broken": True})
    if promote:
        bare = re.fullmatch(r"0*(\d{1,3}[A-Z]?)", raw.strip(), flags=re.IGNORECASE)
        if bare:
            _add_sheet_hit(hits, bare.group(1), "", strong=True)
        elif re.fullmatch(r"[Il|?]+", raw.strip()):
            hits.append({"broken": True})
    return hits


def _line_bounds(text: str, index: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    if end < 0:
        end = len(text)
    return start, end


def _line_slice(text: str, match: re.Match[str]) -> tuple[str, int, int]:
    """The match's line, and the match offsets within that line."""

    start, end = _line_bounds(text, match.start())
    return text[start:end], match.start() - start, match.end() - start


def _fraction_beside_context(text: str, match: re.Match[str]) -> bool:
    """True when REV, DATE, JOB, HP, a unit, or A/V sits against the fraction.

    The neighbour has to be on this line. A REV on the next line is not one.
    """

    line, local_start, local_end = _line_slice(text, match)
    before = _FRACTION_NEIGHBOR.findall(line[:local_start])
    after = _FRACTION_NEIGHBOR.findall(line[local_end:])
    words = []
    if before:
        words.append(before[-1])
    if after:
        words.append(after[0])
    return any(word.upper() in _FRACTION_CONTEXT for word in words)


def _fraction_is_only_token(text: str, match: re.Match[str]) -> bool:
    """True when the fraction is the only word on its own line or in its cell."""

    line, local_start, local_end = _line_slice(text, match)
    rest = line[:local_start] + " " + line[local_end:]
    return re.sub(r"[^A-Za-z0-9]+", "", rest) == ""


def _is_blank_line(line: str) -> bool:
    return str(line or "").strip() == ""


def _is_bare_date_label(line: str) -> bool:
    """True when the line is a date label and does not already hold a value.

    ``DATE``, ``ISSUE DATE``, and ``DATE ISSUED`` are labels. ``DRAWN BY GM
    DATE`` is not: a name is left after the date word is removed. A digit
    left on the line is the date itself, so that line claims nothing below it.
    """

    if re.search(r"\b(?:DATED|DATE)\b", line, flags=re.IGNORECASE) is None:
        return False
    rest = re.sub(r"\b(?:DATED|DATE)\b", " ", line, flags=re.IGNORECASE)
    rest = rest.replace(":", " ").replace(".", " ")
    if re.search(r"\d", rest):
        return False
    words = re.findall(r"[A-Za-z]+", rest)
    return all(word.upper() in _DATE_LABEL_WORDS for word in words)


def _line_is_only_fraction(line: str) -> bool:
    return re.fullmatch(r"\s*\d{1,3}\s*/\s*\d{1,3}\s*", line or "") is not None


def _fraction_under_bare_date(
    text: str,
    match: re.Match[str],
    original_lines: list[str],
) -> bool:
    """True when this fraction is the whole line under a date label.

    The decision uses the line before dates are stripped. A date value on
    that line means the label already has its value and claims nothing.
    Blank lines between the label and the fraction are skipped.
    """

    index = text.count("\n", 0, match.start())
    if index >= len(original_lines) or not _line_is_only_fraction(original_lines[index]):
        return False
    above = index - 1
    while above >= 0 and _is_blank_line(original_lines[above]):
        above -= 1
    if above < 0:
        return False
    return _is_bare_date_label(original_lines[above])


def _fraction_is_scale(text: str, match: re.Match[str]) -> bool:
    """True for SCALE 3/7 and for a common ratio such as 1/2 with no sheet label."""

    line, local_start, _local_end = _line_slice(text, match)
    if _SCALE_BEFORE_RE.search(line[:local_start]):
        return True
    try:
        pair = (int(match.group(2)), int(match.group(3)))
    except (TypeError, ValueError):
        return False
    return pair in _DRAWING_SCALES


def _resolve_sheet_hits(hits: list[dict[str, Any]]) -> dict[str, Any]:
    """One explicit number is stored. Two different numbers are needs-info."""

    strong = [hit for hit in hits if hit.get("number") and hit.get("strong")]
    weak = [hit for hit in hits if hit.get("number") and not hit.get("strong")]
    broken = any(hit.get("broken") for hit in hits)

    def choose(group: list[dict[str, Any]]) -> dict[str, Any] | None:
        numbers = {str(hit["number"]) for hit in group}
        if len(numbers) > 1:
            return {**_empty_sheet_fields(), "sheet_number_needs_info": True}
        if len(numbers) != 1:
            return None
        number = next(iter(numbers))
        count = next((str(hit["count"]) for hit in group if hit.get("count")), "")
        return {"sheet_number": number, "sheet_count": count, "sheet_number_needs_info": False}

    chosen = choose(strong)
    if chosen is not None:
        return chosen
    chosen = choose(weak)
    if chosen is not None:
        return chosen
    if broken:
        return {**_empty_sheet_fields(), "sheet_number_needs_info": True}
    return _empty_sheet_fields()


def sheet_fields_from_text(
    text: str,
    *,
    in_title_block: bool,
    labeled_values: list[str] | None = None,
    confidence: float | None = None,
) -> dict[str, Any]:
    """Sheet number and count from title-block text.

    ``SH.NO. 9C`` stays 9C. ``SHEET 3 OF 9``, ``DWG … SH 3``, and ``PAGE 4``
    are the same fact. ``in_title_block`` is required before a bare ``6 OF 6``
    or a page number counts, so a note on the drawing cannot become the sheet.
    A second different number, a count that is smaller than the sheet, or an
    OCR confidence under ``SHEET_OCR_MIN_CONFIDENCE`` is needs-info and is not stored.
    """

    hits = _collect_sheet_hits(text, in_title_block=in_title_block, promote=False)
    for value in labeled_values or []:
        hits.extend(_collect_sheet_hits(value, in_title_block=True, promote=True))
    found = _resolve_sheet_hits(hits)
    return _drop_uncertain_sheet(found, confidence)


def _drop_uncertain_sheet(found: dict[str, Any], confidence: float | None) -> dict[str, Any]:
    if not found.get("sheet_number") or confidence is None:
        return found
    if float(confidence) >= SHEET_OCR_MIN_CONFIDENCE:
        return found
    return {**_empty_sheet_fields(), "sheet_number_needs_info": True}


def _sheet_number_confidence(tokens: list[dict[str, Any]], number: str) -> float | None:
    """Best OCR score of this sheet number near a sheet label. Vector text has none."""

    target = _norm_sheet(number)
    if not target:
        return None
    labels: list[dict[str, Any]] = []
    for tok in tokens:
        raw = str(tok.get("text") or "").upper()
        word = raw.strip(".")
        if word in {"SHEET", "SHT", "PAGE", "PG", "SH"} or raw.startswith(("SH.", "SHT", "S5.NO", "SH.NO")):
            labels.append(tok)
    scores: list[float] = []
    for tok in tokens:
        text = str(tok.get("text") or "")
        if _norm_sheet(text) != target and target not in text.upper():
            continue
        if "confidence" not in tok or tok.get("confidence") is None:
            continue
        if labels:
            try:
                x = float(tok.get("x") or 0)
                y = float(tok.get("y") or 0)
            except (TypeError, ValueError):
                continue
            near = False
            for lab in labels:
                try:
                    lx = float(lab.get("x") or 0)
                    ly = float(lab.get("y") or 0)
                except (TypeError, ValueError):
                    continue
                if abs(y - ly) < 14 and abs(x - lx) < 90:
                    near = True
                    break
            if not near:
                continue
        try:
            scores.append(float(tok["confidence"]))
        except (TypeError, ValueError):
            continue
    if not scores:
        return None
    return max(scores)


def _read_sheet_numbers(text: str, *, allow_of: bool) -> tuple[str, str]:
    """Sheet number and count. ``allow_of`` means the text is already in the title block."""

    found = sheet_fields_from_text(text, in_title_block=allow_of)
    return str(found["sheet_number"]), str(found["sheet_count"])


def detect_drawing_standard(
    *,
    title_standard: str = "",
    devices: list[dict[str, Any]] | None = None,
    wires: list[dict[str, Any]] | None = None,
    texts: list[str] | None = None,
    rails: list[str] | None = None,
) -> str:
    """NEMA/ANSI, IEC, mixed, or empty when the page does not say.

    Title-block text that already named a standard wins, so a GE elementary
    sheet stays NEMA and an IEC title stays IEC. Otherwise the tags, rails,
    and symbol words on the page are the evidence. Both sides, or neither,
    do not become a guessed IEC sheet.
    """

    from panelcad.importer.symbols import supported_tag_family, tag_prefix
    from panelcad.library.catalog import normalize_standard

    named = str(title_standard or "").strip()
    if named:
        token = named.lower()
        if token in {"mixed", "unknown"}:
            return "mixed" if token == "mixed" else ""
        norm = normalize_standard(named, default="")
        if norm in {"NEMA/ANSI", "IEC"}:
            return norm
    nema = 0
    iec = 0
    blob = " ".join(str(text or "") for text in (texts or [])).upper()
    if re.search(r"\b(NEMA|ANSI|JIC|NFPA)\b", blob):
        nema += 3
    if re.search(r"\bIEC\b", blob):
        iec += 3
    if re.search(r"\b(JOB\s*NO|SH\.?\s*NO|ELEMENTARY)\b", blob) and "IEC" not in blob:
        nema += 3
    # CR, PB, LS, FU, OL, PL, DISC, and MTR are ordinary NEMA/JIC tag letters.
    # PBLT and DRIVE are not. Power rails L1/L2/L3 and control rails 24V/0V
    # are used on both standards, and X1/X2 is a transformer secondary, so
    # none of those names score a page.
    nema_prefixes = {"CR", "PB", "LS", "FU", "OL", "PLT", "PL", "DISC", "MTR", "MOT"}
    iec_prefixes = {"K", "Q", "S"}
    for device in devices or []:
        if not isinstance(device, dict):
            continue
        raw_tag = str(device.get("tag") or "").strip().upper()
        dashed = raw_tag.startswith("-")
        tag = raw_tag.lstrip("-")
        prefix = tag_prefix(tag)
        symbol = str(device.get("symbol") or device.get("type") or "").strip().lower()
        if re.match(r"\d+[A-Z]", tag):
            nema += 2
        if prefix in nema_prefixes:
            nema += 1
        if prefix in iec_prefixes and re.fullmatch(rf"{prefix}\d+", tag):
            iec += 2 if dashed else 1
        if symbol in {"coil", "contact"} and prefix in nema_prefixes:
            nema += 1
        if symbol in {"contactor", "breaker"} and prefix in iec_prefixes:
            iec += 1
        if symbol:
            family = supported_tag_family(tag)
            if family in {"relay", "pushbutton", "limit", "fuse", "pilot"} and prefix in nema_prefixes:
                nema += 1
    # Shared rail names are not evidence. L+, M stay IEC control rails.
    iec_rails = {"L+", "L-", "M"}
    rail_names = [str(name or "").strip().upper() for name in (rails or [])]
    for wire in wires or []:
        if isinstance(wire, dict):
            rail_names.append(str(wire.get("bus") or "").strip().upper())
    if any(name in iec_rails for name in rail_names):
        iec += 2
    if nema >= 2 and iec >= 2:
        return "mixed"
    if nema >= 2 and nema > iec:
        return "NEMA/ANSI"
    if iec >= 2 and iec > nema:
        return "IEC"
    if nema and iec:
        return "mixed"
    return ""


# A diagram title is not a layout. "Wiring" is not one of these: a wiring layout is a layout.
_DIAGRAM_WORDS = {"schematic", "diagram", "ladder", "elementary"}
_NAME_SUFFIXES = {
    "section",
    "front",
    "rear",
    "left",
    "right",
    "top",
    "bottom",
    "mcc",
    "no",
    "number",
    "upper",
    "lower",
    "inner",
    "outer",
    "rev",
    "general",
    "detail",
    "layout",
    "arrangement",
    "wiring",
    "mounting",
    "plate",
    "back",
    "panel",
}
_REFERENCE_WORDS = {"refer", "ref", "see", "revised", "for", "on", "note", "notes"}
# Revision notes that are not the sheet name when they stand in for a missing TITLE.
_REVISION_WORDS = {"updated", "built"}


def _title_words(title: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", str(title or "").lower())


def _starts_with_pointer(title: str) -> bool:
    """A labelled title that is only a pointer: "SEE …" or "REFER TO …"."""

    words = _title_words(title)
    return bool(words) and words[0] in {"see", "refer"}


def _is_cross_reference(line: str) -> bool:
    """A title-band stand-in that points at another sheet, or a revision note.

    This does not apply to a labelled TITLE field. "Panel Layout Sheet 2" in
    that field is the sheet name. ``sheet`` or ``dwg`` plus a number, and a
    revision word, disqualify only a line that is standing in for the label.
    """

    words = _title_words(line)
    if any(word in _REFERENCE_WORDS or word in _REVISION_WORDS for word in words):
        return True
    raw = str(line or "")
    if re.search(r"\b(?:sheet|sht)\b\W*\d+", raw, re.I):
        return True
    if re.search(r"\bdwg\b\W*[a-z]*\W*\d+", raw, re.I):
        return True
    return False


def _is_door_title(words: list[str]) -> bool:
    """Door or doors. A trailing s does not make it a panel."""

    stemmed = _stem_subject_words(words)
    return "door" in stemmed or "door_layout" in words


def _stem_subject_words(words: list[str]) -> list[str]:
    """Plural enclosure nouns count as the singular: panels, backplates, subpanels."""

    stemmed: list[str] = []
    for word in words:
        if word.endswith("s") and word[:-1] in _SUBJECT_NOUNS:
            stemmed.append(word[:-1])
        else:
            stemmed.append(word)
    return stemmed


def _is_enclosure_kind(words: list[str]) -> bool:
    """A detail of the box: enclosure, backplate, backpanel, or a mounting plate.

    "Back plate" is the same part as "back panel". A trailing s does not change it.
    """

    stemmed = _stem_subject_words(words)
    if any(word in {"enclosure", "backplate", "backpanel"} for word in stemmed):
        return True
    joined = " ".join(stemmed)
    return "back panel" in joined or "back plate" in joined or "mounting plate" in joined


# Nouns that name the thing being laid out. A sentence that merely mentions a layout
# has other words, and those words keep the line from becoming the sheet title.
_LAYOUT_NOUNS = {
    "enclosure",
    "backplate",
    "backpanel",
    "cabinet",
    "subpanel",
    "door",
    "panel",
    "component",
    "equipment",
    "device",
    "drawing",
    "bom",
    "control",
    "door_layout",
    "panel_layout",
}


def _layout_name_words(words: list[str]) -> bool:
    """True when the line is a name, not a sentence that mentions a layout."""

    allowed = _NAME_SUFFIXES | _LAYOUT_NOUNS | {"and"}
    for word in words:
        if word in allowed:
            continue
        if word.isdigit() or len(word) == 1 or re.fullmatch(r"[a-z]\d+|\d+[a-z]", word):
            continue
        return False
    return True


_VIEW_WORDS = {"layout", "arrangement", "elevation", "cutout", "detail", "view"}
_PHYSICAL_NOUNS = {"panel", "cabinet", "enclosure", "rack", "console", "box", "pedestal", "cubicle"}
_SUBJECT_NOUNS = _PHYSICAL_NOUNS | {"door", "backplate", "backpanel", "subpanel", "plate", "panel"}
_SHEET_TITLE_PREFIXES = {"DRAWING", "SHEET", "DWG", "DRAWING SHEET"}
_OTHER_TITLE_PREFIXES = {"PROJECT", "JOB", "CLIENT", "CUSTOMER"}


def _with_view_stems(words: list[str]) -> list[str]:
    """Plural view words count as the singular: layouts, cutouts, elevations, details."""

    stemmed = list(words)
    for word in words:
        if word.endswith("s") and word[:-1] in _VIEW_WORDS and word[:-1] not in stemmed:
            stemmed.append(word[:-1])
    return stemmed


def _is_building_view(words: list[str]) -> bool:
    """A floor plan or a plan-and-elevation with no panel, enclosure, or door.

    An enclosure on the same sheet keeps the elevation. "Floor plan and
    enclosure elevation" is that sheet. "Floor plan and elevation" is not.
    """

    joined = " ".join(words)
    plan = "floor plan" in joined or "site plan" in joined or ("plan" in words and "elevation" in words)
    if not plan:
        return False
    if _layout_subject(words) or any(word in _PHYSICAL_NOUNS for word in words):
        return False
    return True


def _is_layout_view(words: list[str]) -> bool:
    """Layout, arrangement, elevation, or a front view. The noun is separate.

    A floor plan is a building drawing. A floor-standing enclosure elevation
    still names the enclosure.
    """

    if _is_building_view(words):
        return False
    if any(word in words for word in ("layout", "arrangement", "elevation")):
        return True
    return "front" in words and "view" in words


def _layout_subject(words: list[str]) -> bool:
    """A door, an enclosure, or a panel. Cutout and detail need one of these."""

    return _is_door_title(words) or "panel" in words or _is_enclosure_kind(words)


def _layout_title_kind(title: str) -> tuple[str, str]:
    """Kind and strength from a sheet title.

    The word layout, arrangement, or elevation is a layout. So is a cutout or
    a detail of a door, enclosure, or panel. The noun only chooses door or
    panel. A title that starts with see or refer is a pointer, not a name.
    Strength ``strong`` wins over rails and dimensions.
    """

    words = _with_view_stems(_title_words(title))
    if not words or _starts_with_pointer(title):
        return "", ""
    if any(word in _DIAGRAM_WORDS for word in words):
        return "", ""
    if "door_layout" in words and "panel_layout" not in words:
        return "door_layout", "strong"
    if "panel_layout" in words:
        return "panel_layout", "strong"
    door = _is_door_title(words)
    kind = "door_layout" if door else "panel_layout"
    if _is_layout_view(words):
        return kind, "strong"
    if "cutout" in words and _layout_subject(words):
        return kind, "strong"
    # A nameplate detail is the nameplate, not the door or the enclosure.
    if "detail" in words and _layout_subject(words) and "nameplate" not in words:
        return kind, "strong"
    if _on_door_phrase(title):
        return "door_layout", "strong"
    plain = _title_words(title)
    if any(left == "door" and right in {"mounted", "mtd"} for left, right in zip(plain, plain[1:], strict=False)):
        return "door_layout", "strong"
    if _is_enclosure_kind(words) and _layout_name_words(words):
        return "panel_layout", "strong"
    if (
        door
        and _layout_name_words(words)
        and not any(word in _NOT_A_PLACEMENT or word == "switch" for word in words)
    ):
        return "door_layout", "weak"
    return "", ""


# One set for a tail after a separator and a trailing tail with no separator.
_STATUS_WORDS = {"revised", "existing", "typ", "continued", "cont"}
_DOOR_GEAR = {
    "device",
    "devices",
    "component",
    "components",
    "equipment",
    "pushbutton",
    "pushbuttons",
    "light",
    "lights",
    "pilot",
    "pilots",
}
_STATE_BEFORE_DOOR = {"open", "closed", "when"}
_NOT_A_PLACEMENT = {"circuit", "interlock", "wiring"}
_HARD_SPLIT = re.compile(r"(?:\s[-–—]|[-–—]\s)|(?:,)|(?:\s/|/\s)|(?::)|(?:\(|\))|\b(?:for|at)\b", re.I)
_IN_WORD = re.compile(r"\b(?:in|inside)\b", re.I)


def _strip_title_qualifiers(words: list[str]) -> list[str]:
    """Drop a trailing sheet number, revision, notes, or drawing number.

    These sit on the name with no separator: sht 2, rev B, revision 2, E-101.
    Words that are not that shape stay, so "on door open" does not match.
    """

    trimmed = list(words)
    changed = True
    while changed and trimmed:
        changed = False
        if (
            len(trimmed) >= 4
            and trimmed[-4] in {"sht", "sheet"}
            and trimmed[-3].isdigit()
            and trimmed[-2] == "of"
            and trimmed[-1].isdigit()
        ):
            trimmed = trimmed[:-4]
            changed = True
            continue
        if len(trimmed) >= 2 and trimmed[-2] in {"sht", "sheet"} and trimmed[-1].isdigit():
            trimmed = trimmed[:-2]
            changed = True
            continue
        if len(trimmed) >= 2 and trimmed[-2] in {"rev", "revision"}:
            trimmed = trimmed[:-2]
            changed = True
            continue
        if len(trimmed) >= 3 and trimmed[-3].isdigit() and trimmed[-2] == "and" and trimmed[-1].isdigit():
            trimmed = trimmed[:-3]
            changed = True
            continue
        if len(trimmed) >= 2 and trimmed[-2] == "and" and trimmed[-1] in {"notes", "note", "bom"}:
            trimmed = trimmed[:-2]
            changed = True
            continue
        if len(trimmed) >= 2 and trimmed[-2] == "as" and trimmed[-1] == "built":
            trimmed = trimmed[:-2]
            changed = True
            continue
        if len(trimmed) >= 2 and trimmed[-2] == "no" and trimmed[-1].isdigit():
            trimmed = trimmed[:-2]
            changed = True
            continue
        if trimmed[-1] in _STATUS_WORDS or trimmed[-1] in {"revised", "rev", "revision"}:
            trimmed = trimmed[:-1]
            changed = True
            continue
        if len(trimmed) >= 2 and len(trimmed[-2]) == 1 and trimmed[-2].isalpha() and trimmed[-1].isdigit():
            trimmed = trimmed[:-2]
            changed = True
            continue
        if re.fullmatch(r"[a-z]\d+|\d+[a-z]", trimmed[-1]):
            trimmed = trimmed[:-1]
            changed = True
            continue
        if len(trimmed) > 1 and trimmed[-1].isdigit():
            trimmed = trimmed[:-1]
            changed = True
            continue
    return trimmed


def _door_indexes(words: list[str]) -> list[int]:
    return [index for index, word in enumerate(words) if word in {"door", "doors"}]


def _physical_subject_words(words: list[str]) -> bool:
    """Enclosure, panel, cabinet, rack, console, subpanel, or a mounting plate.

    A modifier such as interior or side does not hide the noun. Plurals count.
    """

    stemmed = _stem_subject_words(words)
    if not stemmed:
        return False
    if any(word in _PHYSICAL_NOUNS or word == "subpanel" for word in stemmed):
        return True
    return _layout_subject(stemmed) or _is_enclosure_kind(stemmed)


def _joined_subject_after_door(words: list[str], door_at: int) -> bool:
    """'door and backpanel', 'door & subpanel', or 'door and mounting plate'."""

    after = words[door_at + 1 :]
    if not after:
        return False
    if after[0] == "and":
        after = after[1:]
    return _physical_subject_words(after)


def _placement_ends_with_door(words: list[str]) -> bool:
    """On/in/inside … door(s), with door ending the clause or joined to a panel."""

    doors = _door_indexes(words)
    if not doors:
        return False
    door_at = doors[-1]
    if not any(word in {"on", "in", "inside"} for word in words[:door_at]):
        return False
    if door_at == len(words) - 1:
        return True
    return _joined_subject_after_door(words, door_at)


def _subject_span(words: list[str], start: int) -> int:
    """Words that name a panel part, one word or a pair such as mounting plate."""

    if start >= len(words):
        return 0
    one = _physical_subject_words(words[start : start + 1])
    if start + 1 < len(words):
        two = _physical_subject_words(words[start : start + 2])
        if two and not one:
            return 2
    return 1 if one else 0


def _door_equipment_phrase(words: list[str]) -> bool:
    """'Door devices', 'door and subpanel mounted devices', or a list of panel parts.

    After the door, mounted, and, and panel-part words may repeat. A circuit,
    wiring, or schematic word still cancels the phrase.
    """

    for index, word in enumerate(words):
        if word not in {"door", "doors"}:
            continue
        gear_at = index + 1
        while gear_at < len(words):
            if words[gear_at] in {"mounted", "mtd", "and"}:
                gear_at += 1
                continue
            span = _subject_span(words, gear_at)
            if span:
                gear_at += span
                continue
            break
        if gear_at >= len(words) or words[gear_at] not in _DOOR_GEAR:
            continue
        rest = words[gear_at:]
        if any(item in {"circuit", "wiring", "schematic", "interlock", "diagram"} for item in rest):
            continue
        return True
    return False


def _in_word_is_placement(title: str, start: int) -> bool:
    """True when this 'in'/'inside' introduces the door, not a location tail."""

    tail = str(title or "")[start:]
    stop = _HARD_SPLIT.search(tail, 1)
    chunk = tail[: stop.start()] if stop else tail
    words = _strip_title_qualifiers(_title_words(chunk))
    if not words or words[0] not in {"in", "inside"}:
        return False
    return _placement_ends_with_door(words)


def _split_points(title: str) -> list[int]:
    text = str(title or "")
    points = [match.start() for match in _HARD_SPLIT.finditer(text)]
    for match in _IN_WORD.finditer(text):
        if _in_word_is_placement(text, match.start()):
            continue
        points.append(match.start())
    return sorted(set(points))


def _segments(title: str) -> list[str]:
    text = str(title or "")
    points = _split_points(text)
    if not points:
        return [text] if text.strip() else []
    parts: list[str] = []
    cursor = 0
    for point in points:
        parts.append(text[cursor:point])
        cursor = point
        # Drop the separator token itself so the next segment starts at the name.
        sep = _HARD_SPLIT.match(text, point) or _IN_WORD.match(text, point)
        if sep:
            cursor = sep.end()
    parts.append(text[cursor:])
    return [part for part in parts if part.strip()]


def _segment_carries_name(segment: str) -> bool:
    """A view word or a door phrase. A bare enclosure name is not this strong."""

    words = _title_words(segment)
    if not words:
        return False
    stemmed = _with_view_stems(words)
    if any(word in {"layout", "arrangement", "elevation"} for word in stemmed):
        return True
    if "front" in words and "view" in words:
        return True
    plain = _strip_title_qualifiers(words)
    return _placement_ends_with_door(plain) or _door_equipment_phrase(plain)


def _segment_rank(segment: str) -> int:
    """2 when the segment has a view or a door phrase. 1 for a bare enclosure name."""

    if _segment_carries_name(segment):
        return 2
    words = _title_words(segment)
    if not words or any(word.isdigit() for word in words):
        return 0
    plain = _strip_title_qualifiers(words)
    if _layout_subject(plain) or _is_enclosure_kind(plain) or "subpanel" in plain:
        return 1
    return 0


def _name_clause(title: str) -> str:
    """The segment that names the sheet.

    A view word or a door phrase wins. A bare enclosure name such as
    "control panel" is only the name when no later segment is stronger.
    A leading tag or sheet number is skipped.
    """

    weak = ""
    for segment in _segments(title):
        rank = _segment_rank(segment)
        if rank >= 2:
            return segment
        if rank == 1 and not weak:
            weak = segment
    return weak or str(title or "")


def _equipment_clause(title: str) -> str:
    """Name up to a dash or parenthesis. A comma stays, so a part list can continue."""

    match = re.search(r"(?:\s[-–—]|[-–—]\s)|\(|\b(?:for|at)\b", str(title or ""), flags=re.I)
    if not match:
        return str(title or "")
    head = str(title or "")[: match.start()]
    if not head.strip():
        return str(title or "")
    return head


def _on_door_phrase(title: str) -> bool:
    """True when the name clause places gear on or in the door.

    Articles and other words may sit between on/in and door. A word after door
    in the same clause, other than "and" a panel, is not this phrase. A circuit
    or a state word such as open/when is not a placement.
    """

    if _door_equipment_phrase(_strip_title_qualifiers(_title_words(_equipment_clause(title)))):
        return True
    words = _strip_title_qualifiers(_title_words(_name_clause(title)))
    if _door_equipment_phrase(words):
        return True
    doors = _door_indexes(words)
    if not doors or not _placement_ends_with_door(words):
        return False
    door_at = doors[-1]
    if door_at > 0 and words[door_at - 1] in _STATE_BEFORE_DOOR:
        return False
    place_at = next(index for index, word in enumerate(words) if word in {"on", "in", "inside"})
    if any(word in _NOT_A_PLACEMENT for word in words[:place_at]):
        return False
    return True


def _view_head(title: str) -> str:
    """The noun directly in front of the first layout, arrangement, or elevation.

    The name is the first segment that carries one, so a leading tag is skipped
    and a view word after a dash stays in the tail.
    "Terminal box layout" is a box. "Junction box terminal layout" is the terminal plan.
    """

    words = _with_view_stems(_title_words(_name_clause(title)))
    view_at = None
    for index, word in enumerate(words):
        if word in {"layout", "arrangement", "elevation"}:
            view_at = index
            break
    if view_at is None:
        return ""
    index = view_at - 1
    while index >= 0 and words[index] in {"and", "of", "the", "a", "an", "with"}:
        index -= 1
    if index < 0:
        return ""
    word = words[index]
    if word in _PHYSICAL_NOUNS or word in {"door", "backplate", "backpanel", "plate"}:
        return "physical"
    if word == "terminal":
        return "terminal"
    if word in {"network", "ethernet", "modbus"}:
        return "network"
    if word == "o" and index > 0 and words[index - 1] == "i":
        return "io"
    return ""


def _specific_title_kind(title: str) -> str:
    """Terminal, PLC I/O, and network keep their kind when they are the head.

    The noun in front of layout, arrangement, or elevation decides. A panel,
    cabinet, enclosure, rack, console, or box there is a layout. Bare "plc"
    without I/O is left for the later check.
    """

    clause = _name_clause(title)
    head = _view_head(title)
    if head == "physical":
        return ""
    if head == "terminal":
        return "terminal_plan"
    if head == "io":
        return "plc_io"
    if head == "network":
        return "network"
    title_l = clause.lower()
    if "terminal" in title_l:
        return "terminal_plan"
    if "i/o" in title_l:
        return "plc_io"
    if any(word in title_l for word in ("ethernet", "rs-485", "rs485", "network", "modbus")):
        return "network"
    return ""


def _sheet_title_label(text: str) -> bool:
    """DRAWING TITLE, SHEET TITLE, DWG TITLE, or DRAWING SHEET TITLE.

    PROJECT TITLE, JOB TITLE, CLIENT TITLE, and CUSTOMER TITLE name the job,
    not the sheet.
    """

    folded = " ".join(str(text or "").split()).upper()
    if not folded.endswith(" TITLE") or folded == "TITLE":
        return False
    prefix = folded[: -len(" TITLE")].strip()
    return prefix in _SHEET_TITLE_PREFIXES


def _title_under_extended_label(band: list[dict[str, Any]], zone_ys: set[int]) -> str:
    """Value under a sheet-title label such as DRAWING TITLE."""

    for tok in band:
        raw = str(tok.get("text") or "").strip()
        if not _sheet_title_label(raw):
            continue
        found = _join_under(band, raw, zone_ys)
        if found:
            return found
    return ""


def _token_height(tok: dict[str, Any]) -> float:
    try:
        height = float(tok.get("h") or 0)
    except (TypeError, ValueError):
        height = 0.0
    return height if height > 0.2 else 2.4


def _token_width(tok: dict[str, Any]) -> float:
    try:
        width = float(tok.get("w") or 0)
    except (TypeError, ValueError):
        width = 0.0
    if width > 0.2:
        return width
    return max(1.0, len(str(tok.get("text") or "")) * _token_height(tok) * 0.5)


def _token_span(tok: dict[str, Any]) -> tuple[float, float]:
    """Horizontal span. PDF words store a center; the width reaches both ways."""

    x = float(tok["x"])
    width = _token_width(tok)
    return x - width / 2.0, x + width / 2.0


def _band_text_lines(band: list[dict[str, Any]]) -> list[str]:
    """Join words that sit on one row into the line a person reads.

    A PDF stores one word per token. "REVISED PANEL LAYOUT" must be judged as
    that line, not as the single word LAYOUT. A field label breaks the line.
    Words further apart than about two text-heights are a different phrase.
    """

    rows: list[list[dict[str, Any]]] = []
    for tok in sorted(band, key=lambda item: (float(item["y"]), float(item["x"]))):
        y = float(tok["y"])
        placed = False
        for row in rows:
            if abs(y - float(row[0]["y"])) < 1.4:
                row.append(tok)
                placed = True
                break
        if not placed:
            rows.append([tok])
    lines: list[str] = []
    for row in rows:
        row.sort(key=lambda item: float(item["x"]))
        buf: list[str] = []
        prev_right: float | None = None
        prev_height = 2.4
        for tok in row:
            text = " ".join(str(tok.get("text") or "").split())
            if not text:
                continue
            if _is_field_label(text):
                if buf:
                    lines.append(" ".join(buf))
                    buf = []
                prev_right = None
                continue
            left, right = _token_span(tok)
            height = _token_height(tok)
            if prev_right is not None and left - prev_right > 2.0 * max(prev_height, height):
                if buf:
                    lines.append(" ".join(buf))
                buf = []
            buf.append(text)
            prev_right = right
            prev_height = height
        if buf:
            lines.append(" ".join(buf))
    return lines


def _layout_line_in_band(band: list[dict[str, Any]]) -> str:
    """A short layout name standing in for a missing TITLE label.

    The equipment name in front of layout is not a fixed list. A cross-reference
    or a revision note never becomes the title. Checks run on the whole line.
    """

    named = ""
    for line in _band_text_lines(band):
        if not line or _is_cross_reference(line):
            continue
        words = _title_words(line)
        if any(word in {"panel_layout", "door_layout"} for word in words):
            continue
        if len(words) > 6:
            continue
        kind, strength = _layout_title_kind(line)
        if not kind:
            continue
        named = line
        if strength == "strong":
            break
    return named


def _kind_token_in_band(band: list[dict[str, Any]]) -> str:
    """``panel_layout`` or ``door_layout`` printed on the title strip.

    The DWG line that carries the token is not the sheet title. The token
    still names the kind. A mention outside this band does not.
    """

    found = ""
    for tok in band:
        words = _title_words(str(tok.get("text") or ""))
        if "panel_layout" in words:
            found = "panel_layout"
        elif "door_layout" in words and found != "panel_layout":
            found = "door_layout"
    return found


def _rung_evidence(body: str) -> bool:
    """A wire number sitting between two device tags on one short line.

    L1/L2/L3 and dimensions anywhere on the page are not a rung. A whole
    page joined into one line is not one rung either.
    """

    for raw in re.split(r"[\r\n]+", body or ""):
        parts = re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]*", raw)
        if len(parts) < 3 or len(parts) > 14:
            continue
        tag_at = [index for index, part in enumerate(parts) if is_device_tag(part)]
        if len(tag_at) < 2:
            continue
        for left, right in zip(tag_at, tag_at[1:], strict=False):
            if any(re.fullmatch(r"\d{2,4}", part) for part in parts[left + 1 : right]):
                return True
    return False


def page_kind_from_title(title_block: dict[str, Any], body: str) -> str:
    """Page kind from the sheet title, then a title-strip kind token."""

    kind = classify_text(str(title_block.get("title") or ""), body)
    hint = str(title_block.get("layout_kind") or "")
    if hint in {"panel_layout", "door_layout"} and kind in {"", "other"}:
        return hint
    return kind


def classify_text(title: str, body: str, standard_hint: str = "") -> str:
    """Page kind from words already on the sheet. No vision."""

    title_l = (title or "").lower()
    blob = f"{title} {body}".lower()
    if any(phrase in blob for phrase in ("bill of material", "parts list", "part number")) and (
        "qty" in blob or "catalog" in blob
    ):
        return "bom"
    if "table of contents" in blob or "cover sheet" in blob:
        return "cover" if "cover" in blob else "index"
    if title_l.strip() in {"index", "contents"}:
        return "index"
    # Terminal, PLC I/O, and network are more specific than layout. A labelled
    # title that says layout still wins over rails. A pointer is not a layout.
    specific = _specific_title_kind(title or "")
    if specific:
        return specific
    layout, strength = _layout_title_kind(title or "")
    if layout in {"door_layout", "panel_layout"} and (strength == "strong" or not _rung_evidence(body)):
        return layout
    if "terminal" in title_l or "terminal plan" in blob or "terminal strip" in blob:
        return "terminal_plan"
    if any(word in title_l for word in ("i/o", "plc")) or "plc i/o" in blob:
        return "plc_io"
    if any(word in title_l for word in ("ethernet", "rs-485", "rs485", "network", "modbus")):
        return "network"
    # The project name ("DOL Motor Starter") is printed on every sheet. The
    # sheet title decides. A control sheet stays a control sheet.
    power_title = "motor starter" in title_l or (
        "elementary" in title_l and any(word in blob for word in ("motor", "starter", "power"))
    )
    if not power_title and "elementary" in blob and "motor" in blob and "starter" in blob:
        power_title = True
    if power_title and "control" not in title_l and "ladder" not in title_l:
        return "schematic_power"
    if "ladder" in blob and ("control" in title_l or "elementary" in blob):
        return "schematic_ladder"
    if "power" in title_l:
        return "schematic_power"
    if "control" in title_l:
        return "schematic_ladder" if "ladder" in blob else "schematic_control"
    if "ladder" in blob:
        return "schematic_ladder"
    if standard_hint:
        return "other"
    return "other"


def _has_drawing_text(tokens: list[dict[str, Any]], width_mm: float, height_mm: float) -> bool:
    working = [tok for tok in tokens if _in_working(tok, width_mm, height_mm)]
    return len(working) >= 6


def _border_grid_token(
    tok: dict[str, Any],
    width_mm: float,
    height_mm: float,
    insets: tuple[float, float, float, float] | None = None,
) -> bool:
    """Page-border row numbers (01–61), column letters, and OCR of those cells."""

    if not in_sheet_border(float(tok["x"]), float(tok["y"]), width_mm, height_mm, insets):
        return False
    text = str(tok["text"] or "").strip().upper()
    # A 4-digit wire number in the margin is not a zone digit (zones are 01–61).
    if re.fullmatch(r"\d{1,3}", text):
        return True
    if re.fullmatch(r"\d{1,2}[A-Z]", text):
        return True
    return len(text) == 1 and text.isalpha() and text not in CONVENTIONAL_WIRES


def _grid_junk_tag(
    tok: dict[str, Any],
    width_mm: float,
    height_mm: float,
    insets: tuple[float, float, float, float] | None = None,
) -> bool:
    """OCR of a grid cell such as 0S is not a device, even when it matches a tag."""

    if not in_sheet_border(float(tok["x"]), float(tok["y"]), width_mm, height_mm, insets):
        return False
    text = str(tok["text"] or "").strip().upper()
    return bool(re.fullmatch(r"\d{1,2}[A-Z]", text) or re.fullmatch(r"\d{1,2}", text))


def _damaged_xref_number(text: str) -> bool:
    """9834 is [9B34] with B read as 8. A plain 4-digit wire number is not this shape."""

    return bool(re.fullmatch(r"\d{1,2}8\d{2}", str(text or "").strip().upper()))


def _outer_band(x: float, y: float, width_mm: float, height_mm: float) -> bool:
    cap_x = min(width_mm * 0.22, 50.0) if width_mm else 0.0
    cap_y = min(height_mm * 0.22, 50.0) if height_mm else 0.0
    if cap_x and (x < cap_x or x > width_mm - cap_x):
        return True
    if cap_y and (y < cap_y or y > height_mm - cap_y):
        return True
    return False


def _near_damaged_xref(tok: dict[str, Any], tokens: list[dict[str, Any]]) -> bool:
    """9834 beside [9B34] is a B read as 8, not a wire number."""

    text = str(tok.get("text") or "").strip().upper()
    if not re.fullmatch(r"\d{3,4}", text):
        return False
    damaged = _damaged_xref_number(text)
    x = float(tok.get("x") or 0)
    y = float(tok.get("y") or 0)
    for other in tokens:
        if other is tok:
            continue
        if abs(float(other.get("x") or 0) - x) > 30 or abs(float(other.get("y") or 0) - y) > 12:
            continue
        other_text = str(other.get("text") or "").upper()
        if "[" in other_text or "]" in other_text or is_cross_ref(other_text):
            return True
        if damaged and re.search(r"\dB\d", other_text):
            return True
    return False


def grid_column_insets(
    tokens: list[dict[str, Any]],
    width_mm: float,
    height_mm: float,
) -> tuple[float, float, float, float] | None:
    """Insets that cover a column of row numbers or a row of column letters.

    The zone grid sits about 40 mm in, past the historical 14 mm margin.
    A lone wire is not a column of row numbers, so this does not empty a ladder.
    """

    if width_mm <= 40 or height_mm <= 40:
        return None
    cap_x = min(width_mm * 0.22, 50.0)
    cap_y = min(height_mm * 0.22, 50.0)
    columns: dict[int, list[float]] = {}
    rows: dict[int, list[float]] = {}
    for tok in tokens:
        text = str(tok.get("text") or "").strip().upper()
        try:
            x = float(tok.get("x") or 0)
            y = float(tok.get("y") or 0)
        except (TypeError, ValueError):
            continue
        if re.fullmatch(r"\d{2}", text):
            value = int(text)
            if 1 <= value <= 80:
                columns.setdefault(round(x / 4.0) * 4, []).append(x)
        elif re.fullmatch(r"[A-Z]", text) and text not in {"L", "N"}:
            rows.setdefault(round(y / 4.0) * 4, []).append(y)
    left = 0.0
    right = 0.0
    for _key, xs in columns.items():
        if len(xs) < 8 or max(xs) - min(xs) > 8:
            continue
        center = sum(xs) / len(xs)
        if center <= cap_x:
            left = max(left, center + 6.0)
        from_right = width_mm - center
        if from_right <= cap_x:
            right = max(right, from_right + 6.0)
    top = 0.0
    for _key, ys in rows.items():
        if len(ys) < 8 or max(ys) - min(ys) > 8:
            continue
        center = sum(ys) / len(ys)
        if center <= cap_y:
            top = max(top, center + 6.0)
    if left <= 0 and right <= 0 and top <= 0:
        return None
    return (min(left, cap_x), min(top, cap_y), min(right, cap_x), 0.0)


def effective_insets(
    frame: tuple[float, float, float, float] | None,
    tokens: list[dict[str, Any]],
    width_mm: float,
    height_mm: float,
) -> tuple[float, float, float, float] | None:
    """The deeper of the drawing frame and a detected zone-grid column."""

    grid = grid_column_insets(tokens, width_mm, height_mm)
    if frame is None and grid is None:
        return None
    base = frame or (14.0, 16.0, 14.0, 54.0)
    extra = grid or (0.0, 0.0, 0.0, 0.0)
    cap_x = min(width_mm * 0.22, 50.0) if width_mm else 50.0
    cap_y = min(height_mm * 0.22, 50.0) if height_mm else 50.0
    return (
        min(cap_x, max(base[0], extra[0])),
        min(cap_y, max(base[1], extra[1])),
        min(cap_x, max(base[2], extra[2])),
        max(base[3], extra[3]),
    )


def _accept_drawing(value: str) -> str:
    token = str(value or "").strip(" .-")
    if not token:
        return ""
    head = token.split()[0].upper().strip(".")
    if head in _DRAWING_REJECT or head in _FIELD_LABELS:
        return ""
    return token


def _drawn_by(upper: str) -> str:
    match = DRAWN_RE.search(upper)
    if not match:
        return ""
    return _drawn_name(match.group(1))


def _hyphen_date(value: str) -> str:
    """The printed form. OCR often reads the hyphens in 8-3-92 as dots or slashes."""

    return str(value or "").replace(".", "-").replace("/", "-")


def _date_text(value: str) -> str:
    """Join a date whose separators were split into their own tokens."""

    return re.sub(r"\s*([./])\s*", r"\1", value or "")


def _drawn_date(upper: str) -> str:
    """The date next to DATE. A revision date such as 4-14-93 is not the drawn date."""

    match = re.search(rf"DATE\s*[:.]?\s*({_DATE_BODY})", _date_text(upper))
    if match:
        return _hyphen_date(match.group(1))
    return ""


def _drawn_name(value: str) -> str:
    """A person's name under DRAWN BY. A nearby device tag is not the drafter."""

    words = []
    for word in (value or "").split():
        if word.upper() in _DRAWN_STOP:
            break
        if is_device_tag(word) or DATE_RE.fullmatch(word) or word.upper() in _FIELD_LABELS:
            break
        if not re.fullmatch(r"[A-Za-z][A-Za-z.'-]*", word):
            break
        words.append(word)
    return " ".join(words)


def _accepted_date(value: str) -> str:
    match = DATE_RE.search(_date_text(value or ""))
    return _hyphen_date(match.group(1)) if match else ""


def _first_date(text: str) -> str:
    match = DATE_RE.search(_date_text(text))
    return _hyphen_date(match.group(1)) if match else ""


def _margin_device_token(
    tok: dict[str, Any],
    width_mm: float,
    height_mm: float,
    insets: tuple[float, float, float, float] | None = None,
) -> bool:
    """A real device tag in the zone band. Not a column letter and not the title strip."""

    text = str(tok.get("text") or "")
    if not is_device_tag(text) or _grid_junk_tag(tok, width_mm, height_mm, insets):
        return False
    try:
        x = float(tok["x"])
        y = float(tok["y"])
    except (TypeError, ValueError, KeyError):
        return False
    if width_mm <= 0 or height_mm <= 0:
        return False
    if insets is None:
        left, top, right, bottom = 14.0, 16.0, 14.0, 54.0
    else:
        left, top, right, bottom = insets
    # Side borders hold the zone grid and off-page arrows. The title strip
    # is the bottom band. Only a tag that crossed the top line is rescued.
    if x < left or x > width_mm - right:
        return False
    if y >= height_mm - bottom or y < 1.0:
        return False
    return y < top


def _in_working(
    tok: dict[str, Any],
    width_mm: float,
    height_mm: float,
    insets: tuple[float, float, float, float] | None = None,
) -> bool:
    x = float(tok["x"])
    y = float(tok["y"])
    if insets is None:
        left, top, right, bottom = 14.0, 16.0, 14.0, 54.0
    else:
        left, top, right, bottom = insets
    if x < left or x > width_mm - right:
        return False
    if y < top or y > height_mm - bottom:
        return False
    return True


def _title_tokens(tokens: list[dict[str, Any]], height_mm: float) -> list[dict[str, Any]]:
    return [tok for tok in tokens if float(tok["y"]) >= height_mm - 58.0]


_FIELD_LABELS = {
    "TITLE",
    "DESCRIPTION",
    "DWG",
    "STANDARD",
    "SHEET",
    "SIZE",
    "SCALE",
    "CUSTOMER",
    "LOCATION",
    "DRAWN",
    "CHECKED",
    "APPROVED",
    "PROJECT",
    "COMPANY",
    "REV",
    "DATE",
    "LOGO",
    "NO.",
}


def _is_field_label(text: str) -> bool:
    """A title-block label. The value run stops here and the word is not a title."""

    folded = " ".join(str(text or "").split()).upper()
    if folded in _FIELD_LABELS:
        return True
    if _sheet_title_label(folded):
        return True
    if folded.endswith(" TITLE"):
        prefix = folded[: -len(" TITLE")].strip()
        if prefix in _OTHER_TITLE_PREFIXES:
            return True
    return False


def _value_under(tokens: list[dict[str, Any]], label: str) -> str:
    return _join_under(tokens, label, _zone_rows(tokens))


def _left_neighbor_word(tokens: list[dict[str, Any]], lab: dict[str, Any]) -> str:
    """The word directly left of ``lab`` on the same row."""

    lab_left, _lab_right = _token_span(lab)
    ly = float(lab["y"])
    height = _token_height(lab)
    best: tuple[float, str] | None = None
    for tok in tokens:
        if tok is lab:
            continue
        if abs(float(tok["y"]) - ly) >= 1.4:
            continue
        left, right = _token_span(tok)
        if right > lab_left + 1.0:
            continue
        if lab_left - right > 2.0 * max(height, _token_height(tok)):
            continue
        word = " ".join(str(tok.get("text") or "").split()).upper()
        if best is None or right > best[0]:
            best = (right, word)
    return best[1] if best else ""


def _label_tokens(tokens: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    found = [tok for tok in tokens if str(tok.get("text") or "").upper() == label.upper()]
    if label.upper() == "TITLE":
        # PROJECT TITLE is two words on a PDF. That TITLE is not the sheet name.
        # DRAWING / SHEET / DWG TITLE wins over a bare TITLE.
        sheet: list[dict[str, Any]] = []
        bare: list[dict[str, Any]] = []
        for tok in found:
            neighbor = _left_neighbor_word(tokens, tok)
            if neighbor in _OTHER_TITLE_PREFIXES:
                continue
            if neighbor in {"DRAWING", "SHEET", "DWG"}:
                sheet.append(tok)
            else:
                bare.append(tok)
        found = sheet or bare
    found.sort(key=lambda tok: (float(tok["x"]), float(tok["y"])))
    return found


def _zone_rows(tokens: list[dict[str, Any]]) -> set[int]:
    """A row of many one- and two-character tokens is the zone grid, not a field."""

    buckets: dict[int, int] = {}
    for tok in tokens:
        if len(tok["text"]) <= 2:
            key = round(float(tok["y"]))
            buckets[key] = buckets.get(key, 0) + 1
    return {key for key, count in buckets.items() if count >= 8}


def _join_under(tokens: list[dict[str, Any]], label: str, zone_ys: set[int], *, index: int = 0) -> str:
    lines = _lines_under(tokens, label, zone_ys=zone_ys, index=index, max_lines=1)
    return lines[0] if lines else ""


def _title_qualifier_left(tokens: list[dict[str, Any]], lab: dict[str, Any]) -> float | None:
    """Left edge of DRAWING, SHEET, or DWG on the same row, just left of TITLE.

    PDF text is one word per token, so the value under DRAWING TITLE starts
    under DRAWING, not under the word TITLE.
    """

    label = " ".join(str(lab.get("text") or "").split()).upper()
    if label != "TITLE" and not _sheet_title_label(label):
        return None
    lab_left, _lab_right = _token_span(lab)
    ly = float(lab["y"])
    height = _token_height(lab)
    qualifiers: list[tuple[float, float, dict[str, Any]]] = []
    for tok in tokens:
        if tok is lab:
            continue
        if abs(float(tok["y"]) - ly) >= 1.4:
            continue
        word = " ".join(str(tok.get("text") or "").split()).upper()
        if word not in {"DRAWING", "SHEET", "DWG"}:
            continue
        left, right = _token_span(tok)
        if right > lab_left + 1.0:
            continue
        qualifiers.append((left, right, tok))
    qualifiers.sort(key=lambda item: item[0])
    chosen: float | None = None
    cursor = lab_left
    for left, right, tok in reversed(qualifiers):
        if cursor - right > 2.0 * max(height, _token_height(tok)):
            break
        chosen = left
        cursor = left
    return chosen


def _lines_under(
    tokens: list[dict[str, Any]],
    label: str,
    *,
    zone_ys: set[int],
    index: int = 0,
    max_lines: int = 1,
    y_min: float = 1.5,
    y_max: float = 18.0,
) -> list[str]:
    labs = _label_tokens(tokens, label)
    if index >= len(labs):
        return []
    lab = labs[index]
    lx, ly = float(lab["x"]), float(lab["y"])
    x1 = _next_label_x(tokens, lab)
    x0 = lx - 6.0
    # "DRAWING TITLE" arrives as two words. The value starts under DRAWING,
    # left of the word TITLE, so the range has to begin at that qualifier.
    qualifier_left = _title_qualifier_left(tokens, lab)
    if qualifier_left is not None:
        x0 = qualifier_left
    candidates = []
    for tok in tokens:
        if tok is lab or _is_field_label(str(tok.get("text") or "")):
            continue
        if round(float(tok["y"])) in zone_ys:
            continue
        dy = float(tok["y"]) - ly
        if dy < y_min or dy > y_max:
            continue
        if float(tok["x"]) < x0 or float(tok["x"]) > x1:
            continue
        candidates.append(tok)
    if not candidates:
        return []
    candidates.sort(key=lambda tok: (float(tok["y"]), float(tok["x"])))
    grouped: list[list[dict[str, Any]]] = []
    current_y: float | None = None
    buf: list[dict[str, Any]] = []
    for tok in candidates:
        y = float(tok["y"])
        if current_y is None or abs(y - current_y) < 1.4:
            if current_y is None:
                current_y = y
            buf.append(tok)
            continue
        grouped.append(buf)
        buf = [tok]
        current_y = y
        if len(grouped) >= max_lines:
            break
    if buf and len(grouped) < max_lines:
        grouped.append(buf)
    texts = []
    for line in grouped[:max_lines]:
        line.sort(key=lambda tok: float(tok["x"]))
        text = " ".join(tok["text"] for tok in line).strip()
        # The company-box footer is "PanelCAD" plus the version. A drawn-by name is not.
        if text.upper().startswith("PANELCAD") and any(ch.isdigit() for ch in text):
            continue
        texts.append(text)
    return texts


def _next_label_x(tokens: list[dict[str, Any]], lab: dict[str, Any]) -> float:
    lx, ly = float(lab["x"]), float(lab["y"])
    best = lx + 88.0
    for tok in tokens:
        if tok is lab or not _is_field_label(str(tok.get("text") or "")):
            continue
        tx, ty = float(tok["x"]), float(tok["y"])
        if tx <= lx + 8.0:
            continue
        if ty < ly - 2.0:
            continue
        if tx < best:
            best = tx
    return best - 1.5


def _dedupe_tags(tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One reading of a tag at one place. A coil and a contact of that name both stay.

    Two glyphs of one label sit within a few line-heights. The same name printed
    on another symbol is a second occurrence, and a wire can end on either one.
    """

    kept: list[dict[str, Any]] = []
    for tok in tags:
        height = float(tok.get("h") or 2.5)
        limit = max(12.0, height * 3.5)
        match: dict[str, Any] | None = None
        for current in kept:
            if str(current.get("text") or "").upper() != str(tok.get("text") or "").upper():
                continue
            if math.hypot(float(tok["x"]) - float(current["x"]), float(tok["y"]) - float(current["y"])) <= limit:
                match = current
                break
        if match is None:
            kept.append(tok)
            continue
        if float(tok.get("h") or 0) > float(match.get("h") or 0):
            kept[kept.index(match)] = tok
    return kept
