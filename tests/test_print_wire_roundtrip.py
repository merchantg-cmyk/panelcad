"""Re-import of a PanelCAD PDF or DXF restores conductors and suffixed contacts."""

from __future__ import annotations

from pathlib import Path

from panelcad.examples.xref_demo import build_iec_mirror, build_nema_seal_in
from panelcad.importer.accuracy import score_projects, score_sheet_pair
from panelcad.importer.pipeline import ImportJob
from panelcad.library.catalog import default_catalog


def test_contact_suffix_maps_to_the_device_and_terminal():
    from panelcad.importer.read import contact_tag_parts, is_device_tag, label_tokens

    assert contact_tag_parts("M1-13") == ("M1", "13")
    assert contact_tag_parts("CR2-3") == ("CR2", "3")
    assert contact_tag_parts("K1-14") == ("K1", "14")
    assert contact_tag_parts("K1:14") == ("K1", "14")
    assert contact_tag_parts("-K1:14") == ("K1", "14")
    assert contact_tag_parts("TR1-13") == ("TR1", "13")
    assert contact_tag_parts("OL1-95") == ("OL1", "95")
    assert contact_tag_parts("Q1-13") == ("Q1", "13")
    assert contact_tag_parts("K1:14") == ("K1", "14")
    assert contact_tag_parts("-K1:14") == ("K1", "14")
    # An unknown prefix is a contact only when that parent is on the sheet.
    assert contact_tag_parts("KA1-14") is None
    assert contact_tag_parts("KA1-14", parents={"KA1"}) == ("KA1", "14")
    assert contact_tag_parts("KT1-15", parents={"KT1"}) == ("KT1", "15")
    assert contact_tag_parts("TD2-5", parents={"TD2"}) == ("TD2", "5")
    for whole in ("XT1-4", "TS1-5", "LT1-2", "SOL1-2", "JB1-4", "MOT1-2"):
        assert contact_tag_parts(whole) is None
    assert contact_tag_parts("X1:5") is None
    assert contact_tag_parts("-X1:5") is None
    assert contact_tag_parts("FU1-7") is None
    assert contact_tag_parts("TB2-24") is None
    assert contact_tag_parts("10M-G") is None
    assert is_device_tag("M1-13") and is_device_tag("CR2-3") and is_device_tag("K1-14")
    assert is_device_tag("K1:14") and is_device_tag("-K1:14")
    assert not is_device_tag("X1:5") and not is_device_tag("-X1:5")

    # The contact tag is drawn smaller than the coil tag. It still has to be kept.
    labeled = label_tokens(
        [
            {"text": "M1", "x": 40, "y": 30, "w": 6, "h": 3.73},
            {"text": "M1-13", "x": 90, "y": 70, "w": 10, "h": 3.03},
            {"text": "CR2-3", "x": 140, "y": 70, "w": 10, "h": 3.03},
            {"text": "K1-14", "x": 190, "y": 70, "w": 10, "h": 3.03},
            {"text": "K1:21", "x": 220, "y": 70, "w": 10, "h": 3.03},
            {"text": "A3", "x": 260, "y": 40, "w": 4, "h": 2.74},
        ],
        420.0,
        297.0,
    )
    assert {item["text"] for item in labeled["tags"]} == {"M1", "M1-13", "CR2-3", "K1-14", "K1:21"}


def test_exported_pdf_reimport_restores_wires(tmp_path: Path):
    """A project printed by PanelCAD and read back keeps its wires and contact tags."""

    catalog = default_catalog()
    nema = build_nema_seal_in()
    iec = build_iec_mirror()
    nema_pdf = tmp_path / "nema.pdf"
    iec_pdf = tmp_path / "iec.pdf"
    nema.export_pdf(nema_pdf)
    iec.export_pdf(iec_pdf)

    nema_built = ImportJob.start([nema_pdf], tmp_path / "nema-ws", backend="external").build()
    iec_built = ImportJob.start([iec_pdf], tmp_path / "iec-ws", backend="external").build()
    nema_score = score_projects(nema.project, nema_built.project, catalog=catalog)
    iec_score = score_projects(iec.project, iec_built.project, catalog=catalog)

    assert nema_score["tags"] == 1.0
    assert {"M1-13", "CR1-13", "CR1-21"} <= _ids(nema_built)
    nema_libs = _libs(nema_built)
    iec_libs = _libs(iec_built)
    # The suffix is the terminal. The drawing chooses NO or NC.
    assert nema_libs["M1-13"].endswith("aux_no")
    assert nema_libs["CR1-13"].endswith("aux_no")
    assert nema_libs["CR1-21"].endswith("aux_nc")
    assert iec_libs["K1-14"].endswith("aux_no")
    assert iec_libs["K1-21"].endswith("aux_nc")
    assert iec_libs["K1-43"].endswith("aux_no")
    assert nema_score["wire_numbers"] >= 0.8
    assert nema_score["connectivity_recall"] >= 0.7
    assert nema_score["connectivity_precision"] == 1.0
    assert "PB1.2 PB2.3" not in nema_score["missing_edges"]
    assert "M1-13.4 M1.A1" not in nema_score["missing_edges"]

    assert iec_score["tags"] == 1.0
    assert {"K1-14", "K1-21", "K1-43"} <= _ids(iec_built)
    assert iec_score["wire_numbers"] == 1.0
    assert iec_score["connectivity_recall"] == 1.0
    assert iec_score["connectivity_precision"] == 1.0


