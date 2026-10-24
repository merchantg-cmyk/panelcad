"""IEC <-> NEMA conversion: table, round trip, geometry, GUI, session, JSONL, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from panelcad.api.session import Session
from panelcad.cli.agent import Agent
from panelcad.cli.main import app
from panelcad.convert.netlist import same_netlist, sheet_edges
from panelcad.convert.standard_map import ENTRIES
from panelcad.core.phase_color import palette_for
from panelcad.io.bom import grouped_rows, instance_rows
from tests.test_circuit_builder import (
    _assert_conductors_land,
    _assert_contact_pins_not_dangling,
    _assert_drawn_segments_match_pins,
    _assert_marks_clear_of_pins,
    _assert_star_delta_power_geometry,
    _assert_wires_clear_text,
    _assert_xref_in_frame,
    _assert_xref_stays_in_column,
)

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "dol_starter.pcad.yaml"
KINDS = ["dol", "vfd", "reversing", "star_delta", "soft_starter", "plc_motor"]


def _session(name: str, standard: str) -> Session:
    session = Session.create(name)
    if standard == "IEC":
        session.set_title_block(standard="IEC")
    return session


def _insert(session: Session, kind: str, *, standard: str) -> dict:
    kwargs: dict = {"kind": kind, "control": "local", "standard": standard}
    if kind == "vfd":
        kwargs["drive"] = "generic_1k5"
    if kind == "star_delta":
        kwargs["timer_seconds"] = 8
    if kind == "plc_motor":
        kwargs["hp"] = 5
        kwargs["voltage"] = 480
    return session.insert_circuit(**kwargs)


def _tags(session: Session) -> list[str]:
    return sorted(comp.id for sheet in session.project.sheets for comp in sheet.components)


def _numbers(session: Session) -> list[tuple[str, str, str]]:
    rows = []
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            rows.append((sheet.id, wire.id, str(wire.number or "")))
    return sorted(rows)


def _bom(session: Session) -> tuple[int, int, int]:
    inst = instance_rows(session.project, session.catalog)
    grp = grouped_rows(session.project, session.catalog)
    return (len(inst), len(grp), sum(int(row["qty"]) for row in grp))


def _convert(session: Session, target: str, tmp: Path, name: str, **kwargs) -> dict:
    return session.convert_standard(
        target,
        out=tmp / f"{name}.pcad.yaml",
        workspace=tmp / f"{name}.convert.json",
        **kwargs,
    )


def _renamed_numbers(
    before: list[tuple[str, str, str]],
    rail_map: dict,
) -> list[tuple[str, str, str]]:
    """Ordinary numbers stay. A number that was the source rail becomes the target rail."""

    from panelcad.convert.layout_convert import _rail_rename

    rows = []
    for sheet_id, wire_id, number in before:
        pair = rail_map.get(sheet_id) or {}
        rename = _rail_rename(
            str(pair.get("from_hot") or ""),
            str(pair.get("from_ret") or ""),
            str(pair.get("to_hot") or ""),
            str(pair.get("to_ret") or ""),
        )
        rows.append((sheet_id, wire_id, rename.get(number, number)))
    return rows


def _assert_source_rails_gone(session: Session, result: dict) -> None:
    for sheet_id, pair in (result.get("rail_map") or {}).items():
        banned: set[str] = set()
        for src, dst in (("from_hot", "to_hot"), ("from_ret", "to_ret")):
            old = str(pair.get(src) or "")
            new = str(pair.get(dst) or "")
            if old and old != new:
                banned.add(old)
        if not banned:
            continue
        sheet = session.sheet(sheet_id)
        found = {str(sheet.rail_hot or ""), str(sheet.rail_return or "")}
        for wire in sheet.wires:
            found.update(
                {
                    str(wire.number or ""),
                    str(wire.net or ""),
                    str(wire.label or ""),
                    str(wire.frm or ""),
                    str(wire.to or ""),
                }
            )
        leaked = banned & found
        assert not leaked, (sheet_id, leaked, pair)


def _assert_pin_marks(session: Session) -> None:
    from panelcad.core.pins import pin_caption

    for sheet in session.project.sheets:
        for comp in sheet.components:
            try:
                device = session.catalog.get(comp.library_id)
            except KeyError:
                continue
            for pin in device.pins:
                cap = pin_caption(pin, comp).strip().lower()
                assert cap not in {"plus", "minus"}, (comp.id, pin.id, cap)


def _assert_family_numbers(session: Session) -> None:
    """Each letter family is 1, 2, 3… with no holes after a fresh conversion."""

    import re

    groups: dict[str, set[int]] = {}
    for comp_id in _tags(session):
        match = re.match(r"^([A-Za-z]+)(\d+)", comp_id)
        if match is None:
            continue
        groups.setdefault(match.group(1), set()).add(int(match.group(2)))
    for prefix, numbers in groups.items():
        assert numbers == set(range(1, max(numbers) + 1)), (prefix, sorted(numbers))


def _geometry(session: Session, result: dict, *, columns: bool, kind: str, standard: str) -> None:
    reference: Session | None = None
    for key in ("sheet", "control_sheet", "io_sheet"):
        sheet_id = result.get(key)
        if not sheet_id:
            continue
        _assert_contact_pins_not_dangling(session, sheet_id)
        _assert_marks_clear_of_pins(session, sheet_id)
        _assert_drawn_segments_match_pins(session, sheet_id, bodies=True)
        hits = _assert_wires_clear_text(session, sheet_id, collect=True) or []
        if hits:
            # A fresh insert of the target standard may already graze a tag.
            # Conversion may not add a crossing that insert does not draw.
            if reference is None:
                reference = _session(f"native {kind} {standard}", standard)
                _insert(reference, kind, standard=standard)
            allowed = set(_assert_wires_clear_text(reference, sheet_id, collect=True) or [])
            extra = [hit for hit in hits if hit not in allowed]
            assert not extra, extra
        _assert_xref_in_frame(session, sheet_id)
        if columns and key == "control_sheet":
            _assert_xref_stays_in_column(session, sheet_id)


def _remap(result: dict, tag_map: dict[str, str]) -> dict:
    cloned = dict(result)
    cloned["components"] = [
        {**row, "id": tag_map.get(row["id"], row["id"])} for row in result["components"]
    ]
    return cloned


def test_symbol_table_uses_library_symbols_and_matching_pins():
    catalog = Session.create("map").catalog
    seen: set[str] = set()
    for row in ENTRIES:
        nema = catalog.get(row["nema"])
        iec = catalog.get(row["iec"])
        assert row["id"] not in seen
        seen.add(row["id"])
        nema_pins = {pin.id: pin for pin in nema.pins}
        iec_pins = {pin.id: pin for pin in iec.pins}
        forward = {str(a): str(b) for a, b in (row.get("pins_nema_to_iec") or {}).items()}
        assert len(forward) == len(set(forward.values()))
        for src, dst in forward.items():
            assert src in nema_pins
            assert dst in iec_pins
            # The 3-lead motors are not the same outline: NEMA is centered on
            # x=18 (T2/T3 at 18/28) and IEC on x=20 (V1/W1 at 20/32). Relayout
            # reconnects those leads. Every other mapped pair shares a coordinate.
            dx = abs(nema_pins[src].x - iec_pins[dst].x)
            dy = abs(nema_pins[src].y - iec_pins[dst].y)
            if row["id"] == "loads.motor_3ph" and (src, dst) in {("T2", "V1"), ("T3", "W1")}:
                assert dy < 0.2
                assert dx <= 4.0
                continue
            assert dx < 0.2
            assert dy < 0.2
        for pin in row.get("iec_only") or []:
            assert str(pin) in iec_pins
            assert str(pin) not in forward.values()
        for pin in row.get("nema_only") or []:
            assert str(pin) in nema_pins


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("standard", ["NEMA/ANSI", "IEC"])
def test_circuit_round_trip_keeps_netlist_tags_and_bom(kind: str, standard: str, tmp_path: Path):
    session = _session(kind, standard)
    _insert(session, kind, standard=standard)
    before_edges = sheet_edges(session.project)
    before_tags = _tags(session)
    before_numbers = _numbers(session)
    before_bom = _bom(session)
    target = "iec" if standard == "NEMA/ANSI" else "nema"
    back = "nema" if target == "iec" else "iec"
    mid = _convert(session, target, tmp_path, "mid")
    assert _numbers(session) == _renamed_numbers(before_numbers, mid["rail_map"])
    _assert_source_rails_gone(session, mid)
    _assert_pin_marks(session)
    _assert_family_numbers(session)
    assert _bom(session) == before_bom
    assert same_netlist(
        before_edges,
        sheet_edges(session.project),
        tag_map=mid["tag_map"],
        pin_rewrites=mid["pin_rewrites"],
        rail_map=mid["rail_map"],
    )
    _assert_pins_on_body_and_in_frame(session)
    _assert_conductors_land(session)
    _convert(session, back, tmp_path, "back")
    _assert_conductors_land(session)
    assert sheet_edges(session.project) == before_edges
    assert _tags(session) == before_tags
    assert _numbers(session) == before_numbers
    assert _bom(session) == before_bom


def test_converted_sheets_pass_geometry(tmp_path: Path):
    nema = _session("geom", "NEMA/ANSI")
    reversing = _insert(nema, "reversing", standard="NEMA/ANSI")
    rev = _convert(nema, "iec", tmp_path, "rev")
    _geometry(nema, _remap(reversing, rev["tag_map"]), columns=True, kind="reversing", standard="IEC")
    iec_palette = palette_for(nema.project, nema.project.sheet(reversing["sheet"]))
    assert iec_palette is not None
    assert iec_palette[0] == "iec"
    assert iec_palette[1].l2 == "#111111"
    assert iec_palette[1].l3 == "#7f7f7f"

    star = _session("star", "NEMA/ANSI")
    built = _insert(star, "star_delta", standard="NEMA/ANSI")
    mapped = _convert(star, "iec", tmp_path, "star")
    _assert_star_delta_power_geometry(star, _remap(built, mapped["tag_map"]))
    _geometry(star, _remap(built, mapped["tag_map"]), columns=True, kind="star_delta", standard="IEC")

    plc = _session("plc", "NEMA/ANSI")
    plc_built = _insert(plc, "plc_motor", standard="NEMA/ANSI")
    plc_map = _convert(plc, "iec", tmp_path, "plc")
    _geometry(plc, _remap(plc_built, plc_map["tag_map"]), columns=True, kind="plc_motor", standard="IEC")

    iec = _session("back", "IEC")
    iec_built = _insert(iec, "reversing", standard="IEC")
    iec_map = _convert(iec, "nema", tmp_path, "iec-rev")
    _geometry(iec, _remap(iec_built, iec_map["tag_map"]), columns=False, kind="reversing", standard="NEMA/ANSI")
    nema_palette = palette_for(iec.project, iec.project.sheet(iec_built["sheet"]))
    assert nema_palette is not None
    assert nema_palette[0] == "nema_high"
    assert nema_palette[1].l2 == "#e06a12"
    assert nema_palette[1].l3 == "#c9a000"
    for drawn in (nema, star, plc, iec):
        _assert_pins_on_body_and_in_frame(drawn)
        _assert_conductors_land(drawn)


def test_demo_round_trip(tmp_path: Path):
    original = DEMO.read_bytes()
    session = Session.open(DEMO)
    before_edges = sheet_edges(session.project)
    before_tags = _tags(session)
    before_bom = _bom(session)
    first = session.project.sheets[0].id
    to_nema = _convert(session, "nema", tmp_path, "demo-nema")
    assert session.project.sheets[0].id == first
    assert _bom(session) == before_bom
    assert same_netlist(
        before_edges,
        sheet_edges(session.project),
        tag_map=to_nema["tag_map"],
        pin_rewrites=to_nema["pin_rewrites"],
        rail_map=to_nema["rail_map"],
    )
    assert to_nema["unmapped"]
    symbols = [row for row in to_nema["unmapped"] if row["flag"] == "unmapped_symbol"]
    assert symbols
    assert all("Leave the vendor symbol" in str(row.get("detail") or "") for row in symbols)
    assert all("pick a counterpart" in str(row.get("detail") or "") for row in symbols)
    pins = [row for row in to_nema["unmapped"] if row["flag"] == "unmapped_pin"]
    assert all("same terminals" in str(row.get("detail") or "") for row in pins)
    # Device U1 becomes VFD1. The motor lead net U1 is a terminal name, not that tag.
    assert any(comp.id == "VFD1" for sheet in session.project.sheets for comp in sheet.components)
    power = session.project.sheets[0]
    lead_nets = {str(wire.net or "") for wire in power.wires}
    lead_labels = {str(wire.label or "") for wire in power.wires}
    assert {"U1", "V1", "W1"} <= lead_nets
    assert "VFD1" not in lead_nets
    assert "VFD1" not in lead_labels
    ends = {wire.frm for wire in power.wires} | {wire.to for wire in power.wires}
    assert any(end.endswith(".T1") for end in ends)
    assert not any(end.startswith("VFD1.") and end.rsplit(".", 1)[-1] in {"U1", "V1", "W1", "T1"} for end in ends)
    aux = [row for row in pins if "97" in str(row.get("detail") or "")]
    assert aux
    assert all("not wired" in str(row.get("detail") or "") for row in aux)
    _assert_pins_on_body_and_in_frame(session)
    _assert_conductors_land(session)
    _convert(session, "iec", tmp_path, "demo-iec")
    _assert_conductors_land(session)
    assert sheet_edges(session.project) == before_edges
    assert _tags(session) == before_tags
    assert _bom(session) == before_bom
    assert DEMO.read_bytes() == original


def test_unmapped_symbol_is_kept(tmp_path: Path):
    session = _session("unmap", "IEC")
    session.place("iec.operators.selector_2pos", ref="S9", x=40, y=40)
    result = _convert(session, "nema", tmp_path, "unmap")
    kept = [
        comp
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.library_id == "iec.operators.selector_2pos"
    ]
    assert len(kept) == 1
    assert any(row["library_id"] == "iec.operators.selector_2pos" for row in result["unmapped"])
    notes = " ".join(str(row.get("detail") or "") for row in result["unmapped"])
    notes += " ".join(str(chunk.get("suggestion") or "") for chunk in result["chunks"])
    assert "Leave the vendor symbol" in notes
    assert "pick a counterpart" in notes
    assert "Deterministic" not in notes


def test_source_file_is_not_overwritten(tmp_path: Path):
    session = _session("save", "NEMA/ANSI")
    _insert(session, "dol", standard="NEMA/ANSI")
    source = tmp_path / "source.pcad.yaml"
    session.save(source)
    raw = source.read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        session.convert_standard("iec", out=source)
    assert source.read_bytes() == raw
    out = tmp_path / "source-iec.pcad.yaml"
    session.convert_standard("iec", out=out)
    assert source.read_bytes() == raw
    assert out.is_file()
    assert out.resolve() != source.resolve()
    assert session.path == out
    with pytest.raises(ValueError, match="overwrite"):
        session.convert_standard("nema", out=session.path)
    assert source.read_bytes() == raw


def test_undo_restores_drawing_and_path(tmp_path: Path):
    session = _session("undo", "NEMA/ANSI")
    _insert(session, "dol", standard="NEMA/ANSI")
    source = tmp_path / "undo.pcad.yaml"
    session.save(source)
    tags = _tags(session)
    session.convert_standard("iec", out=tmp_path / "undo-iec.pcad.yaml")
    assert session.path == tmp_path / "undo-iec.pcad.yaml"
    assert _tags(session) != tags
    assert session.undo()
    assert _tags(session) == tags
    assert session.path == source
    assert source.read_bytes() == (tmp_path / "undo.pcad.yaml").read_bytes()


def test_resume_continues_at_the_next_chunk(tmp_path: Path):
    session = _session("resume", "NEMA/ANSI")
    _insert(session, "reversing", standard="NEMA/ANSI")
    source = tmp_path / "resume.pcad.yaml"
    session.save(source)
    out = tmp_path / "resume-iec.pcad.yaml"
    workspace = tmp_path / "resume.convert.json"
    plan = session.convert_standard_plan("iec", out=out, workspace=workspace)
    assert plan["pending"]
    first = session.convert_standard_next(workspace)
    assert first["chunks"][0]["status"] == "done"
    stopped = json.loads(workspace.read_text(encoding="utf-8"))
    assert stopped["working_project"]
    other = Session.open(source)
    other.convert_standard_plan("", resume=workspace)
    finished = other.convert_standard_apply(workspace)
    assert finished["done"]
    assert finished["pending"] == []
    direct = _session("resume-direct", "NEMA/ANSI")
    _insert(direct, "reversing", standard="NEMA/ANSI")
    direct.convert_standard("iec", out=tmp_path / "direct.pcad.yaml", workspace=tmp_path / "direct.convert.json")
    assert _tags(other) == _tags(direct)


def test_jsonl_agent_steps_chunks(tmp_path: Path):
    agent = Agent()
    created = agent.handle({"id": "corr", "op": "create_project", "name": "Agent convert"})
    assert created["ok"]
    inserted = agent.handle({"id": "corr-2", "op": "insert_circuit", "kind": "dol", "control": "local"})
    assert inserted["ok"], inserted
    out = tmp_path / "agent-iec.pcad.yaml"
    workspace = tmp_path / "agent.convert.json"
    plan = agent.handle(
        {
            "id": "plan-id",
            "op": "convert_standard_plan",
            "to": "iec",
            "out": str(out),
            "workspace": str(workspace),
        }
    )
    assert plan["ok"], plan
    assert plan["result"]["planned_tag_map"]
    stepped = agent.handle({"id": "next-id", "op": "convert_standard_next", "workspace": str(workspace)})
    assert stepped["ok"], stepped
    assert stepped["result"]["chunk"]["id"] == "tags"
    done = agent.handle({"id": "apply-id", "op": "convert_standard_apply", "workspace": str(workspace)})
    assert done["ok"], done
    assert done["result"]["done"]
    assert out.is_file()
    review = agent.handle({"id": "review-id", "op": "convert_standard_review", "workspace": str(workspace)})
    assert review["ok"]
    assert any(row["status"] in {"done", "accepted"} for row in review["result"]["chunks"])


def test_cli_convert_and_resume(tmp_path: Path):
    session = _session("cli", "NEMA/ANSI")
    _insert(session, "dol", standard="NEMA/ANSI")
    source = tmp_path / "cli.pcad.yaml"
    session.save(source)
    out = tmp_path / "cli-iec.pcad.yaml"
    workspace = tmp_path / "cli.convert.json"
    runner = CliRunner()
    plan = runner.invoke(
        app,
        ["convert", str(source), "--to", "iec", "--out", str(out), "--resume", str(workspace)],
    )
    # resume of a missing workspace is an error; the one-shot path is --to.
    assert plan.exit_code != 0

    once = runner.invoke(app, ["convert", str(source), "--to", "iec", "--out", str(out)])
    assert once.exit_code == 0, once.output
    assert out.is_file()
    payload = json.loads(once.output)
    assert payload["target"] == "IEC"
    assert payload["pending"] == []
    opened = Session.open(out)
    assert opened.project.title_block.standard == "IEC"

    fresh = _session("cli2", "NEMA/ANSI")
    _insert(fresh, "dol", standard="NEMA/ANSI")
    src2 = tmp_path / "cli2.pcad.yaml"
    fresh.save(src2)
    ws = tmp_path / "cli2.convert.json"
    partial = Session.open(src2)
    partial.convert_standard_plan("iec", out=tmp_path / "cli2-iec.pcad.yaml", workspace=ws)
    partial.convert_standard_next(ws)
    resumed = runner.invoke(
        app,
        ["convert", str(src2), "--resume", str(ws), "--out", str(tmp_path / "cli2-iec.pcad.yaml")],
    )
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.output)["pending"] == []


def test_sequential_tags_and_kept_numbers(tmp_path: Path):
    """IEC reversing becomes ES1, PB1, PB2, PB3. Keeping numbers leaves the PB2 gap.

    Either way, converting back restores the original tags.
    """

    fresh = _session("seq", "IEC")
    _insert(fresh, "reversing", standard="IEC")
    original = _tags(fresh)
    _convert(fresh, "nema", tmp_path, "seq")
    ids = set(_tags(fresh))
    assert "ES1" in ids
    assert {"PB1", "PB2", "PB3"} <= ids
    assert "PB4" not in ids
    _assert_family_numbers(fresh)
    _convert(fresh, "iec", tmp_path, "seq-back")
    assert _tags(fresh) == original

    kept = _session("kept", "IEC")
    _insert(kept, "reversing", standard="IEC")
    before = _tags(kept)
    _convert(kept, "nema", tmp_path, "kept", keep_numbers=True)
    kept_ids = set(_tags(kept))
    assert "ES1" in kept_ids
    assert "PB1" not in kept_ids
    assert {"PB2", "PB3", "PB4"} <= kept_ids
    _convert(kept, "iec", tmp_path, "kept-back")
    assert _tags(kept) == before


def test_jsonl_keep_numbers(tmp_path: Path):
    agent = Agent()
    created = agent.handle({"id": "a", "op": "create_project", "name": "Keep numbers"})
    assert created["ok"]
    titled = agent.handle({"id": "b", "op": "set_title_block", "standard": "IEC"})
    assert titled["ok"], titled
    inserted = agent.handle(
        {"id": "c", "op": "insert_circuit", "kind": "reversing", "control": "local", "standard": "IEC"}
    )
    assert inserted["ok"], inserted
    plan = agent.handle(
        {
            "id": "d",
            "op": "convert_standard_plan",
            "to": "nema",
            "keep_numbers": True,
            "out": str(tmp_path / "keep-nema.pcad.yaml"),
            "workspace": str(tmp_path / "keep.convert.json"),
        }
    )
    assert plan["ok"], plan
    values = set(plan["result"]["planned_tag_map"].values())
    assert {"PB2", "PB3", "PB4"} <= values
    assert "PB1" not in values


def test_gui_convert_dialog_reviews_chunks(tmp_path: Path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog, QFrame, QLabel, QPushButton

    from panelcad.gui.convert_dialog import ConvertDialog
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    session = _session("gui", "NEMA/ANSI")
    _insert(session, "dol", standard="NEMA/ANSI")
    source = tmp_path / "gui.pcad.yaml"
    session.save(source)
    window = MainWindow(session)
    window.show()
    texts = []

    def _walk(menu):
        for action in menu.actions():
            texts.append(action.text())
            if action.menu() is not None:
                _walk(action.menu())

    _walk(window.menuBar())
    assert "Convert drawing standard..." in texts

    dialog = ConvertDialog(session, window)
    dialog.whole.setChecked(True)
    dialog.iec.setChecked(True)
    assert dialog.keep_numbers.isChecked() is False
    dialog.path.setText(str(tmp_path / "gui-iec.pcad.yaml"))
    dialog.show()
    result = dialog.perform()
    app.processEvents()
    assert result["target"] == "IEC"
    texts = [lab.text() for lab in dialog.findChildren(QLabel)]
    assert not any("Review appears after" in text for text in texts)
    header = dialog.findChild(QLabel, "convert-review-header")
    assert header is not None and header.isVisible()
    assert header.text().startswith("Target")
    for lab in dialog.review_host.findChildren(QLabel):
        if lab is header or not lab.isVisible() or not lab.text():
            continue
        if lab.parent() is dialog.review_host:
            assert not header.geometry().intersects(lab.geometry()), (header.text(), lab.text())
    sheet_id = next(chunk["sheet"] for chunk in result["chunks"] if chunk.get("sheet"))
    thumb = dialog.findChild(QFrame, f"thumb:Before:{sheet_id}")
    assert thumb is not None
    QTest.mouseClick(thumb, Qt.MouseButton.LeftButton)
    app.processEvents()
    compare = dialog.findChild(QDialog, "convert-compare")
    assert compare is not None and compare.isVisible()
    before_img = compare.findChild(QLabel, "compare-image:Before")
    after_img = compare.findChild(QLabel, "compare-image:After")
    assert before_img is not None and before_img.pixmap() is not None
    assert after_img is not None and after_img.pixmap() is not None
    assert before_img.pixmap().width() > 240
    assert after_img.pixmap().width() > 240
    compare.close()
    accept = dialog.findChild(QPushButton, "accept:tags")
    assert accept is not None
    accept.click()
    app.processEvents()
    tags = next(chunk for chunk in session.convert_standard_review(result["workspace"])["chunks"] if chunk["id"] == "tags")
    assert tags["status"] == "accepted"
    rerun = dialog.findChild(QPushButton, "rerun:tags")
    assert rerun is not None
    revert = dialog.findChild(QPushButton, "revert:power:symbols") or dialog.findChild(QPushButton, "revert:tags")
    assert revert is not None
    window.close()


def _assert_pins_on_body_and_in_frame(session: Session) -> None:
    """Every pin sits on its drawn body. Nothing lands outside the frame or in the title block."""

    from panelcad.core.route import component_aabb
    from panelcad.render.sheet import pin_world
    from panelcad.render.titleblock import MARGIN, title_block_rect
    from panelcad.render.wire_labels import boxes_overlap, mark_box, place_wire_marks
    from panelcad.render.wiring import route_sheet_wires
    from panelcad.render.xref import annotation_box, place_xref_labels
    from panelcad.core.ink import schematic_ink_rect
    from panelcad.core.ladder import is_iec_column_sheet, is_ladder_sheet

    def _in_frame(box: tuple[float, float, float, float], frame: tuple[float, float, float, float], where: str) -> None:
        assert box[0] >= frame[0] - 0.4, (where, box, frame)
        assert box[1] >= frame[1] - 0.4, (where, box, frame)
        assert box[2] <= frame[2] + 0.4, (where, box, frame)
        assert box[3] <= frame[3] + 0.4, (where, box, frame)

    for sheet in session.project.sheets:
        if sheet.kind not in {"schematic", "io"}:
            continue
        pw, ph = sheet.page.dimensions()
        frame = (MARGIN, MARGIN, pw - MARGIN, ph - MARGIN)
        tx, ty, tw, th = title_block_rect(sheet)
        title = (tx, ty, tx + tw, ty + th)
        for comp in sheet.components:
            try:
                device = session.catalog.get(comp.library_id)
            except KeyError:
                continue
            scale = comp.visual_scale()
            width = float(device.schematic_width) * scale
            height = float(device.schematic_height) * scale
            if comp.properties.get("ladder_card") and int(comp.rotation or 0) == 0:
                height = max(height, float(comp.properties.get("card_h") or height) * scale)
            body = component_aabb(
                comp.x,
                comp.y,
                width,
                height,
                int(comp.rotation or 0),
                bool(comp.mirror),
            )
            for pin in device.pins:
                wx, wy = pin_world(comp, pin)
                assert body[0] - 0.6 <= wx <= body[2] + 0.6, (sheet.id, comp.id, pin.id, (wx, wy), body)
                assert body[1] - 0.6 <= wy <= body[3] + 0.6, (sheet.id, comp.id, pin.id, (wx, wy), body)
            ink = schematic_ink_rect(device, comp.x, comp.y, comp.id, component=comp)
            _in_frame(ink, frame, f"{sheet.id}:{comp.id}")
            assert not boxes_overlap(ink, title, pad=0.15), (sheet.id, comp.id, "title", ink, title)
        routes = route_sheet_wires(sheet, session.catalog, project=session.project)
        for wire in sheet.wires:
            for point in routes.get(wire.id) or []:
                assert frame[0] - 0.4 <= point.x <= frame[2] + 0.4, (sheet.id, wire.id, point.x)
                assert frame[1] - 0.4 <= point.y <= frame[3] + 0.4, (sheet.id, wire.id, point.y)
                inside_title = title[0] <= point.x <= title[2] and title[1] <= point.y <= title[3]
                assert not inside_title, (sheet.id, wire.id, point, "title block")
        ladder = is_ladder_sheet(sheet) or is_iec_column_sheet(sheet)
        marks = place_wire_marks(sheet, session.catalog, routes, ladder=ladder, project=session.project)
        for wire in sheet.wires:
            placed = marks.get(wire.id)
            if not placed:
                continue
            box = mark_box(
                placed[0],
                placed[1],
                wire.drawn_mark(ladder=ladder),
                align=placed[2],
                spec=wire.spec_text(),
            )
            _in_frame(box, frame, f"{sheet.id}:{wire.id}:mark")
            assert not boxes_overlap(box, title, pad=0.15), (sheet.id, wire.id, "mark in title", box)
        for ann, x, y, align in place_xref_labels(session.project, session.catalog, sheet, ladder=ladder):
            box = annotation_box(ann, x, y, align)
            _in_frame(box, frame, f"{sheet.id}:{ann.owner}:xref")
            assert not boxes_overlap(box, title, pad=0.15), (sheet.id, ann.owner, "xref in title", box)


def _wire_row(session: Session) -> list[tuple[str, str, str, str]]:
    rows = []
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            rows.append((wire.frm, wire.to, str(wire.net or ""), str(wire.label or "")))
    return rows


def test_device_rename_leaves_terminals_nets_and_rails(tmp_path: Path):
    """A device tag that matches another part's terminal, or a net, renames only the device."""

    iec = _session("tag clash", "IEC")
    iec.place("iec.drives.vfd_1k5", ref="U1", x=40, y=30)
    iec.place(
        "iec.loads.motor_3ph",
        ref="M1",
        x=40,
        y=120,
        properties={"callout": "motor M1.U1 from drive U1"},
    )
    iec.place("iec.contactors.ac3_9a", ref="K1", x=140, y=30)
    iec.wire("U1.U", "M1.U1", net="U1", label="U1")
    iec.wire("K1.T1", "M1.V1", net="K1", label="K1")
    sheet = iec.sheet()
    sheet.rail_hot = "K1"
    sheet.rail_return = "N"
    iec.wire("K1", "M1.W1", net="K1", label="K1")

    to_nema = _convert(iec, "nema", tmp_path, "clash-nema")
    assert to_nema["tag_map"]["U1"] == "VFD1"
    assert to_nema["tag_map"]["M1"] == "MTR1"
    assert to_nema["tag_map"]["K1"] == "M1"
    page = iec.sheet()
    assert page.rail_hot == "K1"
    assert page.rail_return == "N"
    ids = {comp.id for comp in page.components}
    assert ids == {"VFD1", "MTR1", "M1"}
    by_end = {(wire.frm, wire.to) for wire in page.wires}
    assert ("VFD1.U", "MTR1.T1") in by_end or ("MTR1.T1", "VFD1.U") in by_end
    assert ("M1.T1", "MTR1.T2") in by_end or ("MTR1.T2", "M1.T1") in by_end
    assert ("K1", "MTR1.T3") in by_end or ("MTR1.T3", "K1") in by_end
    for wire in page.wires:
        assert wire.net in {"U1", "K1"}
        assert wire.label in {"U1", "K1"}
        assert "VFD1" not in {wire.net, wire.label}
    motor = page.component("MTR1")
    assert motor.properties.get("callout") == "motor MTR1.U1 from drive VFD1"
    assert "VFD1" not in motor.properties["callout"].split("MTR1.", 1)[-1].split(" ", 1)[0]

    back = _convert(iec, "iec", tmp_path, "clash-iec")
    assert back["tag_map"]["VFD1"] == "U1"
    assert back["tag_map"]["MTR1"] == "M1"
    assert back["tag_map"]["M1"] == "K1"
    page = iec.sheet()
    assert page.rail_hot == "K1"
    ends = {(wire.frm, wire.to) for wire in page.wires}
    assert ("U1.U", "M1.U1") in ends or ("M1.U1", "U1.U") in ends
    assert ("K1.T1", "M1.V1") in ends or ("M1.V1", "K1.T1") in ends
    assert ("K1", "M1.W1") in ends or ("M1.W1", "K1") in ends
    for wire in page.wires:
        assert wire.net in {"U1", "K1"}
        assert wire.label in {"U1", "K1"}
    assert page.component("M1").properties.get("callout") == "motor M1.U1 from drive U1"

    nema = _session("tag clash nema", "NEMA/ANSI")
    nema.place("nema.drives.vfd_1k5", ref="VFD1", x=40, y=30)
    nema.place(
        "nema.loads.motor_3ph",
        ref="MTR1",
        x=40,
        y=120,
        properties={"callout": "contactor M1 to motor MTR1 lead T1"},
    )
    nema.place("nema.contactors.ac3_9a", ref="M1", x=140, y=30)
    nema.wire("VFD1.U", "MTR1.T1", net="VFD1", label="VFD1")
    nema.wire("M1.T1", "MTR1.T2", net="M1", label="M1")
    nema.sheet().rail_hot = "VFD1"
    nema.sheet().rail_return = "0V"
    nema.wire("VFD1", "MTR1.T3", net="VFD1", label="VFD1")
    to_iec = _convert(nema, "iec", tmp_path, "clash-from-nema")
    assert to_iec["tag_map"]["VFD1"] == "U1"
    assert to_iec["tag_map"]["M1"] == "K1"
    assert to_iec["tag_map"]["MTR1"] == "M1"
    page = nema.sheet()
    assert page.rail_hot == "VFD1"
    ends = {(wire.frm, wire.to) for wire in page.wires}
    assert ("U1.U", "M1.U1") in ends or ("M1.U1", "U1.U") in ends
    assert ("K1.T1", "M1.V1") in ends or ("M1.V1", "K1.T1") in ends
    assert ("VFD1", "M1.W1") in ends or ("M1.W1", "VFD1") in ends
    for wire in page.wires:
        assert wire.net in {"VFD1", "M1"}
        assert wire.label in {"VFD1", "M1"}
        assert wire.net != "U1"
    assert page.component("M1").properties.get("callout") == "contactor K1 to motor M1 lead T1"
    _convert(nema, "nema", tmp_path, "clash-back-nema")
    page = nema.sheet()
    assert page.rail_hot == "VFD1"
    ends = {(wire.frm, wire.to) for wire in page.wires}
    assert ("VFD1.U", "MTR1.T1") in ends or ("MTR1.T1", "VFD1.U") in ends
    assert ("M1.T1", "MTR1.T2") in ends or ("MTR1.T2", "M1.T1") in ends
    assert page.component("MTR1").properties.get("callout") == "contactor M1 to motor MTR1 lead T1"
    for wire in page.wires:
        assert wire.net in {"VFD1", "M1"}


