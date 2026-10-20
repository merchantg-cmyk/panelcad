"""Wires and tags from a print PanelCAD did not draw.

The cases are synthetic: line colour, line width, text height, IEC aspect
marks, and DXF entity kinds vary. Nothing here is taken from one shop's print.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from panelcad.importer.accuracy import score_projects
from panelcad.importer.pipeline import ImportJob
from panelcad.library.catalog import default_catalog

PT = 25.4 / 72.0


def _mm(x: float, y: float):
    return pymupdf.Point(x / PT, y / PT)


def _ladder_pdf(path: Path, *, color: tuple[float, float, float], width: float) -> None:
    """Two rails, three devices, wire numbers the same height as the tags."""

    doc = pymupdf.open()
    page = doc.new_page(width=420 / PT, height=297 / PT)
    page.draw_line(_mm(40, 30), _mm(40, 180), color=color, width=width)
    page.draw_line(_mm(220, 30), _mm(220, 180), color=color, width=width)
    page.draw_line(_mm(40, 55), _mm(78, 55), color=color, width=width)
    page.draw_line(_mm(98, 55), _mm(150, 55), color=color, width=width)
    page.draw_line(_mm(170, 55), _mm(220, 55), color=color, width=width)
    page.draw_line(_mm(40, 110), _mm(78, 110), color=color, width=width)
    page.draw_line(_mm(98, 110), _mm(220, 110), color=color, width=width)
    page.draw_circle(_mm(88, 55), 8 / PT, color=color, width=width)
    page.draw_circle(_mm(160, 55), 8 / PT, color=color, width=width)
    page.draw_circle(_mm(88, 110), 8 / PT, color=color, width=width)
    labels = [
        ("PB1", 80, 40, 9),
        ("PB2", 150, 40, 9),
        ("PB3", 80, 96, 9),
        ("101", 50, 51, 9),
        ("W101", 112, 51, 9),
        ("1", 185, 51, 9),
        ("12345", 140, 106, 9),
        ("L1", 36, 18, 11),
        ("L2", 214, 18, 11),
        ("DWG", 40, 250, 8),
        ("NO:", 58, 250, 8),
        ("LS-2041-E1", 78, 250, 8),
        ("REV:", 40, 262, 8),
        ("B", 58, 262, 8),
        ("ACME CONTROLS INC.", 40, 274, 8),
        ("NEMA", 200, 262, 8),
        ("-K1", 270, 80, 9),
        ("-S1", 310, 80, 9),
        ("=A1-K1", 270, 100, 9),
        ("2-15", 270, 140, 9),
    ]
    for text, x, y, size in labels:
        page.insert_text(_mm(x, y), text, fontsize=size, color=color)
    doc.save(path)
    doc.close()


def _truth_for(built):
    from panelcad.api.session import Session

    libs = {comp.id: comp.library_id for sheet in built.project.sheets for comp in sheet.components}
    truth = Session.create("ladder", sheet_id="s", title="Ladder")
    truth.set_title_block(standard="NEMA/ANSI", drawing_style="ladder")
    for ref in ("PB1", "PB2", "PB3"):
        truth.place(libs[ref], ref=ref, x=40, y=40, snap=False)
    sheet = truth.project.sheet("s")
    sheet.rail_hot = "L1"
    sheet.rail_return = "L2"
    truth.wire("PB1.3", "L1", number="101")
    truth.wire("PB1.4", "PB2.3", number="W101")
    truth.wire("PB2.4", "L2", number="1")
    truth.wire("PB3.4", "L2", number="12345")
    return truth


def _score(path: Path, tmp_path: Path) -> dict:
    built = ImportJob.start([path], tmp_path / "ws", backend="external").build()
    score = score_projects(_truth_for(built).project, built.project, catalog=default_catalog())
    score["built"] = built
    return score


def test_monochrome_thin_lines_recover_wire_numbers(tmp_path: Path):
    """Black 0.6 pt ink, tags and wire numbers the same height."""

    pdf = tmp_path / "thin.pdf"
    _ladder_pdf(pdf, color=(0, 0, 0), width=0.6)
    score = _score(pdf, tmp_path)
    assert score["wire_numbers"] >= 0.8
    assert score["connectivity_precision"] >= 0.95
    numbers = {wire.number for sheet in score["built"].project.sheets for wire in sheet.wires}
    assert {"101", "W101", "1", "12345"} <= numbers
    block = score["built"].project.title_block
    assert block.drawing_number == "LS-2041-E1"
    assert block.company == "ACME CONTROLS INC."
    assert block.revisions and block.revisions[0].rev == "B"
    tags = {comp.id for sheet in score["built"].project.sheets for comp in sheet.components}
    # The sheet prints the aspect mark. The stored id keeps it.
    assert {"PB1", "PB2", "PB3", "-K1", "-S1"} <= tags


def test_purple_heavy_lines_score_the_same(tmp_path: Path):
    """A colour and width the old stroke test rejected."""

    pdf = tmp_path / "purple.pdf"
    _ladder_pdf(pdf, color=(0.45, 0.05, 0.55), width=2.0)
    score = _score(pdf, tmp_path)
    assert score["wire_numbers"] >= 0.8
    assert score["connectivity_precision"] >= 0.95


def test_unconnected_number_is_a_problem_with_a_position(tmp_path: Path):
    pdf = tmp_path / "thin.pdf"
    _ladder_pdf(pdf, color=(0, 0, 0), width=0.6)
    built = ImportJob.start([pdf], tmp_path / "ws", backend="external").build()
    from panelcad.core.checks import run_checks

    findings = [item for item in run_checks(built.project, built.catalog) if item.check == "print_wire"]
    hands = [item for item in findings if "2-15" in item.message and "connect by hand" in item.message]
    assert hands
    assert hands[0].x is not None and hands[0].y is not None
    reviews = [item for item in findings if "W101" in item.message and "review" in item.message]
    assert reviews


def test_tag_height_uses_the_dominant_size_and_wire_patterns():
    from panelcad.importer.read import (
        iec_product_tag,
        is_device_tag,
        is_wire_label,
        label_tokens,
        lettered_wire_text,
        read_title_block,
    )

    assert iec_product_tag("-K1") == "K1"
    assert iec_product_tag("-S1") == "S1"
    assert iec_product_tag("-X2-4") == "X2-4"
    assert iec_product_tag("=A1-K1") == "K1"
    assert is_device_tag("-K1") and is_device_tag("=A1-K1") and is_device_tag("-X2-4")
    assert is_wire_label("1") and is_wire_label("12345") and is_wire_label("2-15")
    assert not is_wire_label("6-410")
    assert lettered_wire_text("W101") and lettered_wire_text("101A")
    assert not lettered_wire_text("K1") and not lettered_wire_text("10M")
    assert is_wire_label("W101") and not is_device_tag("W101")

    # Nine-point rail captions must not drop the seven-point tags.
    tokens = [{"text": "X1", "x": 40, "y": 30, "w": 6, "h": 3.2}]
    tokens += [{"text": f"K{index}", "x": 40 + index * 20, "y": 80, "w": 6, "h": 2.5} for index in range(1, 8)]
    labeled = label_tokens(tokens, 420, 297)
    assert {item["text"] for item in labeled["tags"]} >= {f"K{index}" for index in range(1, 8)}

    # One text size: the wire number is kept. A lettered number is a wire, not a device.
    same = label_tokens(
        [
            {"text": "PB1", "x": 40, "y": 40, "w": 8, "h": 3.2},
            {"text": "101", "x": 80, "y": 40, "w": 8, "h": 3.2},
            {"text": "12345", "x": 120, "y": 40, "w": 12, "h": 3.2},
            {"text": "W101", "x": 160, "y": 40, "w": 10, "h": 3.2},
        ],
        420,
        297,
    )
    assert "101" in {item["text"] for item in same["wires"]}
    assert "12345" in {item["text"] for item in same["wires"]}
    assert "PB1" in {item["text"] for item in same["tags"]}

    height = 297.0
    band = [
        {"text": "DWG", "x": 40, "y": 250, "w": 8, "h": 3},
        {"text": "NO:", "x": 55, "y": 250, "w": 8, "h": 3},
        {"text": "LS-2041-E1", "x": 90, "y": 250, "w": 24, "h": 3},
        {"text": "REV:", "x": 40, "y": 262, "w": 8, "h": 3},
        {"text": "B", "x": 58, "y": 262, "w": 4, "h": 3},
        {"text": "ACME", "x": 40, "y": 274, "w": 12, "h": 3},
        {"text": "CONTROLS", "x": 60, "y": 274, "w": 16, "h": 3},
        {"text": "INC.", "x": 84, "y": 274, "w": 8, "h": 3},
    ]
    block = read_title_block(band, height)
    assert block["drawing_number"] == "LS-2041-E1"
    assert block["rev"] == "B"
    assert block["company"] == "ACME CONTROLS INC."


def test_dxf_entities_trace_like_pdf_wires(tmp_path: Path):
    import ezdxf

    doc = ezdxf.new()
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()
    msp.add_line((40, 250), (40, 120))
    msp.add_line((220, 250), (220, 120))
    # Rung pieces: a line, an lwpolyline, a polyline, and a shallow arc chord.
    msp.add_line((40, 220), (78, 220))
    msp.add_lwpolyline([(98, 220), (150, 220)])
    msp.add_polyline2d([(170, 220), (220, 220)])
    msp.add_line((40, 170), (78, 170))
    msp.add_lwpolyline([(98, 170), (200, 170), (220, 170)])
    msp.add_circle((88, 220), 8)
    msp.add_circle((160, 220), 8)
    msp.add_circle((88, 170), 8)
    block = doc.blocks.new("HOP")
    block.add_arc((0, 0), 4, 10, 40)
    msp.add_blockref("HOP", (120, 170))
    for text, x, y, height in (
        ("PB1", 80, 232, 3.2),
        ("PB2", 150, 232, 3.2),
        ("PB3", 80, 182, 3.2),
        ("101", 55, 222, 3.2),
        ("W101", 118, 222, 3.2),
        ("1", 190, 222, 3.2),
        ("12345", 140, 172, 3.2),
        ("L1", 36, 258, 4.0),
        ("L2", 214, 258, 4.0),
        ("NEMA", 40, 40, 3.0),
    ):
        msp.add_text(text, dxfattribs={"insert": (x, y), "height": height})
    path = tmp_path / "ladder.dxf"
    doc.saveas(path)

    from panelcad.importer.vectors import strokes_from_dxf

    strokes = strokes_from_dxf(path)
    kinds = {stroke.get("kind") for stroke in strokes}
    assert "line" in kinds and "curve" in kinds
    assert any(stroke.get("role") == "wire" for stroke in strokes)

    score = _score(path, tmp_path)
    assert score["wire_numbers"] >= 0.8
    assert score["connectivity_precision"] >= 0.95


def test_a_number_does_not_jump_to_the_next_rung(tmp_path: Path):
    """Two rungs, 18 mm apart. 101 names the upper pair only."""

    pdf = tmp_path / "rungs.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=420 / PT, height=297 / PT)
    black = (0, 0, 0)
    page.draw_line(_mm(40, 30), _mm(40, 160), color=black, width=0.6)
    page.draw_line(_mm(220, 30), _mm(220, 160), color=black, width=0.6)
    page.draw_line(_mm(40, 55), _mm(90, 55), color=black, width=0.6)
    page.draw_line(_mm(110, 55), _mm(220, 55), color=black, width=0.6)
    page.draw_line(_mm(40, 73), _mm(90, 73), color=black, width=0.6)
    page.draw_line(_mm(110, 73), _mm(220, 73), color=black, width=0.6)
    page.draw_circle(_mm(100, 55), 8 / PT, color=black, width=0.6)
    page.draw_circle(_mm(100, 73), 8 / PT, color=black, width=0.6)
    page.insert_text(_mm(92, 42), "PB1", fontsize=9, color=black)
    page.insert_text(_mm(92, 86), "CR1", fontsize=9, color=black)
    page.insert_text(_mm(55, 51), "101", fontsize=9, color=black)
    page.insert_text(_mm(55, 69), "202", fontsize=9, color=black)
    page.insert_text(_mm(36, 18), "L1", fontsize=11, color=black)
    page.insert_text(_mm(214, 18), "L2", fontsize=11, color=black)
    page.insert_text(_mm(200, 250), "NEMA", fontsize=8, color=black)
    doc.save(pdf)
    doc.close()
    built = ImportJob.start([pdf], tmp_path / "ws", backend="external").build()
    numbered = {
        wire.number: {wire.frm.split(".")[0], wire.to.split(".")[0]}
        for sheet in built.project.sheets
        for wire in sheet.wires
        if wire.number
    }
    if "101" in numbered:
        assert "CR1" not in numbered["101"]
    if "202" in numbered:
        assert "PB1" not in numbered["202"]
    # A guessed pair is not a connection.
    wrong = [ends for number, ends in numbered.items() if number == "101" and "CR1" in ends]
    assert wrong == []


def test_pin_digit_is_not_a_wire_number_and_nearby_sizes_stay_tags():
    from panelcad.importer.read import iec_product_tag, is_device_tag, label_tokens, lettered_wire_text

    assert iec_product_tag("-B3") == "B3"
    assert iec_product_tag("-X2-4") == "X2-4"
    assert iec_product_tag("-Y1") == "Y1"
    assert is_device_tag("-B3") and is_device_tag("-X2-4") and is_device_tag("-Y1")
    assert is_device_tag("TB1-1")
    assert lettered_wire_text("W101") and lettered_wire_text("W109")

    tokens = []
    for index, height in enumerate((4.32, 5.04, 5.76)):
        for slot in range(3):
            tokens.append(
                {
                    "text": f"LS{index * 3 + slot + 1}",
                    "x": 40 + slot * 30,
                    "y": 40 + index * 25,
                    "w": 10,
                    "h": height,
                }
            )
    labeled = label_tokens(tokens, 420, 297)
    assert {item["text"] for item in labeled["tags"]} == {f"LS{n}" for n in range(1, 10)}


def test_contact_height_gate_still_drops_a_much_smaller_token():
    """The coil tag and its contacts stay. A clearly smaller stray tag does not."""

    from panelcad.importer.read import label_tokens

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


def test_a_matched_drive_named_u1_is_not_a_lead_mark():
    """U1 on a wire with no symbol is a motor lead. A matched drive named U1 stays.

    T1 T2 T3 on one baseline are terminal marks even when a prefix match
    assigned them a library symbol.
    """

    from panelcad.importer.vectors import _drop_lead_marks

    wires = [
        {"kind": "line", "role": "wire", "a": (0, 50), "b": (220, 50)},
        {"kind": "line", "role": "wire", "a": (40, 205), "b": (90, 205)},
    ]
    devices = [
        {"tag": "U1", "x": 40, "y": 48, "library_id": "", "guess": True},
        {"tag": "U1", "x": 200, "y": 48, "text_x": 200, "text_y": 48, "library_id": "adc.durapulse.gs21_20p5", "guess": False},
        # The matcher parked a soft starter on the motor. The printed U1 V1 W1 stay one row.
        {"tag": "U1", "x": 45.0, "y": 207.0, "text_x": 54.5, "text_y": 205.2, "library_id": "iec.drives.softstarter", "guess": False},
        {"tag": "V1", "x": 66.5, "y": 205.2, "text_x": 66.5, "text_y": 205.2, "library_id": "", "guess": True},
        {"tag": "W1", "x": 78.8, "y": 205.2, "text_x": 78.8, "text_y": 205.2, "library_id": "", "guess": True},
        {"tag": "T1", "x": 20, "y": 52, "library_id": "", "guess": True},
        {"tag": "T1", "x": 50, "y": 46, "library_id": "iec.power.control_transformer", "guess": False},
        {"tag": "T2", "x": 60, "y": 46, "library_id": "iec.power.control_transformer", "guess": False},
        {"tag": "T3", "x": 70, "y": 46, "library_id": "iec.power.control_transformer", "guess": False},
        {"tag": "M1", "x": 10, "y": 70, "library_id": "iec.loads.motor_3ph", "guess": False},
    ]
    kept = _drop_lead_marks(devices, wires)
    tags = [(item["tag"], round(float(item["x"]), 1)) for item in kept]
    assert ("U1", 40.0) not in tags
    assert ("T1", 20.0) not in tags
    assert ("T1", 50.0) not in tags
    assert ("T2", 60.0) not in tags
    assert ("T3", 70.0) not in tags
    assert ("U1", 45.0) not in tags
    assert ("V1", 66.5) not in tags
    assert ("W1", 78.8) not in tags
    assert ("U1", 200.0) in tags
    assert ("M1", 10.0) in tags


def test_a_number_beside_one_stub_stays_on_that_stub():
    """Ten millimetres off one phase is that phase. Two phases equally close stay open.

    The short run across one device's own terminals is not that wire.
    """

    from panelcad.importer.vectors import _label_chain_index

    chains = [
        {"segments": [((48, 180), (48, 220))], "ends": [{"tag": "F1", "pin": "2"}, {"tag": "M1", "pin": "U1"}]},
        {
            "segments": [((48, 190), (48, 210))],
            "ends": [{"tag": "F1", "pin": "1"}, {"tag": "F1", "pin": "2"}],
        },
        {"segments": [((70, 180), (70, 220))], "ends": []},
    ]
    beside = {"text": "104", "x": 37.6, "y": 205.0, "h": 2.7}
    between = {"text": "9", "x": 59.0, "y": 200.0, "h": 2.7}
    assert _label_chain_index(beside, chains, []) == 0
    assert _label_chain_index(between, chains, []) is None


def test_a_side_with_no_terminal_does_not_borrow_another_pin():
    from panelcad.importer.build import choose_side_pin

    class Pin:
        def __init__(self, pin_id, side, x, y):
            self.id, self.side, self.x, self.y = pin_id, side, x, y

    pins = [Pin("L2", "top", 22, 0), Pin("A2", "left", 0, 44)]
    assert choose_side_pin(pins, "right", (40, 20)) is None
    assert choose_side_pin(pins, "left", (0, 40)).id == "A2"
    # The overload has no left terminal. The wire arrives on the line of 95.
    overload = [Pin("95", "top", 18, 8), Pin("96", "bottom", 18, 40)]
    assert choose_side_pin(overload, "left", (0, 8)).id == "95"
    assert choose_side_pin(overload, "left", (0, 8), owned={"95"}) is None


def test_two_symbols_on_one_spot_keep_the_tag_printed_there():
    """The terminal belongs to the tag on that symbol, not the later tag at the same origin."""

    from panelcad.importer.vectors import _snap

    point = (210.0, 43.7)
    pins = [
        {
            "tag": "PL20",
            "pin": "X1",
            "x": 210.0,
            "y": 43.7,
            "ox": 210.0,
            "oy": 31.7,
            "text_x": 222.0,
            "text_y": 53.56,
        },
        {
            "tag": "PL19",
            "pin": "X1",
            "x": 210.0,
            "y": 43.7,
            "ox": 210.0,
            "oy": 31.7,
            "text_x": 222.0,
            "text_y": 28.16,
        },
    ]
    assert _snap(point, pins)["tag"] == "PL19"


def test_two_symbols_on_the_same_spot_do_not_take_a_wire():
    from panelcad.importer.build import stacked_tag_ids

    stacked = stacked_tag_ids([("PL3", 210.0, 82.0), ("PL4", 210.5, 82.4), ("M5", 210.0, 130.0)])
    assert stacked == {"PL3", "PL4"}


def test_a_second_wire_does_not_take_a_pin_that_is_already_used():
    """The closer run keeps the terminal. The one past it is a question."""

    from panelcad.importer.vectors import _release_duplicate_pins

    near = {
        "number": "108",
        "from_tag": "CR2",
        "from_pin": "2",
        "to_tag": "PL4",
        "to_pin": "X1",
        "to_x": 210.0,
        "to_y": 120.0,
        "ambiguous": False,
        "connect_by_hand": False,
    }
    far = {
        "number": "107",
        "from_tag": "CR1",
        "from_pin": "4",
        "to_tag": "PL4",
        "to_pin": "X1",
        "to_x": 210.0,
        "to_y": 90.0,
        "ambiguous": False,
        "connect_by_hand": False,
    }
    _release_duplicate_pins([near, far], [{"tag": "PL4", "x": 210.0, "y": 130.0}])
    assert near["to_pin"] == "X1"
    assert far["connect_by_hand"] is True
    assert far["to_tag"] == ""


def test_an_end_does_not_land_on_the_neighbouring_rung():
    """A box grown onto the next rung is not that rung's terminal."""

    from panelcad.importer.vectors import _provisional_land

    # The terminal's own ink stays on its rung. The next rung is 21 mm below.
    upper = {"tag": "TB1", "box": (94.0, 54.0, 106.0, 68.0), "x": 100.0, "y": 60.0}
    assert _provisional_land((100.0, 81.0), [upper]) is None


