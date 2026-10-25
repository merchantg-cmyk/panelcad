from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QImage, QPainter, QPen

from panelcad.render.painter import Painter

_FONT_FAMILY: str | None = None
_FONTS_READY = False
_GLYPH_PX = 72


def draw_world_text(
    painter: QPainter,
    x: float,
    y: float,
    text: str,
    size_mm: float,
    align: str = "middle",
    rotate: float = 0,
    *,
    bold: bool = False,
    color: str | None = None,
) -> None:
    """Draw text `size_mm` tall in the painter's world units.

    Glyphs are shaped at a large pixel size, then scaled. Drawing at 1–3 px
    and letting the world transform blow them up mashes letters together.
    """
    if not text:
        return
    size_mm = max(0.9, float(size_mm))
    font = QFont(_font_family())
    font.setPixelSize(_GLYPH_PX)
    if bold:
        font.setBold(True)
        font.setWeight(QFont.Weight.Bold)
    font.setHintingPreference(QFont.PreferNoHinting)
    font.setStyleStrategy(QFont.PreferAntialias)
    fm = QFontMetricsF(font)
    scale = size_mm / float(_GLYPH_PX)
    advance = fm.horizontalAdvance(text) * scale
    local_x = 0.0
    if align == "middle":
        local_x = -advance / 2.0
    elif align == "end":
        local_x = -advance
    from panelcad.theme import active_screen_ink

    ink = color or active_screen_ink() or "#111111"
    painter.save()
    painter.setPen(QColor(ink))
    painter.setBrush(QColor(0, 0, 0, 0))
    painter.setFont(font)
    painter.translate(x, y)
    if rotate:
        painter.rotate(rotate)
    painter.translate(local_x, 0.0)
    painter.scale(scale, scale)
    painter.drawText(QPointF(0.0, 0.0), text)
    painter.restore()


def text_width_mm(text: str, size_mm: float, *, bold: bool = False) -> float:
    """Advance of ``text`` in the same font and size as ``draw_world_text``."""

    return text_advance_mm(text, size_mm, bold=bold)


def text_advance_mm(text: str, size_mm: float, *, bold: bool = False) -> float:
    """Width of ``text`` in millimetres, using the same font the page draws."""

    if not text:
        return 0.0
    size_mm = max(0.9, float(size_mm))
    font = QFont(_font_family())
    font.setPixelSize(_GLYPH_PX)
    if bold:
        font.setBold(True)
        font.setWeight(QFont.Weight.Bold)
    font.setHintingPreference(QFont.PreferNoHinting)
    font.setStyleStrategy(QFont.PreferAntialias)
    advance = QFontMetricsF(font).horizontalAdvance(text)
    return float(advance) * (size_mm / float(_GLYPH_PX))


def _add_font(path: Path) -> str | None:
    if not path.is_file():
        return None
    fid = QFontDatabase.addApplicationFont(str(path))
    if fid == -1:
        return None
    families = QFontDatabase.applicationFontFamilies(fid)
    return families[0] if families else None


