"""Coil/contact cross-reference: NEMA rungs, IEC mirror, checks, live update."""

import shutil
from pathlib import Path

import yaml

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.ladder import RAIL_INSET
from panelcad.core.xref import (
    iec_mirror_rows,
    mark_nc,
    nc_draw_mark,
    offpage_callout,
    occupied_rung_indices,
    xref_annotations,
    xref_families,
    xref_style,
)
from panelcad.core.zones import DEFAULT_RUNG_PITCH_MM, nema_line_at, rung_origin_y, rung_y, zone_at
from panelcad.examples.xref_demo import build_iec_mirror, build_nema_seal_in
from panelcad.io.yaml_format import dump_project
from panelcad.render.dxf import LAYERS
from panelcad.render.titleblock import working_rect
from panelcad.render.xref import annotation_box, place_xref_labels
from panelcad.render.sheet_labels import colliding_sheet_labels
from panelcad.render.wire_labels import (
    boxes_overlap,
    mark_box,
    place_wire_marks,
    routes_share_collinear_run,
    routes_share_line,
)


def test_nema_line_uses_rung_pitch():
    s = Session.create("Lines")
    sheet = s.sheet()
    row, col, index = zone_at(sheet, 40, 30)
    assert row == "A"
    assert col >= 1
    assert index == 1
    origin = rung_origin_y(sheet)
    assert nema_line_at(sheet, origin, 1) == 101
    assert nema_line_at(sheet, origin + DEFAULT_RUNG_PITCH_MM, 1) == 102
    assert nema_line_at(sheet, origin + 2 * DEFAULT_RUNG_PITCH_MM, 1) == 103
    # Two devices a half-inch apart no longer share a zone-row line.
    assert nema_line_at(sheet, origin + DEFAULT_RUNG_PITCH_MM, 1) != nema_line_at(
        sheet, origin + 3 * DEFAULT_RUNG_PITCH_MM, 1
    )


def test_nema_seal_in_lists_contacts_by_rung():
    s = build_nema_seal_in()
    assert xref_style(s.project, s.sheet("control")) == "nema"
    rows = {(row["sheet"], row["child"], row["role"]): row for row in s.cross_reference_rows()}
    coil = rows[("control", "M1", "coil")]
    seal = next(row for row in s.cross_reference_rows() if row["child"] == "M1-13")
    assert seal["location"] in coil["xref"]
    assert coil["location"] == seal["xref"]
    assert coil["line"] != seal["line"]
    assert int(seal["line"]) > int(coil["line"])
    cr_coil = rows[("control", "CR1", "coil")]
    cr_nc = next(row for row in s.cross_reference_rows() if row["child"] == "CR1-21")
    assert cr_nc["state"] == "NC"
    assert cr_nc["location"] in cr_coil["xref"]
    assert str(cr_nc["line"]).startswith("2")
    families = xref_families(s.project, s.catalog)
    assert "M1" in families and "CR1" in families
    used = occupied_rung_indices(s.project, s.sheet("control"), s.catalog)
    assert len(used) >= 3


def test_nema_demo_is_a_real_ladder():
    s = build_nema_seal_in()
    control = s.sheet("control")
    aux = s.sheet("aux")
    # Coil return is L2 only — no off-page on A2 / the return side.
    assert any(w.frm == "CR1.A2" and w.to == "L2" or w.frm == "L2" and w.to == "CR1.A2" for w in control.wires)
    assert not any("CR1.A2" in (w.frm, w.to) and "offpage" in (w.frm + w.to).lower() for w in control.wires)
    # Off-page is fed from the CR1-13 contact output, not from the coil node.
    assert any({w.frm, w.to} == {"CR1-13.4", "OP1.A"} for w in control.wires)
    assert any({w.frm, w.to} == {"OP2.A", "CR1-21.1"} for w in aux.wires)
    assert any("PL1" in (w.frm + w.to) for w in aux.wires)
    assert not any({w.frm, w.to} == {"CR1-21.2", "L2"} for w in aux.wires)
    assert not any("L1" in (w.frm, w.to) and "CR1-21" in (w.frm + w.to) for w in aux.wires)
    # Auto wire numbers, not leftover hand labels.
    for sheet in s.project.sheets:
        for wire in sheet.wires:
            assert wire.label == ""
            assert wire.number
            assert wire.number.isdigit()
            assert wire.number not in {"HOLD", "CR1"}


