"""Every primitive type paints without raising (a raise inside QGraphicsItem.paint crashes Qt natively)."""

from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from panelcad.core.primitives import Primitive
from panelcad.gui.canvas import _paint_primitive


def test_every_primitive_type_paints_without_raising():
    QApplication.instance() or QApplication([])
    prims = [
        Primitive(type="rect", x=1, y=1, w=5, h=4),
        Primitive(type="line", x=0, y=0, x2=10, y2=10),
        Primitive(type="circle", x=5, y=5, r=2),
        Primitive(type="arc", x=5, y=5, r=3, start=0, sweep=90),
        Primitive(type="polyline", points=[(0, 0), (4, 2), (8, 0)]),
        Primitive(type="polyline", points=[]),
        Primitive(type="text", x=5, y=5, text="M1"),
    ]
    img = QImage(64, 64, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    try:
        for prim in prims:
            _paint_primitive(painter, prim)
    finally:
        painter.end()
