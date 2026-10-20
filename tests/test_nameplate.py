"""Nameplate markings: full-load math, design checks, and real-size PDF/DXF."""

from __future__ import annotations

from pathlib import Path

from panelcad import Session
from panelcad.core.motor_sizing import motor_flc
from panelcad.electrical.nameplate import build_nameplate

INCH = 25.4


def _base(name: str = "Nameplate job") -> Session:
    session = Session.create(name)
    session.set_title_block(
        company="Acme Controls",
        drawing_number="E-100",
        standard="NEMA/ANSI",
        rev="A",
        date="2026-10-08",
        rev_description="Issue",
    )
    session.project.properties["voltage"] = 480
    session.project.properties["phases"] = 3
    session.project.properties["frequency"] = 60
    session.project.properties["job"] = "J-42"
    session.add_sheet("panel", kind="panel_layout")
    session.set_enclosure(nema_type="12", sheet_id="panel")
    return session


def _schematic_id(session: Session) -> str:
    for sheet in session.project.sheets:
        if sheet.kind == "schematic":
            return sheet.id
    return session.project.default_sheet().id


def _place(session: Session, library_id: str, ref: str, **props: object):
    # The enclosure sheet is a layout. These symbols are schematic loads, so
    # they stay off that sheet and the layout-ready gate does not apply.
    sheet_id = _schematic_id(session)
    session.place(library_id, ref=ref, x=40, y=40, sheet_id=sheet_id)
    if props:
        session.set_component_properties(ref, props, sheet_id=sheet_id)
    return ref


def _field(session: Session, key: str):
    plate = build_nameplate(session.project, session.catalog)
    found = plate.field(key)
    assert found is not None, key
    return plate, found


def _checks(session: Session, check: str) -> list[dict[str, str]]:
    return [row for row in session.design_checks() if row["check"] == check]


def test_three_phase_fla_is_table_current_plus_transformer_primary():
    """10 hp at 480 V is the NEC Table 430.250 460 V cell, 14 A, plus VA/V."""

    session = _base()
    _place(session, "nema.loads.motor_3ph", "M1", hp=10, voltage=480, phases=3)
    _place(session, "nema.power.control_transformer", "T1", VA=480, primary="480V", phases=1)
    looked = motor_flc(hp=10, voltage=480, phases=3)
    assert looked is not None
    _hp, column, amps = looked
    assert column == 460
    assert amps == 14.0
    plate, fla = _field(session, "fla")
    assert fla.value == "15 A"
    assert fla.raw == 15.0
    assert fla.overridden is False
    included = [line for line in plate.lines if line.included and line.ref]
    assert {line.ref: line.amperes for line in included} == {"M1": 14.0, "T1": 1.0}
    working = " ".join(line.text for line in plate.lines)
    assert "NEC Table 430.250" in working
    assert "460" in working
    assert "480 VA / 480 V" in working
    assert "verify per UL 508A 49.2" in working
    assert any(row["check"] == "nameplate_flc_approximate" for row in plate.issues)
    assert plate.field("voltage").value == "480 V"
    assert plate.field("phases").value == "3"
    assert plate.field("frequency").value == "60 Hz"
    assert plate.field("enclosure_type").value == "12"
    assert plate.field("drawing_number").value == "E-100"
    assert plate.field("date_code").value == "2026-10-08"
    assert plate.field("manufacturer").value == "Acme Controls"
    assert plate.field("job_number").value == "J-42"
    assert plate.field("largest_load").raw == 14.0


def test_single_phase_table_and_three_phase_transformer_primary():
    session = _base()
    session.project.properties["voltage"] = 115
    session.project.properties["phases"] = 1
    _place(session, "nema.loads.motor_1ph", "M1", hp=1, voltage=115, phases=1)
    looked = motor_flc(hp=1, voltage=115, phases=1)
    assert looked is not None
    assert looked[2] == 16.0
    _plate, fla = _field(session, "fla")
    assert fla.value == "16 A"
    assert "NEC Table 430.248" in " ".join(line.text for line in _plate.lines)

    three = _base("Three-phase CPT")
    _place(three, "nema.power.control_transformer", "T1", VA=480, primary="480V", phases=3)
    plate, fla = _field(three, "fla")
    expected = 480.0 / (3.0**0.5 * 480.0)
    line = next(item for item in plate.lines if item.ref == "T1")
    assert line.included is True
    assert abs(line.amperes - expected) < 1e-9
    assert fla.value == "0.58 A"
    assert "√3" in line.text


def test_interlock_spare_and_control_voltage_are_left_out_of_the_sum():
    session = _base()
    _place(session, "nema.loads.motor_3ph", "M1", FLA="10", interlock_group="PUMPS")
    _place(session, "nema.loads.motor_3ph", "M2", FLA="4", interlock_group="PUMPS")
    _place(session, "nema.loads.motor_3ph", "M3", FLA="9", spare="yes")
    _place(session, "iec.loads.solenoid", "Y1", FLA="2", voltage=24)
    _place(session, "nema.power.control_transformer", "T1", VA=480, primary="480V", phases=1)
    plate, fla = _field(session, "fla")
    assert fla.value == "11 A"
    by_ref = {line.ref: line for line in plate.lines if line.ref}
    assert by_ref["M1"].included is True
    assert by_ref["M2"].included is False
    assert "left out" in by_ref["M2"].text
    assert by_ref["M3"].included is False
    assert by_ref["Y1"].included is False
    assert by_ref["T1"].amperes == 1.0
    assert plate.field("voltage").value == "480 V"


