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
    # Off-page primary-feed stubs carry the arrow callout, not a sheet-line number.
    for wire in elementary.wires:
        ends = {str(wire.frm).split(".")[0], str(wire.to).split(".")[0]}
        offpage = any(
            "offpage" in str(getattr(comp, "library_id", "") or "").lower()
            for comp in elementary.components
            if comp.id in ends
        )
        if offpage:
            continue
        assert str(wire.number or "").strip(), f"{wire.frm}->{wire.to}"
    assert xref_annotations(session.project, session.catalog, elementary)

    ids = [comp.id for comp in elementary.components]
    assert ids.index("FU1") < ids.index("ES1") < ids.index("PB1") < ids.index("M1")
    assert "T1" in ids
    assert "FU3" in ids
    ends = {(str(w.frm), str(w.to)) for w in elementary.wires}
    assert ("T1.X1", "FU3.1") in ends or ("FU3.1", "T1.X1") in ends
    assert ("FU3.2", "L1") in ends or ("L1", "FU3.2") in ends
    assert ("T1.H4", "FU2.2") in ends or ("FU2.2", "T1.H4") in ends
    assert not any({"FU1.1", "L1"} <= {str(w.frm), str(w.to)} for w in elementary.wires)
    assert not any({"FU2.1", "L2"} <= {str(w.frm), str(w.to)} for w in elementary.wires)

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
