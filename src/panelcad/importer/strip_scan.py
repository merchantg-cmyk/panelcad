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
    devices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, block in enumerate(blocks):
        tag = _tag_in(gray, block, ppm)
        if tag == "?":
            # An unreadable block is not given a made-up name.
            continue
        if tag in seen:
            continue
        seen.add(tag)
        top = _number_in(gray, block, ppm, "top")
        bottom = _number_in(gray, block, ppm, "bottom")
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


def find_block_row(gray: np.ndarray, rgb: np.ndarray, ppm: float) -> list[dict[str, Any]]:
    """One row of similar narrow blocks. Coordinates are millimetres, top-left."""

    if ppm <= 0 or gray.size == 0:
        return []
    ink = (gray < 90).astype(np.uint8)
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
        blocks = _blocks_from_strokes(row, gray, rgb, ppm)
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
        ground = _is_ground(gray, rgb, x, y, width, height)
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


def _is_ground(gray: np.ndarray, rgb: np.ndarray, x: int, y: int, w: int, h: int) -> bool:
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
        if float(((green > red + 18) & (green > blue + 8) & (green > 70)).mean()) > 0.12:
            return True
    ink = gray[y0:y1, x0:x1] < 100
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


def _tag_in(gray: np.ndarray, block: dict[str, Any], ppm: float) -> str:
    crop = _crop(gray, block, 0.28, 0.72)
    if crop is None:
        return "?"
    heard = _ocr(crop) + " " + _ocr(cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE))
    tags = _TAG.findall(heard.upper())
    hyphen = [tag for tag in tags if "-" in tag]
    if hyphen:
        return hyphen[0]
    if tags:
        return tags[0]
    return "?"


def _number_in(gray: np.ndarray, block: dict[str, Any], ppm: float, side: str) -> str:
    """The wire number above the block or below it. ``?`` when it is not a number.

    The digits are often turned along the block. Both orientations are read.
    Two different numbers is unreadable, not a guess.
    """

    span = max(8, int(round(11.0 * ppm)))
    # A narrow window so the next cell's number is not read as this one.
    inset = max(1, int(round(0.08 * float(block["w"]))))
    x0 = int(block["x"]) + inset
    x1 = int(block["x"] + block["w"]) - inset
    overlap = max(2, int(round(0.22 * float(block["h"]))))
    if side == "top":
        y1 = int(block["y"]) + overlap
        y0 = int(block["y"]) - span
    else:
        y0 = int(block["y"] + block["h"]) - overlap
        y1 = int(block["y"] + block["h"]) + span
    y0 = max(0, y0)
    y1 = min(gray.shape[0], y1)
    x0 = max(0, x0)
    x1 = min(gray.shape[1], x1)
    if y1 - y0 < 4 or x1 - x0 < 4:
        return "?"
    crop = _without_rules(gray[y0:y1, x0:x1])

    def _threes(image: np.ndarray) -> list[str]:
        heard = _ocr(image, digits=True)
        return re.findall(r"\d{3}", heard)

    upright = _threes(crop)
    clockwise = _threes(cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE))
    counter = _threes(cv2.rotate(crop, cv2.ROTATE_90_COUNTERCLOCKWISE))
    upside = _threes(cv2.rotate(crop, cv2.ROTATE_180))
    found = list(dict.fromkeys([*upright, *clockwise, *counter, *upside]))
    if len(found) == 1:
        return found[0]
    # The two quarter-turns read the same glyphs in opposite directions.
    # Keep the clockwise one. Two unrelated numbers stay unread.
    if len(clockwise) == 1 and len(counter) == 1 and clockwise[0] == counter[0][::-1]:
        return clockwise[0]
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
    up = cv2.resize(image, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    up = cv2.copyMakeBorder(up, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=255)
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
