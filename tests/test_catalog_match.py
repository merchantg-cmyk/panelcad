"""Catalog part versus the device on the drawing. Problems wording, no library ids."""

from panelcad import Session
from panelcad.core.checks import CHECK_IDS


def _rows(session: Session, check: str) -> list[dict[str, str]]:
    return [row for row in session.design_checks() if row["check"] == check]


def _no_library_ids(rows: list[dict[str, str]]) -> None:
    for row in rows:
        assert "nema." not in row["message"]
        assert "iec." not in row["message"]
        assert "library_id" not in row["message"]


def test_catalog_match_checks_are_registered():
    for name in (
        "catalog_poles",
        "catalog_current",
        "catalog_coil_voltage",
        "catalog_pilot_voltage",
        "catalog_secondary_voltage",
    ):
        assert name in CHECK_IDS


def test_breaker_part_poles_and_current_against_the_sized_device():
    mismatch = Session.create("Breaker")
    mismatch.place("nema.breakers.mcb_3p_32a", ref="CB1", x=20, y=20)
    mismatch.set_component_properties("CB1", {"In": "20A"})
    mismatch.assign_part("CB1", "1489-M1C010")
    poles = _rows(mismatch, "catalog_poles")
    current = _rows(mismatch, "catalog_current")
    assert poles and "3-pole" in poles[0]["message"] and "1-pole" in poles[0]["message"]
    assert "1489-M1C010" in poles[0]["message"]
    assert current and "20 A" in current[0]["message"] and "1 A" in current[0]["message"]
    _no_library_ids(poles + current)

    fit = Session.create("Breaker fit")
    fit.place("nema.breakers.mcb_3p_32a", ref="CB1", x=20, y=20)
    fit.set_component_properties("CB1", {"In": "20A"})
    fit.assign_part("CB1", "1489-M3C200")
    assert _rows(fit, "catalog_poles") == []
    assert _rows(fit, "catalog_current") == []


def test_current_uses_the_sized_circuit_not_the_symbol_frame():
    bare = Session.create("Frame")
    bare.place("nema.breakers.mcb_3p_32a", ref="CB1", x=20, y=20)
    bare.place("nema.contactors.ac3_32a", ref="M1", x=80, y=20)
    bare.assign_part("CB1", "1489-M3C160")
    bare.assign_part("M1", "100-C23D10")
    assert _rows(bare, "catalog_current") == []

    sized = Session.create("Sized")
    sized.place("nema.breakers.mcb_3p_32a", ref="CB1", x=20, y=20)
    sized.set_component_properties(
        "CB1", {"In": "32A", "part_number": "OLD", "catalog_key": "old", "ocpd_max_a": 20}
    )
    sized.assign_part("CB1", "1489-M3C200")
    assert _rows(sized, "catalog_current") == []
    assert sized.sheet().component("CB1").properties.get("device_amps") != 32

    low = Session.create("Motor")
    low.place("nema.contactors.ac3_9a", ref="M1", x=40, y=40)
    low.place("nema.loads.motor_3ph", ref="MTR1", x=40, y=120)
    low.set_component_properties("MTR1", {"FLA": "9A"})
    low.wire("M1.T1", "MTR1.T1")
    low.assign_part("M1", "XTCE007B10A")
    rows = _rows(low, "catalog_current")
    assert rows and "9 A" in rows[0]["message"] and "7 A" in rows[0]["message"]
    assert "32" not in rows[0]["message"]
    _no_library_ids(rows)


def test_fuse_holder_poles_against_a_one_pole_fuse():
    session = Session.create("Fuse")
    session.place("nema.protection.fuse_10a", ref="FU1", x=20, y=20)
    session.assign_part("FU1", "BH-0122")
    rows = _rows(session, "catalog_poles")
    assert rows
    assert "1-pole" in rows[0]["message"] and "3-pole" in rows[0]["message"]
    assert "BH-0122" in rows[0]["message"]
    _no_library_ids(rows)