def test_wired_overload_aux_stays_and_unwired_aux_is_flagged(tmp_path: Path):
    wired = _session("ol wired", "IEC")
    wired.place("iec.protection.overload_9a", ref="F1", x=40, y=40)
    wired.wire("F1.97", "F1.98", net="AUX2")
    kept = _convert(wired, "nema", tmp_path, "ol-wired")
    comp = wired.sheet().component("OL1")
    assert comp.library_id == "iec.protection.overload_9a"
    assert any("OL1.97" in (wire.frm, wire.to) for wire in wired.sheet().wires)
    notes = [row for row in kept["unmapped"] if row["flag"] == "unmapped_pin"]
    assert notes
    assert any("97" in str(row.get("detail") or "") and "same terminals" in str(row.get("detail") or "") for row in notes)

    open_ol = _session("ol open", "IEC")
    open_ol.place("iec.protection.overload_9a", ref="F1", x=40, y=40)
    flagged = _convert(open_ol, "nema", tmp_path, "ol-open")
    swapped = open_ol.sheet().component("OL1")
    assert swapped.library_id == "nema.protection.overload"
    aux = [row for row in flagged["unmapped"] if "97" in str(row.get("detail") or "")]
    assert aux
    assert all("not wired" in str(row.get("detail") or "") for row in aux)
    assert all("same terminals" in str(row.get("detail") or "") for row in aux)


