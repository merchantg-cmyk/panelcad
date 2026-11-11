"""Facts go in. The engine draws the ladder. The gate rejects the old mess."""

from panelcad.api.session import Session
from panelcad.core.ansi_drafting import ansi_drafting_issues
from panelcad.core.elementary_layout import elementary_layout_issues, issue_elementary, layout_elementary
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


def test_a_relay_or_timer_coil_mid_sheet_fails_and_the_engine_parks_it_at_l2():
    """A CR circle and a timer coil are loads. A timer contact is not."""

    session = Session.create("Relay", sheet_id="elementary", title="Elementary")
    session.set_title_block(
        company="Demo Controls",
        drawing_number="E-200",
        standard="NEMA/ANSI",
        drawing_style="ladder",
        drawn_by="Demo",
    )
    session.project.sheets[0].page.size = "TABLOID"
    session.project.sheets[0].page.orientation = "landscape"
    session.project.sheets[0].drawing_style = "ladder"
    session.place("nema.operators.pb_no", ref="PB1", x=40, y=50, snap=False)
    session.place("nema.relays.cr", ref="CR9", x=120, y=50, snap=False)
    session.place("nema.operators.pb_no", ref="PB2", x=40, y=110, snap=False)
    session.place("nema.timers.on_delay", ref="TR1", x=140, y=110, snap=False)
    session.place("nema.timers.contact_notc", ref="TR1-1", x=40, y=170, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=360, y=170, snap=False)
    session.wire("L1", "PB1.3")
    session.wire("PB1.4", "CR9.A1")
    session.wire("CR9.A2", "L2")
    session.wire("L1", "PB2.3")
    session.wire("PB2.4", "TR1.A1")
    session.wire("TR1.A2", "L2")
    session.wire("L1", "TR1-1.3")
    session.wire("TR1-1.4", "PL1.X1")
    session.wire("PL1.X2", "L2")
    sheet = session.project.sheet("elementary")
    issues = elementary_layout_issues(session, sheet)
    assert any("CR9" in item and "L2" in item for item in issues)
    assert any("TR1" in item and "L2" in item for item in issues)
    assert not any("TR1-1" in item and "L2" in item for item in issues)
    layout_elementary(session, sheet)
    assert elementary_layout_issues(session, sheet) == [], elementary_layout_issues(session, sheet)
    from panelcad.core.ladder import rail_xs
    from panelcad.render.sheet import pin_world

    def _right_pin(ref: str) -> float:
        comp = sheet.component(ref)
        device = session.catalog.get(comp.library_id)
        pin = next((item for item in device.pins if str(item.side) == "right"), device.pins[-1])
        return pin_world(comp, pin)[0]

    _left, right = rail_xs(sheet)
    for ref in ("CR9", "TR1", "PL1"):
        assert right - _right_pin(ref) <= 36.0, ref
    assert _pin(session, sheet, "PB1")[0] < _pin(session, sheet, "CR9")[0]
    assert _pin(session, sheet, "PB2")[0] < _pin(session, sheet, "TR1")[0]
    assert _pin(session, sheet, "TR1-1")[0] < _pin(session, sheet, "PL1")[0]


def test_a_rail_wire_is_not_moved_onto_the_primary_pin():
    """FU2.1 already goes to L2, and FU2.2 already carries 101 to H4.

    Facing the rail would put both wires on FU2.2 and tie the primary to L2.
    The return wires on the control rungs are then refused for that new finding.
    """

    session = Session.create("Primary", sheet_id="elementary", title="Elementary")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", coil_style="circle", drawn_by="Demo")
    sheet = session.project.sheet("elementary")
    sheet.page.size = "TABLOID"
    sheet.page.orientation = "landscape"
    sheet.drawing_style = "ladder"
    session.place("nema.power.control_transformer", ref="T1", x=100, y=20, snap=False)
    session.place("nema.protection.fuse_10a", ref="FU2", x=70, y=40, snap=False)
    session.place("nema.relays.cr", ref="CR1", x=300, y=160, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=300, y=200, snap=False)
    session.wire("T1.H4", "FU2.2", number="101")
    session.wire("FU2.1", "L2", number="104")
    session.wire("CR1.A2", "L2", number="104")
    session.wire("PL1.X2", "L2", number="104")
    layout_elementary(session, sheet)
    ends = {(wire.frm, wire.to) for wire in sheet.wires}
    assert ("FU2.1", "L2") in ends or ("L2", "FU2.1") in ends
    assert not any("ties the primary" in item for item in elementary_layout_issues(session, sheet))


