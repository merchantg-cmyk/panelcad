"""A row of terminal blocks on a scan.

Blocks are narrow rectangles of one pitch, left to right, the way a strip is
mounted. The tag is the text inside the block. The wire number above the block
and the one below it are read separately. A value that is not readable stays
``?``. The letters OCR returned are not edited.
"""

from __future__ import annotations

import re
from typing import Any

import cv2
import numpy as np

_TAG = re.compile(r"[A-Z]{1,4}\d+(?:-[A-Z0-9]+)?")
# One digit is a screw or a zone mark. A strip wire number has at least two.
_NUMBER = re.compile(r"\d{2,4}")


def read_terminal_strip(
    gray: np.ndarray,
    rgb: np.ndarray,
    ppm: float,
) -> list[dict[str, Any]]:
    """Devices for one strip, in mounting order. Empty when the page has no row."""

    blocks = find_block_row(gray, rgb, ppm)
    if len(blocks) < 3:
        return []
    paper, dark, _thr = _ink_levels(gray)
    devices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, block in enumerate(blocks):
        tag = _tag_in(gray, block, ppm, paper=paper, dark=dark)
        if tag == "?":
            # An unreadable block is not given a made-up name.
            continue
        if tag in seen:
            continue
        seen.add(tag)
        top = _number_in(gray, block, ppm, "top", paper=paper, dark=dark)
        bottom = _number_in(gray, block, ppm, "bottom", paper=paper, dark=dark)
        devices.append(
            {
                "tag": tag,
                "type": "terminal",
                "symbol": "terminal",
                "pins": [],
                "x": round(block["x_mm"], 2),
                "y": round(block["y_mm"], 2),
                "w": round(block["w_mm"], 2),
                "h": round(block["h_mm"], 2),
                "confidence": 0.72,
                "unreadable": False,
                "guess": False,
                "library_id": "",
                "pin_frame": "",
                "scale": 1.0,
                "source": "ocr",
                "ground": bool(block.get("ground")),
                "wire_top": top,
                "wire_bottom": bottom,
                "strip_index": index,
            }
        )
    return devices


def _ink_levels(gray: np.ndarray) -> tuple[float, float, int]:
    """Return ``(paper, dark, threshold)`` adapted to the scan's contrast."""

    if gray.size == 0:
        return 255.0, 0.0, 90
    paper = float(np.percentile(gray, 90))
    dark_px = gray[gray < paper - 25.0]
    if dark_px.size == 0:
        return paper, paper, max(0, int(paper - 25))
    dark = float(np.percentile(dark_px, 5))
    thr = int(round(dark + 0.38 * (paper - dark)))
    return paper, dark, min(int(paper - 15), max(40, thr))


def _normalize_contrast(patch: np.ndarray, paper: float, dark: float) -> np.ndarray:
    """Stretch faded page contrast into 0..255 without amplifying blank noise."""

    span = paper - dark
    if span < 20.0:
        return patch
    if paper >= 245.0 and dark <= 45.0:
        return patch
    scaled = (patch.astype(np.float32) - dark) * (255.0 / span)
    return np.clip(scaled, 0, 255).astype(np.uint8)


def _despec_single_dots(patch: np.ndarray) -> np.ndarray:
    """Remove isolated 1-pixel salt/pepper dots without touching glyphs or hyphens."""

    if patch.size == 0 or min(patch.shape[:2]) < 3:
        return patch
    out = patch.copy()
    ink = (out < 175).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    single_dots = 0
    if count > 1:
        for idx in range(1, count):
            area = int(stats[idx, cv2.CC_STAT_AREA])
            w = int(stats[idx, cv2.CC_STAT_WIDTH])
            h = int(stats[idx, cv2.CC_STAT_HEIGHT])
            if area <= 1 and w <= 1 and h <= 1:
                out[labels == idx] = 255
                single_dots += 1
        # On a genuinely speckled crop, also clear isolated 2x2 toner clumps
        # (never touching hyphens which are wider than 2 px).
        if single_dots >= 2:
            for idx in range(1, count):
                area = int(stats[idx, cv2.CC_STAT_AREA])
                w = int(stats[idx, cv2.CC_STAT_WIDTH])
                h = int(stats[idx, cv2.CC_STAT_HEIGHT])
                if area <= 4 and w <= 2 and h <= 2:
                    out[labels == idx] = 255
    # Fill isolated 1x1 bright salt specks sitting inside dark strokes.
    kernel = np.ones((3, 3), dtype=np.uint8)
    kernel[1, 1] = 0
    bright = (out > 200).astype(np.uint8)
    bright_neighbors = cv2.filter2D(bright, cv2.CV_16S, kernel)
    med = cv2.medianBlur(out, 3)
    salt = (bright == 1) & (bright_neighbors == 0) & (med < 165)
    if salt.any():
        out[salt] = med[salt]
    return out


