"""Facts go in. The engine draws the ladder. The gate rejects the old mess."""

from panelcad.api.session import Session
from panelcad.core.ansi_drafting import ansi_drafting_issues
from panelcad.core.elementary_layout import elementary_layout_issues, layout_elementary
from panelcad.core.route import component_aabb
from panelcad.examples.scan_ladder_demo import _elementary_facts
from panelcad.render.sheet import pin_world
from panelcad.render.titleblock import working_rect


def _facts() -> Session:
    session = Session.create("Harness", sheet_id="elementary", title="Elementary")
    session.set_title_block(
        company="Demo Controls",
        drawing_number="E-100",
        standard="NEMA/ANSI",
        drawing_style="ladder",
        drawn_by="Demo",
    )
    session.project.sheets[0].page.size = "TABLOID"
    session.project.sheets[0].page.orientation = "landscape"
    _elementary_facts(session)
    return session


def _pin(session, sheet, ref: str) -> tuple[float, float]:
    comp = sheet.component(ref)
    device = session.catalog.get(comp.library_id)
    pin = next((item for item in device.pins if str(item.side) == "left"), device.pins[0])
    return pin_world(comp, pin)


def test_a_piled_elementary_fails_the_gate():
    session = _facts()
    sheet = session.project.sheet("elementary")
    for index, comp in enumerate(sheet.components):
        comp.x = 30.0 + (index % 4) * 18.0
        comp.y = 40.0 + (index % 3) * 8.0
    issues = elementary_layout_issues(session, sheet)
    assert issues, "the old piled elementary must not pass"
    assert any("rung" in item or "wander" in item or "jog" in item or "diagonal" in item or "title" in item for item in issues)


def test_the_engine_issues_a_clean_ladder_from_scrambled_facts():
    session = _facts()
    sheet = session.project.sheet("elementary")
    for comp in sheet.components:
        comp.x, comp.y = 6.0, 6.0
    assert elementary_layout_issues(session, sheet)
    layout_elementary(session, sheet)
    session.number_wires(overwrite=True)
    assert elementary_layout_issues(session, sheet) == []
    assert ansi_drafting_issues(session.project, session.catalog) == []

    _wx, wy, _ww, wh = working_rect(sheet)
    limit = wy + wh - 4.0
    ys = {}
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        box = component_aabb(comp.x, comp.y, device.schematic_width, device.schematic_height)
        assert box[3] <= limit + 0.2, comp.id
        ys[comp.id] = _pin(session, sheet, comp.id)[1]
    assert ys["ES1"] == ys["PB1"] == ys["M1"]
    assert ys["M1-13"] > ys["PB1"]
    assert abs(_pin(session, sheet, "M1-13")[0] - _pin(session, sheet, "PB1")[0]) < 1.0
    assert ys["CR1"] != ys["M1"]
    assert ys["PL1"] not in {ys["M1"], ys["CR1"], ys["M1-33"]}
    assert abs(ys["X1-1"] - ys["M1-33"]) < 1.0
    assert abs(ys["X1-1"] - ys["X1-2"]) < 1.0
    assert abs(ys["X1-2"] - ys["X1-4"]) < 1.0
    assert _pin(session, sheet, "X1-1")[0] < _pin(session, sheet, "X1-4")[0]
    assert _pin(session, sheet, "PB1")[0] < _pin(session, sheet, "M1")[0] < _pin(session, sheet, "OL1")[0]
    assert _pin(session, sheet, "M1-23")[0] < _pin(session, sheet, "CR1")[0]
    assert _pin(session, sheet, "CR1-13")[0] < _pin(session, sheet, "PL1")[0]
    from panelcad.core.ladder import rail_xs

    _left, right = rail_xs(sheet)
    for ref in ("OL1", "CR1", "PL1"):
        assert right - _pin(session, sheet, ref)[0] < 50.0, ref
    assert ys["M1"] < ys["CR1"] < ys["PL1"]


def test_a_two_digit_ocr_mark_stays_unreadable():
    from panelcad.core.elementary_layout import scrub_illegal_wire_numbers

    session = _facts()
    sheet = session.project.sheet("elementary")
    sheet.wires[0].number = "07"
    sheet.wires[0].label = "07"
    assert scrub_illegal_wire_numbers(session) == 1
    assert sheet.wires[0].number == "?"
    assert sheet.wires[0].label == "?"


def test_a_terminal_arch_nest_fails():
    from panelcad.core.models import Point
    from panelcad.render.wiring import wire_ends

    session = _facts()
    sheet = session.project.sheet("elementary")
    layout_elementary(session, sheet)
    assert elementary_layout_issues(session, sheet) == []
    wire = next(item for item in sheet.wires if item.frm.endswith("X1-2.B") or item.to.endswith("X1-2.B"))
    ax, ay, _aside, bx, by, _bside = wire_ends(sheet, session.catalog, wire, session.project)
    wire.points = [
        Point(x=ax, y=ay - 14),
        Point(x=bx - 30, y=ay - 14),
        Point(x=bx - 30, y=ay + 14),
        Point(x=bx, y=ay + 14),
    ]
    issues = elementary_layout_issues(session, sheet)
    assert any("arches over the terminals" in item for item in issues)


def test_a_coil_left_in_the_middle_fails():
    session = _facts()
    sheet = session.project.sheet("elementary")
    layout_elementary(session, sheet)
    assert elementary_layout_issues(session, sheet) == []
    sheet.component("CR1").x = 70.0
    issues = elementary_layout_issues(session, sheet)
    assert any("CR1" in item and "L2" in item for item in issues)


def test_spare_terminals_in_the_gap_fail():
    session = _facts()
    sheet = session.project.sheet("elementary")
    layout_elementary(session, sheet)
    assert elementary_layout_issues(session, sheet) == []
    starter = _pin(session, sheet, "M1")[1]
    seal = _pin(session, sheet, "M1-13")[1]
    mid = (starter + seal) / 2.0
    for ref in ("X1-2", "X1-3", "X1-4"):
        comp = sheet.component(ref)
        comp.y = mid - 12.0
    issues = elementary_layout_issues(session, sheet)
    assert any("floats between rungs" in item for item in issues)


def test_a_terminal_on_a_foreign_rung_fails():
    session = _facts()
    sheet = session.project.sheet("elementary")
    layout_elementary(session, sheet)
    assert elementary_layout_issues(session, sheet) == []
    _mx, my = _pin(session, sheet, "M1")
    sheet.component("X1-2").y = my + 5.0 - 12.0
    issues = elementary_layout_issues(session, sheet)
    assert any("X1-2" in item and "rung" in item for item in issues)
