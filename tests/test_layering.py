"""Architectural layering and god-class decomposition guard tests (H5/H6)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "panelcad"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _violations(subdir: str, forbidden_prefixes: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for py in sorted((SRC / subdir).glob("*.py")):
        for mod in sorted(_imported_modules(py)):
            if any(mod == pref or mod.startswith(f"{pref}.") for pref in forbidden_prefixes):
                hits.append(f"{py.relative_to(SRC)} -> {mod}")
    return hits


def test_ocr_layer_does_not_import_importer_api_or_gui() -> None:
    assert _violations("ocr", ("panelcad.importer", "panelcad.api", "panelcad.gui")) == []


def test_render_layer_does_not_import_api_or_gui() -> None:
    assert _violations("render", ("panelcad.api", "panelcad.gui")) == []


def test_core_layer_does_not_import_gui_or_render_sheet() -> None:
    assert _violations("core", ("panelcad.gui", "panelcad.render.sheet")) == []


def test_compatibility_reexports_and_mixin_decomposition() -> None:
    from panelcad.api.session import Session
    from panelcad.api.session_ports import SessionPortsMixin
    from panelcad.api.session_reconstruct import SessionReconstructMixin
    from panelcad.api.session_troubleshoot import SessionTroubleshootMixin
    from panelcad.core import pin_geometry
    from panelcad.gui import canvas, canvas_items
    from panelcad.gui.main_window import MainWindow
    from panelcad.gui.main_window_lifecycle import MainWindowLifecycleMixin
    from panelcad.importer import tesseract_runtime as imp_rt
    from panelcad.ocr import tesseract_runtime as ocr_rt
    from panelcad.render import sheet

    assert imp_rt is ocr_rt
    assert sheet.parse_pin_ref is pin_geometry.parse_pin_ref
    assert sheet.pin_world is pin_geometry.pin_world
    assert issubclass(Session, SessionTroubleshootMixin)
    assert issubclass(Session, SessionReconstructMixin)
    assert issubclass(Session, SessionPortsMixin)
    assert issubclass(MainWindow, MainWindowLifecycleMixin)
    assert canvas.ComponentItem is canvas_items.ComponentItem
    assert canvas.WireItem is canvas_items.WireItem
