"""Local OCR for shop electrical drawings.

Tesseract is a general page reader. A schematic is a different page: short
device tags, wire numbers, terminal and cable ids, and a title block. The
glyphs are small, sometimes turned a quarter turn, and often crossed by a
wire or sitting on a blue sheet.

This engine stays on the PC. It suppresses long rules, cuts the remaining ink
into glyphs, and matches each glyph to a prototype bank. The bank is built
from the DejaVu fonts already in this repo, from OpenCV's built-in stroke
fonts, and from the usual Windows UI fonts when those files are on the
machine. Nothing is downloaded and no image leaves the process.

It is not a claim to beat Tesseract or Paddle. ``scripts/bench_electrical_ocr.py``
measures that. The importer still decides which tokens are tags and wire
numbers; this module only returns words and boxes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from panelcad.ocr.types import OcrSpan

_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-:/."
_CANVAS_W = 16
_CANVAS_H = 24
_MIN_NCC = 0.40
# A gap wider than this, relative to the glyph height, starts a new word.
_WORD_GAP = 0.42
_SOURCE = "electrical"
# A wire number is digits. These glyphs are the usual misreads of a digit, and
# only get rewritten when the rest of the word is already numeric. 10M stays
# 10M. 1O4 becomes 104.
_DIGIT_LOOKALIKE = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6"})

_Bank = tuple[np.ndarray, np.ndarray, list[str]]
_BANK: _Bank | None = None


@dataclass
class _Glyph:
    label: str
    confidence: float
    x: int
    y: int
    w: int
    h: int

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0


class ElectricalOcr:
    """Glyph matcher for tags, wire numbers, and title-block words."""

    name = _SOURCE

    def available(self) -> bool:
        return True

    def recognize(
        self,
        image: np.ndarray | Image.Image,
        *,
        psm: int = 6,
        whitelist: str | None = None,
        rotations: tuple[int, ...] = (0,),
    ) -> list[OcrSpan]:
        arr = _as_array(image)
        if arr.size == 0 or min(arr.shape[:2]) < 6:
            return []
        allowed = _allowed(whitelist)
        mode = _mode(psm)
        found: list[OcrSpan] = []
        for rotation in rotations:
            if rotation not in {0, 90, 180, 270}:
                continue
            turned, applied = _upright_view(arr, rotation)
            spans = _read_upright(turned, mode=mode, allowed=allowed, rotation=rotation)
            found.extend(_unmap_spans(spans, applied, arr.shape[1], arr.shape[0]))
        return _suppress_overlaps(found)


def _suppress_overlaps(spans: list[OcrSpan]) -> list[OcrSpan]:
    """Keep an upright word. A turned read fills ink the upright pass missed.

    A single turned glyph can score as high as a real word and sit on the
    same pixels. That must not replace SHEET or OF. A turned word replaces
    an upright fragment only when the upright read was already weak.
    """

    upright = [span for span in spans if span.rotation == 0 and span.confidence >= 0.62]
    kept = list(upright)
    rest = [span for span in spans if span not in upright]
    for span in sorted(rest, key=lambda item: (item.confidence * max(len(item.text), 1)), reverse=True):
        if any(_iou(span, other) > 0.3 for other in kept):
            continue
        kept.append(span)
    return kept


def _iou(left: OcrSpan, right: OcrSpan) -> float:
    x0 = max(left.left, right.left)
    y0 = max(left.top, right.top)
    x1 = min(left.left + left.width, right.left + right.width)
    y1 = min(left.top + left.height, right.top + right.height)
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    if inter <= 0:
        return 0.0
    area = left.width * left.height + right.width * right.height - inter
    if area <= 0:
        return 0.0
    return inter / area


def _mode(psm: int) -> str:
    if psm in {8, 10}:
        return "word"
    if psm in {7, 13}:
        return "line"
    return "page"


def _allowed(whitelist: str | None) -> set[str] | None:
    if not whitelist:
        return None
    return {ch.upper() for ch in whitelist if not ch.isspace()}


def _as_array(image: np.ndarray | Image.Image) -> np.ndarray:
    if isinstance(image, Image.Image):
        return np.asarray(image.convert("RGB"))
    return np.asarray(image)


def _upright_view(image: np.ndarray, text_rotation: int) -> tuple[np.ndarray, int]:
    """Rotate so clockwise-turned text stands upright.

    The returned angle is the clockwise rotation applied to the image. Boxes
    read on this view are mapped back by the inverse.
    """

    applied = (360 - int(text_rotation)) % 360
    return _rotate_cw(image, applied), applied


def _rotate_cw(image: np.ndarray, degrees: int) -> np.ndarray:
    if degrees == 90:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if degrees == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    if degrees == 270:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return image


def _unmap_point(x: int, y: int, applied_cw: int, width: int, height: int) -> tuple[int, int]:
    """Inverse of ``_rotate_cw`` for one pixel. ``applied_cw`` is the view rotation."""

    if applied_cw == 90:
        # CW 90: new_x = height - 1 - old_y, new_y = old_x.
        return y, height - 1 - x
    if applied_cw == 180:
        return width - 1 - x, height - 1 - y
    if applied_cw == 270:
        # CW 270: new_x = old_y, new_y = width - 1 - old_x.
        # old_x = width - 1 - new_y, old_y = new_x.
        return width - 1 - y, x
    return x, y


def _unmap_spans(spans: list[OcrSpan], applied_cw: int, width: int, height: int) -> list[OcrSpan]:
    if applied_cw == 0:
        return spans
    mapped: list[OcrSpan] = []
    for span in spans:
        corners = (
            (span.left, span.top),
            (span.left + span.width - 1, span.top),
            (span.left, span.top + span.height - 1),
            (span.left + max(span.width - 1, 0), span.top + max(span.height - 1, 0)),
        )
        points = [_unmap_point(x, y, applied_cw, width, height) for x, y in corners]
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        left = max(0, min(xs))
        top = max(0, min(ys))
        right = min(width - 1, max(xs))
        bottom = min(height - 1, max(ys))
        mapped.append(
            OcrSpan(
                text=span.text,
                left=int(left),
                top=int(top),
                width=int(max(1, right - left + 1)),
                height=int(max(1, bottom - top + 1)),
                confidence=span.confidence,
                rotation=span.rotation,
                source=span.source,
            )
        )
    return mapped


def _read_upright(
    image: np.ndarray,
    *,
    mode: str,
    allowed: set[str] | None,
    rotation: int,
) -> list[OcrSpan]:
    gray = _gray(image)
    if gray.size == 0 or float(gray.std()) < 3.0:
        return []
    ink = _ink(gray)
    # A tag on a sheet is a few hundred dark pixels, not a percentage of the page.
    if int((ink > 0).sum()) < 25 or float(ink.mean()) / 255.0 > 0.72:
        return []
    ink = _suppress_rules(ink)
    boxes = _glyph_boxes(ink, mode)
    if not boxes:
        return []
    glyphs: list[_Glyph] = []
    for x, y, w, h in boxes:
        crop = ink[y : y + h, x : x + w]
        label, confidence = _classify(_feature(crop))
        if allowed is not None and label not in allowed:
            continue
        if confidence < _MIN_NCC:
            continue
        glyphs.append(_Glyph(label, confidence, x, y, w, h))
    if not glyphs:
        return []
    if mode == "word":
        groups = [sorted(glyphs, key=lambda item: item.x)]
        return _spans(groups, rotation, spaces=False)
    if mode == "line":
        ordered = sorted(glyphs, key=lambda item: item.x)
        return _spans(_split_words(ordered), rotation, spaces=False)
    lines = _cluster_lines(glyphs)
    spans: list[OcrSpan] = []
    for line in lines:
        ordered = sorted(line, key=lambda item: item.x)
        spans.extend(_spans(_split_words(ordered), rotation, spaces=False))
    return spans


def _gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        gray = image
    elif image.ndim == 3:
        channels = image.shape[2]
        if channels == 4:
            image = image[:, :, :3]
        # Callers pass RGB (PIL) or a single gray plane. OpenCV samples in this
        # package are RGB as well.
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    else:
        return np.zeros((1, 1), np.uint8)
    if gray.dtype != np.uint8:
        gray = np.clip(gray, 0, 255).astype(np.uint8)
    # A blue sheet or a dark photo has light ink. Flip so ink is dark.
    if float(np.median(gray)) < 115.0:
        gray = 255 - gray
    low = float(np.percentile(gray, 2))
    high = float(np.percentile(gray, 98))
    if high > low + 18.0:
        stretched = (gray.astype(np.float32) - low) * (255.0 / (high - low))
        gray = np.clip(stretched, 0, 255).astype(np.uint8)
    return gray


def _ink(gray: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _thr, binary = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return binary


def _suppress_rules(ink: np.ndarray) -> np.ndarray:
    """Drop long straight rules. A wire through a digit is one of those rules."""

    height, width = ink.shape
    rules = np.zeros_like(ink)
    # A rule runs a long way across the crop. A glyph stem does not. The
    # importer enlarges a tag before this runs, so a short kernel would delete
    # the upright of a "1".
    if width >= 28:
        kernel = max(22, int(width * 0.45))
        horizontal = cv2.morphologyEx(
            ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (kernel, 1))
        )
        rules = cv2.bitwise_or(rules, horizontal)
    if height >= 40:
        kernel = max(28, int(height * 0.42))
        vertical = cv2.morphologyEx(
            ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, kernel))
        )
        rules = cv2.bitwise_or(rules, vertical)
    cleaned = cv2.subtract(ink, rules)
    if int(rules.max()) == 0:
        return cleaned
    # Join a glyph only where a rule cut it. Other letters keep their original gap.
    closed = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5)))
    bridge = cv2.dilate(rules, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5)))
    return cv2.bitwise_or(cleaned, cv2.bitwise_and(closed, bridge))


def _glyph_boxes(ink: np.ndarray, mode: str) -> list[tuple[int, int, int, int]]:
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(ink, 8)
    if count > 4000:
        return []
    height, width = ink.shape
    raw: list[tuple[int, int, int, int]] = []
    for index in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[index])
        if area < 8 or h < 4 or w < 1:
            continue
        if mode == "page":
            # A full sheet uses a low cap so a symbol box is not a glyph. A
            # tight crop is mostly the word, so the same cap would drop it.
            if height < 200:
                max_h = height * 0.92
            else:
                max_h = max(64.0, height * 0.18)
            if h < 8 or h > max_h or w > width * 0.5:
                continue
            if h <= 5 and w > 24:
                continue
            if w <= 5 and h > 24:
                continue
        elif h > height * 0.96 and w > width * 0.96:
            continue
        raw.append((x, y, w, h))
    raw = _merge_stacked_dots(raw)
    return _split_wide(ink, raw)


def _merge_stacked_dots(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """A colon is two dots. Stack them into one glyph before classification."""

    if len(boxes) < 2:
        return boxes
    heights = sorted(box[3] for box in boxes)
    typical = heights[len(heights) // 2]
    used = [False] * len(boxes)
    merged: list[tuple[int, int, int, int]] = []
    order = sorted(range(len(boxes)), key=lambda index: (boxes[index][0], boxes[index][1]))
    for index in order:
        if used[index]:
            continue
        x, y, w, h = boxes[index]
        partner = None
        for other in order:
            if other == index or used[other]:
                continue
            ox, oy, ow, oh = boxes[other]
            if abs((x + w / 2) - (ox + ow / 2)) > max(w, ow):
                continue
            gap = max(y, oy) - min(y + h, oy + oh)
            if -2 <= gap <= typical * 0.8 and h < typical * 0.7 and oh < typical * 0.7:
                partner = other
                break
        if partner is None:
            merged.append((x, y, w, h))
            used[index] = True
            continue
        ox, oy, ow, oh = boxes[partner]
        x0 = min(x, ox)
        y0 = min(y, oy)
        x1 = max(x + w, ox + ow)
        y1 = max(y + h, oy + oh)
        merged.append((x0, y0, x1 - x0, y1 - y0))
        used[index] = True
        used[partner] = True
    return merged


def _split_wide(ink: np.ndarray, boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Split a blob that is wide enough to be two touching glyphs."""

    pieces: list[tuple[int, int, int, int]] = []
    for x, y, w, h in boxes:
        if w < h * 1.25 or w < 12:
            pieces.append((x, y, w, h))
            continue
        column = ink[y : y + h, x : x + w].sum(axis=0).astype(np.float32)
        if column.size < 8:
            pieces.append((x, y, w, h))
            continue
        peak = float(np.percentile(column, 80))
        if peak <= 0:
            continue
        valley = column < peak * 0.18
        cuts = _valley_spans(valley)
        # A cut near either end is the side bearing, not a letter boundary.
        inner = [span for span in cuts if span[0] > w * 0.18 and span[1] < w * 0.82]
        if not inner:
            pieces.append((x, y, w, h))
            continue
        edges = [0]
        for start, stop in inner:
            edges.append((start + stop) // 2)
        edges.append(w)
        for left, right in zip(edges, edges[1:]):
            if right - left < 2:
                continue
            piece = ink[y : y + h, x + left : x + right]
            ys, xs = np.where(piece > 0)
            if len(xs) == 0:
                continue
            pieces.append(
                (
                    x + left + int(xs.min()),
                    y + int(ys.min()),
                    int(xs.max() - xs.min() + 1),
                    int(ys.max() - ys.min() + 1),
                )
            )
    return pieces


def _valley_spans(valley: np.ndarray) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, on in enumerate(valley.tolist()):
        if on and start is None:
            start = index
        elif not on and start is not None:
            if index - start >= 1:
                spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(valley)))
    return spans


