"""Cached routes and marks must follow the drawing after an edit, undo, rename, or import."""

from __future__ import annotations

from pathlib import Path

import pytest

from panelcad.api.session import Session
from panelcad.render.label_avoid import clear_resolved_labels, label_cache_stats, resolve_labels

_ROOT = Path(__file__).resolve().parents[1]


def _ladder(session: Session, sheet_id: str | None = None) -> bool:
    from panelcad.core.ladder import sheet_drawing_style

    sheet = session.sheet(sheet_id)
    style = sheet_drawing_style(sheet, session.project)
    return style in {"ladder", "iec"} and sheet.kind == "schematic"


def _resolve(session: Session, sheet_id: str):
    sheet = session.project.sheet(sheet_id)
    return resolve_labels(
        session.project,
        session.catalog,
        sheet,
        ladder=_ladder(session, sheet_id),
        sheet_no=session.project.sheets.index(sheet) + 1,
    )


def _sig(resolved) -> tuple:
    routes = tuple(
        (wid, tuple((round(pt.x, 3), round(pt.y, 3)) for pt in pts))
        for wid, pts in sorted(resolved.routes.items())
    )
    marks = tuple(
        (wid, round(at[0], 3), round(at[1], 3), at[2]) for wid, at in sorted(resolved.marks.items())
    )
    tags = tuple(
        (ref, round(at[0], 3), round(at[1], 3), at[2], round(float(at[3]), 3))
        for ref, at in sorted(resolved.tags.items())
    )
    return routes, marks, tags


def _fresh(session: Session, sheet_id: str):
    clear_resolved_labels()
    return _resolve(session, sheet_id)


@pytest.fixture
def dol() -> Session:
    clear_resolved_labels()
    return Session.open(_ROOT / "examples" / "dol_starter.pcad.yaml")


def test_second_visit_reuses_the_sheet(dol: Session) -> None:
    before = label_cache_stats()[0]
    _resolve(dol, "power")
    _resolve(dol, "control")
    _resolve(dol, "power")
    assert label_cache_stats()[0] == before + 2


def test_move_and_undo_do_not_keep_the_old_routes(dol: Session) -> None:
    original = _sig(_resolve(dol, "power"))
    with dol.command("move"):
        dol.move("M1", 200, 80, "power", snap=False)
    moved = _resolve(dol, "power")
    assert _sig(moved) != original
    assert _sig(moved) == _sig(_fresh(dol, "power"))

    assert dol.undo()
    restored = _resolve(dol, "power")
    assert _sig(restored) == original
    assert _sig(restored) == _sig(_fresh(dol, "power"))

    assert dol.redo()
    again = _resolve(dol, "power")
    assert _sig(again) == _sig(moved)
    assert _sig(again) == _sig(_fresh(dol, "power"))


def test_direct_edit_changes_the_key(dol: Session) -> None:
    """A position change misses the cache even when no command cleared it."""

    before = _sig(_resolve(dol, "power"))
    dol.sheet("power").component("M1").x += 30
    edited = _resolve(dol, "power")
    assert _sig(edited) != before
    assert _sig(edited) == _sig(_fresh(dol, "power"))


def test_rename_does_not_keep_the_old_tag(dol: Session) -> None:
    before = _sig(_resolve(dol, "power"))
    dol.rename_component("M1", "MOT9", "power")
    renamed = _resolve(dol, "power")
    assert "MOT9" in renamed.tags
    assert "M1" not in renamed.tags
    assert _sig(renamed) != before
    assert _sig(renamed) == _sig(_fresh(dol, "power"))

    dol.set_sheet_title("Power renamed", "power")
    titled = _resolve(dol, "power")
    assert _sig(titled) == _sig(_fresh(dol, "power"))


def test_library_geometry_is_part_of_the_key(dol: Session) -> None:
    _resolve(dol, "power")
    device = dol.catalog.get(dol.sheet("power").component("M1").library_id)
    pin = device.pins[0]
    old = pin.y
    pin.y = old + 4
    try:
        computes = label_cache_stats()[0]
        edited = _resolve(dol, "power")
        assert label_cache_stats()[0] == computes + 1
        assert _sig(edited) == _sig(_fresh(dol, "power"))
    finally:
        pin.y = old
        clear_resolved_labels()


def test_import_does_not_reuse_the_previous_sheet(dol: Session, tmp_path: Path) -> None:
    _resolve(dol, "power")
    before_ids = [sheet.id for sheet in dol.project.sheets]
    dol.import_print(_ROOT / "examples" / "dol_power.dxf", workspace=tmp_path / "ws", vision_backend="none")
    after_ids = [sheet.id for sheet in dol.project.sheets]
    assert after_ids != before_ids
    sheet_id = dol.sheet().id
    imported = _resolve(dol, sheet_id)
    assert _sig(imported) == _sig(_fresh(dol, sheet_id))


def test_canvas_reload_follows_edit_and_undo(dol: Session) -> None:
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import SchematicView, WireItem

    app = QApplication.instance() or QApplication([])
    dol.set_active_sheet("power")
    view = SchematicView(dol)
    view.resize(900, 700)
    view.show()
    app.processEvents()

    def paths() -> dict[str, tuple]:
        view.reload()
        app.processEvents()
        found = {}
        for item in view._items:
            if isinstance(item, WireItem) and item.wire_id:
                path = item.path()
                found[item.wire_id] = tuple(
                    (round(path.elementAt(i).x, 2), round(path.elementAt(i).y, 2))
                    for i in range(path.elementCount())
                )
        return found

    original = paths()
    with dol.command("move"):
        dol.move("M1", 220, 90, "power", snap=False)
    moved = paths()
    assert moved != original
    assert dol.undo()
    assert paths() == original
    view.close()