def test_a_run_stops_at_the_first_symbol_it_reaches():
    """The end belongs to the lamp it reaches, not the coil past that lamp."""

    from panelcad.importer.vectors import _provisional_land

    lamp = {"tag": "PL1", "box": (94.0, 94.0, 106.0, 110.0), "x": 100.0, "y": 88.0}
    coil = {"tag": "M1", "box": (90.0, 90.0, 110.0, 140.0), "x": 100.0, "y": 130.0}
    landed = _provisional_land((100.0, 100.0), [lamp, coil])
    assert landed is not None and landed["tag"] == "PL1"


def test_a_number_on_a_conductor_in_the_margin_is_kept():
    """A rung number above the border inset is a wire. A line column is not."""

    from panelcad.importer.vectors import _numbers_on_conductors

    conductors = [{"a": [300.0, 18.0], "b": [500.0, 18.0], "role": "wire"}]
    tokens = [
        {"text": "3", "x": 321.5, "y": 14.0, "h": 5.0},
        {"text": "3", "x": 50.0, "y": 113.0, "h": 5.0},
    ]
    labels = _numbers_on_conductors([], tokens, conductors, {(50.0, 113.0, "3")})
    assert [(item["number"], item["y"]) for item in labels] == [("3", 14.0)]


def test_line_references_and_rail_names_are_not_wire_questions():
    from panelcad.importer.vectors import _line_reference_keys, _wire_question

    tokens = []
    ys = [30.0, 72.0, 93.0, 114.0, 135.0, 156.0]
    for index, y in enumerate(ys, start=1):
        tokens.append({"text": str(index), "x": 12.0, "y": y, "h": 2.5})
    rails = [
        {"a": [30.0, 20.0], "b": [30.0, 180.0], "role": "wire"},
        {"a": [200.0, 20.0], "b": [200.0, 180.0], "role": "wire"},
    ]
    marked = _line_reference_keys(tokens, rails, 420.0, 297.0)
    assert len(marked) == 6
    assert _wire_question({"text": "L1", "number": "L1", "x": 30.0, "y": 20.0}) is False
    assert _wire_question({"text": "0V", "number": "0V", "x": 30.0, "y": 20.0}) is False
    assert _wire_question({"text": "", "number": "", "x": 10.0, "y": 10.0}) is False
    assert _wire_question({"text": "W101", "number": "W101", "x": 40.0, "y": 40.0}) is True