def find_block_row(gray: np.ndarray, rgb: np.ndarray, ppm: float) -> list[dict[str, Any]]:
    """One row of similar narrow blocks. Coordinates are millimetres, top-left."""

    if ppm <= 0 or gray.size == 0:
        return []
    paper, dark, thr = _ink_levels(gray)
    ink = (gray < thr).astype(np.uint8)
    min_h = max(8, int(round(16.0 * ppm)))
    max_h = int(round(48.0 * ppm))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, min_h // 2)))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(vertical, 8)
    strokes: list[tuple[int, int, int, int]] = []
    for index in range(1, count):
        x, y, w, h, _area = (int(value) for value in stats[index])
        if w > max(6, int(2.5 * ppm)) or h < min_h or h > max_h:
            continue
        strokes.append((x, y, w, h))
    if len(strokes) < 4:
        return []
    strokes.sort(key=lambda item: (item[1], item[0]))
    rows = _cluster_rows(strokes, ppm)
    best: list[dict[str, Any]] = []
    for row in rows:
        blocks = _blocks_from_strokes(row, gray, rgb, ppm, paper=paper, dark=dark)
        if len(blocks) > len(best):
            best = blocks
    return best


def _cluster_rows(
    strokes: list[tuple[int, int, int, int]],
    ppm: float,
) -> list[list[tuple[int, int, int, int]]]:
    heights = [stroke[3] for stroke in strokes]
    median_h = float(np.median(heights)) if heights else 0.0
    # A broken side still belongs to the row. The slack is a fraction of the block.
    tol = max(4.0, 0.28 * median_h, 3.0 * ppm)
    rows: list[list[tuple[int, int, int, int]]] = []
    for stroke in strokes:
        y = stroke[1]
        placed = False
        for row in rows:
            if abs(y - row[0][1]) <= tol:
                row.append(stroke)
                placed = True
                break
        if not placed:
            rows.append([stroke])
    return rows


def _blocks_from_strokes(
    strokes: list[tuple[int, int, int, int]],
    gray: np.ndarray,
    rgb: np.ndarray,
    ppm: float,
    *,
    paper: float = 255.0,
    dark: float = 0.0,
) -> list[dict[str, Any]]:
    strokes = sorted(strokes, key=lambda item: item[0])
    gaps: list[tuple[int, int, int, int]] = []
    for left, right in zip(strokes, strokes[1:]):
        x0 = left[0] + left[2]
        x1 = right[0]
        width = x1 - x0
        if width < 4.0 * ppm or width > 16.0 * ppm:
            continue
        y = min(left[1], right[1])
        height = max(left[3], right[3])
        gaps.append((x0, y, width, height))
    if len(gaps) < 3:
        return []
    widths = np.array([gap[2] for gap in gaps], dtype=np.float32)
    median = float(np.median(widths))
    if median <= 0:
        return []
    kept = [gap for gap in gaps if abs(gap[2] - median) / median <= 0.35]
    if len(kept) < 3:
        return []
    pitch = np.diff([gap[0] for gap in kept])
    if len(pitch) and float(np.std(pitch)) > float(np.median(pitch)) * 0.45:
        return []
    blocks: list[dict[str, Any]] = []
    for x, y, width, height in kept:
        ground = _is_ground(gray, rgb, x, y, width, height, paper=paper, dark=dark)
        blocks.append(
            {
                "x_mm": x / ppm,
                "y_mm": y / ppm,
                "w_mm": width / ppm,
                "h_mm": height / ppm,
                "x": x,
                "y": y,
                "w": width,
                "h": height,
                "ground": ground,
            }
        )
    blocks.sort(key=lambda item: item["x_mm"])
    return blocks


