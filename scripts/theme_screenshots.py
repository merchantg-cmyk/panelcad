"""Offscreen theme screenshots at 1920x1080, 1366x768, and 150% of 1080p.

150% is a second process because Qt reads QT_SCALE_FACTOR at startup.
The logical window on a 1920x1080 screen at 150% is 1280x720.
Writes PNGs under /opt/cursor/artifacts/screenshots. Does not start the event loop.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "1")
os.environ["PANELCAD_CONFIG"] = tempfile.mkdtemp(prefix="panelcad-shots-")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from panelcad.examples.dol import build_dol  # noqa: E402
from panelcad.gui.cable_dialog import CableEditorDialog  # noqa: E402
from panelcad.gui.circuit_dialog import CircuitBuilderDialog  # noqa: E402
from panelcad.gui.main_window import MainWindow  # noqa: E402
from panelcad.gui.plc_tag_dialog import PlcTagExportDialog  # noqa: E402
from panelcad.gui.revision_dialog import IssueRevisionDialog, RevisionHistoryDialog  # noqa: E402
from panelcad.gui.sccr_dialog import SccrDialog  # noqa: E402
from panelcad.gui.settings_dialog import SettingsDialog  # noqa: E402
from panelcad.gui.shortcuts_dialog import ShortcutsDialog  # noqa: E402
from panelcad.gui.thermal_dialog import ThermalDialog  # noqa: E402
from panelcad.gui.wire_dialogs import FromToDialog, WireMarkerDialog  # noqa: E402
from panelcad.gui import theme as gui_theme  # noqa: E402
from panelcad.theme import (  # noqa: E402
    CANVASES,
    CONDUCTOR_TRUE,
    canvas_hex,
    remember_project,
    screen_conductor,
    set_canvas,
    set_theme,
)

OUT = Path("/opt/cursor/artifacts/screenshots")
_PASS = os.environ.get("PANELCAD_SHOT_PASS", "100")
SIZES = ((1280, 720),) if _PASS == "150" else ((1920, 1080), (1366, 768))
_SUFFIX = "-150" if _PASS == "150" else ""


def main() -> None:
    if _PASS == "100":
        _capture()
        env = os.environ.copy()
        env["QT_SCALE_FACTOR"] = "1.5"
        env["PANELCAD_SHOT_PASS"] = "150"
        env["QT_QPA_PLATFORM"] = "offscreen"
        subprocess.check_call([sys.executable, str(Path(__file__).resolve())], env=env)
        print(f"wrote {len(list(OUT.glob('*.png')))} pngs to {OUT}", flush=True)
        os._exit(0)
    _capture()
    os._exit(0)


def _capture() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # Keep the requested pixel size. The dialog clamp is covered by unit tests.
    gui_theme.clamp_widget_to_screen = lambda widget: None  # type: ignore[assignment]
    app = QApplication.instance() or QApplication(sys.argv)
    session = build_dol()
    window = MainWindow(session)
    window.show()
    app.processEvents()

    for theme in ("light", "dark"):
        set_theme(theme)
        _banner(window, "neutral")
        for canvas in CANVASES:
            set_canvas(canvas)
            _sheet(window, "power")
            for width, height in SIZES:
                _grab(app, window, width, height, f"{theme}-{canvas}-schematic-{width}x{height}{_SUFFIX}.png")
            _sheet(window, "enclosure")
            for width, height in SIZES:
                _grab(app, window, width, height, f"{theme}-{canvas}-panel-{width}x{height}{_SUFFIX}.png")
        _raise_inspector(window)
        for width, height in SIZES:
            _grab(app, window, width, height, f"{theme}-inspector-{width}x{height}{_SUFFIX}.png")
        set_canvas("paper")
        _sheet(window, "power")
        _raise_bom(window)
        for width, height in SIZES:
            _grab(app, window, width, height, f"{theme}-bom-{width}x{height}{_SUFFIX}.png")
        for state in ("neutral", "working", "error", "idle", "warning"):
            _banner(window, state)
            _raise_ai(window)
            for width, height in SIZES:
                _grab(app, window, width, height, f"{theme}-ai-{state}-{width}x{height}{_SUFFIX}.png")
        _raise_power(window)
        for width, height in SIZES:
            _grab(app, window, width, height, f"{theme}-power-{width}x{height}{_SUFFIX}.png")
        for width, height in SIZES:
            _dialog(app, SettingsDialog(), width, height, f"{theme}-settings-{width}x{height}{_SUFFIX}.png")
            _dialog(
                app,
                ShortcutsDialog(window),
                width,
                height,
                f"{theme}-shortcuts-{width}x{height}{_SUFFIX}.png",
            )
            _dialog(
                app,
                CircuitBuilderDialog(standard="IEC"),
                width,
                height,
                f"{theme}-circuit-{width}x{height}{_SUFFIX}.png",
            )
            _dialog(app, SccrDialog(session), width, height, f"{theme}-sccr-{width}x{height}{_SUFFIX}.png")
            _dialog(app, ThermalDialog(session), width, height, f"{theme}-thermal-{width}x{height}{_SUFFIX}.png")
            _dialog(app, FromToDialog(suggested="from_to.csv"), width, height, f"{theme}-wires-{width}x{height}{_SUFFIX}.png")
            _dialog(
                app,
                WireMarkerDialog(suggested="wire_markers.csv"),
                width,
                height,
                f"{theme}-markers-{width}x{height}{_SUFFIX}.png",
            )
            _dialog(app, CableEditorDialog(session), width, height, f"{theme}-cables-{width}x{height}{_SUFFIX}.png")
            _dialog(app, PlcTagExportDialog(suggested="plc-tags.csv"), width, height, f"{theme}-plc-{width}x{height}{_SUFFIX}.png")
            _dialog(app, IssueRevisionDialog(session), width, height, f"{theme}-revision-{width}x{height}{_SUFFIX}.png")
            _dialog(
                app,
                RevisionHistoryDialog(session),
                width,
                height,
                f"{theme}-revision-history-{width}x{height}{_SUFFIX}.png",
            )
        for width, height in SIZES:
            _start(app, theme, width, height, f"{theme}-start-{width}x{height}{_SUFFIX}.png")

    _phase_sheet(app, window, "NEMA/ANSI", "Motor power circuit", "nema")
    _phase_sheet(app, window, "NEMA/ANSI", "480 V motor power", "nema480")
    if _PASS == "100":
        _swatch(OUT / "conductors-swatch.png")
    window.close()
    app.processEvents()
    print(f"pass {_PASS} wrote into {OUT}")


def _sheet(window: MainWindow, sheet_id: str) -> None:
    window.session.set_active_sheet(sheet_id)
    window.canvas.reload()
    window.sheet_bar.refresh()


def _grab(app: QApplication, window: MainWindow, width: int, height: int, name: str) -> None:
    window.resize(width, height)
    window.show()
    app.processEvents()
    window.canvas.fit_drawing()
    app.processEvents()
    image = window.grab().toImage()
    image = _match_size(image, width, height)
    image.save(str(OUT / name))
    print(f"{name}  {image.width()}x{image.height()}")


def _match_size(image: QImage, width: int, height: int) -> QImage:
    """At 100% the grab should match the requested pixels. At 150% keep device pixels."""

    if _PASS == "150":
        return image
    if image.width() == width and image.height() == height:
        return image
    return image.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)


def _raise_inspector(window: MainWindow) -> None:
    window.prop_wrap.show()
    window.prop_wrap.raise_()
    window.resizeDocks([window.prop_wrap], [280], Qt.Orientation.Horizontal)
    components = list(window.session.sheet().components)
    if components:
        window.canvas.select_ref(components[0].id)


def _raise_bom(window: MainWindow) -> None:
    window.bom_wrap.show()
    window.bom_wrap.raise_()
    window.resizeDocks([window.bom_wrap], [320], Qt.Orientation.Vertical)


def _raise_power(window: MainWindow) -> None:
    window.power_wrap.show()
    window.power_wrap.raise_()
    window.resizeDocks([window.power_wrap], [320], Qt.Orientation.Vertical)


def _raise_ai(window: MainWindow) -> None:
    window.ai_wrap.show()
    window.ai_wrap.raise_()
    window.resizeDocks([window.prop_wrap, window.ai_wrap], [460, 460], Qt.Orientation.Horizontal)


def _banner(window: MainWindow, state: str) -> None:
    dock = window.ai_dock
    dock.log.clear()
    dock.model_name = ""
    dock.status_name = "idle"
    dock.banner.setText("No AI connected")
    if state == "neutral":
        dock.apply_theme()
        return
    if state == "warning":
        dock.model_name = "Grok 4.6"
        dock.status_name = "warning"
        dock.banner.setText("Catalog match is partial — confirm the part number")
        dock.apply_theme()
        return
    event = {
        "t": "2026-10-08T12:00:00",
        "op": "place",
        "summary": "placed K1 on Power",
        "status": state,
        "sheet_title": "Power",
        "sheet_kind": "schematic",
        "actor": {"kind": "ai", "model": "Grok 4.6", "name": "Grok"},
    }
    if state == "error":
        event["error"] = "pin K1.L9 is not on this symbol"
        event["summary"] = "wire failed"
    dock.apply_event(event)


def _dialog(app: QApplication, dialog, width: int, height: int, name: str) -> None:
    if _PASS == "150":
        # A 1080p screen at 150% is 1280x672 logical. Show the dialog inside that.
        dialog.show()
        app.processEvents()
        gui_theme.fit_widget_to_box(dialog, 0, 0, 1280, 672)
        dialog.resize(1100, 620)
    else:
        dialog.resize(width, height)
        dialog.show()
    app.processEvents()
    image = _match_size(dialog.grab().toImage(), width, height)
    dialog.close()
    image.save(str(OUT / name))
    print(f"{name}  {image.width()}x{image.height()}")


def _start(app: QApplication, theme: str, width: int, height: int, name: str) -> None:
    set_theme(theme)
    remember_project(r"C:\jobs\riverside-pump.pcad.yaml")
    remember_project(r"C:\jobs\mcc-2.pcad.yaml")
    window = MainWindow(None, show_start=True)
    window.show()
    app.processEvents()
    window.resize(width, height)
    app.processEvents()
    image = window.grab().toImage()
    window.close()
    image = _match_size(image, width, height)
    image.save(str(OUT / name))
    print(f"{name}  {image.width()}x{image.height()}")


def _phase_sheet(app: QApplication, window: MainWindow, standard: str, description: str, tag: str) -> None:
    window.session.set_title_block(standard=standard, description=description)
    props = window.session.project.properties
    if tag == "nema480":
        props["voltage"] = 480
    else:
        props.pop("voltage", None)
    _sheet(window, "power")
    width, height = SIZES[0]
    for theme in ("light", "dark"):
        set_theme(theme)
        for canvas in ("paper", "dark"):
            set_canvas(canvas)
            _sheet(window, "power")
            _grab(app, window, width, height, f"{theme}-{canvas}-{tag}-{width}x{height}{_SUFFIX}.png")
    props.pop("voltage", None)
    window.session.set_title_block(standard="IEC", description="Motor power circuit")


def _swatch(path: Path) -> None:
    names = list(CONDUCTOR_TRUE)
    cell_w, cell_h = 140, 36
    width = 80 + cell_w * len(CANVASES)
    height = 36 + cell_h * len(names)
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    painter.setFont(QFont("Sans Serif", 9))
    for index, canvas in enumerate(CANVASES):
        painter.setPen(QColor("#111111"))
        painter.drawText(80 + index * cell_w, 22, canvas)
    for row, name in enumerate(names):
        painter.setPen(QColor("#111111"))
        painter.drawText(8, 36 + row * cell_h + 22, name)
        for index, canvas in enumerate(CANVASES):
            paper = canvas_hex("paper", canvas)
            core, outline = screen_conductor(name, canvas)
            x = 80 + index * cell_w
            y = 36 + row * cell_h
            painter.fillRect(x, y, cell_w - 8, cell_h - 6, QColor(paper))
            if outline:
                painter.setPen(QPen(QColor(outline), 5))
                painter.drawLine(x + 16, y + 15, x + cell_w - 24, y + 15)
            painter.setPen(QPen(QColor(core), 3))
            painter.drawLine(x + 16, y + 15, x + cell_w - 24, y + 15)
    painter.end()
    image.save(str(path))
    print(f"{path.name}  {image.width()}x{image.height()}")


if __name__ == "__main__":
    main()