def test_a_rating_beside_a_unit_is_not_a_wire_number():
    """60 A, 15 HP, and 480 V are ratings. The wire number on the rung stays."""

    from panelcad.importer.read import is_rating, label_tokens

    tokens = [
        {"text": "F1", "x": 40, "y": 40, "w": 6, "h": 3.2},
        {"text": "60", "x": 48, "y": 48, "w": 4, "h": 2.5},
        {"text": "A", "x": 52, "y": 48, "w": 2, "h": 2.5},
        {"text": "15", "x": 48, "y": 60, "w": 4, "h": 2.5},
        {"text": "HP", "x": 54, "y": 60, "w": 4, "h": 2.5},
        {"text": "480", "x": 70, "y": 60, "w": 6, "h": 2.5},
        {"text": "V", "x": 76, "y": 60, "w": 2, "h": 2.5},
        {"text": "20A", "x": 90, "y": 60, "w": 6, "h": 2.5},
        {"text": "101", "x": 40, "y": 80, "w": 6, "h": 2.5},
    ]
    assert is_rating(tokens[1], tokens)
    assert not is_rating(tokens[-1], tokens)
    assert not is_rating({"text": "24V", "x": 10, "y": 10, "h": 2.5}, tokens)
    assert not is_rating({"text": "0V", "x": 10, "y": 20, "h": 2.5}, tokens)
    wires = {item["text"] for item in label_tokens(tokens, 420.0, 297.0)["wires"]}
    assert "101" in wires
    assert "60" not in wires and "15" not in wires and "480" not in wires and "20A" not in wires