def _is_ground(
    gray: np.ndarray,
    rgb: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    paper: float = 255.0,
    dark: float = 0.0,
) -> bool:
    """A green body, or the three-bar earth mark inside the block."""

    x1, y1 = min(gray.shape[1], x + w), min(gray.shape[0], y + h)
    x0, y0 = max(0, x), max(0, y)
    if x1 <= x0 or y1 <= y0:
        return False
    crop = rgb[y0:y1, x0:x1]
    if crop.size:
        red = crop[:, :, 0].astype(np.int16)
        green = crop[:, :, 1].astype(np.int16)
        blue = crop[:, :, 2].astype(np.int16)
        scale = max(0.35, min(1.0, (paper - dark) / 255.0))
        dr = max(6, int(round(18 * scale)))
        db = max(3, int(round(8 * scale)))
        if float(((green > red + dr) & (green > blue + db) & (green > 70)).mean()) > 0.12:
            return True
    patch = _normalize_contrast(gray[y0:y1, x0:x1], paper, dark)
    ink = patch < 100
    if ink.size == 0 or ink.shape[1] < 6:
        return False
    lengths: list[int] = []
    last = -10
    width = ink.shape[1]
    mid = width / 2.0
    for index in range(ink.shape[0]):
        cols = np.where(ink[index])[0]
        if len(cols) < 3:
            continue
        # One centered stroke. A screw is a wide blob, not this.
        if int(cols[-1] - cols[0]) != len(cols) - 1:
            continue
        length = int(cols[-1] - cols[0] + 1)
        center = float(cols[0] + cols[-1]) / 2.0
        if not (0.12 * width <= length <= 0.5 * width):
            continue
        if abs(center - mid) > width * 0.18:
            continue
        if index - last < max(2, h // 28):
            continue
        lengths.append(length)
        last = index
    if len(lengths) < 3:
        return False
    longest_first = lengths[0] >= lengths[1] >= lengths[2]
    return longest_first


def _earth_top_y(
    gray: np.ndarray,
    block: dict[str, Any],
    paper: float,
    dark: float,
) -> int | None:
    """Top y of the three-bar earth mark inside a ground block, if present."""

    x0 = max(0, int(block["x"]) + 2)
    x1 = min(gray.shape[1], int(block["x"] + block["w"]) - 2)
    y0 = max(0, int(block["y"] + block["h"] * 0.42))
    y1 = min(gray.shape[0], int(block["y"] + block["h"] * 0.82))
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None
    patch = _normalize_contrast(gray[y0:y1, x0:x1], paper, dark)
    ink = patch < 140
    width = ink.shape[1]
    mid = width / 2.0
    bars: list[tuple[int, int]] = []
    last = -10
    for idx in range(ink.shape[0]):
        cols = np.where(ink[idx])[0]
        if len(cols) < 3:
            continue
        length = int(cols[-1] - cols[0] + 1)
        if not (0.22 * width <= length <= 0.90 * width):
            continue
        if len(cols) < 0.80 * length:
            continue
        center = float(cols[0] + cols[-1]) / 2.0
        if abs(center - mid) > width * 0.20:
            continue
        if idx - last < max(2, int(block["h"]) // 32):
            continue
        bars.append((idx, length))
        last = idx
    if len(bars) >= 3 and bars[0][1] >= bars[1][1] >= bars[2][1]:
        return y0 + bars[0][0]
    return None


def _tag_crop(
    gray: np.ndarray,
    block: dict[str, Any],
    ppm: float,
    paper: float,
    dark: float,
) -> np.ndarray | None:
    x0 = max(0, int(block["x"]) + 2)
    x1 = min(gray.shape[1], int(block["x"] + block["w"]) - 2)
    top = max(0, int(block["y"] + block["h"] * 0.22))
    bot = min(gray.shape[0], int(block["y"] + block["h"] * 0.78))
    earth_y = _earth_top_y(gray, block, paper, dark)
    if earth_y is not None and earth_y - 2 > top + 4:
        bot = min(bot, earth_y - 2)
    if x1 - x0 < 4 or bot - top < 4:
        return None
    patch = _normalize_contrast(gray[top:bot, x0:x1], paper, dark)
    # A tinted block body (e.g. green PE housing ~226 gray) must be lifted to
    # paper white so white border padding does not create a box edge in OCR.
    local_bg = float(np.percentile(patch, 85))
    if 160.0 <= local_bg < 248.0:
        patch = _normalize_contrast(patch, local_bg, 0.0)
    patch = _despec_single_dots(patch)
    row_ink = (patch < 185).sum(axis=1) >= 1
    col_ink = (patch < 185).sum(axis=0) >= 1
    ink_rows = np.where(row_ink)[0]
    ink_cols = np.where(col_ink)[0]
    if ink_rows.size == 0 or ink_cols.size == 0:
        return None
    pad = max(3, int(round(0.4 * ppm)))
    return patch[
        max(0, int(ink_rows[0]) - pad) : min(patch.shape[0], int(ink_rows[-1]) + pad + 1),
        max(0, int(ink_cols[0]) - pad) : min(patch.shape[1], int(ink_cols[-1]) + pad + 1),
    ]


def _tag_in(
    gray: np.ndarray,
    block: dict[str, Any],
    ppm: float,
    *,
    paper: float | None = None,
    dark: float | None = None,
) -> str:
    if paper is None or dark is None:
        paper, dark, _thr = _ink_levels(gray)
    crop = _tag_crop(gray, block, ppm, paper, dark)
    if crop is None:
        return "?"
    h, w = crop.shape
    views = (
        (cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE), crop)
        if h > w * 1.15
        else (crop, cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE))
    )
    fallback_tag = ""
    for view in views:
        heard = _ocr_crop(view, digits=False).replace("\u00d7", "X").upper()
        tags = _TAG.findall(heard)
        hyphen = [tag for tag in tags if "-" in tag]
        if hyphen:
            return hyphen[0]
        if tags and not fallback_tag:
            fallback_tag = tags[0]
        # If the detail ONNX pass returned a partial tag without the suffix,
        # check the secondary single-line reader on the same crop.
        alt = _ocr(view, digits=False).replace("\u00d7", "X").upper()
        alt_tags = _TAG.findall(alt)
        alt_hyphen = [tag for tag in alt_tags if "-" in tag]
        if alt_hyphen:
            return alt_hyphen[0]
        if alt_tags and not fallback_tag:
            fallback_tag = alt_tags[0]
    return fallback_tag or "?"