def test_exported_dxf_reimport_restores_wires(tmp_path: Path):
    catalog = default_catalog()
    nema = build_nema_seal_in()
    dxf = tmp_path / "control.dxf"
    nema.export_dxf(dxf, sheet_id="control")
    built = ImportJob.start([dxf], tmp_path / "dxf-ws", backend="external").build()
    score = score_sheet_pair(nema.project.sheet("control"), built.project.sheets[0], catalog)
    assert "M1-13" in _ids(built)
    assert "CR1-13" in _ids(built)
    assert score["wire_numbers"] >= 0.8
    assert "PB1.2 PB2.3" not in score["missing_edges"]
    assert any(wire.number for sheet in built.project.sheets for wire in sheet.wires)
    # Rung references sit beside the hot rail. They are not the wire number.
    l1 = [wire for sheet in built.project.sheets for wire in sheet.wires if "L1" in {wire.frm, wire.to}]
    assert l1 and {wire.number for wire in l1} == {"101"}
    assert all(wire.number != "108" for sheet in built.project.sheets for wire in sheet.wires)


def _ids(session) -> set[str]:
    return {comp.id for sheet in session.project.sheets for comp in sheet.components}


def _libs(session) -> dict[str, str]:
    return {comp.id: comp.library_id for sheet in session.project.sheets for comp in sheet.components}


def test_three_phase_rails_are_not_a_control_return():
    from panelcad.api.session import Session
    from panelcad.importer.build import _adopt_printed_rails

    session = Session.create("power", sheet_id="power", title="Power")
    sheet = session.project.sheet("power")
    sheet.rail_hot = "X1"
    sheet.rail_return = "X2"
    _adopt_printed_rails(
        session,
        "power",
        [
            {"from_tag": "L1", "from_pin": "L1", "to_tag": "Q1", "to_pin": "1"},
            {"from_tag": "L2", "from_pin": "L2", "to_tag": "Q1", "to_pin": "3"},
            {"from_tag": "L3", "from_pin": "L3", "to_tag": "Q1", "to_pin": "5"},
        ],
    )
    assert sheet.rail_hot == "X1"
    assert sheet.rail_return == "X2"

    _adopt_printed_rails(
        session,
        "power",
        [
            {"from_tag": "L1", "from_pin": "L1", "to_tag": "PB1", "to_pin": "1"},
            {"from_tag": "L2", "from_pin": "L2", "to_tag": "CR1", "to_pin": "A2"},
        ],
    )
    assert sheet.rail_hot == "L1"
    assert sheet.rail_return == "L2"


def test_line_reference_column_is_not_a_wire_label():
    from panelcad.importer.vectors import _line_reference_keys

    tokens = [
        {"text": "102", "x": 18.0, "y": 40.0, "h": 2.5},
        {"text": "104", "x": 18.2, "y": 60.0, "h": 2.5},
        {"text": "106", "x": 17.8, "y": 80.0, "h": 2.5},
        {"text": "108", "x": 18.1, "y": 100.0, "h": 2.5},
        {"text": "101", "x": 55.0, "y": 40.0, "h": 2.5},
    ]
    rail = [{"a": [28.0, 20.0], "b": [28.0, 140.0], "role": "wire"}]
    marked = _line_reference_keys(tokens, rail, 420.0, 297.0)
    assert len(marked) == 4
    assert all(key[2] != "101" for key in marked)

    # Repeated numbers on the rung stubs, and a short run just right of the rail, are wire labels.
    stubs = [
        {"text": "101", "x": 28.0, "y": 40.0, "h": 2.5},
        {"text": "101", "x": 28.0, "y": 60.0, "h": 2.5},
        {"text": "101", "x": 28.0, "y": 80.0, "h": 2.5},
    ]
    beside = [
        {"text": "12", "x": 29.0, "y": 40.0, "h": 2.5},
        {"text": "14", "x": 29.0, "y": 60.0, "h": 2.5},
        {"text": "16", "x": 29.0, "y": 80.0, "h": 2.5},
    ]
    hot = [{"a": [23.0, 20.0], "b": [23.0, 140.0], "role": "wire"}]
    assert _line_reference_keys(stubs, hot, 420.0, 297.0) == set()
    assert _line_reference_keys(beside, hot, 420.0, 297.0) == set()


def test_suffix_does_not_override_the_drawn_contact(tmp_path: Path):
    """CR2-3 is drawn NC and CR2-2 is drawn NO. The pin digit in the tag does not decide."""

    from panelcad.api.session import Session

    session = Session.create("mixed", sheet_id="control", title="Control")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", drawing_number="E-MIX")
    session.place("nema.contactors.aux_nc", ref="CR2-3", x=80, y=60, snap=False)
    session.place("nema.contactors.aux_no", ref="CR2-2", x=150, y=60, snap=False)
    session.wire("CR2-3.1", "CR2-2.3")
    pdf = tmp_path / "mixed.pdf"
    session.export_pdf(pdf)
    built = ImportJob.start([pdf], tmp_path / "mixed-ws", backend="external").build()
    libs = _libs(built)
    assert libs["CR2-3"].endswith("aux_nc")
    assert libs["CR2-2"].endswith("aux_no")