def test_converted_plc_io_keeps_pins_on_the_card(tmp_path: Path):
    """NEMA ladder I/O stays a horizontal ladder after the standard swap. Pins stay on the card."""

    from panelcad.render.sheet import pin_world
    from panelcad.render.wiring import route_sheet_wires

    session = _session("plc pins", "NEMA/ANSI")
    built = _insert(session, "plc_motor", standard="NEMA/ANSI")
    _convert(session, "iec", tmp_path, "plc-pins")
    io = session.sheet(built["io_sheet"])
    control = session.sheet(built["control_sheet"])
    assert io.drawing_style == "ladder"
    assert (io.rail_hot, io.rail_return) == (control.rail_hot, control.rail_return) == ("L+", "0V")
    plc = next(comp for comp in io.components if comp.properties.get("role") == "plc")
    common = next(wire for wire in io.wires if f"{plc.id}.M" in (wire.frm, wire.to))
    assert common.number == "0V"
    assert session.catalog.get(plc.library_id).pin("M")
    relay = next(comp for comp in io.components if comp.properties.get("role") == "relay")
    run = next(comp for comp in io.components if comp.properties.get("role") == "run_fb")
    assert plc.properties.get("ladder_card")
    assert run.x < plc.x
    assert relay.x > plc.x + 20.0
    routes = route_sheet_wires(io, session.catalog, project=session.project)
    device = session.catalog.get(plc.library_id)
    q00 = pin_world(plc, device.pin("Q0_0"))
    # The coil rung leaves Q0.0 and runs across to the relay, level with that pin.
    coil_wires = [wire for wire in io.wires if f"{plc.id}.Q0_0" in (wire.frm, wire.to)]
    assert coil_wires
    for wire in coil_wires:
        pts = routes[wire.id]
        ys = [point.y for point in pts]
        assert max(ys) - min(ys) <= 8.0, (wire.frm, wire.to, ys)
        assert abs(ys[0] - q00[1]) <= 1.0 or abs(ys[-1] - q00[1]) <= 1.0
    _assert_pins_on_body_and_in_frame(session)