def test_single_phase_heater_on_a_three_phase_supply_uses_watts_over_volts():
    session = _base()
    _place(session, "iec.loads.solenoid", "H1", watts=2400, voltage=480, phases=1)
    plate, fla = _field(session, "fla")
    assert fla.value == "5 A"
    line = next(item for item in plate.lines if item.ref == "H1")
    assert abs(line.amperes - 5.0) < 1e-9
    assert "√3" not in line.text


def test_iec_drawing_does_not_borrow_nec_motor_tables():
    session = _base()
    session.set_title_block(standard="IEC")
    _place(session, "iec.loads.motor_3ph", "M1", hp=10, voltage=400, phases=3)
    plate, fla = _field(session, "fla")
    assert fla.value == ""
    assert fla.engineer_confirm is True
    text = " ".join(line.text for line in plate.lines)
    assert "NEC Table 430.250" not in text
    assert "IEC" in text
    assert any(row["target"] == "fla" for row in _checks(session, "nameplate_missing_field"))


def test_override_is_badged_and_can_be_cleared():
    session = _base()
    _place(session, "nema.loads.motor_3ph", "M1", FLA="14")
    session.nameplate_set(fla="22", manufacturer="Shop Plate")
    plate, fla = _field(session, "fla")
    assert fla.value == "22 A"
    assert fla.computed == "14 A"
    assert fla.overridden is True
    assert plate.field("manufacturer").overridden is True
    assert not any(mark.text == "OVR" for mark in plate.marks)
    working = " ".join(line.text for line in plate.lines)
    assert "Overridden: 22 A (computed 14 A)" in working
    session.nameplate_set(clear=["fla"])
    _plate, fla = _field(session, "fla")
    assert fla.value == "14 A"
    assert fla.overridden is False


def test_missing_fields_sccr_and_fla_checks():
    from panelcad.electrical.nameplate import NAMEPLATE_PROMPT

    bare = Session.create("Bare")
    prompt = _checks(bare, "nameplate_missing_field")
    assert [row["message"] for row in prompt] == [NAMEPLATE_PROMPT]
    assert prompt[0]["severity"] == "info"
    assert "NEC" not in prompt[0]["message"]

    started = Session.create("Started")
    started.nameplate_set(preset="3x5")
    missing = {row["target"] for row in _checks(started, "nameplate_missing_field")}
    assert "manufacturer" in missing
    assert "voltage" in missing
    assert "phases" in missing
    assert "frequency" in missing
    assert "fla" in missing
    assert "sccr" in missing
    assert "enclosure_type" in missing
    assert "drawing_number" in missing
    assert "designation" in missing
    assert "date_code" not in missing
    assert "service_equipment" not in missing

    session = _base()
    _place(session, "nema.loads.motor_3ph", "M1", FLA="40")
    _place(session, "nema.breakers.mcb_3p_32a", "CB1", main="yes")
    session.nameplate_set(sccr="5")
    session.project.properties["available_fault_ka"] = 10
    assert _checks(session, "nameplate_fla_above_ocpd")
    assert _checks(session, "nameplate_sccr_below_available")
    assert not any(row["target"] == "voltage" for row in _checks(session, "nameplate_missing_field"))

    session.nameplate_set(fla="20", sccr="25")
    assert _checks(session, "nameplate_fla_above_ocpd") == []
    assert _checks(session, "nameplate_sccr_below_available") == []


def test_two_supplies_print_a_caution_label():
    session = _base()
    _place(session, "nema.loads.motor_3ph", "M1", FLA="10", supply="1")
    _place(
        session,
        "nema.loads.motor_1ph",
        "M2",
        FLA="6",
        supply="2",
        voltage=240,
        phases=1,
        frequency=60,
    )
    plate = build_nameplate(session.project, session.catalog)
    assert plate.field("voltage").value == "480 V"
    assert plate.field("voltage:2").value == "240 V"
    assert plate.field("fla").value == "10 A"
    assert plate.field("fla:2").value == "6 A"
    assert plate.field("fla:1") is None
    assert any(row["target"] == "disconnect_locations" for row in plate.issues)
    wording = " ".join(mark.text for mark in plate.caution_marks if mark.kind == "text")
    assert "VERIFY AGAINST UL 508A 55.4 / NEC 409" in wording
    assert "MULTIPLE DISCONNECTING MEANS" in wording
    assert wording.count("WARNING") == 1
    assert not any(mark.text == "WARNING" for mark in plate.caution_marks)
    assert "DS1" not in wording
    assert "Risk of Electric Shock" not in wording

    session.nameplate_set(disconnect_locations="DS1 on the door, DS2 in the field")
    plate = build_nameplate(session.project, session.catalog)
    assert plate.caution_marks
    assert plate.caution_size_mm is not None
    wording = " ".join(mark.text for mark in plate.caution_marks if mark.kind == "text")
    assert "VERIFY AGAINST UL 508A 55.4 / NEC 409" in wording
    assert wording.count("WARNING") == 1
    assert "DS1 on the door" in wording
    assert not any(issue["target"] == "disconnect_locations" for issue in plate.issues)
    width, height = plate.caution_size_mm
    for mark in plate.caution_marks:
        if mark.kind == "text":
            assert mark.x >= 0
            assert mark.x < width
            assert mark.y <= height