def _cluster_lines(glyphs: list[_Glyph]) -> list[list[_Glyph]]:
    lines: list[list[_Glyph]] = []
    centers: list[float] = []
    heights: list[float] = []
    for glyph in sorted(glyphs, key=lambda item: (item.cy, item.x)):
        placed = False
        for index, line in enumerate(lines):
            if abs(glyph.cy - centers[index]) <= 0.55 * max(glyph.h, heights[index]):
                line.append(glyph)
                centers[index] = sum(item.cy for item in line) / len(line)
                heights[index] = sum(item.h for item in line) / len(line)
                placed = True
                break
        if not placed:
            lines.append([glyph])
            centers.append(glyph.cy)
            heights.append(float(glyph.h))
    return lines


def _split_words(glyphs: list[_Glyph]) -> list[list[_Glyph]]:
    if not glyphs:
        return []
    groups = [[glyphs[0]]]
    for previous, glyph in zip(glyphs, glyphs[1:]):
        gap = glyph.x - (previous.x + previous.w)
        if gap > _WORD_GAP * max(previous.h, glyph.h, 1):
            groups.append([glyph])
        else:
            groups[-1].append(glyph)
    return groups


def _spans(groups: list[list[_Glyph]], rotation: int, *, spaces: bool) -> list[OcrSpan]:
    spans: list[OcrSpan] = []
    for group in groups:
        if not group:
            continue
        text = _wire_digits("".join(glyph.label for glyph in group))
        if spaces:
            text = " ".join(glyph.label for glyph in group)
        if not text:
            continue
        confidence = sum(glyph.confidence for glyph in group) / len(group)
        if confidence < _MIN_NCC:
            continue
        left = min(glyph.x for glyph in group)
        top = min(glyph.y for glyph in group)
        right = max(glyph.x + glyph.w for glyph in group)
        bottom = max(glyph.y + glyph.h for glyph in group)
        spans.append(
            OcrSpan(
                text=text,
                left=int(left),
                top=int(top),
                width=int(max(1, right - left)),
                height=int(max(1, bottom - top)),
                confidence=round(float(confidence), 4),
                rotation=rotation,
                source=_SOURCE,
            )
        )
    return spans