def test_coil_voltage_against_the_circuit():
    wrong = Session.create("Coil")
    wrong.place("nema.contactors.coil", ref="CR1", x=40, y=40)
    wrong.wire("CR1.A1", "CR1.A2", net="120VAC")
    wrong.assign_part("CR1", "100-C09EJ10")
    rows = _rows(wrong, "catalog_coil_voltage")
    assert rows
    assert "120 V" in rows[0]["message"] and "24 V" in rows[0]["message"]
    assert "100-C09EJ10" in rows[0]["message"]
    _no_library_ids(rows)

    right = Session.create("Coil fit")
    right.place("nema.contactors.coil", ref="CR1", x=40, y=40)
    right.wire("CR1.A1", "CR1.A2", net="120VAC")
    right.assign_part("CR1", "100-C09D10")
    assert _rows(right, "catalog_coil_voltage") == []

    dc = Session.create("Coil dc")
    dc.place("iec.contactors.coil", ref="K1", x=40, y=40)
    dc.wire("K1.A1", "K1.A2", net="24VDC")
    dc.assign_part("K1", "100-C09EJ10")
    assert _rows(dc, "catalog_coil_voltage") == []


def test_pilot_light_voltage_against_the_circuit():
    wrong = Session.create("Lamp")
    wrong.set_title_block(drawing_style="ladder", rail_hot="120VAC", rail_return="N")
    wrong.place("nema.operators.pilot_light", ref="PL1", x=40, y=40)
    wrong.wire("120VAC", "PL1.X1")
    wrong.wire("PL1.X2", "N")
    wrong.assign_part("PL1", "800FD-P3N3R")
    rows = _rows(wrong, "catalog_pilot_voltage")
    assert rows
    assert "120 V" in rows[0]["message"] and "24 V" in rows[0]["message"]
    assert "pilot light" in rows[0]["message"]
    assert "800FD-P3N3R" in rows[0]["message"]
    _no_library_ids(rows)

    unwired = Session.create("Lamp library")
    unwired.place("nema.operators.pilot_light", ref="PL1", x=40, y=40)
    unwired.assign_part("PL1", "800FD-P3N3R")
    assert _rows(unwired, "catalog_pilot_voltage") == []

    match = Session.create("Lamp fit")
    match.place("iec.operators.pilot_light", ref="H1", x=40, y=40)
    match.wire("H1.X1", "H1.X2", net="24VDC")
    match.assign_part("H1", "800FD-P3N3R")
    assert _rows(match, "catalog_pilot_voltage") == []


def test_transformer_secondary_against_control_voltage():
    wrong = Session.create("CPT", sheet_id="control")
    wrong.set_title_block(
        standard="NEMA/ANSI",
        drawing_style="ladder",
        rail_hot="L1",
        rail_return="L2",
    )
    wrong.place("nema.power.control_transformer", ref="T1", x=40, y=30)
    wrong.place("nema.contactors.coil", ref="CR1", x=160, y=80)
    wrong.wire("T1.X1", "CR1.A1", net="120VAC")
    wrong.wire("CR1.A2", "L2")
    wrong.assign_part("T1", "9070T100D23")
    rows = _rows(wrong, "catalog_secondary_voltage")
    assert rows
    assert "24 V" in rows[0]["message"] and "120 V" in rows[0]["message"]
    assert "secondary" in rows[0]["message"] and "control circuit" in rows[0]["message"]
    assert "9070T100D23" in rows[0]["message"]
    _no_library_ids(rows)

    match = Session.create("CPT fit", sheet_id="control")
    match.place("nema.power.control_transformer", ref="T1", x=40, y=30)
    match.place("iec.contactors.coil", ref="K1", x=160, y=80)
    match.sheet().rail_hot = "24V"
    match.sheet().rail_return = "0V"
    match.wire("T1.X1", "K1.A1", net="24VDC")
    match.assign_part("T1", "9070T100D23")
    assert _rows(match, "catalog_secondary_voltage") == []