def test_iec_mirror_uses_sheet_column_and_terminals():
    s = build_iec_mirror()
    assert xref_style(s.project, s.sheet()) == "iec"
    k14 = next(row for row in s.cross_reference_rows() if row["child"] == "K1-14")
    k21 = next(row for row in s.cross_reference_rows() if row["child"] == "K1-21")
    assert k14["location_iec"] == "/1.3"
    assert k21["location_iec"] == "/1.3"
    anns = [item for item in xref_annotations(s.project, s.catalog, s.sheet("main")) if item.kind == "mirror"]
    assert anns
    mirror = anns[0]
    assert mirror.columns is not None
    no, nc = mirror.columns
    assert "/1.3" in no
    assert "/1.3" in nc
    assert any("/2." in text for text in no)
    rows = iec_mirror_rows(xref_families(s.project, s.catalog)["K1"])
    terms = {row["terminals"] for row in rows}
    assert "13-14" in terms
    assert "21-22" in terms
    assert any(row["terminals"] == "13-14" and row["location"].startswith("/2.") for row in rows)
    assert any(row["location"] == "/1.3" and row["state"] == "NO" for row in rows)
    assert any(row["location"] == "/1.3" and row["state"] == "NC" for row in rows)
    contact = next(row for row in s.cross_reference_rows() if row["child"] == "K1-21")
    assert contact["xref"].startswith("/")
    assert contact["style"] == "iec"
    main = s.sheet("main")
    assert not any({w.frm, w.to} == {"K1.A1", "K1.A2"} for w in main.wires)
    assert any({w.frm, w.to} == {"L+", "S1.13"} for w in main.wires)
    assert any({w.frm, w.to} == {"S1.14", "K1.A1"} for w in main.wires)
    assert any({w.frm, w.to} == {"K1.A2", "0V"} for w in main.wires)
    assert any({w.frm, w.to} == {"K1-14.14", "K1.A1"} for w in main.wires)
    assert any("H1" in (w.frm + w.to) and "K1-21" in (w.frm + w.to) for w in main.wires)


def _pin_numbers(session: Session, sheet_id: str, end: str) -> set[str]:
    return {w.number for w in session.sheet(sheet_id).wires if end in (w.frm, w.to) and w.number}


def test_nema_seal_in_sides_match_start_nets():
    s = build_nema_seal_in()
    assert _pin_numbers(s, "control", "M1-13.3") == _pin_numbers(s, "control", "PB2.3")
    assert _pin_numbers(s, "control", "M1-13.4") == _pin_numbers(s, "control", "PB2.4")
    assert _pin_numbers(s, "control", "M1-13.3") != _pin_numbers(s, "control", "M1-13.4")


def test_nema_rails_keep_the_same_number_on_every_sheet():
    s = build_nema_seal_in()
    l2 = {w.number for sh in s.project.sheets for w in sh.wires if "L2" in (w.frm, w.to)}
    l1 = {w.number for sh in s.project.sheets for w in sh.wires if "L1" in (w.frm, w.to)}
    assert len(l2) == 1 and next(iter(l2))
    assert len(l1) == 1 and next(iter(l1))


def test_iec_coil_without_contacts_has_no_mirror_frame():
    s = Session.create("Bare coil", sheet_id="main")
    s.set_title_block(standard="IEC")
    s.place("iec.contactors.coil", ref="K1", x=120, y=40)
    anns = xref_annotations(s.project, s.catalog, s.sheet("main"))
    assert not any(item.kind == "mirror" and item.mirror_rows for item in anns)
    assert not any(item.lines == ["—"] for item in anns)
    placed = place_xref_labels(s.project, s.catalog, s.sheet("main"))
    assert not any(item.owner == "K1" for item, _x, _y, _align in placed)