def test_two_numbers_on_one_net_or_a_primary_on_the_return_cannot_issue():
    session = Session.create("Short", sheet_id="elementary", title="Elementary")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", coil_style="circle", drawn_by="Demo")
    sheet = session.project.sheet("elementary")
    sheet.page.size = "TABLOID"
    sheet.page.orientation = "landscape"
    sheet.drawing_style = "ladder"
    sheet.rail_return = "X2"
    session.place("nema.protection.fuse_10a", ref="FU2", x=40, y=40, snap=False)
    session.place("nema.power.control_transformer", ref="T1", x=100, y=40, snap=False)
    session.wire("L1", "FU2.1", number="100", sheet_id=sheet.id)
    session.wire("FU2.2", "T1.H4", number="101", sheet_id=sheet.id)
    session.wire("T1.H4", "X2", number="104", sheet_id=sheet.id)
    text = " ".join(elementary_layout_issues(session, sheet))
    assert "two wire numbers on one net" in text
    assert "ties the primary to X2" in text
    assert issue_elementary(session, sheet, force=True) is False


def test_a_line_through_a_symbol_fails_and_is_not_issued():
    from panelcad.core.elementary_layout import issue_elementary
    from panelcad.core.models import Wire

    session = _facts()
    sheet = session.project.sheet("elementary")
    layout_elementary(session, sheet)
    assert elementary_layout_issues(session, sheet) == []
    sheet.wires.append(Wire(id="through", frm="ES1.1", to="ES1.2", number="?"))
    issues = elementary_layout_issues(session, sheet)
    assert any("through connects ES1's own pins" in item for item in issues), issues
    assert issue_elementary(session, sheet, force=True) is False
    assert any(wire.frm == "ES1.1" and wire.to == "ES1.2" for wire in sheet.wires)


def test_a_pin_beside_the_rail_without_a_landing_fails():
    from panelcad.core.elementary_layout import issue_elementary

    session = Session.create("Gap", sheet_id="elementary", title="Elementary")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", coil_style="circle", drawn_by="Demo")
    session.project.sheets[0].page.size = "TABLOID"
    session.project.sheets[0].page.orientation = "landscape"
    session.project.sheets[0].drawing_style = "ladder"
    session.place("nema.operators.estop", ref="ES1", x=41, y=80, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=300, y=80, snap=False)
    session.wire("ES1.2", "M1.A1")
    session.wire("M1.A2", "L2")
    sheet = session.project.sheet("elementary")
    issues = elementary_layout_issues(session, sheet)
    assert any("ES1" in item and "does not land on L1" in item for item in issues), issues
    assert issue_elementary(session, sheet, force=True) is True
    assert elementary_layout_issues(session, sheet) == []
    from panelcad.core.ladder import rail_xs

    left, _right = rail_xs(sheet)
    assert _pin(session, sheet, "ES1")[0] - left > 42.0


def test_a_relay_circle_is_a_catalog_coil():
    from panelcad.render.painter import is_control_coil

    session = Session.create("CR")
    device = session.catalog.get("nema.relays.cr")
    assert device.primitives
    assert is_control_coil(device, "nema.relays.cr")
    for library_id in (
        "nema.operators.estop",
        "nema.protection.fuse_10a",
        "nema.operators.pb_nc",
        "nema.operators.pb_no",
        "nema.operators.pilot_light",
        "nema.contactors.aux_no",
        "nema.protection.ol_contact",
        "nema.power.control_transformer",
    ):
        symbol = session.catalog.get(library_id)
        assert symbol.primitives, library_id


