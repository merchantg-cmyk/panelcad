"""Theme tokens, contrast, live appearance, and export independence."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from panelcad.theme import (
    CANVAS,
    CANVAS_TEXT_PAIRS,
    CANVASES,
    CONDUCTOR_TRUE,
    MIN_GRAPHIC_CONTRAST,
    MIN_TEXT_CONTRAST,
    UI,
    UI_GRAPHIC_PAIRS,
    UI_TEXT_PAIRS,
    adapt_print_color,
    canvas_hex,
    contrast_hex,
    get_canvas,
    get_theme,
    load_settings,
    phase_color_name,
    remember_project,
    resolved_theme,
    screen_conductor,
    set_canvas,
    set_theme,
)

_HEX = re.compile(r"#[0-9A-Fa-f]{3,8}\b")
_GUI = Path(__file__).resolve().parents[1] / "src" / "panelcad" / "gui"


def test_text_contrast_meets_wcag_aa():
    """WCAG 2.2 SC 1.4.3 — normal text at least 4.5:1."""

    failures = []
    for theme, palette in UI.items():
        for fg, bg in UI_TEXT_PAIRS:
            ratio = contrast_hex(palette[fg], palette[bg])
            if ratio < MIN_TEXT_CONTRAST:
                failures.append(f"{theme} {fg} on {bg} = {ratio:.2f}")
    for canvas, palette in CANVAS.items():
        for fg, bg in CANVAS_TEXT_PAIRS:
            ratio = contrast_hex(palette[fg], palette[bg])
            if ratio < MIN_TEXT_CONTRAST:
                failures.append(f"{canvas} {fg} on {bg} = {ratio:.2f}")
    for theme, palette in UI.items():
        for fg, bg in UI_GRAPHIC_PAIRS:
            ratio = contrast_hex(palette[fg], palette[bg])
            if ratio < MIN_GRAPHIC_CONTRAST:
                failures.append(f"{theme} graphic {fg} on {bg} = {ratio:.2f}")
    assert failures == []


def test_conductors_stay_distinct_on_every_canvas():
    for canvas in CANVASES:
        paper = canvas_hex("paper", canvas)
        seen = {}
        for name in CONDUCTOR_TRUE:
            core, outline = screen_conductor(name, canvas)
            best = contrast_hex(core, paper)
            if outline:
                best = max(best, contrast_hex(outline, paper))
            assert best >= MIN_GRAPHIC_CONTRAST, f"{name} on {canvas} contrast {best:.2f}"
            assert core.lower() != paper.lower()
            seen[name] = core.lower()
        assert seen["black"] != seen["grey"]


def test_phase_colors_follow_standard_and_explicit_color():
    assert phase_color_name("", "L1", standard="IEC") == "brown"
    assert phase_color_name("", "L2", standard="IEC") == "black"
    assert phase_color_name("", "L3", standard="IEC") == "grey"
    assert phase_color_name("", "T1", standard="IEC") == "brown"
    assert phase_color_name("", "U1", standard="IEC") == "brown"
    assert phase_color_name("", "L1", standard="NEMA/ANSI") == "black"
    assert phase_color_name("", "L2", standard="NEMA/ANSI") == "red"
    assert phase_color_name("", "L3", standard="NEMA/ANSI") == "blue"
    assert phase_color_name("", "L1", standard="NEMA/ANSI", voltage=480) == "brown"
    assert phase_color_name("", "L2", standard="NEMA/ANSI", voltage=480) == "orange"
    assert phase_color_name("", "L3", standard="NEMA/ANSI", voltage=480) == "yellow"
    assert phase_color_name("red", "L1", standard="IEC") == "red"
    assert phase_color_name("", "RUN") == ""


def test_match_windows_falls_back_to_light_when_scheme_is_unknown():
    assert resolved_theme("system", system_dark=None) == "light"
    assert resolved_theme("system", system_dark=False) == "light"
    assert resolved_theme("system", system_dark=True) == "dark"


def test_adapt_print_color_is_identity_on_paper_and_readable_on_dark():
    assert adapt_print_color("#111111", "paper") == "#111111"
    assert adapt_print_color("#f7f4ea", "white") == "#ffffff"
    assert adapt_print_color("#0b4ea2", "white") == "#0b4ea2"
    ink = adapt_print_color("#111111", "dark")
    assert contrast_hex(ink, canvas_hex("paper", "dark")) >= MIN_GRAPHIC_CONTRAST


# Files this theme owns. A new GUI module from another PR is not a failure
# until it is added here. TODO: new GUI modules should use theme tokens
# and join this set.
_HEX_BASELINE = frozenset(
    {
        "__init__.py",
        "ai_dock.py",
        "app.py",
        "bom_dock.py",
        "cable_dialog.py",
        "canvas.py",
        "circuit_dialog.py",
        "docks.py",
        "enclosure_dialog.py",
        "library_editor.py",
        "main_window.py",
        "object_editor.py",
        "package_dialog.py",
        "part_picker.py",
        "plc_tag_dialog.py",
        "power_dock.py",
        "problems.py",
        "revision_dialog.py",
        "sccr_dialog.py",
        "settings_dialog.py",
        "sheet_bar.py",
        "shortcuts_dialog.py",
        "start_page.py",
        "strip_canvas.py",
        "style_dialog.py",
        "symbol_canvas.py",
        "symbol_draw.py",
        "terminal_dialog.py",
        "thermal_dialog.py",
        "wire_dialogs.py",
    }
)


def test_gui_sources_have_no_hex_outside_the_theme_module():
    bad = []
    for path in sorted(_GUI.rglob("*.py")):
        if path.name == "theme.py" or path.name not in _HEX_BASELINE:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _HEX.search(line):
                bad.append(f"{path.name}:{number}: {line.strip()}")
    assert bad == []


def test_settings_persist_and_recent_projects_do_not_notify():
    set_theme("dark")
    set_canvas("white")
    assert get_theme() == "dark"
    assert get_canvas() == "white"
    saved = load_settings()
    assert saved["theme"] == "dark"
    assert saved["canvas"] == "white"
    remember_project("C:/jobs/one.pcad.yaml")
    assert load_settings()["recent"][0].endswith("one.pcad.yaml")
    set_theme("light")
    set_canvas("paper")


def test_agent_and_session_set_theme_and_canvas():
    from panelcad.api.session import Session
    from panelcad.cli.agent import Agent

    agent = Agent()
    theme = agent.handle({"id": "req-theme", "op": "ui_set_theme", "theme": "dark"})
    assert theme["ok"] is True
    assert theme["result"]["theme"] == "dark"
    assert theme["result"]["resolved_theme"] == "dark"
    canvas = agent.handle({"id": "req-canvas", "op": "ui_set_canvas", "canvas": "dark"})
    assert canvas["ok"] is True
    assert canvas["result"]["canvas"] == "dark"
    bad = agent.handle({"id": "req-bad", "op": "ui_set_theme", "theme": "blue"})
    assert bad["ok"] is False
    listed = agent.handle({"op": "help"})
    assert "ui_set_theme" in listed["result"]["ops"]
    assert "ui_set_canvas" in listed["result"]["help"]

    session = Session.create("Appearance")
    assert session.ui_set_theme("system")["theme"] == "system"
    assert session.ui_set_canvas("paper")["canvas"] == "paper"


def test_cli_settings_set_theme(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path / "cli-config"))
    result = CliRunner().invoke(app, ["settings", "set", "theme", "dark"])
    assert result.exit_code == 0, result.output
    assert get_theme() == "dark"
    shown = CliRunner().invoke(app, ["settings", "show"])
    assert shown.exit_code == 0
    assert "dark" in shown.output


def test_exports_match_across_themes(tmp_path):
    from panelcad.examples.dol import build_dol

    session = build_dol()
    set_theme("light")
    set_canvas("paper")
    session.export_svg(tmp_path / "a.svg", sheet_id="power")
    session.export_dxf(tmp_path / "a.dxf", sheet_id="power")
    session.export_pdf(tmp_path / "a.pdf", sheet_id="power")
    set_theme("dark")
    set_canvas("dark")
    session.export_svg(tmp_path / "b.svg", sheet_id="power")
    session.export_dxf(tmp_path / "b.dxf", sheet_id="power")
    session.export_pdf(tmp_path / "b.pdf", sheet_id="power")
    assert (tmp_path / "a.svg").read_bytes() == (tmp_path / "b.svg").read_bytes()
    assert _stable_dxf((tmp_path / "a.dxf").read_bytes()) == _stable_dxf((tmp_path / "b.dxf").read_bytes())
    assert _stable_pdf((tmp_path / "a.pdf").read_bytes()) == _stable_pdf((tmp_path / "b.pdf").read_bytes())


def test_clamp_rect_keeps_a_dialog_inside_the_screen():
    from panelcad.gui.theme import clamp_rect

    assert clamp_rect(10, 12, 100, 80, 0, 0, 200, 200) == (10, 12, 100, 80)
    assert clamp_rect(-30, 12, 100, 80, 0, 0, 200, 200)[0] == 0
    left, top, width, height = clamp_rect(180, 190, 100, 80, 0, 0, 200, 200)
    assert left + width <= 200
    assert top + height <= 200
    assert (width, height) == (100, 80)
    left, top, width, height = clamp_rect(0, 0, 900, 700, 10, 20, 640, 480)
    assert (left, top, width, height) == (10, 20, 640, 480)


def test_toolbar_icon_is_crisp_at_one_and_a_half_scale():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.theme import render_svg_icon

    QApplication.instance() or QApplication([])
    pixmap = render_svg_icon("save", 24, 1.5)
    assert pixmap.width() == 36
    assert abs(pixmap.devicePixelRatio() - 1.5) < 0.01
    image = pixmap.toImage()
    opaque = False
    for x in range(0, image.width(), 2):
        for y in range(0, image.height(), 2):
            if image.pixelColor(x, y).alpha() > 10:
                opaque = True
                break
        if opaque:
            break
    assert opaque


def test_main_window_applies_dark_theme_live():
    from PySide6.QtWidgets import QApplication

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow
    from panelcad.theme import ui_hex

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Session.create("Theme"))
    set_theme("dark")
    set_canvas("dark")
    app.processEvents()
    assert ui_hex("window", "dark") in app.styleSheet()
    assert window.canvas.backgroundBrush().color().name().lower() == canvas_hex("paper", "dark")
    assert window._theme_actions["dark"].isChecked()
    assert window._canvas_actions["dark"].isChecked()
    window.close()


def test_drawing_tools_include_theme_ops():
    from panelcad.ai.drawing import ui_set_canvas, ui_set_phase_colors, ui_set_theme

    assert callable(ui_set_theme)
    assert callable(ui_set_canvas)
    assert callable(ui_set_phase_colors)


def test_settings_cache_skips_disk_and_keeps_unknown_keys(tmp_path, monkeypatch):
    import json

    from panelcad.settings import invalidate, settings_path

    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    invalidate()
    path = settings_path()
    path.write_text(
        json.dumps(
            {
                "theme": "light",
                "canvas": "paper",
                "recent": [],
                "slack_webhook": "https://hooks.example/abc",
                "check_updates": True,
            }
        ),
        encoding="utf-8",
    )
    invalidate()
    set_theme("dark")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["slack_webhook"] == "https://hooks.example/abc"
    assert saved["check_updates"] is True
    assert saved["theme"] == "dark"
    calls = {"n": 0}
    real = Path.read_text

    def wrapped(self, *args, **kwargs):
        if self.name == "settings.json":
            calls["n"] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", wrapped)
    for _ in range(40):
        from panelcad.theme import ui_hex as lookup

        lookup("text")
        canvas_hex("ink")
    assert calls["n"] == 0
    set_theme("light")
    set_canvas("paper")


def test_auto_canvas_follows_the_theme():
    set_theme("light")
    set_canvas("auto")
    assert get_canvas() == "paper"
    set_theme("dark")
    assert get_canvas() == "dark"
    set_canvas("paper")
    set_theme("light")


def test_rendered_chrome_meets_contrast_in_both_themes():
    """Inspector, dock titles, checkboxes, and spin arrows, as Qt paints them."""

    from PySide6.QtWidgets import QApplication, QCheckBox, QSpinBox

    from panelcad.examples.dol import build_dol
    from panelcad.gui.main_window import MainWindow
    from panelcad.gui.theme import install

    app = QApplication.instance() or QApplication([])
    install()
    window = MainWindow(build_dol())
    window.resize(1280, 800)
    window.show()
    ref = window.session.sheet().components[0].id
    window.canvas.select_ref(ref)
    checkbox = QCheckBox("Outdoor")
    checkbox.resize(160, 28)
    spin = QSpinBox()
    spin.resize(96, 28)
    try:
        for theme in ("light", "dark"):
            set_theme(theme)
            set_canvas("paper" if theme == "light" else "dark")
            app.processEvents()
            assert ref in window.canvas.selected_refs()
            assert window._theme_actions[theme].isChecked()
            _assert_inspector(window, theme)
            _assert_dock_title(window, theme)
            checkbox.show()
            spin.show()
            app.processEvents()
            _assert_checkbox(checkbox, theme)
            _assert_spin_arrows(spin, theme)
    finally:
        checkbox.close()
        spin.close()
        window.close()
        set_theme("light")
        set_canvas("paper")


def test_main_window_minimum_fits_a_1280_logical_screen():
    from PySide6.QtWidgets import QApplication

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow
    from panelcad.gui.theme import fit_widget_to_box

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Session.create("Fit"))
    window.show()
    app.processEvents()
    assert window.minimumWidth() <= 1280
    assert window.minimumHeight() <= 720
    from panelcad.gui.settings_dialog import SettingsDialog

    dialog = SettingsDialog()
    fit_widget_to_box(dialog, 0, 0, 1280, 672)
    assert dialog.minimumWidth() <= 1280
    assert dialog.minimumHeight() <= 672
    dialog.close()
    window.close()


def test_start_page_recent_names_stay_below_the_cards():
    from PySide6.QtWidgets import QApplication, QToolButton

    from panelcad.gui.main_window import MainWindow
    from panelcad.gui.start_page import _ElideLabel, _RecentRow
    from panelcad.theme import remember_project

    app = QApplication.instance() or QApplication([])
    remember_project(r"C:\jobs\riverside-pump.pcad.yaml")
    remember_project(r"C:\jobs\mcc-2.pcad.yaml")
    window = MainWindow(None, show_start=True)
    window.resize(1366, 768)
    window.show()
    app.processEvents()
    start = window._start
    buttons = start.findChildren(QToolButton)
    rows = start.findChildren(_RecentRow)
    assert len(buttons) >= 5
    assert len(rows) == 2
    card_bottom = max(button.mapTo(start, button.rect().bottomLeft()).y() for button in buttons)
    names = []
    for row in rows:
        top = row.mapTo(start, row.rect().topLeft()).y()
        assert top >= card_bottom
        assert row.width() <= 960
        labels = row.findChildren(_ElideLabel)
        names.append(labels[0]._full)
        assert "\\" not in labels[0]._full
    assert names == ["mcc-2.pcad", "riverside-pump.pcad"] or set(names) == {
        "mcc-2.pcad",
        "riverside-pump.pcad",
    }
    window.close()


def _shown_dock_visibility(window) -> dict[str, bool]:
    from PySide6.QtWidgets import QDockWidget

    docks = window.findChildren(QDockWidget)
    assert docks
    assert all(dock.objectName() for dock in docks)
    return {dock.objectName(): dock.isVisible() for dock in docks}


def test_start_page_hides_every_dock_without_a_saved_layout():
    """A first launch has no dock layout. Quote is hidden with the others."""

    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(None, show_start=True)
    window.resize(1280, 800)
    window.show()
    app.processEvents()
    visible = _shown_dock_visibility(window)
    assert "quoteDock" in visible
    assert not any(visible.values())
    window.close()


def test_start_page_hides_docks_and_the_editor_restores_the_saved_layout(tmp_path):
    """Closing the editor must not put tool docks back on the next welcome page.

    Opening a project from that page applies the layout saved at editor close.
    """

    from PySide6.QtWidgets import QApplication

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    editor = MainWindow(Session.create("Layout"))
    editor.show()
    app.processEvents()
    editor.lib_wrap.hide()
    app.processEvents()
    saved = _shown_dock_visibility(editor)
    assert saved["libraryDock"] is False
    assert any(saved.values())
    editor.close()
    app.processEvents()

    start = MainWindow(None, show_start=True)
    start.resize(1280, 800)
    start.show()
    app.processEvents()
    welcome = _shown_dock_visibility(start)
    assert not any(welcome.values())

    path = tmp_path / "job.pcad.yaml"
    Session.create("Opened").save(path)
    start.open_path(str(path))
    app.processEvents()
    assert _shown_dock_visibility(start) == saved
    start.close()


def test_closing_the_welcome_page_keeps_the_saved_editor_layout():
    """Quitting from the welcome page must not store the hidden-dock state."""

    from PySide6.QtWidgets import QApplication

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    editor = MainWindow(Session.create("Layout"))
    editor.show()
    app.processEvents()
    editor.lib_wrap.hide()
    app.processEvents()
    editor.close()
    app.processEvents()

    start = MainWindow(None, show_start=True)
    start.show()
    app.processEvents()
    assert not any(_shown_dock_visibility(start).values())
    start.close()
    app.processEvents()

    again = MainWindow(Session.create("Again"))
    again.show()
    app.processEvents()
    visible = _shown_dock_visibility(again)
    assert visible["libraryDock"] is False
    assert any(visible.values())
    again.close()


def _bottom_dock_tab_bars(window) -> list[set[str]]:
    from PySide6.QtWidgets import QTabBar

    sheet = getattr(window.sheet_bar, "tabs", None)
    found: list[set[str]] = []
    for bar in window.findChildren(QTabBar):
        if bar is sheet or not bar.isVisible():
            continue
        titles = {bar.tabText(index).replace("&", "") for index in range(bar.count())}
        titles.discard("")
        if titles & {"BOM", "Problems", "Power", "Quote"} or any(
            title.startswith("Problems") or title.startswith("Quote") for title in titles
        ):
            if "BOM" in titles:
                found.append(titles)
    return found


def test_restored_quote_tab_leaves_one_bottom_dock_bar():
    """A saved BOM|Problems|Power|Quote group must not keep the earlier tab bar."""

    from PySide6.QtWidgets import QApplication

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    editor = MainWindow(Session.create("Docks"))
    editor.resize(1400, 900)
    editor.show()
    app.processEvents()
    editor.tabifyDockWidget(editor.bom_wrap, editor.problems_wrap)
    editor.tabifyDockWidget(editor.bom_wrap, editor.power_wrap)
    editor.tabifyDockWidget(editor.bom_wrap, editor.quote_wrap)
    app.processEvents()
    editor.close()
    app.processEvents()

    again = MainWindow(Session.create("Again"))
    again.resize(1400, 900)
    again.show()
    app.processEvents()
    bars = _bottom_dock_tab_bars(again)
    assert len(bars) == 1, bars
    titles = bars[0]
    assert "BOM" in titles
    assert "Power" in titles
    assert "Quote" in titles
    assert any(title == "Problems" or title.startswith("Problems") for title in titles)
    again.close()


@pytest.mark.confirm_generic_layout
def test_editor_ladder_card_draws_the_point_map():
    """The on-screen PLC card shows addresses, descriptions, and the SEE cross-reference."""

    from PySide6.QtWidgets import QApplication

    from panelcad.api.session import Session
    from panelcad.gui import canvas as canvas_mod
    from panelcad.gui.canvas import ComponentItem
    from panelcad.gui.main_window import MainWindow
    from panelcad.theme import MIN_TEXT_CONTRAST, canvas_hex, contrast_hex, set_canvas

    for mode in ("paper", "white", "dark"):
        ink = canvas_hex("ink", mode)
        for role in ("ladder_fill", "ladder_header"):
            assert contrast_hex(ink, canvas_hex(role, mode)) >= MIN_TEXT_CONTRAST

    session = Session.create("IO")
    session.io_add_module("ab.5069_ib16", ref="IO1", slot=1)
    session.io_assign_point("IO1", "I00", description="Conveyor start")
    session.io_assign_point("IO1", "I01", description="Conveyor stop")
    session.io_generate_sheets()
    card = session.project.sheet("io-5069-ib16").component("IO1")
    assert card.properties.get("ladder_card") is True
    assert card.properties.get("placeholder_footprint") is True
    # No published body — the module is not copied onto a rack sheet.
    assert not card.properties.get("xref_rack")
    xref_rack = str(card.properties.get("xref_rack") or "")

    app = QApplication.instance() or QApplication([])
    window = MainWindow(session)
    window.show()
    app.processEvents()
    window._switch_sheet("io-5069-ib16")
    app.processEvents()
    calls: list[dict] = []
    original = canvas_mod.draw_world_text

    def capture(painter, x, y, text, size, align="middle", rotate=0, *, bold=False, color=None):
        calls.append(
            {"text": str(text), "y": float(y), "color": None if color is None else str(color)}
        )
        return original(painter, x, y, text, size, align, rotate, bold=bold, color=color)

    def card():
        return next(
            child
            for child in window.canvas.scene.items()
            if (
                isinstance(child, ComponentItem)
                and child.component_id == "IO1"
                and child.ladder_card
            )
        )

    def paint_card(mode: str):
        from PySide6.QtGui import QColor, QImage, QPainter

        set_canvas(mode)
        app.processEvents()
        item = card()
        calls.clear()
        image = QImage(
            max(1, int(item._w * 4)), max(1, int(item._h * 4)), QImage.Format.Format_RGB32
        )
        image.fill(QColor(canvas_hex("ladder_fill", mode)))
        painter = QPainter(image)
        painter.scale(4, 4)
        item.paint(painter, None)
        painter.end()
        return item, image, list(calls)

    canvas_mod.draw_world_text = capture
    try:
        item, image, painted = paint_card("paper")
        header = 11.0 * item._scale
        assert any("Local:1:I.Pt00.Data" in row["text"] and row["y"] > header for row in painted)
        assert any(row["text"] == "Conveyor start" and row["y"] > header for row in painted)
        assert any(row["text"] == "Conveyor stop" and row["y"] > header for row in painted)
        if xref_rack:
            assert any(row["text"] == f"SEE {xref_rack}" and row["y"] > header for row in painted)
        paper_ink = str(canvas_hex("ink", "paper"))
        assert all(row["color"] == paper_ink for row in painted)
        from PySide6.QtGui import QColor

        ink_color = QColor(paper_ink)
        fill_color = QColor(canvas_hex("ladder_fill", "paper"))

        def _dist(pixel: QColor, other: QColor) -> int:
            return (
                abs(pixel.red() - other.red())
                + abs(pixel.green() - other.green())
                + abs(pixel.blue() - other.blue())
            )

        ink_pixels = 0
        for y in range(int(header * 4) + 2, image.height()):
            for x in range(image.width()):
                pixel = QColor.fromRgb(image.pixel(x, y))
                # Antialiased glyphs are blends. Count body pixels closer to ink than to the fill.
                if _dist(pixel, ink_color) + 30 < _dist(pixel, fill_color):
                    ink_pixels += 1
        assert ink_pixels > 40
        _item, _image, dark_painted = paint_card("dark")
        assert any("Local:1:I.Pt01.Data" in row["text"] for row in dark_painted)
        assert all(row["color"] == str(canvas_hex("ink", "dark")) for row in dark_painted)
    finally:
        canvas_mod.draw_world_text = original
        set_canvas("paper")
        window.close()


def test_external_settings_write_checks_the_theme_menu():
    import json

    from PySide6.QtWidgets import QApplication

    from panelcad.api.session import Session
    from panelcad.gui import theme as gui_theme
    from panelcad.gui.main_window import MainWindow
    from panelcad.settings import settings_path

    app = QApplication.instance() or QApplication([])
    set_theme("light")
    set_canvas("paper")
    window = MainWindow(Session.create("Watch"))
    window.show()
    app.processEvents()
    path = settings_path()
    path.write_text(
        json.dumps({"theme": "dark", "canvas": "dark", "recent": [], "phase_colors": True}),
        encoding="utf-8",
    )
    assert gui_theme._hooks is not None
    gui_theme._hooks._on_settings_file(str(path))
    app.processEvents()
    assert window._theme_actions["dark"].isChecked()
    assert get_theme() == "dark"
    window.close()
    set_theme("light")
    set_canvas("paper")


def _hex(color) -> str:
    return f"#{color.red():02x}{color.green():02x}{color.blue():02x}"


def _near(color, hex_color: str, tol: int = 56) -> bool:
    raw = hex_color.lstrip("#")
    red, green, blue = int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    return abs(color.red() - red) <= tol and abs(color.green() - green) <= tol and abs(color.blue() - blue) <= tol


def _assert_inspector(window, theme: str) -> None:
    from PySide6.QtWidgets import QLabel

    from panelcad.theme import ui_hex

    window.prop_wrap.show()
    window.prop_wrap.raise_()
    label = next(item for item in window.props.findChildren(QLabel) if item.text().strip())
    scroll = window.prop_wrap.widget()
    viewport = scroll.viewport()
    image = viewport.grab().toImage()
    origin = label.mapTo(viewport, label.rect().topLeft())
    pixels = []
    for y in range(max(0, origin.y()), min(image.height(), origin.y() + label.height())):
        for x in range(max(0, origin.x()), min(image.width(), origin.x() + min(label.width(), 180))):
            color = image.pixelColor(x, y)
            if color.alpha() > 200:
                pixels.append(color)
    assert pixels, "inspector grab was empty"
    background = ui_hex("window", theme)
    text = ui_hex("text", theme)
    assert any(_near(color, text, 70) for color in pixels), theme
    assert any(_near(color, background, 40) or _near(color, ui_hex("panel", theme), 40) for color in pixels)
    samples = [color for color in pixels if _near(color, text, 70)]
    backs = [color for color in pixels if _near(color, background, 40) or _near(color, ui_hex("panel", theme), 40)]
    assert contrast_hex(_hex(samples[0]), _hex(backs[0])) >= MIN_TEXT_CONTRAST


def _assert_dock_title(window, theme: str) -> None:
    from panelcad.theme import ui_hex

    window.lib_wrap.show()
    image = window.lib_wrap.grab().toImage()
    assert image.height() > 8
    band = []
    for y in range(0, min(22, image.height())):
        for x in range(8, min(image.width() - 8, 160)):
            color = image.pixelColor(x, y)
            if color.alpha() > 200:
                band.append(color)
    assert band
    title = ui_hex("dock_title", theme)
    ink = ui_hex("dock_title_text", theme)
    assert any(_near(color, title, 36) for color in band), theme
    assert any(_near(color, ink, 70) for color in band), theme
    backs = [color for color in band if _near(color, title, 36)]
    glyphs = [color for color in band if _near(color, ink, 70)]
    assert contrast_hex(_hex(glyphs[0]), _hex(backs[0])) >= MIN_TEXT_CONTRAST


def _assert_checkbox(box, theme: str) -> None:
    image = box.grab().toImage()
    height = image.height()
    center = height // 2
    indicator = [
        image.pixelColor(x, y)
        for x in range(2, 16)
        for y in range(max(0, center - 8), min(height, center + 8))
        if image.pixelColor(x, y).alpha() > 200
    ]
    assert indicator
    if theme == "dark":
        assert not any(color.red() > 245 and color.green() > 245 and color.blue() > 245 for color in indicator)
    from panelcad.theme import ui_hex

    field = ui_hex("field", theme)
    border = ui_hex("border", theme)
    assert any(_near(color, field, 30) or _near(color, border, 40) for color in indicator)


def _assert_spin_arrows(spin, theme: str) -> None:
    """The up and down buttons contain a chevron, not an empty fill."""

    from panelcad.theme import ui_hex

    image = spin.grab().toImage()
    width = image.width()
    height = image.height()
    button = ui_hex("button", theme)

    def marked(y0: int, y1: int) -> bool:
        for y in range(y0, y1):
            for x in range(max(0, width - 16), max(0, width - 2)):
                if not _near(image.pixelColor(x, y), button, 18):
                    return True
        return False

    mid = height // 2
    assert marked(2, max(3, mid - 1)), theme
    assert marked(mid + 1, max(mid + 2, height - 2)), theme


def _stable_dxf(data: bytes) -> bytes:
    """Drop ezdxf handles and timestamps. Geometry and colors stay."""

    text = data.decode("utf-8", errors="replace")
    text = re.sub(
        r"\$(TDCREATE|TDUCREATE|TDUPDATE|TDUUPDATE)\r?\n\s*40\r?\n[^\r\n]+",
        lambda match: f"${match.group(1)}\n 40\n0",
        text,
    )
    text = re.sub(
        r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}",
        "{GUID}",
        text,
    )
    text = re.sub(r"\d+\.\d+\.\d+ @ \d{4}-\d{2}-\d{2}T[^\r\n]+", "VERSION", text)
    return text.encode("utf-8")


def _stable_pdf(data: bytes) -> bytes:
    """Drop PDF dates and document ids. Page content stays."""

    data = re.sub(rb"/CreationDate\s*\(.*?\)", b"/CreationDate (X)", data)
    data = re.sub(rb"/ModDate\s*\(.*?\)", b"/ModDate (X)", data)
    data = re.sub(rb"/ID\s*\[.*?\]", b"/ID [X]", data, flags=re.S)
    data = re.sub(rb"uuid:[0-9a-fA-F-]+", b"uuid:X", data)
    data = re.sub(
        rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+\-]\d{2}:\d{2})?",
        b"TIME",
        data,
    )
    return data


def test_external_theme_round_trip_is_seen_once(tmp_path, monkeypatch):
    """In-process light, then an outside dark, then an outside light, each once."""

    import json

    from panelcad.settings import consume_external_change, settings_path, update

    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    from panelcad.settings import invalidate

    invalidate()
    update({"theme": "light"})
    assert consume_external_change() is False

    path = settings_path()

    def _outside(theme: str) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["theme"] = theme
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    _outside("dark")
    assert consume_external_change() is True
    assert consume_external_change() is False
    assert get_theme() == "dark"
    set_canvas("white")
    assert get_theme() == "dark"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["theme"] == "dark"
    assert saved["canvas"] == "white"
    _outside("light")
    assert consume_external_change() is True
    assert consume_external_change() is False
    assert get_theme() == "light"


def test_one_refresh_per_external_change(tmp_path, monkeypatch):
    import json

    from PySide6.QtWidgets import QApplication

    from panelcad.gui import theme as gui_theme
    from panelcad.settings import consume_external_change, settings_path, update
    from panelcad.theme import get_theme, refresh_appearance, set_canvas, subscribe, unsubscribe

    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    from panelcad.settings import invalidate

    invalidate()
    QApplication.instance() or QApplication([])
    gui_theme.install()
    update({"theme": "light"})
    calls = {"n": 0}

    def _count() -> None:
        calls["n"] += 1

    subscribe(_count)
    try:
        path = settings_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["theme"] = "dark"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        assert gui_theme._hooks is not None
        gui_theme._hooks._on_settings_file(str(path))
        gui_theme._hooks._on_settings_file(str(path))
        gui_theme._hooks._on_settings_file(str(path.parent))
        assert calls["n"] == 1
        assert get_theme() == "dark"
        set_canvas("white")
        assert get_theme() == "dark"
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["theme"] == "dark"
        assert saved["canvas"] == "white"
        assert consume_external_change() is False
    finally:
        unsubscribe(_count)
        refresh_appearance()


def test_autosave_edit_does_not_refresh_the_sheet(tmp_path, monkeypatch):
    """A settings write that leaves theme and canvas alone does not repaint."""

    import json

    from PySide6.QtWidgets import QApplication

    from panelcad.gui import theme as gui_theme
    from panelcad.settings import get, invalidate, settings_path, update
    from panelcad.theme import get_theme, subscribe, unsubscribe

    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    invalidate()
    QApplication.instance() or QApplication([])
    gui_theme.install()
    update({"theme": "dark", "canvas": "paper", "autosave_seconds": 30})
    calls = {"n": 0}

    def _count() -> None:
        calls["n"] += 1

    subscribe(_count)
    try:
        path = settings_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["autosave_seconds"] = 90
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        assert gui_theme._hooks is not None
        gui_theme._hooks._on_settings_file(str(path))
        assert calls["n"] == 0
        assert get_theme() == "dark"
        assert get("autosave_seconds") == 90
    finally:
        unsubscribe(_count)


def test_first_autosave_edit_after_startup_does_not_refresh(tmp_path, monkeypatch):
    """The file on disk at startup is the baseline, before any settings write."""

    import json

    from PySide6.QtWidgets import QApplication

    from panelcad.api.session import Session
    from panelcad.gui import theme as gui_theme
    from panelcad.gui.main_window import MainWindow
    from panelcad.settings import get, invalidate, settings_path
    from panelcad.theme import get_theme, subscribe, unsubscribe

    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    invalidate()
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"theme": "light", "canvas": "paper", "autosave_seconds": 30}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    app = QApplication.instance() or QApplication([])
    window = MainWindow(Session.create("Fresh"))
    window.show()
    app.processEvents()
    calls = {"n": 0}

    def _count() -> None:
        calls["n"] += 1

    subscribe(_count)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["autosave_seconds"] = 90
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
        assert gui_theme._hooks is not None
        gui_theme._hooks._on_settings_file(str(path))
        assert calls["n"] == 0
        assert get_theme() == "light"
        assert get("autosave_seconds") == 90
        payload["theme"] = "dark"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
        gui_theme._hooks._on_settings_file(str(path))
        assert calls["n"] == 1
        assert get_theme() == "dark"
    finally:
        unsubscribe(_count)
        window.close()


def _assert_theme_held(theme: str, *, canvas: str | None = None, read_file: bool = True) -> None:
    """Applied theme and in-memory cache name ``theme``.

    ``read_file`` loads settings.json. Leave it false while a Windows
    share-none lock still holds the file; read it after the lock is released.
    """

    import json

    from panelcad import settings as settings_mod
    from panelcad.settings import peek, settings_path
    from panelcad.theme import get_canvas_choice

    assert get_theme() == theme
    cached = settings_mod._cache
    assert isinstance(cached, dict)
    assert cached.get("theme") == theme
    assert peek()["theme"] == theme
    assert peek() is cached
    if canvas is not None:
        assert get_canvas_choice() == canvas
        assert cached.get("canvas") == canvas
    if not read_file:
        return
    saved = json.loads(settings_path().read_text(encoding="utf-8"))
    assert saved["theme"] == theme
    if canvas is not None:
        assert saved["canvas"] == canvas


def _reject_light_theme(dialog, app, *, read_file: bool = True) -> None:
    """The failed light choice stays off the combo and the cache.

    The file is checked too when ``read_file`` is set. A Windows share-none
    lock makes that read raise, so the exclusive-lock test checks the file
    only after it releases the lock.
    """

    from panelcad.gui import theme as gui_theme
    from panelcad.settings import settings_path
    from panelcad.theme import subscribe, unsubscribe

    calls = {"n": 0}

    def _count() -> None:
        calls["n"] += 1

    subscribe(_count)
    try:
        dialog.theme.setCurrentIndex(dialog.theme.findData("light"))
        app.processEvents()
        assert gui_theme._hooks is not None
        gui_theme._hooks._on_settings_file(str(settings_path()))
        app.processEvents()
        assert calls["n"] == 0
        assert dialog.theme.currentData() == "dark"
        _assert_theme_held("dark", canvas="paper", read_file=read_file)
    finally:
        unsubscribe(_count)


def _save_unrelated_canvas(dialog, app) -> None:
    """A later canvas save writes the sheet choice and the prior theme."""

    dialog.canvas.setCurrentIndex(dialog.canvas.findData("white"))
    app.processEvents()
    assert dialog.theme.currentData() == "dark"
    assert dialog.canvas.currentData() == "white"
    _assert_theme_held("dark", canvas="white")


def _mute_settings_warning(monkeypatch) -> list[tuple]:
    warnings: list[tuple] = []

    def warning(*args, **kwargs):
        warnings.append(args)

    monkeypatch.setattr("panelcad.gui.bug_report_dialog.QMessageBox.warning", warning)
    return warnings


def test_locked_settings_revert_the_theme_combo(tmp_path, monkeypatch):
    """A failed write leaves the applied theme, combo, cache, and file alone.

    The next canvas save must persist that prior theme, not the rejected one.
    """

    from PySide6.QtWidgets import QApplication

    from panelcad import settings as settings_mod
    from panelcad.gui import theme as gui_theme
    from panelcad.gui.settings_dialog import SettingsDialog
    from panelcad.settings import invalidate

    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    invalidate()
    set_theme("dark")
    set_canvas("paper")
    app = QApplication.instance() or QApplication([])
    gui_theme.install(app)
    dialog = SettingsDialog()
    dialog.show()
    app.processEvents()
    assert dialog.theme.currentData() == "dark"
    assert get_theme() == "dark"

    monkeypatch.setattr(settings_mod, "_REPLACE_WAIT_S", 0)
    real_replace = settings_mod.os.replace

    def denied(_src, _dst):
        raise PermissionError("denied")

    monkeypatch.setattr(settings_mod.os, "replace", denied)
    warnings = _mute_settings_warning(monkeypatch)
    try:
        _reject_light_theme(dialog, app)
    finally:
        monkeypatch.setattr(settings_mod.os, "replace", real_replace)
    try:
        _save_unrelated_canvas(dialog, app)
    finally:
        dialog.close()
    assert warnings
    assert "could not save settings" in warnings[0][2]


_EXCLUSIVE_LOCK = r"""
import sys
import ctypes
from ctypes import wintypes

GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel.CreateFileW.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
kernel.CloseHandle.restype = wintypes.BOOL
invalid = wintypes.HANDLE(-1).value
handle = kernel.CreateFileW(
    sys.argv[1], GENERIC_READ, 0, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
)
if handle == invalid:
    sys.stderr.write("CreateFileW failed %s\n" % ctypes.get_last_error())
    sys.exit(1)
sys.stdout.write("locked\n")
sys.stdout.flush()
sys.stdin.readline()
kernel.CloseHandle(handle)
"""


@pytest.mark.skipif(sys.platform != "win32", reason="needs a Windows exclusive file lock")
def test_exclusive_lock_does_not_apply_or_persist_a_failed_theme(tmp_path, monkeypatch):
    """FileShare None blocks the save. The new theme is not applied or kept."""

    import subprocess
    import threading

    from PySide6.QtWidgets import QApplication

    from panelcad.gui import theme as gui_theme
    from panelcad.gui.settings_dialog import SettingsDialog
    from panelcad.settings import invalidate, settings_path

    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    invalidate()
    set_theme("dark")
    set_canvas("paper")
    path = settings_path()
    script = tmp_path / "exclusive_lock.py"
    script.write_text(_EXCLUSIVE_LOCK, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script), str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready: dict[str, str] = {}

    def _read_ready() -> None:
        assert proc.stdout is not None
        ready["line"] = proc.stdout.readline()

    waiter = threading.Thread(target=_read_ready, daemon=True)
    waiter.start()
    waiter.join(5)
    if waiter.is_alive():
        proc.kill()
        raise AssertionError("exclusive lock helper did not start")
    if ready.get("line", "").strip() != "locked":
        err = proc.stderr.read() if proc.stderr is not None else ""
        proc.kill()
        raise AssertionError(f"exclusive lock helper failed: {err}")
    dialog = None
    warnings: list[tuple] = []
    try:
        with pytest.raises(OSError):
            path.read_bytes()
        app = QApplication.instance() or QApplication([])
        gui_theme.install(app)
        dialog = SettingsDialog()
        dialog.show()
        app.processEvents()
        warnings = _mute_settings_warning(monkeypatch)
        _reject_light_theme(dialog, app, read_file=False)
    finally:
        if proc.poll() is None and proc.stdin is not None:
            proc.stdin.write("\n")
            proc.stdin.flush()
            proc.wait(timeout=5)
    _assert_theme_held("dark", canvas="paper")
    try:
        _save_unrelated_canvas(dialog, app)
    finally:
        if dialog is not None:
            dialog.close()
    assert warnings
    assert "could not save settings" in warnings[0][2]


def test_recent_list_failure_does_not_break_open_or_save(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QFileDialog

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    old = tmp_path / "old.pcad.yaml"
    new = tmp_path / "new.pcad.yaml"
    Session.create("Old Job").save(old)
    Session.create("New Job").save(new)
    window = MainWindow(Session.open(old))
    window.show()
    app.processEvents()

    def denied(_path: str) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr("panelcad.theme.remember_project", denied)
    window.open_path(str(new))
    assert window.session.project.name == "New Job"
    assert window.canvas.session is window.session
    assert window.sheet_bar._session is window.session
    assert window.bom._session is window.session
    assert "New Job" in window.windowTitle()
    assert "could not save settings" in window.statusBar().currentMessage()

    window.save_project()
    assert "New Job" in new.read_text(encoding="utf-8")
    assert window.session.path == new
    assert "could not save settings" in window.statusBar().currentMessage()
    assert "saved" in window.statusBar().currentMessage()

    other = tmp_path / "copy.pcad.yaml"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args, **_kwargs: (str(other), ""))
    window.save_project_as()
    assert other.is_file()
    assert "New Job" in other.read_text(encoding="utf-8")
    assert window.session.path == other
    assert window.canvas.session is window.session
    assert window.sheet_bar._session is window.session
    assert window.bom._session is window.session
    assert "could not save settings" in window.statusBar().currentMessage()
    window.close()


def test_failed_theme_save_keeps_the_menu_on_the_real_theme(monkeypatch):
    from PySide6.QtWidgets import QApplication

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    set_theme("dark")
    window = MainWindow(Session.create("Theme"))
    window.show()
    app.processEvents()
    assert window._theme_actions["dark"].isChecked()

    def denied(_theme: str) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr("panelcad.theme.set_theme", denied)
    window._theme_actions["light"].trigger()
    app.processEvents()
    assert get_theme() == "dark"
    assert window._theme_actions["dark"].isChecked()
    assert window._theme_actions["light"].isChecked() is False
    assert "could not save settings" in window.statusBar().currentMessage()
    window.close()


def test_settings_replace_retries_when_the_file_is_busy(tmp_path, monkeypatch):
    import os

    from panelcad.settings import settings_path, update

    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    from panelcad import settings as settings_mod

    monkeypatch.setattr(settings_mod, "_REPLACE_WAIT_S", 0)
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("denied")
        return real(src, dst)

    monkeypatch.setattr(settings_mod.os, "replace", flaky)
    update({"theme": "dark"})
    assert calls["n"] == 3
    assert '"theme": "dark"' in settings_path().read_text(encoding="utf-8")


def test_cli_settings_save_is_one_line_when_replace_is_denied(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from panelcad.cli.main import app
    from panelcad import settings as settings_mod

    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    monkeypatch.setattr(settings_mod, "_REPLACE_WAIT_S", 0)

    def denied(src, dst):
        raise PermissionError("denied")

    monkeypatch.setattr(settings_mod.os, "replace", denied)
    result = CliRunner().invoke(app, ["settings", "set", "theme", "dark"])
    assert result.exit_code == 1
    assert "Traceback" not in (result.output + (result.stderr or ""))
    assert "could not save settings" in (result.stderr or result.output)


def test_update_rereads_disk_before_merge(tmp_path, monkeypatch):
    import json

    from panelcad.diag.settings import BugSettings, save_settings, set_autosave_seconds
    from panelcad.settings import settings_path, update
    from panelcad.theme import get_theme

    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    from panelcad.settings import invalidate

    invalidate()
    update({"theme": "light", "canvas": "paper"})
    path = settings_path()
    disk = json.loads(path.read_text(encoding="utf-8"))
    disk["autosave_seconds"] = 60
    disk["bug_reports"] = {"webhook_url": "https://hooks.example/kept", "slack_bot_token": "", "slack_channel": ""}
    path.write_text(json.dumps(disk, indent=2) + "\n", encoding="utf-8")
    update({"theme": "dark"})
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["theme"] == "dark"
    assert saved["autosave_seconds"] == 60
    assert saved["bug_reports"]["webhook_url"].endswith("/kept")
    set_autosave_seconds(15)
    save_settings(BugSettings(webhook_url="https://hooks.example/kept", slack_channel="#bugs"))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["theme"] == "dark"
    assert saved["autosave_seconds"] == 15
    assert get_theme() == "dark"
    assert saved["bug_reports"]["slack_channel"] == "#bugs"


def test_settings_file_is_shared_with_bug_reports(tmp_path, monkeypatch):
    from panelcad.diag.paths import settings_path as diag_path
    from panelcad.settings import settings_path

    monkeypatch.delenv("PANELCAD_CONFIG", raising=False)
    monkeypatch.setenv("PANELCAD_DATA_DIR", str(tmp_path / "data"))
    assert settings_path() == diag_path()
    assert settings_path() == tmp_path / "data" / "settings.json"
    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path / "cfg"))
    assert settings_path() == diag_path()
    assert settings_path().parent == tmp_path / "cfg"


def test_dark_canvas_backplate_is_not_cream():
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from panelcad.core.models import Enclosure
    from panelcad.render.layout import draw_enclosure
    from panelcad.render.qt_painter import QtPainter
    from panelcad.theme import _luminance

    QApplication.instance() or QApplication([])
    enc = Enclosure(x=0, y=0, width_mm=80, height_mm=80, depth_mm=40, name="Box")

    def _sample(print_paper: bool) -> str:
        image = QImage(100, 100, QImage.Format.Format_ARGB32)
        image.fill(0)
        qp = QPainter(image)
        draw_enclosure(QtPainter(qp, print_paper=print_paper), enc)
        qp.end()
        color = image.pixelColor(40, 40)
        return f"#{color.red():02x}{color.green():02x}{color.blue():02x}"

    set_theme("dark")
    set_canvas("dark")
    dark = _sample(False)
    assert dark == "#32363d"
    assert canvas_hex("paper", "dark") == "#2a2e34"
    assert _luminance(dark) > _luminance(canvas_hex("paper", "dark"))
    assert _luminance(dark) < 0.2
    assert contrast_hex(canvas_hex("ink", "dark"), dark) >= MIN_TEXT_CONTRAST
    light_plate = canvas_hex("enclosure_backplate", "paper")
    light_paper = canvas_hex("paper", "paper")
    assert light_plate == "#f3f1ea"
    assert light_paper == "#f7f4ea"
    assert light_plate != light_paper
    assert contrast_hex(light_plate, light_paper) > 1
    set_canvas("paper")
    set_theme("light")
    printed = _sample(True)
    assert printed == "#f3f1ea"
    assert _luminance(printed) > 0.8


def test_vertical_duct_label_rotates_inside_the_duct():
    from panelcad.core.models import WireTray
    from panelcad.render.layout import draw_tray

    class _Rec:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def set_layer(self, _name: str) -> None:
            return

        def set_fill(self, _color) -> None:
            return

        def set_stroke(self, _color, _width: float) -> None:
            return

        def rect(self, *_args) -> None:
            return

        def line(self, *_args) -> None:
            return

        def text(self, x, y, text, size, align="middle", rotate=0, **_kw) -> None:
            self.calls.append((text, rotate, x, y, size))

        def layout_is_true_scale(self) -> bool:
            return False

    tray = WireTray(id="wt-left", x=10, y=20, length_mm=180, width_mm=12, orientation="v")
    painter = _Rec()
    draw_tray(painter, tray)
    text, rotate, x, y, _size = painter.calls[0]
    assert rotate == -90
    assert text.startswith("wt-left")
    # Along the duct centerline, inside the duct. The caption runs up the slot.
    assert abs(x - (tray.x + tray.width_mm / 2.0)) < 0.1
    assert tray.y < y < tray.y + tray.length_mm


def test_phase_rejects_unknown_values():
    from typer.testing import CliRunner

    from panelcad.api.session import Session
    from panelcad.cli.agent import Agent
    from panelcad.cli.main import app

    bad = Agent().handle({"id": "phase", "op": "ui_set_phase_colors", "enabled": "purple"})
    assert bad["ok"] is False
    assert "on or off" in bad["error"]
    session = Session.create("Phase")
    try:
        session.ui_set_phase_colors("purple")
    except ValueError as exc:
        assert "on or off" in str(exc)
    else:
        raise AssertionError("purple phase was accepted")
    result = CliRunner().invoke(app, ["settings", "set", "phase", "purple"])
    assert result.exit_code == 1
    assert "on or off" in result.output
    ok = CliRunner().invoke(app, ["settings", "set", "phase", "off"])
    assert ok.exit_code == 0, ok.output
    assert "False" in ok.output or "false" in ok.output


def test_first_window_fits_the_available_screen():
    from PySide6.QtWidgets import QApplication

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Session.create("Fit"))
    avail = app.primaryScreen().availableGeometry()
    limit_w = max(window.minimumWidth(), min(1400, int(avail.width() * 0.9)))
    limit_h = max(window.minimumHeight(), min(860, int(avail.height() * 0.9)))
    assert window.width() <= limit_w + 8
    assert window.height() <= limit_h + 8
    window.close()


def test_inspector_keeps_the_restored_selection():
    from PySide6.QtWidgets import QApplication

    from panelcad.examples.dol import build_dol
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(build_dol())
    window.show()
    app.processEvents()
    ref = window.session.sheet().components[0].id
    window.canvas.select_ref(ref)
    app.processEvents()
    assert window.props.heading.text() == ref
    set_theme("dark")
    app.processEvents()
    assert window.props.heading.text() == ref
    window.close()
    set_theme("light")
    set_canvas("paper")


def test_inspector_fields_fit_the_default_dock():
    from PySide6.QtWidgets import QApplication, QDoubleSpinBox

    from panelcad.examples.dol import build_dol
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(build_dol())
    window.show()
    window.resize(1100, 700)
    app.processEvents()
    ref = window.session.sheet().components[0].id
    window.canvas.select_ref(ref)
    window.prop_wrap.show()
    window.prop_wrap.raise_()
    app.processEvents()
    scroll = window.prop_wrap.widget()
    assert scroll.horizontalScrollBar().maximum() == 0
    spin = window.props.findChild(QDoubleSpinBox)
    assert spin is not None
    edge = spin.mapTo(scroll.viewport(), spin.rect().bottomRight())
    assert edge.x() <= scroll.viewport().width() + 1
    window.close()
