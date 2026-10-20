"""Three-page ANSI ladder print: power supply, relay logic, motor starter.

Used as a from-print sharpen fixture. The figures are textbook-simple so a
weak model can read the labels.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMarginsF, QSizeF, Qt
from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen
from PySide6.QtWidgets import QApplication

PACKAGE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PRINT = PACKAGE_ROOT / "examples" / "simple_circuit.pdf"


def write_print(dest: Path | None = None) -> Path:
    """Write a 3-page landscape Letter PDF of the simple circuit."""

    if QApplication.instance() is None:
        QApplication([])

    path = Path(dest) if dest is not None else DEFAULT_PRINT
    path.parent.mkdir(parents=True, exist_ok=True)

    writer = QPdfWriter(str(path))
    writer.setResolution(150)
    writer.setTitle("Simple Circuit - ANSI Ladder")
    writer.setPageSize(QPageSize(QSizeF(11.0, 8.5), QPageSize.Unit.Inch))
    writer.setPageMargins(QMarginsF(18, 18, 18, 18))

    painter = QPainter(writer)
    _page_power(painter, writer)
    writer.newPage()
    _page_relay(painter, writer)
    writer.newPage()
    _page_starter(painter, writer)
    painter.end()
    return path


def _page_size(writer) -> tuple[float, float]:
    rect = writer.pageLayout().paintRect(QPageLayout.Unit.Point)
    return float(rect.width()), float(rect.height())


def _blank(painter: QPainter, writer) -> tuple[float, float]:
    w, h = _page_size(writer)
    painter.fillRect(painter.viewport(), Qt.GlobalColor.white)
    painter.fillRect(0, 0, int(w) + 40, int(h) + 40, Qt.GlobalColor.white)
    painter.setPen(QPen(Qt.GlobalColor.black, 1.2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(2, 2, int(w) - 4, int(h) - 4)
    return w, h


def _page_w(writer) -> float:
    return _page_size(writer)[0]


def _header(painter: QPainter, writer, title: str, fig: str) -> None:
    w, _h = _blank(painter, writer)
    painter.setPen(QPen(Qt.GlobalColor.black, 1.2))
    painter.setFont(QFont("Arial", 18, QFont.Weight.Bold))
    painter.drawText(20, 40, title)
    painter.setFont(QFont("Arial", 12, QFont.Weight.Bold))
    painter.drawText(w - 140, 40, fig)
    painter.setFont(QFont("Arial", 10))
    painter.drawText(20, 62, "NEMA / ANSI   ladder   square coils")
    painter.drawLine(20, 72, w - 20, 72)


def _rails(painter: QPainter, writer, y0: float, y1: float) -> tuple[float, float]:
    left, right = 80.0, _page_w(writer) - 80.0
    painter.setPen(QPen(Qt.GlobalColor.black, 3))
    painter.drawLine(int(left), int(y0), int(left), int(y1))
    painter.drawLine(int(right), int(y0), int(right), int(y1))
    painter.setFont(QFont("Arial", 12, QFont.Weight.Bold))
    painter.drawText(int(left - 18), int(y0) - 8, "L1")
    painter.drawText(int(right - 8), int(y0) - 8, "L2")
    return left, right


def _rung(painter: QPainter, left: float, right: float, y: float) -> None:
    painter.setPen(QPen(Qt.GlobalColor.black, 2))
    painter.drawLine(int(left), int(y), int(right), int(y))


def _fuse(painter: QPainter, x: float, y: float, ref: str) -> None:
    painter.setPen(QPen(Qt.GlobalColor.black, 2))
    painter.drawRect(int(x - 12), int(y - 22), 24, 44)
    painter.drawLine(int(x), int(y - 22), int(x), int(y - 36))
    painter.drawLine(int(x), int(y + 22), int(x), int(y + 36))
    painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
    painter.drawText(int(x - 14), int(y + 4), ref)


def _xfmr(painter: QPainter, x: float, y: float) -> None:
    painter.setPen(QPen(Qt.GlobalColor.black, 2))
    painter.drawEllipse(int(x - 28), int(y - 22), 32, 44)
    painter.drawEllipse(int(x - 4), int(y - 22), 32, 44)
    painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
    painter.drawText(int(x - 36), int(y - 28), "H1")
    painter.drawText(int(x + 16), int(y - 28), "X1")
    painter.drawText(int(x - 36), int(y + 40), "H2")
    painter.drawText(int(x + 16), int(y + 40), "X2")
    painter.drawText(int(x - 6), int(y + 6), "T")


def _pb_nc(painter: QPainter, x: float, y: float, ref: str) -> None:
    painter.setPen(QPen(Qt.GlobalColor.black, 2))
    painter.drawLine(int(x - 18), int(y), int(x + 18), int(y))
    painter.drawLine(int(x), int(y - 10), int(x), int(y))
    painter.drawEllipse(int(x - 8), int(y - 28), 16, 16)
    painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
    painter.drawText(int(x - 16), int(y + 22), ref)


def _pb_no(painter: QPainter, x: float, y: float, ref: str) -> None:
    painter.setPen(QPen(Qt.GlobalColor.black, 2))
    painter.drawLine(int(x - 18), int(y), int(x - 4), int(y))
    painter.drawLine(int(x + 4), int(y - 8), int(x + 18), int(y))
    painter.drawLine(int(x - 4), int(y), int(x + 4), int(y - 8))
    painter.drawEllipse(int(x - 8), int(y - 28), 16, 16)
    painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
    painter.drawText(int(x - 18), int(y + 22), ref)


def _coil(painter: QPainter, x: float, y: float, ref: str) -> None:
    painter.setPen(QPen(Qt.GlobalColor.black, 2))
    painter.drawRect(int(x - 18), int(y - 18), 36, 36)
    painter.setFont(QFont("Arial", 10, QFont.Weight.Bold))
    painter.drawText(int(x - 10), int(y + 5), ref)


def _ol(painter: QPainter, x: float, y: float) -> None:
    painter.setPen(QPen(Qt.GlobalColor.black, 2))
    painter.drawLine(int(x - 16), int(y), int(x + 16), int(y))
    painter.drawLine(int(x), int(y - 8), int(x), int(y))
    painter.drawLine(int(x - 8), int(y - 14), int(x + 8), int(y - 14))
    painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
    painter.drawText(int(x - 10), int(y + 20), "OL")


def _lamp(painter: QPainter, x: float, y: float, ref: str) -> None:
    painter.setPen(QPen(Qt.GlobalColor.black, 2))
    painter.drawEllipse(int(x - 14), int(y - 14), 28, 28)
    painter.drawLine(int(x - 10), int(y - 10), int(x + 10), int(y + 10))
    painter.drawLine(int(x + 10), int(y - 10), int(x - 10), int(y + 10))
    painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
    painter.drawText(int(x - 12), int(y + 28), ref)


def _aux_no(painter: QPainter, x: float, y: float, ref: str) -> None:
    painter.setPen(QPen(Qt.GlobalColor.black, 2))
    painter.drawLine(int(x - 16), int(y), int(x - 4), int(y))
    painter.drawLine(int(x + 4), int(y - 8), int(x + 16), int(y))
    painter.drawLine(int(x - 4), int(y), int(x + 4), int(y - 8))
    painter.setFont(QFont("Arial", 8, QFont.Weight.Bold))
    painter.drawText(int(x - 16), int(y + 18), ref)


def _motor(painter: QPainter, x: float, y: float) -> None:
    painter.setPen(QPen(Qt.GlobalColor.black, 2))
    painter.drawEllipse(int(x - 22), int(y - 22), 44, 44)
    painter.setFont(QFont("Arial", 12, QFont.Weight.Bold))
    painter.drawText(int(x - 8), int(y + 5), "M")
    painter.setFont(QFont("Arial", 9))
    painter.drawText(int(x - 16), int(y + 36), "M1")


def _page_power(painter: QPainter, writer) -> None:
    _header(painter, writer, "POWER SUPPLY", "FIG. P1")
    left, right = _rails(painter, writer, 100, 430)
    y = 200.0
    _rung(painter, left, right, y)
    _fuse(painter, left + 90, y, "FU1")
    _xfmr(painter, left + 250, y)
    _fuse(painter, left + 430, y, "FU2")
    painter.setFont(QFont("Arial", 11))
    painter.drawText(int(left), 500, "480 V primary (H1-H2) to 120 V control (X1-X2). Fuse both sides.")
    painter.drawText(int(left), 520, "Place: FU1, T (control transformer), FU2. Ladder rung L1 to L2.")


def _page_relay(painter: QPainter, writer) -> None:
    _header(painter, writer, "RELAY LOGIC", "FIG. P2")
    left, right = _rails(painter, writer, 100, 430)
    y1, y2 = 180.0, 300.0
    _rung(painter, left, right, y1)
    _pb_no(painter, left + 140, y1, "PB1")
    _coil(painter, left + 360, y1, "CR1")
    _rung(painter, left, right, y2)
    _aux_no(painter, left + 140, y2, "CR1 3-4")
    _lamp(painter, left + 360, y2, "PL1")
    painter.setFont(QFont("Arial", 11))
    painter.drawText(int(left), 500, "Rung 1: PB1 (NO) to CR1 coil. Rung 2: CR1 aux NO to PL1 lamp.")
    painter.drawText(int(left), 520, "Place: PB1, CR1 coil, CR1 aux, PL1. Two ladder rungs.")


def _page_starter(painter: QPainter, writer) -> None:
    _header(painter, writer, "MOTOR STARTER", "FIG. P3")
    left, right = _rails(painter, writer, 100, 430)
    y = 200.0
    _rung(painter, left, right, y)
    _pb_nc(painter, left + 100, y, "STOP")
    _pb_no(painter, left + 220, y, "START")
    _coil(painter, left + 360, y, "M")
    _ol(painter, left + 470, y)
    _aux_no(painter, left + 220, y + 70, "M 3-4")
    painter.setPen(QPen(Qt.GlobalColor.black, 1.2))
    painter.drawLine(int(left + 220), int(y), int(left + 220), int(y + 70))
    _motor(painter, left + 580, y + 80)
    painter.setFont(QFont("Arial", 11))
    painter.drawText(int(left), 500, "STOP (NC) then START (NO) then M coil then OL. Hold-in M 3-4 around START.")
    painter.drawText(int(left), 520, "Place: STOP, START, M coil, OL, M1 motor. One ladder rung plus motor.")


if __name__ == "__main__":
    print(write_print())