def _clean_rule_row(row: np.ndarray) -> np.ndarray:
    """Subtract a horizontal rule background while preserving darker digit strokes."""

    med = float(np.median(row))
    if med <= 45.0:
        return np.full_like(row, 255)
    delta = med - row.astype(np.float32)
    scale = 255.0 / max(med - 15.0, 35.0)
    out = np.where(delta > 10.0, 255.0 - delta * scale * 1.15, 255.0)
    return np.clip(out, 0, 255).astype(np.uint8)


def _number_crop(
    gray: np.ndarray,
    block: dict[str, Any],
    ppm: float,
    side: str,
    paper: float,
    dark: float,
) -> np.ndarray | None:
    """Tight wire-number crop strictly outside the block.

    Skips past the block's own horizontal border (never entering the screw
    region), subtracts a touched horizontal rail lip while preserving darker
    digit ink, stops before a solid DIN rail body, and isolates the ink cluster
    closest to the block edge so distant strip titles are excluded.
    """

    inset = max(1, int(round(0.06 * float(block["w"]))))
    x0 = max(0, int(block["x"]) + inset)
    x1 = min(gray.shape[1], int(block["x"] + block["w"]) - inset)
    if x1 - x0 < 4:
        return None
    max_span = max(10, int(round(13.0 * ppm)))
    edge_slack = max(2, int(round(0.6 * ppm)))
    if side == "top":
        edge = int(block["y"])
        y0 = max(0, edge - max_span)
        y1 = min(gray.shape[0], edge + edge_slack)
        if y1 - y0 < 4:
            return None
        band = gray[y0:y1, x0:x1]
        # Reverse vertically so row 0 is closest to the block edge, walking outward.
        outward = _normalize_contrast(band, paper, dark)[::-1, :].copy()
    else:
        edge = int(block["y"] + block["h"])
        y0 = max(0, edge - edge_slack)
        y1 = min(gray.shape[0], edge + max_span)
        if y1 - y0 < 4:
            return None
        band = gray[y0:y1, x0:x1]
        outward = _normalize_contrast(band, paper, dark).copy()

    h = outward.shape[0]
    # Skip past the block's own horizontal border stroke.
    start = 0
    border_limit = min(h, edge_slack * 2 + 2)
    for r in range(border_limit):
        if float((outward[r] < 180).mean()) >= 0.60:
            start = r + 1
    outward = outward[start:, :]
    h = outward.shape[0]
    if h < 4:
        return None

    cleaned = outward.copy()
    stop_row = h
    seen_ink = False
    r = 0
    while r < h:
        row = cleaned[r]
        med = float(np.median(row))
        dark_frac = float((row < 190).mean())
        if dark_frac >= 0.65:
            cleaned[r] = _clean_rule_row(row)
            if seen_ink:
                look_end = min(h, r + max(4, int(round(1.5 * ppm))))
                ahead = cleaned[r + 1 : look_end]
                if ahead.size > 0 and int((np.median(ahead, axis=1) < 230).sum()) >= 2:
                    end_lip = r + 1
                    while end_lip < h and float((cleaned[end_lip] < 190).mean()) >= 0.65:
                        cleaned[end_lip] = _clean_rule_row(cleaned[end_lip])
                        end_lip += 1
                    stop_row = end_lip
                    break
            r += 1
            continue
        if med < 225.0 and r + 1 < h and float(np.median(cleaned[r + 1])) < 225.0:
            stop_row = r
            break
        if int((row < 170).sum()) >= 2:
            seen_ink = True
        r += 1

    cleaned = _despec_single_dots(cleaned[:stop_row, :])
    if cleaned.shape[0] < 4:
        return None

    # Isolate the ink cluster closest to the block edge (excludes distant titles).
    row_ink = (cleaned < 185).sum(axis=1) >= 2
    ink_rows = np.where(row_ink)[0]
    if ink_rows.size == 0:
        return None
    first = int(ink_rows[0])
    if first > int(round(5.5 * ppm)):
        return None
    gap_limit = max(6, int(round(2.0 * ppm)))
    last = first
    for idx in ink_rows[1:]:
        if int(idx) - last > gap_limit:
            break
        last = int(idx)

    pad = max(3, int(round(0.5 * ppm)))
    r0 = max(0, first - pad)
    r1 = min(cleaned.shape[0], last + pad + 1)
    tight = cleaned[r0:r1, :]
    if side == "top":
        tight = tight[::-1, :]
    col_ink = (tight < 185).sum(axis=0) >= 1
    ink_cols = np.where(col_ink)[0]
    if ink_cols.size == 0:
        return None
    c0 = max(0, int(ink_cols[0]) - pad)
    c1 = min(tight.shape[1], int(ink_cols[-1]) + pad + 1)
    tight = tight[:, c0:c1]
    if tight.shape[0] < 3 or tight.shape[1] < 3:
        return None
    return tight