def test_plate_preset_custom_size_and_marks_stay_on_the_plate():
    session = _base()
    _place(session, "nema.loads.motor_3ph", "M1", FLA="14")
    _place(session, "nema.breakers.mcb_3p_32a", "CB1", main="yes")
    session.nameplate_set(sccr="65", service_equipment="yes")
    plate = build_nameplate(session.project, session.catalog)
    assert plate.preset == "3x5"
    assert abs(plate.width_mm - 5 * INCH) < 1e-6
    assert abs(plate.height_mm - 3 * INCH) < 1e-6
    _assert_on_plate(plate)
    assert any(mark.text.startswith("SUITABLE FOR USE") for mark in plate.marks)
    wide = [mark.text for mark in plate.marks if mark.kind == "text"]
    assert "SHORT-CIRCUIT CURRENT RATING" in wide
    assert "LARGEST MOTOR OR LOAD" in wide

    session.nameplate_set(preset="custom", width_in=4, height_in=2, hole_diameter_in=0.125, hole_inset_in=0.25)
    plate = build_nameplate(session.project, session.catalog)
    assert plate.preset == "custom"
    assert abs(plate.width_mm - 4 * INCH) < 1e-6
    assert abs(plate.height_mm - 2 * INCH) < 1e-6
    holes = [mark for mark in plate.marks if mark.layer == "HOLE"]
    assert len(holes) == 4
    assert abs(holes[0].r - (0.125 * INCH) / 2.0) < 1e-6
    _assert_on_plate(plate)
    texts = [mark.text for mark in plate.marks if mark.kind == "text"]
    for key in ("voltage", "phases", "frequency", "fla", "sccr", "enclosure_type", "drawing_number", "date_code"):
        field = plate.field(key)
        assert field is not None and field.value
        assert any(field.label[:8] in text or field.value[:6] in text for text in texts), key
    assert "SCCR" in texts
    assert "LARGEST LOAD FLA" in texts
    assert not any("…" in text or "..." in text for text in texts)


def _assert_on_plate(plate) -> None:
    for mark in plate.marks:
        if mark.kind == "rect":
            assert mark.x >= -0.05
            assert mark.y >= -0.05
            assert mark.x + mark.w <= plate.width_mm + 0.05
            assert mark.y + mark.h <= plate.height_mm + 0.05
        elif mark.kind == "circle":
            assert mark.x - mark.r >= -0.05
            assert mark.y - mark.r >= -0.05
            assert mark.x + mark.r <= plate.width_mm + 0.05
            assert mark.y + mark.r <= plate.height_mm + 0.05
        elif mark.kind == "text":
            assert 0 <= mark.x <= plate.width_mm
            assert 0 <= mark.y <= plate.height_mm
        elif mark.kind == "line":
            assert mark.x >= 0
            assert mark.x + mark.w <= plate.width_mm + 0.05


