"""Tools → Panel → Cutout drawings. Preview, print, and shop DXF from the layout."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from panelcad.api.session import Session
from panelcad.core.cutout import DATUMS, STYLES, SURFACES, UNITS, VIEWS, SurfaceDrawing, surface_title
from panelcad.render.cutout_draw import render_cutout_qimage


def dialog_size_for_screen(avail_width: int, avail_height: int) -> tuple[int, int]:
    """Opening size from the screen, so the preview can use the leftover height.

    A fixed 860 × 700 cap left the sheet about 230 px tall on a large monitor.
    The floor still fits the settings rows on an 800 × 600 screen.
    """

    width = min(max(640, int(avail_width) - 48), 1680)
    height = min(max(520, int(avail_height) - 64), 1200)
    return width, height


_DATUM_LABELS = {
    "top_left": "Top left",
    "top_right": "Top right",
    "bottom_left": "Bottom left",
    "bottom_right": "Bottom right",
}


class CutoutDialog(QDialog):
    """Generate cutout sheets from the current door, backplate, and walls."""

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Cutout drawings")
        self.setMinimumSize(640, 420)
        # This dialog sizes itself. The theme clamp must not reparent its preview.
        self.setProperty("_pc_own_fit", True)
        self._drawings: list[SurfaceDrawing] = []
        self._preview_image = None
        self._zoom_fit = True

        root = QVBoxLayout(self)
        title = QLabel("Cutout and drill drawings")
        font = title.font()
        font.setPointSize(12)
        font.setBold(True)
        title.setFont(font)
        root.addWidget(title)
        note = QLabel(
            "One sheet per surface, dimensioned from the datum. "
            "Unknown cutouts are flagged and are not given a hole."
        )
        note.setWordWrap(True)
        root.addWidget(note)

        settings_host = QWidget()
        settings_host.setObjectName("cutoutSettings")
        settings_box = QVBoxLayout(settings_host)
        settings_box.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.datum = QComboBox()
        self.datum.setObjectName("cutoutDatum")
        for key in DATUMS:
            self.datum.addItem(_DATUM_LABELS[key], key)
        self.style = QComboBox()
        self.style.setObjectName("cutoutStyle")
        self.style.addItem("Ordinate", "ordinate")
        self.style.addItem("Baseline", "baseline")
        self.view = QComboBox()
        self.view.setObjectName("cutoutView")
        self.view.addItem("View from front", "front")
        self.view.addItem("View from back", "back")
        self.units = QComboBox()
        self.units.setObjectName("cutoutUnits")
        self.units.addItem("Millimetres", "mm")
        self.units.addItem("Inches", "inch")
        form.addRow("Datum", self.datum)
        form.addRow("Dimensioning", self.style)
        form.addRow("View", self.view)
        form.addRow("Dimension units", self.units)
        self.edge = QLineEdit()
        self.edge.setObjectName("cutoutEdge")
        self.edge.setPlaceholderText("blank — warn only if a cut crosses the edge")
        self.flange = QLineEdit()
        self.flange.setObjectName("cutoutFlange")
        self.flange.setPlaceholderText("blank — no flange width on file")
        self.thickness = QLineEdit()
        self.thickness.setObjectName("cutoutThickness")
        self.thickness.setPlaceholderText("blank — panel thickness not checked")
        form.addRow("Edge clearance (mm)", self.edge)
        form.addRow("Flange (mm)", self.flange)
        form.addRow("Panel thickness (mm)", self.thickness)
        settings_box.addLayout(form)

        surfaces = QGridLayout()
        self.surface_boxes: dict[str, QCheckBox] = {}
        for index, name in enumerate(SURFACES):
            box = QCheckBox(surface_title(name))
            box.setChecked(True)
            self.surface_boxes[name] = box
            surfaces.addWidget(box, index // 3, index % 3)
        settings_box.addLayout(surfaces)
        for widget in (self.datum, self.style, self.view, self.units, self.edge, self.flange, self.thickness):
            widget.setMinimumHeight(max(22, widget.sizeHint().height()))
        settings_host.adjustSize()
        host_h = settings_host.sizeHint().height()
        settings_host.setMinimumHeight(host_h)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setObjectName("cutoutSettingsScroll")
        self.settings_scroll.setWidget(settings_host)
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_scroll.setMinimumHeight(140)
        # The form stays its own height. Extra window height goes to the preview.
        self.settings_scroll.setMaximumHeight(host_h + 8)
        self.settings_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        root.addWidget(self.settings_scroll)

        pick = QHBoxLayout()
        pick.addWidget(QLabel("Preview"))
        self.preview_surface = QComboBox()
        pick.addWidget(self.preview_surface, 1)
        self.zoom_fit = QPushButton("Zoom to fit")
        self.zoom_fit.setObjectName("cutoutZoomFit")
        self.zoom_fit.clicked.connect(self.zoom_to_fit)
        pick.addWidget(self.zoom_fit)
        root.addLayout(pick)

        self.preview = QLabel("Generating…")
        self.preview.setObjectName("cutoutPreview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background: white;")
        self.preview_scroll = QScrollArea()
        self.preview_scroll.setWidget(self.preview)
        self.preview_scroll.setWidgetResizable(False)
        self.preview_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_scroll.setMinimumHeight(80)
        self.preview_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.preview_scroll.setStyleSheet("background: white; border: 1px solid #ccc;")
        root.addWidget(self.preview_scroll, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        buttons = QHBoxLayout()
        apply_btn = QPushButton("Apply to design checks")
        print_btn = QPushButton("Print…")
        pdf_btn = QPushButton("Export PDF…")
        dxf_btn = QPushButton("Export DXF…")
        close_btn = QPushButton("Close")
        apply_btn.clicked.connect(self.apply_settings)
        print_btn.clicked.connect(self.print_sheets)
        pdf_btn.clicked.connect(self.export_pdf)
        dxf_btn.clicked.connect(self.export_dxf)
        close_btn.clicked.connect(self.accept)
        for button in (apply_btn, print_btn, pdf_btn, dxf_btn, close_btn):
            buttons.addWidget(button)
        root.addLayout(buttons)

        self._load_project_settings()
        for widget in (self.datum, self.style, self.view, self.units, self.preview_surface):
            widget.currentIndexChanged.connect(self.refresh_preview)
        for box in self.surface_boxes.values():
            box.toggled.connect(self.refresh_preview)
        for field in (self.edge, self.flange, self.thickness):
            field.editingFinished.connect(self.refresh_preview)
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            width, height = dialog_size_for_screen(avail.width(), avail.height())
            self.resize(width, height)
        self.refresh_preview()

    def _load_project_settings(self) -> None:
        from panelcad.core.cutout import settings_from_project

        settings = settings_from_project(self.session.project, None)
        self._select(self.datum, settings.datum)
        self._select(self.style, settings.style)
        self._select(self.view, settings.view)
        self._select(self.units, settings.units)
        if settings.edge_clearance_mm is not None:
            self.edge.setText(f"{settings.edge_clearance_mm:g}")
        if settings.flange_mm is not None:
            self.flange.setText(f"{settings.flange_mm:g}")
        if settings.panel_thickness_mm is not None:
            self.thickness.setText(f"{settings.panel_thickness_mm:g}")

    def _select(self, box: QComboBox, value: str) -> None:
        index = box.findData(value)
        if index >= 0:
            box.setCurrentIndex(index)

    def options(self) -> dict[str, object]:
        surfaces = [name for name in SURFACES if self.surface_boxes[name].isChecked()]
        return {
            "datum": str(self.datum.currentData() or "top_left"),
            "style": str(self.style.currentData() or "ordinate"),
            "view": str(self.view.currentData() or "front"),
            "units": str(self.units.currentData() or "mm"),
            "edge_clearance_mm": _optional_mm(self.edge.text()),
            "flange_mm": _optional_mm(self.flange.text()),
            "panel_thickness_mm": _optional_mm(self.thickness.text()),
            "surfaces": surfaces,
        }

    def refresh_preview(self) -> None:
        try:
            self._drawings = self.session.cutout_drawings(self.options())
        except (ValueError, TypeError) as exc:
            self._drawings = []
            self.preview.setText(str(exc))
            self.status.setText("")
            return
        self._fill_surface_picker()
        drawing = self._current_drawing()
        if drawing is None:
            self.preview.setText("No sheet for the selected surfaces.")
            self.preview.setPixmap(QPixmap())
            self._preview_image = None
            self.status.setText("")
            return
        self._preview_image = render_cutout_qimage(drawing, self.session.project, px_per_mm=2.0)
        self._apply_preview()
        self.status.setText(
            f"{surface_title(drawing.surface)}  {drawing.panel_width_mm:g} × {drawing.panel_height_mm:g} mm  "
            f"scale 1:{drawing.scale_denom:g}  {len(drawing.rows)} table row(s)"
        )

    def zoom_to_fit(self) -> None:
        """Scale the sheet so the whole page, including the title block, stays inside the preview."""

        self._zoom_fit = True
        self._apply_preview()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # __init__ fits before the layout has a viewport. Fit again once shown.
        self._refit_preview()
        QTimer.singleShot(0, self._refit_preview)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._refit_preview()

    def _refit_preview(self) -> None:
        if self._zoom_fit:
            self._apply_preview()

    def _apply_preview(self) -> None:
        if self._preview_image is None:
            return
        pixmap = QPixmap.fromImage(self._preview_image)
        if self._zoom_fit:
            viewport = self.preview_scroll.viewport().size()
            target_w = int(viewport.width())
            target_h = int(viewport.height())
            # The layout has not settled yet. showEvent retries.
            if target_w < 2 or target_h < 2:
                return
            pixmap = pixmap.scaled(
                target_w,
                target_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self.preview.setPixmap(pixmap)
        self.preview.resize(pixmap.size())

    def _fill_surface_picker(self) -> None:
        current = str(self.preview_surface.currentData() or "")
        self.preview_surface.blockSignals(True)
        self.preview_surface.clear()
        for drawing in self._drawings:
            self.preview_surface.addItem(surface_title(drawing.surface), drawing.surface)
        index = self.preview_surface.findData(current)
        if index >= 0:
            self.preview_surface.setCurrentIndex(index)
        self.preview_surface.blockSignals(False)

    def _current_drawing(self) -> SurfaceDrawing | None:
        want = str(self.preview_surface.currentData() or "")
        for drawing in self._drawings:
            if drawing.surface == want:
                return drawing
        return self._drawings[0] if self._drawings else None

    def apply_settings(self) -> None:
        try:
            self.session.cutouts_generate(self.options(), persist=True)
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Cutout drawings", str(exc))
            return
        self.status.setText("Saved datum, view, units, and clearances on the project.")
        self.refresh_preview()

    def print_sheets(self) -> None:
        drawings = self._persist_and_drawings()
        if not drawings:
            return
        from PySide6.QtPrintSupport import QPrintPreviewDialog, QPrinter

        from panelcad.render.cutout_draw import paint_cutouts_on_printer

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        preview = QPrintPreviewDialog(printer, self)
        preview.setWindowTitle("Print cutout drawings")
        preview.paintRequested.connect(lambda device: paint_cutouts_on_printer(device, drawings, self.session.project))
        preview.exec()

    def export_pdf(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export cutout PDF", "cutouts.pdf", "PDF (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        self.export_pdf_to(path)

    def export_pdf_to(self, path: str | Path) -> Path | None:
        drawings = self._persist_and_drawings()
        if not drawings:
            return None
        from panelcad.render.cutout_draw import write_cutout_pdf

        try:
            dest = write_cutout_pdf(path, drawings, self.session.project)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Cutout PDF", str(exc))
            return None
        self.status.setText(f"Wrote {dest}")
        return dest

    def export_dxf(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Export cutout DXF folder")
        if not path:
            return
        self.export_dxf_to(path)

    def export_dxf_to(self, path: str | Path) -> list[Path]:
        if not self._persist_and_drawings():
            return []
        try:
            result = self.session.cutouts_export_dxf(path, self.options())
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Cutout DXF", str(exc))
            return []
        paths = [Path(item) for item in result.get("paths") or []]
        skipped = result.get("skipped") or []
        note = f"Wrote {len(paths)} DXF file(s)"
        if skipped:
            from panelcad.core.cutout import surface_title as _title

            names = ", ".join(_title(name) for name in skipped)
            note += f". Skipped (no openings): {names}"
        self.status.setText(note)
        return paths

    def _persist_and_drawings(self) -> list[SurfaceDrawing]:
        try:
            self.session.cutouts_generate(self.options(), persist=True)
            drawings = self.session.cutout_drawings({"surfaces": self.options().get("surfaces")})
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Cutout drawings", str(exc))
            return []
        if not drawings:
            QMessageBox.information(self, "Cutout drawings", "No sheets for the selected surfaces.")
        return drawings


def _optional_mm(text: str) -> float | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    return float(raw)