def test_two_short_tokens_are_not_a_tag_size():
    """Two words under the tag size are not a second size, and they are not parts."""

    from panelcad.importer.read import label_tokens

    tokens = [{"text": f"K{index}", "x": 20 + index * 15, "y": 40, "w": 6, "h": 3.4} for index in range(1, 9)]
    tokens += [
        {"text": "M7", "x": 40, "y": 80, "w": 6, "h": 3.4 * 0.73},
        {"text": "CR8", "x": 70, "y": 80, "w": 6, "h": 3.4 * 0.73},
    ]
    tags = {item["text"] for item in label_tokens(tokens, 420.0, 297.0)["tags"]}
    assert "K1" in tags and "K8" in tags
    assert "M7" not in tags and "CR8" not in tags


def test_a_small_token_at_symbol_ink_is_not_placed():
    """A device-shaped word at 55% of the tag height is not a catalog part."""

    from panelcad.importer.vectors import _restore_tags_at_symbols

    devices = [
        {"tag": "PL1", "x": 40.0, "y": 40.0, "h": 2.3, "library_id": "nema.operators.pilot_light", "guess": False}
    ]
    tokens = [
        {"text": "PL1", "x": 40.0, "y": 30.0, "h": 2.3, "w": 6},
        {"text": "M7", "x": 80.0, "y": 40.0, "h": 2.3 * 0.55, "w": 4},
    ]
    lines = [
        {"kind": "line", "role": "ink", "a": [76.0, 36.0], "b": [84.0, 36.0]},
        {"kind": "line", "role": "ink", "a": [76.0, 44.0], "b": [84.0, 44.0]},
        {"kind": "line", "role": "ink", "a": [76.0, 36.0], "b": [76.0, 44.0]},
        {"kind": "line", "role": "ink", "a": [84.0, 36.0], "b": [84.0, 44.0]},
    ]
    restored = _restore_tags_at_symbols(tokens, devices, lines, [])
    assert {item.get("tag") for item in restored} == {"PL1"}