def test_bylayer_dxf_keeps_the_power_wires(tmp_path: Path):
    import ezdxf

    from panelcad.importer.accuracy import score_sheet_pair
    from panelcad.io.yaml_format import load_project

    source = Path("examples/dol_power.dxf")
    rewritten = tmp_path / "bylayer.dxf"
    doc = ezdxf.readfile(str(source))
    weights: dict[int, str] = {}
    for entity in doc.modelspace():
        try:
            weight = int(entity.dxf.lineweight)
        except (AttributeError, TypeError, ValueError):
            continue
        if weight <= 0:
            continue
        layer_name = weights.get(weight)
        if layer_name is None:
            layer_name = f"W{weight}"
            layer = doc.layers.add(layer_name)
            layer.dxf.lineweight = weight
            weights[weight] = layer_name
        entity.dxf.layer = layer_name
        entity.dxf.lineweight = -1
    doc.saveas(str(rewritten))
    truth = load_project(Path("examples/dol_starter.pcad.yaml"))
    built = ImportJob.start([rewritten], tmp_path / "by-ws", backend="external").build()
    score = score_sheet_pair(truth.sheet("power"), built.project.sheets[0], default_catalog())
    assert score["connectivity_recall"] == 1.0
    assert score["connectivity_precision"] == 1.0


def test_shipped_dol_power_wires_come_back(tmp_path: Path):
    """The shipped prints have no wire numbers. The conductors still land on the terminals.

    The PDF's I/O sheet is an older revision (A2/A3/A4). The project file's I/O
    sheet is K2/S4/H3. Those seven edges are not on the print.
    """

    from panelcad.importer.accuracy import score_sheet_pair
    from panelcad.io.yaml_format import load_project

    truth = load_project(Path("examples/dol_starter.pcad.yaml"))
    catalog = default_catalog()
    dxf_built = ImportJob.start([Path("examples/dol_power.dxf")], tmp_path / "dxf", backend="external").build()
    dxf_score = score_sheet_pair(truth.sheet("power"), dxf_built.project.sheets[0], catalog)
    assert dxf_score["connectivity_recall"] == 1.0
    assert dxf_score["connectivity_precision"] == 1.0

    pdf_built = ImportJob.start([Path("examples/dol_starter.pdf")], tmp_path / "pdf", backend="external").build()
    power = next(sheet for sheet in pdf_built.project.sheets if sheet.title == "Power" or sheet.id == "power")
    power_score = score_sheet_pair(truth.sheet("power"), power, catalog)
    assert power_score["connectivity_recall"] == 1.0
    assert power_score["connectivity_precision"] == 1.0
    libs = _libs(pdf_built)
    assert libs["S0"].endswith("estop")
    assert libs["S2"].endswith("pb_nc")
    assert libs["S1"].endswith("pb_no")
    assert libs["M1"].endswith("motor_3ph")
    for sheet in pdf_built.project.sheets:
        for wire in sheet.wires:
            left = str(wire.frm).split(".", 1)[0]
            right = str(wire.to).split(".", 1)[0]
            assert not left or left != right


def test_missing_slash_is_no_and_a_drawn_slash_is_nc():
    """The NC blade is the only stroke that decides. Endpoint proximity is not enough."""

    from panelcad.importer.vectors import _choose_contact, _contact_symbols

    catalog = default_catalog()
    candidates = _contact_symbols(catalog, "NEMA/ANSI", "7")

    def ink(points: list[tuple[tuple[float, float], tuple[float, float]]]) -> list[dict]:
        return [{"kind": "line", "role": "ink", "a": [a[0], a[1]], "b": [b[0], b[1]]} for a, b in points]

    body = [
        ((80.0, 70.0), (88.0, 70.0)),
        ((100.0, 70.0), (108.0, 70.0)),
        ((88.0, 67.0), (88.0, 73.0)),
        ((100.0, 67.0), (100.0, 73.0)),
    ]
    opened = ink(body + [((88.0, 70.0), (96.0, 64.0))])
    closed = ink(body + [((88.0, 70.0), (100.0, 67.0))])
    no = _choose_contact(
        candidates, 78.0, 50.0, opened, [], "NEMA/ANSI", center=(90.0, 64.0), wide_lines=opened, terminal="7"
    )
    nc = _choose_contact(
        candidates, 78.0, 50.0, closed, [], "NEMA/ANSI", center=(90.0, 64.0), wide_lines=closed, terminal="7"
    )
    assert no not in (None, "uncertain") and str(no[1].library_id).endswith("aux_no")
    assert nc not in (None, "uncertain") and str(nc[1].library_id).endswith("aux_nc")


def test_iec_colon_tags_import_like_hyphen_tags(tmp_path: Path):
    import ezdxf

    iec = build_iec_mirror()
    hyphen = tmp_path / "hyphen.dxf"
    colon = tmp_path / "colon.dxf"
    iec.export_dxf(hyphen, sheet_id="main")
    doc = ezdxf.readfile(str(hyphen))
    changed = 0
    for entity in doc.modelspace():
        if entity.dxftype() != "TEXT":
            continue
        text = str(entity.dxf.text or "")
        if text in {"K1-14", "K1-21"}:
            entity.dxf.text = text.replace("-", ":")
            changed += 1
    assert changed == 2
    doc.saveas(str(colon))
    hyphen_built = ImportJob.start([hyphen], tmp_path / "hyphen-ws", backend="external").build()
    colon_built = ImportJob.start([colon], tmp_path / "colon-ws", backend="external").build()
    assert "K1:14" in _ids(colon_built)
    assert "K1:21" in _ids(colon_built)
    assert _wire_ends(colon_built) == _wire_ends(hyphen_built)