def test_iec_mirror_terminal_pairs_are_unique():
    s = build_iec_mirror()
    for key, family in xref_families(s.project, s.catalog).items():
        keys = [
            (row["terminals"], row["state"], row["location"])
            for row in iec_mirror_rows(family)
            if row.get("terminals")
        ]
        assert len(keys) == len(set(keys)), (key, keys)


def test_nema_demo_wire_numbers_once_per_run_and_clear():
    from panelcad.render.wiring import route_sheet_wires

    s = build_nema_seal_in()
    for sheet in s.project.sheets:
        routes = route_sheet_wires(sheet, s.catalog, project=s.project)
        marks = place_wire_marks(sheet, s.catalog, routes, ladder=True, project=s.project)
        boxes: list[tuple[str, tuple[float, float, float, float]]] = []
        for wire in sheet.wires:
            at = marks.get(wire.id)
            if not at:
                continue
            box = mark_box(at[0], at[1], wire.drawn_mark(ladder=True), align=at[2])
            for other_id, other in boxes:
                assert not boxes_overlap(box, other), (sheet.id, wire.id, other_id)
            boxes.append((f"{wire.id}:{wire.number}", box))
        for i, a in enumerate(sheet.wires):
            for b in sheet.wires[i + 1 :]:
                if not a.number or a.number != b.number:
                    continue
                if not (
                    routes_share_line(routes.get(a.id) or [], routes.get(b.id) or [])
                    or routes_share_collinear_run(routes.get(a.id) or [], routes.get(b.id) or [])
                ):
                    continue
                have = [wid for wid in (a.id, b.id) if wid in marks]
                assert len(have) == 1, (sheet.id, a.id, b.id, a.number)


def test_demo_sheet_text_does_not_collide():
    from panelcad.core.ladder import is_ladder_sheet

    for builder in (build_nema_seal_in, build_iec_mirror):
        session = builder()
        for index, sheet in enumerate(session.project.sheets, start=1):
            if sheet.kind not in {"schematic", "io"}:
                continue
            ladder = is_ladder_sheet(sheet, session.project)
            hits = colliding_sheet_labels(
                session.project,
                session.catalog,
                sheet,
                ladder=ladder,
                sheet_no=index,
            )
            assert hits == [], hits


def test_nc_mark_styles():
    assert mark_nc("105", "NC", "parentheses") == "(105)"
    assert mark_nc("105", "NO", "parentheses") == "105"
    assert mark_nc("105", "NC", "underline") == "105"
    assert nc_draw_mark("NC", "underline") == "underline"
    assert nc_draw_mark("NC", "bar") == "bar"
    assert nc_draw_mark("NC", "parentheses") == ""
    s = build_nema_seal_in()
    s.set_cross_reference(nc_mark="parentheses")
    cr = next(row for row in s.cross_reference_rows() if row["role"] == "coil" and row["child"] == "CR1")
    assert "(" in cr["xref"] and ")" in cr["xref"]


def test_offpage_callout_tracks_destination_line():
    s = build_nema_seal_in()
    src = next(c for c in s.sheet("control").components if c.id == "OP1")
    before = offpage_callout(s.project, s.catalog, s.sheet("control"), src)
    assert "TO" in before
    dest = s.sheet("aux").component("CR1-21")
    dest.y = 180
    after = offpage_callout(s.project, s.catalog, s.sheet("control"), src)
    assert after != before
    assert "TO" in after