def _wire_digits(text: str) -> str:
    if len(text) < 2:
        return text
    if any(char.isdigit() for char in text) and all(char.isdigit() or char in "OILSBZG" for char in text):
        return text.translate(_DIGIT_LOOKALIKE)
    return text


def _feature(crop: np.ndarray) -> np.ndarray:
    binary = (crop > 0).astype(np.uint8)
    return _fit(binary).reshape(-1)


def _fit(binary: np.ndarray) -> np.ndarray:
    ys, xs = np.where(binary > 0)
    canvas = np.zeros((_CANVAS_H, _CANVAS_W), np.float32)
    if len(xs) == 0:
        return canvas
    crop = binary[int(ys.min()) : int(ys.max()) + 1, int(xs.min()) : int(xs.max()) + 1]
    height, width = crop.shape
    if height < 1 or width < 1:
        return canvas
    scale = min(_CANVAS_W / width, _CANVAS_H / height)
    resized_w = max(1, int(round(width * scale)))
    resized_h = max(1, int(round(height * scale)))
    resized = cv2.resize(crop, (resized_w, resized_h), interpolation=cv2.INTER_AREA)
    resized = (resized > 0.3).astype(np.float32)
    y0 = (_CANVAS_H - resized_h) // 2
    x0 = (_CANVAS_W - resized_w) // 2
    canvas[y0 : y0 + resized_h, x0 : x0 + resized_w] = resized
    return canvas


