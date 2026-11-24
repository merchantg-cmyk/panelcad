"""Full-print OCR from PaddleOCR, without the Paddle runtime.

Stock PaddleOCR on this machine loads PP-OCRv6 medium and the PaddlePaddle
wheel, with oneDNN turned off. A shop drawing does not need that stack.
This module asks PaddleOCR for two smaller English models from the same
family and runs them with ONNX Runtime:

* PP-OCRv5 mobile reads the page. On a schematic that is enough.
* PP-OCRv6 small reads enlarged crops where the glyphs are only a few
  pixels tall (a terminal strip, a row of panel nameplates). The page pass
  shrinks those until the digits disappear.

No image is sent anywhere. The weights are the published ONNX exports
(Apache-2.0, Copyright PaddlePaddle Authors). PanelCAD loads them from the
app-local directory shipped with the install. This module does not download
them and does not read ``~/.paddlex``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import cv2
import numpy as np

from panelcad.ocr.types import OcrSpan

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("ORT_LOG_SEVERITY_LEVEL", "3")

log = logging.getLogger("panelcad.ocr.ppocr")

_SOURCE = "electrical"
_PAGE: Any = None
_DETAIL: Any = None
_LAST_ERROR: str = ""
_MAX_FINE_CROPS = 2


class OcrRuntimeError(RuntimeError):
    """Raised when the bundled electrical ONNX OCR engine fails to load or run."""


def last_error() -> str:
    """Most recent PP-OCR load or inference error, or empty string when clean."""

    return _LAST_ERROR


def clear_last_error() -> None:
    """Reset the recorded PP-OCR load or inference error before a new read."""

    global _LAST_ERROR
    _LAST_ERROR = ""


def runtime_packages() -> list[str]:
    """Import names whose on-disk size belongs to this reader, not to PanelCAD."""

    return ["paddleocr", "paddlex", "onnxruntime", "pyclipper", "shapely"]


def model_dirs() -> list[str]:
    """ONNX weight directories shipped with this install, when they are present."""

    from panelcad.ocr.weights import MODEL_DIRS, model_root

    root = model_root()
    if root is None:
        return []
    return [str(root / name) for name in MODEL_DIRS if (root / name).is_dir()]


def available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import paddleocr  # noqa: F401
    except ImportError:
        return False
    return True


def read_print(image: np.ndarray) -> list[OcrSpan] | None:
    """Read a full print. None means the local engine cannot load.

    A missing wheel or a missing weight file is None. This does not download
    a model to fill the gap. Runtime failures are logged rather than swallowed.
    """

    global _LAST_ERROR
    from panelcad.ocr.weights import weights_ready

    if image.size == 0 or not available() or not weights_ready():
        return None
    height, width = image.shape[:2]
    # A D-size panel stays near 2560. A tabloid reads at 2000, and a fax or
    # a skewed sheet is tried at both because each scale catches different tags.
    long_side = max(height, width)
    limits = [2560] if long_side >= 4000 else [2000]
    if 3000 <= long_side < 4000:
        limits = [2000, 2560]
    try:
        spans = []
        for limit in limits:
            spans.extend(_predict(_page(), image, limit))
        _LAST_ERROR = ""
    except Exception as exc:  # noqa: BLE001 — degrade gracefully to glyph fallback with diagnostic log
        _LAST_ERROR = f"{type(exc).__name__}: {exc}"
        log.warning("electrical PP-OCR page read failed (falling back): %s", _LAST_ERROR)
        return None
    for origin, crop, scale in _fine_crops(image):
        try:
            detail = _predict(_detail(), crop, 2000)
        except Exception as exc:  # noqa: BLE001 — keep page-level spans if one fine crop fails
            log.warning("electrical PP-OCR fine-crop read failed at %s: %s: %s", origin, type(exc).__name__, exc)
            continue
        for span in detail:
            spans.append(_place(span, origin, scale))
    return spans


def read_crop(image: np.ndarray) -> list[OcrSpan] | None:
    """Read a tight, already-oriented crop using the bundled detail ONNX reader.

    Returns None when the ONNX runtime or shipped weights are unavailable so
    callers can fall back to the glyph matcher without downloading anything.
    """

    from panelcad.ocr.weights import weights_ready

    if image.size == 0 or not available() or not weights_ready():
        return None
    short = min(int(image.shape[0]), int(image.shape[1]))
    if short < 3:
        return []
    primary = 4 if short < 16 else (3 if short < 56 else 2)
    border = (255, 255, 255) if image.ndim == 3 else 255

    def _run(scale: int) -> list[OcrSpan] | None:
        enlarged = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        padded = cv2.copyMakeBorder(
            enlarged,
            24,
            24,
            24,
            24,
            cv2.BORDER_CONSTANT,
            value=border,
        )
        try:
            return _predict(_detail(), padded, 2000)
        except Exception:
            return None

    spans = _run(primary)
    if spans is None:
        return None
    best_conf = max((span.confidence for span in spans), default=0.0)
    if best_conf < 0.85:
        alt_scale = 3 if primary == 4 else 4
        alt = _run(alt_scale)
        if alt:
            alt_conf = max((span.confidence for span in alt), default=0.0)
            if alt_conf > best_conf:
                return alt
    return spans


def _page() -> Any:
    global _PAGE
    if _PAGE is None:
        _PAGE = _reader("PP-OCRv5_mobile_det", "en_PP-OCRv5_mobile_rec")
    return _PAGE


def _detail() -> Any:
    global _DETAIL
    if _DETAIL is None:
        _DETAIL = _reader("PP-OCRv6_small_det", "PP-OCRv6_small_rec")
    return _DETAIL


def _reader(det_name: str, rec_name: str) -> Any:
    from panelcad.ocr.weights import model_directory

    det_dir = model_directory(det_name)
    rec_dir = model_directory(rec_name)
    ori_dir = model_directory("PP-LCNet_x1_0_textline_ori")
    if det_dir is None or rec_dir is None or ori_dir is None:
        raise FileNotFoundError("electrical OCR weights are not installed")
    # A model_dir is a local folder. These flags stop PaddleX from treating a
    # missing file as a request to download one.
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from paddleocr import PaddleOCR

    return PaddleOCR(
        device="cpu",
        engine="onnxruntime",
        enable_mkldnn=False,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=True,
        text_detection_model_name=det_name,
        text_recognition_model_name=rec_name,
        text_detection_model_dir=str(det_dir),
        text_recognition_model_dir=str(rec_dir),
        textline_orientation_model_name="PP-LCNet_x1_0_textline_ori",
        textline_orientation_model_dir=str(ori_dir),
        text_det_limit_type="max",
    )


def _reader_image(image: np.ndarray) -> np.ndarray:
    """Give PaddleX an H×W×3 image.

    Its detector unpacks ``src_h, src_w, _ = img.shape``. A gray plane has
    no third axis. Three equal channels are the same picture in RGB and in
    BGR. A fourth channel is alpha; the first three stay in the order the
    caller already used, which is RGB for a PIL image.
    """

    arr = np.asarray(image)
    if arr.ndim == 2:
        return np.stack((arr, arr, arr), axis=-1)
    if arr.ndim != 3:
        return arr
    channels = int(arr.shape[2])
    if channels == 1:
        plane = arr[:, :, 0]
        return np.stack((plane, plane, plane), axis=-1)
    if channels > 3:
        return np.ascontiguousarray(arr[:, :, :3])
    return arr


def _predict(reader: Any, image: np.ndarray, limit: int) -> list[OcrSpan]:
    result = reader.predict(
        _reader_image(image),
        text_det_limit_side_len=limit,
        text_det_limit_type="max",
    )
    return _spans(result)


def _spans(result: Any) -> list[OcrSpan]:
    spans: list[OcrSpan] = []
    for page in result or []:
        texts = _field(page, "rec_texts") or []
        scores = _field(page, "rec_scores") or []
        polys = _field(page, "rec_polys") or _field(page, "dt_polys") or []
        for index, text in enumerate(texts):
            cleaned = str(text).strip()
            if not cleaned:
                continue
            score = float(scores[index]) if index < len(scores) else 1.0
            left, top, right, bottom = _poly_box(polys[index] if index < len(polys) else None)
            spans.append(
                OcrSpan(
                    text=cleaned,
                    left=left,
                    top=top,
                    width=max(1, right - left),
                    height=max(1, bottom - top),
                    confidence=round(score, 4),
                    rotation=0,
                    source=_SOURCE,
                )
            )
    return spans


def _field(page: Any, name: str) -> Any:
    if hasattr(page, "get"):
        value = page.get(name)
        if value is not None:
            return value
    return getattr(page, name, None)


def _poly_box(poly: Any) -> tuple[int, int, int, int]:
    if poly is None:
        return 0, 0, 1, 1
    points = np.asarray(poly).reshape(-1, 2)
    if points.size == 0:
        return 0, 0, 1, 1
    return (
        int(points[:, 0].min()),
        int(points[:, 1].min()),
        int(points[:, 0].max()),
        int(points[:, 1].max()),
    )


def _place(span: OcrSpan, origin: tuple[int, int], scale: float) -> OcrSpan:
    return OcrSpan(
        text=span.text,
        left=int(round(origin[0] + span.left / scale)),
        top=int(round(origin[1] + span.top / scale)),
        width=max(1, int(round(span.width / scale))),
        height=max(1, int(round(span.height / scale))),
        confidence=span.confidence,
        rotation=span.rotation,
        source=span.source,
    )


def _fine_crops(image: np.ndarray) -> list[tuple[tuple[int, int], np.ndarray, float]]:
    """Enlarge up to two dense blocks of tiny glyphs above the title band.

    A terminal strip or panel legend sits in a corner and its wire numbers are
    only a few pixels tall. Filtering connected components in NumPy (instead of
    bailing out at ``count > 6000``) keeps speckled scans from losing fine-crop
    enrichment. Window sums use a 20 px cell grid so each candidate still matches
    the original exact 720×500 sliding window (an 80 px cell grid over-counted
    sideways pages and flipped faded panel scans 180°).
    """

    gray = image if image.ndim == 2 else cv2.cvtColor(image[:, :, :3], cv2.COLOR_RGB2GRAY)
    _threshold, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(ink, 8)
    if count <= 1:
        return []
    height, width = image.shape[:2]
    ceiling = int(height * 0.72)
    body = stats[1:]
    xs = body[:, cv2.CC_STAT_LEFT]
    ys = body[:, cv2.CC_STAT_TOP]
    ws = body[:, cv2.CC_STAT_WIDTH]
    hs = body[:, cv2.CC_STAT_HEIGHT]
    areas = body[:, cv2.CC_STAT_AREA]
    # Vectorized glyph-size filter strips 1-7 px dust speckles even on very noisy scans.
    mask = (areas >= 8) & (hs >= 4) & (hs <= 18) & (ws >= 1) & (ws <= 40) & (ys < ceiling)
    gx = xs[mask]
    gy = ys[mask]
    if gx.size < 30:
        return []

    win_w, win_h, step, cell = 720, 500, 80, 20
    nx = max(1, (width + cell - 1) // cell)
    ny = max(1, (max(1, ceiling) + cell - 1) // cell)
    grid = np.zeros((ny, nx), dtype=np.int32)
    np.add.at(grid, (np.clip(gy // cell, 0, ny - 1), np.clip(gx // cell, 0, nx - 1)), 1)
    kw = win_w // cell
    kh = win_h // cell
    pad_y = max(0, kh - 1)
    pad_x = max(0, kw - 1)
    integral = np.pad(grid, ((1, pad_y), (1, pad_x))).cumsum(axis=0).cumsum(axis=1)
    tops = np.arange(0, max(1, ceiling - 40), step) // cell
    lefts = np.arange(0, max(1, width - 80), step) // cell
    if tops.size == 0 or lefts.size == 0:
        return []
    t0 = tops[:, None]
    l0 = lefts[None, :]
    sums = (
        integral[t0 + kh, l0 + kw]
        - integral[t0, l0 + kw]
        - integral[t0 + kh, l0]
        + integral[t0, l0]
    )

    crops: list[tuple[tuple[int, int], np.ndarray, float]] = []
    chosen_boxes: list[tuple[int, int, int, int]] = []
    work_sums = sums.copy()
    while len(crops) < _MAX_FINE_CROPS:
        flat = int(np.argmax(work_sums))
        best_count = int(work_sums.flat[flat])
        if best_count < 40:
            break
        row, col = divmod(flat, work_sums.shape[1])
        best_left = int(lefts[col] * cell)
        best_top = int(tops[row] * cell)
        x0 = max(0, best_left - 8)
        y0 = max(0, best_top - 8)
        x1 = min(width, best_left + win_w + 8)
        y1 = min(height, best_top + win_h + 8)
        # Suppress the chosen window neighborhood so a second crop covers a distinct region.
        r0, r1 = max(0, row - (kh * cell) // step + 1), min(work_sums.shape[0], row + (kh * cell) // step)
        c0, c1 = max(0, col - (kw * cell) // step + 1), min(work_sums.shape[1], col + (kw * cell) // step)
        work_sums[r0:r1, c0:c1] = 0
        if any(
            not (x1 <= ox0 or x0 >= ox1 or y1 <= oy0 or y0 >= oy1)
            for ox0, oy0, ox1, oy1 in chosen_boxes
        ):
            continue
        chosen_boxes.append((x0, y0, x1, y1))
        crop = image[y0:y1, x0:x1]
        scale = 4.0
        if max(crop.shape[:2]) * scale > 2800:
            scale = 2800 / max(crop.shape[:2])
        enlarged = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        crops.append(((x0, y0), enlarged, scale))
    return crops