def test_a_phase_number_beside_the_rail_stays_on_the_phase():
    """The number is as near the rail as the short phase. It belongs to the phase end."""

    from panelcad.importer.vectors import _label_chain_index

    chains = [
        {
            "segments": [((23.0, 25.0), (23.0, 241.0))],
            "ends": [{"tag": "L1", "pin": "L1", "rail": True}, {"tag": "L1", "pin": "L1", "rail": True}],
        },
        {
            "segments": [((48.0, 153.0), (48.0, 157.0))],
            "ends": [{"tag": "M1", "pin": "T1"}, {"tag": "OL1", "pin": "T1"}],
        },
    ]
    label = {"text": "101", "number": "101", "x": 35.41, "y": 154.8, "h": 2.68}
    assert _label_chain_index(label, chains, []) == 1


def test_lettered_wire_numbers_are_wire_questions_not_devices():
    from panelcad.importer.read import is_device_tag, is_wire_label, label_tokens

    assert is_wire_label("W101") and is_wire_label("W109") and is_wire_label("101A")
    assert not is_device_tag("W101") and not is_device_tag("101A")
    labeled = label_tokens(
        [
            {"text": "K1", "x": 40, "y": 40, "w": 8, "h": 3.2},
            {"text": "W101", "x": 80, "y": 40, "w": 12, "h": 3.2},
            {"text": "W102", "x": 120, "y": 40, "w": 12, "h": 3.2},
        ],
        420.0,
        297.0,
    )
    assert {item["text"] for item in labeled["tags"]} == {"K1"}
    assert {item["text"] for item in labeled["wires"]} >= {"W101", "W102"}
    margin = label_tokens(
        [{"text": "W101", "x": 40, "y": 8, "w": 12, "h": 3.2}, {"text": "1011", "x": 80, "y": 8, "w": 12, "h": 3.2}],
        420.0,
        297.0,
    )
    assert {item["text"] for item in margin["wires"]} >= {"W101", "1011"}
    assert margin["tags"] == []


