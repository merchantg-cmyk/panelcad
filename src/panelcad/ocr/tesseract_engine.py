"""Tesseract as the same word-box interface as the electrical engine.

``find_tesseract`` looks at the runtime shipped with the install, then on
PATH. A missing binary makes ``available`` false. Word boxes from
``image_to_data`` do not carry a per-word angle, so ``rotation`` is 0. Page
turning still happens in ``imageutil`` before this reader is called.
"""

from __future__ import annotations

import re

import numpy as np
from PIL import Image

from panelcad.ocr.types import OcrSpan


class TesseractOcr:
    name = "tesseract"

    def available(self) -> bool:
        from panelcad.importer.raster import find_tesseract

        return bool(find_tesseract())

    def recognize(
        self,
        image: np.ndarray | Image.Image,
        *,
        psm: int = 6,
        whitelist: str | None = None,
        rotations: tuple[int, ...] = (0,),
    ) -> list[OcrSpan]:
        from panelcad.importer.raster import configure_tesseract

        if not configure_tesseract():
            return []
        import pytesseract

        config = f"--psm {psm}"
        if whitelist:
            config += f" -c tessedit_char_whitelist={whitelist}"
        spans: list[OcrSpan] = []
        # Rotations other than 0 are searched by turning the image, the same
        # way a caller would. The binary itself still reports no angle.
        for rotation in rotations:
            if rotation not in {0, 90, 180, 270}:
                continue
            view = _rotate_pil(image, rotation)
            try:
                data = pytesseract.image_to_data(
                    view, output_type=pytesseract.Output.DICT, config=config
                )
            except Exception as exc:  # noqa: BLE001 — the caller treats a miss as no words
                from panelcad.importer.raster import TesseractMissing, _reraise_tesseract

                if isinstance(exc, TesseractMissing):
                    return []
                try:
                    _reraise_tesseract(exc)
                except TesseractMissing:
                    return []
                raise
            spans.extend(_spans_from_data(data, rotation, _view_shape(view), _shape(image)))
        return spans


def _shape(image: np.ndarray | Image.Image) -> tuple[int, int]:
    if isinstance(image, Image.Image):
        return image.size
    return int(image.shape[1]), int(image.shape[0])


def _view_shape(image: np.ndarray | Image.Image) -> tuple[int, int]:
    return _shape(image)


def _rotate_pil(image: np.ndarray | Image.Image, rotation: int) -> np.ndarray | Image.Image:
    if rotation == 0:
        return image
    import cv2

    if isinstance(image, Image.Image):
        arr = np.asarray(image.convert("RGB"))
        turned = _rotate_arr(arr, rotation, cv2)
        return Image.fromarray(turned)
    return _rotate_arr(np.asarray(image), rotation, cv2)


def _rotate_arr(image: np.ndarray, text_rotation: int, cv2: object) -> np.ndarray:
    # Undo a clockwise text rotation so the binary sees upright glyphs.
    applied = (360 - text_rotation) % 360
    if applied == 90:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if applied == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    if applied == 270:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return image


def _spans_from_data(
    data: dict,
    rotation: int,
    view_size: tuple[int, int],
    original_size: tuple[int, int],
) -> list[OcrSpan]:
    spans: list[OcrSpan] = []
    texts = data.get("text") or []
    confs = data.get("conf") or []
    lefts = data.get("left") or []
    tops = data.get("top") or []
    widths = data.get("width") or []
    heights = data.get("height") or []
    view_w, view_h = view_size
    orig_w, orig_h = original_size
    applied = (360 - rotation) % 360
    for raw, conf, left, top, width, height in zip(texts, confs, lefts, tops, widths, heights):
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            score = float(conf)
            box = (int(left), int(top), int(width), int(height))
        except (TypeError, ValueError):
            continue
        if score < 0 or box[2] <= 0 or box[3] <= 0:
            continue
        left_i, top_i, width_i, height_i = _unmap_box(box, applied, view_w, view_h, orig_w, orig_h)
        spans.append(
            OcrSpan(
                text=text,
                left=left_i,
                top=top_i,
                width=width_i,
                height=height_i,
                confidence=max(0.0, min(1.0, score / 100.0)),
                rotation=rotation,
                source="tesseract",
            )
        )
    return spans


def _unmap_box(
    box: tuple[int, int, int, int],
    applied_cw: int,
    view_w: int,
    view_h: int,
    orig_w: int,
    orig_h: int,
) -> tuple[int, int, int, int]:
    left, top, width, height = box
    if applied_cw == 0:
        return left, top, width, height
    # The view was produced by rotating the original clockwise by ``applied_cw``.
    # Invert that to land the box on the original image. ``view_*`` is unused
    # once the original size is known; the inverse depends on the original.
    del view_w, view_h
    corners = (
        (left, top),
        (left + width - 1, top),
        (left, top + height - 1),
        (left + width - 1, top + height - 1),
    )
    points = [_unmap_point(x, y, applied_cw, orig_w, orig_h) for x, y in corners]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    x0 = max(0, min(xs))
    y0 = max(0, min(ys))
    x1 = min(orig_w - 1, max(xs))
    y1 = min(orig_h - 1, max(ys))
    return int(x0), int(y0), int(max(1, x1 - x0 + 1)), int(max(1, y1 - y0 + 1))


def _unmap_point(x: int, y: int, applied_cw: int, width: int, height: int) -> tuple[int, int]:
    if applied_cw == 90:
        return y, height - 1 - x
    if applied_cw == 180:
        return width - 1 - x, height - 1 - y
    if applied_cw == 270:
        return width - 1 - y, x
    return x, y


_PSM_RE = re.compile(r"--psm\s+(\d+)")
_WHITE_RE = re.compile(r"tessedit_char_whitelist=(\S+)")


def parse_tesseract_config(config: str) -> tuple[int, str | None]:
    """Pull ``--psm`` and the character whitelist out of a Tesseract config string."""

    psm = 3
    match = _PSM_RE.search(config or "")
    if match:
        psm = int(match.group(1))
    white = _WHITE_RE.search(config or "")
    return psm, white.group(1) if white else None
