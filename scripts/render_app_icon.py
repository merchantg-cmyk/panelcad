#!/usr/bin/env python3
"""Rasterize src/panelcad/assets/panelcad.svg to PNGs and a multi-size .ico.

Run from the repo root:

    python scripts/render_app_icon.py

Qt paints each size from the SVG. Pillow packs those PNGs into panelcad.ico.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "panelcad" / "assets"
SVG = ASSETS / "panelcad.svg"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    del app
    renderer = QSvgRenderer(str(SVG))
    if not renderer.isValid():
        print(f"SVG did not load: {SVG}", file=sys.stderr)
        return 1
    ASSETS.mkdir(parents=True, exist_ok=True)
    pngs: list[Path] = []
    for size in SIZES:
        image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        path = ASSETS / f"panelcad-{size}.png"
        if not image.save(str(path), "PNG"):
            print(f"Could not write {path}", file=sys.stderr)
            return 1
        pngs.append(path)
        print(path.relative_to(ROOT))

    from PIL import Image

    images = [Image.open(path).convert("RGBA") for path in pngs]
    ico = ASSETS / "panelcad.ico"
    images[-1].save(
        ico,
        format="ICO",
        sizes=[(image.width, image.height) for image in images],
        append_images=images[:-1],
    )
    print(ico.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