def test_offpage_text_clears_rails():
    s = build_nema_seal_in()
    sheet = s.sheet("aux")
    placed = place_xref_labels(s.project, s.catalog, sheet, ladder=True)
    wx, wy, ww, wh = working_rect(sheet)
    left = wx + RAIL_INSET
    rail = (left - 2.6, wy + 8.0, left + 2.6, wy + wh - 6.0)
    offs = [item for item in placed if item[0].kind == "offpage"]
    assert offs
    for ann, x, y, align in offs:
        box = annotation_box(ann, x, y, align)
        assert not boxes_overlap(box, rail, pad=0.05), (ann.lines, box, rail)


def test_live_update_on_move_and_retag():
    s = Session.create("Live")
    s.place("iec.contactors.coil", ref="K1", x=40, y=30)
    s.place("iec.contactors.aux_no", ref="K1-13", x=40, y=150)
    first = next(row for row in s.cross_reference_rows() if row["role"] == "coil")
    contact = next(row for row in s.cross_reference_rows() if row["role"] == "contact")
    assert contact["location"] in first["xref"]
    s.move("K1", 40, 180)
    later = {row["role"]: row for row in s.cross_reference_rows() if row["parent"] == "K1"}
    assert later["coil"]["location"] != later["contact"]["location"] or later["coil"]["y"] != first["y"]
    s.add_sheet("two", kind="schematic")
    s.place("iec.contactors.aux_nc", ref="K1-21", x=40, y=40, sheet_id="two")
    coil = next(row for row in s.cross_reference_rows() if row["role"] == "coil")
    assert any(row["child"] == "K1-21" for row in s.cross_reference_rows())
    assert coil["xref"]
    s.retag("family")
    assert s.cross_reference_rows()


def test_contact_without_coil_and_count_and_duplicate():
    orphan = Session.create("Orphan")
    orphan.place("iec.contactors.aux_no", ref="K9", x=20, y=20)
    checks = {row["check"] for row in orphan.design_checks()}
    assert "contact_without_coil" in checks

    over = Session.create("Over")
    over.place("iec.contactors.coil", ref="K1", x=20, y=20)
    over.place("iec.contactors.aux_no", ref="K1-13", x=20, y=60)
    over.place("iec.contactors.aux_nc", ref="K1-21", x=20, y=100)
    over.place("iec.contactors.aux_no", ref="K1-31", x=20, y=140)
    assert not any(row["check"] == "contact_count_exceeded" for row in over.design_checks())
    over.assign_part("K1", "100-C09EJ10")
    found = [row for row in over.design_checks() if row["check"] == "contact_count_exceeded"]
    assert found
    assert "100-C09EJ10" in found[0]["message"]
    assert "auxiliary contact block" in found[0]["message"]
    assert "library allows" not in found[0]["message"]

    dup = Session.create("DupNo")
    dup.place("iec.contactors.coil", ref="K1", x=20, y=20)
    dup.place("iec.contactors.aux_no", ref="K1-13", x=20, y=60)
    dup.place("iec.contactors.aux_no", ref="AUX2", x=20, y=100, properties={"parent": "K1", "contact_no": "13"})
    dups = [row for row in dup.design_checks() if row["check"] == "duplicate_contact_number"]
    assert dups


def test_old_file_open_does_not_write_xref(tmp_path: Path):
    src = Path(__file__).resolve().parents[1] / "examples" / "dol_starter.pcad.yaml"
    original = src.read_text(encoding="utf-8")
    copy = tmp_path / src.name
    shutil.copy2(src, copy)
    s = Session.open(copy)
    assert s.cross_reference_rows() or s.cross_reference_report() is not None
    dumped = dump_project(s.project)
    assert "xref:" not in dumped
    assert "cross_reference:" not in dumped
    again = yaml.safe_load(dumped)
    first = yaml.safe_load(original)
    assert "cross_reference" not in again
    for sheet in first.get("sheets", []):
        for comp in sheet.get("components", []):
            assert "xref" not in (comp.get("properties") or {})
    dest = tmp_path / "copy.pcad.yaml"
    s.save(dest)
    loaded = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert "cross_reference" not in loaded
    for sheet in loaded.get("sheets", []):
        for comp in sheet.get("components", []):
            assert "xref" not in (comp.get("properties") or {})


