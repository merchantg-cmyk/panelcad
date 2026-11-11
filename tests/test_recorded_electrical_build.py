"""Checker findings on the real electrical-OCR builds from the home PC.

The counts are the no-vision baselines at 0d138f8: 13, 15, and 17 wires.
They are not a synthetic ladder. Phone is two wires under the installed
0.5.26 count of 19 because those two wires are the self-shorts FU2.1->FU2.2
and ES1.1->ES1.2, which this checker now names and will not issue.
"""

from __future__ import annotations

import json
from pathlib import Path

from panelcad.api.session import Session
from panelcad.core.elementary_layout import elementary_layout_issues
from panelcad.importer.cleanup import remaining_gaps

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "scan_ladder" / "recorded-electrical-0526.json"
COUNTS = {"clean_300": 13, "dpi_150": 15, "phone": 17}
RETURNS = {"clean_300": "X2", "dpi_150": "L2", "phone": "X2"}


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _replay(name: str, recorded: dict) -> Session:
    session = Session.create(name, sheet_id="page-001", title="Elementary")
    session.set_title_block(
        company="Demo Controls",
        drawing_number="E-100",
        standard="NEMA/ANSI",
        drawing_style="ladder",
        coil_style="circle",
        drawn_by="Demo",
    )
    block = recorded["fixtures"][name]
    first = True
    for sheet_id, sheet_info in block["sheets"].items():
        if first:
            sheet = session.project.sheet("page-001")
            first = False
        else:
            sheet = session.add_sheet(sheet_id, title=sheet_id)
        sheet.page.size = "TABLOID"
        sheet.page.orientation = "landscape"
        sheet.drawing_style = "ladder"
        sheet.rail_hot = "L1"
        sheet.rail_return = RETURNS[name]
        for tag, library_id in sheet_info["components"]:
            session.place(library_id, ref=tag, x=40, y=40, sheet_id=sheet.id, snap=False)
        for index, (frm, to, number) in enumerate(sheet_info["wires"], start=1):
            session.wire(frm, to, number=number, sheet_id=sheet.id, wire_id=f"{sheet.id}_w{index}")
    return session


def test_recorded_real_reads_match_the_home_pc_counts():
    recorded = _load()
    assert recorded["floors_0_5_26"]["phone"] == 19
    assert recorded["phone_omitted_self_shorts"] == [["FU2.1", "FU2.2", "?"], ["ES1.1", "ES1.2", "105"]]
    for name, count in COUNTS.items():
        block = recorded["fixtures"][name]
        assert block["wire_total"] == count
        wires = sum(len(sheet["wires"]) for sheet in block["sheets"].values())
        assert wires == count


def test_recorded_real_reads_name_the_self_short_the_dead_rung_and_the_rail_fuse():
    recorded = _load()
    findings: dict[str, list[str]] = {}
    for name in COUNTS:
        session = _replay(name, recorded)
        found: list[str] = []
        for sheet in session.project.sheets:
            if sheet.kind != "schematic":
                continue
            found.extend(elementary_layout_issues(session, sheet))
        findings[name] = found
        text = "\n".join(found)
        assert "with no load" in text, found
        assert "M1-13" in text and "M1-23" in text
        gaps = remaining_gaps(session)
        assert any(gap["kind"] == "contacts_only" for gap in gaps)
        if name == "clean_300":
            assert any("connects FU2's own pins" in item for item in found), found
            assert any(gap["kind"] == "self_wire" for gap in gaps)
        if name == "dpi_150":
            assert any("connects X1-3's own pins" in item for item in found), found
            assert any(gap["kind"] == "self_wire" for gap in gaps)
        if name == "phone":
            assert any("FU1 is a fuse from L1 to X2" in item for item in found), found
            assert any(gap["kind"] == "fuse_rail" for gap in gaps)
            assert not any("connects FU2's own pins" in item or "connects ES1's own pins" in item for item in found)
    assert findings["phone"]


def _route_facts(session, sheet) -> dict:
    from panelcad.render.wiring import clear_route_cache, route_sheet_wires

    clear_route_cache()
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    return {
        wire_id: [(round(point.x, 3), round(point.y, 3)) for point in points]
        for wire_id, points in sorted(routes.items())
    }


def test_retag_and_number_edits_do_not_change_routing():
    from panelcad.core.elementary_layout import layout_elementary
    from panelcad.importer.cleanup import _rename_tag

    recorded = _load()
    for name in ("clean_300", "dpi_150"):
        session = _replay(name, recorded)
        sheet = session.project.sheet("page-001")
        layout_elementary(session, sheet)
        before = _route_facts(session, sheet)
        numbered = next(wire for wire in sheet.wires if str(wire.number or "") not in {"", "?"})
        numbered.number = "107"
        assert _route_facts(session, sheet) == before
        numbered.number = "107"
        target = next(comp for comp in sheet.components if comp.id not in {"L1", "L2"})
        assert _rename_tag(session, target.id, f"{target.id}X", "")
        assert _route_facts(session, sheet) == before


def test_adding_fu1_and_t1_to_clean_300_does_not_route_through_a_symbol():
    from panelcad.core.elementary_layout import elementary_layout_issues, layout_elementary

    recorded = _load()
    session = _replay("clean_300", recorded)
    sheet = session.project.sheet("page-001")
    layout_elementary(session, sheet)
    before = {item for item in elementary_layout_issues(session, sheet) if "runs through a symbol" in item}
    session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=40, snap=False)
    session.place("nema.power.control_transformer", ref="T1", x=90, y=40, snap=False)
    session.wire("L1", "FU1.1", number="101", sheet_id=sheet.id)
    session.wire("FU1.2", "T1.H1", number="102", sheet_id=sheet.id)
    session.wire("T1.X1", "X2", number="103", sheet_id=sheet.id)
    layout_elementary(session, sheet)
    after = {item for item in elementary_layout_issues(session, sheet) if "runs through a symbol" in item}
    assert not (after - before)


def test_dpi_150_redraw_keeps_the_same_findings():
    from panelcad.core.elementary_layout import elementary_layout_issues, layout_elementary

    recorded = _load()
    session = _replay("dpi_150", recorded)
    sheet = session.project.sheet("page-001")
    layout_elementary(session, sheet)
    first = elementary_layout_issues(session, sheet)
    numbered = next(wire for wire in sheet.wires if str(wire.number or "") in {"", "?"})
    numbered.number = "107"
    layout_elementary(session, sheet)
    second = elementary_layout_issues(session, sheet)
    assert second == first
