"""Word boxes returned by every PanelCAD OCR backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class OcrSpan:
    """One word in pixel coordinates.

    ``left`` and ``top`` are the top-left of an axis-aligned box on the image
    that was passed in. ``rotation`` is how far the glyphs are turned clockwise
    from ordinary left-to-right text: 0, 90, 180, or 270. ``confidence`` is 0
    to 1. ``source`` names the backend that produced the box.
    """

    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float
    rotation: int = 0
    source: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
            "confidence": self.confidence,
            "rotation": self.rotation,
            "source": self.source,
        }


class OcrEngine(Protocol):
    """Read one image. The same image in is the same kind of boxes out."""

    name: str

    def available(self) -> bool:
        """True when this machine can run the backend."""

    def recognize(
        self,
        image: np.ndarray,
        *,
        psm: int = 6,
        whitelist: str | None = None,
        rotations: tuple[int, ...] = (0,),
    ) -> list[OcrSpan]:
        """Words on ``image``.

        ``psm`` follows Tesseract's page-segmentation numbers so existing call
        sites can pass the mode they already use. 7 is one line, 8 is one
        word, 6 and 11 are a block or a sparse page. ``whitelist`` keeps only
        those characters. ``rotations`` are clockwise text angles to try.
        """
