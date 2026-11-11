"""App-local PP-OCR weights.

The Windows setup zip carries the ONNX files under
``resources/ocr/electrical/models``. A scan reads that directory. It does
not download weights and it does not look in ``~/.paddlex``.
"""

from __future__ import annotations

import os
from pathlib import Path

# Directory names of the published ONNX exports. The reader asks PaddleOCR
# for the names without the ``_onnx`` suffix and passes these folders as
# ``model_dir``, so the pipeline does not fetch them.
MODEL_DIRS = (
    "PP-OCRv5_mobile_det_onnx",
    "en_PP-OCRv5_mobile_rec_onnx",
    "PP-OCRv6_small_det_onnx",
    "PP-OCRv6_small_rec_onnx",
    "PP-LCNet_x1_0_textline_ori_onnx",
)

_NAME_TO_DIR = {
    "PP-OCRv5_mobile_det": "PP-OCRv5_mobile_det_onnx",
    "en_PP-OCRv5_mobile_rec": "en_PP-OCRv5_mobile_rec_onnx",
    "PP-OCRv6_small_det": "PP-OCRv6_small_det_onnx",
    "PP-OCRv6_small_rec": "PP-OCRv6_small_rec_onnx",
    "PP-LCNet_x1_0_textline_ori": "PP-LCNet_x1_0_textline_ori_onnx",
}

_REQUIRED = ("inference.onnx", "inference.yml")


def model_dir_name(model_name: str) -> str:
    return _NAME_TO_DIR.get(model_name, f"{model_name}_onnx")


def _complete(root: Path) -> bool:
    if not root.is_dir():
        return False
    for folder in MODEL_DIRS:
        model = root / folder
        if not all((model / name).is_file() for name in _REQUIRED):
            return False
    return True


def candidate_roots() -> list[Path]:
    """Override first. Otherwise the models shipped next to this install.

    ``PANELCAD_OCR_MODELS`` is the whole ``models`` directory. When it is
    set, a missing folder is not filled from another place.
    """

    override = os.environ.get("PANELCAD_OCR_MODELS", "").strip()
    if override:
        return [Path(override)]
    here = Path(__file__).resolve()
    # src/panelcad/ocr/weights.py -> checkout or %USERPROFILE%\\PanelCAD
    if len(here.parents) > 3:
        return [here.parents[3] / "resources" / "ocr" / "electrical" / "models"]
    return []


def model_root() -> Path | None:
    for root in candidate_roots():
        if _complete(root):
            return root
    return None


def model_directory(model_name: str) -> Path | None:
    root = model_root()
    if root is None:
        return None
    path = root / model_dir_name(model_name)
    if all((path / name).is_file() for name in _REQUIRED):
        return path
    return None


def weights_ready() -> bool:
    return model_root() is not None