def test_explicit_write_and_csv(tmp_path: Path):
    s = build_nema_seal_in()
    path = tmp_path / "xref.csv"
    s.export_cross_reference(path)
    text = path.read_text(encoding="utf-8")
    assert "coil" in text
    assert "M1" in text
    assert "CR1" in text
    s.update_cross_references()
    assert s.sheet("control").component("M1").properties.get("xref")


def test_svg_and_dxf_include_xref(tmp_path: Path):
    s = build_nema_seal_in()
    svg = s.export_svg(tmp_path / "nema.svg", sheet_id="control").read_text(encoding="utf-8")
    coil = next(row for row in s.cross_reference_rows() if row["role"] == "coil" and row["child"] == "CR1")
    assert coil["xref"]
    for token in coil["xref"].replace("(", "").replace(")", "").split(","):
        token = token.strip()
        if token:
            assert token in svg
    # Rung line numbers print at the left rail.
    assert str(coil["line"]) in svg
    assert "XREF" in LAYERS
    dxf = s.export_dxf(tmp_path / "nema.dxf", sheet_id="control")
    raw = dxf.read_text(encoding="utf-8", errors="ignore")
    assert "XREF" in raw


def test_iec_svg_draws_mirror_terminals(tmp_path: Path):
    s = build_iec_mirror()
    svg = s.export_svg(tmp_path / "iec.svg", sheet_id="main").read_text(encoding="utf-8")
    assert "13" in svg
    assert "14" in svg
    assert "21" in svg
    assert "22" in svg
    assert "/1.3" in svg


def test_agent_report_does_not_write():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Xref"})["ok"]
    assert agent.handle({"id": 2, "op": "place", "library_id": "iec.contactors.coil", "ref": "K1", "x": 20, "y": 20})["ok"]
    assert agent.handle({"id": 3, "op": "place", "library_id": "iec.contactors.aux_no", "ref": "K1-13", "x": 20, "y": 80})["ok"]
    listed = agent.handle({"id": 4, "op": "cross_reference"})
    assert listed["ok"], listed
    assert listed["result"]["rows"]
    assert listed["result"]["report"]
    settings = agent.handle(
        {"id": 5, "op": "set_cross_reference", "nc_mark": "bar", "rung_pitch_mm": 12.7, "rung_numbers": "all"}
    )
    assert settings["ok"]
    assert settings["result"]["nc_mark"] == "bar"
    assert settings["result"]["rung_numbers"] == "all"
    assert abs(float(settings["result"]["rung_pitch_mm"]) - 12.7) < 0.01


def test_check_ids_include_xref():
    from panelcad.core.checks import CHECK_IDS

    assert "contact_without_coil" in CHECK_IDS
    assert "contact_count_exceeded" in CHECK_IDS
    assert "duplicate_contact_number" in CHECK_IDS
    assert "broken_offpage" in CHECK_IDS


def test_rung_y_helper_matches_line():
    s = Session.create("Pitch")
    sheet = s.sheet()
    y = rung_y(sheet, 5)
    assert nema_line_at(sheet, y, 1) == 105


def _menu_action_texts(window, *titles: str) -> list[str]:
    actions = list(window.menuBar().actions())
    for title in titles:
        match = next(
            (item.menu() for item in actions if item.menu() and item.text().replace("&", "") == title),
            None,
        )
        assert match is not None, title
        actions = list(match.actions())
    return [item.text().replace("&", "") for item in actions if item.text()]


def _trigger_menu(window, *titles: str):
    actions = list(window.menuBar().actions())
    current = None
    for title in titles:
        current = next(
            (
                item
                for item in actions
                if item.text().replace("&", "") == title
                or (item.menu() and item.text().replace("&", "") == title)
            ),
            None,
        )
        assert current is not None, title
        if current.menu():
            actions = list(current.menu().actions())
    assert current is not None
    current.trigger()


