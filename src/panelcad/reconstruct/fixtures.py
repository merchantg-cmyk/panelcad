"""Synthetic phone-like panel photos for tests (perspective + glare + noise).

These are stand-ins for a tech's phone shot of a *physical* panel — not a
scan of a drawing. Geometry (corners, rail, part crop) is recorded from the
perspective warp so scale and part-ID tests exercise the real-photo path.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from PySide6.QtCore import QBuffer, QIODevice, QPoint, QRect, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QPainter,
    QPen,
    QPolygon,
    QRadialGradient,
    QTransform,
)


def write_backpanel_photo(path: Path, *, seed: int = 7) -> dict[str, Any]:
    """A cluttered backplate: DIN rail, LS MC-9b nameplate, duct, terminals, wire 104."""

    src = _paint_backpanel()
    meta = _warp_and_noise(src, path, seed=seed, inset=48)
    meta.update(
        {
            "role": "backpanel",
            "catalog": "MC-9b",
            "manufacturer": "LS Electric",
            "din_rail_mm": 35.0,
            "enclosure_width_mm": 400.0,
            "enclosure_height_mm": 500.0,
            "kind": "panel_photo",
        }
    )
    return meta


def write_door_photo(path: Path, *, seed: int = 11) -> dict[str, Any]:
    src = _paint_door()
    meta = _warp_and_noise(src, path, seed=seed, inset=36)
    meta["role"] = "door_inside"
    meta["kind"] = "panel_photo"
    return meta


def write_nameplate_photo(path: Path, *, seed: int = 19) -> dict[str, Any]:
    """Close-up of a dirty, glared LS MC-9b nameplate — typical phone crop."""

    src = _paint_nameplate()
    meta = _warp_and_noise(src, path, seed=seed, inset=28)
    meta.update(
        {
            "role": "nameplate",
            "catalog": "MC-9b",
            "manufacturer": "LS Electric",
            "kind": "panel_photo",
        }
    )
    return meta


def write_drawing_scan(path: Path) -> dict[str, Any]:
    """Flat white schematic scan. Secondary print input only — not a panel photo."""

    image = QImage(700, 500, QImage.Format.Format_RGB32)
    image.fill(QColor("#f4f1e8"))
    painter = QPainter(image)
    painter.fillRect(24, 20, 652, 460, QColor("#ffffff"))
    painter.setPen(QPen(QColor("#111111"), 2))
    painter.drawRect(40, 40, 620, 420)
    painter.setFont(QFont("DejaVu Sans", 11, QFont.Weight.Bold))
    painter.drawText(QRect(40, 48, 620, 22), Qt.AlignmentFlag.AlignCenter, "SCHEMATIC  —  SHEET 1")
    # IEC coil box and a ladder rung (drawing, not hardware)
    painter.drawRect(160, 160, 70, 90)
    painter.drawText(QRect(160, 252, 70, 20), Qt.AlignmentFlag.AlignCenter, "K1")
    painter.drawLine(80, 205, 160, 205)
    painter.drawLine(230, 205, 520, 205)
    painter.drawRect(520, 170, 50, 70)
    painter.drawText(QRect(520, 244, 50, 20), Qt.AlignmentFlag.AlignCenter, "X1")
    painter.setFont(QFont("DejaVu Sans", 9))
    painter.drawText(QRect(300, 180, 80, 18), Qt.AlignmentFlag.AlignCenter, "104")
    painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(path), "PNG")
    return {
        "path": str(path),
        "role": "scan",
        "kind": "drawing_scan",
        "width_px": image.width(),
        "height_px": image.height(),
        "corners": [(0, 0), (image.width(), 0), (image.width(), image.height()), (0, image.height())],
    }


def _paint_backpanel() -> QImage:
    image = QImage(800, 600, QImage.Format.Format_RGB32)
    image.fill(QColor("#6d6a63"))
    painter = QPainter(image)
    painter.fillRect(40, 30, 720, 540, QColor("#8a8680"))
    painter.fillRect(40, 30, 720, 540, QColor("#00000000"))
    # enclosure inside 400 mm × 300 mm → 1.8 px/mm on the 720×540 face.
    # DIN rail hat 35 mm → 63 px tall band.
    painter.fillRect(70, 210, 660, 63, QColor("#c4b896"))
    painter.fillRect(70, 232, 660, 8, QColor("#9a8b62"))
    # wire duct under the rail
    painter.fillRect(70, 290, 660, 40, QColor("#6b8f3a"))
    # contactor body 45 × 73.5 mm → 81 × 132 px, sitting on the rail
    painter.fillRect(200, 140, 81, 132, QColor("#1c1c1c"))
    painter.fillRect(206, 146, 69, 36, QColor("#f4f0d8"))
    painter.setPen(QColor("#111111"))
    font = QFont("DejaVu Sans", 9)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(QRect(206, 146, 69, 18), Qt.AlignmentFlag.AlignCenter, "LS Electric")
    painter.drawText(QRect(206, 162, 69, 18), Qt.AlignmentFlag.AlignCenter, "MC-9b")
    painter.setPen(QColor("#f2f2f2"))
    painter.setFont(QFont("DejaVu Sans", 11, QFont.Weight.Bold))
    painter.drawText(QRect(200, 118, 81, 20), Qt.AlignmentFlag.AlignCenter, "K1")
    # terminal strip
    painter.fillRect(420, 168, 120, 90, QColor("#d8d2c2"))
    painter.setPen(QColor("#111111"))
    painter.setFont(QFont("DejaVu Sans", 9, QFont.Weight.Bold))
    painter.drawText(QRect(420, 168, 120, 18), Qt.AlignmentFlag.AlignCenter, "X1")
    for i, label in enumerate(("1", "2", "3")):
        painter.fillRect(430 + i * 36, 192, 28, 48, QColor("#eeeae0"))
        painter.drawText(QRect(430 + i * 36, 192, 28, 48), Qt.AlignmentFlag.AlignCenter, label)
    # wire number sleeve in the bundle
    painter.fillRect(360, 300, 36, 16, QColor("#f4e27a"))
    painter.setFont(QFont("DejaVu Sans", 8, QFont.Weight.Bold))
    painter.drawText(QRect(360, 300, 36, 16), Qt.AlignmentFlag.AlignCenter, "104")
    # glare blob, hot-spot, and cable clutter (phone shot, not a clean render)
    painter.fillRect(520, 80, 90, 40, QColor("#c8c4b8"))
    painter.setBrush(QColor(255, 255, 230, 90))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(210, 148, 50, 28)
    painter.setPen(QColor("#2a2418"))
    for i in range(8):
        painter.drawLine(80 + i * 8, 300, 140 + i * 6, 540)
    for i in range(10):
        painter.setPen(QColor(20 + i * 4, 18, 10, 200))
        painter.drawLine(300 + i * 5, 310, 280 + i * 9, 560)
    painter.end()
    return image


def _paint_nameplate() -> QImage:
    image = QImage(480, 360, QImage.Format.Format_RGB32)
    image.fill(QColor("#4a4842"))
    painter = QPainter(image)
    painter.fillRect(40, 40, 400, 280, QColor("#1c1c1c"))
    painter.fillRect(70, 90, 340, 140, QColor("#f4f0d8"))
    painter.setPen(QColor("#111111"))
    font = QFont("DejaVu Sans", 18, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(QRect(70, 100, 340, 50), Qt.AlignmentFlag.AlignCenter, "LS Electric")
    painter.setFont(QFont("DejaVu Sans", 22, QFont.Weight.Bold))
    painter.drawText(QRect(70, 150, 340, 60), Qt.AlignmentFlag.AlignCenter, "MC-9b")
    painter.setBrush(QColor(255, 255, 240, 110))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(240, 80, 160, 70)
    painter.end()
    return image


def _paint_door() -> QImage:
    image = QImage(640, 480, QImage.Format.Format_RGB32)
    image.fill(QColor("#5c5a54"))
    painter = QPainter(image)
    painter.fillRect(30, 24, 580, 430, QColor("#7a7670"))
    painter.setBrush(QColor("#c0392b"))
    painter.setPen(QColor("#111111"))
    painter.drawEllipse(280, 160, 80, 80)
    painter.setFont(QFont("DejaVu Sans", 10, QFont.Weight.Bold))
    painter.drawText(QRect(260, 250, 120, 24), Qt.AlignmentFlag.AlignCenter, "ESTOP")
    painter.end()
    return image


def _warp_and_noise(src: QImage, dest: Path, *, seed: int, inset: int) -> dict[str, Any]:
    rng = random.Random(seed)
    w, h = src.width(), src.height()
    corners = [
        QPoint(inset + rng.randint(-8, 8), inset + rng.randint(-6, 6)),
        QPoint(w - inset + rng.randint(-8, 8), inset + rng.randint(-8, 8)),
        QPoint(w - inset + rng.randint(-10, 6), h - inset + rng.randint(-6, 8)),
        QPoint(inset + rng.randint(-6, 10), h - inset + rng.randint(-8, 6)),
    ]
    quad = QPolygon(corners)
    source_quad = QPolygon(
        [QPoint(0, 0), QPoint(w, 0), QPoint(w, h), QPoint(0, h)]
    )
    transform = QTransform()
    QTransform.quadToQuad(source_quad, quad, transform)
    warped = QImage(w, h, QImage.Format.Format_RGB32)
    warped.fill(QColor("#2b2a28"))
    painter = QPainter(warped)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.setTransform(transform)
    painter.drawImage(0, 0, src)
    painter.resetTransform()
    painter.setBrush(QColor(255, 255, 236, 70))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(
        QPoint(int(w * 0.62) + rng.randint(-20, 20), int(h * 0.22) + rng.randint(-12, 12)),
        70 + rng.randint(-10, 16),
        36 + rng.randint(-8, 10),
    )
    painter.end()
    _speckle(warped, rng)
    _vignette(warped)
    warped = _jpeg_roundtrip(warped, quality=72)
    dest.parent.mkdir(parents=True, exist_ok=True)
    warped.save(str(dest), "PNG")
    mapped_rail = _map_points(transform, [(70, 210), (70, 273)])
    mapped_part = _map_points(transform, [(200, 140), (281, 272)])
    return {
        "path": str(dest),
        "width_px": w,
        "height_px": h,
        "corners": [(p.x(), p.y()) for p in corners],
        "rail_p1": mapped_rail[0],
        "rail_p2": mapped_rail[1],
        "part_region": {
            "x": mapped_part[0][0],
            "y": mapped_part[0][1],
            "w": mapped_part[1][0] - mapped_part[0][0],
            "h": mapped_part[1][1] - mapped_part[0][1],
        },
    }


def _map_points(transform: QTransform, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for x, y in points:
        mapped = transform.map(float(x), float(y))
        out.append((float(mapped[0]), float(mapped[1])))
    return out


def _speckle(image: QImage, rng: random.Random) -> None:
    for _ in range(1800):
        x = rng.randint(0, image.width() - 1)
        y = rng.randint(0, image.height() - 1)
        color = image.pixelColor(x, y)
        delta = rng.randint(-16, 16)
        image.setPixelColor(
            x,
            y,
            QColor(
                max(0, min(255, color.red() + delta)),
                max(0, min(255, color.green() + delta)),
                max(0, min(255, color.blue() + delta)),
            ),
        )


def _vignette(image: QImage) -> None:
    """Darken the frame the way a phone lens / indoor cabinet shot does."""

    painter = QPainter(image)
    gradient = QRadialGradient(
        image.width() / 2.0,
        image.height() / 2.0,
        max(image.width(), image.height()) * 0.72,
    )
    gradient.setColorAt(0.50, QColor(0, 0, 0, 0))
    gradient.setColorAt(0.78, QColor(0, 0, 0, 40))
    gradient.setColorAt(1.00, QColor(0, 0, 0, 120))
    painter.fillRect(image.rect(), QBrush(gradient))
    painter.end()


def _jpeg_roundtrip(image: QImage, *, quality: int) -> QImage:
    """Phone-camera compression; geometry is unchanged."""

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.ReadWrite)
    if not image.save(buffer, "JPEG", quality):
        return image
    compressed = QImage()
    if not compressed.loadFromData(buffer.data(), "JPEG"):
        return image
    return compressed