def test_a_contacts_only_run_to_l2_fails_and_the_starter_rung_passes():
    session = Session.create("Dead rung", sheet_id="elementary", title="Elementary")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", coil_style="circle", drawn_by="Demo")
    session.project.sheets[0].page.size = "TABLOID"
    session.project.sheets[0].page.orientation = "landscape"
    session.project.sheets[0].drawing_style = "ladder"
    session.place("nema.operators.estop", ref="ES1", x=40, y=40, snap=False)
    session.place("nema.operators.pb_nc", ref="PB2", x=80, y=40, snap=False)
    session.place("nema.operators.pb_no", ref="PB1", x=120, y=40, snap=False)
    session.wire("L1", "ES1.1")
    session.wire("ES1.2", "PB2.1")
    session.wire("PB2.2", "PB1.3")
    session.wire("PB1.4", "L2")
    sheet = session.project.sheet("elementary")
    issues = elementary_layout_issues(session, sheet)
    assert any("ES1, PB2, PB1" in item and "no load" in item for item in issues), issues
    from panelcad.core.elementary_layout import issue_elementary

    assert issue_elementary(session, sheet, force=True) is False
    assert any("no load" in item for item in elementary_layout_issues(session, sheet))

    good = _facts()
    good_sheet = good.project.sheet("elementary")
    layout_elementary(good, good_sheet)
    assert elementary_layout_issues(good, good_sheet) == []
    assert _pin(good, good_sheet, "PB2")[0] < _pin(good, good_sheet, "PB1")[0] < _pin(good, good_sheet, "M1")[0]
    assert _pin(good, good_sheet, "M1-13")[0] == _pin(good, good_sheet, "PB1")[0]
    assert _pin(good, good_sheet, "M1-13")[1] > _pin(good, good_sheet, "PB1")[1]


def test_a_fuse_from_rail_to_rail_and_a_terminal_self_short_cannot_issue():
    from panelcad.core.elementary_layout import issue_elementary
    from panelcad.importer.cleanup import remaining_gaps

    session = Session.create("Rail fuse", sheet_id="elementary", title="Elementary")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", coil_style="circle", drawn_by="Demo")
    sheet = session.project.sheets[0]
    sheet.page.size = "TABLOID"
    sheet.page.orientation = "landscape"
    sheet.drawing_style = "ladder"
    sheet.rail_hot = "L1"
    sheet.rail_return = "X2"
    session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=40, snap=False)
    session.place("nema.terminals.feed_through", ref="X1-3", x=200, y=40, snap=False)
    session.place("nema.contactors.aux_no", ref="M1-13", x=80, y=80, snap=False)
    session.place("nema.contactors.aux_no", ref="M1-23", x=80, y=120, snap=False)
    session.wire("L1", "FU1.1", number="102")
    session.wire("FU1.2", "X2", number="103")
    session.wire("X1-3.A", "X1-3.B", number="222")
    session.wire("L1", "M1-13.3", number="115")
    session.wire("M1-13.4", "X2", number="106")
    session.wire("L1", "M1-23.3", number="113")
    session.wire("M1-23.4", "X2", number="114")
    issues = elementary_layout_issues(session, sheet)
    text = "\n".join(issues)
    assert "FU1 is a fuse from L1 to X2" in text, issues
    assert "connects X1-3's own pins" in text, issues
    assert "M1-13" in text and "no load" in text
    assert "M1-23" in text and "no load" in text
    assert issue_elementary(session, sheet, force=True) is False
    kinds = {gap["kind"] for gap in remaining_gaps(session)}
    assert {"fuse_rail", "self_wire", "contacts_only"} <= kinds