def test_converted_plc_io_follows_the_control_rail_pair(tmp_path: Path):
    """I/O takes the control pair both ways. The card pin stays M."""

    dc = _session("plc dc iec", "IEC")
    dc_built = dc.insert_circuit(kind="plc_motor", control="local", standard="IEC", hp=5, voltage=480)
    _convert(dc, "nema", tmp_path, "plc-dc-nema")
    dc_control = dc.sheet(dc_built["control_sheet"])
    dc_io = dc.sheet(dc_built["io_sheet"])
    assert (dc_io.rail_hot, dc_io.rail_return) == (dc_control.rail_hot, dc_control.rail_return) == ("24V", "0V")
    dc_plc = next(comp for comp in dc_io.components if comp.properties.get("role") == "plc")
    dc_common = next(wire for wire in dc_io.wires if f"{dc_plc.id}.M" in (wire.frm, wire.to))
    assert dc_common.number == "0V"

    ac = _session("plc ac nema", "NEMA/ANSI")
    ac_built = ac.insert_circuit(
        kind="plc_motor", control="local", standard="NEMA/ANSI", hp=5, voltage=480, control_voltage=120
    )
    _convert(ac, "iec", tmp_path, "plc-ac-iec")
    ac_control = ac.sheet(ac_built["control_sheet"])
    ac_io = ac.sheet(ac_built["io_sheet"])
    assert (ac_io.rail_hot, ac_io.rail_return) == (ac_control.rail_hot, ac_control.rail_return) == ("X1", "X2")
    ac_plc = next(comp for comp in ac_io.components if comp.properties.get("role") == "plc")
    ac_common = next(wire for wire in ac_io.wires if f"{ac_plc.id}.M" in (wire.frm, wire.to))
    assert f"{ac_plc.id}.M" in (ac_common.frm, ac_common.to)
    assert "X2" in (ac_common.frm, ac_common.to)
    assert ac_common.net == "X2"
    assert ac_common.number == "X2"
    assert "L1" not in (ac_common.frm, ac_common.to, ac_common.net)
    assert ac_io.drawing_style == "ladder"

    back = _session("plc ac iec", "IEC")
    back_built = back.insert_circuit(
        kind="plc_motor", control="local", standard="IEC", hp=5, voltage=400, control_voltage=120
    )
    _convert(back, "nema", tmp_path, "plc-ac-nema")
    back_control = back.sheet(back_built["control_sheet"])
    back_io = back.sheet(back_built["io_sheet"])
    assert (back_io.rail_hot, back_io.rail_return) == (back_control.rail_hot, back_control.rail_return) == ("X1", "X2")
    back_plc = next(comp for comp in back_io.components if comp.properties.get("role") == "plc")
    back_common = next(wire for wire in back_io.wires if f"{back_plc.id}.M" in (wire.frm, wire.to))
    assert back_common.number == "X2"


def test_converted_ac_control_stays_off_the_supply_phase(tmp_path: Path):
    """A 230 V control rail stays X1/X2. It does not join supply phase L1."""

    from panelcad.core.wire_numbering import wire_networks

    def _ends(session: Session, target: str) -> set[str]:
        for members in wire_networks(session.project, session.catalog):
            ends = {wire.frm for _sheet, wire in members} | {wire.to for _sheet, wire in members}
            if target in ends:
                return ends
        return set()

    def _check(session: Session, built: dict) -> None:
        control = session.sheet(built["control_sheet"])
        io = session.sheet(built["io_sheet"])
        assert (control.rail_hot, control.rail_return) == ("X1", "X2")
        assert (io.rail_hot, io.rail_return) == ("X1", "X2")
        power = session.sheet(built["sheet"])
        breaker = next(comp for comp in power.components if comp.properties.get("role") == "breaker")
        transformer = next(
            comp
            for sheet in session.project.sheets
            for comp in sheet.components
            if comp.properties.get("role") == "transformer"
        )
        supply = f"{breaker.id}.2"
        secondary = f"{transformer.id}.X1"
        supply_ends = _ends(session, supply)
        assert secondary not in supply_ends
        assert "X1" not in supply_ends
        assert "X2" not in supply_ends
        for sheet in (control, io):
            for wire in sheet.wires:
                for end in (wire.frm, wire.to, wire.net, wire.number):
                    assert str(end or "") not in {"L1", "L2", "L3", "N"}

    iec = _session("ac iec", "IEC")
    iec_built = iec.insert_circuit(
        kind="plc_motor", control="local", standard="IEC", hp=5, voltage=400, control_voltage=230
    )
    _check(iec, iec_built)
    _convert(iec, "nema", tmp_path, "ac-nema")
    _check(iec, iec_built)
    _convert(iec, "iec", tmp_path, "ac-iec-back")
    _check(iec, iec_built)

    nema = _session("ac nema", "NEMA/ANSI")
    nema_built = nema.insert_circuit(
        kind="plc_motor", control="local", standard="NEMA/ANSI", hp=5, voltage=480, control_voltage=230
    )
    _check(nema, nema_built)
    _convert(nema, "iec", tmp_path, "ac-from-nema")
    _check(nema, nema_built)


def test_converted_star_bridge_is_not_neutral_ink(tmp_path: Path):
    """K3.T1–T2 and K3.T2–T3 stay ordinary power ink after either conversion.

    The star tag is K3 on an IEC sheet and the renamed contactor after a NEMA
    conversion. Both wires are the load-side short on that contactor.
    """

    from panelcad.core.phase_color import wire_phase_pens

    def _check(session: Session) -> None:
        found: dict[frozenset[str], object] = {}
        pens_for: dict[frozenset[str], dict] = {}
        for sheet in session.project.sheets:
            stars = [comp for comp in sheet.components if comp.properties.get("role") == "star"]
            if not stars:
                continue
            star = stars[0]
            pens = wire_phase_pens(session.project, sheet, session.catalog)
            for wire in sheet.wires:
                ends = (str(wire.frm), str(wire.to))
                if not all(end.startswith(f"{star.id}.") for end in ends):
                    continue
                pins = frozenset(end.split(".", 1)[1] for end in ends)
                if pins in {frozenset({"T1", "T2"}), frozenset({"T2", "T3"})}:
                    found[pins] = wire
                    pens_for[pins] = pens
        assert set(found) == {frozenset({"T1", "T2"}), frozenset({"T2", "T3"})}
        for pins, wire in found.items():
            pens = pens_for[pins]
            assert wire.id not in pens
            assert str(wire.net or "").strip().upper() not in {"N", "NEUTRAL"}
            pen = pens.get(wire.id)
            assert pen is None or pen.phase != "N"
            assert pen is None or pen.stroke != "#5eb0e8"

    iec = _session("star ink", "IEC")
    _insert(iec, "star_delta", standard="IEC")
    iec.set_phase_colors(iec="blue,black,grey")
    _check(iec)
    _convert(iec, "nema", tmp_path, "star-ink-nema")
    _check(iec)
    _convert(iec, "iec", tmp_path, "star-ink-iec")
    _check(iec)

    nema = _session("star ink nema", "NEMA/ANSI")
    _insert(nema, "star_delta", standard="NEMA/ANSI")
    _convert(nema, "iec", tmp_path, "star-ink-from-nema")
    nema.set_phase_colors(iec="blue,black,grey")
    _check(nema)
    star = next(
        comp
        for sheet in nema.project.sheets
        for comp in sheet.components
        if comp.properties.get("role") == "star"
    )
    power = next(sheet for sheet in nema.project.sheets if any(comp.id == star.id for comp in sheet.components))
    pairs = {
        frozenset(end.split(".", 1)[1] for end in (wire.frm, wire.to))
        for wire in power.wires
        if {wire.frm, wire.to} <= {f"{star.id}.T1", f"{star.id}.T2", f"{star.id}.T3"}
        and wire.frm.startswith(f"{star.id}.")
        and wire.to.startswith(f"{star.id}.")
    }
    assert frozenset({"T1", "T2"}) in pairs
    assert frozenset({"T2", "T3"}) in pairs


