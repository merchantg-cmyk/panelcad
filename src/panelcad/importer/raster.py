"""Read a scan from pixels only.

Deskew and denoise with OpenCV, read text with Tesseract, match library
symbol renders against the ink, and snap traced line ends to those pins.
This module does not open a PDF and does not read a text layer.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from panelcad.core.geometry import rotate_point
from panelcad.importer.models import CONVENTIONAL_WIRES
from panelcad.importer.read import (
    LEAD_WIRE_RE,
    TAG_RE,
    WIRE_RE,
    BUS_WIRE_RE,
    _damaged_xref_number,
    _outer_band,
    page_kind_from_title,
    effective_insets,
    in_sheet_border,
    is_cross_ref,
    is_device_tag,
    is_wire_label,
    label_tokens,
    SheetGlyph as _OcrToken,
    read_title_block,
)
from panelcad.importer.symbols import PREFIX_CONFIDENCE, shape_class, tag_prefix

# A template hit at this ink fraction is a symbol. Below it, the tag letters
# are only a prefix guess. OCR keeps this bar.
MATCH_MIN = 0.80
# A degraded scan of the right symbol often lands under the OCR bar. A vision
# tag already names the class, so that hit can still supply the pin origin
# when it beats every other class.
VISION_SNAP_MIN = 0.55
VISION_CLASS_MARGIN = 0.04
PIN_SNAP_MM = 3.4
AMBIGUOUS_MM = 1.15
# Vision boxes sit about 12 mm off. A traced end may be that far from the pin.
PIN_TRACE_MM = 14.0
INFERRED_CONFIDENCE = 0.55
# Sheet chrome: zone band plus the bottom title strip. See render.titleblock.
# A 1–2 digit terminal label lives inside the symbol, next to the pin.
TERMINAL_MARGIN_MM = 6.0
TERMINAL_PIN_MM = 8.0
ENDPOINT_LIBRARY = "iec.connection.node"
TESSERACT_MESSAGE = (
    "Scans need OCR or vision turned on. "
    "Tesseract OCR was not found. Install Tesseract and put the tesseract command on PATH. "
    "Official source: https://github.com/tesseract-ocr/tesseract "
    "Downloads: https://tesseract-ocr.github.io/tessdoc/Downloads.html"
)
TESSDATA_MESSAGE = (
    "Tesseract language data eng.traineddata was not found. "
    "Install the English traineddata next to Tesseract's tessdata directory. "
    "Official source: https://github.com/tesseract-ocr/tessdata"
)
# A label may sit a few millimetres past a short conductor. Farther than this
# is the next wire's endpoint, not this segment.
ALONG_SLACK_MM = 3.0
SIDE_PENALTY_MM = 12.0


class TesseractMissing(RuntimeError):
    """The tesseract binary is not installed. The message names the official source."""
_DIGIT = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6"})


def prepare_opencv(image: Image.Image) -> tuple[Image.Image, dict[str, float]]:
    """Denoise, then rotate so the long ruled lines are level. Angle is degrees CCW."""

    rgb = np.asarray(image.convert("RGB"))
    den = cv2.bilateralFilter(rgb, 5, 40, 40)
    gray = cv2.cvtColor(den, cv2.COLOR_RGB2GRAY)
    angle = _skew_angle(gray)
    if abs(angle) >= 0.3:
        den_img = Image.fromarray(den).rotate(-angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=(255, 255, 255))
        den = np.asarray(den_img)
    else:
        angle = 0.0
    return Image.fromarray(den), {"deskew_deg": float(angle)}


def read_pixels(
    image: Image.Image,
    width_mm: float,
    height_mm: float,
    catalog: Any,
    *,
    standard: str = "",
    allow_missing_ocr: bool = False,
) -> dict[str, Any]:
    """Devices, wires, and title-block fields from one prepared page image."""

    rgb = np.asarray(image.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    ppm = rgb.shape[1] / width_mm if width_mm else 1.0
    ocr_skipped = False
    try:
        tokens = _ocr_tokens(gray, rgb, ppm, width_mm, height_mm)
        tokens.extend(_title_tokens(rgb, ppm, width_mm, height_mm))
    except TesseractMissing:
        if not allow_missing_ocr:
            raise
        tokens = []
        ocr_skipped = True
    binary = _ink(gray)
    insets = effective_insets(detect_frame_insets(binary, width_mm, height_mm), tokens, width_mm, height_mm)
    labeled = label_tokens(tokens, width_mm, height_mm, insets=insets)
    # A one-pixel template only hits when the grid lands on the stroke. Dilate
    # the ink so a match within the stroke width still counts.
    match_ink = cv2.dilate(binary, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    devices = [
        _match_symbol(tok, match_ink, ppm, rgb.shape, catalog, standard, height_mm=height_mm)
        for tok in labeled["tags"]
    ]
    _mark_caption_tags(devices, tokens)
    # Rotation first, so the trace sees line pins on the bus side. Spread after
    # the trace so a moved origin does not leave the ink.
    orient_ladder_devices(devices, catalog, spread=False)
    wires = _trace_wires(labeled["wires"], binary, rgb, devices, catalog, ppm, width_mm, height_mm)
    orient_ladder_devices(devices, catalog, spread=True, page_width=width_mm)
    title = read_title_block(tokens, height_mm)
    if not ocr_skipped and not str(title.get("sheet_number") or "").strip() and not title.get("sheet_number_needs_info"):
        # Ruled cells miss a free-form line and a low-resolution photo. Read the
        # title-block region itself. An uncertain read stays blank.
        found = ocr_title_sheet(rgb, ppm, width_mm, height_mm)
        if found.get("sheet_number") or found.get("sheet_number_needs_info"):
            title = {**title, **found}
    title_confidence = _title_confidence(title)
    texts = _title_items(title, title_confidence)
    kind = page_kind_from_title(title, str(title.get("text") or ""))
    return {
        "devices": devices,
        "wires": wires,
        "xrefs": [],
        "texts": texts,
        "title": title,
        "kind": kind,
        "confidence": title_confidence,
        "ocr_skipped": ocr_skipped,
        "frame_insets": list(insets) if insets else None,
    }


def refine_with_model(
    image: Image.Image,
    facts: dict[str, Any],
    backend: Any,
    dest_dir: Path,
    *,
    width_mm: float = 420.0,
    height_mm: float = 297.0,
) -> dict[str, Any]:
    """Ask a configured vision model about low-confidence reads. Leaves the rest alone.

    The model is not called unless ``backend`` is the configured provider and it
    answers. Replacements are marked ``source=model``.
    """

    if backend is None or getattr(backend, "name", "") != "configured":
        return facts
    if not getattr(backend, "available", lambda: False)():
        return facts
    dest_dir.mkdir(parents=True, exist_ok=True)
    width, height = image.size
    for index, device in enumerate(facts.get("devices") or []):
        if float(device.get("confidence") or 0) >= 0.6 and not device.get("guess"):
            continue
        _refine_item(
            image,
            device,
            backend,
            dest_dir / f"device-{index}.png",
            width,
            height,
            kind="device",
            width_mm=width_mm,
            height_mm=height_mm,
        )
    for index, wire in enumerate(facts.get("wires") or []):
        if float(wire.get("confidence") or 0) >= 0.6 and not wire.get("ambiguous"):
            continue
        _refine_item(
            image,
            wire,
            backend,
            dest_dir / f"wire-{index}.png",
            width,
            height,
            kind="wire",
            width_mm=width_mm,
            height_mm=height_mm,
        )
    return facts


def _refine_item(
    image: Image.Image,
    item: dict[str, Any],
    backend: Any,
    path: Path,
    width: int,
    height: int,
    *,
    kind: str,
    width_mm: float,
    height_mm: float,
) -> None:
    x = float(item.get("x") or 0)
    y = float(item.get("y") or 0)
    page_w = width_mm if width_mm else 420.0
    page_h = height_mm if height_mm else 297.0
    px = int(x / page_w * width)
    py = int(y / page_h * height)
    box = (max(0, px - 80), max(0, py - 80), min(width, px + 160), min(height, py + 160))
    if box[2] <= box[0] or box[3] <= box[1]:
        return
    image.crop(box).save(path)
    work = {"id": f"refine-{kind}", "kind": "tile", "image": str(path), "page_mm": {"x": 0, "y": 0, "w": 40, "h": 40}}
    try:
        data = backend.extract(work)
    except Exception:  # noqa: BLE001 — a model miss leaves the raster read
        return
    if not isinstance(data, dict):
        return
    if kind == "device":
        rows = data.get("devices") or []
        if not rows or not isinstance(rows[0], dict):
            return
        row = rows[0]
        tag = str(row.get("tag") or item.get("tag") or "")
        if tag:
            item["tag"] = tag
        if row.get("library_id"):
            item["library_id"] = str(row["library_id"])
            item["guess"] = False
        if row.get("symbol") or row.get("type"):
            item["symbol"] = str(row.get("symbol") or row.get("type"))
            item["type"] = item["symbol"]
        item["source"] = "model"
        item["model_assisted"] = True
        item["confidence"] = min(0.9, max(float(item.get("confidence") or 0), float(row.get("confidence") or 0.7)))
        return
    number = ""
    rows = data.get("wires") or []
    if rows and isinstance(rows[0], dict):
        number = str(rows[0].get("number") or "")
        if rows[0].get("from_pin") and rows[0].get("to_pin"):
            item["from_pin"] = str(rows[0]["from_pin"])
            item["to_pin"] = str(rows[0]["to_pin"])
            item["from_tag"] = str(rows[0].get("from_tag") or item.get("from_tag") or "")
            item["to_tag"] = str(rows[0].get("to_tag") or item.get("to_tag") or "")
            item["ambiguous"] = False
    if number:
        item["number"] = number
    item["source"] = "model"
    item["model_assisted"] = True
    item["confidence"] = min(0.9, max(float(item.get("confidence") or 0), 0.7))


def _skew_angle(gray: np.ndarray) -> float:
    small = cv2.resize(gray, (0, 0), fx=0.4, fy=0.4, interpolation=cv2.INTER_AREA)
    bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    found = cv2.HoughLinesP(bw, 1, np.pi / 180.0, 60, minLineLength=30, maxLineGap=6)
    if found is None:
        return 0.0
    angles: list[float] = []
    for x1, y1, x2, y2 in found.reshape(-1, 4):
        angle = math.degrees(math.atan2(float(y2 - y1), float(x2 - x1)))
        if abs(angle) <= 12:
            angles.append(angle)
        elif abs(abs(angle) - 180) <= 12:
            angles.append(angle - math.copysign(180, angle))
    if len(angles) < 4:
        return 0.0
    median = float(np.median(np.asarray(angles)))
    return median if abs(median) <= 8 else 0.0


def detect_frame_insets(
    binary: np.ndarray,
    width_mm: float,
    height_mm: float,
) -> tuple[float, float, float, float] | None:
    """Drawing-frame insets ``(left, top, right, bottom)`` in millimetres.

    A long ink line is the border. The inner line, plus a pad, keeps the zone
    grid just inside the frame out of the working area. No long lines means
    the caller keeps the fixed margin.
    """

    if binary.size == 0 or width_mm < 40 or height_mm < 40:
        return None
    height_px, width_px = binary.shape[:2]
    if height_px < 20 or width_px < 20:
        return None
    ink = (binary > 0).astype(np.uint8)
    ppm_x = width_px / width_mm
    ppm_y = height_px / height_mm

    def centers(profile: np.ndarray, span_px: int, ppm: float, span_mm: float) -> list[float]:
        if span_px <= 0 or ppm <= 0:
            return []
        threshold = span_px * 0.55
        found: list[float] = []
        start: int | None = None
        flags = (profile >= threshold).tolist()
        for index, hit in enumerate(flags):
            if hit and start is None:
                start = index
            elif not hit and start is not None:
                found.append(((start + index - 1) / 2.0) / ppm)
                start = None
        if start is not None:
            found.append(((start + len(flags) - 1) / 2.0) / ppm)
        limit = min(55.0, span_mm * 0.22)
        return [value for value in found if 4.0 <= value <= limit]

    def paired(one: list[float], other: list[float]) -> float | None:
        """Distance of a rectangular frame. A line with no opposite side is a wire."""

        matches: list[float] = []
        for left in one:
            for right in other:
                if abs(left - right) <= 12.0:
                    matches.append((left + right) / 2.0)
        if not matches:
            return None
        outer = min(matches)
        inner = [value for value in matches if outer + 4.0 < value <= outer + 28.0]
        return max(inner) if inner else outer

    lefts = centers(ink.sum(axis=0), height_px, ppm_x, width_mm)
    rights = centers(ink.sum(axis=0)[::-1], height_px, ppm_x, width_mm)
    # One side of a ladder is not a frame. Both vertical sides have to agree.
    across = paired(lefts, rights)
    if across is None:
        return None
    down = paired(
        centers(ink.sum(axis=1), width_px, ppm_y, height_mm),
        centers(ink.sum(axis=1)[::-1], width_px, ppm_y, height_mm),
    )

    def pad(frame: float | None, span: float, floor: float) -> float:
        if frame is None:
            return floor
        inset = frame + 10.0
        cap = min(span * 0.22, 50.0)
        return max(floor, min(inset, cap))

    return (
        pad(across, width_mm, 14.0),
        pad(down, height_mm, 16.0),
        pad(across, width_mm, 14.0),
        pad(down, height_mm, 54.0),
    )


_CAPTION_TAG = re.compile(r"^[A-Z]{1,3}\d{2,4}$")


def _mark_caption_tags(devices: list[dict[str, Any]], tokens: list[dict[str, Any]]) -> None:
    """A guessed letter-first tag sitting in a caption is title ink, not a relay.

    Number-first power tags stay. CR055 beside prose such as a conveyor note
    does not become a device. A real symbol match (``guess`` cleared) stays.
    """

    for device in devices:
        if device.get("title_ink") or not device.get("guess"):
            continue
        tag = str(device.get("tag") or "").upper()
        if _CAPTION_TAG.fullmatch(tag) is None:
            continue
        try:
            x = float(device.get("x") or 0)
            y = float(device.get("y") or 0)
        except (TypeError, ValueError):
            continue
        prose = 0
        for tok in tokens:
            text = str(tok.get("text") or "").strip()
            if len(text) < 4 or is_device_tag(text) or is_wire_label(text) or is_cross_ref(text):
                continue
            try:
                tx = float(tok.get("x") or 0)
                ty = float(tok.get("y") or 0)
            except (TypeError, ValueError):
                continue
            if abs(ty - y) > 6 or abs(tx - x) > 90:
                continue
            prose += 1
        long = 0
        for tok in tokens:
            text = str(tok.get("text") or "").strip()
            if len(text) < 5 or is_device_tag(text) or is_wire_label(text) or is_cross_ref(text):
                continue
            try:
                tx = float(tok.get("x") or 0)
                ty = float(tok.get("y") or 0)
            except (TypeError, ValueError):
                continue
            if abs(ty - y) > 8 or abs(tx - x) > 90:
                continue
            long += 1
        if prose >= 2 or long >= 1:
            device["title_ink"] = True
            device["library_id"] = ""
            device["needs_review"] = True
            device["guess"] = True


def _ink(gray: np.ndarray) -> np.ndarray:
    from panelcad.importer.preprocess import normalize_illumination

    norm, _changed = normalize_illumination(gray)
    blur = cv2.medianBlur(norm, 3)
    return (blur < 180).astype(np.uint8)


def find_tesseract() -> str:
    """The tesseract binary on PATH or in a usual install folder. Empty when it is absent."""

    import os
    import shutil

    candidates: list[str] = []
    for key in ("TESSERACT_CMD", "PANELCAD_TESSERACT"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            candidates.append(value)
    found = shutil.which("tesseract")
    if found:
        candidates.append(found)
    candidates.extend(
        [
            "/usr/bin/tesseract",
            "/usr/local/bin/tesseract",
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return ""


def configure_tesseract() -> str:
    """Point pytesseract at the local binary. Returns the path, or empty."""

    path = find_tesseract()
    if not path:
        return ""
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = path
    return path


def _reraise_tesseract(exc: BaseException) -> None:
    name = type(exc).__name__
    if name == "TesseractNotFoundError":
        raise TesseractMissing(TESSERACT_MESSAGE) from None
    if name == "TesseractError":
        raise TesseractMissing(TESSDATA_MESSAGE) from None
    text = str(exc).lower()
    if isinstance(exc, (FileNotFoundError, OSError)) and "tesseract" in text:
        raise TesseractMissing(TESSERACT_MESSAGE) from None


def _ocr_tokens(gray: np.ndarray, rgb: np.ndarray, ppm: float, width_mm: float, height_mm: float) -> list[dict[str, Any]]:
    import pytesseract

    if not configure_tesseract():
        raise TesseractMissing(TESSERACT_MESSAGE)
    try:
        ink = _text_ink(gray, rgb)
        tokens = _sparse_tokens(_ocr_canvas(gray, rgb), ppm, height_mm, pytesseract)
        tokens.extend(_ruled_numbers(gray, ink, ppm, tokens, pytesseract))
    except TesseractMissing:
        raise
    except Exception as exc:  # noqa: BLE001 — missing binary is a clear message, other OCR errors propagate
        _reraise_tesseract(exc)
        raise
    return _dedupe_tokens(tokens, width_mm, height_mm)


def _ocr_canvas(gray: np.ndarray, rgb: np.ndarray) -> np.ndarray:
    """Grey page for Tesseract, with coloured conductor ink painted white.

    A hard black-and-white mask turns a JPEG'd tag into a broken glyph. Grey
    text stays. Blue and light-blue wires are not letters. A brown phase wire
    sits against the tag, so painting it out breaks the glyph.
    """

    canvas = gray.copy()
    red = rgb[:, :, 0].astype(np.int16)
    green = rgb[:, :, 1].astype(np.int16)
    blue = rgb[:, :, 2].astype(np.int16)
    blue_wire = (blue > 90) & (blue > red + 25) & (blue > green + 12)
    light_blue = (blue > 170) & (red > 60) & (red < 150) & (green > 120) & (blue > green)
    canvas[blue_wire | light_blue] = 255
    return canvas


def _text_ink(gray: np.ndarray, rgb: np.ndarray) -> np.ndarray:
    """Dark pixels with blue wire ink removed when the page actually uses blue wires."""

    red = rgb[:, :, 0].astype(np.int16)
    green = rgb[:, :, 1].astype(np.int16)
    blue = rgb[:, :, 2].astype(np.int16)
    colored = (blue > 90) & (blue > red + 25) & (blue > green + 12)
    if 0.00005 < float(colored.mean()) < 0.08:
        return ((gray < 200) & ~colored).astype(np.uint8) * 255
    return (gray < 200).astype(np.uint8) * 255


def _sparse_tokens(canvas: np.ndarray, ppm: float, height_mm: float, pytesseract: Any) -> list[dict[str, Any]]:
    """Tags and wire numbers that Tesseract can see among the symbols."""

    height, width = canvas.shape
    # One pass on a wide sheet misses tags. Overlapping full-height strips
    # keep a ladder column in a box Tesseract can read. Cutting off the title
    # band first makes the same pass return nothing.
    strip = max(int(200 * ppm), 1)
    step = max(int(140 * ppm), 1)
    found: list[dict[str, Any]] = []
    x = 0
    while x < width:
        piece = np.ascontiguousarray(canvas[:, x : min(width, x + strip)])
        found.extend(_tokens_in_band(piece, ppm, 0, x, pytesseract))
        if x + strip >= width:
            break
        x += step
    return [tok for tok in found if 8.0 <= tok["y"] < height_mm - 54]


def _tokens_in_band(
    band: np.ndarray,
    ppm: float,
    y0: int,
    x0: int,
    pytesseract: Any,
) -> list[dict[str, Any]]:
    up = cv2.resize(band, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    data = pytesseract.image_to_data(up, output_type=pytesseract.Output.DICT, config="--psm 11")
    found: list[dict[str, Any]] = []
    for index, raw in enumerate(data["text"]):
        try:
            conf = float(data["conf"][index])
        except (TypeError, ValueError):
            continue
        if conf < 45:
            continue
        raw_text = str(raw or "")
        text = _fix_token(raw_text)
        if not text:
            text = _reread_broken_tag(up, data, index, ppm)
        if not text:
            continue
        w_px = float(data["width"][index]) / 2.0
        h_px = float(data["height"][index]) / 2.0
        if h_px / ppm > 8 or w_px / ppm > 28:
            continue
        x_px = x0 + float(data["left"][index]) / 2.0 + w_px / 2.0
        y_px = y0 + float(data["top"][index]) / 2.0 + h_px / 2.0
        found.append(
            {
                "text": text,
                "x": round(x_px / ppm, 2),
                "y": round(y_px / ppm, 2),
                "w": round(max(w_px, 1) / ppm, 2),
                "h": round(max(h_px, 1) / ppm, 2),
                "confidence": 0.8 if conf >= 70 else 0.62,
                "source": "ocr",
            }
        )
    return found


def _merge_vertical(comps: list[tuple[int, int, int, int]], ppm: float) -> list[tuple[int, int, int, int]]:
    """Stack blobs that sit on top of each other with a small gap, such as a digit cut by a line."""

    comps = sorted(comps, key=lambda item: (item[0], item[1]))
    used = [False] * len(comps)
    groups: list[tuple[int, int, int, int]] = []
    for index, (x, y, w, h) in enumerate(comps):
        if used[index]:
            continue
        x0, y0, x1, y1 = x, y, x + w, y + h
        used[index] = True
        changed = True
        while changed:
            changed = False
            for other, (ox, oy, ow, oh) in enumerate(comps):
                if used[other]:
                    continue
                if min(x1, ox + ow) - max(x0, ox) < 0.45 * min(x1 - x0, ow):
                    continue
                gap = max(y0, oy) - min(y1, oy + oh)
                if -2 <= gap <= 0.9 * ppm:
                    x0, y0 = min(x0, ox), min(y0, oy)
                    x1, y1 = max(x1, ox + ow), max(y1, oy + oh)
                    used[other] = True
                    changed = True
        groups.append((x0, y0, x1 - x0, y1 - y0))
    return groups


def _merge_letters(comps: list[tuple[int, int, int, int]], ppm: float) -> list[tuple[int, int, int, int]]:
    comps = sorted(comps, key=lambda item: (item[1], item[0]))
    used = [False] * len(comps)
    groups: list[tuple[int, int, int, int]] = []
    for index, (x, y, w, h) in enumerate(comps):
        if used[index]:
            continue
        x0, y0, x1, y1 = x, y, x + w, y + h
        used[index] = True
        changed = True
        while changed:
            changed = False
            for other, (ox, oy, ow, oh) in enumerate(comps):
                if used[other]:
                    continue
                if min(y1, oy + oh) - max(y0, oy) < 0.4 * min(y1 - y0, oh):
                    continue
                if -3 <= ox - x1 <= 0.9 * ppm:
                    x0, y0 = min(x0, ox), min(y0, oy)
                    x1, y1 = max(x1, ox + ow), max(y1, oy + oh)
                    used[other] = True
                    changed = True
        groups.append((x0, y0, x1 - x0, y1 - y0))
    return groups


def _reread_broken_tag(image: np.ndarray, data: dict[str, Any], index: int, ppm: float) -> str:
    """Read a tag whose letter Tesseract turned into a symbol or dropped.

    The word box already holds K1 when the letter was read as a mark. A lone
    digit is the right half of the tag, so the second read looks one letter left.
    """

    raw = str(data["text"][index] or "")
    cleaned = re.sub(r"[^A-Za-z0-9]", "", raw)
    if not any(ch.isdigit() for ch in raw):
        return ""
    height_mm = float(data["height"][index]) / 2.0 / ppm
    if not 1.4 <= height_mm <= 7.0:
        return ""
    box = (
        int(data["left"][index]),
        int(data["top"][index]),
        int(data["width"][index]),
        int(data["height"][index]),
    )
    fixed = _fix_token(_ocr_word(image, box, expand_left_mm=0.0, psm=7, ppm=ppm))
    if TAG_RE.match(fixed):
        return fixed
    if cleaned.isdigit() and len(cleaned) == 1:
        fixed = _fix_token(_ocr_word(image, box, expand_left_mm=2.6, psm=8, ppm=ppm))
        if TAG_RE.match(fixed):
            return fixed
    return ""


def _ocr_word(
    image: np.ndarray,
    box: tuple[int, int, int, int],
    *,
    expand_left_mm: float,
    psm: int,
    ppm: float,
) -> str:
    import pytesseract

    configure_tesseract()

    left, top, width, height = box
    pad = int(expand_left_mm * ppm * 2.0)
    x0 = max(0, left - pad - 2)
    y0 = max(0, top - 4)
    x1 = min(image.shape[1], left + width + 4)
    y1 = min(image.shape[0], top + height + 4)
    if x1 <= x0 or y1 <= y0:
        return ""
    crop = image[y0:y1, x0:x1]
    bigger = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    bigger = cv2.copyMakeBorder(bigger, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=255)
    config = f"--psm {psm} -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return str(pytesseract.image_to_string(bigger, config=config) or "").strip()


def _fix_token(raw: str) -> str:
    """Turn an OCR token into a tag or wire number. A letter read as a digit stays a letter.

    Bracketed cross-references are not wire numbers. A hyphenated device tag
    keeps the suffix. Digit-confusion (O06) still becomes a wire.
    """

    if is_cross_ref(str(raw or "")):
        return ""
    hyphenated = re.sub(r"[^A-Za-z0-9\-]", "", raw or "").upper()
    if is_device_tag(hyphenated) or LEAD_WIRE_RE.match(hyphenated) or BUS_WIRE_RE.match(hyphenated):
        return hyphenated
    text = re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()
    if not text:
        return ""
    if is_device_tag(text) or LEAD_WIRE_RE.match(text) or BUS_WIRE_RE.match(text) or text in CONVENTIONAL_WIRES:
        return text
    options: list[str] = []
    if all(char.isdigit() or char in "OILSBZG" for char in text):
        number = text.translate(_DIGIT)
        if number.isdigit() and 2 <= len(number) <= 4:
            options.append(number)
    for split in range(1, min(5, len(text))):
        prefix, tail = text[:split], text[split:]
        if not prefix.isalpha():
            continue
        digits = tail.translate(_DIGIT)
        if digits.isdigit() and 1 <= len(digits) <= 4:
            options.append(prefix + digits)
    tagged = [item for item in options if TAG_RE.match(item) or item in {"L1", "L2", "L3", "N", "PE"}]
    numbered = [item for item in options if WIRE_RE.match(item)]
    # O06 is a misread wire, not a device. L1 and Q1 keep the letter.
    if tagged:
        kept = [item for item in tagged if item[0] not in "OISBZG" or not numbered]
        if kept:
            return kept[0]
    if numbered:
        return numbered[0]
    if text in {"L1", "L2", "L3", "N", "PE"} or WIRE_RE.match(text) or is_device_tag(text):
        return text
    if LEAD_WIRE_RE.match(text) or BUS_WIRE_RE.match(text):
        return text
    return ""


def _dedupe_tokens(tokens: list[dict[str, Any]], width_mm: float, height_mm: float) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for tok in tokens:
        if not (0 <= float(tok["x"]) <= width_mm and 0 <= float(tok["y"]) <= height_mm):
            continue
        if any(other["text"] == tok["text"] and abs(other["x"] - tok["x"]) < 2.5 and abs(other["y"] - tok["y"]) < 2.5 for other in kept):
            continue
        kept.append(tok)
    return kept


def _ruled_numbers(
    gray: np.ndarray,
    ink: np.ndarray,
    ppm: float,
    existing: list[dict[str, Any]],
    pytesseract: Any,
) -> list[dict[str, Any]]:
    """Wire numbers drawn on a ruled line. The line is removed and the digits are closed back up."""

    kernel = max(7, int(10 * ppm))
    lines = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (kernel, 1)))
    pieces = cv2.subtract(ink, lines)
    count, _labels, stats, _cent = cv2.connectedComponentsWithStats(pieces, 8)
    raw: list[tuple[int, int, int, int]] = []
    for index in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[index])
        if h < 2 or w < 2:
            continue
        wmm, hmm = w / ppm, h / ppm
        if 0.3 <= wmm <= 3.6 and 0.4 <= hmm <= 3.6 and area >= 4:
            raw.append((x, y, w, h))
    # A ruled line cuts each digit in half. Stack the halves before reading them.
    comps = [box for box in _merge_vertical(raw, ppm) if 1.0 <= box[3] / ppm <= 3.8]
    found: list[dict[str, Any]] = []
    for x, y, w, h in _merge_letters(comps, ppm):
        cx, cy = (x + w / 2.0) / ppm, (y + h / 2.0) / ppm
        page_h = gray.shape[0] / ppm if ppm else 0.0
        if cy < 14 or cy > page_h - 60:
            continue
        if any(abs(tok["x"] - cx) < 3.0 and abs(tok["y"] - cy) < 2.2 for tok in existing):
            continue
        if w / ppm > 22:
            continue
        text = _read_closed_digits(gray, lines, x, y, w, h, ppm, pytesseract)
        text = _fix_token(text)
        if not text or not WIRE_RE.match(text):
            continue
        found.append(
            {
                "text": text,
                "x": round(cx, 2),
                "y": round(cy, 2),
                "w": round(w / ppm, 2),
                "h": round(h / ppm, 2),
                "confidence": 0.66,
                "source": "ocr",
            }
        )
    return found


def _read_closed_digits(
    gray: np.ndarray,
    lines: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
    ppm: float,
    pytesseract: Any,
) -> str:
    height, width = gray.shape
    pad = max(1, int(0.25 * ppm))
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(width, x + w + pad), min(height, y + h + pad)
    ink = gray[y0:y1, x0:x1] < 190
    row_line = lines[y0:y1, x0:x1].mean(axis=1) > 0.3
    keep = [index for index in range(ink.shape[0]) if not row_line[index]]
    if len(keep) < 4:
        return ""
    ink = ink[keep]
    cols = np.where(ink.any(axis=0))[0]
    rows = np.where(ink.any(axis=1))[0]
    if len(cols) == 0 or len(rows) == 0:
        return ""
    ink = ink[rows.min() : rows.max() + 1, cols.min() : cols.max() + 1]
    up = cv2.resize((~ink).astype(np.uint8) * 255, None, fx=6, fy=6, interpolation=cv2.INTER_CUBIC)
    _thr, bw = cv2.threshold(up, 160, 255, cv2.THRESH_BINARY)
    bw = cv2.copyMakeBorder(bw, 18, 18, 18, 18, cv2.BORDER_CONSTANT, value=255)
    return pytesseract.image_to_string(bw, config="--psm 7 -c tessedit_char_whitelist=0123456789").strip()


def ocr_title_sheet(
    rgb: np.ndarray,
    ppm: float,
    _width_mm: float,
    _height_mm: float,
) -> dict[str, Any]:
    """Sheet number from the title-block region of a scan or a photo.

    A photographed sheet is the bright page on whatever is behind it. That page
    is straightened before the crops, so a few degrees of leftover tilt do not
    defeat the line reader. The crop then follows the ink on the paper, so a
    margin around the sheet does not push the block out of the band. Bottom,
    right, left, and top edges are separate crops. The first explicit number
    wins. Two different numbers, or a number read below the confidence floor,
    come back as needs-info.
    """

    from panelcad.importer.read import _empty_sheet_fields, sheet_fields_from_text

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY) if rgb.ndim == 3 else rgb
    sheet = _isolate_sheet(gray)
    box = _content_bounds(sheet)
    if box is None:
        return _empty_sheet_fields()
    for crop in _title_region_crops(sheet, box):
        found = _sheet_from_crop(crop, ppm, sheet_fields_from_text)
        if found.get("sheet_number") or found.get("sheet_number_needs_info"):
            return found
    for crop in _edge_title_crops(sheet, box):
        found = _sheet_from_crop(crop, ppm, sheet_fields_from_text)
        if found.get("sheet_number") or found.get("sheet_number_needs_info"):
            return found
    return _empty_sheet_fields()


def _sheet_from_crop(crop: np.ndarray, ppm: float, parse: Any) -> dict[str, Any]:
    """Read one title crop. A bare ``N OF M`` has to sit on the OF line."""

    from panelcad.importer.read import _empty_sheet_fields

    if crop.size == 0:
        return _empty_sheet_fields()
    found = _sheet_from_prepared(_prepare_title_pieces(crop, upscale=1.0), parse)
    if found.get("sheet_number") or found.get("sheet_number_needs_info") or ppm >= 8.0:
        return found
    scale = min(2.0, max(1.0, 8.0 / ppm))
    if scale < 1.15:
        return found
    larger = _sheet_from_prepared(_prepare_title_pieces(crop, upscale=scale), parse)
    if larger.get("sheet_number") or larger.get("sheet_number_needs_info"):
        return larger
    return found


def _sheet_from_prepared(pieces: list[np.ndarray], parse: Any) -> dict[str, Any]:
    """Each tile is its own line. A digit in one tile cannot complete OF in another."""

    from panelcad.importer.read import _empty_sheet_fields

    if not pieces:
        return _empty_sheet_fields()
    for psm in (6, 11):
        results: list[dict[str, Any]] = []
        hold = False
        for image in pieces:
            words = _tesseract_words(image, psm)
            if _title_crop_may_hold_sheet(words):
                hold = True
            results.append(_sheet_from_piece(words, parse))
        merged = _merge_sheet_reads(results)
        if merged.get("sheet_number") or merged.get("sheet_number_needs_info"):
            return merged
        if psm == 6 and not hold:
            break
    return _empty_sheet_fields()


def _word_supports_sheet(word: str, target: str) -> bool:
    """True when this OCR token is the sheet id, including a glued label or OF."""

    from panelcad.importer.read import glyph_supports_sheet

    return glyph_supports_sheet(word, target)


def _title_crop_may_hold_sheet(words: list[_OcrToken]) -> bool:
    """Sparse mode is for a second title line. Ordinary ``of`` is not a sheet mark."""

    if not words:
        return True
    blob = " ".join(word.text for word in words).upper()
    if re.search(r"SHEET|PAGE|\bSHT?\b|\bPG\b|\bDWG\b", blob):
        return True
    return bool(re.search(r"\b\d{1,3}\s+OF\s+\d{1,3}\b|\d\s*/\s*\d", blob))


def _sheet_from_piece(words: list[_OcrToken], parse: Any) -> dict[str, Any]:
    """One tile. The box rule is the same one the bottom-band tokens use."""

    from panelcad.importer.read import sheet_from_glyphs

    return sheet_from_glyphs(words, in_title_block=True, labeled_values=None, parse=parse)


def _merge_sheet_reads(results: list[dict[str, Any]]) -> dict[str, Any]:
    """One number across tiles is stored. Two numbers, or only a bare count, need info."""

    from panelcad.importer.read import _empty_sheet_fields

    numbered = [item for item in results if item.get("sheet_number")]
    numbers = {str(item["sheet_number"]) for item in numbered}
    if len(numbers) > 1:
        return {**_empty_sheet_fields(), "sheet_number_needs_info": True}
    if len(numbers) == 1:
        with_count = [item for item in numbered if item.get("sheet_count")]
        return dict(with_count[0] if with_count else numbered[0])
    needy = [item for item in results if item.get("sheet_number_needs_info")]
    if not needy:
        return _empty_sheet_fields()
    counts = {str(item.get("sheet_count") or "") for item in needy if item.get("sheet_count")}
    count = next(iter(counts)) if len(counts) == 1 else ""
    return {**_empty_sheet_fields(), "sheet_count": count, "sheet_number_needs_info": True}



def _tesseract_words(image: np.ndarray, psm: int) -> list[_OcrToken]:
    import pytesseract

    if not configure_tesseract():
        raise TesseractMissing(TESSERACT_MESSAGE)
    try:
        data = pytesseract.image_to_data(
            image, output_type=pytesseract.Output.DICT, config=f"--psm {psm}"
        )
    except TesseractMissing:
        raise
    except Exception as exc:  # noqa: BLE001 — a missing binary is TesseractMissing
        _reraise_tesseract(exc)
        raise
    words: list[_OcrToken] = []
    texts = data.get("text") or []
    confs = data.get("conf") or []
    lefts = data.get("left") or []
    tops = data.get("top") or []
    widths = data.get("width") or []
    heights = data.get("height") or []
    for raw, conf, left, top, width, height in zip(texts, confs, lefts, tops, widths, heights):
        text = str(raw or "").strip()
        try:
            score = float(conf)
            box = (int(left), int(top), int(width), int(height))
        except (TypeError, ValueError):
            continue
        if not text or score < 0 or box[2] <= 0 or box[3] <= 0:
            continue
        words.append(_OcrToken(text, score / 100.0, box[0], box[1], box[2], box[3]))
    return words


# Tesseract reads a line more reliably once the long side is a few thousand
# pixels. A wider photo is cut into overlapping pieces so the enlargement
# still happens; skipping it left phone-sized crops at the original scale.
_TITLE_UPSCALE_LIMIT = 1800


def _prepare_title_pieces(crop: np.ndarray, *, upscale: float) -> list[np.ndarray]:
    """Contrast-stretch, enlarge, then binarize. A wide crop becomes several pieces."""

    if crop.size == 0:
        return []
    gray = _stretch_title_crop(crop)
    pieces = _upscale_title_pieces(gray, upscale)
    return [_finish_title_piece(piece) for piece in pieces if piece.size]


def _stretch_title_crop(crop: np.ndarray) -> np.ndarray:
    gray = crop
    low = float(np.percentile(gray, 2))
    high = float(np.percentile(gray, 98))
    if high > low + 20:
        stretched = (gray.astype(np.float32) - low) * (255.0 / (high - low))
        gray = np.clip(stretched, 0, 255).astype(np.uint8)
    return gray


def _upscale_title_pieces(gray: np.ndarray, upscale: float) -> list[np.ndarray]:
    """Enlarge a low-resolution crop. Tile a photo-width crop so each piece still grows."""

    if gray.size == 0 or upscale < 1.15:
        return [gray]
    height, width = gray.shape[:2]
    if max(height, width) * upscale <= _TITLE_UPSCALE_LIMIT:
        return [cv2.resize(gray, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)]
    tile = max(48, int(_TITLE_UPSCALE_LIMIT / upscale))
    overlap = max(12, tile // 5)
    step = max(1, tile - overlap)
    pieces: list[np.ndarray] = []
    if width >= height:
        x = 0
        while x < width:
            piece = gray[:, x : min(width, x + tile)]
            if piece.shape[1] >= 16:
                pieces.append(
                    cv2.resize(piece, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
                )
            if x + tile >= width:
                break
            x += step
    else:
        y = 0
        while y < height:
            piece = gray[y : min(height, y + tile), :]
            if piece.shape[0] >= 16:
                pieces.append(
                    cv2.resize(piece, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
                )
            if y + tile >= height:
                break
            y += step
    return pieces or [gray]


def _finish_title_piece(gray: np.ndarray) -> np.ndarray:
    short = min(gray.shape[:2]) if gray.size else 0
    if short >= 15 and float(np.median(gray)) < 210:
        block = 31 if short >= 32 else 15
        if block % 2 == 0:
            block += 1
        if block < short:
            gray = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, 8
            )
    return cv2.copyMakeBorder(gray, 18, 18, 18, 18, cv2.BORDER_CONSTANT, value=255)


def _isolate_sheet(gray: np.ndarray) -> np.ndarray:
    """Straighten the largest bright page. A scan that already fills the frame stays put.

    Phone photos sit on a bench or a carpet. Counting every dark pixel as ink
    then makes the box the whole picture, and the title crops land on the
    background. The page is the bright quadrilateral. Ink is read inside it.
    """

    quad = _paper_quad(gray)
    if quad is None:
        return gray
    warped = _warp_sheet(gray, quad)
    if warped is gray or warped.size == 0 or min(warped.shape[:2]) < 32:
        return gray
    # A bad quad turns the page into a dark wedge. Keep the original then.
    if float(np.mean(warped)) < 140.0:
        return gray
    return warped


def _paper_quad(gray: np.ndarray) -> np.ndarray | None:
    """Four corners of the page, in full-image pixels. None when the page is the frame."""

    height, width = gray.shape[:2]
    if width < 32 or height < 32:
        return None
    long_side = max(height, width)
    scale = 1.0
    work = gray
    if long_side > 960:
        scale = 960.0 / float(long_side)
        work = cv2.resize(
            gray,
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    blur_k = max(3, min(15, int(round(min(work.shape[:2]) * 0.012)) | 1))
    blurred = cv2.GaussianBlur(work, (blur_k, blur_k), 0)
    _threshold, bright = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    fraction = float(np.count_nonzero(bright)) / float(bright.size)
    # Almost no paper, or a scan that is already the whole frame. A page can
    # still cover most of a photo and leave a dark surround.
    if fraction < 0.12 or fraction > 0.985:
        return None
    close_k = max(5, min(11, int(round(min(work.shape[:2]) * 0.012)) | 1))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (close_k, close_k))
    closed = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, kernel)
    closed = cv2.dilate(closed, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    contours, _hierarchy = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    area = float(cv2.contourArea(contour))
    image_area = float(work.shape[0] * work.shape[1])
    # Closing text holes fills a scan. A real surround stays outside the page.
    if area < 0.18 * image_area or area > 0.975 * image_area:
        return None
    rect = cv2.minAreaRect(contour)
    rect_w, rect_h = rect[1]
    rect_area = float(rect_w) * float(rect_h)
    if rect_area <= 1.0 or area / rect_area < 0.72:
        return None
    short_side = min(float(rect_w), float(rect_h))
    long_rect = max(float(rect_w), float(rect_h))
    if short_side < 16.0 or long_rect / short_side > 6.0:
        return None
    quad = cv2.boxPoints(rect).astype(np.float32)
    if scale != 1.0:
        quad = quad / scale
    return quad


def _warp_sheet(gray: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """Perspective-correct ``quad`` onto an upright rectangle."""

    ordered = _order_quad(quad)
    if not _quad_is_sheet(ordered, gray.shape):
        return gray
    top_left, top_right, bottom_right, bottom_left = ordered
    across_top = np.linalg.norm(top_right - top_left)
    across_bottom = np.linalg.norm(bottom_right - bottom_left)
    down_left = np.linalg.norm(bottom_left - top_left)
    down_right = np.linalg.norm(bottom_right - top_right)
    width = int(round(max(across_top, across_bottom)))
    height = int(round(max(down_left, down_right)))
    if width < 32 or height < 32:
        return gray
    if width > gray.shape[1] * 2 or height > gray.shape[0] * 2:
        return gray
    destination = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(ordered, destination)
    return cv2.warpPerspective(
        gray,
        matrix,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )


def _order_quad(points: np.ndarray) -> np.ndarray:
    """Clockwise from the top-left image corner: top-left, top-right, bottom-right, bottom-left."""

    pts = points.reshape(4, 2).astype(np.float32)
    sums = pts.sum(axis=1)
    diffs = np.diff(pts, axis=1).reshape(-1)
    ordered = np.array(
        [
            pts[int(np.argmin(sums))],
            pts[int(np.argmin(diffs))],
            pts[int(np.argmax(sums))],
            pts[int(np.argmax(diffs))],
        ],
        dtype=np.float32,
    )
    return ordered


def _quad_is_sheet(ordered: np.ndarray, shape: tuple[int, ...]) -> bool:
    """Four distinct corners with some area, sitting on the picture."""

    rounded = {(round(float(point[0]), 1), round(float(point[1]), 1)) for point in ordered}
    if len(rounded) < 4:
        return False
    area = 0.0
    for index in range(4):
        x1, y1 = ordered[index]
        x2, y2 = ordered[(index + 1) % 4]
        area += float(x1) * float(y2) - float(x2) * float(y1)
    if abs(area) < 400.0:
        return False
    height, width = shape[:2]
    for x, y in ordered:
        if float(x) < -width * 0.15 or float(x) > width * 1.15:
            return False
        if float(y) < -height * 0.15 or float(y) > height * 1.15:
            return False
    return True


def _content_bounds(gray: np.ndarray) -> tuple[int, int, int, int] | None:
    """Ink box on the isolated sheet, ignoring a white margin and single specks."""

    if gray.size == 0:
        return None
    height, width = gray.shape[:2]
    if width < 16 or height < 16:
        return None
    ink = gray < 185
    # A single title line on a large scan is only a few dark pixels wide.
    # The minimum stays small so that line is inside the box; a speck is not a row of text.
    row_min = max(3, min(10, int(width * 0.002)))
    col_min = max(3, min(8, int(height * 0.0015)))
    rows = np.where(ink.sum(axis=1) >= row_min)[0]
    cols = np.where(ink.sum(axis=0) >= col_min)[0]
    if len(rows) == 0 or len(cols) == 0:
        return None
    pad = 4
    x0 = max(0, int(cols[0]) - pad)
    y0 = max(0, int(rows[0]) - pad)
    x1 = min(width, int(cols[-1]) + 1 + pad)
    y1 = min(height, int(rows[-1]) + 1 + pad)
    if x1 - x0 < 16 or y1 - y0 < 16:
        return None
    return x0, y0, x1, y1


def _title_region_crops(gray: np.ndarray, box: tuple[int, int, int, int]) -> list[np.ndarray]:
    """Bottom strip of a full sheet, or the whole ink box when that box is already the block."""

    x0, y0, x1, y1 = box
    content_h = y1 - y0
    content_w = x1 - x0
    image_h = gray.shape[0]
    # A photographed margin is already gone. Cropping a tight title block again
    # slices the sheet number off the top of the block.
    if content_h < image_h * 0.5:
        band = gray[y0:y1, x0:x1]
        crops = [band]
    else:
        band_h = max(28, int(content_h * 0.30))
        band_h = min(content_h, band_h)
        top = max(y0, y1 - band_h)
        band = gray[top:y1, x0:x1]
        crops = [band]
        corner_w = max(int(content_w * 0.48), 32)
        if content_w - corner_w > 24:
            crops.append(gray[top:y1, max(x0, x1 - corner_w) : x1])
    return [crop for crop in crops if crop.size]


def _edge_title_crops(gray: np.ndarray, box: tuple[int, int, int, int]) -> list[np.ndarray]:
    """Right, then left, then top. Side titles are tried both ways up.

    A landscape sheet held in portrait puts the block on the left edge. The
    bottom band is already searched. A blank edge is not read.
    """

    x0, y0, x1, y1 = box
    content_w = x1 - x0
    content_h = y1 - y0
    if content_h < 16 or content_w < 16:
        return []
    if content_w < content_h * 0.28:
        return _inked(_both_ways(gray[y0:y1, x0:x1]))
    side_w = min(content_w // 3, max(int(content_w * 0.22), 24))
    crops: list[np.ndarray] = []
    if side_w >= 12:
        crops.extend(_both_ways(gray[y0:y1, x1 - side_w : x1]))
        crops.extend(_both_ways(gray[y0:y1, x0 : x0 + side_w]))
    band_h = min(content_h // 3, max(int(content_h * 0.30), 28))
    if band_h >= 12 and content_h - band_h > 16:
        top = gray[y0 : y0 + band_h, x0:x1]
        crops.append(top)
        crops.append(cv2.rotate(top, cv2.ROTATE_180))
    return _inked(crops)


def _both_ways(strip: np.ndarray) -> list[np.ndarray]:
    if strip.size == 0:
        return []
    return [
        cv2.rotate(strip, cv2.ROTATE_90_CLOCKWISE),
        cv2.rotate(strip, cv2.ROTATE_90_COUNTERCLOCKWISE),
    ]


def _inked(crops: list[np.ndarray]) -> list[np.ndarray]:
    return [crop for crop in crops if _crop_has_ink(crop)]


def _crop_has_ink(crop: np.ndarray) -> bool:
    """A title line has a run of dark pixels. A blank margin does not."""

    if crop.size == 0 or min(crop.shape[:2]) < 8:
        return False
    ink = crop < 170
    return int(ink.sum()) >= 40 and float(ink.mean()) >= 0.004


def _title_tokens(rgb: np.ndarray, ppm: float, width_mm: float, height_mm: float) -> list[dict[str, Any]]:
    """Title-block words. A missing Tesseract binary becomes TesseractMissing."""

    try:
        return _read_title_cells(rgb, ppm, width_mm, height_mm)
    except TesseractMissing:
        raise
    except Exception as exc:  # noqa: BLE001
        _reraise_tesseract(exc)
        raise


def _read_title_cells(rgb: np.ndarray, ppm: float, width_mm: float, height_mm: float) -> list[dict[str, Any]]:
    """Columns and rows are the ruled cells, read one cell-row at a time."""

    import pytesseract

    configure_tesseract()
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    top = max(0, height - int(68 * ppm))
    band = gray[top:]
    if band.size == 0:
        return []
    ink = (band < 180).astype(np.uint8) * 255
    vkernel = max(5, int(8 * ppm))
    hkernel = max(5, int(6 * ppm))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, vkernel)))
    xs = np.where(vertical.sum(axis=0) > 0.35 * band.shape[0])[0]
    cuts = [0]
    for x in xs:
        if int(x) - cuts[-1] > int(8 * ppm):
            cuts.append(int(x))
    cuts.append(width)
    found: list[dict[str, Any]] = []
    for left, right in zip(cuts, cuts[1:]):
        if right - left < int(12 * ppm):
            continue
        column = band[:, left + 2 : right - 2]
        col_ink = ink[:, left + 2 : right - 2]
        horizontal = cv2.morphologyEx(col_ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (hkernel, 1)))
        rows = np.where(horizontal.any(axis=1))[0]
        row_cuts = [0]
        for y in rows:
            if int(y) - row_cuts[-1] > int(2 * ppm):
                row_cuts.append(int(y))
        row_cuts.append(column.shape[0])
        for top_row, bot_row in zip(row_cuts, row_cuts[1:]):
            if bot_row - top_row < int(2.4 * ppm):
                continue
            cell = column[top_row + 1 : bot_row - 1]
            if cell.size == 0 or cell.shape[0] < 6:
                continue
            found.extend(_cell_words(cell, pytesseract, ppm, origin_x=left + 2, origin_y=top + top_row + 1))
    return [tok for tok in found if 0 <= tok["x"] <= width_mm + 2 and 0 <= tok["y"] <= height_mm + 2]


def _cell_words(cell: np.ndarray, pytesseract: Any, ppm: float, *, origin_x: int, origin_y: int) -> list[dict[str, Any]]:
    up = cv2.resize(cell, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    up = cv2.copyMakeBorder(up, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
    data = pytesseract.image_to_data(up, output_type=pytesseract.Output.DICT, config="--psm 6")
    found: list[dict[str, Any]] = []
    for index, raw in enumerate(data["text"]):
        text = str(raw or "").strip()
        try:
            conf = float(data["conf"][index])
        except (TypeError, ValueError):
            continue
        if not text or conf < 55 or text in {"|", "—"}:
            continue
        x_px = origin_x + (float(data["left"][index]) - 12 + float(data["width"][index]) / 2.0) / 3.0
        y_px = origin_y + (float(data["top"][index]) - 12 + float(data["height"][index]) / 2.0) / 3.0
        found.append(
            {
                "text": text,
                "x": round(x_px / ppm, 2),
                "y": round(y_px / ppm, 2),
                "w": round(float(data["width"][index]) / 3.0 / ppm, 2),
                "h": round(max(float(data["height"][index]) / 3.0, 1) / ppm, 2),
                "confidence": min(0.9, conf / 100.0),
                "source": "ocr",
            }
        )
    return found


def _match_symbol(
    token: dict[str, Any],
    binary: np.ndarray,
    ppm: float,
    shape: tuple[int, ...],
    catalog: Any,
    standard: str,
    *,
    height_mm: float = 0.0,
) -> dict[str, Any]:
    tag = str(token["text"])
    candidates = _candidates(catalog, tag_prefix(tag), standard, tag)
    left = float(token["x"]) - float(token.get("w") or 0) / 2.0
    base_y = float(token["y"])
    best: tuple[float, Any, float, float] | None = None
    scores: list[tuple[float, Any]] = []
    for device in candidates[:12]:
        score, ox, oy = _search_template(binary, device, ppm, shape, left, base_y)
        scores.append((score, device))
        if best is None or score > best[0]:
            best = (score, device, ox, oy)
    row = {
        "tag": tag,
        "type": "",
        "symbol": "",
        "pins": [],
        "x": round(left, 2),
        "y": round(base_y + 2.5, 2),
        "confidence": min(float(token.get("confidence") or 0.7), PREFIX_CONFIDENCE),
        "unreadable": False,
        "guess": True,
        "library_id": "",
        "pin_frame": "",
        "scale": 1.0,
        "source": "ocr",
    }
    title_floor = height_mm - 58.0 if height_mm else 0.0
    in_title = title_floor > 0 and (float(token["y"]) >= title_floor or (best is not None and best[3] >= title_floor))
    if in_title:
        # Title-block ink is not a relay. Leave the tag for review and do not place it.
        row["library_id"] = ""
        row["needs_review"] = True
        row["title_ink"] = True
        return row
    if best is None or best[0] < MATCH_MIN:
        # A missed template is not a prefix guess. M6 must not become aux_nc.
        row["library_id"] = ""
        row["needs_review"] = True
        row["unconfirmed"] = True
        row["review_note"] = "the reader did not confirm a symbol"
        return row
    score, device, ox, oy = best
    same = [item for item in scores if shape_class(item[1]) == shape_class(device)]
    other = [item[0] for item in scores if shape_class(item[1]) != shape_class(device)]
    margin = score - (max(other) if other else 0.0)
    if margin < 0.05 and other:
        confidence = 0.55
        guess = True
    else:
        confidence = round(min(0.93, 0.66 + 0.27 * min(1.0, (score - MATCH_MIN) / 0.28)), 2)
        guess = False
    row.update(
        {
            "type": shape_class(device),
            "symbol": shape_class(device),
            "library_id": device.library_id,
            "x": round(ox, 2),
            "y": round(oy, 2),
            "confidence": confidence,
            "guess": guess,
            "pin_frame": "schematic",
            "source": "symbol",
        }
    )
    return row


def _candidates(catalog: Any, prefix: str, standard: str, tag: str = "") -> list[Any]:
    from panelcad.library.catalog import normalize_standard

    if not prefix:
        return []
    std = normalize_standard(standard, default="") if standard else ""
    found = []
    for device in catalog.devices.values():
        if device.ref_prefix.upper() != prefix:
            continue
        lib = device.library_id.lower()
        if "offpage" in lib or "notes" in lib:
            continue
        found.append(device)
    upper = str(tag or "").upper()
    if re.fullmatch(r"\d+M(?:-[A-Z0-9]+)?", upper):
        contactors = [
            device
            for device in found
            if "contactor" in device.library_id.lower()
            and len(device.pins) >= 3
            and "aux" not in device.library_id.lower()
            and "coil" not in device.library_id.lower()
        ]
        if contactors:
            found = contactors
    elif re.fullmatch(r"\d+G", upper):
        found = [
            device
            for device in catalog.devices.values()
            if shape_class(device) == "motor" and "1ph" not in device.library_id.lower() and len(device.pins) >= 3
        ]
    elif re.fullmatch(r"\d+OL(?:-[A-Z0-9]+)?", upper):
        overloads = [
            device
            for device in found
            if shape_class(device) == "overload" and "ol_contact" not in device.library_id.lower() and len(device.pins) >= 3
        ]
        if overloads:
            found = overloads
    if std == "NEMA/ANSI":
        found = [device for device in found if str(device.library_id).startswith("nema.")]
    found.sort(key=lambda device: (0 if not std or device.standard in {std, ""} else 1, len(device.pins), device.library_id))
    return found


def _search_template(
    binary: np.ndarray,
    device: Any,
    ppm: float,
    shape: tuple[int, ...],
    left: float,
    base_y: float,
    *,
    x_span: tuple[float, float] = (-8.0, 14.0),
    y_span: tuple[float, float] = (-6.0, 18.0),
    cover_origin: bool = False,
) -> tuple[float, float, float]:
    tmpl = _render_template(device, ppm)
    if tmpl is None:
        return 0.0, left, base_y
    th, tw = tmpl.shape
    page_h, page_w = binary.shape[:2]
    total = int((tmpl > 0).sum())
    if total < 8 or page_h < th or page_w < tw:
        return 0.0, left, base_y
    # A 1.5 mm lattice steps over the peak. One pixel shift changes the fraction
    # from a sure hit to a miss, so the window is scored at every pixel.
    response = cv2.matchTemplate(binary, tmpl, cv2.TM_CCORR)
    x0 = max(0, int(round((left + x_span[0]) * ppm)))
    x1 = min(response.shape[1], int(round((left + x_span[1]) * ppm)))
    y0 = max(0, int(round((base_y + y_span[0]) * ppm)))
    y1 = min(response.shape[0], int(round((base_y + y_span[1]) * ppm)))
    if cover_origin:
        ox = int(round(left * ppm))
        oy = int(round(base_y * ppm))
        x0, x1 = min(x0, max(0, ox)), max(x1, min(response.shape[1], ox + 1))
        y0, y1 = min(y0, max(0, oy)), max(y1, min(response.shape[0], oy + 1))
    if x1 <= x0 or y1 <= y0:
        return 0.0, left, base_y
    window = response[y0:y1, x0:x1]
    index = int(np.argmax(window))
    iy, ix = divmod(index, window.shape[1])
    px = x0 + ix
    py = y0 + iy
    return float(window[iy, ix]) / total, px / ppm, py / ppm


def _render_template(device: Any, ppm: float) -> np.ndarray | None:
    width = max(4, int(float(device.schematic_width) * ppm) + 2)
    height = max(4, int(float(device.schematic_height) * ppm) + 2)
    if width > 800 or height > 800:
        return None
    image = np.zeros((height, width), np.uint8)

    def pt(x: float, y: float) -> tuple[int, int]:
        return int(round(x * ppm)), int(round(y * ppm))

    for prim in device.primitives:
        kind = getattr(prim, "type", "")
        if kind == "line":
            cv2.line(image, pt(prim.x, prim.y), pt(prim.x2, prim.y2), 1, 1)
        elif kind == "rect":
            x, y = pt(prim.x, prim.y)
            cv2.rectangle(image, (x, y), (int(round((prim.x + prim.w) * ppm)), int(round((prim.y + prim.h) * ppm))), 1, 1)
        elif kind == "circle":
            cv2.circle(image, pt(prim.x, prim.y), max(1, int(round(prim.r * ppm))), 1, 1)
        elif kind == "polyline" and prim.points:
            pts = np.array([pt(x, y) for x, y in prim.points], np.int32)
            cv2.polylines(image, [pts], False, 1, 1)
    if int(image.sum()) < 8:
        return None
    return image


def _trace_wires(
    numbers: list[dict[str, Any]],
    binary: np.ndarray,
    rgb: np.ndarray,
    devices: list[dict[str, Any]],
    catalog: Any,
    ppm: float,
    width_mm: float,
    height_mm: float,
) -> list[dict[str, Any]]:
    pins = _pin_points(devices, catalog)
    mask = _wire_mask(binary, rgb, devices, catalog, ppm)
    segments = _line_segments(mask, ppm, width_mm, height_mm)
    chains = _chains(segments)
    dots = _junction_dots(binary, ppm)
    labeled: list[dict[str, Any]] = []
    used_numbers: set[int] = set()
    # A conductor is the ink between two pins. Line segments still feed the
    # chain fallback when the pins are further apart than that direct run.
    for pin_a, pin_b, point_a, point_b in _pin_links(pins, mask, ppm):
        number, nindex = _nearest_number([point_a, point_b], numbers, used_numbers)
        if number is None:
            continue
        used_numbers.add(nindex)
        labeled.append(_wire_item(number, pin_a, pin_b, False))
    for chain in chains:
        ends = _snap_ends(chain, pins, dots)
        number, nindex = _nearest_number(chain, numbers, used_numbers)
        if number is None or ends is None:
            continue
        used_numbers.add(nindex)
        a, b, ambiguous = ends
        labeled.append(_wire_item(number, a, b, ambiguous))
    for index, number in enumerate(numbers):
        if index in used_numbers:
            continue
        labeled.append(
            {
                "number": str(number["text"]),
                "x": number["x"],
                "y": number["y"],
                "from_tag": "",
                "to_tag": "",
                "from_pin": "",
                "to_pin": "",
                "confidence": 0.4,
                "unreadable": False,
                "ambiguous": True,
                "source": "ocr",
            }
        )
    return collapse_wire_numbers(labeled)


def collapse_wire_numbers(wires: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per wire number and pin pair. A bus keeps each pair. A stub loses to a pin."""

    kept: list[dict[str, Any] | None] = []
    index_of: dict[tuple[str, tuple[tuple[str, str], tuple[str, str]]], int] = {}
    loose_of: dict[str, int] = {}
    pinned_numbers: set[str] = set()
    for wire in wires:
        number = str(wire.get("number") or "")
        if not number:
            kept.append(wire)
            continue
        # One printed number can feed several pins (a bus). Different pin pairs stay.
        # An unpinned stub of that number does not.
        pair = _pinned_pair(wire)
        if pair is None:
            if number in pinned_numbers:
                continue
            prior = loose_of.get(number)
            if prior is None:
                loose_of[number] = len(kept)
                kept.append(wire)
            elif _wire_rank(wire) > _wire_rank(kept[prior] or wire):
                kept[prior] = wire
            continue
        pinned_numbers.add(number)
        loose = loose_of.pop(number, None)
        if loose is not None:
            kept[loose] = None
        prior = index_of.get((number, pair))
        if prior is None:
            index_of[(number, pair)] = len(kept)
            kept.append(wire)
            continue
        if _wire_rank(wire) > _wire_rank(kept[prior] or wire):
            kept[prior] = wire
    return [wire for wire in kept if wire is not None]