def test_an_unwired_holding_contact_sits_under_the_start():
    session = Session.create("Seal-in spot", sheet_id="elementary", title="Elementary")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", coil_style="circle", drawn_by="Demo")
    session.project.sheets[0].page.size = "TABLOID"
    session.project.sheets[0].page.orientation = "landscape"
    session.project.sheets[0].drawing_style = "ladder"
    session.place("nema.operators.pb_nc", ref="PB2", x=40, y=40, snap=False)
    session.place("nema.operators.pb_no", ref="PB1", x=90, y=40, snap=False)
    session.place("nema.contactors.aux_no", ref="M1-13", x=200, y=40, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=280, y=40, snap=False)
    session.wire("L1", "PB2.1")
    session.wire("PB2.2", "PB1.3")
    session.wire("PB1.4", "M1.A1")
    session.wire("M1.A2", "L2")
    sheet = session.project.sheet("elementary")
    layout_elementary(session, sheet)
    assert elementary_layout_issues(session, sheet) == [], elementary_layout_issues(session, sheet)
    assert abs(_pin(session, sheet, "M1-13")[0] - _pin(session, sheet, "PB1")[0]) < 1.0
    assert _pin(session, sheet, "M1-13")[1] > _pin(session, sheet, "PB1")[1]
    assert not any(wire.frm.startswith("M1-13") or wire.to.startswith("M1-13") for wire in sheet.wires)


def test_a_read_nc_stop_stays_nc():
    from panelcad.importer.ladder_synth import _classify_device_role_and_library

    catalog = Session.create("PB").catalog
    _role, lid, pins = _classify_device_role_and_library(
        "PB2", "pushbutton", "pushbutton", "nema.operators.pb_nc", [], "NEMA/ANSI", catalog
    )
    assert lid == "nema.operators.pb_nc"
    assert pins == ["1", "2"]
    _role, start, _pins = _classify_device_role_and_library(
        "PB2", "pushbutton", "pushbutton", "nema.operators.pb_no", [], "NEMA/ANSI", catalog
    )
    assert start == "nema.operators.pb_no"
    _role, unread, _pins = _classify_device_role_and_library(
        "PB2", "pushbutton", "pushbutton", "", [], "NEMA/ANSI", catalog
    )
    assert unread == "nema.operators.pb_no"


def test_a_read_transformer_tag_is_not_redrawn_as_a_timer():
    from panelcad.importer.ladder_synth import _classify_device_role_and_library

    catalog = Session.create("T").catalog
    _role, lid, pins = _classify_device_role_and_library(
        "T1",
        "transformer",
        "transformer",
        "nema.power.control_transformer",
        ["H1", "H4", "X1", "X2"],
        "NEMA/ANSI",
        catalog,
    )
    assert lid == "nema.power.control_transformer"
    assert "H1" in pins and "X1" in pins
    bare_role, bare_lid, _bare_pins = _classify_device_role_and_library(
        "T1", "", "", "", [], "NEMA/ANSI", catalog
    )
    assert "transformer" in bare_lid
    assert "on_delay" not in bare_lid
    assert not bare_role.startswith("control_block")
    _timer_role, timer_lid, _timer_pins = _classify_device_role_and_library(
        "TR1", "", "", "", [], "NEMA/ANSI", catalog
    )
    assert timer_lid == "nema.timers.on_delay"


def test_a_terminal_on_a_foreign_rung_fails():
    session = _facts()
    sheet = session.project.sheet("elementary")
    layout_elementary(session, sheet)
    assert elementary_layout_issues(session, sheet) == []
    _mx, my = _pin(session, sheet, "M1")
    sheet.component("X1-2").y = my + 5.0 - 12.0
    issues = elementary_layout_issues(session, sheet)
    assert any("X1-2" in item and "rung" in item for item in issues)


