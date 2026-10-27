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


def test_resolve_labels_shares_wire_marks_and_xref_within_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """One resolve_labels pass must not recompute _place_xref_labels or identical _place_wire_marks."""

    from panelcad.render import wire_labels, xref as xref_mod

    clear_resolved_labels()
    session = Session.open(_ROOT / "examples" / "xref_nema_seal_in.pcad.yaml")
    sheet = session.project.sheet("control")
    xref_calls = 0
    real_xref = xref_mod._place_xref_labels

    def counted_xref(*args, **kwargs):
        nonlocal xref_calls
        xref_calls += 1
        return real_xref(*args, **kwargs)

    score_calls = 0
    real_score = wire_labels._score

    def counted_score(*args, **kwargs):
        nonlocal score_calls
        score_calls += 1
        return real_score(*args, **kwargs)

    monkeypatch.setattr(xref_mod, "_place_xref_labels", counted_xref)
    monkeypatch.setattr(wire_labels, "_score", counted_score)
    resolved = resolve_labels(session.project, session.catalog, sheet, ladder=True, sheet_no=1)
    assert resolved.xrefs
    assert resolved.marks
    assert xref_calls == 1
    first_pass_scores = score_calls
    assert first_pass_scores > 0

    # Inside the same share_wire_marks scope, a second place_wire_marks call with identical inputs hits cache.
    with wire_labels.share_wire_marks():
        score_calls = 0
        wire_labels.place_wire_marks(sheet, session.catalog, resolved.routes, ladder=True, project=session.project)
        after_first = score_calls
        wire_labels.place_wire_marks(sheet, session.catalog, resolved.routes, ladder=True, project=session.project)
        assert score_calls == after_first


def test_hand_drawn_io_check_shares_resolve_labels_with_io_sheet_draw() -> None:
    """run_checks on a hand-drawn I/O sheet must reuse the same resolve_labels cache key as draw_io_sheet."""

    from panelcad.core.checks import run_checks
    from panelcad.render.io_ladder import draw_io_sheet
    from panelcad.render.svg import SvgPainter

    clear_resolved_labels()
    session = Session.open(_ROOT / "examples" / "dol_starter.pcad.yaml")
    io_sheet = session.project.sheet("io")
    assert io_sheet is not None

    before = label_cache_stats()[0]
    run_checks(session.project, session.catalog, label_cover=True)
    after_checks = label_cache_stats()[0]
    assert after_checks > before

    painter = SvgPainter()
    draw_io_sheet(painter, io_sheet, session.catalog, project=session.project)
    assert label_cache_stats()[0] == after_checks


def test_print_package_reuses_planned_reports_thermal_and_power(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """write_print_package must reuse the rows/thermal/power computed during plan_print_package."""

    from panelcad.render import print_package as pp
    from panelcad.render import thermal_page as tp

    session = Session.open(_ROOT / "examples" / "dol_starter.pcad.yaml")
    report_calls: list[str] = []
    real_collect = pp._collect_report_rows_and_note

    def counted_collect(sess, kind: str):
        report_calls.append(kind)
        return real_collect(sess, kind)

    painted_thermals: list[int] = []
    real_paint_thermal = tp.paint_thermal_page

    def counted_paint_thermal(qp, project, thermal_dict, **kwargs):
        painted_thermals.append(id(thermal_dict))
        return real_paint_thermal(qp, project, thermal_dict, **kwargs)

    monkeypatch.setattr(pp, "_collect_report_rows_and_note", counted_collect)
    monkeypatch.setattr(tp, "paint_thermal_page", counted_paint_thermal)

    out_pdf = tmp_path / "pkg.pdf"
    pp.write_print_package(session, out_pdf)
    assert out_pdf.is_file()
    # Every report kind is collected once during planning and reused during painting.
    assert len(report_calls) == len(set(report_calls))
    assert len(painted_thermals) == 1