def _pinned_pair(wire: dict[str, Any]) -> tuple[tuple[str, str], tuple[str, str]] | None:
    if not (wire.get("from_pin") and wire.get("to_pin")) or wire.get("ambiguous"):
        return None
    left = (str(wire.get("from_tag") or ""), str(wire.get("from_pin") or ""))
    right = (str(wire.get("to_tag") or ""), str(wire.get("to_pin") or ""))
    if not left[0] or not right[0]:
        return None
    return tuple(sorted((left, right)))


def _wire_rank(wire: dict[str, Any]) -> tuple[int, int, float]:
    pinned = bool(wire.get("from_pin") and wire.get("to_pin") and not wire.get("ambiguous"))
    attached = bool(wire.get("attached")) and not wire.get("ambiguous")
    return (1 if pinned else 0, 1 if attached else 0, float(wire.get("confidence") or 0))


def snap_model_wires(
    wires: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    image: Image.Image,
    width_mm: float,
    height_mm: float,
    catalog: Any,
) -> list[dict[str, Any]]:
    """Attach model wire numbers to ink runs between the detected symbol pins."""

    pinned = [wire for wire in wires if wire.get("from_pin") and wire.get("to_pin") and not wire.get("ambiguous")]
    loose = [wire for wire in wires if wire not in pinned]
    if not loose or not devices:
        return wires
    rgb = np.asarray(image.convert("RGB"))
    ppm = rgb.shape[1] / width_mm if width_mm else 1.0
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    used_pins = set()
    for wire in pinned:
        used_pins.add((str(wire.get("from_tag") or ""), str(wire.get("from_pin") or "")))
        used_pins.add((str(wire.get("to_tag") or ""), str(wire.get("to_pin") or "")))
    pins = [pin for pin in _pin_points(devices, catalog, include_guess=True) if (pin[0], pin[1]) not in used_pins]
    if len(pins) < 2:
        return wires
    mask = _wire_mask(binary=_ink(gray), rgb=rgb, devices=devices, catalog=catalog, ppm=ppm)
    numbers = [
        {
            "text": str(wire.get("number") or ""),
            "x": float(wire.get("x") or 0),
            "y": float(wire.get("y") or 0),
            "confidence": wire.get("confidence") or 0.7,
            "wire": wire,
        }
        for wire in loose
        if str(wire.get("number") or "")
    ]
    used: set[int] = set()
    attached: list[dict[str, Any]] = []
    for pin_a, pin_b, point_a, point_b in _pin_links(pins, mask, ppm):
        number, nindex = _nearest_number([point_a, point_b], numbers, used)
        if number is None:
            continue
        used.add(nindex)
        wire = dict(number["wire"])
        wire.update(
            {
                "from_tag": pin_a[0],
                "from_pin": pin_a[1],
                "to_tag": pin_b[0],
                "to_pin": pin_b[1],
                "ambiguous": False,
                "x": number["x"],
                "y": number["y"],
            }
        )
        attached.append(wire)
    for index, number in enumerate(numbers):
        if index in used:
            continue
        attached.append(dict(number["wire"]))
    return pinned + attached