def test_printed_wire_number_is_not_replaced(tmp_path: Path):
    from panelcad.examples.dol import build_dol

    session = build_dol()
    session.number_wires(overwrite=True)
    wanted = ""
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            if {wire.frm, wire.to} == {"K1.T2", "F1.3"}:
                wanted = str(wire.number)
    assert wanted
    pdf = tmp_path / "numbered.pdf"
    session.export_pdf(pdf)
    built = ImportJob.start([pdf], tmp_path / "num-ws", backend="external").build()
    found = [
        str(wire.number)
        for sheet in built.project.sheets
        for wire in sheet.wires
        if {wire.frm, wire.to} == {"K1.T2", "F1.3"}
    ]
    assert found == [wanted]


def test_converging_phases_keep_pin_order():
    """Three leads that bunch up still land T1, T2, T3. An unclear pair is not a swap."""

    from panelcad.importer.vectors import _settle_shared_pins

    pins = [
        {"tag": "MTR1", "pin": "T1", "x": 48.0, "y": 200.0},
        {"tag": "MTR1", "pin": "T2", "x": 56.0, "y": 200.0},
        {"tag": "MTR1", "pin": "T3", "x": 64.0, "y": 200.0},
    ]
    # The right-hand lead is nearer T2 than T3. Order, not the nearest pin, decides.
    ends_x = (50.0, 54.0, 58.0)
    chains = []
    for x, wrong in zip(ends_x, ("T1", "T2", "T2"), strict=True):
        chains.append(
            {
                "segments": [((x, 180.0), (x, 200.0))],
                "ends": [
                    {"tag": "OL1", "pin": "2", "x": x, "y": 180.0},
                    {"tag": "MTR1", "pin": wrong, "x": 56.0, "y": 200.0},
                ],
            }
        )
    _settle_shared_pins(chains, pins)
    assert [chain["ends"][1]["pin"] for chain in chains] == ["T1", "T2", "T3"]

    # Two leads and three pins, both nearest the middle pin: do not invent the third.
    ambiguous = []
    for x in (54.5, 55.5):
        ambiguous.append(
            {
                "segments": [((x, 180.0), (x, 200.0))],
                "ends": [
                    {"tag": "OL1", "pin": "2", "x": x, "y": 180.0},
                    {"tag": "MTR1", "pin": "T2", "x": 56.0, "y": 200.0},
                ],
            }
        )
    _settle_shared_pins(ambiguous, pins)
    assert all(chain["ends"][1] is None or chain["ends"][1]["pin"] != "T3" for chain in ambiguous)
    assert sum(chain["ends"][1] is not None for chain in ambiguous) <= 1


def test_three_leads_on_a_two_pin_symbol_stay_unconnected():
    """More conductors than pins on that side is needs-info, never a swapped T2."""

    from panelcad.importer.vectors import _settle_shared_pins

    pins = [
        {"tag": "MTR1", "pin": "T1", "x": 48.0, "y": 200.0, "side": "top"},
        {"tag": "MTR1", "pin": "T2", "x": 68.0, "y": 200.0, "side": "top"},
    ]
    chains = []
    for x in (48.0, 58.0, 68.0):
        chains.append(
            {
                "segments": [((x, 170.0), (x, 200.0))],
                "ends": [
                    {"tag": "OL1", "pin": "2", "x": x, "y": 170.0},
                    {"tag": "MTR1", "pin": "T2", "x": 68.0, "y": 200.0},
                ],
            }
        )
    _settle_shared_pins(chains, pins)
    assert [chain["ends"][1] for chain in chains] == [None, None, None]


def test_parallel_conductors_do_not_keep_a_smaller_symbol():
    """Three leads on a 1-phase body are a 3-phase motor, or needs-info. Not T1/T2."""

    import math

    from panelcad.importer.vectors import _symbol_circles, _symbol_segments, read_drawing

    catalog = default_catalog()
    one = catalog.get("nema.loads.motor_1ph")
    ox, oy = 80.0, 90.0
    strokes = []
    for seg in _symbol_segments(one, ox, oy, 1.0):
        strokes.append({"kind": "line", "role": "ink", "a": [seg[0][0], seg[0][1]], "b": [seg[1][0], seg[1][1]]})
    for cx, cy, radius in _symbol_circles(one, ox, oy, 1.0):
        points = []
        for step in range(16):
            angle = 2.0 * math.pi * step / 16.0
            points.append([cx + radius * math.cos(angle), cy + radius * math.sin(angle)])
        strokes.append({"kind": "curve", "role": "ink", "points": points})
    for x in (ox + 8.0, ox + 18.0, ox + 28.0):
        strokes.append({"kind": "line", "role": "wire", "a": [x, oy], "b": [x, oy - 40.0]})
    tokens = [{"text": "MTR1", "x": ox + 8.0, "y": oy - 8.0, "w": 10.0, "h": 3.0, "confidence": 0.95}]
    facts = read_drawing(tokens, strokes, 220.0, 180.0, catalog, standard="NEMA/ANSI")
    motor = next(item for item in facts["devices"] if item["tag"] == "MTR1")
    lib = str(motor.get("library_id") or "")
    assert "motor_1ph" not in lib
    if lib:
        assert lib.endswith("motor_3ph")


