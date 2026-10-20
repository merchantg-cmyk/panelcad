"""Unmatched symbols, NEMA library gaps, import words, and wire-editor fixes."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_unmatched_symbol_is_not_a_ground_or_a_24v_relay():
    from panelcad.importer.build import build_session
    from panelcad.importer.symbols import resolve_symbol
    from panelcad.library.catalog import default_catalog
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    catalog = default_catalog()
    terminal = resolve_symbol(catalog, tag="TB1-4", type_text="", standard="NEMA/ANSI")
    assert "terminals.pe" not in str(terminal["library_id"])
    relay = resolve_symbol(catalog, tag="CR2", type_text="", standard="NEMA/ANSI")
    assert "k_4co_24vdc" not in str(relay["library_id"])
    assert relay["library_id"] == "nema.relays.cr"
    floated = resolve_symbol(catalog, tag="FS1", type_text="float", standard="NEMA/ANSI")
    assert "limit" not in str(floated["library_id"])
    assert floated["library_id"].startswith("nema.operators.float_")

    session, report = build_session(
        {
            "standard": "NEMA/ANSI",
            "sheets": [
                {
                    "id": "power",
                    "kind": "schematic",
                    "width_mm": 420,
                    "height_mm": 297,
                    "devices": [
                        {
                            "tag": "TB1-1",
                            "type": "",
                            "symbol": "",
                            "library_id": "nema.terminals.pe",
                            "guess": True,
                            "x": 40,
                            "y": 40,
                            "confidence": 0.4,
                            "source": "model",
                        },
                        {
                            "tag": "CR2",
                            "type": "",
                            "symbol": "",
                            "library_id": "nema.relays.k_4co_24vdc",
                            "guess": True,
                            "x": 80,
                            "y": 40,
                            "confidence": 0.4,
                            "source": "vector",
                        },
                    ],
                    "wires": [],
                }
            ],
        },
        catalog=catalog,
    )
    placed = {comp.id: comp.library_id for comp in session.project.sheet("power").components}
    assert "terminals.pe" not in placed.get("TB1-1", "")
    assert placed["TB1-1"] == "nema.terminals.feed_through"
    # A prefix guess of the 24 V relay falls back to the generic coil, which has pins.
    assert placed["CR2"] == "nema.relays.cr"
    assert session.catalog.get(placed["CR2"]).pins
    assert "k_4co" not in placed["CR2"]
    assert NEEDS_INFO_LIBRARY_ID not in placed.values()
    _ = report


def test_vector_guess_keeps_a_family_symbol_with_pins():
    from panelcad.importer.build import build_session
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    devices = []
    for index, tag in enumerate(("CB1", "M1", "OL1", "MTR1", "CR1", "ZZ9")):
        devices.append(
            {
                "tag": tag,
                "type": "",
                "symbol": "",
                "library_id": "",
                "guess": True,
                "x": 40 + index * 30,
                "y": 40,
                "confidence": 0.4,
                "source": "vector",
            }
        )
    session, _report = build_session(
        {
            "standard": "NEMA/ANSI",
            "sheets": [
                {
                    "id": "power",
                    "kind": "schematic",
                    "width_mm": 420,
                    "height_mm": 297,
                    "devices": devices,
                    "wires": [],
                }
            ],
        },
        catalog=catalog,
    )
    placed = {comp.id: comp.library_id for comp in session.project.sheet("power").components}
    for tag in ("CB1", "M1", "OL1", "MTR1", "CR1"):
        assert tag in placed, tag
        assert placed[tag] != "reconstruct.needs_info"
        assert catalog.get(placed[tag]).pins, placed[tag]
    assert placed["CR1"] == "nema.relays.cr"
    assert "ZZ9" not in placed or placed["ZZ9"] == "reconstruct.needs_info"


def test_dol_pdf_symbol_classes_stay(tmp_path: Path):
    from panelcad.examples.dol import build_dol
    from panelcad.importer.pipeline import ImportJob
    from panelcad.importer.symbols import shape_class
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    truth = build_dol()
    pdf = tmp_path / "dol.pdf"
    truth.export_pdf(pdf)
    built = ImportJob.start([pdf], tmp_path / "ws", backend="external").build()
    truth_class = {}
    for sheet in truth.project.sheets:
        if sheet.kind in {"panel_layout", "door_layout"}:
            continue
        for comp in sheet.components:
            truth_class[comp.id] = shape_class(catalog.get(comp.library_id))
    for sheet in built.project.sheets:
        for comp in sheet.components:
            assert comp.library_id != "reconstruct.needs_info", comp.id
            if comp.id not in truth_class:
                continue
            assert shape_class(catalog.get(comp.library_id)) == truth_class[comp.id], comp.id


def test_unconnected_wire_numbers_are_print_wire_rows():
    from panelcad.importer.checks import findings_from_payload
    from panelcad.importer.pipeline import describe_wire_recovery

    numbers = [str(100 + index) for index in range(19)]
    items = [
        {"kind": "wire", "text": number, "reason": "needs_info" if index < 8 else "unresolved", "sheet": "power"}
        for index, number in enumerate(numbers)
    ]
    items.append({"kind": "device", "text": "U1", "reason": "needs_info", "sheet": "power"})
    rows = findings_from_payload({"items": items})
    wires = [row for row in rows if row.check == "print_wire"]
    assert len(wires) == 19
    assert all("was not connected" in row.message for row in wires)
    assert any(row.check == "print_unplaced" and row.ref == "U1" for row in rows)
    summary = describe_wire_recovery(devices=1, numbers_read=19, connected=0, unconnected=19)
    assert "19 numbers were left unconnected" in summary
    assert "Problems" in summary


def test_duplicate_unconnected_wire_number_is_one_problems_row():
    from panelcad.importer.checks import findings_from_payload
    from panelcad.importer.pipeline import describe_wire_recovery

    items = [
        {
            "kind": "wire",
            "text": "103",
            "reason": "unresolved",
            "sheet": "power",
            "x": 40,
            "y": 80,
            "crop": "crops/103-a.png",
        },
        {
            "kind": "wire",
            "text": "103",
            "reason": "needs_info",
            "sheet": "power",
            "x": 120,
            "y": 80,
            "crop": "crops/103-b.png",
        },
        {"kind": "wire", "text": "104", "reason": "unresolved", "sheet": "power", "x": 10, "y": 12},
        {"kind": "device", "text": "U1", "reason": "needs_info", "sheet": "power"},
    ]
    rows = findings_from_payload({"items": items})
    wires = [row for row in rows if row.check == "print_wire"]
    unique = []
    for item in items:
        number = str(item.get("text") or "").strip()
        if item.get("kind") == "wire" and number and number not in unique:
            unique.append(number)
    assert len(wires) == len(unique) == 2
    doubled = next(row for row in wires if row.ref == "103")
    assert doubled.message == "wire number 103 was not connected (2 places)"
    assert doubled.x == 40 and doubled.y == 80
    assert doubled.extra["crop"] == "crops/103-a.png"
    assert doubled.to_row()["crop"] == "crops/103-a.png"
    single = next(row for row in wires if row.ref == "104")
    assert single.message == "wire number 104 was not connected"
    assert "(2 places)" not in single.message
    assert any(row.check == "print_unplaced" and row.ref == "U1" for row in rows)
    summary = describe_wire_recovery(
        devices=1, numbers_read=len(unique), connected=0, unconnected=len(unique)
    )
    assert f"{len(wires)} numbers were left unconnected" in summary


def test_iec_s_tag_without_contact_sense_is_a_pushbutton():
    from panelcad.importer.symbols import resolve_symbol
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    for tag in ("S0", "S1", "S2"):
        resolved = resolve_symbol(catalog, tag=tag, type_text="", standard="IEC")
        assert resolved["library_id"] == "iec.operators.pb_no", tag
        assert resolved["guess"] is True
    sensed = resolve_symbol(catalog, tag="S1", type_text="nc", standard="IEC")
    assert sensed["library_id"] == "iec.operators.limit_nc"


@pytest.mark.parametrize(
    "library_id",
    ["iec.terminals.x_2p5", "iec.terminals.phoenix_ut_2p5", "iec.terminals.adc_dn_t10"],
)
def test_catalog_feedthrough_is_one_number_until_split(library_id: str):
    from panelcad.api.session import Session

    session = Session.create("Catalog TB")
    session.place("nema.operators.pb_no", ref="SS1", x=40, y=40)
    session.place(library_id, ref="TB", x=80, y=40)
    session.place("nema.operators.limit_nc", ref="LS1", x=120, y=40)
    session.wire("SS1.3", "TB.A")
    session.wire("TB.B", "LS1.1")
    numbers = [wire.number for wire in session.sheet().wires]
    assert len(set(numbers)) == 1, library_id
    session.set_wire_numbering(split_feedthrough=True)
    numbers = [wire.number for wire in session.sheet().wires]
    assert len(set(numbers)) == 2, library_id
    assert all(not wire.number_locked for wire in session.sheet().wires)


def test_svg_empty_import_does_not_ask_for_vision():
    from panelcad.importer.pipeline import _empty_import_warning

    svg = [{"source": "sheet.svg", "vector": False, "svg_notice": "The SVG was read as vectors."}]
    text = _empty_import_warning(svg, {"wires": []}, vision_preset="none")
    assert "scan needs a vision backend" not in text.lower()
    assert "SVG" in text
    scan = [{"source": "sheet.png", "vector": False}]
    assert "scan needs a vision backend" in _empty_import_warning(scan, {"wires": []}, vision_preset="none").lower()


def test_new_nema_symbols_have_no_millimetres():
    from panelcad.library.catalog import default_catalog
    from panelcad.library.panel import library_catalog_ready

    catalog = default_catalog()
    ids = [
        "nema.terminals.feed_through",
        "nema.operators.float_no",
        "nema.operators.float_nc",
        "nema.operators.level_no",
        "nema.operators.level_nc",
        "nema.operators.pressure_no",
        "nema.operators.pressure_nc",
        "nema.operators.temperature_no",
        "nema.operators.temperature_nc",
        "nema.operators.flow_no",
        "nema.operators.flow_nc",
        "nema.operators.foot_no",
        "nema.operators.foot_nc",
        "nema.timers.off_delay",
        "nema.timers.contact_notc",
        "nema.timers.contact_ncto",
        "nema.timers.contact_noto",
        "nema.timers.contact_nctc",
        "nema.operators.horn",
        "nema.relays.alternating",
        "nema.relays.cr",
    ]
    for library_id in ids:
        device = catalog.get(library_id)
        assert device.pins, library_id
        assert device.primitives, library_id
        assert float(device.panel.width_mm or 0) == 0, library_id
        assert float(device.panel.height_mm or 0) == 0, library_id
        assert not library_catalog_ready(device), library_id


def test_feedthrough_keeps_one_wire_number():
    from panelcad.api.session import Session
    from panelcad.core.wire_numbering import assign_wire_numbers

    session = Session.create("Numbers")
    session.place("nema.operators.pb_no", ref="SS1", x=40, y=40)
    session.place("nema.terminals.feed_through", ref="TB1-1", x=80, y=40)
    session.place("nema.operators.limit_nc", ref="LS1", x=120, y=40)
    session.wire("SS1.3", "TB1-1.A")
    session.wire("TB1-1.B", "LS1.1")
    assign_wire_numbers(session.project, session.catalog, mode="full", overwrite=True)
    numbers = {wire.number for wire in session.sheet().wires}
    assert len(numbers) == 1

    session.place("nema.terminals.pe", ref="GND1", x=80, y=80)
    session.place("nema.operators.pb_no", ref="PB1", x=40, y=80)
    session.place("nema.operators.pb_nc", ref="PB2", x=120, y=80)
    session.wire("PB1.3", "GND1.A")
    session.wire("GND1.B", "PB2.1")
    assign_wire_numbers(session.project, session.catalog, mode="full", overwrite=True)
    ground = [
        wire.number
        for wire in session.sheet().wires
        if "GND1" in wire.frm or "GND1" in wire.to
    ]
    assert len(set(ground)) == 2

    session.set_wire_numbering(split_feedthrough=True)
    assign_wire_numbers(session.project, session.catalog, mode="full", overwrite=True)
    through = [
        wire.number
        for wire in session.sheet().wires
        if "TB1-1" in wire.frm or "TB1-1" in wire.to
    ]
    assert len(set(through)) == 2


def test_rail_rename_updates_wires_in_one_undo(tmp_path: Path):
    from panelcad.api.session import Session

    session = Session.create("Rails")
    session.place("nema.contactors.coil", ref="K1", x=80, y=40)
    session.wire("L1", "K1.A1")
    session.wire("K1.A2", "L2")
    session.set_title_block(rail_hot="X1", rail_return="X2")
    ends = {(wire.frm, wire.to) for wire in session.sheet().wires}
    assert ("X1", "K1.A1") in ends
    assert ("K1.A2", "X2") in ends
    assert session.undo()
    ends = {(wire.frm, wire.to) for wire in session.sheet().wires}
    assert ("L1", "K1.A1") in ends
    assert ("K1.A2", "L2") in ends
    assert session.project.title_block.rail_hot == "L1"


def test_failed_pdf_leaves_no_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from panelcad.api.session import Session
    from panelcad.render import pdf as pdf_mod
    from panelcad.render import page as page_mod

    session = Session.create("PDF")
    session.place("nema.contactors.coil", ref="K1", x=80, y=40)
    dest = tmp_path / "sheet.pdf"

    def boom(*_args, **_kwargs):
        raise RuntimeError("pin ref must be Component.Pin, got 'L1'")

    monkeypatch.setattr(page_mod, "paint_project_on_device", boom)
    with pytest.raises(RuntimeError, match="Component.Pin"):
        pdf_mod.project_to_pdf(session.project, session.catalog, dest)
    assert not dest.exists()
    assert list(tmp_path.glob(".sheet.pdf.partial-*")) == []


def test_cli_wire_number_locks(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.api.session import Session
    from panelcad.cli.main import app

    session = Session.create("CLI")
    session.place("nema.contactors.coil", ref="K1", x=40, y=40)
    session.place("nema.operators.pb_no", ref="S1", x=80, y=40)
    path = tmp_path / "job.pcad.yaml"
    session.save(path)
    runner = CliRunner()
    result = runner.invoke(app, ["wire", str(path), "--from", "S1.3", "--to", "K1.A1", "--number", "104"])
    assert result.exit_code == 0, result.output
    opened = Session.open(path)
    wire = opened.sheet().wires[-1]
    assert wire.number == "104"
    assert wire.number_locked is True


def test_vendor_csv_title_line_and_clean_cli_error(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.api.session import Session
    from panelcad.cli.main import app

    session = Session.create("BOM")
    path = tmp_path / "job.pcad.yaml"
    session.save(path)
    bom = tmp_path / "vendor.csv"
    bom.write_text(
        "Acme job 14\nPart Number,Description,Qty,Manufacturer\nABC-1,Example relay,1,Example Maker\n",
        encoding="utf-8",
    )
    opened = Session.open(path)
    result = opened.import_bom(bom, place_missing=True)
    assert result["errors"] == []
    opened.save(path)

    bad = tmp_path / "bad.csv"
    bad.write_text("hello,world\n1,2\n", encoding="utf-8")
    runner = CliRunner()
    cli = runner.invoke(app, ["bom", "import", str(path), str(bad)])
    text = (cli.output or "") + (getattr(cli, "stderr", None) or "")
    assert cli.exit_code == 1
    assert "Traceback" not in text
    assert "Unrecognized CSV" in text


def test_import_recovery_words_and_problem_order():
    from panelcad.core.checks import Finding, _finding_rank
    from panelcad.importer.pipeline import describe_wire_recovery, page_kind_words

    quiet = describe_wire_recovery(devices=4, numbers_read=4, connected=4, unconnected=0)
    assert "Placed 4 devices" in quiet
    assert "Read 4 wire numbers" in quiet
    assert "Connected 4 wires" in quiet
    assert "left unconnected" in quiet
    assert "Problems" not in quiet
    missed = describe_wire_recovery(devices=2, numbers_read=10, connected=0, unconnected=10)
    assert "Problems" in missed
    assert page_kind_words("other") == ""
    assert page_kind_words("schematic_power") == "schematic power"

    rows = [
        Finding("missing_part_number", "warning", "no part", ref="K1"),
        Finding("print_wire", "warning", "wire 12 was not connected", ref="12"),
        Finding("unconnected_device", "warning", "open", ref="K1"),
        Finding("print_unplaced", "warning", "no symbol", ref="U1"),
    ]
    ordered = sorted(rows, key=_finding_rank)
    assert [item.check for item in ordered[:2]] == ["print_wire", "print_unplaced"]


def test_think_checkbox_is_only_for_ollama(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.import_wizard import ImportWizard
    from panelcad.gui.vision_settings import THINK_TOOLTIP, VisionSettingsDialog
    from panelcad.importer.vision_config import VisionSettings, save_settings

    QApplication.instance() or QApplication([])
    path = tmp_path / "vision.json"
    save_settings(VisionSettings(preset="grok", think=True, model="grok-4"), path)
    dialog = VisionSettingsDialog(path=path)
    assert dialog.think.isChecked() is True
    assert dialog.think.isEnabled() is False
    assert dialog.think.toolTip() == THINK_TOOLTIP
    dialog.preset.setCurrentIndex(dialog.preset.findData("ollama"))
    assert dialog.think.isEnabled() is True
    assert dialog.think.isChecked() is True
    dialog.preset.setCurrentIndex(dialog.preset.findData("none"))
    assert dialog.think.isEnabled() is False
    assert dialog.think.isChecked() is True
    dialog._save()
    from panelcad.importer.vision_config import load_settings

    assert load_settings(path).think is True
    dialog.close()

    wizard = ImportWizard()
    wizard.think.setChecked(True)
    wizard.vision.setCurrentIndex(wizard.vision.findData("grok"))
    assert wizard.think.isEnabled() is False
    assert wizard.think.isChecked() is True
    assert "Ollama" in wizard.think.toolTip()
    wizard.vision.setCurrentIndex(wizard.vision.findData("ollama"))
    assert wizard.think.isEnabled() is True
    assert wizard.think.isChecked() is True
    assert wizard.vision_settings().think is True
    wizard.close()


def test_help_covers_wire_numbers_and_import_recovery():
    from panelcad.guidance import HELP_TOPICS
    from panelcad.learn import EXTRA_TOPICS

    wires = HELP_TOPICS["wires"]["body"]
    assert "sheet/line" in wires
    assert "Set wire number" in wires
    assert "full renumber" in wires.lower() or "renumber keeps" in wires
    assert "--number" in wires
    body = EXTRA_TOPICS["print-import"]["body"]
    assert "left unconnected" in body
    assert "needs hand work" in body or "needs-info" in body