def test_round_trip_nets_match(tmp_path: Path):
    """IEC→NEMA→IEC and NEMA→IEC→NEMA keep the same pin-to-pin netlist."""

    jobs = (
        ("star_delta", {"timer_seconds": 8}),
        ("plc_motor", {"hp": 5, "voltage": 480, "control_voltage": 230}),
        ("reversing", {"pilot_lights": True}),
    )
    for kind, extra in jobs:
        for start, target, back in (
            ("IEC", "nema", "iec"),
            ("NEMA/ANSI", "iec", "nema"),
        ):
            session = _session(f"nets {kind} {start}", start)
            built = session.insert_circuit(kind=kind, control="local", standard=start, **extra)
            before = sheet_edges(session.project)
            before_rails = {
                sheet.id: (sheet.rail_hot, sheet.rail_return)
                for sheet in session.project.sheets
                if sheet.rail_hot or sheet.rail_return
            }
            mid = _convert(session, target, tmp_path, f"nets-{kind}-{target}")
            assert same_netlist(
                before,
                sheet_edges(session.project),
                tag_map=mid["tag_map"],
                pin_rewrites=mid["pin_rewrites"],
                rail_map=mid["rail_map"],
            )
            _convert(session, back, tmp_path, f"nets-{kind}-{back}")
            assert sheet_edges(session.project) == before
            after_rails = {
                sheet.id: (sheet.rail_hot, sheet.rail_return)
                for sheet in session.project.sheets
                if sheet.rail_hot or sheet.rail_return
            }
            assert after_rails == before_rails
            if kind == "plc_motor":
                control = session.sheet(built["control_sheet"])
                io = session.sheet(built["io_sheet"])
                assert (control.rail_hot, control.rail_return) == ("X1", "X2")
                assert (io.rail_hot, io.rail_return) == ("X1", "X2")
            if kind == "star_delta":
                star = next(
                    comp
                    for sheet in session.project.sheets
                    for comp in sheet.components
                    if comp.properties.get("role") == "star"
                )
                ends = {
                    frozenset((wire.frm, wire.to))
                    for sheet in session.project.sheets
                    for wire in sheet.wires
                }
                assert frozenset((f"{star.id}.T1", f"{star.id}.T2")) in ends
                assert frozenset((f"{star.id}.T2", f"{star.id}.T3")) in ends


def test_round_trip_contact_tables_stay_in_coil_columns(tmp_path: Path):
    """K3/K4 contact tables keep a clear x gap after the sheet returns to IEC columns.

    A bay is the vertical channel under a coil. Tables that share an x range
    are the same bay, including a gap of several millimetres. The check is the
    one a fresh IEC insert uses, run after IEC→NEMA→IEC and after NEMA→IEC.
    """

    from panelcad.core.zones import column_from_header
    from panelcad.render.xref import annotation_box, place_xref_labels

    def _check(session: Session, sheet_id: str) -> None:
        sheet = session.sheet(sheet_id)
        assert sheet.drawing_style == "iec"
        placed = place_xref_labels(session.project, session.catalog, sheet, ladder=True)
        mirrors = [item for item in placed if item[0].kind == "mirror"]
        assert mirrors, sheet_id
        for ann, x, y, align in mirrors:
            box = annotation_box(ann, x, y, align)
            center = (box[0] + box[2]) * 0.5
            assert column_from_header(sheet, center) == column_from_header(sheet, ann.anchor_x), (
                sheet_id,
                ann.owner,
                ann.lines,
                center,
                ann.anchor_x,
            )
        boxes = [(ann.owner, annotation_box(ann, x, y, align)) for ann, x, y, align in mirrors]
        for index, (name, box) in enumerate(boxes):
            for other, obox in boxes[index + 1 :]:
                overlap = min(box[2], obox[2]) - max(box[0], obox[0])
                assert overlap <= 0.05, (sheet_id, name, other, round(overlap, 2), box, obox)

    jobs = (
        {"kind": "star_delta", "control": "local", "timer_seconds": 8},
        {"kind": "reversing", "control": "local", "pilot_lights": True},
        {"kind": "soft_starter", "control": "local", "bypass": True},
        {"kind": "plc_motor", "control": "local", "hp": 5, "voltage": 480},
    )
    for kwargs in jobs:
        kind = kwargs["kind"]
        iec = _session(f"seats {kind}", "IEC")
        built = iec.insert_circuit(standard="IEC", **kwargs)
        _check(iec, built["control_sheet"])
        _convert(iec, "nema", tmp_path, f"seats-{kind}-nema")
        _convert(iec, "iec", tmp_path, f"seats-{kind}-iec")
        _check(iec, built["control_sheet"])

        nema = _session(f"seats nema {kind}", "NEMA/ANSI")
        built = nema.insert_circuit(standard="NEMA/ANSI", **kwargs)
        _convert(nema, "iec", tmp_path, f"seats-{kind}-from-nema")
        _check(nema, built["control_sheet"])


def _assert_no_joined_wire_numbers(session: Session) -> None:
    """No drawn segment end sits on a wire with a different number, except at a pin."""

    for sheet in session.project.sheets:
        if sheet.kind in {"panel_layout", "door_layout"}:
            continue
        if not sheet.wires:
            continue
        _assert_drawn_segments_match_pins(session, sheet.id, mix_numbers=True)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("start", ["NEMA/ANSI", "IEC"])
def test_converted_sheets_do_not_join_different_wire_numbers(
    kind: str, start: str, tmp_path: Path
):
    """Power shelves are rebuilt for the target standard, then again on the way back.

    A NEMA soft starter with bypass leaves the T2 lead unstored. After the
    symbol swap that lead autoroutes into the other phase. A fresh insert of
    the target standard stores the shelf clear of that wire. Every Circuit
    Builder sheet, both directions, has to pass the same check.
    """

    kwargs: dict = {"kind": kind, "control": "local"}
    if kind == "vfd":
        kwargs["drive"] = "generic_1k5"
    if kind == "star_delta":
        kwargs["timer_seconds"] = 8
    if kind == "plc_motor":
        kwargs["hp"] = 5
        kwargs["voltage"] = 480
    if kind == "soft_starter":
        kwargs["bypass"] = True
    session = _session(f"join {start} {kind}", start)
    session.insert_circuit(standard=start, **kwargs)
    other = "iec" if start == "NEMA/ANSI" else "nema"
    back = "nema" if other == "iec" else "iec"
    _convert(session, other, tmp_path, f"{kind}-{start}-out")
    _assert_no_joined_wire_numbers(session)
    _convert(session, back, tmp_path, f"{kind}-{start}-back")
    _assert_no_joined_wire_numbers(session)


def _assert_pe_absent_from_sccr(session: Session) -> dict:
    rated = session.compute_sccr()
    assert "PE" not in rated["limiting_refs"]
    for part in rated["components"]:
        assert part["role"] != "pe_bar", part
        assert not str(part["library_id"]).endswith("connection.ground"), part
        assert part["ref"] != "PE", part
    return rated


def test_conversion_keeps_pe_out_of_the_sccr(tmp_path: Path):
    """The earth symbol stays a pe_bar and is not a limiting device either way."""

    session = _session("sccr iec", "IEC")
    _insert(session, "star_delta", standard="IEC")
    earth = [
        comp
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.properties.get("role") == "pe_bar"
    ]
    assert earth and earth[0].library_id == "iec.connection.ground"
    before = _assert_pe_absent_from_sccr(session)
    assert before["panel_sccr_ka"] is not None

    _convert(session, "nema", tmp_path, "sccr-nema")
    earth = [
        comp
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.properties.get("role") == "pe_bar" or str(comp.library_id).endswith(".connection.ground")
    ]
    assert earth
    assert all(comp.library_id == "iec.connection.ground" for comp in earth)
    assert all(comp.properties.get("role") == "pe_bar" for comp in earth)
    nema = _assert_pe_absent_from_sccr(session)
    assert nema["panel_sccr_ka"] is not None

    _convert(session, "iec", tmp_path, "sccr-iec")
    earth = [
        comp
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.properties.get("role") == "pe_bar"
    ]
    assert earth and all(comp.library_id == "iec.connection.ground" for comp in earth)
    back = _assert_pe_absent_from_sccr(session)
    assert back["panel_sccr_ka"] is not None

    other = _session("sccr nema", "NEMA/ANSI")
    _insert(other, "star_delta", standard="NEMA/ANSI")
    _convert(other, "iec", tmp_path, "sccr-from-nema")
    _assert_pe_absent_from_sccr(other)


def test_pilot_tags_clear_the_coil_list_and_supply_pins_stay(tmp_path: Path):
    """One text height between a coil list and the next pilot tag, and between LT and the next tag."""

    from panelcad.core.ink import device_ref_layout, text_box
    from panelcad.core.ladder import is_ladder_sheet
    from panelcad.render.xref import annotation_box, place_xref_labels

    session = _session("pilots", "IEC")
    built = session.insert_circuit(kind="reversing", control="local", pilot_lights=True, standard="IEC")
    _convert(session, "nema", tmp_path, "pilots")
    control = session.sheet(built["control_sheet"])
    assert control.drawing_style == "ladder"
    psu = next(comp for comp in control.components if comp.properties.get("role") == "psu")
    labels = psu.properties.get("pin_labels") or {}
    assert labels.get("L") == "L"
    assert labels.get("N") == "N"
    assert labels.get("PE") == "PE"
    # The rating printed in the symbol is not a rail name.
    texts = {str(prim.text).strip() for prim in session.catalog.get(psu.library_id).primitives if prim.type == "text"}
    assert "24V" in texts

    def _tag(comp):
        device = session.catalog.get(comp.library_id)
        layout = device_ref_layout(device, comp, ref_align="middle")
        assert layout is not None, comp.id
        x, y, align, size = layout
        box = text_box(x, y, comp.id, size, align)
        assert box is not None
        return box

    pl1 = next(comp for comp in control.components if comp.properties.get("role") == "pilot_fwd")
    pl2 = next(comp for comp in control.components if comp.properties.get("role") == "pilot_rev")
    coil = next(comp for comp in control.components if comp.properties.get("role") == "rev_coil")
    ladder = is_ladder_sheet(control)
    placed = place_xref_labels(session.project, session.catalog, control, ladder=ladder)
    lists = [
        annotation_box(ann, x, y, align)
        for ann, x, y, align in placed
        if ann.owner == coil.id and ann.kind == "coil"
    ]
    assert lists, coil.id
    tag1 = _tag(pl1)
    tag2 = _tag(pl2)

    def _shares_x(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
        return a[2] > b[0] and b[2] > a[0]

    def _gap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
        if a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]:
            dx = 0.0 if _shares_x(a, b) else min(abs(a[0] - b[2]), abs(b[0] - a[2]))
            dy = 0.0 if a[3] > b[1] and b[3] > a[1] else min(abs(a[1] - b[3]), abs(b[1] - a[3]))
            if _shares_x(a, b):
                return dy
            if a[3] > b[1] and b[3] > a[1]:
                return dx
            return max(dx, dy)
        return 0.0

    # The coil list sits just above the next pilot tag. A native NEMA insert
    # leaves about 0.4 mm; conversion has to match that, and the ink must not meet.
    for box in lists:
        clearance = _gap(box, tag1)
        assert clearance >= 0.3, (clearance, box, tag1, "PL tag meets the coil list")
    device = session.catalog.get(pl1.library_id)
    lt = next(prim for prim in device.primitives if prim.type == "text" and str(prim.text).strip() == "LT")
    lt_box = text_box(pl1.x + float(lt.x), pl1.y + float(lt.y), "LT", float(lt.size or 2.5), "middle")
    assert lt_box is not None
    # A native NEMA reversing ladder draws LT about 0.8 mm into the next pilot tag.
    assert tag2[1] - lt_box[3] >= -1.0, (tag2, lt_box, "LT covers the next pilot tag")
    _assert_pins_on_body_and_in_frame(session)