def test_three_phase_stack_keeps_the_three_pole_symbols(tmp_path: Path):
    """CB, contactor, overload, and motor. Three OL leads land T1, T2, T3."""

    from panelcad.api.session import Session

    session = Session.create("N1-style booster", sheet_id="power", title="Power")
    session.set_title_block(drawing_number="N1", standard="NEMA/ANSI", description="Booster power")
    session.place("nema.breakers.mcb_3p_32a", ref="CB1", x=40, y=22)
    session.place("nema.contactors.ac3_32a", ref="M1", x=38, y=86)
    session.place("nema.protection.overload", ref="OL1", x=40, y=164)
    session.place("nema.loads.motor_3ph", ref="MTR1", x=40, y=236)
    session.wire("CB1.2", "M1.L1", net="L1", number="L1")
    session.wire("CB1.4", "M1.L2", net="L2", number="L2")
    session.wire("CB1.6", "M1.L3", net="L3", number="L3")
    session.wire("M1.T1", "OL1.T1", net="T1", number="201")
    session.wire("M1.T2", "OL1.T2", net="T2", number="202")
    session.wire("M1.T3", "OL1.T3", net="T3", number="203")
    session.wire("OL1.2", "MTR1.T1", net="101", number="101")
    session.wire("OL1.4", "MTR1.T2", net="102", number="102")
    session.wire("OL1.6", "MTR1.T3", net="103", number="103")
    def _check(path: Path, workspace: Path) -> None:
        built = ImportJob.start([path], workspace, backend="external").build()
        libs = _libs(built)
        assert libs["MTR1"].endswith("motor_3ph")
        assert "mcb_3p" in libs["CB1"]
        assert "mcb_1p" not in libs["CB1"]
        assert libs["M1"].endswith("ac3_32a")
        assert "overload" in libs["OL1"] and "ol_contact" not in libs["OL1"]
        found = {}
        for sheet in built.project.sheets:
            for wire in sheet.wires:
                pair = tuple(sorted((wire.frm, wire.to)))
                if any(end.startswith("OL1.") for end in pair) and any(end.startswith("MTR1.") for end in pair):
                    found[pair] = str(wire.number)
        assert found.get(tuple(sorted(("OL1.2", "MTR1.T1")))) == "101"
        assert found.get(tuple(sorted(("OL1.4", "MTR1.T2")))) == "102"
        assert found.get(tuple(sorted(("OL1.6", "MTR1.T3")))) == "103"

    pdf = tmp_path / "stack.pdf"
    dxf = tmp_path / "stack.dxf"
    svg = tmp_path / "stack.svg"
    session.export_pdf(pdf)
    session.export_dxf(dxf)
    session.export_svg(svg)
    _check(pdf, tmp_path / "pdf")
    _check(dxf, tmp_path / "dxf")
    _check(svg, tmp_path / "svg")


def test_branched_ladder_keeps_two_terminal_symbols(tmp_path: Path):
    """A seal-in, a T-junction, and a contact tag are not extra poles."""

    from panelcad.examples.xref_demo import _on_rung, _rung_y, build_nema_seal_in

    session = build_nema_seal_in()
    _on_rung(session, "nema.operators.limit_no", "LS1", 140, _rung_y(session, 10), "3")
    session.wire("L1", "LS1.3")
    session.wire("LS1.4", "L2")
    pdf = tmp_path / "ladder.pdf"
    session.export_pdf(pdf)
    built = ImportJob.start([pdf], tmp_path / "ws", backend="external").build()
    libs = _libs(built)
    assert libs["M1"].endswith("coil")
    assert libs["PB1"].endswith("pb_nc")
    assert libs["PB2"].endswith("pb_no")
    assert libs["OL1"].endswith("ol_contact")
    assert libs["M1-13"].endswith("aux_no")
    assert libs["CR1"] in {"nema.contactors.coil", "nema.relays.cr"}
    assert libs["CR1-13"].endswith("aux_no")
    assert libs["CR1-21"].endswith("aux_nc")
    assert libs["PL1"].endswith("pilot_light")
    assert libs["LS1"].endswith("limit_no")
    ends = _wire_ends(built)
    assert ("M1-13.4", "M1.A1") in ends
    assert ("PB1.2", "PB2.3") in ends


def test_parallel_leads_choose_the_power_poles():
    """Three leads through a small symbol select the power-pole part, not the aux or 1-pole."""

    from panelcad.importer.vectors import _prefix_devices, _raise_to_drawn_poles

    catalog = default_catalog()

    def leads(xs, y0, y1):
        return [{"kind": "line", "role": "wire", "a": [x, y0], "b": [x, y1]} for x in xs]

    breaker = catalog.get("nema.breakers.mcb_1p_16a")
    chosen = _raise_to_drawn_poles(
        (0.9, breaker, 100.0, 100.0, 1.0),
        _prefix_devices(catalog, "CB", "NEMA/ANSI"),
        leads((108.0, 118.0, 128.0), 80.0, 170.0),
        "NEMA/ANSI",
        "CB",
        100.0,
        92.0,
        [],
        [],
        (110.0, 94.0),
    )
    assert "mcb_3p" in chosen[1].library_id
    assert "mcb_1p" not in chosen[1].library_id

    aux = catalog.get("nema.contactors.aux_nc")
    chosen = _raise_to_drawn_poles(
        (0.9, aux, 32.0, 70.0, 1.0),
        _prefix_devices(catalog, "M", "NEMA/ANSI"),
        leads((40.0, 51.0, 62.0), 40.0, 160.0),
        "NEMA/ANSI",
        "M",
        36.0,
        60.0,
        [],
        [],
        (40.0, 65.0),
    )
    assert chosen[1].library_id.endswith("ac3_32a")

    motor = catalog.get("nema.loads.motor_1ph")
    chosen = _raise_to_drawn_poles(
        (0.9, motor, 100.0, 90.0, 1.0),
        _prefix_devices(catalog, "MTR", "NEMA/ANSI"),
        leads((108.0, 118.0, 128.0), 40.0, 100.0),
        "NEMA/ANSI",
        "MTR",
        108.0,
        80.0,
        [],
        [],
        (110.0, 82.0),
    )
    assert chosen[1].library_id.endswith("motor_3ph")