def test_unwired_coil_does_not_use_the_library_default():
    session = Session.create("Unwired coil")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    session.assign_part("K1", "GH15BN-3-10A")
    assert _rows(session, "catalog_coil_voltage") == []


def test_voltage_strings_cover_ranges_and_both_kinds():
    from panelcad.core.catalog_match import parse_voltage_options, voltage_fits

    both = parse_voltage_options("24 VAC/VDC")
    assert voltage_fits(both, (24, "ac"))
    assert voltage_fits(both, (24, "dc"))
    span = parse_voltage_options("24-60VAC/DC")
    assert voltage_fits(span, (24, "dc"))
    assert voltage_fits(span, (48, "ac"))
    assert not voltage_fits(span, (120, "ac"))
    wide = parse_voltage_options("100-240VAC")
    assert voltage_fits(wide, (120, "ac"))
    assert not voltage_fits(wide, (24, "ac"))
    narrow = parse_voltage_options("110-120VAC")
    assert voltage_fits(narrow, (110, "ac"))
    assert voltage_fits(narrow, (120, "ac"))
    assert voltage_fits(parse_voltage_options("120V 60Hz"), (120, "ac"))
    pair = parse_voltage_options("110VAC 50Hz / 120VAC 60Hz")
    assert voltage_fits(pair, (110, "ac"))
    assert voltage_fits(pair, (120, "ac"))
    assert not voltage_fits(pair, (24, "dc"))
    hertz = parse_voltage_options("24VAC 50/60Hz")
    assert voltage_fits(hertz, (24, "ac"))
    assert not voltage_fits(hertz, (50, "ac"))
    assert not voltage_fits(hertz, (60, "ac"))


def test_l1_l2_ladder_uses_agreed_catalog_coil_voltage():
    agreed = Session.create("Agreed", sheet_id="control")
    agreed.set_title_block(drawing_style="ladder", standard="NEMA/ANSI", rail_hot="L1", rail_return="L2")
    agreed.place("nema.contactors.coil", ref="CR1", x=40, y=40)
    agreed.place("nema.contactors.coil", ref="CR2", x=110, y=40)
    agreed.place("nema.operators.pilot_light", ref="PL1", x=180, y=40)
    agreed.place("nema.power.control_transformer", ref="T1", x=40, y=120)
    agreed.assign_part("CR1", "100-C09D10")
    agreed.assign_part("CR2", "GH15BN-3-10A")
    agreed.assign_part("PL1", "800FD-P3N3R")
    agreed.assign_part("T1", "9070T100D23")
    lights = _rows(agreed, "catalog_pilot_voltage")
    secondary = _rows(agreed, "catalog_secondary_voltage")
    assert lights and "24 V" in lights[0]["message"] and "120 V" in lights[0]["message"]
    assert secondary and "24 V" in secondary[0]["message"] and "120 V" in secondary[0]["message"]
    _no_library_ids(lights + secondary)

    bare = Session.create("Unassigned", sheet_id="control")
    bare.set_title_block(drawing_style="ladder", standard="NEMA/ANSI", rail_hot="L1", rail_return="L2")
    bare.place("nema.contactors.coil", ref="CR1", x=40, y=40)
    bare.place("nema.contactors.coil", ref="CR2", x=110, y=40)
    bare.place("nema.operators.pilot_light", ref="PL1", x=180, y=40)
    bare.place("nema.power.control_transformer", ref="T1", x=40, y=120)
    bare.assign_part("PL1", "800FD-P3N3R")
    bare.assign_part("T1", "9070T100D23")
    assert _rows(bare, "catalog_pilot_voltage") == []
    assert _rows(bare, "catalog_secondary_voltage") == []


