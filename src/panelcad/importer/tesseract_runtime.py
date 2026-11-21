"""Compatibility alias for :mod:`panelcad.ocr.tesseract_runtime`.

The canonical Tesseract runtime discovery module now lives under
``panelcad.ocr`` so ``ocr`` never imports ``importer``. Aliasing the module
object keeps existing imports and ``monkeypatch.setattr`` calls pointing at
the exact same module state.
"""

from __future__ import annotations

import sys

from panelcad.ocr import tesseract_runtime as _runtime

sys.modules[__name__] = _runtime
