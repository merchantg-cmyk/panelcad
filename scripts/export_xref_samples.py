"""PNG + CSV samples for coil/contact cross-reference."""

from __future__ import annotations

from pathlib import Path

from panelcad.examples.xref_demo import build_iec_mirror, build_nema_seal_in
from panelcad.render.page import paint_sheet

OUT = Path(__file__).resolve().parents[1] / "examples"


def _png(session, sheet_id: str, dest: Path, scale: float = 4.0) -> None:
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])
    sheet = session.sheet(sheet_id)
    width, height = sheet.page.dimensions()
    image = QImage(int(width * scale), int(height * scale), QImage.Format.Format_ARGB32)
    image.fill(QColor("#f7f4ea"))
    painter = QPainter(image)
    painter.scale(scale, scale)
    paint_sheet(
        painter,
        session.project,
        sheet,
        session.catalog,
        sheet_index=session.project.sheets.index(sheet),
        sheet_count=len(session.project.sheets),
    )
    painter.end()
    dest.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(dest))


def main() -> None:
    nema = build_nema_seal_in()
    nema.save(OUT / "xref_nema_seal_in.pcad.yaml")
    nema.export_cross_reference(OUT / "xref_report.csv")
    _png(nema, "control", OUT / "xref_nema_control.png")
    _png(nema, "aux", OUT / "xref_nema_aux.png")
    iec = build_iec_mirror()
    iec.save(OUT / "xref_iec_mirror.pcad.yaml")
    _png(iec, "main", OUT / "xref_iec_mirror.png")
    print("wrote", OUT / "xref_nema_control.png")
    print("wrote", OUT / "xref_nema_aux.png")
    print("wrote", OUT / "xref_iec_mirror.png")
    print("wrote", OUT / "xref_report.csv")


if __name__ == "__main__":
    main()