def test_conversion_carries_project_fields_and_switches_phase_colors(tmp_path: Path):
    """Revision control, cables, catalog SKUs, and both phase palettes survive.

    The active palette follows the sheet standard. Custom colors stay on the
    unused palette. JSONL still exposes report_bug, settings, and set_phase_colors.
    """

    import inspect

    from panelcad.core.phase_color import pen_for_phase, phase_color_settings
    from panelcad.core.pins import rating_caption_positions
    from panelcad.core.revisions import RevisionCloud, RevisionControl
    from panelcad.render.sheet_labels import caption_layouts, place_device_tags
    from panelcad.render.wiring import pin_keepouts, route_sheet_wires

    assert "manufacturer" in inspect.signature(Session.insert_circuit).parameters
    assert "secondary_manufacturer" in inspect.signature(Session.insert_circuit).parameters
    assert "standard" in inspect.signature(Session.insert_circuit).parameters

    session = _session("fields", "NEMA/ANSI")
    built = session.insert_circuit(
        kind="dol",
        control="local",
        standard="NEMA/ANSI",
        manufacturer="AutomationDirect",
        secondary_manufacturer="LS Electric",
        voltage=480,
        fla=4.8,
        control_voltage=24,
    )
    power = session.project.sheet(built["sheet"])
    before = palette_for(session.project, power)
    assert before is not None and before[0] == "nema_high"
    assert before[1].l2 == "#e06a12"
    assert before[1].l3 == "#c9a000"
    colors = phase_color_settings(session.project)
    assert colors.neutral == "#f2f2f2"
    assert colors.iec_neutral == "#5eb0e8"
    nema_n = pen_for_phase(session.project, power, "N")
    assert nema_n is not None and nema_n.stroke == "#a6a6a6"
    skus = {
        (str(comp.properties.get("manufacturer") or ""), str(comp.properties.get("catalog") or ""))
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.properties.get("catalog")
    }
    assert skus
    assert any(maker == "AutomationDirect" for maker, _catalog in skus)
    session.project.revision_control = RevisionControl(
        scheme="number",
        clouds=[
            RevisionCloud(
                id="cld1",
                sheet=power.id,
                x=12,
                y=18,
                w=30,
                h=16,
                rev="1",
                note="field carry",
            )
        ],
    )
    session.add_cable(tag="CBL9", to_device="MTR1", cable_type="3C+G #14 TC-ER", conductors=4, gauge="#14")

    to_iec = _convert(session, "iec", tmp_path, "fields-iec")
    assert session.project.title_block.standard == "IEC"
    assert session.project.sheet(built["sheet"]).standard == "IEC"
    switched = palette_for(session.project, session.project.sheet(built["sheet"]))
    assert switched is not None and switched[0] == "iec"
    assert switched[1].l2 == "#111111"
    assert switched[1].l3 == "#7f7f7f"
    pe = pen_for_phase(session.project, session.project.sheet(built["sheet"]), "PE")
    assert pe is not None and pe.outline == "#a68400"
    iec_n = pen_for_phase(session.project, session.project.sheet(built["sheet"]), "N")
    assert iec_n is not None and iec_n.stroke == "#5eb0e8"
    carried = phase_color_settings(session.project)
    assert carried.neutral == "#f2f2f2"
    assert carried.iec_neutral == "#5eb0e8"
    assert session.project.revision_control is not None
    assert session.project.revision_control.scheme == "number"
    assert session.project.revision_control.clouds[0].note == "field carry"
    assert session.project.cables[0].tag == "CBL9"
    assert session.project.cables[0].cable_type == "3C+G #14 TC-ER"
    after = {
        (str(comp.properties.get("manufacturer") or ""), str(comp.properties.get("catalog") or ""))
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.properties.get("catalog")
    }
    assert after == skus
    page = session.project.sheet(built["sheet"])
    assert pin_keepouts(page, session.catalog)
    assert route_sheet_wires(page, session.catalog, project=session.project)
    tags = place_device_tags(page, session.catalog, project=session.project)
    assert tags
    sample = next(iter(page.components))
    device = session.catalog.get(sample.library_id)
    caption_layouts(sample, tags.get(sample.id))
    rating_caption_positions(sample, float(device.schematic_height), sample.visual_scale(), float(device.schematic_width))

    session.set_phase_colors(
        iec="brown,#112233,grey",
        nema_high="brown,#445566,yellow",
        neutral="#d0d0d0",
        iec_neutral="#2244aa",
    )
    nema_file = _convert(session, "nema", tmp_path, "fields-nema")
    back = palette_for(session.project, session.project.sheet(built["sheet"]))
    assert back is not None and back[0] == "nema_high"
    assert back[1].l2 == "#445566"
    assert session.project.phase_colors is not None
    assert session.project.phase_colors.iec.l2 == "#112233"
    assert session.project.phase_colors.nema_high.l2 == "#445566"
    assert session.project.phase_colors.neutral == "#d0d0d0"
    assert session.project.phase_colors.iec_neutral == "#2244aa"
    custom_n = pen_for_phase(session.project, session.project.sheet(built["sheet"]), "N")
    assert custom_n is not None and custom_n.stroke == "#d0d0d0"
    reloaded_nema = Session.open(nema_file["out_path"])
    assert reloaded_nema.project.phase_colors is not None
    assert reloaded_nema.project.phase_colors.neutral == "#d0d0d0"
    assert reloaded_nema.project.phase_colors.iec_neutral == "#2244aa"
    assert session.project.title_block.standard == "NEMA/ANSI"
    _convert(session, "iec", tmp_path, "fields-iec-custom")
    again = palette_for(session.project, session.project.sheet(built["sheet"]))
    assert again is not None and again[0] == "iec"
    assert again[1].l2 == "#112233"
    assert session.project.phase_colors.nema_high.l2 == "#445566"
    assert session.project.phase_colors.neutral == "#d0d0d0"
    assert session.project.phase_colors.iec_neutral == "#2244aa"
    iec_custom = pen_for_phase(session.project, session.project.sheet(built["sheet"]), "N")
    assert iec_custom is not None and iec_custom.stroke == "#2244aa"
    assert session.project.revision_control.clouds[0].id == "cld1"
    assert [cable.tag for cable in session.project.cables] == ["CBL9"]

    saved = Session.open(to_iec["out_path"])
    assert saved.project.revision_control is not None
    assert saved.project.revision_control.scheme == "number"
    assert saved.project.revision_control.clouds[0].note == "field carry"
    assert saved.project.cables[0].tag == "CBL9"
    # The first save-as is the default palettes. The later custom colors live on the session.
    reloaded = palette_for(saved.project, saved.project.sheet(built["sheet"]))
    assert reloaded is not None and reloaded[0] == "iec"
    assert reloaded[1].l2 == "#111111"

    agent = Agent()
    listed = agent.handle({"id": "help-id", "op": "help"})
    assert listed["ok"], listed
    ops = set(listed["result"]["ops"])
    assert {
        "report_bug",
        "settings",
        "set_phase_colors",
        "convert_standard",
        "convert_standard_plan",
        "convert_standard_next",
        "convert_standard_apply",
    } <= ops
    schemas = listed["result"]["schemas"]
    assert "report_bug" in schemas and "settings" in schemas and "set_phase_colors" in schemas
    assert "manufacturer" in schemas["insert_circuit"]["args"]
    assert "secondary_manufacturer" in schemas["insert_circuit"]["args"]
    assert "standard" in schemas["insert_circuit"]["args"]
    assert "iec_neutral" in schemas["set_phase_colors"]["args"]
    assert "iec_neutral" in listed["result"]["help"]["set_phase_colors"]


def test_expanded_symbol_table_covers_timers_operators_fuses_terminals_and_loads(tmp_path: Path):
    """Patch 2 expands ENTRIES to 62 symbol pairs and converts them cleanly both ways."""

    from panelcad.convert.standard_map import ENTRIES
    from panelcad.convert.table import counterpart

    assert len(ENTRIES) >= 62
    catalog = Session.create("map-expanded").catalog
    new_pairs = [
        ("nema.protection.fuse", "iec.protection.fuse"),
        ("nema.terminals.x_2p5", "iec.terminals.x_2p5"),
        ("nema.terminals.feed_through", "iec.terminals.feed_through"),
        ("nema.loads.fan_24v", "iec.loads.fan_24v"),
        ("nema.sensors.prox_24v", "iec.sensors.prox_24v"),
        ("nema.power.receptacle", "iec.power.receptacle"),
        ("nema.relays.safety_24vdc", "iec.relays.safety_24vdc"),
        ("nema.timers.off_delay", "iec.timers.off_delay"),
        ("nema.timers.contact_notc", "iec.timers.contact_notc"),
        ("nema.timers.contact_ncto", "iec.timers.contact_ncto"),
        ("nema.timers.contact_noto", "iec.timers.contact_noto"),
        ("nema.timers.contact_nctc", "iec.timers.contact_nctc"),
        ("nema.relays.alternating", "iec.relays.alternating"),
        ("nema.relays.cr", "iec.relays.cr"),
        ("nema.operators.float_no", "iec.operators.float_no"),
        ("nema.operators.float_nc", "iec.operators.float_nc"),
        ("nema.operators.level_no", "iec.operators.level_no"),
        ("nema.operators.level_nc", "iec.operators.level_nc"),
        ("nema.operators.pressure_no", "iec.operators.pressure_no"),
        ("nema.operators.pressure_nc", "iec.operators.pressure_nc"),
        ("nema.operators.temperature_no", "iec.operators.temperature_no"),
        ("nema.operators.temperature_nc", "iec.operators.temperature_nc"),
        ("nema.operators.flow_no", "iec.operators.flow_no"),
        ("nema.operators.flow_nc", "iec.operators.flow_nc"),
        ("nema.operators.foot_no", "iec.operators.foot_no"),
        ("nema.operators.foot_nc", "iec.operators.foot_nc"),
        ("nema.operators.horn", "iec.operators.horn"),
        ("nema.operators.estop_2nc", "iec.operators.estop_2nc"),
        ("nema.plc.analog_1ai_1ao", "iec.plc.analog_1ai_1ao"),
    ]
    for nema_id, iec_id in new_pairs:
        assert counterpart(iec_id, "nema") == nema_id
        assert counterpart(nema_id, "iec") == iec_id
        nema_dev = catalog.get(nema_id)
        iec_dev = catalog.get(iec_id)
        assert any(
            dev.panel.in_panel is False
            and dev.panel.din_rail is False
            and dev.panel.mount == "none"
            and dev.panel.width_mm == 0.0
            and dev.panel.height_mm == 0.0
            and dev.panel.depth_mm == 0.0
            for dev in (nema_dev, iec_dev)
        )

    # Round-trip a custom sheet containing expanded symbols and wired pins.
    session = _session("expanded-symbols", "IEC")
    session.place("iec.protection.fuse", ref="F1", x=30, y=30)
    session.place("iec.timers.off_delay", ref="KT1", x=60, y=30)
    session.place("iec.timers.contact_notc", ref="KT1_NO", x=90, y=30)
    session.place("iec.operators.pressure_no", ref="SP1", x=120, y=30)
    session.place("iec.operators.horn", ref="HA1", x=150, y=30)
    session.place("iec.sensors.prox_24v", ref="PX1", x=180, y=30)
    session.place("iec.loads.fan_24v", ref="FAN1", x=210, y=30)
    session.place("iec.operators.estop_2nc", ref="SF1", x=240, y=30)
    session.wire("F1.1", "F1.2", net="L1")
    session.wire("KT1.A1", "KT1.A2", net="24V")
    session.wire("KT1_NO.15", "KT1_NO.18", net="T_OUT")
    session.wire("SP1.13", "SP1.14", net="P_SIG")
    session.wire("HA1.X1", "HA1.X2", net="H_SIG")
    session.wire("PX1.Lplus", "PX1.M", net="PX_SIG")
    session.wire("FAN1.Lplus", "FAN1.M", net="FAN_PWR")
    session.wire("SF1.11", "SF1.12", net="ES_CH1")
    session.wire("SF1.21", "SF1.22", net="ES_CH2")

    to_nema = _convert(session, "nema", tmp_path, "expanded-nema")
    assert to_nema["unmapped"] == []
    assert to_nema["connection_check"]["ok"] is True
    assert to_nema["connection_check"]["scorer"] == "from-to-check/1"
    libs_nema = {comp.id: comp.library_id for comp in session.project.sheets[0].components}
    assert "nema.protection.fuse" in libs_nema.values()
    assert "nema.timers.off_delay" in libs_nema.values()
    assert "nema.timers.contact_notc" in libs_nema.values()
    assert "nema.operators.pressure_no" in libs_nema.values()
    assert "nema.operators.horn" in libs_nema.values()
    assert "nema.sensors.prox_24v" in libs_nema.values()
    assert "nema.loads.fan_24v" in libs_nema.values()
    assert "nema.operators.estop_2nc" in libs_nema.values()

    to_iec = _convert(session, "iec", tmp_path, "expanded-iec")
    assert to_iec["unmapped"] == []
    assert to_iec["connection_check"]["ok"] is True
    assert to_iec["connection_check"]["scorer"] == "from-to-check/1"


