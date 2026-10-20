"""From-to wire list and wire-marker exports."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from panelcad import Session
from panelcad.ai.drawing import drawing_agent, open_drawing
from panelcad.cli.agent import Agent
from panelcad.core.electrical import symbol_role
from panelcad.core.from_to import FROM_TO_COLUMNS
from panelcad.examples.dol import build_dol
from panelcad.io.wire_markers import (
    BRADY_DOC_URLS,
    MARKER_COLUMNS,
    PHOENIX_DOC_URLS,
    WEIDMUELLER_DOC_URLS,
)
from panelcad.render.print_package import extract_pdf_text

GOLDEN = Path(__file__).resolve().parent / "golden" / "wire_list"

_EXAMPLES: dict[str, Session] = {}


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n")


def _example(kind: str) -> Session:
    cached = _EXAMPLES.get(kind)
    if cached is not None:
        return cached
    if kind == "dol":
        session = build_dol()
    elif kind == "reversing":
        session = Session.create("Reversing")
        session.insert_circuit(kind="reversing", control="local")
    elif kind == "plc":
        session = Session.create("PLC")
        session.insert_circuit(kind="plc_motor", control="local")
    else:
        raise AssertionError(kind)
    _EXAMPLES[kind] = session
    return session


def _assert_golden(path: Path, name: str) -> None:
    expected = GOLDEN / name
    got = _norm(path.read_text(encoding="utf-8"))
    if os.environ.get("UPDATE_GOLDEN") == "1":
        expected.parent.mkdir(parents=True, exist_ok=True)
        expected.write_text(got, encoding="utf-8")
        return
    assert expected.is_file(), f"missing golden {expected}"
    assert got == _norm(expected.read_text(encoding="utf-8"))


def test_columns_sort_and_terminal_crossing(tmp_path: Path):
    session = Session.create("Xing")
    session.place("iec.contactors.coil", ref="K1", x=20, y=40)
    session.place("iec.terminals.x_2p5", ref="X1-1", x=70, y=40)
    session.place("iec.loads.motor_3ph", ref="M1", x=130, y=40)
    session.place("iec.contactors.coil", ref="K2", x=20, y=100)
    session.place("iec.operators.pilot_light", ref="H2", x=90, y=100)
    session.wire("K1.A2", "X1-1.A", net="COIL", number="10")
    session.wire("X1-1.B", "M1.U1", net="FIELD", number="2")
    session.wire("K2.A2", "H2.X1", net="LAMP", number="2")
    rows = session.from_to_rows(sort="number")
    assert list(rows[0])[: len(FROM_TO_COLUMNS)] == list(FROM_TO_COLUMNS) or set(FROM_TO_COLUMNS) <= set(rows[0])
    numbers = [row["wire_number"] for row in rows]
    assert numbers.index("2") < numbers.index("10")
    by_num = {}
    for row in rows:
        by_num.setdefault(row["wire_number"], []).append(row)
    coil = next(row for row in by_num["10"] if row["from"] == "K1:A2")
    assert coil["to"] == "X1:1"
    assert coil["crossing"] == "X1:1"
    assert "X1-1" not in coil["from"] and "X1-1" not in coil["to"]
    field = next(row for row in by_num["2"] if "M1:U1" in (row["from"], row["to"]))
    assert field["crossing"] == "X1:1"
    assert "X1:1" in (field["from"], field["to"])
    device_order = [row["from"] for row in session.from_to_rows(sort="device")]
    assert device_order.index("K1:A2") < device_order.index("X1:1")

    same = Session.create("Same")
    same.place("iec.contactors.coil", ref="K1", x=20, y=40)
    same.place("iec.terminals.x_2p5", ref="X1-1", x=70, y=40)
    same.place("iec.loads.motor_3ph", ref="M1", x=130, y=40)
    same.wire("K1.A2", "X1-1.A", net="COIL", number="101")
    same.wire("X1-1.B", "M1.U1", net="FIELD", number="101")
    for row in same.from_to_rows():
        assert row["crossing"] == "X1:1"
        assert "X1:1" in (row["from"], row["to"])
    lamp = next(row for row in by_num["2"] if "H2:X1" in (row["from"], row["to"]))
    assert lamp["crossing"] == ""

    dest = tmp_path / "from_to.csv"
    session.export_from_to(dest, sort="number")
    text = dest.read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(FROM_TO_COLUMNS)
    assert "K1:A2" in text
    assert "X1:1" in text


def test_net_and_wire_defaults_round_trip(tmp_path: Path):
    session = Session.create("Spec")
    session.place("iec.contactors.coil", ref="K1", x=20, y=20)
    session.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    session.place("iec.contactors.coil", ref="K2", x=20, y=80)
    session.place("iec.operators.pilot_light", ref="H2", x=80, y=80)
    session.set_net_wire_spec("COIL", size="18", color="RD", wire_type="MTW")
    session.set_net_wire_spec("*", size="16", color="BK", wire_type="MTW")
    coil = session.wire("K1.A2", "H1.X1", net="COIL", gauge="14", wire_type="THHN")
    other = session.wire("K2.A2", "H2.X1", net="LAMP")
    rows = {row["from"]: row for row in session.from_to_rows()}
    assert rows["K1:A2"]["size"] == "14"
    assert rows["K1:A2"]["color"] == "RD"
    assert rows["K1:A2"]["type"] == "THHN"
    assert rows["K2:A2"]["size"] == "16"
    assert rows["K2:A2"]["color"] == "BK"
    assert rows["K2:A2"]["type"] == "MTW"
    assert coil.wire_type == "THHN"
    assert other.gauge == ""
    path = tmp_path / "spec.pcad.yaml"
    session.save(path)
    raw = path.read_text(encoding="utf-8")
    assert "net_wire_specs" in raw
    assert "MTW" in raw
    assert "type: THHN" in raw or "type: THHN\n" in raw
    loaded = Session.open(path)
    again = {row["from"]: row for row in loaded.from_to_rows()}
    assert again["K1:A2"]["size"] == "14"
    assert again["K2:A2"]["type"] == "MTW"
    cleared = loaded.set_net_wire_spec("COIL", size="", color="", wire_type="")
    assert cleared["cleared"] is True
    assert "COIL" not in loaded.project.net_wire_specs


def test_marker_end_count_skips_junction(tmp_path: Path):
    """A marker is one conductor end. A junction dot is not a device or terminal pin."""

    session = Session.create("Node")
    session.place("iec.contactors.coil", ref="K1", x=20, y=20)
    session.place("iec.connection.node", ref="N1", x=55, y=20)
    session.place("iec.operators.pilot_light", ref="H1", x=90, y=20)
    first = session.wire("K1.A2", "N1.A", net="RUN")
    second = session.wire("N1.B", "H1.X1", net="RUN")
    assert first.number and first.number == second.number
    report = session.from_to_rows()
    assert len(report) == 1
    assert {report[0]["from"], report[0]["to"]} == {"K1:A2", "H1:X1"}
    assert "N1" not in report[0]["from"] and "N1" not in report[0]["to"]
    markers = session.wire_marker_rows()
    assert len(markers) == 2 * len(report)
    assert {row["text"] for row in markers} == {first.number}
    assert {row["device"] for row in markers} == {"K1", "H1"}
    assert all(row["device"] and row["pin"] for row in markers)

    blank = Session.create("Blank")
    blank.set_wire_numbering(enabled=False)
    blank.place("iec.contactors.coil", ref="K1", x=20, y=20)
    blank.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    blank.wire("K1.A2", "H1.X1", net="RUN")
    written = blank.export_wire_markers(tmp_path / "blank.csv", style="clip-on")
    assert written["rows"] == 0
    assert written["blank_ends"] == 2
    assert written["style"] == "clip-on"


def _ends(row: dict[str, str]) -> tuple[str, str]:
    return row["from"], row["to"]


def _assert_real_ends(rows: list[dict[str, str]], markers: list[dict[str, str]]) -> None:
    assert rows
    assert len(markers) == 2 * len(rows)
    expected: list[tuple[str, str]] = []
    for row in rows:
        expected.append((row["wire_number"], row["from"]))
        expected.append((row["wire_number"], row["to"]))
    got = [(row["text"], f"{row['device']}:{row['pin']}") for row in markers]
    assert got == expected
    for row in rows:
        for end in _ends(row):
            tag, _, pin = end.partition(":")
            assert tag and pin and ":" in end
            assert end not in {"0V", "24V", "L1", "L2"}
    for marker in markers:
        assert marker["device"] and marker["pin"]


def test_rail_nets_are_point_to_point_and_markers_follow():
    """A rail name is not a termination. Two conductors on one pin print two markers."""

    session = _example("reversing")
    rows = session.from_to_rows()
    markers = session.wire_marker_rows()
    _assert_real_ends(rows, markers)
    zero = [row for row in rows if row["wire_number"] == "0V"]
    landed = {end for row in zero for end in _ends(row)}
    assert {"PS1:minus", "M1:A2", "M2:A2"} <= landed
    hot = [row for row in rows if row["wire_number"] == "24V"]
    hot_ends = {end for row in hot for end in _ends(row)}
    assert "PS1:plus" in hot_ends
    ol = [row for row in markers if row["device"] == "OL1" and row["pin"] == "T1"]
    assert len(ol) == 2
    assert {row["text"] for row in ol} == {"101"}
    for number in ("203", "204", "206"):
        branch = [row for row in markers if row["text"] == number]
        pins = [f"{row['device']}:{row['pin']}" for row in branch]
        assert branch
        assert any(pins.count(pin) >= 2 for pin in pins)
    power = next(row for row in rows if "OL1:T1" in _ends(row))
    assert power["size"] == "14"
    assert power["color"] == "BK"
    assert power["type"] == "MTW"
    # Control conductors are the 16 AWG NEC 430.72 size. The list stores it bare.
    control = next(row for row in rows if "PS1:minus" in _ends(row))
    assert control["size"] == "16"
    assert control["color"] == "BU"
    assert control["type"] == "MTW"


def test_power_color_hook_stays_black_until_phase_colors():
    from panelcad.core.from_to import default_power_color

    project = Session.create("Phase").project
    assert default_power_color(project, number="L1", net="L1", pins=("L1", "T1")) == "BK"
    assert default_power_color(project, number="L2", net="L2", pins=("L2",)) == "BK"
    assert default_power_color(project, number="L3", net="L3", pins=("L3",)) == "BK"


def test_builtin_and_class_spec_fallback():
    session = Session.create("Fallback")
    session.place("iec.contactors.ac3_9a", ref="K1", x=20, y=20)
    session.place("iec.loads.motor_3ph", ref="MTR1", x=120, y=20)
    session.place("iec.operators.pilot_light", ref="H1", x=20, y=90)
    session.wire("K1.T1", "MTR1.U1", net="T1")
    session.wire("K1.A2", "H1.X1", net="RUN")
    rows = {tuple(sorted(_ends(row))): row for row in session.from_to_rows()}
    power = rows[tuple(sorted(("K1:T1", "MTR1:U1")))]
    control = rows[tuple(sorted(("K1:A2", "H1:X1")))]
    assert (power["size"], power["color"], power["type"]) == ("TBD", "BK", "MTW")
    assert (control["size"], control["color"], control["type"]) == ("TBD", "BU", "MTW")

    session.set_net_wire_spec("RUN", color="RD")
    session.set_net_wire_spec("power", size="12", color="BK", wire_type="THHN")
    session.set_net_wire_spec("*", size="16", color="GN", wire_type="MTW")
    again = {tuple(sorted(_ends(row))): row for row in session.from_to_rows()}
    power = again[tuple(sorted(("K1:T1", "MTR1:U1")))]
    control = again[tuple(sorted(("K1:A2", "H1:X1")))]
    assert (power["size"], power["color"], power["type"]) == ("12", "BK", "THHN")
    assert (control["size"], control["color"], control["type"]) == ("16", "RD", "MTW")
    raw = session.project.model_dump(mode="json", by_alias=True)
    assert raw["sheets"][0]["wires"][0].get("gauge", "") in {"", None}
    assert "18" not in str(raw["sheets"][0]["wires"])


def test_unknown_gauge_is_tbd(tmp_path: Path):
    """A blank gauge reports TBD. A stored gauge and an explicit net size stay."""

    from panelcad.core.from_to import WIRE_LIST_SCHEMA

    description = str(WIRE_LIST_SCHEMA["description"])
    assert "TBD" in description
    assert "does not substitute a default size" in description

    session = Session.create("Gauge")
    session.place("iec.contactors.ac3_9a", ref="K1", x=20, y=20)
    session.place("iec.loads.motor_3ph", ref="M1", x=140, y=20)
    session.place("iec.operators.pilot_light", ref="H1", x=20, y=90)
    session.place("iec.contactors.coil", ref="K2", x=20, y=150)
    session.place("iec.operators.pilot_light", ref="H2", x=100, y=150)
    session.place("iec.contactors.coil", ref="K3", x=20, y=210)
    session.place("iec.connection.node", ref="N1", x=70, y=210)
    session.place("iec.operators.pilot_light", ref="H3", x=130, y=210)
    power = session.wire("K1.T1", "M1.U1", net="T1")
    control = session.wire("K1.A2", "H1.X1", net="RUN")
    known = session.wire("K2.A2", "H2.X1", net="LAMP", gauge="14 AWG")
    thick = session.wire("K1.T2", "M1.V1", net="T2", gauge="350 kcmil")
    odd = session.wire("K1.T3", "M1.W1", net="T3", gauge="2/0")
    left = session.wire("K3.A2", "N1.A", net="NODE")
    right = session.wire("N1.B", "H3.X1", net="NODE")
    power.gauge = "   "

    def by_ends() -> dict[tuple[str, str], dict[str, str]]:
        return {tuple(sorted(_ends(row))): row for row in session.from_to_rows()}

    rows = by_ends()
    power_row = rows[tuple(sorted(("K1:T1", "M1:U1")))]
    control_row = rows[tuple(sorted(("K1:A2", "H1:X1")))]
    known_row = rows[tuple(sorted(("K2:A2", "H2:X1")))]
    thick_row = rows[tuple(sorted(("K1:T2", "M1:V1")))]
    odd_row = rows[tuple(sorted(("K1:T3", "M1:W1")))]
    node_row = rows[tuple(sorted(("K3:A2", "H3:X1")))]
    assert power_row["size"] == "TBD"
    assert control_row["size"] == "TBD"
    assert node_row["size"] == "TBD"
    assert known_row["size"] == "14"
    assert thick_row["size"] == "350 kcmil"
    assert odd_row["size"] == "2/0"
    assert (power_row["color"], power_row["type"]) == ("BK", "MTW")
    assert (control_row["color"], control_row["type"]) == ("BU", "MTW")
    assert power.gauge.strip() == ""
    assert control.gauge == ""
    assert left.gauge == "" and right.gauge == ""
    assert known.gauge == "14 AWG"
    dumped = str(session.project.model_dump(mode="json", by_alias=True)["sheets"][0]["wires"])
    assert "TBD" not in dumped
    legacy = {row["wire"]: row["gauge"] for row in session.wire_list_rows()}
    assert legacy[power.id] == "   "
    assert legacy[control.id] == ""

    power.gauge = ""
    left.gauge = "12 AWG"
    assert by_ends()[tuple(sorted(("K1:T1", "M1:U1")))]["size"] == "TBD"
    assert by_ends()[tuple(sorted(("K3:A2", "H3:X1")))]["size"] == "12"
    left.gauge = ""

    session.set_net_wire_spec("*", size="16", color="GN", wire_type="MTW")
    starred = by_ends()
    assert starred[tuple(sorted(("K1:T1", "M1:U1")))]["size"] == "16"
    assert starred[tuple(sorted(("K1:A2", "H1:X1")))]["size"] == "16"
    assert starred[tuple(sorted(("K2:A2", "H2:X1")))]["size"] == "14"
    session.set_net_wire_spec("power", size="12")
    session.set_net_wire_spec("RUN", size="18", color="RD", wire_type="THHN")
    specified = by_ends()
    assert specified[tuple(sorted(("K1:T1", "M1:U1")))]["size"] == "12"
    assert specified[tuple(sorted(("K1:A2", "H1:X1")))]["size"] == "18"
    assert specified[tuple(sorted(("K3:A2", "H3:X1")))]["size"] == "16"
    assert specified[tuple(sorted(("K1:T2", "M1:V1")))]["size"] == "350 kcmil"
    session.set_wire_spec(frm="K2.A2", to="H2.X1", gauge="")
    session.set_net_wire_spec("*", size="")
    session.set_net_wire_spec("power", size="")
    session.set_net_wire_spec("RUN", size="")
    cleared = by_ends()
    assert cleared[tuple(sorted(("K2:A2", "H2:X1")))]["size"] == "TBD"
    assert cleared[tuple(sorted(("K1:T1", "M1:U1")))]["size"] == "TBD"
    assert cleared[tuple(sorted(("K1:A2", "H1:X1")))]["size"] == "TBD"
    assert known.gauge == ""

    dest = tmp_path / "from_to.csv"
    session.export_from_to(dest)
    text = dest.read_text(encoding="utf-8")
    assert ",TBD," in text
    markers = session.wire_marker_rows()
    blank_numbers = {power.number, control.number, known.number, left.number}
    assert left.number == right.number
    seen = {row["text"] for row in markers if row["text"] in blank_numbers}
    assert seen == blank_numbers
    assert all(row["size"] == "TBD" for row in markers if row["text"] in blank_numbers)
    assert all(row["size"] == "350 kcmil" for row in markers if row["text"] == thick.number)

    other = Session.create("Second job")
    other.place("iec.contactors.coil", ref="CR1", x=20, y=20)
    other.place("iec.operators.pilot_light", ref="LT1", x=90, y=20)
    other.wire("CR1.A2", "LT1.X1", net="AUX")
    assert other.from_to_rows()[0]["size"] == "TBD"
    assert other.project.sheets[0].wires[0].gauge == ""


def build_strip_crossing() -> Session:
    """24V stays 24V through X1. A terminal between two nets takes a new number on each side."""

    session = Session.create("Strip crossing")
    sheet = session.project.sheets[0]
    sheet.rail_hot = "24V"
    sheet.rail_return = "0V"
    session.place("iec.power.psu_24v_5a", ref="PS1", x=30, y=30)
    session.place("iec.terminals.x_2p5", ref="X1-1", x=120, y=36)
    session.place("iec.operators.pb_no", ref="PB1", x=200, y=30)
    session.place("iec.contactors.ac3_9a", ref="K1", x=30, y=120)
    session.place("iec.terminals.x_2p5", ref="X1-2", x=120, y=130)
    session.place("iec.terminals.x_2p5", ref="X1-3", x=120, y=210)
    session.place("iec.loads.motor_3ph", ref="MTR1", x=210, y=200)
    # Same net name: the field side of the 24V feed-through keeps 24V.
    session.wire("PS1.plus", "24V", net="24V")
    session.wire("X1-1.A", "24V", net="24V")
    session.wire("X1-1.B", "PB1.13", net="24V")
    session.wire("PS1.minus", "0V", net="0V")
    session.wire("X1-2.A", "0V", net="0V")
    session.wire("K1.A2", "0V", net="0V")
    # A feed-through terminal is one conductor, so both sides share a number.
    session.wire("K1.T1", "X1-3.A", net="T1")
    session.wire("X1-3.B", "MTR1.U1", net="MOTOR")
    return session


def test_strip_crossing_sample_fills_the_column():
    session = build_strip_crossing()
    rows = session.from_to_rows()
    markers = session.wire_marker_rows()
    _assert_real_ends(rows, markers)
    for row in rows:
        terminals = [end for end in _ends(row) if end.startswith("X1:")]
        if terminals:
            for end in terminals:
                assert end in row["crossing"]
        else:
            assert row["crossing"] == ""
    pb = [row for row in rows if "PB1:13" in _ends(row)]
    assert pb
    assert {row["wire_number"] for row in pb} == {"24V"}
    assert all(row["wire_number"] != "12" for row in rows)
    assert all(row["crossing"] == "X1:1" for row in pb)
    hot = [row for row in rows if "PS1:plus" in _ends(row)]
    assert hot and {row["wire_number"] for row in hot} == {"24V"}
    internal = next(row for row in rows if "K1:T1" in _ends(row))
    field = next(row for row in rows if "MTR1:U1" in _ends(row))
    assert internal["wire_number"] == field["wire_number"]
    assert internal["wire_number"].isdigit() and field["wire_number"].isdigit()
    assert internal["crossing"] == field["crossing"] == "X1:3"
    locked = [wire for wire in session.project.sheets[0].wires if wire.net in {"T1", "MOTOR"}]
    assert locked and all(wire.number_locked is False for wire in locked)
    zero_rows = [row for row in rows if "X1:2" in _ends(row)]
    assert zero_rows
    assert all(row["wire_number"] == "0V" and "X1:2" in row["crossing"] for row in zero_rows)
    assert all(row["size"] and row["color"] and row["type"] for row in rows)


def test_contact_sides_are_separate_rows():
    session = _example("reversing")
    found = False
    for sheet in session.project.sheets:
        for comp in sheet.components:
            try:
                device = session.catalog.get(comp.library_id)
            except KeyError:
                continue
            if symbol_role(device) != "contact":
                continue
            landed = []
            for pin in device.pins:
                token = f"{comp.id}.{pin.id}".upper()
                hits = [wire for wire in sheet.wires if wire.frm.upper() == token or wire.to.upper() == token]
                if hits:
                    landed.append(hits[0])
            if len(landed) < 2:
                continue
            numbers = {wire.number for wire in landed[:2]}
            assert "" not in numbers
            assert len(numbers) == 2, (comp.id, [(wire.number, wire.frm, wire.to) for wire in landed])
            report = session.from_to_rows()
            for number in numbers:
                matches = [row for row in report if row["wire_number"] == number]
                assert matches
                assert all(":" in row["from"] or row["from"] for row in matches)
            found = True
            break
        if found:
            break
    assert found


def test_vendor_markers_match_the_documented_shape(tmp_path: Path):
    session = Session.create("Marks")
    session.place("iec.contactors.coil", ref="K1", x=20, y=20)
    session.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    session.wire("K1.A2", "H1.X1", net="RUN", color="BU", gauge="18", wire_type="MTW")
    generic = session.export_wire_markers(tmp_path / "generic.csv", target="generic", style="heat-shrink")
    phoenix = session.export_wire_markers(tmp_path / "phoenix.csv", target="phoenix", style="clip-on")
    weid = session.export_wire_markers(tmp_path / "weid.csv", target="weidmueller", style="heat-shrink")
    brady = session.export_wire_markers(tmp_path / "brady.csv", target="brady", style="heat-shrink")
    generic_text = (tmp_path / "generic.csv").read_text(encoding="utf-8")
    assert generic_text.splitlines()[0] == ",".join(MARKER_COLUMNS)
    assert generic["confidence"] == "panelcad"
    assert generic["rows"] == 2
    assert generic["markers"][0]["text"] == generic["markers"][1]["text"]
    assert "heat-shrink" in generic_text
    assert "MTW" in generic_text
    phoenix_lines = (tmp_path / "phoenix.csv").read_text(encoding="utf-8").splitlines()
    assert phoenix_lines == [row["text"] for row in generic["markers"]]
    assert phoenix["header"] is False
    assert phoenix["confidence"] == "documented"
    assert phoenix["format"] == "phoenix-single-column"
    assert "single column" in phoenix["note"].lower()
    assert phoenix["doc_urls"] == list(PHOENIX_DOC_URLS)
    assert (tmp_path / "weid.csv").read_bytes() == (tmp_path / "generic.csv").read_bytes()
    assert (tmp_path / "brady.csv").read_bytes() == (tmp_path / "generic.csv").read_bytes()
    assert weid["confidence"] == "unverified"
    assert brady["confidence"] == "unverified"
    assert weid["doc_urls"] == list(WEIDMUELLER_DOC_URLS)
    assert brady["doc_urls"] == list(BRADY_DOC_URLS)
    assert "field mapping" in weid["note"].lower()
    assert "first row" in brady["note"].lower()
    clip = session.export_wire_markers(tmp_path / "clip.csv", style="clipon")
    assert clip["style"] == "clip-on"
    assert "clip-on" in (tmp_path / "clip.csv").read_text(encoding="utf-8")


def test_golden_exports_for_dol_reversing_and_plc(tmp_path: Path):
    for kind in ("dol", "reversing", "plc"):
        session = _example(kind)
        report = session.from_to_rows(sort="number")
        assert report
        assert all(set(FROM_TO_COLUMNS) <= set(row) for row in report)
        numbers = [row["wire_number"] for row in report if row["wire_number"]]
        assert numbers == sorted(numbers, key=_number_sort)
        from_to = tmp_path / f"{kind}-from-to.csv"
        generic = tmp_path / f"{kind}-markers.csv"
        phoenix = tmp_path / f"{kind}-phoenix.csv"
        session.export_from_to(from_to, sort="number")
        generic_result = session.export_wire_markers(generic, target="generic", sort="number")
        phoenix_result = session.export_wire_markers(phoenix, target="phoenix", sort="number")
        weid = session.export_wire_markers(tmp_path / f"{kind}-weid.csv", target="weidmueller", sort="number")
        brady = session.export_wire_markers(tmp_path / f"{kind}-brady.csv", target="brady", sort="number")
        assert weid["confidence"] == "unverified"
        assert brady["confidence"] == "unverified"
        assert (tmp_path / f"{kind}-weid.csv").read_bytes() == generic.read_bytes()
        assert (tmp_path / f"{kind}-brady.csv").read_bytes() == generic.read_bytes()
        texts = [line for line in phoenix.read_text(encoding="utf-8").splitlines() if line]
        assert texts == [row["text"] for row in generic_result["markers"]]
        assert phoenix_result["header"] is False
        _assert_golden(from_to, f"{kind}-from-to.csv")
        _assert_golden(generic, f"{kind}-markers-generic.csv")
        _assert_golden(phoenix, f"{kind}-markers-phoenix.csv")
    dol_rows = _example("dol").from_to_rows()
    assert any(row["from"].startswith("M1:") or row["to"].startswith("M1:") for row in dol_rows)
    assert any(row["from_column"] for row in dol_rows)
    rev_rows = _example("reversing").from_to_rows()
    assert any(row["from_line"] for row in rev_rows)
    plc_rows = _example("plc").from_to_rows()
    assert len({row["from_sheet"] for row in plc_rows}) >= 2


def _number_sort(text: str) -> tuple:
    raw = str(text)
    if raw.isdigit():
        return (0, int(raw), 0, "")
    if "-" in raw:
        left, _, right = raw.partition("-")
        if left.isdigit() and right.isdigit():
            return (1, int(left), int(right), "")
    return (2, 0, 0, raw.upper())


def test_from_to_print_section(tmp_path: Path):
    session = _example("reversing")
    plan = session.plan_print_package(sections=["from_to"])
    assert plan["sections"] == ["from_to"]
    dest = tmp_path / "from_to.pdf"
    written = session.print_package(dest, sections=["from_to"])
    assert dest.is_file()
    assert "from_to" in written["order"]
    text = extract_pdf_text(dest, 0)
    assert "FROM" in text.upper()
    sample = next(row["wire_number"] for row in session.from_to_rows() if row["wire_number"])
    assert sample in text
    assert "MTW" in text
    assert "18" in text or "14" in text


def test_jsonl_schema_and_commands(tmp_path: Path):
    agent = Agent()
    created = agent.handle({"id": 1, "op": "create_project", "name": "AgentFT"})
    assert created["ok"]
    help_result = agent.handle({"id": 2, "op": "help"})
    schemas = help_result["result"]["schemas"]
    wire_schema = schemas["wire_list"]
    marker_schema = schemas["export_wire_markers"]
    assert wire_schema["columns"] == list(FROM_TO_COLUMNS)
    assert "conductor" in str(wire_schema["description"]).lower()
    assert "phoenix" in str(marker_schema["description"]).lower()
    assert marker_schema["doc_urls"]["weidmueller"] == list(WEIDMUELLER_DOC_URLS)
    assert "export_wire_markers" in help_result["result"]["ops"]
    assert "set_net_wire_spec" in help_result["result"]["ops"]
    assert agent.handle({"id": 3, "op": "place", "library_id": "iec.contactors.coil", "ref": "K1", "x": 20, "y": 20})["ok"]
    assert agent.handle(
        {"id": 4, "op": "place", "library_id": "iec.operators.pilot_light", "ref": "H1", "x": 80, "y": 20}
    )["ok"]
    wired = agent.handle({"id": 5, "op": "wire", "from": "K1.A2", "to": "H1.X1", "net": "RUN", "type": "MTW"})
    assert wired["ok"], wired
    assert wired["result"]["type"] == "MTW"
    spec = agent.handle({"id": 6, "op": "set_net_wire_spec", "net": "RUN", "size": "18", "color": "BU", "type": "MTW"})
    assert spec["ok"], spec
    legacy = agent.handle({"id": 7, "op": "wire_list"})
    assert legacy["ok"]
    assert legacy["result"]["rows"][0]["from_device"] == "K1"
    assert legacy["result"]["from_to"][0]["from"] == "K1:A2"
    assert legacy["result"]["from_to"][0]["size"] == "18"
    listed = agent.handle({"id": 8, "op": "wire_list", "from_to": True, "sort": "device"})
    assert listed["result"]["rows"][0]["wire_number"]
    assert "from_device" not in listed["result"]["rows"][0]
    dest = tmp_path / "agent.csv"
    exported = agent.handle(
        {"id": 9, "op": "export_wire_markers", "path": str(dest), "target": "phoenix", "style": "clip-on"}
    )
    assert exported["ok"], exported
    assert exported["result"]["confidence"] == "documented"
    assert dest.read_text(encoding="utf-8").strip()
    missing = agent.handle({"id": 10, "op": "export_wire_markers"})
    assert not missing["ok"]


def test_cli_from_to_and_markers(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    session = Session.create("CLI")
    session.place("iec.contactors.coil", ref="K1", x=20, y=20)
    session.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    session.wire("K1.A2", "H1.X1", net="RUN")
    src = tmp_path / "job.pcad.yaml"
    session.save(src)
    runner = CliRunner()
    from_to = tmp_path / "from_to.csv"
    result = runner.invoke(app, ["electrical", "from-to", str(src), "--out", str(from_to), "--sort", "device"])
    assert result.exit_code == 0, result.output
    assert "K1:A2" in from_to.read_text(encoding="utf-8")
    spec = runner.invoke(
        app,
        ["electrical", "net-spec", str(src), "--net", "RUN", "--size", "18", "--color", "BU", "--type", "MTW"],
    )
    assert spec.exit_code == 0, spec.output
    markers = tmp_path / "markers.csv"
    marked = runner.invoke(
        app,
        [
            "electrical",
            "wire-markers",
            str(src),
            "--out",
            str(markers),
            "--target",
            "phoenix",
            "--style",
            "heat-shrink",
        ],
    )
    assert marked.exit_code == 0, marked.output
    assert "documented" in marked.output
    assert markers.read_text(encoding="utf-8").strip()


def test_drawing_agent_wire_tools_describe_the_report(tmp_path: Path):
    tools: dict[str, object] = {}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools.update({tool.name: tool for tool in info.function_tools})
        return ModelResponse(parts=[TextPart(content="ready")])

    drawing = open_drawing(name="Wire tools")
    with drawing_agent.override(model=FunctionModel(model)):
        result = drawing_agent.run_sync("List wire reports.", deps=drawing)
    assert result.output == "ready"
    listed = tools["wire_list"]
    markers = tools["export_wire_markers"]
    list_desc = (getattr(listed, "description", None) or "").lower()
    mark_desc = (getattr(markers, "description", None) or "").lower()
    assert "conductor" in list_desc
    assert "contact" in list_desc
    assert "marker" in mark_desc
    list_props = listed.parameters_json_schema["properties"]
    mark_props = markers.parameters_json_schema["properties"]
    assert "sort" in list_props and "device" in str(list_props["sort"].get("description") or "").lower()
    assert "phoenix" in str(mark_props["target"].get("description") or "").lower()
    assert "heat-shrink" in str(mark_props["style"].get("description") or "").lower()
    assert "path" in set(markers.parameters_json_schema.get("required") or [])
    dest = tmp_path / "agent_from_to.csv"
    drawing.operator.handle({"op": "place", "library_id": "iec.contactors.coil", "ref": "K1", "x": 20, "y": 20})
    drawing.operator.handle({"op": "place", "library_id": "iec.operators.pilot_light", "ref": "H1", "x": 80, "y": 20})
    drawing.operator.handle({"op": "wire", "from": "K1.A2", "to": "H1.X1", "net": "RUN"})
    calls = {"n": 0}

    def call_tools(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        from pydantic_ai.messages import ToolCallPart

        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="wire_list",
                        args={"path": str(dest), "sort": "number"},
                        tool_call_id="wl",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="Wrote the from-to list.")])

    with drawing_agent.override(model=FunctionModel(call_tools)):
        drawing_agent.run_sync("Write the from-to list.", deps=drawing)
    text = dest.read_text(encoding="utf-8")
    assert "K1:A2" in text
    assert text.splitlines()[0].startswith("wire_number")


def test_gui_reports_and_export_dialog(tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QDialog

    from panelcad.gui.main_window import MainWindow
    from panelcad.gui.wire_dialogs import FromToDialog, NetWireDialog, WireMarkerDialog, WireSpecDialog

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Session.create("Menus"))
    window.session.place("iec.contactors.coil", ref="K1", x=20, y=20)
    window.session.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    window.session.wire("K1.A2", "H1.X1", net="RUN")
    window.show()
    app.processEvents()

    def menu_named(title: str):
        for action in window.menuBar().actions():
            menu = action.menu()
            if menu and menu.title().replace("&", "") == title:
                return menu
        raise AssertionError(title)

    report_titles = [action.text().replace("&", "") for action in menu_named("Reports").actions()]
    assert "Wire List (From-To)…" in report_titles
    assert "Net wire defaults…" in report_titles
    export = None
    for action in menu_named("File").actions():
        child = action.menu()
        if child and action.text().replace("&", "") == "Export":
            export = child
    assert export is not None
    assert "Wire Markers…" in [action.text().replace("&", "") for action in export.actions()]

    from panelcad.gui.wire_dialogs import markers_doc_path

    dialog = WireMarkerDialog(suggested="wire_markers.csv")
    dialog.target.setCurrentIndex(dialog.target.findData("phoenix"))
    dialog.style.setCurrentIndex(dialog.style.findData("clip-on"))
    vals = dialog.values()
    assert vals["target"] == "phoenix"
    assert vals["style"] == "clip-on"
    summary = dialog.summary.text()
    assert "Phoenix Contact: one column, no header (documented)" in summary
    assert "Weidmüller, Brady: generic CSV, header unverified" in summary
    assert "http" not in summary
    assert "WIRE_MARKERS.md" in dialog.details.toolTip()
    doc = markers_doc_path()
    assert doc is not None and doc.is_file()
    dialog.close()

    from_to = FromToDialog(suggested="from_to.csv")
    from_to.sort.setCurrentIndex(from_to.sort.findData("device"))
    assert from_to.values()["sort"] == "device"
    from_to.close()

    spec = WireSpecDialog(size="18", color="BU", wire_type="MTW")
    assert spec.values()["type"] == "MTW"
    spec.close()
    nets = NetWireDialog(nets=["RUN"])
    assert nets.values()["net"] == "*"
    nets.close()

    from_path = tmp_path / "gui_from_to.csv"
    marker_path = tmp_path / "gui_markers.csv"

    def accept_from_to(self):
        self.path.setText(str(from_path))
        self.sort.setCurrentIndex(self.sort.findData("number"))
        return QDialog.DialogCode.Accepted

    def accept_markers(self):
        self.path.setText(str(marker_path))
        self.target.setCurrentIndex(self.target.findData("phoenix"))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(FromToDialog, "exec", accept_from_to)
    window.export_from_to()
    assert "K1:A2" in from_path.read_text(encoding="utf-8")
    monkeypatch.setattr(WireMarkerDialog, "exec", accept_markers)
    window.export_wire_markers()
    marker_text = marker_path.read_text(encoding="utf-8").strip()
    assert marker_text
    assert "text,end" not in marker_text
    window.close()


def test_net_star_survives_windows_glob(tmp_path: Path, monkeypatch, capsys):
    """``--net *`` is a net name. On Windows, Click would expand ``*`` to cwd files."""

    import sys

    import click.core
    import click.utils
    import typer.core
    from typer.main import get_command

    from panelcad.cli.main import app

    session = Session.create("Star")
    job = tmp_path / "job.pcad.yaml"
    session.save(job)
    for name in ("from-to.out.txt", "from_to.csv", "from_to_device.csv"):
        (tmp_path / name).write_text("x\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    expanded = click.utils._expand_args(["*"])
    assert "from-to.out.txt" in expanded
    assert "from_to.csv" in expanded
    assert "job.pcad.yaml" in expanded
    assert "*" not in expanded

    monkeypatch.setattr(sys, "argv", [
        "panelcad",
        "electrical",
        "net-spec",
        str(job),
        "--net",
        "*",
        "--size",
        "16",
        "--color",
        "BK",
        "--type",
        "MTW",
    ])
    if os.name != "nt":
        class _NtOs:
            """Click and Typer consult their own ``os`` module for the Windows glob."""

            name = "nt"

            def __getattr__(self, item: str):
                return getattr(os, item)

        proxy = _NtOs()
        monkeypatch.setattr(click.core, "os", proxy)
        monkeypatch.setattr(typer.core, "os", proxy)
    command = get_command(app)
    with pytest.raises(SystemExit) as expanded_star:
        command.main(args=None, prog_name="panelcad", windows_expand_args=True)
    assert expanded_star.value.code not in (0, None)
    captured = capsys.readouterr()
    complaint = f"{captured.out}\n{captured.err}"
    assert "unexpected extra argument" in complaint.lower()
    # The first expanded name is consumed as the --net value. The rest are extras.
    names = ("from-to.out.txt", "from_to.csv", "from_to_device.csv", "job.pcad.yaml")
    assert sum(name in complaint for name in names) >= 2

    with pytest.raises(SystemExit) as kept:
        app()
    assert kept.value.code in (0, None)
    echo = capsys.readouterr().out
    assert "net *" in echo
    loaded = Session.open(job)
    spec = loaded.project.net_wire_specs["*"]
    assert spec.size == "16"
    assert spec.color == "BK"
    assert spec.wire_type == "MTW"
    assert "all" not in loaded.project.net_wire_specs


def test_net_all_stores_star(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    session = Session.create("All")
    stored = session.set_net_wire_spec("ALL", size="14", color="BK", wire_type="MTW")
    assert stored["net"] == "*"
    assert "all" not in session.project.net_wire_specs
    src = tmp_path / "job.pcad.yaml"
    session.project.net_wire_specs.clear()
    session.save(src)
    runner = CliRunner()
    spec = runner.invoke(
        app,
        ["electrical", "net-spec", str(src), "--net", "all", "--size", "16", "--color", "BU", "--type", "MTW"],
    )
    assert spec.exit_code == 0, spec.output
    assert "net *" in spec.output
    loaded = Session.open(src)
    assert loaded.project.net_wire_specs["*"].size == "16"
    assert loaded.project.net_wire_specs["*"].color == "BU"
    assert "all" not in loaded.project.net_wire_specs
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "AgentAll"})["ok"]
    marked = agent.handle({"id": 2, "op": "set_net_wire_spec", "net": "all", "size": "12", "type": "MTW"})
    assert marked["ok"], marked
    assert marked["result"]["net"] == "*"


@pytest.mark.confirm_generic_layout
def test_csv_exports_have_no_doubled_crlf(tmp_path: Path, monkeypatch):
    """Windows text mode turns csv's ``\\r\\n`` into ``\\r\\r\\n`` unless newline=''."""

    import csv

    from panelcad.library.parts import CatalogPart, write_parts_csv

    session = Session.create("CSV endings")
    session.place("iec.contactors.coil", ref="K1", x=20, y=20)
    session.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    session.wire("K1.A2", "H1.X1", net="RUN")
    session.place("iec.plc.compact_16dio", ref="A1", x=40, y=80)
    session.place("iec.operators.pb_no", ref="S1", x=160, y=80)
    session.wire("S1.13", "A1.I0_0", net="START")
    session.place("iec.terminals.x_2p5", ref="X1-1", x=40, y=140)
    session.open_terminal_strip("X1")

    real_open = Path.open

    def open_like_windows(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        writing = any(flag in mode for flag in ("w", "a", "x", "+"))
        if "b" in mode or not writing or newline is not None:
            return real_open(self, mode, buffering, encoding, errors, newline)
        handle = real_open(self, mode, buffering, encoding, errors, newline="")

        class _Translated:
            """Stand in for Windows text mode: each ``\\n`` becomes ``\\r\\n``."""

            def write(self, data):
                if isinstance(data, str):
                    data = data.replace("\n", "\r\n")
                return handle.write(data)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                handle.close()
                return False

            def __getattr__(self, name):
                return getattr(handle, name)

        return _Translated()

    monkeypatch.setattr(Path, "open", open_like_windows)
    probe = tmp_path / "probe.csv"
    with probe.open("w", encoding="utf-8") as handle:
        csv.writer(handle).writerow(["a", "b"])
    assert b"\r\r\n" in probe.read_bytes()

    bom = tmp_path / "bom.csv"
    io_list = tmp_path / "io.csv"
    wires = tmp_path / "wires.csv"
    from_to = tmp_path / "from_to.csv"
    generic = tmp_path / "markers.csv"
    phoenix = tmp_path / "phoenix.csv"
    strip = tmp_path / "strip.csv"
    parts = tmp_path / "parts.csv"
    session.export_bom(bom)
    session.export_io_list(io_list)
    session.export_wire_list(wires)
    session.export_from_to(from_to)
    session.export_wire_markers(generic, target="generic")
    session.export_wire_markers(phoenix, target="phoenix")
    session.export_terminal_strip(strip, "X1", "phoenix")
    write_parts_csv(
        parts,
        [
            CatalogPart(
                manufacturer="Wago",
                catalog="2002-1201",
                library_id="iec.terminals.x_2p5",
                category="connection.terminal",
                verified="partial",
                source="https://www.wago.com/us/2002-1201",
            )
        ],
    )

    from panelcad.core.plc_tags import export_plc_tags
    from panelcad.render.revision_report import write_compare_csv

    studio = tmp_path / "studio5000.csv"
    tia_csv = tmp_path / "tia.csv"
    cxp = tmp_path / "cxprogrammer.txt"
    compare = tmp_path / "revisions.csv"
    schedule = tmp_path / "cables.csv"
    pull = tmp_path / "pull.csv"
    terminations = tmp_path / "terminations.csv"
    export_plc_tags(session.project, session.catalog, studio, target="studio5000")
    export_plc_tags(session.project, session.catalog, tia_csv, target="tia_csv")
    export_plc_tags(session.project, session.catalog, cxp, target="cxprogrammer")
    write_compare_csv(compare, {"rows": []})
    empty = Session.create("No field cables")
    empty.export_cable_schedule(schedule)
    empty.export_cable_pull_list(pull)
    empty.export_cable_terminations(terminations)
    for path in (schedule, pull, terminations):
        text = path.read_bytes()
        assert text.endswith(b"\r\n"), path.name
        assert text.count(b"\n") == 1, path.name

    crlf_once = (
        bom,
        io_list,
        wires,
        parts,
        from_to,
        generic,
        phoenix,
        strip,
        studio,
        tia_csv,
        cxp,
        compare,
        schedule,
        pull,
        terminations,
    )
    for path in crlf_once:
        data = path.read_bytes()
        assert data, path.name
        assert b"\r\r\n" not in data, path.name
        assert b"\r\n" in data, path.name
        assert data.endswith(b"\n"), path.name