def test_gui_writes_xref_and_sets_nema_pitch():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = Session.create("GuiXref")
    s.place("iec.contactors.coil", ref="K1", x=40, y=30)
    s.place("iec.contactors.aux_no", ref="K1-13", x=40, y=80)
    win = MainWindow(s)
    win.show()
    app.processEvents()
    prefs = _menu_action_texts(win, "Edit", "Drawing prefs")
    assert any(text.startswith("NEMA line pitch") for text in prefs)
    assert any("0.5 in" in text for text in prefs)
    reports = _menu_action_texts(win, "Reports")
    assert "Update cross-references" in reports
    _trigger_menu(win, "Edit", "Drawing prefs", "NEMA line pitch: 1 in (25.4 mm)")
    assert abs(float(win.session.cross_reference_settings()["rung_pitch_mm"]) - 25.4) < 0.01
    assert win.session.sheet().component("K1").properties.get("xref") is None
    _trigger_menu(win, "Reports", "Update cross-references")
    assert win.session.sheet().component("K1").properties.get("xref")
    win.close()


def test_agent_update_cross_references_and_pitch_help():
    agent = Agent()
    help_r = agent.handle({"id": 1, "op": "help"})
    assert help_r["ok"]
    ops = help_r["result"]["ops"]
    help_text = help_r["result"]["help"]
    assert "update_cross_references" in ops
    assert "set_cross_reference" in ops
    assert "rung_pitch_mm" in help_text["set_cross_reference"]
    assert "12.7" in help_text["set_cross_reference"]
    assert "persist" in help_text["update_cross_references"].lower() or "write" in help_text["update_cross_references"].lower()
    assert agent.handle({"id": 2, "op": "create_project", "name": "XrefWrite"})["ok"]
    assert agent.handle({"id": 3, "op": "place", "library_id": "iec.contactors.coil", "ref": "K1", "x": 20, "y": 20})["ok"]
    assert agent.handle({"id": 4, "op": "place", "library_id": "iec.contactors.aux_no", "ref": "K1-13", "x": 20, "y": 80})["ok"]
    pitched = agent.handle({"id": 5, "op": "set_cross_reference", "rung_pitch_mm": 25.4})
    assert pitched["ok"], pitched
    assert abs(float(pitched["result"]["rung_pitch_mm"]) - 25.4) < 0.01
    wrote = agent.handle({"id": 6, "op": "update_cross_references"})
    assert wrote["ok"], wrote
    assert wrote["result"]["rows"]
    assert agent.session.sheet().component("K1").properties.get("xref")


def test_examples_snapshot_ignores_sidecars_and_retries_a_lock(tmp_path, monkeypatch):
    """A journal or a brief Windows lock must not fail the last test in the suite."""

    import tests.conftest as project_conftest

    root = tmp_path / "examples"
    root.mkdir()
    drawing = root / "job.pcad.yaml"
    drawing.write_bytes(b"name: Job\r\n")
    journal = root / "job.pcad.ai.jsonl"
    journal.write_bytes(b'{"op":"a"}\n')
    (root / "Thumbs.db").write_bytes(b"xx")
    monkeypatch.setattr(project_conftest, "_EXAMPLES_READ_WAIT_S", 0)
    first = project_conftest._examples_snapshot(root)
    assert list(first) == ["job.pcad.yaml"]
    journal.write_bytes(b'{"op":"a"}\n{"t":"later"}\n')
    (root / "desktop.ini").write_bytes(b"[.ShellClassInfo]\n")
    again = project_conftest._examples_snapshot(root)
    assert again == first
    lf = tmp_path / "lf"
    lf.mkdir()
    (lf / "job.pcad.yaml").write_bytes(b"name: Job\n")
    assert project_conftest._examples_snapshot(lf) == first

    calls = {"n": 0}
    real = Path.read_bytes

    def flaky(self, *args, **kwargs):
        if self.name == "job.pcad.yaml":
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    assert project_conftest._examples_snapshot(root) == first
    assert calls["n"] == 3
    drawing.write_bytes(b"name: Other\n")
    assert project_conftest._examples_snapshot(root) != first