def test_a_coil_range_does_not_invent_the_circuit_voltage():
    session = Session.create("Range", sheet_id="control")
    session.set_title_block(drawing_style="ladder", standard="NEMA/ANSI", rail_hot="L1", rail_return="L2")
    session.place("iec.contactors.coil", ref="K1", x=40, y=40)
    session.place("iec.contactors.coil", ref="K2", x=110, y=40)
    session.place("iec.operators.pilot_light", ref="PL1", x=180, y=40)
    session.place("iec.power.control_transformer", ref="T1", x=40, y=120)
    session.assign_part("K1", "LC1D09BNE")
    session.assign_part("K2", "LC1D09BNE")
    session.assign_part("PL1", "800FD-P3N3R")
    session.assign_part("T1", "9070T100D23")
    assert _rows(session, "catalog_pilot_voltage") == []
    assert _rows(session, "catalog_secondary_voltage") == []

    pointed = Session.create("Point and range", sheet_id="control")
    pointed.set_title_block(drawing_style="ladder", rail_hot="L1", rail_return="L2")
    pointed.place("iec.contactors.coil", ref="K1", x=40, y=40)
    pointed.place("iec.contactors.coil", ref="K2", x=110, y=40)
    pointed.place("iec.operators.pilot_light", ref="PL1", x=180, y=40)
    pointed.assign_part("K1", "3RT2015-1AP02")
    pointed.assign_part("K2", "GH15BN-3-10B")
    pointed.assign_part("PL1", "800FD-P3N3R")
    rows = _rows(pointed, "catalog_pilot_voltage")
    assert rows and "230 V" in rows[0]["message"]
    assert "240 V" not in rows[0]["message"]

    from panelcad.core.catalog_match import (
        _fmt_circuit,
        _part_fits_circuit,
        parse_voltage_options,
    )
    from panelcad.core.catalog_match import _agreed_catalog_voltage

    agreed = _agreed_catalog_voltage(session.project, session.catalog, [session.sheet()], skip_id="PL1")
    assert agreed is not None
    assert "24–60" in _fmt_circuit(agreed)
    assert "60 V" != _fmt_circuit(agreed)
    assert _part_fits_circuit(parse_voltage_options("24VAC/DC"), agreed)
    assert not _part_fits_circuit(parse_voltage_options("120VAC"), agreed)


def test_nominal_coil_band_matches_a_stated_voltage():
    """110/115/120 V and 220/230/240 V are one band. A 24 V light still is not."""

    def _coil(name: str, catalog: str, volts: float):
        session = Session.create(name, sheet_id="control")
        session.set_title_block(drawing_style="ladder", rail_hot="L1", rail_return="L2")
        session.place("iec.contactors.coil", ref="CR1", x=40, y=40)
        session.assign_part("CR1", catalog)
        session.set_control_voltage(volts)
        return _rows(session, "catalog_coil_voltage")

    assert _coil("AB 115", "100-C09D10", 115) == []
    assert _coil("Fuji 115", "SC-E03P-110VAC", 115) == []
    assert _coil("Fuji 120", "SC-E03P-110VAC", 120) == []
    assert _coil("Siemens 220", "3RT2015-1AP02", 220) == []
    assert _coil("Siemens 240", "3RT2015-1AP02", 240) == []

    rails = Session.create("Rails", sheet_id="control")
    rails.set_title_block(drawing_style="ladder", rail_hot="120VAC", rail_return="N")
    rails.place("iec.contactors.coil", ref="CR1", x=40, y=40)
    rails.assign_part("CR1", "SC-E03P-110VAC")
    rails.wire("120VAC", "CR1.A1")
    rails.wire("CR1.A2", "N")
    assert _rows(rails, "catalog_coil_voltage") == []

    wrong = _coil("24 on 120", "SC-E02P-24VAC", 120)
    assert wrong and "24 V" in wrong[0]["message"] and "120 V" in wrong[0]["message"]