def test_a_far_fuse_pin_to_the_return_rail_draws_outside_the_body():
    """Phone T1/FU2: FU2.1 faces the transformer and the wire still reaches L2.

    The channel leaves the pin on its open side and runs under the block.
    """

    session = Session.create("Far pin", sheet_id="elementary", title="Elementary")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", coil_style="circle", drawn_by="Demo")
    sheet = session.project.sheet("elementary")
    sheet.page.size = "TABLOID"
    sheet.page.orientation = "landscape"
    sheet.drawing_style = "ladder"
    session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=40, snap=False)
    session.place("nema.power.control_transformer", ref="T1", x=100, y=40, snap=False)
    session.place("nema.protection.fuse_10a", ref="FU2", x=40, y=80, snap=False)
    session.wire("L1", "FU1.1", number="102")
    session.wire("FU1.2", "T1.H1", number="103")
    session.wire("T1.H4", "FU2.2", number="101")
    session.wire("FU2.1", "L2", number="104")
    layout_elementary(session, sheet)
    issues = elementary_layout_issues(session, sheet)
    assert not any("runs through a symbol" in item or "jogs through" in item for item in issues), issues
    ends = {(wire.frm, wire.to) for wire in sheet.wires}
    assert ("FU2.1", "L2") in ends or ("L2", "FU2.1") in ends


def test_a_seal_in_that_lands_on_the_coil_sits_under_the_start():
    """Phone M1: M1-13.4 to M1.A1 is the seal-in across the start button."""

    session = Session.create("Seal-in", sheet_id="elementary", title="Elementary")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", coil_style="circle", drawn_by="Demo")
    sheet = session.project.sheet("elementary")
    sheet.page.size = "TABLOID"
    sheet.page.orientation = "landscape"
    sheet.drawing_style = "ladder"
    for lib, ref in (
        ("nema.operators.estop", "ES1"),
        ("nema.operators.pb_nc", "PB2"),
        ("nema.operators.pb_no", "PB1"),
        ("nema.contactors.aux_no", "M1-13"),
        ("nema.contactors.coil", "M1"),
        ("nema.protection.ol_contact", "OL1"),
    ):
        session.place(lib, ref=ref, x=40, y=40, snap=False)
    session.wire("L1", "ES1.1", number="102")
    session.wire("ES1.2", "PB2.1", number="105")
    session.wire("PB2.2", "PB1.3", number="106")
    session.wire("PB2.2", "M1-13.3", number="106")
    session.wire("PB1.4", "M1.A1", number="107")
    session.wire("M1-13.4", "M1.A1", number="107")
    session.wire("M1.A2", "OL1.95", number="108")
    session.wire("OL1.96", "L2", number="104")
    layout_elementary(session, sheet)
    issues = elementary_layout_issues(session, sheet)
    assert not any("runs through a symbol" in item for item in issues), issues
    assert abs(sheet.component("M1-13").x - sheet.component("PB1").x) < 1.0


def test_a_fuse_wired_only_to_the_rail_is_not_a_clean_sheet():
    """FU2 to L2, with no transformer on that path, must not issue."""

    session = Session.create("Rail fuse", sheet_id="elementary", title="Elementary")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", coil_style="circle", drawn_by="Demo")
    sheet = session.project.sheet("elementary")
    sheet.page.size = "TABLOID"
    sheet.page.orientation = "landscape"
    sheet.drawing_style = "ladder"
    session.place("nema.protection.fuse_10a", ref="FU2", x=80, y=40, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=200, y=80, snap=False)
    session.wire("FU2.1", "L2", number="104")
    session.wire("PL1.X1", "L1", number="102")
    session.wire("PL1.X2", "L2", number="104")
    text = "\n".join(elementary_layout_issues(session, sheet))
    assert "FU2 is wired to L2 with no path through a load" in text
    session.place("nema.power.control_transformer", ref="T1", x=140, y=40, snap=False)
    session.wire("FU2.2", "T1.H4", number="101")
    session.wire("T1.H1", "L1", number="103")
    layout_elementary(session, sheet)
    text = "\n".join(elementary_layout_issues(session, sheet))
    assert "no path through a load" not in text
    assert "ties the primary" not in text
    ends = {(wire.frm, wire.to) for wire in sheet.wires}
    assert ("FU2.1", "L2") in ends or ("L2", "FU2.1") in ends
    assert ("FU2.2", "T1.H4") in ends or ("T1.H4", "FU2.2") in ends