def test_seat_box_matches_annotation_box():
    """The search builds the same rectangle as ``annotation_box`` without measuring text twice."""

    from panelcad.core.xref import XrefAnnotation
    from panelcad.render.xref import _seat_box, _seat_metrics, annotation_box

    ann = XrefAnnotation(
        sheet_id="s",
        owner="K1",
        kind="contact",
        style="nema",
        lines=["13 4", "14"],
        marks=[""],
        jump_sheet="s",
        jump_ref="K1",
        jump_x=10,
        jump_y=20,
        prefer="below",
        anchor_x=30,
        anchor_y=40,
        size=2.0,
    )
    mirror, w, h, size = _seat_metrics(ann)
    for align in ("start", "middle", "end"):
        for x, y in ((0.0, 0.0), (12.5, -4.0), (80.0, 33.0)):
            assert _seat_box(x, y, align, w, h, size, mirror) == annotation_box(ann, x, y, align)


def test_xref_overlap_index_matches_a_full_scan():
    """Cross-reference hit counts stay exact when the obstacle list is long."""

    import random
    import time

    from panelcad.render.wire_labels import boxes_overlap
    from panelcad.render.xref import _BoxIndex

    rng = random.Random(1)
    boxes = []
    for _ in range(100):
        x = rng.uniform(0.0, 300.0)
        y = rng.uniform(0.0, 200.0)
        boxes.append((x, y, x + rng.uniform(0.4, 48.0), y + rng.uniform(0.4, 24.0)))
    boxes.append(boxes[3])
    index = _BoxIndex(list(boxes))
    for _ in range(30):
        x = rng.uniform(-20.0, 320.0)
        y = rng.uniform(-20.0, 220.0)
        query = (x, y, x + rng.uniform(2.0, 28.0), y + rng.uniform(2.0, 14.0))
        for pad in (0.0, 0.15, 0.3, 0.4):
            full = sum(1 for other in boxes if boxes_overlap(query, other, pad=pad))
            assert index.count(query, pad) == full
            capped = index.count(query, pad, limit=1)
            assert capped == full if full <= 1 else capped == 2
    crowded = []
    for i in range(2000):
        x = (i % 50) * 8.0
        y = (i // 50) * 6.0
        crowded.append((x, y, x + 20.0, y + 3.0))
    fast = _BoxIndex(crowded)
    started = time.perf_counter()
    total = 0
    for i in range(1500):
        x = (i % 40) * 7.0
        y = (i % 25) * 5.0
        total += fast.count((x, y, x + 12.0, y + 6.0), 0.15, limit=0)
    elapsed = time.perf_counter() - started
    assert total >= 0
    assert elapsed < 0.75, elapsed


def test_contact_list_label_normalizes_iec_terminals_in_nema_style():
    """Patch 3: contact_list_label normalizes IEC contact terminals to NEMA 3-4 / 1-2."""

    from panelcad.core.models import Component, Device, Sheet
    from panelcad.core.xref import Location, XrefMember, contact_list_label

    sheet = Sheet(id="s1", title="Control")
    loc = Location(
        sheet_id="s1",
        sheet_no=2,
        row="B",
        col=6,
        row_index=2,
        line=208,
        x=120.0,
        y=60.0,
    )
    dummy_dev = Device(id="dummy", name="dummy", library_id="dummy", category="dummy")

    def make_member(comp: Component, state: str, contact_no: str) -> XrefMember:
        return XrefMember(
            sheet=sheet,
            comp=comp,
            device=dummy_dev,
            role="contact",
            state=state,
            contact_no=contact_no,
            family="M1",
            location=loc,
            element="contact",
        )

    c_no2 = Component(
        id="M1-23",
        library_id="nema.contactors.aux_no",
        x=0,
        y=0,
        properties={"terminals": "23-24", "pin_labels": {"3": "23", "4": "24"}},
    )
    m_no2 = make_member(c_no2, "NO", "23-24")
    assert contact_list_label(m_no2, "nema", "parentheses")[0] == "3-4 208"
    assert contact_list_label(m_no2, "iec", "parentheses")[0] == "23-24 /2.6"

    c_tr_nc = Component(
        id="TR1-16",
        library_id="nema.contactors.aux_nc",
        x=0,
        y=0,
        properties={"terminals": "15-16", "delayed": "NC"},
    )
    m_tr_nc = make_member(c_tr_nc, "NC", "15-16")
    assert contact_list_label(m_tr_nc, "nema", "parentheses")[0] == "(1-2 208)"
    assert contact_list_label(m_tr_nc, "iec", "parentheses")[0] == "(15-16 /2.6)"

    c_tr_no = Component(
        id="TR1-18",
        library_id="nema.contactors.aux_no",
        x=0,
        y=0,
        properties={"terminals": "15-18", "delayed": "NO"},
    )
    m_tr_no = make_member(c_tr_no, "NO", "15-18")
    assert contact_list_label(m_tr_no, "nema", "parentheses")[0] == "3-4 208"
    assert contact_list_label(m_tr_no, "iec", "parentheses")[0] == "15-18 /2.6"

    c_ol = Component(
        id="OL1-95",
        library_id="nema.contactors.ol_contact",
        x=0,
        y=0,
        properties={"terminals": "95-96"},
    )
    m_ol = make_member(c_ol, "NC", "95-96")
    assert contact_list_label(m_ol, "nema", "parentheses")[0] == "(95-96 208)"
    assert contact_list_label(m_ol, "iec", "parentheses")[0] == "(95-96 /2.6)"


def test_offpage_callout_reformats_bare_supply_ref_across_standards():
    """Patch 3: offpage_callout reformats bare supply_ref xrefs across NEMA and IEC."""

    from panelcad.api.session import Session
    from panelcad.core.models import Component
    from panelcad.core.xref import offpage_callout

    s_nema = Session.create("op_nema")
    s_nema.set_title_block(standard="NEMA/ANSI")
    sheet_nema = s_nema.project.sheets[0]
    sheet_nema.standard = "NEMA/ANSI"
    sheet_nema.drawing_style = "ladder"

    op_from_iec = Component(
        id="OP1",
        library_id="nema.interconnect.supply_ref",
        x=0,
        y=0,
        properties={"callout": "/1.2", "net": "L1"},
    )
    assert offpage_callout(s_nema.project, s_nema.catalog, sheet_nema, op_from_iec) == "102"

    op_pe_from_iec = Component(
        id="OP3",
        library_id="nema.interconnect.supply_ref",
        x=0,
        y=0,
        properties={"callout": "PE /1.1", "net": "PE"},
    )
    assert offpage_callout(s_nema.project, s_nema.catalog, sheet_nema, op_pe_from_iec) == "PE 101"

    s_iec = Session.create("op_iec")
    s_iec.set_title_block(standard="IEC")
    sheet_iec = s_iec.project.sheets[0]
    sheet_iec.standard = "IEC"
    sheet_iec.drawing_style = "iec"

    op_from_nema = Component(
        id="OP1",
        library_id="iec.interconnect.supply_ref",
        x=0,
        y=0,
        properties={"callout": "102", "net": "L1"},
    )
    assert offpage_callout(s_iec.project, s_iec.catalog, sheet_iec, op_from_nema) == "/1.2"


def test_xref_nema_seal_in_example_has_zero_offpage_missing_from():
    from pathlib import Path
    from panelcad.api.session import Session

    example = Path(__file__).resolve().parents[1] / "examples" / "xref_nema_seal_in.pcad.yaml"
    s = Session.open(example)
    missing = [row for row in s.design_checks() if row.get("check") == "offpage_missing_from"]
    assert missing == []
