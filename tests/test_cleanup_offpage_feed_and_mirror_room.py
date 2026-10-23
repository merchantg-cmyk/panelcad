"""Cleanup keeps an off-page source on the rung it feeds, and leaves room
under an IEC coil for its contact mirror. Built-in samples stay sensible and
carry neutral title-block data."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from panelcad import Session
from panelcad.examples.xref_demo import build_iec_mirror, build_nema_seal_in
from panelcad.render.sheet import pin_world
from panelcad.render.xref import annotation_box, place_xref_labels

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def _pin(session: Session, sheet_id: str, ref: str, pin: str) -> tuple[float, float]:
    comp = next(c for c in session.sheet(sheet_id).components if c.id == ref)
    return pin_world(comp, session.catalog.get(comp.library_id).pin(pin))


def _ladder(s: Session, sid: str, title: str):
    sh = s.add_sheet(sid, title)
    sh.drawing_style = "ladder"
    sh.rail_hot, sh.rail_return = "L1", "L2"
    return sh


def _fed_from_offpage(
    contact_lib: str, contact_ref: str, pin_in: str, pin_out: str, load_lib: str, load_in: str, load_out: str
) -> Session:
    s = Session.create("fed rung")
    s.set_title_block(rail_hot="L1", rail_return="L2")
    s.project.title_block.drawing_style = "ladder"
    _ladder(s, "b", "Continued")
    # Arrow placed far from its contact, on a different row.
    s.place("iec.connection.offpage_from", ref="OP7", x=200, y=150, sheet_id="b", snap=False)
    s.place(contact_lib, ref=contact_ref, x=120, y=40, sheet_id="b", snap=False)
    s.place(load_lib, ref="LD4", x=220, y=40, sheet_id="b", snap=False)
    s.wire("OP7.A", f"{contact_ref}.{pin_in}", sheet_id="b")
    s.wire(f"{contact_ref}.{pin_out}", f"LD4.{load_in}", sheet_id="b")
    s.wire(f"LD4.{load_out}", "L2", sheet_id="b")
    return s


@pytest.mark.parametrize(
    "lib,ref,pin_in,pin_out,load",
    [
        # aux contact + lamp: both land on the hold row
        ("nema.contactors.aux_nc", "KA5-21", "1", "2", ("nema.operators.pilot_light", "X1", "X2")),
        # ordinary contact + coil: start row
        ("nema.operators.pb_no", "PB5", "3", "4", ("nema.contactors.coil", "A1", "A2")),
    ],
)
def test_offpage_source_sits_on_the_rung_left_of_what_it_feeds(lib, ref, pin_in, pin_out, load):
    s = _fed_from_offpage(lib, ref, pin_in, pin_out, *load)
    s.cleanup()
    ax, ay = _pin(s, "b", "OP7", "A")
    cx, cy = _pin(s, "b", ref, pin_in)
    lx, ly = _pin(s, "b", "LD4", load[1])
    assert abs(ay - cy) < 0.6, (ay, cy)  # same rung
    assert abs(cy - ly) < 0.6
    assert ax <= cx + 0.01  # arrow feeds the contact from the left
    sheet = s.sheet("b")
    assert not any("L1" in (w.frm, w.to) for w in sheet.wires)  # no rail feed added


def _iec_mirror_page() -> Session:
    s = Session.create("mirror room", sheet_id="main", title="Control")
    s.set_title_block(standard="IEC", drawing_style="ladder", rail_hot="L+", rail_return="0V")
    from panelcad.core.zones import rung_y

    def y(i):
        return rung_y(s.sheet("main"), i, project=s.project)

    def put(lib, ref, x, ry, pin):
        dev = s.catalog.get(lib)
        s.place(lib, ref=ref, x=x, y=ry - dev.pin(pin).y, snap=False)

    put("iec.operators.pb_no", "S4", 60, y(2), "13")
    put("iec.contactors.coil", "K7", 210, y(2), "A1")
    put("iec.contactors.aux_no", "K7-14", 60, y(4), "13")
    put("iec.contactors.aux_nc", "K7-21", 60, y(6), "21")
    put("iec.contactors.aux_no", "K7-33", 120, y(6), "13")
    put("iec.operators.pilot_light", "H7", 210, y(6), "X1")
    s.wire("L+", "S4.13")
    s.wire("S4.14", "K7.A1")
    s.wire("K7.A2", "0V")
    s.wire("S4.13", "K7-14.13")
    s.wire("K7-14.14", "K7.A1")
    s.wire("L+", "K7-21.21")
    s.wire("K7-21.22", "K7-33.13")
    s.wire("K7-33.14", "H7.X1")
    s.wire("H7.X2", "0V")
    return s


def _mirror_seat(s: Session, sheet_id: str, owner: str):
    sheet = s.sheet(sheet_id)
    for ann, x, y, align in place_xref_labels(s.project, s.catalog, sheet, ladder=True):
        if ann.kind == "mirror" and ann.owner == owner:
            return ann, x, y, annotation_box(ann, x, y, align)
    raise AssertionError("no mirror for " + owner)


def _assert_mirror_under_coil(s: Session, sheet_id: str, coil: str):
    ann, x, _y, box = _mirror_seat(s, sheet_id, coil)
    comp = next(c for c in s.sheet(sheet_id).components if c.id == coil)
    dev = s.catalog.get(comp.library_id)
    left, right = comp.x, comp.x + dev.schematic_width * comp.visual_scale()
    centre = (box[0] + box[2]) / 2.0
    assert left <= centre <= right, (coil, box, left, right)
    assert box[1] >= comp.y  # below the coil's top, not beside the rung above


def test_cleanup_leaves_room_for_contact_mirror_under_coil():
    s = _iec_mirror_page()
    s.cleanup()
    _assert_mirror_under_coil(s, "main", "K7")
    # Lower rung kept its shape: contacts and lamp still share one pin line.
    ys = {round(_pin(s, "main", ref, pin)[1], 1) for ref, pin in (("K7-21", "21"), ("H7", "X1"))}
    assert len(ys) == 1


def test_iec_sample_mirror_sits_under_k1_after_cleanup():
    s = Session.open(str(EXAMPLES / "xref_iec_mirror.pcad.yaml"))
    s.cleanup()
    _assert_mirror_under_coil(s, "main", "K1")


def test_nema_sample_offpage_rung_after_cleanup():
    s = Session.open(str(EXAMPLES / "xref_nema_seal_in.pcad.yaml"))
    s.cleanup()
    ax, ay = _pin(s, "aux", "OP2", "A")
    cx, cy = _pin(s, "aux", "CR1-21", "1")
    assert abs(ay - cy) < 0.6 and ax <= cx + 0.01
    assert any({w.frm, w.to} == {"OP2.A", "CR1-21.1"} for w in s.sheet("aux").wires)


@pytest.mark.parametrize("source", ["file", "builder"])
def test_nema_cr1_rung_has_a_contact_not_a_bare_feed(source):
    s = (
        Session.open(str(EXAMPLES / "xref_nema_seal_in.pcad.yaml"))
        if source == "file"
        else build_nema_seal_in()
    )
    s.cleanup()
    wires = s.sheet("control").wires
    assert not any({w.frm, w.to} == {"L1", "CR1.A1"} for w in wires)
    feed = [w for w in wires if "CR1.A1" in (w.frm, w.to)]
    other = {(w.to if w.frm == "CR1.A1" else w.frm).split(".")[0] for w in feed}
    assert other and all(ref.startswith("M1-") for ref in other)
    assert any("L1" in (w.frm, w.to) and any(ref in (w.frm + w.to) for ref in other) for w in wires)


NEUTRAL = {"Example Company", "Example Customer", ""}


@pytest.mark.parametrize("name", ["xref_nema_seal_in", "xref_iec_mirror"])
def test_sample_title_blocks_are_neutral(name):
    data = yaml.safe_load((EXAMPLES / f"{name}.pcad.yaml").read_text(encoding="utf-8"))
    tb = data["title_block"]
    assert tb.get("company") == "Example Company"
    assert tb.get("customer") == "Example Customer"


@pytest.mark.parametrize("builder", [build_nema_seal_in, build_iec_mirror])
def test_sample_builders_use_neutral_title_blocks(builder):
    tb = builder().project.title_block
    assert tb.company == "Example Company"
    assert tb.customer == "Example Customer"
    src = (ROOT / "src" / "panelcad" / "examples" / "xref_demo.py").read_text(encoding="utf-8")
    for old in ("Ada", "ECS Shop", "Acme"):
        assert old not in src
