"""App icon, startup splash, and the offscreen gate."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "panelcad" / "assets"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _invalidate_theme() -> None:
    from panelcad.settings import invalidate

    invalidate()


def test_icon_files_and_qicon_load():
    svg = (ASSETS / "panelcad.svg").read_text(encoding="utf-8")
    assert svg.lstrip().startswith("<svg")
    assert "<image" not in svg and "href=" not in svg
    assert "#143A66" in svg and "#F08A24" in svg

    ico = Image.open(ASSETS / "panelcad.ico")
    assert ico.format == "ICO"
    assert set(ico.ico.sizes()) == {(size, size) for size in ICON_SIZES}

    for size in ICON_SIZES:
        image = Image.open(ASSETS / f"panelcad-{size}.png")
        assert image.size == (size, size)
        assert image.mode == "RGBA"

    small = Image.open(ASSETS / "panelcad-32.png").convert("RGBA")
    view = small.load()
    width, height = small.size
    found_orange = False
    found_blue = False
    for y in range(height):
        for x in range(width):
            r, g, b, _alpha = view[x, y]
            found_orange = found_orange or (r > 180 and g > 80 and b < 90)
            found_blue = found_blue or (r < 50 and g < 100 and b > 80)
    assert found_orange and found_blue

    from PySide6.QtWidgets import QApplication

    from panelcad.gui.splash import ICON_SIZES as code_sizes
    from panelcad.gui.splash import application_icon

    assert code_sizes == ICON_SIZES
    QApplication.instance() or QApplication([])
    icon = application_icon()
    assert not icon.isNull()
    for size in (16, 32, 256):
        pixmap = icon.pixmap(size, size)
        assert not pixmap.isNull()
        assert pixmap.width() >= size
        assert pixmap.height() >= size


def test_splash_appears_and_closes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PANELCAD_FORCE_SPLASH", "1")
    from PySide6.QtWidgets import QApplication, QWidget

    from panelcad.gui import splash as splash_mod

    splash_mod.reset_splash_state()
    app = QApplication.instance() or QApplication([])
    started = time.perf_counter()
    card = splash_mod.show_splash(force=True, message="Loading parts library...")
    # QSplashScreen waits up to 1s for a native map. Offscreen never maps, so
    # that wait must be skipped or this stays above a second.
    assert time.perf_counter() - started < 0.5
    assert card is not None
    assert card.isVisible()
    assert splash_mod.splash_is_visible()
    assert "Loading parts library..." in splash_mod.messages()
    splash_mod.splash_message("Opening project...")
    app.processEvents()
    assert "Opening project..." in splash_mod.messages()

    host = QWidget()
    host.resize(160, 100)
    host.setWindowIcon(splash_mod.application_icon())
    host.show()
    app.processEvents()
    splash_mod.finish_splash(host)
    app.processEvents()
    assert splash_mod.splash_is_visible() is False
    assert not host.windowIcon().isNull()
    host.close()
    app.processEvents()


def test_splash_background_follows_theme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    monkeypatch.setenv("PANELCAD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PANELCAD_FORCE_SPLASH", "1")
    (tmp_path / "settings.json").write_text('{"theme": "dark"}\n', encoding="utf-8")
    _invalidate_theme()
    from PySide6.QtWidgets import QApplication

    from panelcad.gui import splash as splash_mod

    try:
        splash_mod.reset_splash_state()
        app = QApplication.instance() or QApplication([])
        card = splash_mod.show_splash(force=True)
        assert card is not None
        app.processEvents()
        color = card.pixmap().toImage().pixelColor(8, 8)
        assert abs(color.red() - 0x1B) < 12
        assert abs(color.green() - 0x1E) < 12
        assert abs(color.blue() - 0x24) < 16
        assert (color.red(), color.green(), color.blue()) != (0, 0, 0)
        splash_mod.close_splash()

        (tmp_path / "settings.json").write_text('{"theme": "light"}\n', encoding="utf-8")
        _invalidate_theme()
        splash_mod.reset_splash_state()
        card = splash_mod.show_splash(force=True)
        assert card is not None
        color = card.pixmap().toImage().pixelColor(8, 8)
        assert color.red() > 220 and color.green() > 220 and color.blue() > 220
        assert (color.red(), color.green(), color.blue()) != (0, 0, 0)
    finally:
        splash_mod.reset_splash_state()
        _invalidate_theme()


def test_reveal_paints_theme_before_the_window_is_mapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    monkeypatch.setenv("PANELCAD_DATA_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text('{"theme": "dark"}\n', encoding="utf-8")
    _invalidate_theme()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QWidget

    from panelcad.gui.splash import reveal_main_window

    try:
        app = QApplication.instance() or QApplication([])
        window = QWidget()
        window.resize(80, 60)
        reveal_main_window(window, app)
        assert window.isVisible()
        assert window.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen) is False
        color = window.grab().toImage().pixelColor(10, 10)
        assert abs(color.red() - 0x1B) < 20
        assert abs(color.green() - 0x1E) < 20
        assert abs(color.blue() - 0x24) < 24
        assert (color.red(), color.green(), color.blue()) != (0, 0, 0)
        window.close()
    finally:
        _invalidate_theme()


def test_offscreen_and_cli_skip_the_splash(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PANELCAD_FORCE_SPLASH", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("PANELCAD_NO_SPLASH", "0")
    from panelcad.gui.splash import command_is_gui, should_show_splash, splash_enabled

    assert splash_enabled() is False
    assert command_is_gui(["panelcad", "gui"]) is True
    assert command_is_gui(["panelcad", "gui", "job.pcad.yaml"]) is True
    assert command_is_gui(["panelcad", "validate", "job.pcad.yaml"]) is False
    assert command_is_gui(["panelcad", "--help"]) is False
    assert command_is_gui(["panelcad", "gui", "--help"]) is False
    assert command_is_gui(["panelcad"]) is False
    assert should_show_splash(["panelcad", "gui"]) is False

    monkeypatch.setenv("PANELCAD_FORCE_SPLASH", "1")
    assert splash_enabled() is True
    assert should_show_splash(["panelcad", "gui"]) is True
    assert should_show_splash(["panelcad", "version"]) is False

    monkeypatch.setenv("PANELCAD_NO_SPLASH", "1")
    assert splash_enabled() is False


def test_launch_closes_splash_when_startup_fails(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PANELCAD_FORCE_SPLASH", "1")
    monkeypatch.setenv("PANELCAD_NO_WELCOME", "1")
    from panelcad.gui import splash as splash_mod
    from panelcad.gui.app import launch_gui

    splash_mod.reset_splash_state()

    def boom(session, *, show_start: bool):
        del session, show_start
        assert splash_mod.splash_is_visible()
        raise RuntimeError("startup failed")

    monkeypatch.setattr("panelcad.gui.app._open_main_window", boom)
    try:
        with pytest.raises(RuntimeError, match="startup failed"):
            launch_gui(None)
        assert splash_mod.splash_is_visible() is False
    finally:
        splash_mod.reset_splash_state()


def test_launch_shows_splash_then_the_main_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PANELCAD_FORCE_SPLASH", "1")
    monkeypatch.setenv("PANELCAD_NO_WELCOME", "1")
    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    monkeypatch.setenv("PANELCAD_DATA_DIR", str(tmp_path / "data"))
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from panelcad.gui import splash as splash_mod
    from panelcad.gui.app import launch_gui
    from panelcad.gui.main_window import MainWindow

    splash_mod.reset_splash_state()
    try:
        app = launch_gui(None)
        assert "Loading parts library..." in splash_mod.messages()
        assert "Opening project..." in splash_mod.messages()
        assert splash_mod.splash_was_shown()
        assert splash_mod.splash_is_visible() is False
        assert not app.windowIcon().isNull()
        windows = [
            widget
            for widget in app.topLevelWidgets()
            if isinstance(widget, MainWindow) and widget.isVisible()
        ]
        assert len(windows) == 1
        window = windows[0]
        assert window.isVisible()
        assert not window.windowIcon().isNull()
        assert window.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen) is False
        window.close()
        QApplication.processEvents()
    finally:
        splash_mod.reset_splash_state()
        _invalidate_theme()


def test_offscreen_launch_does_not_show_the_splash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PANELCAD_FORCE_SPLASH", raising=False)
    monkeypatch.setenv("PANELCAD_NO_SPLASH", "1")
    monkeypatch.setenv("PANELCAD_NO_WELCOME", "1")
    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    monkeypatch.setenv("PANELCAD_DATA_DIR", str(tmp_path / "data"))
    from PySide6.QtWidgets import QApplication

    from panelcad.gui import splash as splash_mod
    from panelcad.gui.app import launch_gui
    from panelcad.gui.main_window import MainWindow

    splash_mod.reset_splash_state()
    try:
        app = launch_gui(None)
        assert splash_mod.splash_was_shown() is False
        assert splash_mod.splash_is_visible() is False
        assert not app.windowIcon().isNull()
        windows = [
            widget
            for widget in app.topLevelWidgets()
            if isinstance(widget, MainWindow) and widget.isVisible()
        ]
        assert len(windows) == 1
        for window in windows:
            window.close()
        QApplication.processEvents()
    finally:
        splash_mod.reset_splash_state()


def test_package_import_does_not_load_the_session():
    code = (
        "import panelcad, sys\n"
        "assert 'panelcad.api.session' not in sys.modules, 'session loaded too early'\n"
        "assert panelcad.__version__\n"
        "from panelcad import Session\n"
        "assert 'panelcad.api.session' in sys.modules\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_version_command_does_not_show_the_splash():
    code = """
import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.pop("PANELCAD_FORCE_SPLASH", None)
os.environ["PANELCAD_NO_SPLASH"] = "1"
sys.argv = ["panelcad", "version"]
from panelcad.launch import main
from panelcad.gui.splash import splash_was_shown
try:
    main()
except SystemExit as exc:
    code = exc.code if isinstance(exc.code, int) else 0
else:
    code = 0
if splash_was_shown():
    raise SystemExit(3)
raise SystemExit(code)
"""
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.pop("PANELCAD_FORCE_SPLASH", None)
    env["PANELCAD_NO_SPLASH"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_installer_shortcuts_use_the_icon():
    text = (ROOT / "scripts" / "Install-PanelCAD.ps1").read_text(encoding="utf-8")
    assert r"src\panelcad\assets\panelcad.ico" in text
    assert text.count("IconLocation") >= 2
    assert 'GetFolderPath("Programs")' in text
    assert "$start.Save()" in text
    assert text.index("$link.IconLocation") < text.index("$link.Save()")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'panelcad = "panelcad.launch:main"' in pyproject
    assert "assets/*" in pyproject
