from pathlib import Path

from panelcad import Session
from panelcad.core.pins import pin_caption, scheme_labels


def test_oem_pin_labels_on_io_card():
    s = Session.create("OEM")
    s.place("oem.keyence.kv_b16xc", ref="A3")
    pin = s.catalog.get("oem.keyence.kv_b16xc").pin("I0")
    assert pin.label == "X0"
    assert pin_caption(pin) == "X0"
    s.set_pin_labels("A3", {"I0": "X0-field"})
    comp = s.sheet().component("A3")
    assert pin_caption(pin, comp) == "X0-field"


def test_keyence_and_smc_schemes():
    assert scheme_labels("keyence.x")["I3"] == "X3"
    assert scheme_labels("keyence.y")["Q4"] == "Y4"
    assert scheme_labels("smc.out")["Q0"] == "OUT0"
    assert scheme_labels("gc.si")["Si4"] == "Si4-Si5"
    assert scheme_labels("gc.si")["SOUTA"] == "S-OUT A"


def test_notes_box_fits_text_not_library_default():
    from panelcad.core.models import Component
    from panelcad.render.painter import notes_box

    short = Component(
        id="N1",
        library_id="iec.notes.block",
        x=20,
        y=20,
        properties={"text": "Si map only.\nTerminals TBD.", "width": 170},
    )
    w, h = notes_box(short)
    assert w == 170
    assert h < 80
    tall = Component(
        id="N2",
        library_id="iec.notes.block",
        x=20,
        y=20,
        properties={"text": "line\n" * 20, "width": 360},
    )
    _w2, h2 = notes_box(tall)
    assert h2 > h


def test_schematic_svg_draws_oem_pin_and_si(tmp_path: Path):
    s = Session.create("Si")
    s.place("oem.keyence.gc1000", ref="GC", x=180, y=80)
    s.place("iec.operators.pb_nc", ref="S2", x=40, y=40, properties={"tag": "Sfty_OpLeftDoor", "Si": "Si4-Si5"})
    svg = s.export_svg(tmp_path / "si.svg").read_text(encoding="utf-8")
    assert "Si4-Si5" in svg
    assert "S-OUT A" in svg
    assert "Sfty_OpLeftDoor" in svg


def test_extra_captions_include_si_pair():
    from panelcad.core.models import Component
    from panelcad.core.pins import extra_captions

    c = Component(id="S2", library_id="iec.operators.pb_nc", x=0, y=0, properties={"tag": "Sfty_OpLeftDoor", "Si": "Si4-Si5"})
    lines = extra_captions(c)
    assert "Sfty_OpLeftDoor" in lines
    assert "Si4-Si5" in lines


def test_coil_style_roundtrip():
    s = Session.create("Coils")
    s.set_title_block(coil_style="square")
    assert s.project.title_block.coil_style == "square"
    s.set_title_block(coil_style="circle")
    assert s.project.title_block.coil_style == "circle"
    y = s.place("iec.loads.solenoid", ref="Y1")
    assert y.library_id == "iec.loads.solenoid"


def test_oem_keyence_smc_library_loads():
    cat = Session.create("x").catalog
    assert "oem.keyence.kv_b16xc" in cat.devices
    assert "oem.smc.ex260_sen1" in cat.devices
    assert "oem.patlite.lr4_302wjnw_ryg" in cat.devices
    kv = cat.get("oem.keyence.kv_b16xc")
    assert kv.panel.dim_source == "catalog"
    assert kv.panel.width_mm == 28.5
    assert cat.get("oem.smc.ex260_sen1").panel.width_mm == 28.2
    gc = cat.get("oem.keyence.gc1000")
    assert gc.pin("Si4").label == "Si4-Si5"
    assert gc.pin("SOUTA").label == "S-OUT A"