def _classify(feature: np.ndarray) -> tuple[str, float]:
    centered, norms, labels = _bank()
    if centered.size == 0:
        return "", 0.0
    sample = feature.astype(np.float32)
    sample = sample - float(sample.mean())
    norm = float(np.linalg.norm(sample)) + 1e-6
    sims = centered @ sample / (norms * norm)
    best = int(np.argmax(sims))
    score = float(sims[best])
    return labels[best], max(0.0, min(1.0, score))


def _bank() -> _Bank:
    global _BANK
    if _BANK is None:
        _BANK = _build_bank()
    return _BANK


def _build_bank() -> _Bank:
    features: list[np.ndarray] = []
    labels: list[str] = []
    for label, crop in _prototypes():
        feat = _feature(crop)
        if float(feat.sum()) < 2:
            continue
        features.append(feat)
        labels.append(label)
    if not features:
        empty = np.zeros((0, _CANVAS_W * _CANVAS_H), np.float32)
        return empty, empty, []
    matrix = np.stack(features).astype(np.float32)
    centered = matrix - matrix.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(centered, axis=1) + 1e-6
    return centered, norms.astype(np.float32), labels


def _prototypes() -> list[tuple[str, np.ndarray]]:
    found: list[tuple[str, np.ndarray]] = []
    for path in _font_files():
        for size in (32, 48):
            try:
                font = ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
            for char in _CHARSET:
                crop = _render_pil(char, font)
                if crop is not None:
                    found.append((char, crop))
                    thick = cv2.dilate(crop, np.ones((2, 2), np.uint8))
                    found.append((char, thick))
    for font, scale, thickness in (
        (cv2.FONT_HERSHEY_SIMPLEX, 1.5, 2),
        (cv2.FONT_HERSHEY_DUPLEX, 1.3, 2),
        (cv2.FONT_HERSHEY_COMPLEX, 1.15, 1),
        (cv2.FONT_HERSHEY_SIMPLEX, 1.1, 1),
    ):
        for char in _CHARSET:
            crop = _render_hershey(char, font, scale, thickness)
            if crop is not None:
                found.append((char, crop))
    return found


