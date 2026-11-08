"""One entry point for the pytesseract calls the importer already makes.

The default backend calls Tesseract and returns its dictionary unchanged, so
a monkeypatched ``pytesseract.image_to_data`` still fails the way the tests
expect. ``PANELCAD_OCR_BACKEND=electrical`` returns the same dictionary shape
from the local engine and does not open the Tesseract binary.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from PIL import Image

from panelcad.ocr.backend import active_backend, tesseract_required
from panelcad.ocr.electrical import ElectricalOcr
from panelcad.ocr.tesseract_engine import TesseractOcr, parse_tesseract_config
from panelcad.ocr.types import OcrEngine, OcrSpan

__all__ = [
    "get_engine",
    "image_to_data",
    "image_to_string",
    "tesseract_required",
]

_DATA_KEYS = (
    "level",
    "page_num",
    "block_num",
    "par_num",
    "line_num",
    "word_num",
    "left",
    "top",
    "width",
    "height",
    "conf",
    "text",
)


def get_engine(name: str | None = None) -> OcrEngine:
    """The named backend, or the one selected for this call."""

    chosen = name or active_backend()
    if chosen == "electrical":
        return ElectricalOcr()
    return TesseractOcr()


def image_to_data(
    image: np.ndarray | Image.Image,
    output_type: Any = None,
    config: str = "",
    **kwargs: Any,
) -> dict[str, list[Any]]:
    """Tesseract's DICT shape, from whichever backend is selected.

    ``output_type`` is accepted so call sites can keep passing
    ``pytesseract.Output.DICT``. The electrical backend always returns a dict.
    """

    cfg = config or str(kwargs.pop("config", "") or "")
    if not tesseract_required():
        return _electrical_dict(image, cfg)
    import pytesseract

    from panelcad.importer.raster import configure_tesseract

    configure_tesseract()
    call: dict[str, Any] = {}
    if output_type is not None:
        call["output_type"] = output_type
    elif hasattr(pytesseract, "Output"):
        call["output_type"] = pytesseract.Output.DICT
    if cfg:
        call["config"] = cfg
    call.update(kwargs)
    return pytesseract.image_to_data(image, **call)


def image_to_string(
    image: np.ndarray | Image.Image,
    config: str = "",
    **kwargs: Any,
) -> str:
    """The words on ``image``, joined the way ``image_to_string`` joins them."""

    cfg = config or str(kwargs.pop("config", "") or "")
    if not tesseract_required():
        return _electrical_string(image, cfg)
    import pytesseract

    from panelcad.importer.raster import configure_tesseract

    configure_tesseract()
    if cfg:
        kwargs["config"] = cfg
    return str(pytesseract.image_to_string(image, **kwargs) or "")


def _electrical_dict(image: np.ndarray | Image.Image, config: str) -> dict[str, list[Any]]:
    spans = _electrical_spans(image, config)
    rows: dict[str, list[Any]] = {key: [] for key in _DATA_KEYS}
    for index, span in enumerate(spans, start=1):
        rows["level"].append(5)
        rows["page_num"].append(1)
        rows["block_num"].append(1)
        rows["par_num"].append(1)
        rows["line_num"].append(1)
        rows["word_num"].append(index)
        rows["left"].append(int(span.left))
        rows["top"].append(int(span.top))
        rows["width"].append(int(span.width))
        rows["height"].append(int(span.height))
        # Tesseract's conf is 0–100. Call sites compare against 45 and 60.
        rows["conf"].append(round(span.confidence * 100.0, 1))
        rows["text"].append(span.text)
    return rows


def _electrical_string(image: np.ndarray | Image.Image, config: str) -> str:
    psm, _whitelist = parse_tesseract_config(config)
    spans = _electrical_spans(image, config)
    texts = [span.text for span in spans if span.text]
    if psm in {8, 10}:
        return "".join(texts)
    if psm in {7, 13}:
        return " ".join(texts)
    return "\n".join(texts)


def _electrical_spans(image: np.ndarray | Image.Image, config: str) -> list[OcrSpan]:
    psm, whitelist = parse_tesseract_config(config)
    rotations = _rotations_for(image, psm)
    engine = ElectricalOcr()
    best: list[OcrSpan] = []
    best_score = -1.0
    for rotation in rotations:
        spans = engine.recognize(image, psm=psm, whitelist=whitelist, rotations=(rotation,))
        score = sum(span.confidence for span in spans)
        if score > best_score:
            best = spans
            best_score = score
    return best


def _rotations_for(image: np.ndarray | Image.Image, psm: int) -> tuple[int, ...]:
    """A tall single-line crop may be a vertical wire number. A page is not.

    The importer turns the whole sheet before the page pass. Searching four
    angles on every strip would also read symbol strokes as words. A crop that
    is already one line, and taller than it is wide, is the case worth turning.
    """

    if psm not in {7, 8, 10, 13}:
        return (0,)
    if isinstance(image, Image.Image):
        height, width = image.height, image.width
    else:
        height, width = int(image.shape[0]), int(image.shape[1])
    if height > width * 1.15:
        return (0, 90, 270)
    return (0,)