def _number_in(
    gray: np.ndarray,
    block: dict[str, Any],
    ppm: float,
    side: str,
    *,
    paper: float | None = None,
    dark: float | None = None,
) -> str:
    """The wire number above the block or below it. ``?`` when it is not a number.

    The digits are often turned along the block. Orientation is chosen from the
    tight ink box; two unrelated numbers stay unreadable, never a guess.
    """

    if paper is None or dark is None:
        paper, dark, _thr = _ink_levels(gray)
    crop = _number_crop(gray, block, ppm, side, paper, dark)
    if crop is None:
        return "?"

    def _nums(image: np.ndarray) -> list[str]:
        heard = _ocr_crop(image, digits=True)
        return _NUMBER.findall(heard)

    h, w = crop.shape
    if h > w * 1.15:
        clockwise = _nums(cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE))
        if len(clockwise) == 1:
            return clockwise[0]
        if len(clockwise) > 1:
            return "?"
        counter = _nums(cv2.rotate(crop, cv2.ROTATE_90_COUNTERCLOCKWISE))
        if len(counter) == 1:
            return counter[0]
        return "?"
    if w > h * 1.15:
        upright = _nums(crop)
        if len(upright) == 1:
            return upright[0]
        return "?"
    clockwise = _nums(cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE))
    upright = _nums(crop)
    found = list(dict.fromkeys([*clockwise, *upright]))
    if len(found) == 1:
        return found[0]
    return "?"