def test_printed_aspect_mark_is_the_stored_tag():
    """-K1 is the reference that was printed. Symbol lookup still uses the product K."""

    from panelcad.importer.read import iec_product_tag, label_tokens, printed_reference_tag
    from panelcad.importer.symbols import tag_prefix

    assert printed_reference_tag("-K1") == "-K1"
    assert printed_reference_tag("=A1-K1") == "=A1-K1"
    assert printed_reference_tag("-X2-4") == "-X2-4"
    assert printed_reference_tag("K1") == ""
    assert iec_product_tag("-K1") == "K1"
    assert tag_prefix("-K1") == "K"
    assert tag_prefix("=A1-K1") == "K"
    assert tag_prefix("-X2-4") == "X"
    labeled = label_tokens(
        [
            {"text": "-K1", "x": 40, "y": 40, "w": 10, "h": 3.4},
            {"text": "-S1", "x": 80, "y": 40, "w": 10, "h": 3.4},
            {"text": "PB1", "x": 120, "y": 40, "w": 10, "h": 3.4},
        ],
        420.0,
        297.0,
    )
    assert {item["text"] for item in labeled["tags"]} == {"-K1", "-S1", "PB1"}


def test_traced_pin_keeps_the_contact_a_motor_would_replace():
    """The wire already names the contact pin. A motor of the same tag must not take the part."""

    from panelcad.importer.tile import mark_secondary_occurrences
    from panelcad.library.catalog import default_catalog

    contact = {
        "tag": "M1",
        "x": 165.0,
        "y": 116.0,
        "h": 3.39,
        "library_id": "nema.contactors.aux_nc",
        "symbol": "contactor",
        "guess": False,
        "confidence": 0.88,
    }
    motor = {
        "tag": "M1",
        "x": 102.0,
        "y": 127.0,
        "h": 3.39,
        "library_id": "iec.loads.motor_3ph",
        "symbol": "motor",
        "guess": False,
        "confidence": 0.9,
    }
    wires = [{"number": "16", "from_tag": "M1", "from_pin": "2", "to_tag": "OL2", "to_pin": "left"}]
    devices = [contact, motor]
    mark_secondary_occurrences(devices, wires, default_catalog())
    assert contact["secondary"] is False
    assert motor["secondary"] is True


