"""Full-print OCR from PaddleOCR, without the Paddle runtime.

Stock PaddleOCR on this machine loads PP-OCRv6 medium and the PaddlePaddle
wheel, with oneDNN turned off. A shop drawing does not need that stack.
This module asks PaddleOCR for two smaller English models from the same
family and runs them with ONNX Runtime:

* PP-OCRv5 mobile reads the page. On a schematic that is enough.
* PP-OCRv6 small reads one enlarged crop where the glyphs are only a few
  pixels tall (a terminal strip, a row of panel nameplates). The page pass
  shrinks those until the digits disappear.

No image is sent anywhere. The weights are the published ONNX exports
(Apache-2.0, Copyright PaddlePaddle Authors). PanelCAD loads them from the
app-local directory shipped with the install. This module does not download
them and does not read ``~/.paddlex``.
"""

from __future__ import annotations

import os
from typing import Any

import cv2
import numpy as np

from panelcad.ocr.types import OcrSpan

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("ORT_LOG_SEVERITY_LEVEL", "3")

_SOURCE = "electrical"
_PAGE: Any = None
_DETAIL: Any = None


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
        import paddleocr  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def read_print(image: np.ndarray) -> list[OcrSpan] | None:
    """Read a full print. None means the local engine cannot load.

    A missing wheel or a missing weight file is None. This does not download
    a model to fill the gap.
    """

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
    except Exception:
        return None
    for origin, crop, scale in _fine_crops(image):
        try:
            detail = _predict(_detail(), crop, 2000)
        except Exception:
            continue
        for span in detail:
            spans.append(_place(span, origin, scale))
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
    """Enlarge the densest block of tiny glyphs above the title band.

    A terminal strip sits in a corner and its wire numbers are only a few
    pixels tall. The page pass shrinks them. The title block is also tiny,
    and the page pass already reads it, so this search stays above that band.
    """

    gray = image if image.ndim == 2 else cv2.cvtColor(image[:, :, :3], cv2.COLOR_RGB2GRAY)
    _threshold, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(ink, 8)
    if count > 6000:
        return []
    height, width = image.shape[:2]
    ceiling = int(height * 0.72)
    points: list[tuple[int, int]] = []
    for index in range(1, count):
        x, y, w, h, area = (int(value) for value in stats[index])
        if area < 8 or h < 4 or h > 18 or w < 1 or w > 40 or y >= ceiling:
            continue
        points.append((x, y))
    if len(points) < 30:
        return []
    win_w, win_h, step = 720, 500, 80
    best_count = 0
    best_xy = (0, 0)
    for top in range(0, max(1, ceiling - 40), step):
        for left in range(0, max(1, width - 80), step):
            hits = sum(1 for x, y in points if left <= x < left + win_w and top <= y < top + win_h)
            if hits > best_count:
                best_count = hits
                best_xy = (left, top)
    if best_count < 40:
        return []
    x0 = max(0, best_xy[0] - 8)
    y0 = max(0, best_xy[1] - 8)
    x1 = min(width, best_xy[0] + win_w + 8)
    y1 = min(height, best_xy[1] + win_h + 8)
    crop = image[y0:y1, x0:x1]
    scale = 4.0
    if max(crop.shape[:2]) * scale > 2800:
        scale = 2800 / max(crop.shape[:2])
    enlarged = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return [((x0, y0), enlarged, scale)]
