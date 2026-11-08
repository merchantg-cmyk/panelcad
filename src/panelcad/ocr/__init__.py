"""On-device OCR backends for scan import and as-built reconstruct.

``tesseract`` is the existing reader. ``electrical`` is the local engine for
shop drawings. Neither backend calls a cloud service. Select one with
``PANELCAD_OCR_BACKEND`` or ``panelcad import --ocr-backend``.
"""

from panelcad.ocr.backend import active_backend, ocr_scope
from panelcad.ocr.dispatch import get_engine, image_to_data, image_to_string
from panelcad.ocr.types import OcrSpan

__all__ = [
    "OcrSpan",
    "active_backend",
    "get_engine",
    "image_to_data",
    "image_to_string",
    "ocr_scope",
]
