"""A device named L1 or L2 is that device, not the ladder rail."""

from __future__ import annotations

import pytest

from panelcad.api.session import Session
from panelcad.core.checks import _end_voltage
from panelcad.core.ladder import endpoint_is_rail, rail_xs
from panelcad.core.wire_numbering import _end_key
from panelcad.render.sheet import pin_world
from panelcad.render.wiring import route_sheet_wires


def _reactors(style: str) -> Session:
    session = Session.create(style)
    if style == "ladder":
        session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.sheet().drawing_style = style  # type: ignore[assignment]
    session.place("nema.operators.pilot_light", ref="L1", x=100, y=40)
    session.place("nema.operators.pilot_light", ref="L2", x=200, y=40)
    return session


def _pin_xy(session: Session, ref: str, pin_id: str) -> tuple[float, float]:
    comp = session.sheet().component(ref)
    device = session.catalog.get(comp.library_id)
    return pin_world(comp, device.pin(pin_id))


@pytest.mark.parametrize("style", ["ladder", "power", "freeform"])
def test_device_pins_named_like_rails_are_drawn_on_the_device(style: str) -> None:
    session = _reactors(style)
    made = session.wire("L1.X2", "L2.X1")
    pts = route_sheet_wires(session.sheet(), session.catalog, session.project)[made.id]
    ax, ay = _pin_xy(session, "L1", "X2")
    bx, by = _pin_xy(session, "L2", "X1")
    assert abs(ax - 124.0) < 0.5
    assert abs(pts[0].x - ax) < 0.8 and abs(pts[0].y - ay) < 0.8
    assert abs(pts[-1].x - bx) < 0.8 and abs(pts[-1].y - by) < 0.8
    left, right = rail_xs(session.sheet())
    assert abs(pts[0].x - left) > 20 and abs(pts[-1].x - right) > 20
    assert not endpoint_is_rail(session.sheet(), "L1.X2", session.project)
    assert not endpoint_is_rail(session.sheet(), "L2.X1", session.project)


@pytest.mark.parametrize("style", ["ladder", "power", "freeform"])
def test_numbering_and_export_keep_the_device_pin(style: str) -> None:
    session = _reactors(style)
    made = session.wire("L1.X2", "L2.X1")
    sheet = session.sheet()
    assert _end_key(sheet, session.project, "L1.X2")[0] == "pin"
    assert _end_key(sheet, session.project, "L2.X1")[0] == "pin"
    assert made.number

    rows = session.from_to_rows()
    ends = {(row["from"], row["to"]) for row in rows}
    assert ("L1:X2", "L2:X1") in ends or ("L2:X1", "L1:X2") in ends

    if style == "ladder":
        other = session.add_sheet("elem")
        other.drawing_style = "ladder"
        session.place("nema.operators.pb_nc", ref="PB1", x=80, y=40, sheet_id=other.id)
        rail = session.wire("PB1.1", "L1", sheet_id=other.id)
        assert endpoint_is_rail(other, "L1", session.project)
        assert _end_key(other, session.project, "L1")[0] == "rail"
        assert rail.number != made.number


def test_problems_do_not_read_a_device_pin_as_the_rail_voltage() -> None:
    session = Session.create("Voltage")
    session.set_title_block(drawing_style="ladder", rails="120VAC/N")
    session.place("nema.operators.pilot_light", ref="L1", x=100, y=40)
    session.place("nema.operators.pilot_light", ref="H1", x=180, y=40)
    made = session.wire("L1.X2", "H1.X1")
    assert _end_voltage(session.sheet(), session.project, "L1.X2", made) != (120.0, "ac")
    session.place("nema.operators.pb_nc", ref="PB1", x=60, y=40)
    rail = session.wire("PB1.1", "120VAC")
    assert _end_voltage(session.sheet(), session.project, "120VAC", rail) == (120.0, "ac")


def test_mix_check_sees_a_device_pin_that_shares_a_rail_name() -> None:
    session = Session.create("Mix")
    session.sheet().drawing_style = "power"  # type: ignore[assignment]
    session.place("nema.operators.pilot_light", ref="L1", x=40, y=40)
    session.place("nema.loads.motor_3ph", ref="MTR1", x=140, y=40)
    with pytest.raises(ValueError, match="control circuit cannot land on a motor power lead"):
        session.wire("L1.X2", "MTR1.T1")


@pytest.mark.parametrize("style", ["power", "freeform"])
def test_bare_rail_words_on_a_rail_less_sheet_still_draw_and_export(style: str, tmp_path) -> None:
    """A saved L1/L2 wire must not stop SVG, PDF, the canvas, or the reports."""

    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import WireItem
    from panelcad.gui.main_window import MainWindow

    session = Session.create(style)
    session.sheet().drawing_style = style  # type: ignore[assignment]
    session.place("nema.operators.pilot_light", ref="PL1", x=150, y=80)
    to_pin = session.wire("L1", "PL1.X1")
    both = session.wire("L1", "L2")
    assert to_pin.frm == "L1" and both.to == "L2"

    routes = route_sheet_wires(session.sheet(), session.catalog, session.project)
    assert len(routes[to_pin.id]) >= 2
    assert len(routes[both.id]) >= 2
    left, right = rail_xs(session.sheet())
    pin_x, pin_y = _pin_xy(session, "PL1", "X1")
    landed = routes[to_pin.id]
    rail_end = landed[0] if to_pin.frm == "L1" else landed[-1]
    pin_end = landed[-1] if to_pin.frm == "L1" else landed[0]
    assert abs(rail_end.x - left) < 0.8
    assert abs(pin_end.x - pin_x) < 0.8 and abs(pin_end.y - pin_y) < 0.8
    pair = routes[both.id]
    assert {round(pair[0].x, 1), round(pair[-1].x, 1)} == {round(left, 1), round(right, 1)}

    session.number_wires()
    assert session.from_to_rows() is not None
    assert isinstance(session.design_checks(), list)
    svg = session.export_svg(tmp_path / f"{style}.svg")
    pdf = session.export_pdf(tmp_path / f"{style}.pdf")
    assert svg.is_file() and svg.stat().st_size > 0
    assert pdf.is_file() and pdf.stat().st_size > 0

    app = QApplication.instance() or QApplication([])
    win = MainWindow(session)
    win.resize(1100, 700)
    win.show()
    app.processEvents()
    assert sum(1 for item in win.canvas.scene.items() if isinstance(item, WireItem)) >= 2
    win.close()


def test_a_real_ladder_rail_is_still_a_rail() -> None:
    session = Session.create("Rails")
    session.set_title_block(drawing_style="ladder", rails="L1/L2")
    session.place("nema.operators.pb_nc", ref="PB1", x=80, y=40)
    made = session.wire("PB1.1", "L1")
    assert endpoint_is_rail(session.sheet(), "L1", session.project)
    pts = route_sheet_wires(session.sheet(), session.catalog, session.project)[made.id]
    left, _right = rail_xs(session.sheet())
    end = pts[-1] if made.to == "L1" else pts[0]
    assert abs(end.x - left) < 0.8