def test_circuit_builder_and_yaml_drop_nonfinite_control_voltage(tmp_path):
    """insert_circuit and a saved .nan/.inf do not become the control voltage."""

    from panelcad.cli.agent import Agent

    for bad in ("nan", "inf"):
        agent = Agent()
        assert agent.handle({"op": "create_project", "name": f"Nonfinite {bad}"})["ok"]
        result = agent.handle(
            {
                "op": "insert_circuit",
                "kind": "dol",
                "control": "local",
                "hp": 5,
                "voltage": 480,
                "control_voltage": bad,
            }
        )
        assert result["ok"], result
        assert agent.session is not None
        assert agent.session.project.control_voltage is None
        assert all(sheet.control_voltage is None for sheet in agent.session.project.sheets)

    direct = Session.create("Direct inf")
    direct.insert_circuit(
        kind="dol", control="local", hp=5, voltage=480, control_voltage=float("nan")
    )
    assert direct.project.control_voltage is None
    assert all(sheet.control_voltage is None for sheet in direct.project.sheets)

    path = tmp_path / "nonfinite.pcad.yaml"
    for token in (".nan", ".inf", "-.inf"):
        path.write_text(
            "format: panelcad\n"
            "version: '0.1'\n"
            "name: Nonfinite\n"
            f"control_voltage: {token}\n"
            "sheets:\n"
            "- id: control\n"
            "  kind: schematic\n"
            f"  control_voltage: {token}\n",
            encoding="utf-8",
        )
        opened = Session.open(path)
        assert opened.project.control_voltage is None, token
        assert opened.sheet("control").control_voltage is None, token

    path.write_text(
        "format: panelcad\n"
        "version: '0.1'\n"
        "name: Finite\n"
        "control_voltage: 120\n"
        "sheets:\n"
        "- id: control\n"
        "  kind: schematic\n"
        "  control_voltage: 24\n",
        encoding="utf-8",
    )
    kept = Session.open(path)
    assert kept.project.control_voltage == 120
    assert kept.sheet("control").control_voltage == 24


def test_nonfinite_control_voltage_is_rejected_without_an_undo_step():
    session = Session.create("Finite")
    session.set_control_voltage("24")
    for bad in ("nan", "inf", "+inf", "-inf"):
        try:
            session.set_control_voltage(bad)
            raised = False
        except ValueError:
            raised = True
        assert raised, bad
        assert session.project.control_voltage == 24
        assert session.sheet().control_voltage == 24
    assert session.undo()
    assert session.project.control_voltage is None


def test_power_sheet_transformer_uses_coils_on_other_sheets():
    caught = Session.create("Power", sheet_id="main")
    caught.set_title_block(drawing_style="ladder", standard="NEMA/ANSI", rail_hot="L1", rail_return="L2")
    caught.place("nema.contactors.ac3_9a", ref="M1", x=40, y=40)
    caught.place("nema.power.control_transformer", ref="T1", x=140, y=40)
    caught.add_sheet("control-ladder", kind="schematic", title="Control")
    caught.place("nema.contactors.coil", ref="CR1", x=40, y=40, sheet_id="control-ladder")
    caught.assign_part("M1", "100-C09D10")
    caught.assign_part("CR1", "100-C09D10")
    caught.assign_part("T1", "9070T100D23")
    rows = _rows(caught, "catalog_secondary_voltage")
    assert rows and rows[0]["ref"] == "T1"
    assert "24 V" in rows[0]["message"] and "120 V" in rows[0]["message"]

    quiet = Session.create("Power quiet", sheet_id="main")
    quiet.place("nema.contactors.ac3_9a", ref="M1", x=40, y=40)
    quiet.place("nema.power.control_transformer", ref="T1", x=140, y=40)
    quiet.add_sheet("control-ladder", kind="schematic")
    quiet.place("nema.contactors.coil", ref="CR1", x=40, y=40, sheet_id="control-ladder")
    quiet.assign_part("T1", "9070T100D23")
    assert _rows(quiet, "catalog_secondary_voltage") == []