def _without_rules(image: np.ndarray) -> np.ndarray:
    """Paint out a ruled line that cuts through the digits."""

    if image.size == 0:
        return image
    ink = (image < 160).astype(np.uint8) * 255
    kernel = max(5, image.shape[1] // 3)
    lines = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (kernel, 1)))
    out = image.copy()
    out[lines > 0] = 255
    return out


def _crop(gray: np.ndarray, block: dict[str, Any], y0_frac: float, y1_frac: float) -> np.ndarray | None:
    x0 = int(block["x"]) + 2
    x1 = int(block["x"] + block["w"]) - 2
    top = int(block["y"] + block["h"] * y0_frac)
    bot = int(block["y"] + block["h"] * y1_frac)
    x0, top = max(0, x0), max(0, top)
    x1, bot = min(gray.shape[1], x1), min(gray.shape[0], bot)
    if x1 - x0 < 4 or bot - top < 4:
        return None
    return gray[top:bot, x0:x1]


def _ocr_crop(image: np.ndarray, *, digits: bool = False) -> str:
    """Read one tight strip crop with the active OCR engine.

    Uses the bundled PP-OCR detail model when the electrical ONNX backend is
    active and weights are present, otherwise delegates to :func:`_ocr`.
    """

    if image.size == 0:
        return ""
    from panelcad.ocr.backend import page_engine_ready, tesseract_required

    if not tesseract_required() and page_engine_ready():
        from panelcad.ocr.ppocr import read_crop

        spans = read_crop(image)
        if spans is not None:
            text = " ".join(span.text for span in spans if span.text)
            if text:
                return text
    return _ocr(image, digits=digits)


def _ocr(image: np.ndarray, *, digits: bool = False) -> str:
    if image.size == 0:
        return ""
    from panelcad.ocr.dispatch import image_to_string, tesseract_required

    if tesseract_required():
        try:
            from panelcad.importer.raster import configure_tesseract
        except Exception:
            return ""
        if not configure_tesseract():
            return ""
    short = min(int(image.shape[0]), int(image.shape[1]))
    scale = 4 if short >= 20 else min(6, max(4, int(round(80.0 / max(short, 1)))))
    up = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    # Keep character height well below 45% of the padded height so rule
    # suppression never treats the upright stem of "1" as a vertical line.
    pad_y = max(24, up.shape[0] // 2)
    up = cv2.copyMakeBorder(up, pad_y, pad_y, 24, 24, cv2.BORDER_CONSTANT, value=255)
    whitelist = "0123456789" if digits else "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
    try:
        return str(
            image_to_string(
                up,
                config=f"--psm 7 -c tessedit_char_whitelist={whitelist}",
            )
            or ""
        )
    except Exception:
        return ""