def test_role_tag_table_disambiguates_shared_iec_prefixes(tmp_path: Path):
    """Patch 2 role table maps -K/-KT/-Q/-F/-S/-P/-H/-T/-M/-X to their NEMA prefixes."""

    from panelcad.convert.standard_map import ROLE_TAG_TABLE
    from panelcad.convert.table import rename_tag_by_role, role_prefix_for

    assert len(ROLE_TAG_TABLE) >= 18
    # Direct role table lookups
    assert role_prefix_for("nema", role="contactor", source_prefix="-K") == "M"
    assert role_prefix_for("nema", role="relay", source_prefix="-K") == "CR"
    assert role_prefix_for("nema", role="timer", source_prefix="-KT") == "TR"
    assert role_prefix_for("nema", role="timer", source_prefix="-K") == "TR"
    assert role_prefix_for("nema", role="breaker", source_prefix="-Q") == "CB"
    assert role_prefix_for("nema", role="disconnect", source_prefix="-Q") == "DISC"
    assert role_prefix_for("nema", role="contactor", source_prefix="-Q") == "M"
    assert role_prefix_for("nema", role="fuse", source_prefix="-F") == "FU"
    assert role_prefix_for("nema", role="overload", source_prefix="-F") == "OL"
    assert role_prefix_for("nema", role="pushbutton", source_prefix="-S") == "PB"
    assert role_prefix_for("nema", role="selector", source_prefix="-S") == "SEL"
    assert role_prefix_for("nema", role="limit_switch", source_prefix="-S") == "LS"
    assert role_prefix_for("nema", role="estop", source_prefix="-S") == "ES"
    assert role_prefix_for("nema", role="pilot_light", source_prefix="-P") == "PL"
    assert role_prefix_for("nema", role="pilot_light", source_prefix="-H") == "PL"
    assert role_prefix_for("nema", role="transformer", source_prefix="-T") == "T"
    assert role_prefix_for("nema", role="motor", source_prefix="-M") == "MTR"
    assert role_prefix_for("nema", role="terminal", source_prefix="-X") == "TB"

    # Full tag renames with leading '-' or aspect/location prefixes
    assert rename_tag_by_role("-K1", "nema", role="contactor") == "M1"
    assert rename_tag_by_role("-K2", "nema", role="relay") == "CR2"
    assert rename_tag_by_role("-KT3", "nema", role="timer") == "TR3"
    assert rename_tag_by_role("-Q1", "nema", role="breaker") == "CB1"
    assert rename_tag_by_role("-Q2", "nema", role="disconnect") == "DISC2"
    assert rename_tag_by_role("-Q3", "nema", role="contactor") == "M3"
    assert rename_tag_by_role("-F1", "nema", role="fuse") == "FU1"
    assert rename_tag_by_role("-F2", "nema", role="overload") == "OL2"
    assert rename_tag_by_role("-S1", "nema", role="pushbutton") == "PB1"
    assert rename_tag_by_role("-S2", "nema", role="selector") == "SEL2"
    assert rename_tag_by_role("-S3", "nema", role="limit_switch") == "LS3"
    assert rename_tag_by_role("-P1", "nema", role="pilot_light") == "PL1"
    assert rename_tag_by_role("-H2", "nema", role="pilot_light") == "PL2"
    assert rename_tag_by_role("-T1", "nema", role="transformer") == "T1"
    assert rename_tag_by_role("-M1", "nema", role="motor") == "MTR1"

    # Engine conversion with role-disambiguated -K, -Q, and -F tags
    session = _session("role-tags", "IEC")
    c_m = session.place("iec.contactors.ac3_32a", ref="-K1", x=30, y=30)
    c_m.properties["role"] = "contactor"
    c_cr = session.place("iec.relays.cr", ref="-K2", x=60, y=30)
    c_cr.properties["role"] = "relay"
    c_tr = session.place("iec.timers.on_delay", ref="-K3", x=90, y=30)
    c_tr.properties["role"] = "timer"
    c_cb = session.place("iec.breakers.mccb_3p_100a", ref="-Q1", x=120, y=30)
    c_cb.properties["role"] = "breaker"
    c_ds = session.place("iec.protection.disconnect_3p", ref="-Q2", x=150, y=30)
    c_ds.properties["role"] = "disconnect"
    c_fu = session.place("iec.protection.fuse", ref="-F1", x=180, y=30)
    c_fu.properties["role"] = "fuse"
    c_ol = session.place("iec.protection.overload_9a", ref="-F2", x=210, y=30)
    c_ol.properties["role"] = "overload"

    res = _convert(session, "nema", tmp_path, "role-tags-nema", keep_numbers=True)
    assert res["tag_map"] == {
        "-K1": "M1",
        "-K2": "CR2",
        "-K3": "TR3",
        "-Q1": "CB1",
        "-Q2": "DISC2",
        "-F1": "FU1",
        "-F2": "OL2",
    }
    back = _convert(session, "iec", tmp_path, "role-tags-iec", keep_numbers=True)
    assert back["connection_check"]["ok"] is True
    assert {comp.id for comp in session.project.sheets[0].components} == {
        "-K1",
        "-K2",
        "-K3",
        "-Q1",
        "-Q2",
        "-F1",
        "-F2",
    }


def test_terminal_and_wire_number_format_tables(tmp_path: Path):
    """Patch 2 terminal table (-X1:3 <-> TB1-3) and wire-number format table."""

    from panelcad.convert.table import convert_terminal_tag, format_wire_number

    assert convert_terminal_tag("-X1:3", "nema") == "TB1-3"
    assert convert_terminal_tag("X1:3", "nema") == "TB1-3"
    assert convert_terminal_tag("=A1+P1-X2:14", "nema") == "TB2-14"
    assert convert_terminal_tag("TB1-3", "iec") == "X1:3"
    assert convert_terminal_tag("TB2-14", "iec") == "X2:14"
    assert convert_terminal_tag("M1", "nema") is None

    # Wire-number table: NEMA ladder rung line-ref vs IEC potential/address vs keep=True
    assert format_wire_number("1.1", "nema") == "101"
    assert format_wire_number("1.1.2", "nema") == "101A"
    assert format_wire_number("", "nema", rung=103) == "103"
    assert format_wire_number("", "nema", rung=103, branch=1) == "103A"
    assert format_wire_number("L+", "nema") == "24V"
    assert format_wire_number("M", "nema") == "0V"
    assert format_wire_number("24V", "iec") == "L+"
    assert format_wire_number("101", "iec") == "11.1"
    assert format_wire_number("101A", "iec") == "11.2"
    assert format_wire_number("1.1.2", "nema", keep=True) == "1.1.2"

    # Full engine conversion of terminal strip pins (-X1:1, -X1:3 -> TB1-1, TB1-3)
    session = _session("terminals", "IEC")
    session.place("iec.terminals.x_2p5", ref="-X1:1", x=30, y=30)
    session.place("iec.terminals.x_2p5", ref="-X1:3", x=30, y=50)
    session.wire("-X1:1.A", "-X1:3.A", net="W101")
    to_nema = _convert(session, "nema", tmp_path, "terminals-nema")
    assert to_nema["tag_map"] == {"-X1:1": "TB1-1", "-X1:3": "TB1-3"}
    assert to_nema["connection_check"]["ok"] is True
    assert {comp.id for comp in session.project.sheets[0].components} == {"TB1-1", "TB1-3"}
    to_iec = _convert(session, "iec", tmp_path, "terminals-iec")
    assert to_iec["connection_check"]["ok"] is True
    assert {comp.id for comp in session.project.sheets[0].components} == {"-X1:1", "-X1:3"}


def test_contact_pin_map_table_and_helpers():
    """Patch 3 contact pin/terminal table covers NO, NC, timed, and overload contacts."""

    from panelcad.convert.table import (
        CONTACT_PIN_MAP_TABLE,
        contact_pin_map_for,
        convert_contact_number,
        convert_contact_pin_labels,
        convert_contact_terminals,
        counterpart,
    )

    assert len(CONTACT_PIN_MAP_TABLE) == 14
    # selector_no added to symbol counterpart table
    entry = counterpart("iec.operators.selector_no", "NEMA/ANSI")
    assert entry == "nema.operators.selector_no"
    assert contact_pin_map_for("aux_no", slot=2)["iec_terminals"] == "23-24"
    assert contact_pin_map_for("aux_no", slot=2)["nema_terminals"] == "3-4"
    assert contact_pin_map_for("timer_nc")["iec_terminals"] == "15-16"
    assert contact_pin_map_for("timer_no")["iec_terminals"] == "15-18"
    assert contact_pin_map_for("ol_nc")["nema_terminals"] == "95-96"

    for iec_terms in ("13-14", "23-24", "33-34", "43-44"):
        assert convert_contact_terminals(iec_terms, "nema", state="NO") == "3-4"
    for iec_terms in ("11-12", "21-22", "31-32", "41-42"):
        assert convert_contact_terminals(iec_terms, "nema", state="NC") == "1-2"
    assert convert_contact_terminals("15-16", "nema", state="NC", delayed="NC") == "1-2"
    assert convert_contact_terminals("15-18", "nema", state="NO", delayed="NO") == "3-4"
    assert convert_contact_terminals("95-96", "nema", state="NC") == "95-96"
    assert convert_contact_terminals("97-98", "nema", state="NO") == "97-98"

    assert convert_contact_terminals("3-4", "iec", state="NO", slot=1) == "13-14"
    assert convert_contact_terminals("3-4", "iec", state="NO", slot=2) == "23-24"
    assert convert_contact_terminals("1-2", "iec", state="NC", delayed="NC") == "15-16"
    assert convert_contact_terminals("3-4", "iec", state="NO", delayed="NO") == "15-18"

    assert convert_contact_pin_labels({"13": "23", "14": "24"}, {}, "nema", state="NO") == {
        "3": "3",
        "4": "4",
    }
    assert convert_contact_pin_labels({"21": "15", "22": "16"}, {}, "nema", state="NC", delayed="NC") == {
        "1": "1",
        "2": "2",
    }
    assert convert_contact_number("23", "nema", state="NO") == "3"
    assert convert_contact_number("24", "nema", state="NO") == "4"
    assert convert_contact_number("15", "nema", state="NC", delayed="NC") == "1"
    assert convert_contact_number("16", "nema", state="NC", delayed="NC") == "2"
    assert convert_contact_number("18", "nema", state="NO", delayed="NO") == "4"
    assert convert_contact_number("95", "nema", state="NC") == "95"