# Measured vision boxes sit about 12 mm off the ink. 14 mm still reaches that segment.
WIRE_ATTACH_MM = 14.0
SEAM_MM = 12.0
# A copied pixel pin must sit on the same conductor the vision number snapped to.
# The next phase column is about 10 mm away, so 6 mm still rejects that column.
_PIN_ON_SEGMENT_MM = 6.0
_PIN_ALONG_SEGMENT_MM = 36.0


def attach_vision_wires(
    wires: list[dict[str, Any]],
    image: Image.Image,
    width_mm: float,
    height_mm: float,
    devices: list[dict[str, Any]],
    catalog: Any,
    pixel_wires: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Snap a vision wire number onto the nearest pixel wire segment.

    No segment inside ``WIRE_ATTACH_MM`` leaves a labelled stub at the same
    point. The stub is not ambiguous. It stays in the review list.
    """

    segments = _pixel_segments(image, width_mm, height_mm, devices, catalog, pixel_wires or [])
    out: list[dict[str, Any]] = []
    for wire in wires:
        if not _vision_wire(wire):
            out.append(wire)
            continue
        item = dict(wire)
        if item.get("from_pin") and item.get("to_pin") and not item.get("ambiguous"):
            item["attached"] = True
            item["stub"] = False
            item["ambiguous"] = False
            out.append(item)
            continue
        point = (float(item.get("x") or 0), float(item.get("y") or 0))
        best = _best_segment(point, segments, WIRE_ATTACH_MM)
        if best is None:
            item["stub"] = True
            item["attached"] = False
            item["ambiguous"] = False
            item["needs_review"] = True
            item["from_pin"] = ""
            item["to_pin"] = ""
            item["from_tag"] = ""
            item["to_tag"] = ""
            out.append(item)
            continue
        item["attached"] = True
        item["stub"] = False
        item["ambiguous"] = False
        item["segment"] = [
            [round(best[0][0], 2), round(best[0][1], 2)],
            [round(best[1][0], 2), round(best[1][1], 2)],
        ]
        pin_points = _pin_points(devices, catalog, include_guess=True) if catalog is not None else None
        _copy_pixel_pins(item, best, pixel_wires or [], pin_points)
        if not (item.get("from_pin") and item.get("to_pin")):
            item["needs_review"] = True
        out.append(item)
    return out


def align_vision_devices(
    devices: list[dict[str, Any]],
    pixel_devices: list[dict[str, Any]],
    image: Image.Image,
    width_mm: float,
    height_mm: float,
    catalog: Any,
    *,
    standard: str = "",
) -> list[dict[str, Any]]:
    """Prefer a pixel template hit for each vision tag, then search the ink nearby.

    The pixel match carries the library symbol and the pin origin. A vision box
    about 12 mm off is only the search window when no pixel device has that tag.
    """

    by_tag = {
        str(device.get("tag") or ""): device
        for device in pixel_devices
        if device.get("tag") and device.get("library_id") and not device.get("guess")
    }
    rgb = np.asarray(image.convert("RGB"))
    if width_mm <= 0 or rgb.size == 0 or catalog is None:
        return devices
    ppm = rgb.shape[1] / width_mm
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    binary = cv2.dilate(_ink(gray), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    aligned: list[dict[str, Any]] = []
    for device in devices:
        item = dict(device)
        tag = str(item.get("tag") or "")
        pixel = by_tag.get(tag)
        if pixel is not None:
            item["library_id"] = pixel.get("library_id") or item.get("library_id")
            item["x"] = pixel.get("x")
            item["y"] = pixel.get("y")
            item["guess"] = False
            item["symbol"] = pixel.get("symbol") or item.get("symbol") or ""
            item["type"] = pixel.get("type") or item.get("type") or item.get("symbol") or ""
            aligned.append(item)
            continue
        snapped = _snap_tag_to_ink(item, binary, ppm, catalog, standard)
        if snapped is not None:
            item.update(snapped)
        aligned.append(item)
    return aligned


def _power_symbols(candidates: list[Any], wanted: str) -> list[Any]:
    """A 2-pin aux contact is not the contactor, overload, or motor the vision class named."""

    token = wanted.strip().lower().replace(" ", "_").replace("-", "_")
    if token not in {"contactor", "overload", "motor"}:
        return []
    return [
        part
        for part in candidates
        if len(part.pins) >= 3
        and "aux" not in part.library_id.lower()
        and "ol_contact" not in part.library_id.lower()
        and "1ph" not in part.library_id.lower()
        and _symbol_class_matches(part, token)
    ]


def _snap_tag_to_ink(
    device: dict[str, Any],
    binary: np.ndarray,
    ppm: float,
    catalog: Any,
    standard: str,
) -> dict[str, Any] | None:
    tag = str(device.get("tag") or "")
    candidates = _candidates(catalog, tag_prefix(tag), standard, tag)
    if not candidates or device.get("x") is None:
        return None
    left = float(device["x"])
    base_y = float(device["y"])
    wanted = str(device.get("symbol") or device.get("type") or "")
    candidates = _power_symbols(candidates, wanted) or candidates
    # Fraction alone prefers a small aux contact sitting on part of a contactor.
    # Among class matches that clear the bar, keep the symbol that explains more ink.
    same: list[tuple[float, float, Any, float, float]] = []
    strong: tuple[float, float, Any, float, float] | None = None
    best_other = 0.0
    ink_count: dict[str, int] = {}
    # The measured vision box is about 12 mm off, so the window is wider than OCR's.
    for part in candidates:
        score, ox, oy = _search_template(
            binary,
            part,
            ppm,
            binary.shape,
            left,
            base_y,
            x_span=(-16.0, 16.0),
            y_span=(-16.0, 16.0),
            cover_origin=True,
        )
        total = ink_count.get(part.library_id)
        if total is None:
            tmpl = _render_template(part, ppm)
            total = 0 if tmpl is None else int((tmpl > 0).sum())
            ink_count[part.library_id] = total
        matched = score * float(total)
        row = (matched, score, part, ox, oy)
        if score >= MATCH_MIN and (strong is None or matched > strong[0]):
            strong = row
        if _symbol_class_matches(part, wanted):
            if score >= VISION_SNAP_MIN:
                same.append(row)
        else:
            best_other = max(best_other, score)
    chosen = _choose_snap(same, strong, best_other, bool(wanted.strip()))
    if chosen is None:
        return None
    _matched, score, part, ox, oy = chosen
    # Vision already named the class. A high-fraction aux contact on a rectangle
    # is not that symbol, and it would drop the power pins the wires name.
    if wanted.strip() and not _symbol_class_matches(part, wanted):
        return None
    return {
        "library_id": part.library_id,
        "x": round(ox, 2),
        "y": round(oy, 2),
        "guess": False,
        "symbol": shape_class(part),
        "type": shape_class(part) or str(device.get("type") or ""),
        "confidence": round(min(float(device.get("confidence") or 0.7), 0.9 if score >= MATCH_MIN else 0.55), 2),
    }


def _choose_snap(
    same: list[tuple[float, float, Any, float, float]],
    strong: tuple[float, float, Any, float, float] | None,
    best_other: float,
    wanted: bool,
) -> tuple[float, float, Any, float, float] | None:
    """A class match that beats other classes wins. Otherwise only an OCR-grade hit."""

    if wanted and same:
        best_same = max(same, key=lambda row: row[0])
        if best_same[1] >= best_other + VISION_CLASS_MARGIN:
            return best_same
    return strong


def _symbol_class_matches(part: Any, wanted: str) -> bool:
    token = wanted.strip().lower().replace(" ", "_").replace("-", "_")
    if not token:
        return False
    got = shape_class(part)
    if token in {"vfd", "drive"}:
        return got == "drive"
    if token in {"psu", "power_supply"}:
        return got == "power_supply"
    if token in {"estop", "e_stop"}:
        return got == "estop"
    return got == token


def reject_junk_wire_numbers(
    wires: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    catalog: Any,
    width_mm: float,
    height_mm: float,
    insets: tuple[float, float, float, float] | None = None,
) -> list[dict[str, Any]]:
    """Drop border letters, title-block text, and digits sitting on a terminal label."""

    kept: list[dict[str, Any]] = []
    for wire in wires:
        if wire_number_reject_reason(wire, devices, catalog, width_mm, height_mm, insets):
            continue
        kept.append(wire)
    return kept


def wire_number_reject_reason(
    wire: dict[str, Any],
    devices: list[dict[str, Any]],
    catalog: Any,
    width_mm: float,
    height_mm: float,
    insets: tuple[float, float, float, float] | None = None,
) -> str:
    """The validation rule that drops this wire number, or an empty string when it stays.

    Pixel wires are not judged here. Vision wires lose cross-references, single
    letters, tag-shaped tokens, border digits, and terminal labels.
    """

    if not _vision_wire(wire):
        return ""
    number = str(wire.get("number") or "").strip()
    if is_cross_ref(number):
        return "XREF_RE"
    if _single_letter_wire(number):
        return "LETTER"
    if _tag_shaped_wire(number, devices):
        return "TAG_RE"
    try:
        x = float(wire.get("x"))
        y = float(wire.get("y"))
    except (TypeError, ValueError):
        return ""
    # Row digits and a phase name with no pins live in the border.
    # A lead or bus number does not. A conductor that already names both pins can
    # be labelled in the margin.
    pinned = bool(wire.get("from_pin") and wire.get("to_pin") and wire.get("from_tag") and wire.get("to_tag"))
    token = number.strip().upper()
    if _in_border(x, y, width_mm, height_mm, insets) and not pinned:
        if re.fullmatch(r"\d{1,4}", token) or token in CONVENTIONAL_WIRES:
            return "BORDER"
    if _damaged_xref_number(number) and _outer_band(x, y, width_mm, height_mm):
        return "XREF_RE"
    if _on_terminal_label(number, x, y, devices, catalog):
        return "TERMINAL_LABEL"
    return ""


def _tag_shaped_wire(number: str, devices: list[dict[str, Any]]) -> bool:
    """A device tag, or any tag-shaped token, is not a wire number.

    L1 matches the tag pattern and stays, because it is a conventional wire.
    """

    token = number.strip().upper()
    if not token or token in CONVENTIONAL_WIRES:
        return False
    tags = {str(device.get("tag") or "").strip().upper() for device in devices if device.get("tag")}
    if token in tags:
        return True
    return is_device_tag(token)


def _single_letter_wire(number: str) -> bool:
    token = number.strip().upper()
    if len(token) != 1 or not token.isalpha():
        return False
    return token not in CONVENTIONAL_WIRES


def _in_border(
    x: float,
    y: float,
    width_mm: float,
    height_mm: float,
    insets: tuple[float, float, float, float] | None = None,
) -> bool:
    return in_sheet_border(x, y, width_mm, height_mm, insets)


def _on_terminal_label(
    number: str,
    x: float,
    y: float,
    devices: list[dict[str, Any]],
    catalog: Any,
) -> bool:
    """1–2 digit text inside a symbol, next to a numbered terminal."""

    if not re.fullmatch(r"\d{1,2}", number.strip()):
        return False
    if catalog is None:
        return False
    for device in devices:
        box = _symbol_box(device, catalog)
        if box is None or not _in_expanded(x, y, box, TERMINAL_MARGIN_MM):
            continue
        for _tag, pin, px, py in _pin_points([device], catalog, include_guess=True):
            if not str(pin).isdigit():
                continue
            if math.hypot(px - x, py - y) <= TERMINAL_PIN_MM:
                return True
    return False


def _symbol_box(device: dict[str, Any], catalog: Any) -> tuple[float, float, float, float] | None:
    lib = str(device.get("library_id") or "")
    try:
        ox = float(device.get("x"))
        oy = float(device.get("y"))
    except (TypeError, ValueError):
        return None
    width = float(device.get("w") or 0)
    height = float(device.get("h") or 0)
    if lib and catalog is not None:
        try:
            part = catalog.get(lib)
        except KeyError:
            part = None
        if part is not None:
            width = float(part.schematic_width) or width
            height = float(part.schematic_height) or height
    if width <= 0 or height <= 0:
        return None
    return ox, oy, width, height


def _in_expanded(x: float, y: float, box: tuple[float, float, float, float], margin: float) -> bool:
    ox, oy, width, height = box
    return (ox - margin) <= x <= (ox + width + margin) and (oy - margin) <= y <= (oy + height + margin)


def trace_vision_wires(
    wires: list[dict[str, Any]],
    image: Image.Image,
    width_mm: float,
    height_mm: float,
    devices: list[dict[str, Any]],
    catalog: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Follow a snapped number along the ink to the pins at each end.

    Both ends become a real wire, pending review, low confidence because the
    ends were inferred. One end stays a review item. It does not become a
    placeholder device on the sheet.
    """

    if catalog is None or width_mm <= 0:
        return wires, []
    rgb = np.asarray(image.convert("RGB"))
    if rgb.size == 0:
        return wires, []
    ppm = rgb.shape[1] / width_mm
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    mask = _wire_mask(_ink(gray), rgb, devices, catalog, ppm)
    # A prefix guess is not a pin map. Aligned symbols have guess cleared.
    pins = _pin_points(devices, catalog, include_guess=False)
    electrical = _electrical_map(devices, catalog)
    links = _pin_links(pins, mask, ppm, sides=_side_map(devices, catalog))
    used_pins: set[tuple[str, str]] = set()
    for wire in wires:
        if wire.get("from_pin") and wire.get("to_pin") and not wire.get("ambiguous"):
            used_pins.add((str(wire.get("from_tag") or ""), str(wire.get("from_pin") or "")))
            used_pins.add((str(wire.get("to_tag") or ""), str(wire.get("to_pin") or "")))
    claims: list[tuple[float, int, int]] = []
    for index, wire in enumerate(wires):
        if not _vision_wire(wire) or wire.get("rejected") or wire.get("unlocalised"):
            continue
        if wire.get("from_pin") and wire.get("to_pin") and not wire.get("ambiguous"):
            continue
        point = (float(wire.get("x") or 0), float(wire.get("y") or 0))
        for link_index, link in enumerate(links):
            if (link[0] in used_pins) or (link[1] in used_pins):
                continue
            score = _convention_score(point[0], point[1], link[2], link[3], PIN_TRACE_MM)
            if score is None:
                continue
            claims.append((_power_rank(score, link[0], link[1], electrical), index, link_index))
    claims.sort()
    taken_links: set[int] = set()
    taken_wires: set[int] = set()
    out = [dict(wire) for wire in wires]
    for _score, index, link_index in claims:
        if index in taken_wires or link_index in taken_links:
            continue
        link = links[link_index]
        if link[0] in used_pins or link[1] in used_pins:
            continue
        _assign_traced(out[index], link[0], link[1], inferred=True)
        used_pins.add(link[0])
        used_pins.add(link[1])
        taken_links.add(link_index)
        taken_wires.add(index)
    segments = _line_segments(mask, ppm, width_mm, height_mm)
    dots = _junction_dots(mask, ppm)
    for index, wire in enumerate(out):
        if index in taken_wires or not _vision_wire(wire) or wire.get("unlocalised"):
            continue
        if wire.get("from_pin") and wire.get("to_pin"):
            continue
        segment = wire.get("segment")
        chosen: tuple[tuple[float, float], tuple[float, float]] | None = None
        if segment and len(segment) == 2:
            chosen = (
                (float(segment[0][0]), float(segment[0][1])),
                (float(segment[1][0]), float(segment[1][1])),
            )
        else:
            point = (float(wire.get("x") or 0), float(wire.get("y") or 0))
            chosen = _best_segment(point, segments, PIN_TRACE_MM)
        if chosen is None:
            continue
        start, end = _extend_colinear(chosen[0], chosen[1], segments, dots)
        forward = _unit(start, end)
        backward = (-forward[0], -forward[1])
        found_start = _snap_pin(start, pins, used_pins, PIN_TRACE_MM, backward, electrical)
        found_end = _snap_pin(end, pins, used_pins, PIN_TRACE_MM, forward, electrical)
        if found_start is not None and found_end is not None and found_start[:2] != found_end[:2]:
            _assign_traced(wire, found_start[:2], found_end[:2], inferred=True)
            used_pins.add(found_start[:2])
            used_pins.add(found_end[:2])
    return out, []


def _assign_traced(
    wire: dict[str, Any],
    start: tuple[str, str],
    end: tuple[str, str],
    *,
    inferred: bool,
) -> None:
    wire["from_tag"], wire["from_pin"] = start
    wire["to_tag"], wire["to_pin"] = end
    wire["ambiguous"] = False
    wire["stub"] = False
    wire["attached"] = True
    wire["needs_review"] = True
    wire["inferred"] = inferred
    if inferred:
        wire["confidence"] = round(min(float(wire.get("confidence") or INFERRED_CONFIDENCE), INFERRED_CONFIDENCE), 2)


def _nearest_segment(
    point: tuple[float, float],
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
    limit: float,
) -> list[list[float]] | None:
    best = None
    best_d = limit
    for start, end in segments:
        distance = _segment_distance(point[0], point[1], start, end)
        if distance <= best_d:
            best_d = distance
            best = (start, end)
    if best is None:
        return None
    return [[best[0][0], best[0][1]], [best[1][0], best[1][1]]]


def _nearest_free_pin(
    point: tuple[float, float],
    pins: list[tuple[str, str, float, float]],
    used: set[tuple[str, str]],
    limit: float,
) -> tuple[str, str, float, float] | None:
    best = None
    best_d = limit
    for pin in pins:
        key = (pin[0], pin[1])
        if key in used:
            continue
        distance = math.hypot(pin[2] - point[0], pin[3] - point[1])
        if distance <= best_d:
            best_d = distance
            best = pin
    return best


def _endpoint_tag(number: str, used: set[str]) -> str:
    body = "".join(ch for ch in number.upper() if ch.isalnum()) or "X"
    tag = "WN" + body[:10]
    candidate = tag
    suffix = 2
    while candidate in used:
        candidate = f"{tag}{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _endpoint_device(tag: str, point: tuple[float, float], wire: dict[str, Any]) -> dict[str, Any]:
    # Pin A sits at (0, 5) inside the node, so the node origin puts A on the free end.
    return {
        "tag": tag,
        "library_id": ENDPOINT_LIBRARY,
        "guess": False,
        "x": round(point[0], 2),
        "y": round(point[1] - 5.0, 2),
        "confidence": INFERRED_CONFIDENCE,
        "needs_review": True,
        "endpoint": True,
        "unreadable": False,
        "type": "node",
        "symbol": "node",
        "source": "model",
        "model_assisted": True,
        "label": str(wire.get("number") or ""),
    }


def stitch_wire_seams(wires: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union endpoints when the same number sits on both sides of a tile seam."""

    used: set[int] = set()
    out: list[dict[str, Any]] = []
    for index, wire in enumerate(wires):
        if index in used:
            continue
        current = dict(wire)
        number = str(current.get("number") or "")
        if number:
            for other_index in range(index + 1, len(wires)):
                if other_index in used:
                    continue
                other = wires[other_index]
                if str(other.get("number") or "") != number:
                    continue
                if not _share_seam(current, other):
                    continue
                # A bus repeats one number on several finished pin pairs. Only a
                # conductor split by the tile edge, missing an end, is one wire.
                pair = _pinned_pair(current)
                other_pair = _pinned_pair(other)
                if pair is not None and other_pair is not None and pair != other_pair:
                    continue
                current = _union_wire_ends(current, other)
                used.add(other_index)
        out.append(current)
    return out


def _vision_wire(wire: dict[str, Any]) -> bool:
    return bool(wire.get("model_assisted") or str(wire.get("source") or "") == "model")


def _pixel_segments(
    image: Image.Image,
    width_mm: float,
    height_mm: float,
    devices: list[dict[str, Any]],
    catalog: Any,
    pixel_wires: list[dict[str, Any]],
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    rgb = np.asarray(image.convert("RGB"))
    if width_mm <= 0 or rgb.size == 0:
        return []
    ppm = rgb.shape[1] / width_mm
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    mask = _wire_mask(_ink(gray), rgb, devices, catalog, ppm)
    segments = _line_segments(mask, ppm, width_mm, height_mm)
    if catalog is None:
        return segments
    pins = _pin_points(devices, catalog, include_guess=True)
    by_pin = {(tag, pin): (x, y) for tag, pin, x, y in pins}
    for wire in pixel_wires:
        start = by_pin.get((str(wire.get("from_tag") or ""), str(wire.get("from_pin") or "")))
        end = by_pin.get((str(wire.get("to_tag") or ""), str(wire.get("to_pin") or "")))
        if start and end:
            segments.append((start, end))
    return segments


def _segment_distance(
    px: float,
    py: float,
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 1e-9:
        return math.hypot(px - ax, py - ay)
    tee = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + tee * dx), py - (ay + tee * dy))


def _best_segment(
    point: tuple[float, float],
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
    limit: float,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """The segment the label sits on, scored by perpendicular distance.

    A projection more than a few millimetres past the segment is rejected.
    The drawing puts a vertical number to the right of its wire and a
    horizontal number above its wire, so the other side ranks worse.
    """

    best = None
    best_score: float | None = None
    for start, end in segments:
        score = _convention_score(point[0], point[1], start, end, limit)
        if score is None:
            continue
        if best_score is None or score < best_score:
            best_score = score
            best = (start, end)
    return best


def _convention_score(
    px: float,
    py: float,
    start: tuple[float, float],
    end: tuple[float, float],
    limit: float,
) -> float | None:
    perp, along = _perp_along(px, py, start, end)
    if along > ALONG_SLACK_MM or perp > limit:
        return None
    return perp + _side_penalty(px, py, start, end)


def _perp_along(
    px: float,
    py: float,
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[float, float]:
    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 1e-9:
        return math.hypot(px - ax, py - ay), 0.0
    length = math.sqrt(length2)
    tee = ((px - ax) * dx + (py - ay) * dy) / length2
    qx = ax + tee * dx
    qy = ay + tee * dy
    perp = math.hypot(px - qx, py - qy)
    if tee < 0.0:
        along = -tee * length
    elif tee > 1.0:
        along = (tee - 1.0) * length
    else:
        along = 0.0
    return perp, along


def _side_penalty(px: float, py: float, start: tuple[float, float], end: tuple[float, float]) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if abs(dy) >= abs(dx):
        # Vertical wire: the number sits to the right, so the wire is on or left.
        if (start[0] + end[0]) / 2.0 > px + 0.4:
            return SIDE_PENALTY_MM
        return 0.0
    # Horizontal wire: the number sits above, so a segment above the label ranks worse.
    # Page y grows downward.
    if (start[1] + end[1]) / 2.0 < py - 0.4:
        return SIDE_PENALTY_MM
    return 0.0


def _power_rank(
    score: float,
    start: tuple[str, str],
    end: tuple[str, str],
    electrical: dict[tuple[str, str], str],
) -> float:
    """Prefer a main-circuit link when the label could also see an aux contact."""

    kinds = {electrical.get(start, ""), electrical.get(end, "")}
    if kinds == {"power"}:
        return score - 6.0
    if "power" not in kinds:
        return score + 6.0
    return score


def _side_map(devices: list[dict[str, Any]], catalog: Any) -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    if catalog is None:
        return found
    for device in devices:
        lib = str(device.get("library_id") or "")
        tag = str(device.get("tag") or "")
        if not lib or not tag or device.get("guess"):
            continue
        try:
            part = catalog.get(lib)
        except KeyError:
            continue
        rotation = int(device.get("rotation") or 0)
        sides = ("left", "top", "right", "bottom")
        order = {name: index for index, name in enumerate(sides)}
        for pin in part.pins:
            raw = str(pin.side or "")
            if raw in order and rotation % 360 in {0, 90, 180, 270}:
                raw = sides[(order[raw] + (rotation // 90)) % 4]
            found[(tag, str(pin.id))] = raw
    return found


_OPPOSITE_SIDES = {("top", "bottom"), ("bottom", "top"), ("left", "right"), ("right", "left")}


def _facing_pair(
    first: tuple[str, str, float, float],
    second: tuple[str, str, float, float],
    sides: dict[tuple[str, str], str],
) -> bool:
    """Power-path pins on two symbols that face each other across a short gap."""

    if not sides or first[0] == second[0]:
        return False
    pair = (sides.get((first[0], first[1]), ""), sides.get((second[0], second[1]), ""))
    if pair not in _OPPOSITE_SIDES:
        return False
    dx = abs(first[2] - second[2])
    dy = abs(first[3] - second[3])
    # A 3-pole motor lead can sit about 6 mm off the overload column.
    # The next phase is about 10 mm away, which _cross_phase still rejects.
    if pair[0] in {"top", "bottom"}:
        return dx <= 7.0 and dy <= 10.0
    return dy <= 7.0 and dx <= 10.0


def _cross_phase(
    first: tuple[str, str, float, float],
    second: tuple[str, str, float, float],
    sides: dict[tuple[str, str], str],
) -> bool:
    """A top/bottom pair that runs sideways is the next phase, not this conductor."""

    if not sides or first[0] == second[0]:
        return False
    pair = (sides.get((first[0], first[1]), ""), sides.get((second[0], second[1]), ""))
    if pair not in _OPPOSITE_SIDES:
        return False
    dx = abs(first[2] - second[2])
    dy = abs(first[3] - second[3])
    # A full phase column is about 10 mm away. A short gap whose pins differ by
    # less than that is still this conductor: two 3-pole symbols rarely share a pitch.
    if pair[0] in {"top", "bottom"}:
        return dx > 8.0 and dx > dy
    return dy > 8.0 and dy > dx


def _electrical_map(devices: list[dict[str, Any]], catalog: Any) -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    if catalog is None:
        return found
    for device in devices:
        lib = str(device.get("library_id") or "")
        tag = str(device.get("tag") or "")
        if not lib or not tag or device.get("guess"):
            continue
        try:
            part = catalog.get(lib)
        except KeyError:
            continue
        for pin in part.pins:
            found[(tag, str(pin.id))] = str(pin.electrical or "")
    return found


def _unit(start: tuple[float, float], end: tuple[float, float]) -> tuple[float, float]:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return (0.0, 0.0)
    return (dx / length, dy / length)


def _dot(a: tuple[float, float], b: tuple[float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _near_dot(point: tuple[float, float], dots: list[tuple[float, float]], limit: float) -> bool:
    return any(math.hypot(dot[0] - point[0], dot[1] - point[1]) <= limit for dot in dots)


def _extend_colinear(
    start: tuple[float, float],
    end: tuple[float, float],
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
    dots: list[tuple[float, float]],
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Join colinear pieces, including a gap that has a junction dot."""

    ends = [start, end]
    used: set[int] = set()
    for forward in (1, 0):
        for _guard in range(12):
            origin = ends[forward]
            direction = _unit(ends[1 - forward], origin)
            if direction == (0.0, 0.0):
                break
            best_index: int | None = None
            best_gap = 1e9
            best_point: tuple[float, float] | None = None
            for index, (a, b) in enumerate(segments):
                if index in used:
                    continue
                for near, far in ((a, b), (b, a)):
                    gap = math.hypot(near[0] - origin[0], near[1] - origin[1])
                    limit = 8.0 if _near_dot(origin, dots, 3.0) or _near_dot(near, dots, 3.0) else 6.0
                    if gap > limit or gap >= best_gap:
                        continue
                    piece = _unit(near, far)
                    if _dot(direction, piece) < 0.75:
                        continue
                    if gap > 0.4 and _dot(direction, _unit(origin, near)) < 0.75:
                        continue
                    best_index = index
                    best_gap = gap
                    best_point = far
            if best_index is None or best_point is None:
                break
            used.add(best_index)
            ends[forward] = best_point
    return ends[0], ends[1]


def _snap_pin(
    point: tuple[float, float],
    pins: list[tuple[str, str, float, float]],
    used: set[tuple[str, str]],
    limit: float,
    direction: tuple[float, float],
    electrical: dict[tuple[str, str], str],
) -> tuple[str, str, float, float] | None:
    """The free pin at this end. A power pin along the wire beats a nearer aux pin."""

    best = None
    best_score: float | None = None
    for pin in pins:
        key = (pin[0], pin[1])
        if key in used:
            continue
        dist = math.hypot(pin[2] - point[0], pin[3] - point[1])
        if dist > limit:
            continue
        score = dist
        kind = electrical.get(key, "")
        align = 1.0
        if dist > 0.2:
            toward = ((pin[2] - point[0]) / dist, (pin[3] - point[1]) / dist)
            align = _dot(direction, toward)
        if kind == "power" and align > 0.35:
            score -= 6.0
        elif kind and kind != "power":
            score += 4.0
        if align < 0:
            score += 3.0
        if best_score is None or score < best_score:
            best = pin
            best_score = score
    return best


def _copy_pixel_pins(
    wire: dict[str, Any],
    segment: tuple[tuple[float, float], tuple[float, float]],
    pixel_wires: list[dict[str, Any]],
    pins: list[tuple[str, str, float, float]] | None = None,
) -> None:
    number = str(wire.get("number") or "")
    px = float(wire.get("x") or 0)
    py = float(wire.get("y") or 0)
    best: dict[str, Any] | None = None
    best_d = WIRE_ATTACH_MM
    for pixel in pixel_wires:
        if str(pixel.get("number") or "") != number:
            continue
        if not (pixel.get("from_pin") and pixel.get("to_pin")) or pixel.get("ambiguous"):
            continue
        try:
            qx = float(pixel.get("x") or 0)
            qy = float(pixel.get("y") or 0)
        except (TypeError, ValueError):
            continue
        near = min(
            math.hypot(qx - px, qy - py),
            _segment_distance(qx, qy, segment[0], segment[1]),
        )
        if near <= best_d:
            best_d = near
            best = pixel
    if best is None or not _pins_on_segment(best, segment, pins):
        wire["needs_review"] = True
        return
    wire["from_tag"] = str(best.get("from_tag") or "")
    wire["from_pin"] = str(best.get("from_pin") or "")
    wire["to_tag"] = str(best.get("to_tag") or "")
    wire["to_pin"] = str(best.get("to_pin") or "")
    wire["needs_review"] = False
    wire["stub"] = False


def _pins_on_segment(
    pixel: dict[str, Any],
    segment: tuple[tuple[float, float], tuple[float, float]],
    pins: list[tuple[str, str, float, float]] | None,
) -> bool:
    """Keep a pixel pin pair only when both pins lie on the vision conductor.

    OCR can pin a number to the next phase. Copying those pins would throw
    away the ink trace. With no pin map the old copy still stands.
    """

    if pins is None:
        return True
    by = {(str(tag), str(pin)): (x, y) for tag, pin, x, y in pins}
    start, end = segment
    for tag_key, pin_key in (("from_tag", "from_pin"), ("to_tag", "to_pin")):
        point = by.get((str(pixel.get(tag_key) or ""), str(pixel.get(pin_key) or "")))
        if point is None:
            return False
        perp, along = _perp_along(point[0], point[1], start, end)
        if perp > _PIN_ON_SEGMENT_MM or along > _PIN_ALONG_SEGMENT_MM:
            return False
    return True


def _share_seam(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not _has_tile(left) or not _has_tile(right):
        return False
    a_box = _tile_box(left)
    b_box = _tile_box(right)
    if _box_gap(a_box, b_box) > 1.0:
        return False
    return _near_box(left, b_box) and _near_box(right, a_box)


def _has_tile(wire: dict[str, Any]) -> bool:
    return all(key in wire for key in ("tile_x", "tile_y", "tile_w", "tile_h"))


def _tile_box(wire: dict[str, Any]) -> tuple[float, float, float, float]:
    return (
        float(wire["tile_x"]),
        float(wire["tile_y"]),
        float(wire["tile_w"]),
        float(wire["tile_h"]),
    )


def _box_gap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x_gap = max(0.0, max(ax, bx) - min(ax + aw, bx + bw))
    y_gap = max(0.0, max(ay, by) - min(ay + ah, by + bh))
    if x_gap > 0 and y_gap > 0:
        return math.hypot(x_gap, y_gap)
    return max(x_gap, y_gap)


def _near_box(wire: dict[str, Any], box: tuple[float, float, float, float]) -> bool:
    x = float(wire.get("x") or 0)
    y = float(wire.get("y") or 0)
    ox, oy, ow, oh = box
    dx = 0.0 if ox <= x <= ox + ow else min(abs(x - ox), abs(x - (ox + ow)))
    dy = 0.0 if oy <= y <= oy + oh else min(abs(y - oy), abs(y - (oy + oh)))
    return math.hypot(dx, dy) <= SEAM_MM


def _union_wire_ends(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    if _wire_rank(b) > _wire_rank(a):
        base: dict[str, Any] = dict(b)
        other = a
    else:
        base = dict(a)
        other = b
    left, right = (a, b) if float(a.get("x") or 0) <= float(b.get("x") or 0) else (b, a)
    if left.get("from_pin") and right.get("to_pin"):
        base["from_tag"] = str(left.get("from_tag") or "")
        base["from_pin"] = str(left.get("from_pin") or "")
        base["to_tag"] = str(right.get("to_tag") or "")
        base["to_pin"] = str(right.get("to_pin") or "")
    else:
        if not base.get("from_pin") and other.get("from_pin"):
            base["from_tag"] = str(other.get("from_tag") or "")
            base["from_pin"] = str(other.get("from_pin") or "")
        if not base.get("to_pin") and other.get("to_pin"):
            base["to_tag"] = str(other.get("to_tag") or "")
            base["to_pin"] = str(other.get("to_pin") or "")
    same = (base.get("from_tag"), base.get("from_pin")) == (base.get("to_tag"), base.get("to_pin"))
    if base.get("from_pin") and base.get("to_pin") and not same:
        base["ambiguous"] = False
        base["attached"] = True
        base["stub"] = False
        base["needs_review"] = False
    return base


def _wire_item(number: dict[str, Any], a: tuple[str, str], b: tuple[str, str], ambiguous: bool) -> dict[str, Any]:
    conf = float(number.get("confidence") or 0.7)
    if ambiguous:
        return {
            "number": str(number["text"]),
            "x": number.get("x"),
            "y": number.get("y"),
            "from_tag": "",
            "to_tag": "",
            "from_pin": "",
            "to_pin": "",
            "confidence": min(conf, 0.45),
            "unreadable": False,
            "ambiguous": True,
            "source": "ocr",
        }
    return {
        "number": str(number["text"]),
        "x": number.get("x"),
        "y": number.get("y"),
        "from_tag": a[0],
        "from_pin": a[1],
        "to_tag": b[0],
        "to_pin": b[1],
        "confidence": round(min(0.9, conf), 2),
        "unreadable": False,
        "ambiguous": False,
        "source": "ocr",
    }


def _power_family(device: dict[str, Any]) -> bool:
    blob = " ".join(
        str(device.get(key) or "")
        for key in ("library_id", "symbol", "type")
    ).lower()
    return any(word in blob for word in ("contactor", "overload", "motor"))


def _symbol_span(device: dict[str, Any], catalog: Any) -> tuple[float, float]:
    """Unrotated schematic width and height. Rotation 270 uses the height along x."""

    width = float(device.get("w") or 0)
    height = float(device.get("h") or 0)
    lib = str(device.get("library_id") or "")
    if catalog is not None and lib:
        try:
            part = catalog.get(lib)
        except KeyError:
            part = None
        if part is not None:
            width = float(getattr(part, "schematic_width", 0) or 0) or width
            height = float(getattr(part, "schematic_height", 0) or 0) or height
    return width or 40.0, height or 70.0


def orient_ladder_devices(
    devices: list[dict[str, Any]],
    catalog: Any,
    *,
    spread: bool = True,
    page_width: float = 0.0,
) -> list[dict[str, Any]]:
    """Turn a horizontal power rung so line pins face the bus and load pins face the motor.

    A vertical starter (same x, stacked y) stays at rotation 0. Rotation is
    clockwise, origin at the symbol's top-left, y down. 270° puts the top pins
    on the left and the bottom pins on the right. ``spread`` pushes overlapping
    symbols apart along x by the rotated span. It does not move y. ``page_width``
    shifts the rung left so the rotated symbols stay on the sheet.
    """

    groups: dict[str, list[dict[str, Any]]] = {}
    for device in devices:
        if device.get("title_ink") or device.get("unlocalised") or device.get("tag_unparsed"):
            continue
        if not _power_family(device):
            continue
        match = re.match(r"(\d+)", str(device.get("tag") or ""))
        if not match:
            continue
        groups.setdefault(match.group(1), []).append(device)
    for group in groups.values():
        if len(group) < 2:
            continue
        xs = [float(device.get("x") or 0) for device in group]
        ys = [float(device.get("y") or 0) for device in group]
        dx = max(xs) - min(xs)
        dy = max(ys) - min(ys)
        if not (dx > dy + 15 and dy < 40):
            continue
        group.sort(key=lambda device: float(device.get("x") or 0))
        for device in group:
            device["rotation"] = 270
        if not spread:
            continue
        for index, device in enumerate(group):
            if index == 0:
                continue
            previous = group[index - 1]
            _pw, previous_height = _symbol_span(previous, catalog)
            needed = float(previous.get("x") or 0) + previous_height + 6.0
            if float(device.get("x") or 0) < needed - 1.0:
                device["x"] = round(needed, 2)
        _keep_rung_on_page(group, catalog, page_width)
    return devices


def _keep_rung_on_page(group: list[dict[str, Any]], catalog: Any, page_width: float) -> None:
    """Shift a horizontal rung left so its rotated extent stays inside the sheet.

    Y is unchanged. A rung that already fits is left where it is.
    """

    if page_width <= 40 or len(group) < 2:
        return
    margin = 6.0

    def right_edge(device: dict[str, Any]) -> float:
        rotation = int(device.get("rotation") or 0) % 360
        width, height = _symbol_span(device, catalog)
        span = height if rotation in {90, 270} else width
        return float(device.get("x") or 0) + span

    limit = page_width - margin
    overflow = max(right_edge(device) for device in group) - limit
    if overflow <= 0.5:
        return
    for device in group:
        device["x"] = round(float(device.get("x") or 0) - overflow, 2)
    left = min(float(device.get("x") or 0) for device in group)
    if left >= margin:
        return
    group.sort(key=lambda device: float(device.get("x") or 0))
    cursor = margin
    for device in group:
        device["x"] = round(cursor, 2)
        _width, height = _symbol_span(device, catalog)
        cursor += height + 6.0


def _pin_points(
    devices: list[dict[str, Any]],
    catalog: Any,
    *,
    include_guess: bool = False,
) -> list[tuple[str, str, float, float]]:
    found = []
    for device in devices:
        lib = str(device.get("library_id") or "")
        if not lib or (device.get("guess") and not include_guess):
            continue
        try:
            part = catalog.get(lib)
        except KeyError:
            continue
        if device.get("unlocalised"):
            continue
        ox, oy = float(device["x"]), float(device["y"])
        scale = float(device.get("scale") or 1.0)
        rotation = int(device.get("rotation") or 0)
        for pin in part.pins:
            px, py = rotate_point(float(pin.x) * scale, float(pin.y) * scale, rotation)
            found.append((str(device["tag"]), str(pin.id), ox + px, oy + py))
    return found


def _pin_links(
    pins: list[tuple[str, str, float, float]],
    mask: np.ndarray,
    ppm: float,
    sides: dict[tuple[str, str], str] | None = None,
) -> list[tuple[tuple[str, str], tuple[str, str], tuple[float, float], tuple[float, float]]]:
    """Shortest pin pairs joined by an unbroken run of ink.

    ``sides`` lets a vision trace keep a top-to-bottom pair the symbol mask
    swallowed, and drop a pair that runs across the phase columns.
    """

    side_of = sides or {}
    candidates = []
    for index, first in enumerate(pins):
        for second in pins[index + 1 :]:
            if first[0] == second[0] and first[1] == second[1]:
                continue
            length = math.hypot(first[2] - second[2], first[3] - second[3])
            facing = _facing_pair(first, second, side_of)
            if length < 1.5 or length > 28:
                if facing:
                    candidates.append((length, first, second))
                continue
            if _cross_phase(first, second, side_of):
                continue
            fraction, gap = _ink_run(mask, ppm, (first[2], first[3]), (second[2], second[3]))
            if fraction >= 0.45 and gap <= 3.6:
                candidates.append((length, first, second))
            elif facing:
                candidates.append((length, first, second))
    candidates.sort(key=lambda item: item[0])
    used: set[tuple[str, str]] = set()
    links = []
    for _length, first, second in candidates:
        a_id, b_id = (first[0], first[1]), (second[0], second[1])
        if a_id in used or b_id in used:
            continue
        used.add(a_id)
        used.add(b_id)
        start, end = _conductor_ends(first, second, side_of)
        links.append((a_id, b_id, start, end))
    return links


def _conductor_ends(
    first: tuple[str, str, float, float],
    second: tuple[str, str, float, float],
    sides: dict[tuple[str, str], str],
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Pin-to-pin, unless the two symbols swallowed a short facing conductor.

    A 0.4 mm leftover is not the wire direction. The pin sides are.
    """

    start = (first[2], first[3])
    end = (second[2], second[3])
    if not _facing_pair(first, second, sides):
        return start, end
    if math.hypot(end[0] - start[0], end[1] - start[1]) >= 3.0:
        return start, end
    mx = (start[0] + end[0]) / 2.0
    my = (start[1] + end[1]) / 2.0
    side = sides.get((first[0], first[1]), "")
    if side in {"top", "bottom"}:
        return (mx, my - 2.0), (mx, my + 2.0)
    return (mx - 2.0, my), (mx + 2.0, my)


def _ink_run(mask: np.ndarray, ppm: float, start: tuple[float, float], end: tuple[float, float]) -> tuple[float, float]:
    length = math.hypot(end[0] - start[0], end[1] - start[1])
    steps = max(6, int(length / 0.5))
    hits = 0
    gap = 0.0
    widest = 0.0
    step = length / steps
    height, width = mask.shape
    for index in range(steps):
        t = index / (steps - 1) if steps > 1 else 0.0
        x = start[0] * (1 - t) + end[0] * t
        y = start[1] * (1 - t) + end[1] * t
        px, py = int(round(x * ppm)), int(round(y * ppm))
        window = mask[max(0, py - 1) : min(height, py + 2), max(0, px - 1) : min(width, px + 2)]
        if window.size and int(window.max()) > 0:
            hits += 1
            widest = max(widest, gap)
            gap = 0.0
        else:
            gap += step
    return hits / steps, max(widest, gap)


def _wire_mask(
    binary: np.ndarray,
    rgb: np.ndarray,
    devices: list[dict[str, Any]],
    catalog: Any,
    ppm: float,
) -> np.ndarray:
    red = rgb[:, :, 0].astype(np.int16)
    green = rgb[:, :, 1].astype(np.int16)
    blue = rgb[:, :, 2].astype(np.int16)
    colored = (blue > 90) & (blue > red + 25) & (blue > green + 12)
    fraction = float(colored.mean()) if colored.size else 0.0
    # A few blue terminal stubs are not a wire layer. Use blue only when it
    # covers a real network of conductors.
    if 0.002 < fraction < 0.08:
        mask = colored.astype(np.uint8)
    else:
        mask = binary.copy()
    for device in devices:
        if device.get("guess") or not device.get("library_id"):
            continue
        try:
            part = catalog.get(device["library_id"])
        except KeyError:
            continue
        tmpl = _render_template(part, ppm)
        if tmpl is None:
            continue
        ox, oy = int(round(float(device["x"]) * ppm)), int(round(float(device["y"]) * ppm))
        th, tw = tmpl.shape
        page_h, page_w = mask.shape
        if ox < 0 or oy < 0 or ox + tw >= page_w or oy + th >= page_h:
            continue
        fat = cv2.dilate(tmpl, np.ones((3, 3), np.uint8))
        mask[oy : oy + th, ox : ox + tw][fat > 0] = 0
    return mask


def _line_segments(mask: np.ndarray, ppm: float, width_mm: float, height_mm: float) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    image = (mask > 0).astype(np.uint8) * 255
    found = cv2.HoughLinesP(
        image,
        1,
        np.pi / 180.0,
        threshold=max(8, int(2.2 * ppm)),
        minLineLength=max(6, int(3.0 * ppm)),
        maxLineGap=max(2, int(1.4 * ppm)),
    )
    segments = []
    if found is None:
        return segments
    for x1, y1, x2, y2 in found.reshape(-1, 4):
        a = (float(x1) / ppm, float(y1) / ppm)
        b = (float(x2) / ppm, float(y2) / ppm)
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        border = min(a[0], b[0]) < 10 or max(a[0], b[0]) > width_mm - 10 or min(a[1], b[1]) < 12
        if length > 40 and border:
            continue
        if max(a[1], b[1]) > height_mm - 48 and length > 25:
            continue
        segments.append((a, b))
    return segments


def _chains(segments: list[tuple[tuple[float, float], tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    if not segments:
        return []
    parent = list(range(len(segments) * 2))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    points = []
    for index, seg in enumerate(segments):
        points.extend(seg)
        union(index * 2, index * 2 + 1)
    for i, a in enumerate(points):
        for j in range(i + 1, len(points)):
            b = points[j]
            if math.hypot(a[0] - b[0], a[1] - b[1]) <= 2.2:
                union(i, j)
    groups: dict[int, list[tuple[float, float]]] = {}
    for i, point in enumerate(points):
        groups.setdefault(find(i), []).append(point)
    chains = []
    for pts in groups.values():
        if len(pts) < 2:
            continue
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        far = max(pts, key=lambda p: math.hypot(p[0] - cx, p[1] - cy))
        near = min(pts, key=lambda p: math.hypot(p[0] - far[0], p[1] - far[1]))
        other = max(pts, key=lambda p: math.hypot(p[0] - near[0], p[1] - near[1]))
        if math.hypot(near[0] - other[0], near[1] - other[1]) < 2.5:
            continue
        chains.append([near, other])
    return chains


def _junction_dots(binary: np.ndarray, ppm: float) -> list[tuple[float, float]]:
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats((binary > 0).astype(np.uint8), 8)
    dots = []
    for index in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[index])
        wmm, hmm = w / ppm, h / ppm
        if not (0.5 <= wmm <= 2.4 and 0.5 <= hmm <= 2.4):
            continue
        if area / float(w * h) < 0.45:
            continue
        cx, cy = centroids[index]
        dots.append((float(cx) / ppm, float(cy) / ppm))
    return dots


def _snap_ends(
    chain: list[tuple[float, float]],
    pins: list[tuple[str, str, float, float]],
    dots: list[tuple[float, float]],
) -> tuple[tuple[str, str], tuple[str, str], bool] | None:
    if len(chain) < 2 or len(pins) < 2:
        return None
    snapped = []
    ambiguous = False
    for point in (chain[0], chain[-1]):
        target = point
        for dot in dots:
            if math.hypot(dot[0] - point[0], dot[1] - point[1]) <= 1.8:
                target = dot
                break
        ranked = sorted(pins, key=lambda pin: math.hypot(pin[2] - target[0], pin[3] - target[1]))
        best = ranked[0]
        dist = math.hypot(best[2] - target[0], best[3] - target[1])
        if dist > PIN_SNAP_MM:
            return None
        second = ranked[1]
        second_d = math.hypot(second[2] - target[0], second[3] - target[1])
        if second_d < dist + AMBIGUOUS_MM and (second[0], second[1]) != (best[0], best[1]):
            ambiguous = True
        snapped.append((best[0], best[1]))
    if snapped[0] == snapped[1]:
        return None
    return snapped[0], snapped[1], ambiguous


def _nearest_number(
    chain: list[tuple[float, float]],
    numbers: list[dict[str, Any]],
    used: set[int],
) -> tuple[dict[str, Any] | None, int]:
    if not numbers or not chain:
        return None, -1
    start, end = chain[0], chain[-1]
    best_i = -1
    best_s: float | None = None
    for index, number in enumerate(numbers):
        if index in used:
            continue
        # Vertical numbers sit to the right of the wire, horizontal numbers above it.
        # Raw distance hands a label to the next phase when the columns are close.
        score = _convention_score(float(number["x"]), float(number["y"]), start, end, 16.0)
        if score is None:
            continue
        if best_s is None or score < best_s:
            best_s = score
            best_i = index
    if best_i < 0:
        return None, -1
    return numbers[best_i], best_i


def _title_confidence(title: dict[str, Any]) -> float:
    keys = ("company", "project", "customer", "location", "description", "sheet_count")
    found = sum(1 for key in keys if str(title.get(key) or "").strip())
    if found == 0:
        return 0.2
    return round(min(0.9, 0.35 + 0.1 * found), 2)


def _title_items(title: dict[str, Any], confidence: float) -> list[dict[str, Any]]:
    rows = []
    if title.get("sheet_number_needs_info"):
        rows.append(
            {
                "text": "",
                "role": "title",
                "field": "sheet_number",
                "x": None,
                "y": None,
                "unreadable": True,
                "needs_info": True,
                "confidence": 0.0,
                "source": "ocr",
            }
        )
    for key in ("company", "project", "customer", "location", "description", "drawing_number", "sheet_count"):
        value = str(title.get(key) or "").strip()
        if not value:
            continue
        rows.append(
            {
                "text": f"{key}: {value}",
                "role": "title",
                "x": None,
                "y": None,
                "unreadable": False,
                "confidence": confidence,
                "source": "ocr",
            }
        )
    return rows