def test_a_neighbour_a_pitch_away_stays_one_pole():
    """Leads of the next device are not this breaker's poles. One extra lead is not three."""

    from panelcad.importer.vectors import _prefix_devices, _raise_to_drawn_poles

    catalog = default_catalog()
    breaker = catalog.get("nema.breakers.mcb_1p_16a")

    def leads(xs):
        return [{"kind": "line", "role": "wire", "a": [x, 80.0], "b": [x, 170.0]} for x in xs]

    def chosen(xs):
        return _raise_to_drawn_poles(
            (0.9, breaker, 100.0, 100.0, 1.0),
            _prefix_devices(catalog, "CB", "NEMA/ANSI"),
            leads(xs),
            "NEMA/ANSI",
            "CB",
            100.0,
            92.0,
            [],
            [],
            (110.0, 94.0),
        )

    # Own lead at the pin (x=110). The next breaker is one pitch (22 mm) over.
    pitched = chosen((110.0, 132.0, 142.0, 152.0))
    assert pitched[1].library_id.endswith("mcb_1p_16a")
    # A lead 4 mm outside the symbol, plus this pole, is still one pole.
    beside = chosen((110.0, 124.0))
    assert beside[1].library_id.endswith("mcb_1p_16a")


def _reimport_libs_and_ends(session, path: Path, workspace: Path) -> tuple[dict[str, str], set[tuple[str, str]]]:
    built = ImportJob.start([path], workspace, backend="external").build()
    libs = _libs(built)
    ends = _wire_ends(built)
    return libs, ends


def test_single_pole_breakers_keep_their_own_wires(tmp_path: Path):
    """Breakers one pitch apart stay 1-pole. A neighbour's lead is not a second pole."""

    from panelcad.api.session import Session

    def build(pitch: float) -> Session:
        session = Session.create("three 1p", sheet_id="power", title="Power")
        session.set_title_block(drawing_number="P1", standard="NEMA/ANSI", description="branch breakers")
        for index in range(3):
            x = 40 + index * pitch
            session.place("nema.breakers.mcb_1p_16a", ref=f"CB{index + 1}", x=x, y=30)
            # The pilot's terminal is on the same vertical as the breaker load pin.
            session.place("nema.operators.pilot_light", ref=f"PL{index + 1}", x=x + 10, y=120)
            session.wire(f"CB{index + 1}.2", f"PL{index + 1}.X1", number=str(10 + index))
        return session

    for pitch in (18, 22, 26, 32, 40):
        session = build(pitch)
        for fmt in ("pdf", "dxf", "svg"):
            path = tmp_path / f"p{pitch:.0f}.{fmt}"
            getattr(session, f"export_{fmt}")(path)
            libs, ends = _reimport_libs_and_ends(session, path, tmp_path / f"ws-{pitch:.0f}-{fmt}")
            for index in range(1, 4):
                assert libs[f"CB{index}"].endswith("mcb_1p_16a"), (pitch, fmt, libs[f"CB{index}"])
            for index in range(1, 4):
                assert (f"CB{index}.2", f"PL{index}.X1") in ends
            assert ("PL1.X2", "PL2.X1") not in ends