def test_taller_occurrence_stays_the_placed_part():
    """A shorter coil must not move the part off the taller tag."""

    from panelcad.importer.tile import _prefer_occurrence

    contact = {"tag": "CR2", "h": 5.04, "library_id": "nema.relays.cr", "confidence": 0.48}
    coil = {"tag": "CR2", "h": 4.32, "library_id": "nema.relays.cr", "confidence": 0.97}
    assert _prefer_occurrence(contact, coil) is False
    assert _prefer_occurrence(coil, contact) is True


def test_coil_and_contact_of_one_name_both_stay():
    """Two glyphs a few millimetres apart are one label. A second symbol is not."""

    from panelcad.importer.read import _dedupe_tags

    kept = _dedupe_tags(
        [
            {"text": "CR1", "x": 145.0, "y": 98.0, "h": 3.4},
            {"text": "CR1", "x": 146.0, "y": 98.5, "h": 3.0},
            {"text": "CR1", "x": 327.0, "y": 39.0, "h": 2.9},
        ]
    )
    places = {(round(item["x"]), round(item["y"])) for item in kept}
    assert len(kept) == 2
    assert (327, 39) in places
    assert (145, 98) in places


def test_a_pin_inside_another_symbols_ink_does_not_take_the_end():
    """The coil pin that reaches the next device does not own that conductor."""

    from panelcad.importer.vectors import _land

    pins = [
        {
            "tag": "M10",
            "pin": "A2",
            "x": 178.0,
            "y": 38.4,
            "side": "right",
            "text_x": 169.0,
            "text_y": 54.4,
            "ox": 150.0,
            "oy": 24.0,
        }
    ]
    clusters = [{"tag": "PB11", "box": (161.0, 28.0, 180.0, 42.0), "x": 169.5, "y": 31.5}]
    landed = _land((177.1, 38.8), pins, [], [], clusters, (206.0, 38.8))
    assert landed is not None
    assert landed["tag"] == "PB11"
    assert landed.get("provisional") is True