def test_pdf_and_dxf_are_real_size(tmp_path: Path):
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWidgets import QApplication

    from panelcad.electrical.nameplate_page import (
        write_nameplate_dxf,
        write_nameplate_pdf,
        write_nameplate_png,
    )

    if QApplication.instance() is None:
        QApplication([])
    session = _base()
    _place(session, "nema.loads.motor_3ph", "M1", FLA="14")
    session.nameplate_set(
        sccr="65",
        hole_diameter_in=0.125,
        hole_inset_in=0.25,
        disconnect_locations="DS1 door; DS2 field",
    )
    _place(
        session,
        "nema.loads.motor_1ph",
        "M2",
        FLA="6",
        supply="2",
        voltage=240,
        phases=1,
        frequency=60,
    )
    plate = build_nameplate(session.project, session.catalog)
    pdf = tmp_path / "nameplate.pdf"
    png = tmp_path / "nameplate.png"
    dxf = tmp_path / "nameplate.dxf"
    write_nameplate_pdf(pdf, plate)
    write_nameplate_png(png, plate, scale=8)
    paths = write_nameplate_dxf(dxf, plate)
    assert pdf.stat().st_size > 1000
    assert png.stat().st_size > 1000
    assert len(paths) == 2

    doc = QPdfDocument()
    assert doc.load(str(pdf)) == QPdfDocument.Error.None_
    assert doc.pageCount() == 2
    first = doc.pagePointSize(0)
    assert abs(first.width() - 5 * 72) < 1.5
    assert abs(first.height() - 3 * 72) < 1.5
    second = doc.pagePointSize(1)
    caution_w, caution_h = plate.caution_size_mm
    assert abs(second.width() - caution_w / INCH * 72) < 0.05
    assert abs(second.height() - caution_h / INCH * 72) < 0.05
    assert abs(second.height() - round(second.height())) < 0.05
    assert abs(second.height() % 6) < 0.05
    text_marks = [mark for mark in plate.caution_marks if mark.kind == "text"]
    assert any("Disconnect locations" in mark.text for mark in text_marks)
    assert max(mark.y for mark in text_marks) < caution_h - 2.0
    from PySide6.QtCore import QSize

    rendered = doc.render(1, QSize(int(second.width() * 3), int(second.height() * 3)))
    width_px, height_px = rendered.width(), rendered.height()
    ink = 0
    for y in range(int(height_px * 0.62), height_px - 2):
        for x in range(6, width_px - 6, 4):
            if rendered.pixelColor(x, y).red() < 180:
                ink += 1
    assert ink > 15
    border = doc.render(1, QSize(int(round(second.width() / 72 * 300)), int(round(second.height() / 72 * 300))))
    edges = {side: _edge_px(border, side) for side in ("left", "right", "top", "bottom")}
    assert edges == {"left": 2, "right": 2, "top": 2, "bottom": 2}, edges
    assert not any(mark.text == "OVR" for mark in plate.marks)

    import ezdxf

    drawing = ezdxf.readfile(dxf)
    assert drawing.units == ezdxf.units.MM
    layers = {entity.dxf.layer for entity in drawing.modelspace()}
    assert layers == {"TEXT", "CUT", "HOLE"}
    outline = next(entity for entity in drawing.modelspace() if entity.dxf.layer == "CUT")
    xs = [point[0] for point in outline.get_points()]
    ys = [point[1] for point in outline.get_points()]
    assert abs(max(xs) - min(xs) - plate.width_mm) < 0.05
    assert abs(max(ys) - min(ys) - plate.height_mm) < 0.05
    assert sum(1 for entity in drawing.modelspace() if entity.dxf.layer == "HOLE") == 4
    caution = ezdxf.readfile(paths[1])
    assert {entity.dxf.layer for entity in caution.modelspace()} <= {"TEXT", "CUT", "HOLE"}
    assert any(entity.dxf.layer == "TEXT" for entity in caution.modelspace())