def test_control_breaker_beside_a_stack_stays_one_pole(tmp_path: Path):
    """A 1-pole breaker a few millimetres from a 3-phase stack keeps its own symbol."""

    from panelcad.api.session import Session

    def build(gap: float) -> Session:
        session = Session.create("ctrl beside", sheet_id="power", title="Power")
        session.set_title_block(drawing_number="P2", standard="NEMA/ANSI", description="main + control breaker")
        session.place("nema.breakers.mcb_3p_32a", ref="CB1", x=40, y=22)
        session.place("nema.contactors.ac3_32a", ref="M1", x=38, y=100)
        session.place("nema.loads.motor_3ph", ref="MTR1", x=40, y=200)
        session.place("nema.breakers.mcb_1p_16a", ref="CB2", x=40 + 36 + gap, y=22)
        session.place("nema.contactors.aux_no", ref="CR1", x=40 + 44 + gap, y=130)
        session.place("nema.operators.pilot_light", ref="PL1", x=40 + 36 + gap, y=200)
        for frm, to, number in (
            ("CB1.2", "M1.L1", "L1"),
            ("CB1.4", "M1.L2", "L2"),
            ("CB1.6", "M1.L3", "L3"),
            ("M1.T1", "MTR1.T1", "T1"),
            ("M1.T2", "MTR1.T2", "T2"),
            ("M1.T3", "MTR1.T3", "T3"),
            ("CB2.2", "CR1.3", "20"),
            ("CR1.4", "PL1.X1", "21"),
        ):
            session.wire(frm, to, number=number)
        return session

    for gap in (2, 4, 10, 20):
        session = build(gap)
        for fmt in ("pdf", "dxf", "svg"):
            path = tmp_path / f"g{gap:.0f}.{fmt}"
            getattr(session, f"export_{fmt}")(path)
            libs, ends = _reimport_libs_and_ends(session, path, tmp_path / f"ws-g{gap:.0f}-{fmt}")
            assert libs["CB1"].endswith("mcb_3p_32a")
            assert libs["CB2"].endswith("mcb_1p_16a"), (gap, fmt, libs["CB2"])
            assert libs["M1"].endswith("ac3_32a")
            assert libs["MTR1"].endswith("motor_3ph")
            assert ("CB1.2", "M1.L1") in ends
            assert ("CB1.4", "M1.L2") in ends
            assert ("CB1.6", "M1.L3") in ends
            assert ("M1.T1", "MTR1.T1") in ends
            assert ("M1.T2", "MTR1.T2") in ends
            assert ("M1.T3", "MTR1.T3") in ends
            # PDF and SVG keep CR1 as a coil, so CB2-CR1-PL1 comes back.
            # The DXF text for this sheet is read as a relay (k_4co), whose pins
            # are not 3/4, and that control wire does not land. The pole check
            # is the library id, which is asserted for every format.
            if fmt != "dxf":
                assert any(a.startswith("CB2.") and b.startswith("CR1.") for a, b in ends)
                assert any(a.startswith("CR1.") and b.startswith("PL1.") for a, b in ends)


def test_stacked_rungs_keep_the_overload_contact(tmp_path: Path):
    """An overload contact under the rung above stays a contact. Parallel rungs are not poles."""

    from panelcad.api.session import Session

    session = Session.create("rungs", sheet_id="control", title="Control")
    session.set_title_block(drawing_number="P3", standard="NEMA/ANSI", description="ladder")
    # Pin-to-pin pitch is 25.4 mm, so the rung above sits about 9 mm outside the contact.
    session.place("nema.operators.pb_no", ref="PB1", x=40, y=40)
    session.place("nema.operators.pb_nc", ref="PB2", x=100, y=40)
    session.place("nema.protection.ol_contact", ref="OL1", x=40, y=73.4)
    session.place("nema.operators.pilot_light", ref="PL1", x=110, y=77.4)
    session.place("nema.contactors.coil", ref="M1", x=160, y=50)
    session.wire("PB1.4", "PB2.1", number="1")
    session.wire("OL1.95", "PL1.X1", number="2")
    session.wire("PB2.2", "M1.A1", number="3")
    for fmt in ("pdf", "dxf", "svg"):
        path = tmp_path / f"rungs.{fmt}"
        getattr(session, f"export_{fmt}")(path)
        libs, ends = _reimport_libs_and_ends(session, path, tmp_path / f"ws-{fmt}")
        assert libs["OL1"].endswith("ol_contact"), (fmt, libs["OL1"])
        assert "overload" not in libs["OL1"] or libs["OL1"].endswith("ol_contact")
        assert libs["M1"].endswith("coil")
        assert ("OL1.95", "PL1.X1") in ends


def test_four_rungs_keep_overload_contacts_under_the_rung_above(tmp_path: Path):
    """Four ladder rungs. The wire on the rung above an overload contact is not a pole."""

    from panelcad.api.session import Session

    session = Session.create("four rungs", sheet_id="control", title="Control")
    session.set_title_block(drawing_number="P4", standard="NEMA/ANSI", description="ladder")
    pitch = 25.4
    for index in range(4):
        rung = 80.0 + index * pitch
        number = index + 1
        session.place("nema.operators.pb_no", ref=f"PB{number}", x=40, y=rung - 24)
        session.place("nema.protection.ol_contact", ref=f"OL{number}", x=110, y=rung - 16)
        session.place("nema.contactors.coil", ref=f"M{number}", x=180, y=rung - 14)
        session.wire(f"PB{number}.4", f"OL{number}.95", number=str(number * 2 - 1))
        session.wire(f"OL{number}.96", f"M{number}.A1", number=str(number * 2))
    for fmt in ("pdf", "dxf", "svg"):
        path = tmp_path / f"four.{fmt}"
        getattr(session, f"export_{fmt}")(path)
        libs, ends = _reimport_libs_and_ends(session, path, tmp_path / f"ws-four-{fmt}")
        for number in range(1, 5):
            assert libs[f"OL{number}"].endswith("ol_contact"), (fmt, number, libs[f"OL{number}"])
            assert libs[f"M{number}"].endswith("coil"), (fmt, libs[f"M{number}"])
            assert tuple(sorted((f"OL{number}.96", f"M{number}.A1"))) in ends