def test_a_nearby_pin_does_not_block_the_symbol_the_ink_meets():
    """A pin just outside the snap, and far from its own tag, is not this end."""

    from panelcad.importer.vectors import _land

    pins = [
        {
            "tag": "M10",
            "pin": "A2",
            "x": 178.0,
            "y": 107.0,
            "side": "right",
            "text_x": 169.0,
            "text_y": 144.0,
            "ox": 150.0,
            "oy": 114.0,
        }
    ]
    clusters = [{"tag": "LS20", "box": (161.0, 93.0, 179.0, 106.0), "x": 169.4, "y": 95.0}]
    landed = _land((177.1, 102.3), pins, [], [], clusters, (220.0, 102.3))
    assert landed is not None
    assert landed["tag"] == "LS20"
    assert landed.get("provisional") is True


def test_a_pin_keeps_the_end_when_its_own_tag_is_nearer():
    from panelcad.importer.vectors import _land

    pins = [
        {
            "tag": "K1",
            "pin": "A2",
            "x": 100.0,
            "y": 40.0,
            "side": "right",
            "text_x": 96.0,
            "text_y": 48.0,
            "ox": 88.0,
            "oy": 40.0,
        }
    ]
    # The box overlaps the pin. Its tag is farther than K1's own mark.
    clusters = [{"tag": "X9", "box": (96.0, 36.0, 108.0, 44.0), "x": 140.0, "y": 40.0}]
    landed = _land((100.0, 40.0), pins, [], [], clusters, (80.0, 40.0))
    assert landed is not None
    assert landed["tag"] == "K1"
    assert landed["pin"] == "A2"
