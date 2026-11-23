"""Typed page-kind scan dispatcher separating schematic, panel, and terminal readers.

Schematic scans use bundled electrical ONNX OCR and default to ladder layout.
Panel-layout and terminal-strip scans pin Tesseract and keep preserve layout so
schematic tuning cannot regress panel or terminal-strip reading.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from PIL import Image

from panelcad.importer.preserve_mode import PRESERVE_SCAN_PAGE_KINDS

ReaderKind = Literal["schematic", "panel", "terminal"]


@dataclass(frozen=True)
class ScanPageRoute:
    """Routing decision for one raster scan page."""

    reader_kind: ReaderKind
    page_kind: str
    ocr_backend: str | None  # "tesseract" or None (keep default electrical)
    layout_mode: Literal["ladder", "preserve"]


@dataclass(frozen=True)
class ScanReadResult:
    """Typed output of dispatching one raster scan page."""

    route: ScanPageRoute
    facts: dict[str, Any]


def looks_like_strip_device(device: dict[str, Any]) -> bool:
    blob = (
        f"{device.get('symbol') or ''} {device.get('type') or ''} "
        f"{device.get('library_id') or ''}"
    ).lower()
    if "terminal" in blob:
        return True
    tag = str(device.get("tag") or "").strip().upper()
    return bool(tag.startswith("X") and "-" in tag)


def is_narrow_strip_row(blocks: list[dict[str, Any]]) -> bool:
    """True when ``blocks`` has >= 3 narrow terminal-block cells (height >= 2.2x width).

    10 mm phase-pitch lines on a schematic are ~16 mm tall (h/w ~ 1.6) and must
    not be mistaken for a terminal strip (typically h/w ~ 2.8-4.0).
    """

    if len(blocks) < 3:
        return False
    narrow = [
        b
        for b in blocks
        if float(b.get("w_mm") or 0.0) > 0.0
        and float(b.get("h_mm") or 0.0) / float(b["w_mm"]) >= 2.2
    ]
    return len(narrow) >= 3


def block_row_in_any_turn(gray: Any, rgb: Any, ppm: float) -> bool:
    """True when ``find_block_row`` hits a narrow strip row upright or after a 90° turn."""

    import cv2

    from panelcad.importer.strip_scan import find_block_row

    if is_narrow_strip_row(find_block_row(gray, rgb, ppm)):
        return True
    for flag in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE):
        if is_narrow_strip_row(find_block_row(cv2.rotate(gray, flag), cv2.rotate(rgb, flag), ppm)):
            return True
    return False


def raw_has_terminal_strip(image: Image.Image, width_mm: float, height_mm: float) -> bool:
    """True when the scan shows a row of narrow terminal blocks (no OCR)."""

    import numpy as np
    import cv2

    rgb = np.asarray(image.convert("RGB"))
    if rgb.size == 0 or width_mm <= 0:
        return False
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    ppm = rgb.shape[1] / width_mm
    if block_row_in_any_turn(gray, rgb, ppm):
        return True
    if height_mm > 0:
        alt = rgb.shape[1] / height_mm
        if abs(alt - ppm) > 0.05 and block_row_in_any_turn(gray, rgb, alt):
            return True
    return False


def peek_layout_page_kind(image: Image.Image, width_mm: float, height_mm: float) -> str:
    """Title-band page kind via Tesseract, or bundled electrical OCR when Tesseract is absent."""

    from panelcad.importer.tesseract_runtime import find_tesseract
    from panelcad.ocr.backend import ocr_scope, page_engine_ready

    has_tess = bool(find_tesseract())
    if not has_tess and not page_engine_ready():
        return ""
    import numpy as np

    from panelcad.importer.raster import _title_tokens
    from panelcad.importer.read import page_kind_from_title, read_title_block

    rgb = np.asarray(image.convert("RGB"))
    ppm = rgb.shape[1] / width_mm if width_mm else 1.0
    scope = "tesseract" if has_tess else None
    with ocr_scope(scope):
        try:
            tokens = _title_tokens(rgb, ppm, width_mm, height_mm)
        except Exception:  # noqa: BLE001 — a title miss leaves the electrical reader
            return ""
    title = read_title_block(tokens, height_mm)
    return str(page_kind_from_title(title, str(title.get("text") or "")) or "")


def route_scan_page(image: Image.Image, width_mm: float, height_mm: float) -> ScanPageRoute:
    """Classify a raw scan page into schematic, panel, or terminal reader route."""

    from panelcad.ocr.backend import active_backend

    if active_backend() != "electrical":
        return ScanPageRoute(
            reader_kind="schematic",
            page_kind="",
            ocr_backend=None,
            layout_mode="ladder",
        )
    kind = peek_layout_page_kind(image, width_mm, height_mm)
    low = str(kind or "").strip().lower()
    if low == "terminal_plan":
        return ScanPageRoute(
            reader_kind="terminal",
            page_kind=low,
            ocr_backend="tesseract",
            layout_mode="preserve",
        )
    if low in PRESERVE_SCAN_PAGE_KINDS:
        return ScanPageRoute(
            reader_kind="panel",
            page_kind=low,
            ocr_backend="tesseract",
            layout_mode="preserve",
        )
    if low.startswith("schematic"):
        return ScanPageRoute(
            reader_kind="schematic",
            page_kind=low,
            ocr_backend=None,
            layout_mode="ladder",
        )
    if raw_has_terminal_strip(image, width_mm, height_mm):
        return ScanPageRoute(
            reader_kind="terminal",
            page_kind="terminal_plan",
            ocr_backend="tesseract",
            layout_mode="preserve",
        )
    return ScanPageRoute(
        reader_kind="schematic",
        page_kind=low,
        ocr_backend=None,
        layout_mode="ladder",
    )


def read_schematic_scan(
    image: Image.Image,
    *,
    width_mm: float,
    height_mm: float,
    catalog: Any,
    standard: str = "",
    allow_missing_ocr: bool = False,
    strip_image: Image.Image | None = None,
) -> dict[str, Any]:
    """Read a schematic scan on the default electrical OCR backend."""

    from panelcad.importer.raster import read_pixels

    return read_pixels(
        image,
        width_mm,
        height_mm,
        catalog,
        standard=standard,
        allow_missing_ocr=allow_missing_ocr,
        strip_image=strip_image,
    )


def read_panel_scan(
    image: Image.Image,
    *,
    width_mm: float,
    height_mm: float,
    catalog: Any,
    standard: str = "",
    allow_missing_ocr: bool = False,
    strip_image: Image.Image | None = None,
) -> dict[str, Any]:
    """Read a panel/door layout scan pinned to Tesseract when installed, else bundled electrical OCR."""

    from panelcad.importer.raster import read_pixels
    from panelcad.importer.tesseract_runtime import find_tesseract
    from panelcad.ocr.backend import ocr_scope, page_engine_ready

    scope = "tesseract" if (find_tesseract() or not page_engine_ready()) else None
    with ocr_scope(scope):
        return read_pixels(
            image,
            width_mm,
            height_mm,
            catalog,
            standard=standard,
            allow_missing_ocr=allow_missing_ocr,
            strip_image=strip_image,
        )


def read_terminal_scan(
    image: Image.Image,
    *,
    width_mm: float,
    height_mm: float,
    catalog: Any,
    standard: str = "",
    allow_missing_ocr: bool = False,
    strip_image: Image.Image | None = None,
) -> dict[str, Any]:
    """Read a terminal-strip scan pinned to Tesseract when installed, else bundled electrical OCR."""

    from panelcad.importer.raster import read_pixels
    from panelcad.importer.tesseract_runtime import find_tesseract
    from panelcad.ocr.backend import ocr_scope, page_engine_ready

    scope = "tesseract" if (find_tesseract() or not page_engine_ready()) else None
    with ocr_scope(scope):
        return read_pixels(
            image,
            width_mm,
            height_mm,
            catalog,
            standard=standard,
            allow_missing_ocr=allow_missing_ocr,
            strip_image=strip_image,
        )


def dispatch_scan_read(
    image: Image.Image,
    *,
    width_mm: float,
    height_mm: float,
    catalog: Any,
    standard: str = "",
    allow_missing_ocr: bool = False,
    peek_image: Image.Image | None = None,
    ocr_backend: str | None = None,
    strip_image: Image.Image | None = None,
) -> ScanReadResult:
    """Route and execute the right reader (schematic / panel / terminal) for one scan."""

    if ocr_backend is None:
        route = route_scan_page(peek_image or image, width_mm, height_mm)
    elif ocr_backend == "tesseract":
        route = ScanPageRoute(
            reader_kind="panel",
            page_kind="",
            ocr_backend="tesseract",
            layout_mode="preserve",
        )
    else:
        route = ScanPageRoute(
            reader_kind="schematic",
            page_kind="",
            ocr_backend=None,
            layout_mode="ladder",
        )

    if route.reader_kind == "terminal":
        facts = read_terminal_scan(
            image,
            width_mm=width_mm,
            height_mm=height_mm,
            catalog=catalog,
            standard=standard,
            allow_missing_ocr=allow_missing_ocr,
            strip_image=strip_image,
        )
    elif route.reader_kind == "panel":
        facts = read_panel_scan(
            image,
            width_mm=width_mm,
            height_mm=height_mm,
            catalog=catalog,
            standard=standard,
            allow_missing_ocr=allow_missing_ocr,
            strip_image=strip_image,
        )
    else:
        facts = read_schematic_scan(
            image,
            width_mm=width_mm,
            height_mm=height_mm,
            catalog=catalog,
            standard=standard,
            allow_missing_ocr=allow_missing_ocr,
            strip_image=strip_image,
        )
    return ScanReadResult(route=route, facts=facts)
