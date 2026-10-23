"""Device copies on network / overview / rack sheets are references, not duplicate tags."""

from pathlib import Path

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.references import is_overview_sheet, reference_links
from panelcad.core.xref_index import device_index


def _dups(session: Session) -> list[dict[str, str]]:
    return [row for row in session.design_checks() if row["check"] == "duplicate_device_tag"]


def _comms() -> Session:
    s = Session.create("Comms")
    s.place("iec.hmi.panel_7in", ref="HMI1", x=140, y=40)
    s.place("iec.plc.compact_16dio", ref="PLC1", x=240, y=40)
    s.add_sheet("lan", title="LAN", kind="network")
    s.set_network(name="LAN", medium="ethernet", protocol="Ethernet")
    s.layout_network(sheet_id="lan", medium="ethernet")
    return s


def test_layout_network_copies_are_references_not_duplicates():
    s = _comms()
    lan = s.sheet("lan")
    assert {c.id for c in lan.components} >= {"HMI1", "PLC1"}
    assert is_overview_sheet(lan)
    assert _dups(s) == []
    links = {(link.sheet.id, link.comp.id): link for link in reference_links(s.project, s.catalog)}
    assert links[("lan", "PLC1")].primary_sheet.id == "main"
    assert links[("lan", "PLC1")].reason == "overview"


def test_index_lists_reference_with_see_location():
    s = _comms()
    rows = device_index(s.project, s.catalog, "PLC1")["elements"]
    ref_rows = [row for row in rows if row["element"] == "reference"]
    assert len(ref_rows) == 1
    assert ref_rows[0]["sheet"] == "lan"
    assert ref_rows[0]["see_sheet"] == "main"
    assert ref_rows[0]["see"].startswith("1-")


def test_network_print_shows_see_sheet(tmp_path: Path):
    s = _comms()
    svg = s.export_svg(tmp_path / "lan.svg", sheet_id="lan").read_text(encoding="utf-8")
    assert "SEE 1-" in svg


def test_true_duplicate_on_ordinary_sheets_still_flagged():
    s = _comms()
    s.add_sheet("two", title="Controls", kind="schematic")
    s.place("iec.plc.compact_16dio", ref="PLC1", x=40, y=40, sheet_id="two")
    rows = _dups(s)
    assert len(rows) == 1
    assert rows[0]["ref"] == "PLC1"
    # The network copy is still a reference and is not named in the finding.
    assert "lan" not in rows[0]["target"]


def test_overview_word_and_flag_mark_overview_sheets():
    s = Session.create("Over")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.add_sheet("ov", title="System overview", kind="schematic")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20, sheet_id="ov")
    assert _dups(s) == []
    s.add_sheet("blk", title="Block diagram", kind="schematic")
    s.place("iec.contactors.coil", ref="K2", x=20, y=20, sheet_id="main")
    s.place("iec.contactors.coil", ref="K2", x=20, y=20, sheet_id="blk")
    assert [row["ref"] for row in _dups(s)] == ["K2"]
    s.set_sheet_overview(True, sheet_id="blk")
    assert _dups(s) == []
    s.undo()
    assert [row["ref"] for row in _dups(s)] == ["K2"]


def test_overview_flag_round_trips_and_is_omitted_when_false(tmp_path: Path):
    s = Session.create("Rt")
    s.add_sheet("ov", title="Rack", kind="schematic")
    s.set_sheet_overview(True, sheet_id="ov")
    path = tmp_path / "rt.pcad.yaml"
    s.save(path)
    text = path.read_text(encoding="utf-8")
    assert "overview: true" in text
    again = Session.open(path)
    assert again.sheet("ov").overview is True
    assert again.sheet("main").overview is False
    assert text.count("overview:") == 1


def test_mark_reference_keeps_wires_and_clears_finding():
    s = Session.create("Mark")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.add_sheet("two", title="Controls 2", kind="schematic")
    s.place("iec.contactors.coil", ref="K1", x=60, y=40, sheet_id="two")
    s.place("iec.operators.pb_no", ref="S1", x=20, y=40, sheet_id="two")
    s.wire("S1.14", "K1.A1", sheet_id="two")
    before = [w.model_dump() for w in s.sheet("two").wires]
    assert _dups(s)
    out = s.mark_reference("K1", sheet_id="two")
    assert out["reference_of"] == "main"
    comp = s.sheet("two").component("K1")
    assert comp.library_id == "iec.contactors.coil"
    assert (comp.x, comp.y) == (60, 40)
    assert [w.model_dump() for w in s.sheet("two").wires] == before
    assert _dups(s) == []
    s.clear_reference("K1", sheet_id="two")
    assert _dups(s)


def test_mark_reference_to_wrong_sheet_is_refused_and_bad_target_warns():
    s = Session.create("Bad")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.add_sheet("two", kind="schematic")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20, sheet_id="two")
    s.add_sheet("three", kind="schematic")
    try:
        s.mark_reference("K1", sheet_id="two", primary_sheet="three")
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected KeyError")
    s.sheet("two").component("K1").properties["reference_of"] = "three"
    rows = [row for row in s.design_checks() if row["check"] == "reference_target_missing"]
    assert rows and rows[0]["ref"] == "K1"


def test_agent_mark_reference_op():
    agent = Agent()
    agent.handle({"id": 1, "op": "create_project", "name": "A"})
    agent.handle({"id": 2, "op": "place", "library_id": "iec.contactors.coil", "ref": "K1", "x": 20, "y": 20})
    agent.handle({"id": 3, "op": "add_sheet", "sheet_id": "two", "kind": "schematic"})
    agent.handle({"id": 4, "op": "place", "library_id": "iec.contactors.coil", "ref": "K1", "x": 20, "y": 20, "sheet": "two"})
    r = agent.handle({"id": 5, "op": "mark_reference", "ref": "K1", "sheet": "two"})
    assert r["ok"], r
    assert r["result"]["reference_of"] == "main"
    r = agent.handle({"id": 6, "op": "set_sheet_overview", "sheet": "two", "overview": "false"})
    assert r["ok"] and r["result"]["overview"] is False


def test_rack_view_of_io_module_is_a_reference():
    s = Session.create("Rack")
    s.place("iec.plc.compact_16dio", ref="U4", x=40, y=40)  # rack drawing on an ordinary sheet
    s.add_sheet("io4", title="Slot 4 inputs", kind="io")
    s.place("iec.plc.compact_16dio", ref="U4", x=200, y=30, sheet_id="io4")
    assert _dups(s) == []
    link = next(link for link in reference_links(s.project, s.catalog) if link.comp.id == "U4")
    assert link.sheet.id == "main" and link.primary_sheet.id == "io4" and link.reason == "rack"


def test_overview_word_in_description_only_is_not_overview():
    from panelcad.core.models import Sheet
    from panelcad.core.references import is_overview_sheet

    assert not is_overview_sheet(Sheet(id="S9", title="Motor controls", description="see overview on sheet 1"))
    assert is_overview_sheet(Sheet(id="S8", title="Network Overview"))
    assert not is_overview_sheet(Sheet(id="S7", title="Overviews and notes"))
