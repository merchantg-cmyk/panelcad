"""ANSI / JIC sheet rules: sizes, title block, symbols, rungs, wires, cross-references."""

from __future__ import annotations

from panelcad.core.ansi_drafting import ANSI_PAGE_NAMES, ansi_drafting_issues, jic_prefix
from panelcad.core.geometry import PAGE_SIZES_MM, match_page_size
from panelcad.examples.xref_demo import build_nema_seal_in
from panelcad.render.titleblock import BORDER_STROKE, RAIL_STROKE, RUNG_NUMBER_MM, ZONE_LABEL_MM
from panelcad.render.wire_labels import MARK_SIZE
from panelcad.render.xref import XREF_SIZE


def test_ansi_sheet_names_match_the_inch_sizes():
    assert ANSI_PAGE_NAMES["ANSI A"] == "LETTER"
    assert ANSI_PAGE_NAMES["ANSI B"] == "TABLOID"
    assert PAGE_SIZES_MM["LETTER"] == (215.9, 279.4)
    assert PAGE_SIZES_MM["TABLOID"] == (279.4, 431.8)
    assert PAGE_SIZES_MM["ANSI_C"] == (431.8, 558.8)
    assert PAGE_SIZES_MM["ANSI_D"] == (558.8, 863.6)
    assert PAGE_SIZES_MM["ANSI_E"] == (863.6, 1117.6)
    assert match_page_size(431.8, 558.8) == ("ANSI_C", "portrait")
    assert match_page_size(863.6, 558.8) == ("ANSI_D", "landscape")
    assert match_page_size(1117.6, 863.6) == ("ANSI_E", "landscape")
    # 11×17 stays tabloid. It does not collapse to A3.
    assert match_page_size(279.4, 431.8) == ("TABLOID", "portrait")
    assert match_page_size(420.0, 297.0) == ("A3", "landscape")


def test_jic_prefix_does_not_split_a_longer_name():
    assert jic_prefix("PB1") == "PB"
    assert jic_prefix("MTR1") == "MTR"
    assert jic_prefix("M1") == "M"
    assert jic_prefix("CR1-13") == "CR"
    assert jic_prefix("OL1") == "OL"
    assert jic_prefix("X1-3") == ""
    assert jic_prefix("Q1") == ""


def test_nema_seal_in_matches_ansi_drafting():
    session = build_nema_seal_in()
    issues = ansi_drafting_issues(session.project, session.catalog)
    assert issues == [], issues


def test_ansi_constants_stay_at_the_drawn_sizes():
    assert BORDER_STROKE == 0.45
    assert RAIL_STROKE == 1.35
    assert RUNG_NUMBER_MM == 2.0
    assert ZONE_LABEL_MM == 2.2
    assert MARK_SIZE == 2.3
    assert XREF_SIZE == 2.0


def test_unreadable_wire_is_marked_and_an_iec_project_is_not_called_ansi():
    session = build_nema_seal_in()
    wire = session.project.sheets[0].wires[0]
    wire.number = "101"
    wire.inferred = True
    assert wire.drawn_mark(ladder=True) == "?101"
    session.project.title_block.standard = "IEC"
    issues = ansi_drafting_issues(session.project, session.catalog)
    assert issues == ["title block standard is IEC, not NEMA/ANSI"]


def test_nema_label_is_painted_on_a_narrow_sheet():
    """A3 portrait used to clip the standard cell, so the sheet never said NEMA/ANSI."""

    session = build_nema_seal_in()
    for size, orient in (("A3", "portrait"), ("TABLOID", "portrait"), ("LETTER", "portrait")):
        session.project.sheets[0].page.size = size
        session.project.sheets[0].page.orientation = orient
        issues = ansi_drafting_issues(session.project, session.catalog)
        assert not any("does not show NEMA/ANSI" in item for item in issues), (size, issues)


def test_a_jic_tag_on_an_iec_symbol_is_rejected():
    session = build_nema_seal_in()
    comp = session.project.sheet("control").component("PB1")
    comp.library_id = "iec.operators.pb_nc"
    issues = ansi_drafting_issues(session.project, session.catalog)
    assert any("PB1" in item and "iec.operators.pb_nc" in item for item in issues)