def test_convert_standard_does_not_store_a_guessed_voltage(tmp_path):
    session = Session.create("24 VAC", sheet_id="control")
    session.set_title_block(drawing_style="ladder", standard="NEMA/ANSI", rail_hot="L1", rail_return="L2")
    session.place("nema.contactors.coil", ref="K1", x=40, y=40)
    session.place("nema.contactors.coil", ref="K2", x=110, y=40)
    session.place("nema.operators.pilot_light", ref="H1", x=180, y=40)
    session.place("nema.power.control_transformer", ref="T1", x=40, y=120)
    session.assign_part("K1", "SC-E02P-24VAC")
    session.assign_part("K2", "SC-E02P-24VAC")
    session.assign_part("H1", "800FD-P3N3R")
    session.assign_part("T1", "9070T100D23")
    voltage_checks = {
        "catalog_coil_voltage",
        "catalog_pilot_voltage",
        "catalog_secondary_voltage",
    }
    assert not any(row["check"] in voltage_checks for row in session.design_checks())
    session.convert_standard(
        "IEC",
        out=tmp_path / "24-VAC-iec.pcad.yaml",
        workspace=tmp_path / "convert.json",
    )
    assert session.project.control_voltage is None
    assert all(sheet.control_voltage is None for sheet in session.project.sheets)
    hit = [row for row in session.design_checks() if row["check"] in voltage_checks]
    assert hit == [], hit


def test_control_voltage_edit_matches_the_checks_and_undoes():
    session = Session.create("Main", sheet_id="main")
    session.add_sheet("motor-control", kind="schematic")
    session.project.control_voltage = 120
    session.sheet("motor-control").control_voltage = 120
    session.set_active_sheet("main")
    session.set_control_voltage("24")
    assert session.project.control_voltage == 24
    assert session.sheet("main").control_voltage == 24
    assert session.sheet("motor-control").control_voltage == 24
    session.place("nema.operators.pilot_light", ref="PL1", x=40, y=40, sheet_id="motor-control")
    session.assign_part("PL1", "800FD-P3N3R")
    assert _rows(session, "catalog_pilot_voltage") == []
    try:
        session.set_control_voltage("abc")
        raised = False
    except ValueError:
        raised = True
    assert raised
    assert session.project.control_voltage == 24
    assert session.sheet("motor-control").control_voltage == 24

    fresh = Session.create("Undo")
    fresh.project.control_voltage = 120
    fresh.sheet().control_voltage = 120
    fresh.set_control_voltage("24")
    assert fresh.project.control_voltage == 24
    assert fresh.undo()
    assert fresh.project.control_voltage == 120
    assert fresh.sheet().control_voltage == 120


def test_circuit_builder_saves_the_control_voltage(tmp_path):
    session = Session.create("Builder")
    result = session.insert_circuit(
        kind="dol", control="local", hp=5, voltage=480, control_voltage=120
    )
    assert session.project.control_voltage == 120
    control = session.sheet(result["control_sheet"])
    assert control.control_voltage == 120
    path = tmp_path / "control.pcad.yaml"
    session.save(path)
    loaded = Session.open(path)
    assert loaded.project.control_voltage == 120
    assert loaded.sheet(result["control_sheet"]).control_voltage == 120
    loaded.place("nema.operators.pilot_light", ref="PL9", x=20, y=20, sheet_id=result["control_sheet"])
    loaded.assign_part("PL9", "800FD-P3N3R", sheet_id=result["control_sheet"])
    rows = _rows(loaded, "catalog_pilot_voltage")
    assert any(row["ref"] == "PL9" and "24 V" in row["message"] for row in rows)


def test_shipped_dol_has_no_catalog_false_findings():
    from pathlib import Path

    from panelcad.examples.dol import build_dol
    from panelcad.examples.sample_catalog import build_sample, sample_yaml_path

    false = {
        "catalog_current",
        "catalog_coil_voltage",
        "catalog_pilot_voltage",
        "catalog_secondary_voltage",
        "catalog_poles",
    }
    sessions = [build_dol(), build_sample()]
    saved = sample_yaml_path()
    if saved.is_file():
        sessions.append(Session.open(saved))
    elif Path("examples/sample_catalog.pcad.yaml").is_file():
        sessions.append(Session.open("examples/sample_catalog.pcad.yaml"))
    for session in sessions:
        hit = [row for row in session.design_checks() if row["check"] in false]
        assert hit == [], hit