def test_star_delta_contact_numbering_and_coil_xref_tables_convert_both_ways(tmp_path: Path):
    """Patch 3: star-delta multi-slot aux and timer contacts normalize in both directions."""

    from panelcad.core.xref import xref_annotations

    # IEC -> NEMA
    s_iec = _session("sd_iec", "IEC")
    _insert(s_iec, "star_delta", standard="IEC")
    _convert(s_iec, "nema", tmp_path, "sd-iec-to-nema")
    ctrl_nema = s_iec.project.sheets[1]
    by_id = {c.id: c for c in ctrl_nema.components}

    assert by_id["M1-23"].properties.get("terminals") == "3-4"
    assert by_id["M1-23"].properties.get("pin_labels") == {"3": "3", "4": "4"}
    assert by_id["TR1-16"].properties.get("terminals") == "1-2"
    assert by_id["TR1-16"].properties.get("pin_labels") == {"1": "1", "2": "2"}
    assert by_id["TR1-18"].properties.get("terminals") == "3-4"
    assert by_id["TR1-18"].properties.get("pin_labels") == {"3": "3", "4": "4"}

    anns_nema = {
        (ann.sheet_id, ann.owner, ann.kind): ann.lines
        for ann in xref_annotations(s_iec.project, s_iec.catalog)
    }
    assert all(not ln.startswith("23-24") for ln in anns_nema.get((ctrl_nema.id, "M1", "coil"), []))
    assert [ln.split()[0] for ln in anns_nema.get((ctrl_nema.id, "M1", "coil"), [])] == ["3-4", "3-4", "poles"]
    assert [ln.split()[0] for ln in anns_nema.get((ctrl_nema.id, "TR1", "coil"), [])] == ["1-2", "3-4"]
    assert [ln.split()[0] for ln in anns_nema.get(("main", "OL1", "coil"), [])] == ["95-96"]

    # NEMA -> IEC
    s_nema = _session("sd_nema", "NEMA/ANSI")
    _insert(s_nema, "star_delta", standard="NEMA/ANSI")
    _convert(s_nema, "iec", tmp_path, "sd-nema-to-iec")
    ctrl_iec = s_nema.project.sheets[1]
    iec_mirrors = {
        ann.owner: ann.lines
        for ann in xref_annotations(s_nema.project, s_nema.catalog)
        if ann.kind == "mirror" and ann.sheet_id == ctrl_iec.id
    }
    assert sorted(ln.split()[0] for ln in iec_mirrors.get("K1", [])) == ["13-14", "23-24"]
    assert sorted(ln.split()[0] for ln in iec_mirrors.get("KT1", [])) == ["15-16", "15-18"]


def test_reversing_and_soft_starter_multi_slot_contacts_convert_both_ways(tmp_path: Path):
    """Patch 3: reversing (pilot=True) and soft-starter 2nd NO aux contacts convert cleanly."""

    from panelcad.core.xref import xref_annotations

    s_rev = _session("rev_iec", "IEC")
    s_rev.insert_circuit(kind="reversing", control="local", standard="IEC", pilot_lights=True)
    _convert(s_rev, "nema", tmp_path, "rev-iec-to-nema")
    ctrl = s_rev.project.sheets[1]
    coil_lines = {
        ann.owner: ann.lines
        for ann in xref_annotations(s_rev.project, s_rev.catalog)
        if ann.kind == "coil" and ann.sheet_id == ctrl.id
    }
    for owner in ("M1", "M2"):
        assert all(not ln.startswith("23-24") for ln in coil_lines.get(owner, []))

    s_ss = _session("ss_nema", "NEMA/ANSI")
    s_ss.insert_circuit(kind="soft_starter", control="local", standard="NEMA/ANSI", pilot_lights=True)
    _convert(s_ss, "iec", tmp_path, "ss-nema-to-iec")
    ctrl_ss = s_ss.project.sheets[1]
    mirrors_ss = {
        ann.owner: ann.lines
        for ann in xref_annotations(s_ss.project, s_ss.catalog)
        if ann.kind == "mirror" and ann.sheet_id == ctrl_ss.id
    }
    assert {"13-14", "23-24"}.issubset({ln.split()[0] for ln in mirrors_ss.get("U1", [])})


def test_iec_mirror_ladder_sheet_converts_control_rails_and_round_trips(tmp_path: Path):
    """Patch 3: build_iec_mirror() ladder sheet converts L+/0V control rails to 24V/0V and back."""

    from panelcad.examples.xref_demo import build_iec_mirror

    session = build_iec_mirror()
    to_nema = _convert(session, "nema", tmp_path, "iec-mirror-nema")
    assert to_nema["connection_check"]["ok"] is True
    sheet = session.project.sheets[0]
    assert sheet.rail_hot == "24V"
    assert sheet.rail_return == "0V"
    assert session.project.title_block.rail_hot == "24V"
    assert all(w.frm != "L+" and w.to != "L+" for w in sheet.wires)
    assert any(w.frm == "24V" for w in sheet.wires)

    to_iec = _convert(session, "iec", tmp_path, "iec-mirror-iec")
    assert to_iec["connection_check"]["ok"] is True
    sheet_back = session.project.sheets[0]
    assert sheet_back.rail_hot in {"L+", ""}
    assert session.project.title_block.rail_hot == "L+"
    assert any(w.frm == "L+" for w in sheet_back.wires)


def test_hand_drawn_iec_column_sheet_relayouts_to_horizontal_nema_ladder_rungs(tmp_path: Path):
    """Patch 3: hand-drawn IEC column sheet without roles converts to horizontal NEMA ladder rungs."""

    session = _session("hand_iec_col", "IEC")
    s1 = session.project.sheets[0]
    s1.drawing_style = "iec"
    s1.rail_hot = "L+"
    s1.rail_return = "0V"

    # Column 1: Stop S1, Start S2 + parallel seal-in K1-13, Coil K1
    session.place("iec.operators.pb_nc", ref="S1", x=60, y=50, rotation=90)
    session.place("iec.operators.pb_no", ref="S2", x=60, y=85, rotation=90)
    c_seal = session.place("iec.contactors.aux_no", ref="K1-13", x=90, y=85, rotation=90)
    c_seal.properties["parent"] = "K1"
    c_seal.properties["terminals"] = "13-14"
    c_seal.properties["pin_labels"] = {"13": "13", "14": "14"}
    session.place("iec.contactors.coil", ref="K1", x=60, y=150, rotation=90)
    session.wire("L+", "S1.11", net="L+")
    session.wire("S1.12", "S2.13", net="W1")
    session.wire("S1.12", "K1-13.13", net="W1")
    session.wire("S2.14", "K1.A1", net="W2")
    session.wire("K1-13.14", "K1.A1", net="W2")
    session.wire("K1.A2", "0V", net="0V")

    # Column 2: 2nd NO contact K1-23 (23-24) -> Timer KT1
    c_k1_23 = session.place("iec.contactors.aux_no", ref="K1-23", x=140, y=70, rotation=90)
    c_k1_23.properties["parent"] = "K1"
    c_k1_23.properties["terminals"] = "23-24"
    c_k1_23.properties["pin_labels"] = {"13": "23", "14": "24"}
    session.place("iec.timers.on_delay", ref="KT1", x=140, y=150, rotation=90)
    session.wire("L+", "K1-23.13", net="L+")
    session.wire("K1-23.14", "KT1.A1", net="W3")
    session.wire("KT1.A2", "0V", net="0V")

    # Column 3: Timed NC contact KT1-16 (15-16) -> Pilot lamp H1
    c_kt_nc = session.place("iec.contactors.aux_nc", ref="KT1-16", x=220, y=70, rotation=90)
    c_kt_nc.properties["parent"] = "KT1"
    c_kt_nc.properties["terminals"] = "15-16"
    c_kt_nc.properties["pin_labels"] = {"21": "15", "22": "16"}
    c_kt_nc.properties["delayed"] = "NC"
    session.place("iec.operators.pilot_light", ref="P1", x=220, y=150, rotation=90)
    session.wire("L+", "KT1-16.21", net="L+")
    session.wire("KT1-16.22", "P1.X1", net="W4")
    session.wire("P1.X2", "0V", net="0V")

    to_nema = _convert(session, "nema", tmp_path, "hand-iec-col-nema")
    assert to_nema["connection_check"]["ok"] is True
    sheet = session.project.sheets[0]
    assert sheet.drawing_style == "ladder"
    assert (sheet.rail_hot, sheet.rail_return) == ("24V", "0V")

    by_id = {c.id: c for c in sheet.components}
    assert all(c.rotation == 0 for c in sheet.components)
    # Series contacts left-to-right on rung 1, seal-in parallel below PB2, coil M1 at right
    assert by_id["PB1"].x < by_id["PB2"].x < by_id["M1"].x
    assert by_id["PB1"].y == by_id["PB2"].y
    assert by_id["M1-13"].x == by_id["PB2"].x
    assert by_id["M1-13"].y > by_id["PB2"].y
    assert by_id["M1"].x >= 350.0
    assert by_id["TR1"].x >= 350.0
    assert by_id["PL1"].x >= 350.0

    # Contact properties and xrefs in NEMA style
    assert by_id["M1-23"].properties.get("terminals") == "3-4"
    assert by_id["M1-23"].properties.get("pin_labels") == {"3": "3", "4": "4"}
    assert by_id["TR1-16"].properties.get("terminals") == "1-2"
    assert by_id["TR1-16"].properties.get("pin_labels") == {"1": "1", "2": "2"}
    assert "3-4" in str(by_id["M1"].properties.get("xref") or "")
    assert "23-24" not in str(by_id["M1"].properties.get("xref") or "")
    assert "1-2" in str(by_id["TR1"].properties.get("xref") or "")

    # Round-trip back to IEC restores columns and rotations
    to_iec = _convert(session, "iec", tmp_path, "hand-iec-col-back")
    assert to_iec["connection_check"]["ok"] is True
    sheet_back = session.project.sheets[0]
    assert sheet_back.drawing_style == "iec"
    assert all(c.rotation == 90 for c in sheet_back.components)


def test_dol_starter_control_sheet_clamps_above_title_block_and_tidies_waypoints(tmp_path: Path):
    """Patch 3 R2: 01_dol_starter control sheet clamps PLC1/HMI1 above title_block_rect and clears backtracking waypoints."""

    from panelcad.core.ink import schematic_ink_rect
    from panelcad.render.label_avoid import resolve_labels
    from panelcad.render.titleblock import title_block_rect, working_rect

    session = Session.open(Path("examples/dol_starter.pcad.yaml"))
    to_nema = _convert(session, "nema", tmp_path, "dol-example-nema")
    assert to_nema["connection_check"]["ok"] is True

    ctrl = session.sheet("control")
    _wx, wy, _ww, _wh = working_rect(ctrl)
    _tx, ty, _tw, _th = title_block_rect(ctrl)
    for comp in ctrl.components:
        dev = session.catalog.get(comp.library_id)
        ink = schematic_ink_rect(dev, comp.x, comp.y, comp.id, component=comp)
        assert ink[1] >= wy, (comp.id, ink[1], wy, "component encroaches on top border")
        assert ink[3] <= ty - 2.0, (comp.id, ink[3], ty, "component encroaches on title block")

    by_id = {c.id: c for c in ctrl.components}
    assert by_id["PL1"].x - (by_id["PB2"].x + 24.0) >= 14.0
    assert by_id["PL2"].x - (by_id["PL1"].x + 26.0) >= 14.0
    w13 = next(w for w in ctrl.wires if w.id == "w13")
    assert w13.points == []
    res = resolve_labels(session.project, session.catalog, ctrl)
    assert len(res.tags) == len(ctrl.components)
    assert all(pt.y <= ty - 2.0 for pts in res.routes.values() for pt in pts)