def test_title_block_and_print_package_include_the_plate(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from panelcad.electrical.nameplate import title_block_lines
    from panelcad.render.print_package import extract_pdf_text

    if QApplication.instance() is None:
        QApplication([])
    session = _base()
    _place(session, "nema.loads.motor_3ph", "M1", FLA="14")
    session.nameplate_set(sccr="65")
    lines = title_block_lines(session.project, session.catalog)
    assert any("480 V" in line and "FLA 14 A" in line for line in lines)
    assert any("SCCR" in line and "TYPE 12" in line for line in lines)

    dest = tmp_path / "package.pdf"
    result = session.print_package(dest, sections=["nameplate"])
    assert result["order"] == ["nameplate"]
    text = extract_pdf_text(dest, 0)
    assert "NAMEPLATE" in text
    assert "FULL-LOAD WORKING" in text
    assert "14" in text


def test_title_block_nameplate_is_computed_once_per_print(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from panelcad.electrical import nameplate as nameplate_mod

    if QApplication.instance() is None:
        QApplication([])
    session = _base()
    assert len(session.project.sheets) >= 2
    calls = {"n": 0}
    real = nameplate_mod.title_block_lines

    def wrapped(project, catalog=None):
        calls["n"] += 1
        return real(project, catalog)

    monkeypatch.setattr(nameplate_mod, "title_block_lines", wrapped)
    dest = tmp_path / "once.pdf"
    session.print_package(dest, sections=["drawings"])
    assert dest.is_file()
    assert calls["n"] == 1


def test_title_block_nameplate_failure_still_paints(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from panelcad.electrical import nameplate as nameplate_mod
    from panelcad.render.svg import sheet_to_svg

    if QApplication.instance() is None:
        QApplication([])
    session = _base()
    sheet = next(item for item in session.project.sheets if item.kind == "schematic")

    def boom(*_args, **_kwargs):
        raise RuntimeError("nameplate failed")

    monkeypatch.setattr(nameplate_mod, "title_block_lines", boom)
    dest = tmp_path / "package.pdf"
    result = session.print_package(dest, sections=["drawings"])
    assert dest.is_file()
    assert dest.stat().st_size > 1000
    assert result["pages"] >= 2

    svg = sheet_to_svg(sheet, session.catalog, session.project)
    assert "Acme Controls" in svg
    assert "480 V" not in svg
    assert "TYPE 12" not in svg


def test_jsonl_cli_and_dialog(tmp_path: Path):
    from PySide6.QtWidgets import QApplication, QLineEdit
    from typer.testing import CliRunner

    from panelcad.cli.agent import Agent
    from panelcad.cli.main import app
    from panelcad.gui.nameplate_dialog import NameplateDialog

    agent = Agent()
    assert agent.handle({"id": "E-100", "op": "create_project", "name": "Agent plate"})["ok"]
    assert agent.handle({"id": "req-2", "op": "help"})["result"]["ops"]
    help_ops = agent.handle({"id": "req-2b", "op": "help"})["result"]["ops"]
    assert {"nameplate_build", "nameplate_set", "nameplate_export"} <= set(help_ops)
    placed = agent.handle(
        {"id": "req-3", "op": "place", "library_id": "nema.loads.motor_3ph", "ref": "M1", "x": 40, "y": 40}
    )
    assert placed["ok"], placed
    assert "E-100" not in {row.get("id") for row in agent.session.list_components()}
    updated = agent.handle({"id": "req-4", "op": "nameplate_set", "manufacturer": "Agent Shop", "fla": "12.5"})
    assert updated["ok"], updated
    built = agent.handle({"id": "req-5", "op": "nameplate_build"})
    assert built["ok"] and built["id"] == "req-5"
    fields = {item["key"]: item for item in built["result"]["fields"]}
    assert fields["manufacturer"]["value"] == "Agent Shop"
    assert fields["manufacturer"]["overridden"] is True
    assert fields["fla"]["value"] == "12.5 A"
    pdf = tmp_path / "agent.pdf"
    dxf = tmp_path / "agent.dxf"
    png = tmp_path / "agent.png"
    exported = agent.handle(
        {"id": "req-6", "op": "nameplate_export", "pdf": str(pdf), "dxf": str(dxf), "png": str(png)}
    )
    assert exported["ok"], exported
    assert pdf.is_file() and dxf.is_file() and png.is_file()
    bad = agent.handle({"id": "req-7", "op": "nameplate_set", "not_a_field": "x"})
    assert bad["ok"] is False

    path = tmp_path / "plate.pcad.yaml"
    session = _base("CLI plate")
    _place(session, "nema.loads.motor_3ph", "M1", FLA="14")
    session.save(path)
    cli_pdf = tmp_path / "cli.pdf"
    cli_dxf = tmp_path / "cli.dxf"
    result = CliRunner().invoke(
        app,
        [
            "nameplate",
            str(path),
            "--pdf",
            str(cli_pdf),
            "--dxf",
            str(cli_dxf),
            "--set",
            "fla=18",
            "--size",
            "4x2",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "FULL-LOAD CURRENT" in result.output
    assert "overridden" in result.output
    assert cli_pdf.is_file() and cli_dxf.is_file()
    opened = Session.open(path)
    plate = build_nameplate(opened.project, opened.catalog)
    assert plate.preset == "custom"
    assert abs(plate.width_mm - 4 * INCH) < 1e-6
    assert plate.field("fla").value == "18 A"
    assert plate.field("fla").overridden is True

    if QApplication.instance() is None:
        QApplication([])
    dialog = NameplateDialog(opened)
    dialog.show()
    QApplication.processEvents()
    edit = dialog.findChild(QLineEdit, "nameplate-fla")
    assert edit is not None
    assert edit.text() == "18 A"
    assert "14 A" in dialog.working.toPlainText()
    assert dialog.preview._plate is not None
    opened.set_title_block(company="Acme Process Controls")
    dialog.close()
    dialog = NameplateDialog(opened)
    dialog.show()
    QApplication.processEvents()
    maker = dialog.findChild(QLineEdit, "nameplate-manufacturer")
    assert maker is not None
    assert maker.text() == "Acme Process Controls"
    margins = maker.textMargins()
    available = maker.contentsRect().width() - margins.left() - margins.right()
    assert available >= maker.fontMetrics().horizontalAdvance(maker.text())
    dialog._commit_field("manufacturer")
    assert build_nameplate(opened.project, opened.catalog).field("manufacturer").overridden is False
    edit = dialog.findChild(QLineEdit, "nameplate-fla")
    assert edit is not None and edit.text() == "18 A"
    edit.setText("16")
    dialog._commit_field("fla")
    QApplication.processEvents()
    assert dialog._badges["fla"].text() == "overridden"
    assert build_nameplate(opened.project, opened.catalog).field("fla").value == "16 A"
    from unittest.mock import patch

    from PySide6.QtWidgets import QMessageBox

    with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.No) as warned:
        dialog.export_pdf()
    assert warned.called
    text = warned.call_args.args[2]
    assert "not confirmed" in text.lower() or "verify" in text.lower()
    dialog.close()


def test_dialog_survives_tab_click_and_enter():
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLineEdit

    from panelcad.gui.nameplate_dialog import NameplateDialog

    if QApplication.instance() is None:
        QApplication([])
    session = _base()
    _place(session, "nema.loads.motor_3ph", "M1", FLA="14")
    dialog = NameplateDialog(session)
    dialog.show()
    QApplication.processEvents()

    def edit(key: str) -> QLineEdit:
        found = dialog.findChild(QLineEdit, f"nameplate-{key}")
        assert found is not None, key
        return found

    fla = edit("fla")
    fla.setFocus()
    QApplication.processEvents()
    fla.clear()
    QTest.keyClicks(fla, "7.5")
    QTest.keyClick(fla, Qt.Key.Key_Tab)
    QApplication.processEvents()
    assert edit("fla").text() == "7.5 A"
    assert dialog._badges["fla"].text() == "overridden"
    assert build_nameplate(session.project, session.catalog).field("fla").value == "7.5 A"

    voltage = edit("voltage")
    voltage.setFocus()
    QApplication.processEvents()
    voltage.selectAll()
    QTest.keyClicks(voltage, "240")
    QTest.mouseClick(edit("manufacturer"), Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert edit("voltage").text() == "240 V"
    assert build_nameplate(session.project, session.catalog).field("voltage").value == "240 V"

    frequency = edit("frequency")
    frequency.setFocus()
    QApplication.processEvents()
    frequency.selectAll()
    QTest.keyClicks(frequency, "50")
    QTest.keyClick(frequency, Qt.Key.Key_Return)
    QApplication.processEvents()
    assert edit("frequency").text() == "50 Hz"
    assert build_nameplate(session.project, session.catalog).field("frequency").value == "50 Hz"
    assert "Overridden: 7.5 A (computed 14 A)" in dialog.working.toPlainText()
    assert not any(mark.text == "OVR" for mark in dialog.preview._plate.marks)
    dialog.close()


def test_enter_commits_without_starting_export():
    """Return in a field commits the value. It does not click Export PDF."""

    from unittest.mock import patch

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QFileDialog, QLineEdit, QMessageBox, QPushButton

    from panelcad.gui.nameplate_dialog import NameplateDialog

    if QApplication.instance() is None:
        QApplication([])
    session = _base()
    _place(session, "nema.loads.motor_3ph", "M1", FLA="14")
    dialog = NameplateDialog(session)
    dialog.show()
    QApplication.processEvents()
    buttons = dialog.findChildren(QPushButton)
    assert buttons
    assert all(not button.autoDefault() and not button.isDefault() for button in buttons)

    fla = dialog.findChild(QLineEdit, "nameplate-fla")
    assert fla is not None
    fla.setFocus()
    QApplication.processEvents()
    fla.clear()
    QTest.keyClicks(fla, "7.5")
    with (
        patch.object(dialog, "_confirm_export", return_value=False) as confirm,
        patch.object(QMessageBox, "warning") as warned,
        patch.object(QFileDialog, "getSaveFileName", return_value=("", "")) as save,
    ):
        QTest.keyClick(fla, Qt.Key.Key_Return)
        QApplication.processEvents()
    assert confirm.call_count == 0
    assert warned.call_count == 0
    assert save.call_count == 0
    committed = dialog.findChild(QLineEdit, "nameplate-fla")
    assert committed is not None
    assert committed.text() == "7.5 A"
    assert build_nameplate(session.project, session.catalog).field("fla").value == "7.5 A"
    buttons = dialog.findChildren(QPushButton)
    assert all(not button.autoDefault() and not button.isDefault() for button in buttons)
    dialog.close()


def test_shop_field_hints_name_the_plate_standards():
    from PySide6.QtWidgets import QApplication, QLineEdit

    from panelcad.electrical.nameplate import SHOP_CONFIRM_PLACEHOLDER
    from panelcad.gui.nameplate_dialog import NameplateDialog

    if QApplication.instance() is None:
        QApplication([])
    session = Session.create("Bare plate")
    dialog = NameplateDialog(session)
    dialog.show()
    QApplication.processEvents()
    plate = dialog.preview._plate
    assert plate is not None
    working = dialog.working.toPlainText()
    expected = {
        "catalog_model": "catalog or model",
        "job_number": "job number",
        "max_ocpd": "maximum overcurrent device",
        "largest_load": "largest motor or load",
    }
    for key, what in expected.items():
        edit = dialog.findChild(QLineEdit, f"nameplate-{key}")
        assert edit is not None, key
        assert edit.text() == ""
        assert edit.placeholderText() == SHOP_CONFIRM_PLACEHOLDER
        margins = edit.textMargins()
        available = edit.contentsRect().width() - margins.left() - margins.right()
        assert available > 0
        assert edit.fontMetrics().horizontalAdvance(edit.placeholderText()) <= available
        item = plate.field(key)
        assert item is not None
        assert edit.toolTip() == item.note
        assert what in item.note
        assert "NEC 409.110" in item.note
        assert "UL 508A" in item.note
        assert "NFPA 79" not in item.note
        assert "NFPA 79" not in item.citation
        assert item.note in working
    designation = next(row for row in plate.issues if row["target"] == "designation")
    assert "NFPA 79" not in designation["message"]
    assert "NEC 409.110" in designation["message"]
    assert "UL 508A" in designation["message"]
    dialog.close()


def _edge_px(image, side: str) -> int:
    """Median dark-pixel run of one plate border, sampled away from the corners."""

    width, height = image.width(), image.height()
    samples: list[int] = []
    if side in {"left", "right"}:
        x0 = 0 if side == "left" else width - 1
        step = 1 if side == "left" else -1
        for y in range(height // 4, 3 * height // 4):
            run = 0
            for offset in range(min(20, width)):
                if image.pixelColor(x0 + step * offset, y).red() < 160:
                    run += 1
                elif run:
                    break
            samples.append(run)
    else:
        y0 = 0 if side == "top" else height - 1
        step = 1 if side == "top" else -1
        for x in range(width // 4, 3 * width // 4, 3):
            run = 0
            for offset in range(min(20, height)):
                if image.pixelColor(x, y0 + step * offset).red() < 160:
                    run += 1
                elif run:
                    break
            samples.append(run)
    samples.sort()
    return samples[len(samples) // 2]


def test_nameplate_export_logs_each_written_path(tmp_path, monkeypatch):
    from panelcad.diag.log import configure_logging, log_file_path

    monkeypatch.setenv("PANELCAD_DATA_DIR", str(tmp_path / "data"))
    configure_logging()
    session = _base()
    pdf = tmp_path / "plate.pdf"
    dxf = tmp_path / "plate.dxf"
    png = tmp_path / "plate.png"
    session.nameplate_export(pdf=pdf, dxf=dxf, png=png)
    text = log_file_path().read_text(encoding="utf-8")
    assert text.count("nameplate_export") >= 3
    assert pdf.name in text
    assert dxf.name in text
    assert png.name in text
    try:
        session.nameplate_export()
    except ValueError:
        pass
    else:
        raise AssertionError("nameplate_export without a path should fail")
    failed = log_file_path().read_text(encoding="utf-8")
    assert "error nameplate_export" in failed


def test_bare_current_overrides_are_engraved_with_units():
    session = _base()
    _place(session, "nema.loads.motor_3ph", "M1", FLA="10", supply="1")
    _place(session, "nema.loads.motor_1ph", "M2", FLA="6", supply="2", voltage=240, phases=1, frequency=60)
    session.nameplate_set(**{"fla": "18", "fla:2": "7.5", "sccr": "65", "max_ocpd": "32"})
    plate = build_nameplate(session.project, session.catalog)
    assert plate.field("fla").value == "18 A"
    assert plate.field("fla:2").value == "7.5 A"
    assert plate.field("sccr").value == "65 kA"
    assert plate.field("max_ocpd").value == "32 A"
    assert any(mark.text == "18 A" for mark in plate.marks if mark.kind == "text")
    assert any(mark.text == "7.5 A" for mark in plate.marks if mark.kind == "text")
    session.nameplate_set(**{"fla": "18 A", "sccr": "5 kA @ 480 V"})
    plate = build_nameplate(session.project, session.catalog)
    assert plate.field("fla").value == "18 A"
    assert plate.field("sccr").value == "5 kA @ 480 V"
    working = " ".join(line.text for line in plate.lines)
    assert "Overridden: 7.5 A (computed 6 A)" in working
    assert not any(mark.text == "OVR" for mark in plate.marks)


def test_bare_voltage_and_frequency_keep_units():
    from panelcad.electrical.nameplate import title_block_lines

    session = _base()
    _place(session, "nema.loads.motor_3ph", "M1", FLA="14")
    session.nameplate_set(voltage="480", frequency="60")
    plate = build_nameplate(session.project, session.catalog)
    assert plate.field("voltage").value == "480 V"
    assert plate.field("frequency").value == "60 Hz"
    line = title_block_lines(session.project, session.catalog)[0]
    assert "480 V" in line
    assert "60 Hz" in line
    bare = line.replace("480 V", "").replace("60 Hz", "")
    assert "480" not in bare
    assert "60" not in bare


def test_single_column_values_share_one_column():
    from panelcad.render.qt_painter import text_advance_mm

    session = _base()
    _place(session, "nema.loads.motor_3ph", "M1", FLA="14")
    _place(session, "nema.breakers.mcb_3p_32a", "CB1", main="yes")
    session.nameplate_set(sccr="65")
    plate = build_nameplate(session.project, session.catalog)

    def beside(label: str):
        row = next(mark for mark in plate.marks if mark.kind == "text" and mark.text == label)
        values = [
            mark
            for mark in plate.marks
            if mark.kind == "text"
            and mark.text not in {"OVR", "CONFIRM", "VERIFY"}
            and abs(mark.y - row.y) < 0.4
            and mark.x > row.x + 1
        ]
        assert len(values) == 1, (label, [mark.text for mark in values])
        gap = values[0].x - (row.x + text_advance_mm(row.text, row.size))
        return values[0], gap

    flc, flc_gap = beside("FULL-LOAD CURRENT")
    sccr, sccr_gap = beside("SHORT-CIRCUIT CURRENT RATING")
    assert abs(flc.x - sccr.x) < 0.05
    assert flc_gap >= 3.0
    assert sccr_gap >= 3.0


def test_supply_blocks_keep_each_supply_together():
    session = _base()
    _place(session, "nema.loads.motor_3ph", "M1", FLA="10", supply="1")
    _place(session, "nema.loads.motor_1ph", "M2", FLA="6", supply="2", voltage=240, phases=1, frequency=60)
    plate = build_nameplate(session.project, session.catalog)

    def at(text: str):
        hits = [mark for mark in plate.marks if mark.kind == "text" and mark.text == text]
        assert len(hits) == 1, (text, [mark.text for mark in hits])
        return hits[0]

    primary = at("480 V")
    second_voltage = at("240 V")
    second_flc = at("6 A")
    hertz = [
        mark
        for mark in plate.marks
        if mark.kind == "text" and mark.text == "60 Hz" and abs(mark.x - second_voltage.x) < 1
    ]
    assert hertz, "supply 2 frequency is not in the supply 2 block"
    assert abs(second_flc.x - second_voltage.x) < 1
    assert abs(primary.x - second_voltage.x) > 10
    ys = sorted(mark.y for mark in (second_voltage, hertz[0], second_flc))
    assert ys[-1] - ys[0] < 30


def test_missing_phases_are_not_assumed_single_phase():
    session = _base()
    session.project.properties.pop("phases")
    _place(session, "iec.loads.solenoid", "H1", watts=2400, voltage=480)
    plate, fla = _field(session, "fla")
    assert fla.value == ""
    text = " ".join(line.text for line in plate.lines)
    assert "understate" in text
    assert not any(line.included and line.amperes for line in plate.lines)

    derived = _base("Derived phases")
    _place(derived, "iec.loads.solenoid", "H1", watts=2400, voltage=480)
    plate, fla = _field(derived, "fla")
    expected = 2400.0 / (3.0**0.5 * 480.0)
    line = next(item for item in plate.lines if item.ref == "H1")
    assert abs(line.amperes - expected) < 1e-9
    assert "√3" in line.text
    assert abs(float(fla.raw) - expected) < 0.02


def test_mixed_phase_loads_are_not_one_line_current():
    session = _base()
    _place(session, "nema.loads.motor_3ph", "M1", FLA="14", phases=3)
    _place(session, "iec.loads.solenoid", "H1", watts=2400, voltage=480, phases=1)
    plate, fla = _field(session, "fla")
    assert fla.value == ""
    assert "not added as one line current" in " ".join(line.text for line in plate.lines)


def test_off_table_horsepower_is_not_snapped():
    from panelcad.core.motor_sizing import motor_flc, nearest_table_hp

    neighbor = nearest_table_hp(6, phases=3)
    assert neighbor is not None
    snapped = motor_flc(hp=neighbor, voltage=480, phases=3)
    assert snapped is not None
    session = _base()
    _place(
        session,
        "nema.loads.motor_3ph",
        "M1",
        hp=6,
        voltage=480,
        phases=3,
        FLA=snapped[2],
        fla_source="nec-table",
    )
    plate, fla = _field(session, "fla")
    assert fla.value == ""
    assert abs(float(snapped[2]) - 0) > 0
    text = " ".join(line.text for line in plate.lines)
    assert "not a published" in text
    assert "neighboring" in text
    assert f"{snapped[2]}" not in text

    session.set_component_properties(
        "M1", {"FLA": "11", "fla_source": "nameplate"}, sheet_id=_schematic_id(session)
    )
    plate, fla = _field(session, "fla")
    assert fla.value == "11 A"
    assert "entered" in " ".join(line.text for line in plate.lines).lower()


def test_sccr_assumed_voltage_is_flagged():
    from panelcad.electrical.nameplate import sccr_mark

    text, confirm, note = sccr_mark(5, 480, "assumed 480 V", 480)
    assert text == "5 kA"
    assert "@" not in text
    assert confirm is True
    assert note
    text, confirm, _note = sccr_mark(5, 480, "project sccr_voltage", 480)
    assert text == "5 kA @ 480 V"
    assert confirm is False
    text, confirm, note = sccr_mark(5, 480, "motor nameplate voltage", 240)
    assert "@" not in text
    assert confirm is True
    assert "240" in note


def test_dol_starter_does_not_invent_supplies_and_accepts_fla(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    source = Path(__file__).resolve().parents[1] / "examples" / "dol_starter.pcad.yaml"
    session = Session.open(source)
    plate = build_nameplate(session.project, session.catalog)
    keys = " ".join(item.key for item in plate.fields)
    assert "24VDC" not in keys
    assert "3.3" not in keys
    assert plate.field("fla") is not None
    assert plate.field("fla:1") is None
    assert plate.field("voltage:1") is None
    assert not any("24VDC" in line.text or "3.3-27" in line.text for line in plate.lines)
    assert plate.field("manufacturer").value.startswith("Acme")
    urls = " ".join(item.get("url") or "" for item in plate.citations)
    assert "mikeholt" not in urls
    assert "industrialmonitordirect" not in urls

    copy = tmp_path / "dol.pcad.yaml"
    session.save(copy)
    pdf = tmp_path / "dol.pdf"
    png = tmp_path / "dol.png"
    result = CliRunner().invoke(app, ["nameplate", str(copy), "--set", "fla=18", "--pdf", str(pdf), "--png", str(png)])
    assert result.exit_code == 0, result.output + result.stderr
    assert "18" in result.output
    opened = Session.open(copy)
    marked = build_nameplate(opened.project, opened.catalog)
    assert marked.field("fla").value == "18 A"
    assert marked.field("fla").overridden is True
    assert pdf.is_file() and png.is_file()
    assert any(mark.text == "18 A" for mark in marked.marks if mark.kind == "text")
    from panelcad.electrical.nameplate import title_block_lines

    dol_lines = title_block_lines(session.project, session.catalog)
    assert dol_lines[0] != "3 PH"
    assert not dol_lines[0] or "V" in dol_lines[0] or "FLA" in dol_lines[0]