def _render_pil(char: str, font: ImageFont.ImageFont) -> np.ndarray | None:
    canvas = Image.new("L", (180, 180), 255)
    ImageDraw.Draw(canvas).text((16, 16), char, fill=0, font=font)
    ink = 255 - np.asarray(canvas)
    return _bbox(ink)


def _render_hershey(char: str, font: int, scale: float, thickness: int) -> np.ndarray | None:
    canvas = np.full((140, 140), 255, np.uint8)
    cv2.putText(canvas, char, (12, 100), font, scale, 0, thickness, cv2.LINE_AA)
    return _bbox((canvas < 210).astype(np.uint8) * 255)


def _bbox(ink: np.ndarray) -> np.ndarray | None:
    ys, xs = np.where(ink > 0)
    if len(xs) == 0:
        return None
    return ink[int(ys.min()) : int(ys.max()) + 1, int(xs.min()) : int(xs.max()) + 1]


def _font_files() -> list[Path]:
    """DejaVu from the repo, then the Windows fonts a shop PC actually has.

    Liberation and other Linux families are left out on purpose. The benchmark
    can draw with a face that is not in the bank and show that gap.
    """

    here = Path(__file__).resolve()
    roots = [
        here.parents[3] / "tests" / "fonts",
        Path.cwd() / "tests" / "fonts",
    ]
    names = ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
    found: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        for name in names:
            path = root / name
            key = str(path)
            if path.is_file() and key not in seen:
                found.append(path)
                seen.add(key)
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for name in ("arial.ttf", "arialbd.ttf", "consola.ttf", "calibri.ttf", "segoeui.ttf", "cour.ttf"):
        path = Path(windir) / "Fonts" / name
        key = str(path)
        if path.is_file() and key not in seen:
            found.append(path)
            seen.add(key)
    return found


def clear_prototype_bank() -> None:
    """Drop the cached prototypes. Tests use this when they need a fresh bank."""

    global _BANK
    _BANK = None