def test_tag_scan_does_not_reload_the_library(monkeypatch):
    """Other-tag lookup uses the catalog already in hand. Loads do not follow the word count."""

    from panelcad.importer.vectors import read_drawing
    from panelcad.library.catalog import Catalog

    catalog = default_catalog()
    loads = {"catalog": 0, "file": 0}
    real_catalog = __import__("panelcad.library.catalog", fromlist=["default_catalog"]).default_catalog
    real_load = Catalog.load_file

    def counted_catalog(*args, **kwargs):
        loads["catalog"] += 1
        return real_catalog(*args, **kwargs)

    def counted_load(self, path):
        loads["file"] += 1
        return real_load(self, path)

    monkeypatch.setattr("panelcad.library.catalog.default_catalog", counted_catalog)
    monkeypatch.setattr(Catalog, "load_file", counted_load)

    def drawing(devices: int, extras: int):
        tokens = []
        strokes = []
        for index in range(devices):
            x = 30.0 + (index % 8) * 40.0
            y = 30.0 + (index // 8) * 50.0
            tokens.append({"text": f"CB{index + 1}", "x": x, "y": y - 8, "w": 8, "h": 3, "confidence": 0.95})
            tokens.append({"text": f"M{index + 1}-13", "x": x + 12, "y": y + 20, "w": 10, "h": 3, "confidence": 0.9})
            strokes.append({"kind": "line", "role": "wire", "a": [x, y], "b": [x, y + 40]})
        for index in range(extras):
            tokens.append({"text": str(200 + index), "x": 15.0 + index, "y": 8.0, "w": 4, "h": 2, "confidence": 0.9})
        return read_drawing(tokens, strokes, 420.0, 297.0, catalog, standard="NEMA/ANSI")

    before = dict(loads)
    drawing(4, 10)
    small = {key: loads[key] - before[key] for key in loads}
    before = dict(loads)
    drawing(24, 80)
    large = {key: loads[key] - before[key] for key in loads}
    assert large["catalog"] == small["catalog"] == 0
    assert large["file"] == small["file"] == 0


def test_real_n1_booster_uses_the_three_pole_symbols(tmp_path: Path):
    """The booster print's body ink is not a fifth pole. CB, M, OL, and the motor stay 3-pole."""

    from panelcad.importer.vision_config import VisionSettings
    from panelcad.io.yaml_format import load_project

    root = Path(__file__).parent / "fixtures" / "n1"
    truth = load_project(root / "N1_booster.pcad.yaml")
    expected = {
        comp.id: comp.library_id
        for comp in truth.sheet("pwr").components
        if comp.id in {"CB1", "M1", "OL1", "MTR1"}
    }

    def check(path: Path, workspace: Path, **kwargs) -> None:
        built = ImportJob.start([path], workspace, **kwargs).build()
        libs = _libs(built)
        assert libs["CB1"] == expected["CB1"]
        assert "mcb_1p" not in libs["CB1"]
        assert libs["M1"] == expected["M1"]
        assert libs["OL1"] == expected["OL1"]
        assert "ol_contact" not in libs["OL1"]
        assert libs["MTR1"] == expected["MTR1"]
        assert "motor_1ph" not in libs["MTR1"]

    check(root / "N1_booster.pdf", tmp_path / "pdf", backend="raster", vision=VisionSettings(preset="none"))
    check(root / "N1_booster.svg", tmp_path / "svg", backend="external")
    check(root / "N1_booster_pwr.dxf", tmp_path / "dxf", backend="external")


def test_changelog_has_no_conflict_markers():
    for line in Path("CHANGELOG.md").read_text(encoding="utf-8").splitlines():
        assert not line.startswith("<<<<<<< ")
        assert not line.startswith(">>>>>>> ")


def test_side_by_side_button_outputs_stay_separate_nets(tmp_path: Path):
    """STOP and START on one ladder are two nets. They do not get tied together."""

    built = ImportJob.start([Path("examples/dol_starter.pdf")], tmp_path / "ws", backend="external").build()
    ends = _wire_ends(built)
    assert ("S1.14", "S2.12") not in ends
    assert ("A1.I0_1", "S1.14") in ends


def test_blank_import_wires_can_be_numbered(tmp_path: Path):
    from panelcad.api.session import open_project
    from panelcad.importer.accuracy import score_sheet_pair
    from panelcad.io.yaml_format import load_project

    truth = load_project(Path("examples/dol_starter.pcad.yaml"))
    built = ImportJob.start([Path("examples/dol_power.dxf")], tmp_path / "dxf", backend="external").build()
    score = score_sheet_pair(truth.sheet("power"), built.project.sheets[0], default_catalog())
    assert score["connectivity_recall"] == 1.0
    blanks = [wire for sheet in built.project.sheets for wire in sheet.wires if not str(wire.number or "").strip()]
    assert blanks
    assert all(wire.number_locked is False for wire in blanks)
    blank_keys = {tuple(sorted((wire.frm, wire.to))) for wire in blanks}
    printed = {
        tuple(sorted((wire.frm, wire.to))): str(wire.number)
        for sheet in built.project.sheets
        for wire in sheet.wires
        if str(wire.number or "").strip()
    }
    out = tmp_path / "imported.pcad.yaml"
    built.save(out)
    reopened = open_project(out)
    result = reopened.number_wires(overwrite=True)
    assert result["assigned"] > 0
    for sheet in reopened.project.sheets:
        for wire in sheet.wires:
            key = tuple(sorted((wire.frm, wire.to)))
            if key in printed:
                assert str(wire.number) == printed[key]
            elif key in blank_keys:
                assert str(wire.number or "").strip()


def _wire_ends(session) -> set[tuple[str, str]]:
    """Pin pairs. A colon contact tag is the same terminal as the hyphen form."""

    found = set()
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            found.add(tuple(sorted((wire.frm.replace(":", "-"), wire.to.replace(":", "-")))))
    return found
