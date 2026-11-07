"""The scan-ladder sources are drafter-style sheets, and they pass the sheet rules."""

from panelcad.core.ansi_drafting import ansi_drafting_issues
from panelcad.core.terminal_strip import layout_terminal_strip
from panelcad.core.xref import xref_annotations
from panelcad.examples.scan_ladder_demo import build_scan_ladder_demo, layout_problems


def test_demo_sheets_pass_ansi_and_sit_on_rails_clear_of_duct():
    session = build_scan_ladder_demo()
    issues = ansi_drafting_issues(session.project, session.catalog)
    assert issues == [], issues
    assert layout_problems(session) == []

    elementary = session.project.sheet("elementary")
    assert elementary.page.size == "TABLOID"
    assert all(str(wire.number or "").strip() for wire in elementary.wires)
    assert xref_annotations(session.project, session.catalog, elementary)

    ids = [comp.id for comp in elementary.components]
    assert ids.index("FU1") < ids.index("ES1") < ids.index("PB1") < ids.index("M1")
    assert "T1" in ids

    backplate = session.project.sheet("backplate")
    assert [comp.id for comp in backplate.components[:3]] == ["DISC1", "FU1", "M1"]
    assert backplate.components[-1].id == "X1-4"
    assert backplate.rails and backplate.trays

    blocks = [
        item
        for item in layout_terminal_strip(session.project, session.catalog, "X1")["items"]
        if item["kind"] == "terminal"
    ]
    assert [item["ref"] for item in blocks] == ["X1-1", "X1-2", "X1-3", "X1-4"]
    assert blocks[-1]["pe"] is True
    for item in blocks:
        assert item["internal"] and item["external"]
        assert item["internal"] != item["external"]
