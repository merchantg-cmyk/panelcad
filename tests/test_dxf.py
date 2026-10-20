import pytest
from pathlib import Path

import ezdxf
from ezdxf import units as dxf_units

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.examples.dol import build_dol
from panelcad.library.panel import display_footprint_size, effective_footprint
from panelcad.render.dxf import LAYERS


REQUIRED_LAYERS = (
    "OUTLINE",
    "DEVICES",
    "WIRES",
    "TEXT",
    "WIRE_NUMBERS",
    "XREF",
    "DUCT",
    "RAIL",
    "CUTOUTS",
    "TITLEBLOCK",
)


def _open(path: Path):
    assert path.is_file()
    assert path.stat().st_size > 200
    return ezdxf.readfile(path)


def _texts(doc) -> list[str]:
    return [entity.dxf.text for entity in doc.modelspace().query("TEXT") if entity.dxf.text]


def _lw_boxes(doc, layer: str) -> list[tuple[float, float, float, float]]:
    boxes = []
    for entity in doc.modelspace().query("LWPOLYLINE"):
        if entity.dxf.layer != layer:
            continue
        points = [(float(p[0]), float(p[1])) for p in entity.get_points("xy")]
        if len(points) < 4:
            continue
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        boxes.append((min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)))
    return boxes


def _assert_units(doc) -> None:
    assert int(doc.units) == int(dxf_units.MM)
    assert int(doc.header["$INSUNITS"]) == int(dxf_units.MM)


def test_dxf_schematic_has_layers_text_and_titleblock(tmp_path: Path):
    session = build_dol()
    dest = tmp_path / "power.dxf"
    session.export_dxf(dest, sheet_id="power")
    doc = _open(dest)
    _assert_units(doc)
    for name in REQUIRED_LAYERS:
        assert name in doc.layers, name
    texts = _texts(doc)
    joined = " ".join(texts)
    assert "Q1" in joined
    assert "K1" in joined
    assert "Acme Process Controls" in joined
    assert "E-001" in joined
    assert any(entity.dxf.layer == "WIRES" for entity in doc.modelspace())
    assert any(entity.dxf.layer == "TITLEBLOCK" for entity in doc.modelspace())
    assert any(entity.dxftype() == "TEXT" for entity in doc.modelspace())
    lines = list(doc.modelspace().query("LINE")) + list(doc.modelspace().query("LWPOLYLINE"))
    assert len(lines) >= 8


@pytest.mark.confirm_generic_layout
def test_dxf_panel_true_scale_footprint(tmp_path: Path):
    session = Session.create("DXF scale")
    session.add_sheet("panel", kind="panel_layout")
    enc = session.sheet().enclosure
    assert enc is not None
    enc.x = 0.0
    enc.y = 0.0
    enc.width_mm = 400.0
    enc.height_mm = 400.0
    enc.draw_scale = 1.0
    enc.name = "400×400×200"
    session.place("iec.contactors.ac3_9a", ref="K1", x=80, y=90)
    device = session.catalog.get("iec.contactors.ac3_9a")
    width, height = display_footprint_size(device, session.sheet().component("K1"))
    assert abs(width - 45.0) < 0.05
    assert abs(height - 58.0) < 0.05
    dest = tmp_path / "panel.dxf"
    session.export_dxf(dest, sheet_id="panel")
    doc = _open(dest)
    _assert_units(doc)
    boxes = _lw_boxes(doc, "DEVICES")
    assert boxes, "expected a device outline"
    match = [box for box in boxes if abs(box[2] - width) < 0.2 and abs(box[3] - height) < 0.2]
    assert match, f"no {width}×{height} footprint in {boxes[:8]}"
    assert any(entity.dxf.layer == "OUTLINE" for entity in doc.modelspace())
    assert any("K1" == text for text in _texts(doc))


@pytest.mark.confirm_generic_layout
def test_dxf_unscales_layout_from_schematic(tmp_path: Path):
    session = build_dol()
    sheet = session.sheet("enclosure")
    assert sheet.enclosure is not None
    q1 = sheet.component("Q1")
    device = session.catalog.get(q1.library_id)
    drawn_w, drawn_h = display_footprint_size(device, q1)
    fp = effective_footprint(device, q1)
    true_w, true_h = float(fp.width_mm), float(fp.height_mm)
    assert true_w > 0 and true_h > 0
    dest = tmp_path / "enclosure.dxf"
    session.export_dxf(dest, sheet_id="enclosure")
    doc = _open(dest)
    _assert_units(doc)
    boxes = _lw_boxes(doc, "DEVICES")
    match = [box for box in boxes if abs(box[2] - true_w) < 0.35 and abs(box[3] - true_h) < 0.35]
    assert match, f"expected true {true_w}×{true_h} (drawn {drawn_w}×{drawn_h}); got {boxes[:12]}"
    assert any(entity.dxf.layer == "RAIL" for entity in doc.modelspace())
    assert any(entity.dxf.layer == "DUCT" for entity in doc.modelspace())
    assert any("Q1" == text for text in _texts(doc))


@pytest.mark.confirm_generic_layout
def test_dxf_door_cutouts_and_operators(tmp_path: Path):
    session = Session.create("Door DXF")
    session.set_title_block(company="ECS", drawing_number="E-DOOR", rev="A")
    session.add_sheet("door", kind="door_layout")
    session.place("iec.operators.estop", ref="S0", x=40, y=40)
    session.place("iec.operators.pb_no", ref="S1", x=100, y=40)
    session.place("iec.operators.pilot_light", ref="H1", x=160, y=40)
    dest = tmp_path / "door.dxf"
    session.export_dxf(dest, sheet_id="door")
    doc = _open(dest)
    _assert_units(doc)
    texts = set(_texts(doc))
    assert {"S0", "S1", "H1"} <= texts
    circles = [entity for entity in doc.modelspace().query("CIRCLE") if entity.dxf.layer == "CUTOUTS"]
    assert len(circles) >= 3
    radii = sorted(float(entity.dxf.radius) for entity in circles)
    assert any(abs(radius - 11.0) < 0.15 for radius in radii)
    assert any(entity.dxf.layer == "DEVICES" for entity in doc.modelspace())


