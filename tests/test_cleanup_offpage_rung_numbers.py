"""cleanup() must not tie a rung fed from another sheet to the hot rail."""

from __future__ import annotations

from pathlib import Path

import pytest

from panelcad import Session

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.parametrize("name", ["xref_nema_seal_in", "xref_iec_mirror"])
def test_cleanup_keeps_wire_numbers(name):
    s = Session.open(str(EXAMPLES / f"{name}.pcad.yaml"))
    before = {(sh.id, w.id): w.number for sh in s.project.sheets for w in sh.wires}
    s.cleanup()
    for sh in s.project.sheets:
        for w in sh.wires:
            if (sh.id, w.id) in before and before[(sh.id, w.id)]:
                assert w.number == before[(sh.id, w.id)], (name, sh.id, w.id, w.number)


def _continued_rung() -> Session:
    s = Session.create("continued rung")
    s.set_title_block(rail_hot="L1", rail_return="L2")
    s.project.title_block.drawing_style = "ladder"
    for sid, title in (("a", "Control"), ("b", "Continued")):
        sh = s.add_sheet(sid, title)
        sh.drawing_style = "ladder"
        sh.rail_hot, sh.rail_return = "L1", "L2"
    s.place("nema.operators.pb_no", ref="PB1", x=60, y=40, sheet_id="a", snap=False)
    s.place("nema.contactors.coil", ref="CR1", x=200, y=40, sheet_id="a", snap=False)
    s.place("nema.contactors.aux_no", ref="CR1-13", x=60, y=60, sheet_id="a", snap=False)
    s.wire("L1", "PB1.3", sheet_id="a")
    s.wire("PB1.4", "CR1.A1", sheet_id="a")
    s.wire("CR1.A2", "L2", sheet_id="a")
    s.wire("L1", "CR1-13.3", sheet_id="a")
    s.place("iec.connection.offpage_from", ref="OP9", x=40, y=40, sheet_id="b", snap=False)
    s.place("nema.contactors.aux_nc", ref="CR1-21", x=110, y=40, sheet_id="b", snap=False)
    s.place("nema.operators.pilot_light", ref="PL1", x=200, y=40, sheet_id="b", snap=False)
    s.wire("OP9.A", "CR1-21.1", sheet_id="b")
    s.wire("CR1-21.2", "PL1.X1", sheet_id="b")
    s.wire("PL1.X2", "L2", sheet_id="b")
    return s


def test_offpage_fed_rung_gets_no_hot_rail_wire():
    s = _continued_rung()
    s.cleanup()
    b = s.project.sheet("b")
    hot_wires = [w for w in b.wires if "L1" in (w.frm, w.to)]
    assert hot_wires == []
    lamp_feed = next(w for w in b.wires if {w.frm, w.to} == {"CR1-21.2", "PL1.X1"})
    assert lamp_feed.number != "L1"


def test_rung_with_outgoing_arrow_mid_rung_keeps_hot_feed():
    s = Session.create("outgoing tap")
    s.set_title_block(rail_hot="L1", rail_return="L2")
    s.project.title_block.drawing_style = "ladder"
    sh = s.add_sheet("a", "Control")
    sh.drawing_style = "ladder"
    sh.rail_hot, sh.rail_return = "L1", "L2"
    s.place("nema.operators.pb_nc", ref="PB0", x=40, y=40, sheet_id="a", snap=False)
    s.place("nema.operators.pb_no", ref="PB1", x=80, y=40, sheet_id="a", snap=False)
    s.place("nema.operators.pb_no", ref="PB2", x=120, y=40, sheet_id="a", snap=False)
    s.place("nema.contactors.coil", ref="CR1", x=200, y=40, sheet_id="a", snap=False)
    s.place("iec.connection.offpage", ref="OP1", x=160, y=80, sheet_id="a", snap=False)
    s.wire("PB0.2", "PB1.3", sheet_id="a")
    s.wire("PB1.4", "PB2.3", sheet_id="a")
    s.wire("PB2.4", "CR1.A1", sheet_id="a")
    s.wire("PB2.4", "OP1.A", sheet_id="a")
    s.cleanup()
    a = s.project.sheet("a")
    assert any("L1" in (w.frm, w.to) for w in a.wires)
    assert any("L2" in (w.frm, w.to) for w in a.wires)