def _font_family() -> str:
    """Load a real TTF. Named families like 'Segoe UI' tofu under offscreen/PDF."""
    from panelcad.api.session import _ensure_qt_app

    _ensure_qt_app()
    global _FONT_FAMILY, _FONTS_READY
    if _FONTS_READY and _FONT_FAMILY:
        return _FONT_FAMILY
    regulars = [
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    ]
    bolds = [
        Path(r"C:\Windows\Fonts\arialbd.ttf"),
        Path(r"C:\Windows\Fonts\segoeuib.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ]
    family = None
    for path in regulars:
        family = _add_font(path)
        if family:
            break
    for path in bolds:
        _add_font(path)
    _FONT_FAMILY = family or "Sans Serif"
    _FONTS_READY = True
    return _FONT_FAMILY


def _screen_or_adapt(color: str) -> str:
    """Keep an explicit canvas token. Adapt every other print hex for the sheet."""

    from panelcad.theme import CanvasColor, adapt_print_color

    if isinstance(color, CanvasColor):
        return str(color)
    return adapt_print_color(color)


# Screen cream used as "paper" inside symbols/title-block cells. Print remaps to white.
_PRINT_FILL = {
    "#f7f4ea": "#ffffff",
    "#fbfaf3": "#ffffff",
    "#ece8dc": "#f2f2f2",
    "#fff8e1": "#ffffff",
    "#fffef8": "#ffffff",
}


class QtPainter(Painter):
    """Adapter so the title-block / sheet frame can draw into the GUI canvas and PDF."""

    def __init__(self, qp: QPainter, *, print_paper: bool = False) -> None:
        self.qp = qp
        self._print_paper = print_paper
        self._stroke = QColor("#111111")
        self._sw = 0.35
        self._fill: QColor | None = None

    def set_stroke(self, color: str, width: float) -> None:
        if not self._print_paper:
            color = _screen_or_adapt(color)
        self._stroke = QColor(color)
        self._sw = width

    def set_fill(self, color: str | None) -> None:
        if color and self._print_paper:
            color = _PRINT_FILL.get(color.lower(), color)
        elif color:
            color = _screen_or_adapt(color)
        self._fill = QColor(color) if color else None

    def _pen(self) -> QPen:
        pen = QPen(self._stroke, self._sw)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return pen

    def _apply(self) -> None:
        self.qp.setPen(self._pen())
        if self._fill is None:
            self.qp.setBrush(QColor(0, 0, 0, 0))
        else:
            self.qp.setBrush(self._fill)

    def rect(self, x: float, y: float, w: float, h: float) -> None:
        self._apply()
        self.qp.drawRect(QRectF(x, y, w, h))

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self._apply()
        self.qp.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    def circle(self, x: float, y: float, r: float) -> None:
        self._apply()
        self.qp.drawEllipse(QPointF(x, y), r, r)

    def arc(self, cx: float, cy: float, radius: float, start_deg: float, sweep_deg: float) -> None:
        """0° at +X. Positive sweep is counter-clockwise toward the top of the page.

        That is the mathematical sense used by the cutout arc, including when
        page Y increases downward.
        """

        if radius <= 0 or abs(sweep_deg) < 1e-9:
            return
        from PySide6.QtGui import QPainterPath

        self._apply()
        rect = QRectF(cx - radius, cy - radius, radius * 2.0, radius * 2.0)
        path = QPainterPath()
        path.arcMoveTo(rect, start_deg)
        path.arcTo(rect, start_deg, sweep_deg)
        self.qp.strokePath(path, self._pen())

    def polyline(self, points: list[tuple[float, float]]) -> None:
        if len(points) < 2:
            return
        self._apply()
        from PySide6.QtGui import QPolygonF

        poly = QPolygonF([QPointF(x, y) for x, y in points])
        self.qp.drawPolyline(poly)

    def polygon(self, points: list[tuple[float, float]]) -> None:
        if len(points) < 3:
            return
        from PySide6.QtGui import QPolygonF

        self._apply()
        self.qp.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))

    def text(
        self,
        x: float,
        y: float,
        text: str,
        size: float,
        align: str = "middle",
        rotate: float = 0,
        *,
        bold: bool = False,
        color: str | None = None,
    ) -> None:
        draw_world_text(self.qp, x, y, text, size, align, rotate=rotate, bold=bold, color=color)

    def image(self, path: str, x: float, y: float, w: float, h: float) -> None:
        target = QRectF(x, y, w, h)
        suffix = path.lower()
        if suffix.endswith(".svg"):
            from PySide6.QtSvg import QSvgRenderer

            renderer = QSvgRenderer(path)
            if renderer.isValid():
                renderer.render(self.qp, target)
            return
        from panelcad.render.images import cached_qimage

        img = cached_qimage(path)
        if img.isNull():
            return
        iw, ih = img.width(), img.height()
        if iw <= 0 or ih <= 0:
            return
        scale = min(w / iw, h / ih)
        dw, dh = iw * scale, ih * scale
        dx = x + (w - dw) / 2.0
        dy = y + (h - dh) / 2.0
        self.qp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.qp.drawImage(QRectF(dx, dy, dw, dh), img)