def test_dxf_exports_builtin_project_every_sheet(tmp_path: Path):
    session = build_dol()
    paths = session.export_dxfs(tmp_path / "dxf")
    assert len(paths) == len(session.project.sheets)
    for path in paths:
        doc = _open(path)
        _assert_units(doc)
        for name in LAYERS:
            assert name in doc.layers
        assert len(list(doc.modelspace())) >= 4


def test_cli_print_dxf(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    src = tmp_path / "job.pcad.yaml"
    build_dol().save(src)
    dest = tmp_path / "power.dxf"
    result = CliRunner().invoke(app, ["print", str(src), "--dxf", str(dest), "--sheet", "power"])
    assert result.exit_code == 0, result.output
    doc = _open(dest)
    _assert_units(doc)
    assert "Q1" in " ".join(_texts(doc))


def _text_bbox(entity) -> tuple[float, float, float, float]:
    x = float(entity.dxf.insert[0])
    y = float(entity.dxf.insert[1])
    height = float(entity.dxf.height)
    width = len(entity.dxf.text) * height * 0.62
    halign = int(getattr(entity.dxf, "halign", 0) or 0)
    if halign == 1:
        x -= width / 2.0
    elif halign == 2:
        x -= width
    return (x, y, width, height)


def test_dxf_layout_omits_page_scale_note(tmp_path: Path):
    session = build_dol()
    enc = session.sheet("enclosure").enclosure
    assert enc is not None
    assert enc.draw_scale and enc.draw_scale < 0.999
    svg = session.export_svg(tmp_path / "enc.svg", sheet_id="enclosure").read_text(encoding="utf-8")
    assert "drawn 1:" in svg
    # main d639fc7 expected the DXF to omit a scale note. The title block now
    # prints the shop ratio (SCALE 1:2.5). Do not ban the substring "1:2":
    # it occurs inside "1:2.5".
    from panelcad.core.layout import STANDARD_DRAW_DENOMINATORS, format_draw_ratio

    ratio = format_draw_ratio(enc.draw_scale)
    assert any(abs(enc.draw_scale * denom - 1.0) <= 1e-3 for denom in STANDARD_DRAW_DENOMINATORS)
    dest = tmp_path / "enclosure.dxf"
    session.export_dxf(dest, sheet_id="enclosure")
    joined = " ".join(_texts(_open(dest)))
    assert "drawn 1:" not in joined
    assert ratio in joined


@pytest.mark.confirm_generic_layout
def test_dxf_footprint_captions_stay_inside_boxes(tmp_path: Path):
    session = build_dol()
    dest = tmp_path / "enclosure.dxf"
    session.export_dxf(dest, sheet_id="enclosure")
    doc = _open(dest)
    boxes = _lw_boxes(doc, "DEVICES")
    assert boxes
    overflow = []
    for entity in doc.modelspace().query("TEXT"):
        if entity.dxf.layer != "TEXT":
            continue
        x, y, tw, th = _text_bbox(entity)
        cx = x + tw / 2.0
        cy = y + th / 2.0
        host = None
        for bx, by, bw, bh in boxes:
            if bx - 0.4 <= cx <= bx + bw + 0.4 and by - 0.4 <= cy <= by + bh + 0.4:
                host = (bx, by, bw, bh)
                break
        if host is None:
            continue
        _bx, _by, bw, _bh = host
        if tw > bw + 0.35:
            overflow.append((entity.dxf.text, tw, bw))
    assert overflow == [], overflow
    texts = set(_texts(doc))
    assert "Q1" in texts
    assert "K1" in texts
    # Narrow I/O keeps a readable name, wrapped or shrunk, not a 16-char overflow.
    assert any(text.startswith("CLICK 16") or text.startswith("CLICK 8") for text in texts)


def test_dxf_title_block_label_does_not_overlap_value(tmp_path: Path):
    session = build_dol()
    dest = tmp_path / "power.dxf"
    session.export_dxf(dest, sheet_id="power")
    doc = _open(dest)
    stacked = []
    for entity in doc.modelspace().query("TEXT"):
        if entity.dxf.layer != "TITLEBLOCK":
            continue
        if int(getattr(entity.dxf, "halign", 0) or 0) != 0:
            continue
        stacked.append(_text_bbox(entity) + (entity.dxf.text,))
    overlaps = []
    for i, (ax, ay, aw, ah, at) in enumerate(stacked):
        for bx, by, bw, bh, bt in stacked[i + 1 :]:
            if abs(ax - bx) > 8.0:
                continue
            if ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by:
                overlaps.append((at, bt, ay, by))
    assert overlaps == [], overlaps


def test_agent_export_dxf(tmp_path: Path):
    src = tmp_path / "job.pcad.yaml"
    build_dol().save(src)
    dest = tmp_path / "agent.dxf"
    agent = Agent()
    assert agent.handle({"id": 1, "op": "open", "path": str(src)})["ok"]
    help_r = agent.handle({"id": 2, "op": "help"})
    assert "export_dxf" in help_r["result"]["ops"]
    reply = agent.handle({"id": 3, "op": "export_dxf", "path": str(dest), "sheet": "power"})
    assert reply["ok"] is True, reply
    doc = _open(dest)
    assert "Q1" in " ".join(_texts(doc))
