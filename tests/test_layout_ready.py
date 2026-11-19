"""George's rule: layout never packs a generic/placeholder size."""

import math
from pathlib import Path

import pytest

from panelcad import Session
from panelcad.ai.drawing import drawing_agent, open_drawing
from panelcad.cli.agent import Agent
from panelcad.core.checks import layout_build_blockers, require_layout_ready_for_build, run_checks
from panelcad.core.cutout import collect_features
from panelcad.core.enclosure import collect_parts, recommend_enclosure
from panelcad.core.layout import footprint_size, pack_din
from panelcad.core.models import Component, Device, Rail
from panelcad.examples.dol import build_dol
from panelcad.library.panel import (
    GENERIC_BY_CATEGORY,
    LayoutNotReadyError,
    apply_confirmed_envelope,
    effective_footprint,
    is_info_needed_placeholder,
    is_layout_ready,
    layout_ready_gap,
    max_confirmed_envelope_mm,
    occupies_panel_layout,
    require_explicit_envelope,
)
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel


def _generic_session() -> Session:
    s = Session.create("Generic")
    s.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    s.place("iec.contactors.ac3_9a", ref="K1", x=40, y=20)
    return s


def _dispose_qt(*widgets) -> None:
    """Hide and delete test widgets so they are not alive at interpreter exit."""

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    for widget in widgets:
        if widget is None:
            continue
        session_holder = getattr(widget, "_session", None)
        if session_holder is not None:
            widget._session = None
        setter = getattr(widget, "set_session", None)
        if callable(setter):
            try:
                setter(None)
            except Exception:
                pass
        widget.hide()
        widget.close()
        widget.deleteLater()
    if app is not None:
        app.processEvents()


def test_generic_library_symbol_is_not_layout_ready():
    s = _generic_session()
    device = s.catalog.get("iec.breakers.mcb_1p_16a")
    q1 = s.sheet().component("Q1")
    assert occupies_panel_layout(device, q1)
    assert not is_layout_ready(device, q1)
    assert "catalog part" in layout_ready_gap(device, q1)
    w, h = footprint_size(device, q1)
    assert w > 0 and h > 0  # generic table still exists for display / existing YAML


def test_oem_catalog_part_is_layout_ready():
    s = Session.create("OEM")
    h = s.place("oem.patlite.lr4_302wjnw_ryg", ref="H1", x=40, y=40)
    device = s.catalog.get(h.library_id)
    assert is_layout_ready(device, h)
    w, height = footprint_size(device, h)
    assert w > 0 and height > 0


def test_confirmed_envelope_makes_generic_layout_ready():
    s = _generic_session()
    row = s.confirm_envelope(
        "Q1",
        width_mm=17.5,
        height_mm=89,
        depth_mm=69,
        mount="din_rail",
        confirmed_by="George",
    )
    q1 = s.sheet().component("Q1")
    device = s.catalog.get(q1.library_id)
    assert is_layout_ready(device, q1)
    assert q1.properties["envelope_confirmed"] is True
    assert q1.properties["envelope_confirmed_by"] == "George"
    assert q1.properties["envelope_confirmed_at"]
    assert row["panel"]["dim_source"] == "measured"
    assert footprint_size(device, q1) == (17.5, 89.0)


def test_measured_without_confirm_flag_is_not_ready():
    s = _generic_session()
    s.set_panel("Q1", width_mm=17.5, height_mm=89, depth_mm=69, mount="din_rail", dim_source="measured")
    q1 = s.sheet().component("Q1")
    device = s.catalog.get(q1.library_id)
    assert not is_layout_ready(device, q1)


def test_place_on_layout_refuses_generic():
    s = Session.create("Place")
    s.add_sheet("enclosure", kind="panel_layout")
    with pytest.raises(ValueError, match="not layout-ready"):
        s.place("iec.breakers.mcb_1p_16a", ref="Q1", x=40, y=40, sheet_id="enclosure")
    assert s.sheet("enclosure").components == []


def test_place_on_layout_allows_catalog_and_confirmed():
    s = Session.create("Place ok")
    s.add_sheet("enclosure", kind="panel_layout")
    s.add_sheet("power", kind="schematic")
    s.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20, sheet_id="power")
    s.confirm_envelope("Q1", width_mm=18, height_mm=80, depth_mm=70, mount="din_rail", confirmed_by="test")
    placed = s.place_from_bom("iec.breakers.mcb_1p_16a", ref="Q1", x=40, y=40, sheet_id="enclosure")
    assert placed["placed"] is True
    h = s.place("oem.patlite.lr4_302wjnw_ryg", ref="H1", x=80, y=40, sheet_id="enclosure")
    assert h.id == "H1"


def test_cli_place_refuses_generic_on_layout(tmp_path: Path):
    s = Session.create("CLI")
    s.add_sheet("enclosure", kind="panel_layout")
    dest = tmp_path / "job.pcad.yaml"
    s.save(dest)
    agent = Agent()
    assert agent.handle({"id": 1, "op": "open", "path": str(dest)})["ok"]
    reply = agent.handle(
        {
            "id": 2,
            "op": "place",
            "library_id": "iec.contactors.ac3_9a",
            "ref": "K1",
            "x": 40,
            "y": 40,
            "sheet": "enclosure",
        }
    )
    assert reply["ok"] is False
    assert "not layout-ready" in str(reply.get("error") or "")
    assert "crash" not in reply


def test_layout_from_schematic_leaves_generics_unplaced():
    s = _generic_session()
    s.add_sheet("enclosure", kind="panel_layout")
    result = s.layout_from_schematic()
    assert result["din"] == 0
    assert result["field"] == 0
    refs = {row["ref"] for row in result["unplaced"]}
    assert "Q1" in refs and "K1" in refs
    assert s.sheet("enclosure").components == []


def test_pack_din_skips_generic_table_size():
    s = _generic_session()
    device = s.catalog.get("iec.breakers.mcb_1p_16a")
    q1 = s.sheet().component("Q1")
    rails = [Rail(id="r1", x=40, y=80, length_mm=400)]
    placed, _live = pack_din([("Q1", device, q1)], rails)
    assert placed == []


def test_enclosure_sizing_refuses_generic_sizes():
    s = _generic_session()
    s.add_sheet("enclosure", kind="panel_layout")
    cabinet, door = collect_parts(s.project, s.catalog)
    assert cabinet
    assert all(part.missing_size for part in cabinet)
    rec = recommend_enclosure(s.project, s.catalog)
    assert "Q1" in rec["missing_size"]
    assert "K1" in rec["missing_size"]
    packed = rec["needed"]["packed_width_mm"]
    # Empty pack must not grow a box from GENERIC_BY_CATEGORY (18×80 breaker).
    breaker = GENERIC_BY_CATEGORY["protection.breaker"]
    assert packed < breaker[0] + 200


def test_enclosure_sizing_uses_confirmed_envelope():
    s = _generic_session()
    s.confirm_envelope("Q1", width_mm=17.5, height_mm=89, depth_mm=69, mount="din_rail", confirmed_by="test")
    s.confirm_envelope("K1", width_mm=45, height_mm=75, depth_mm=85, mount="din_rail", confirmed_by="test")
    rec = recommend_enclosure(s.project, s.catalog)
    assert "Q1" not in rec["missing_size"]
    assert "K1" not in rec["missing_size"]
    assert rec["needed"]["packed_width_mm"] >= 17.5


def test_insert_circuit_does_not_pack_generic_symbols():
    s = Session.create("Circuit")
    result = s.insert_circuit(kind="dol", place_layout=True)
    layout = result.get("layout") or {}
    assert layout.get("din", 0) == 0
    warnings = " ".join(layout.get("warnings") or [])
    assert "not layout-ready" in warnings or layout.get("din") == 0
    panel = next((sh for sh in s.project.sheets if sh.kind == "panel_layout"), None)
    if panel is not None:
        assert panel.components == [] or all(
            is_layout_ready(s.catalog.get(c.library_id), c) for c in panel.components
        )


def test_cutouts_skip_generic_body():
    s = Session.create("Cuts")
    s.add_sheet("door", kind="door_layout")
    s.sheet("door").components.append(
        Component(id="S1", library_id="iec.operators.pb_no", x=40, y=40)
    )
    grouped = collect_features(s.project, s.catalog)
    door = grouped.get("door") or []
    assert all(feat.ref != "S1" for feat in door)


def test_problems_lists_layout_not_ready_as_error():
    s = _generic_session()
    s.add_sheet("enclosure", kind="panel_layout")
    rows = [row for row in s.design_checks() if row["check"] == "layout_not_ready"]
    refs = {row["ref"] for row in rows}
    assert "Q1" in refs and "K1" in refs
    assert all(row["severity"] == "error" for row in rows)
    assert all("catalog part" in row["message"] or "envelope" in row["message"] for row in rows)


def test_existing_project_with_generic_on_layout_opens():
    s = build_dol(confirm_envelopes=False)
    # DOL packs whatever it can; leftover generics must not crash checks.
    rows = s.design_checks()
    assert isinstance(rows, list)
    assert s.validate() == []
    assert any(row["check"] == "layout_not_ready" for row in rows)


def test_print_package_and_quote_blocked(tmp_path: Path):
    s = _generic_session()
    s.add_sheet("enclosure", kind="panel_layout")
    # Existing YAML may already have a generic on the layout.
    s.sheet("enclosure").components.append(
        Component(id="Q1", library_id="iec.breakers.mcb_1p_16a", x=40, y=40)
    )
    with pytest.raises(ValueError, match="NOT FOR BUILD"):
        require_layout_ready_for_build(s.project, s.catalog)
    with pytest.raises(ValueError, match="NOT FOR BUILD"):
        s.print_package(tmp_path / "pack.pdf")
    with pytest.raises(ValueError, match="NOT FOR BUILD"):
        s.quote_export(tmp_path / "quote.xlsx")
    blockers = layout_build_blockers(s.project, s.catalog)
    assert blockers


def test_print_package_allowed_after_envelopes(tmp_path: Path):
    s = Session.create("Ready pack")
    s.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    s.confirm_envelope("Q1", width_mm=18, height_mm=80, depth_mm=70, mount="din_rail", confirmed_by="test")
    s.add_sheet("enclosure", kind="panel_layout")
    s.place_from_bom("iec.breakers.mcb_1p_16a", ref="Q1", x=40, y=40, sheet_id="enclosure")
    result = s.print_package(tmp_path / "ok.pdf")
    assert Path(result["path"]).is_file()


def test_agent_confirm_envelope_and_place():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Env"})["ok"]
    assert agent.handle(
        {"id": 2, "op": "place", "library_id": "iec.breakers.mcb_1p_16a", "ref": "Q1", "x": 20, "y": 20}
    )["ok"]
    assert agent.handle({"id": 3, "op": "add_sheet", "sheet_id": "enclosure", "kind": "panel_layout"})["ok"]
    denied = agent.handle(
        {"id": 4, "op": "place", "library_id": "iec.breakers.mcb_1p_16a", "ref": "Q1", "x": 40, "y": 40, "sheet": "enclosure"}
    )
    assert denied["ok"] is False
    ok = agent.handle(
        {
            "id": 5,
            "op": "confirm_envelope",
            "ref": "Q1",
            "width_mm": 18,
            "height_mm": 80,
            "depth_mm": 70,
            "mount": "din_rail",
            "confirmed_by": "agent-test",
        }
    )
    assert ok["ok"] is True
    placed = agent.handle(
        {"id": 6, "op": "place_from_bom", "library_id": "iec.breakers.mcb_1p_16a", "ref": "Q1", "x": 40, "y": 40, "sheet": "enclosure"}
    )
    assert placed["ok"] is True


def test_ai_place_tool_refuses_generic_on_layout():
    drawing = open_drawing(name="AI layout")
    drawing.operator.handle({"op": "add_sheet", "sheet_id": "enclosure", "kind": "panel_layout"})
    drawing.operator.handle({"op": "set_active_sheet", "sheet": "enclosure"})

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(isinstance(part, ToolCallPart) for m in messages for part in getattr(m, "parts", [])):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="place",
                        args={"library_id": "iec.breakers.mcb_1p_16a", "x": 40, "y": 40, "ref": "Q1"},
                        tool_call_id="p1",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="stopped")])

    with drawing_agent.override(model=FunctionModel(model)):
        drawing_agent.run_sync("Put a breaker on the backpanel.", deps=drawing)
    panel = drawing.operator.session.sheet("enclosure")
    assert all(c.id != "Q1" for c in panel.components)


def test_apply_confirmed_envelope_rejects_zero():
    comp = Component(id="Q1", library_id="iec.breakers.mcb_1p_16a", x=0, y=0)
    with pytest.raises(ValueError, match="width_mm"):
        apply_confirmed_envelope(comp, width_mm=0, height_mm=80, depth_mm=70, mount="din_rail")


def test_run_checks_includes_layout_not_ready_id():
    assert "layout_not_ready" in {item.check for item in run_checks(Session.create("x").project, Session.create("y").catalog)} or True
    s = _generic_session()
    s.add_sheet("enclosure", kind="panel_layout")
    found = {item.check for item in run_checks(s.project, s.catalog)}
    assert "layout_not_ready" in found


def test_io_rack_refuses_placeholder_module():
    s = Session.create("IO rack")
    s.io_add_module("ab.5069_ib16", ref="IB1")
    s.io_generate_sheets()
    assert all(
        sheet.kind != "panel_layout" or all(comp.id != "IB1" for comp in sheet.components)
        for sheet in s.project.sheets
    )
    card = next(
        comp
        for sheet in s.project.sheets
        for comp in sheet.components
        if comp.id == "IB1" and comp.properties.get("io_module")
    )
    assert card.properties.get("placeholder_footprint") is True


def test_io_rack_places_catalog_module():
    s = Session.create("IO catalog")
    s.io_add_module("brx.bx_08nd3", ref="A1")
    s.io_generate_sheets()
    panel = next(sheet for sheet in s.project.sheets if sheet.kind == "panel_layout")
    foot = panel.component("A1")
    assert foot.properties.get("io_module_ref") == "brx.bx_08nd3"
    device = s.catalog.get(foot.library_id)
    assert is_layout_ready(device, foot)


def test_terminal_strip_does_not_copy_generic_terminals_to_panel():
    from panelcad.core.terminal_strip import open_terminal_strip

    s = Session.create("Strip")
    s.place("iec.terminals.x_2p5", ref="X1-1", x=40, y=20)
    s.place("iec.terminals.x_2p5", ref="X1-2", x=40, y=50)
    open_terminal_strip(s, "X1")
    assert all(
        sheet.kind != "panel_layout" or all(comp.id not in {"X1-1", "X1-2"} for comp in sheet.components)
        for sheet in s.project.sheets
    )


def test_inspector_envelope_dialog_confirms(monkeypatch):
    from PySide6.QtWidgets import QApplication, QDialog

    from panelcad.gui.envelope_dialog import EnvelopeDialog
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = _generic_session()
    s.add_sheet("enclosure", kind="panel_layout")
    win = MainWindow(s)
    dialog = EnvelopeDialog(s, "Q1", win)
    try:
        dialog.width.setValue(17.5)
        dialog.height.setValue(89)
        dialog.depth.setValue(69)
        dialog.mount.setCurrentText("din_rail")
        dialog.who.setText("George")
        monkeypatch.setattr(EnvelopeDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
        result = dialog.apply()
        assert result["confirmed"] is True
        q1 = next(comp for sheet in s.project.sheets for comp in sheet.components if comp.id == "Q1")
        assert is_layout_ready(s.catalog.get(q1.library_id), q1)
        assert q1.properties["envelope_confirmed_by"] == "George"
    finally:
        _dispose_qt(dialog, win)
        _ = app


def test_set_panel_confirm_without_numbers_raises():
    s = _generic_session()
    with pytest.raises(ValueError, match="explicit"):
        s.set_panel("Q1", confirm=True)
    with pytest.raises(ValueError, match="explicit"):
        s.set_panel("Q1", confirm=True, width_mm=18, height_mm=80, depth_mm=70)
    q1 = s.sheet().component("Q1")
    assert q1.properties.get("envelope_confirmed") not in {True, "yes", "1"}
    assert not is_layout_ready(s.catalog.get(q1.library_id), q1)


def test_confirmed_envelope_rejects_zero_negative_absurd():
    s = _generic_session()
    limit = max_confirmed_envelope_mm()
    with pytest.raises(LayoutNotReadyError, match="at least 1"):
        s.confirm_envelope("Q1", width_mm=0, height_mm=80, depth_mm=70, mount="din_rail")
    with pytest.raises(LayoutNotReadyError, match="at least 1"):
        s.set_panel("Q1", confirm=True, width_mm=-18, height_mm=80, depth_mm=70, mount="din_rail")
    with pytest.raises(ValueError, match="larger than"):
        s.confirm_envelope(
            "Q1",
            width_mm=limit + 50,
            height_mm=80,
            depth_mm=70,
            mount="din_rail",
        )
    with pytest.raises(ValueError, match="explicit|mount"):
        require_explicit_envelope(18, 80, 70, None)
    q1 = s.sheet().component("Q1")
    assert not is_layout_ready(s.catalog.get(q1.library_id), q1)


def test_dxf_door_cutout_skips_generic_and_lists_unresolved(tmp_path: Path):
    from panelcad.core.cutout import unresolved_cutout_refs
    from panelcad.core.models import Enclosure
    from panelcad.render.dxf import draw_door_cutouts, sheet_to_dxf

    s = Session.create("DXF door")
    s.add_sheet("door", kind="door_layout")
    door = s.sheet("door")
    door.enclosure = Enclosure(x=0, y=0, width_mm=400, height_mm=400, depth_mm=200)
    door.components.append(Component(id="S1", library_id="iec.operators.pb_no", x=40, y=40))

    class Capture:
        def __init__(self) -> None:
            self.circles: list[tuple[float, float, float]] = []
            self.rects: list[tuple[float, float, float, float]] = []
            self.texts: list[str] = []

        def set_layer(self, name: str) -> None:
            return

        def set_stroke(self, color, width=0.35) -> None:
            return

        def set_fill(self, color) -> None:
            return

        def circle(self, x: float, y: float, r: float) -> None:
            self.circles.append((x, y, r))

        def rect(self, x: float, y: float, w: float, h: float) -> None:
            self.rects.append((x, y, w, h))

        def text(self, x, y, text, size, align="middle", rotate=0, **kwargs):
            self.texts.append(str(text))

    painter = Capture()
    unresolved = draw_door_cutouts(painter, door, s.catalog)
    assert "S1" in unresolved
    assert painter.circles == []
    assert painter.rects == []
    assert any("UNRESOLVED" in row and "S1" in row for row in painter.texts)
    assert ("door", "S1") in unresolved_cutout_refs(s.project, s.catalog)

    dest = tmp_path / "door.dxf"
    sheet_to_dxf(dest, door, s.catalog, project=s.project)
    blob = dest.read_text(errors="replace")
    assert "UNRESOLVED" in blob
    assert "S1" in blob


def test_info_needed_placeholder_is_not_layout_ready():
    s = Session.create("Reconstruct")
    ghost = Component(
        id="UNK1",
        library_id="reconstruct.needs_info",
        x=10,
        y=10,
        properties={"reconstruct_needs_info": "yes", "in_panel": "no"},
    )
    s.sheet().components.append(ghost)
    device = Device(
        library_id="reconstruct.needs_info",
        id="needs_info",
        name="INFO NEEDED",
        category="misc",
    )
    assert is_info_needed_placeholder(ghost)
    assert occupies_panel_layout(device, ghost)
    assert not is_layout_ready(device, ghost)
    assert "INFO NEEDED" in layout_ready_gap(device, ghost)
    s.place("iec.breakers.mcb_1p_16a", ref="UNK2", x=20, y=20)
    marked = s.sheet().component("UNK2")
    marked.properties["reconstruct_needs_info"] = "yes"
    s.add_sheet("enclosure", kind="panel_layout")
    with pytest.raises(ValueError, match="not layout-ready|INFO NEEDED"):
        s.place_from_bom("iec.breakers.mcb_1p_16a", ref="UNK2", x=40, y=40, sheet_id="enclosure")


def test_bom_blanks_generic_table_dimensions():
    from panelcad.io.bom import instance_rows

    s = _generic_session()
    row = next(r for r in instance_rows(s.project, s.catalog) if r["ref"] == "Q1")
    assert row["dim_source"] == "generic"
    assert row["width_mm"] == ""
    assert row["height_mm"] == ""
    assert row["depth_mm"] == ""
    s.confirm_envelope("Q1", width_mm=17.5, height_mm=89, depth_mm=69, mount="din_rail")
    ready = next(r for r in instance_rows(s.project, s.catalog) if r["ref"] == "Q1")
    assert ready["width_mm"] == "17.5"
    assert ready["dim_source"] == "measured"


def _assert_not_ready_unplaced(session: Session, ref: str) -> None:
    comp = next(c for sheet in session.project.sheets for c in sheet.components if c.id == ref)
    device = session.catalog.get(comp.library_id)
    assert not is_layout_ready(device, comp)
    session.add_sheet("enclosure", kind="panel_layout")
    result = session.layout_from_schematic()
    refs = {row["ref"] for row in result["unplaced"]}
    assert ref in refs
    assert all(c.id != ref for c in session.sheet("enclosure").components)


def test_set_panel_without_confirm_does_not_write_catalog():
    s = _generic_session()
    s.set_panel("K1")
    k1 = s.sheet().component("K1")
    assert k1.properties.get("dim_source") != "catalog"
    _assert_not_ready_unplaced(s, "K1")


def test_set_panel_absurd_sizes_do_not_become_ready():
    s = _generic_session()
    s.set_panel("K1", width_mm=99999, height_mm=0.001)
    k1 = s.sheet().component("K1")
    device = s.catalog.get(k1.library_id)
    assert not is_layout_ready(device, k1)
    assert k1.properties.get("dim_source") != "catalog"
    _assert_not_ready_unplaced(s, "K1")


def test_agent_set_panel_tiny_sizes_stay_not_ready():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Tiny"})["ok"]
    assert agent.handle(
        {"id": 2, "op": "place", "library_id": "iec.contactors.ac3_9a", "ref": "K1", "x": 20, "y": 20}
    )["ok"]
    reply = agent.handle({"id": 3, "op": "set_panel", "ref": "K1", "width_mm": 1, "height_mm": 1})
    assert reply["ok"] is True
    assert "crash" not in reply
    s = agent.session
    k1 = s.sheet().component("K1")
    assert k1.properties.get("dim_source") != "catalog"
    _assert_not_ready_unplaced(s, "K1")


def test_agent_set_panel_confirm_false_does_not_launder_catalog():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "NoConfirm"})["ok"]
    assert agent.handle(
        {"id": 2, "op": "place", "library_id": "iec.contactors.ac3_9a", "ref": "K1", "x": 20, "y": 20}
    )["ok"]
    reply = agent.handle({"id": 3, "op": "set_panel", "ref": "K1", "confirm": "false"})
    assert reply["ok"] is True
    k1 = agent.session.sheet().component("K1")
    assert k1.properties.get("dim_source") != "catalog"
    _assert_not_ready_unplaced(agent.session, "K1")


def test_apply_oem_without_sizes_stays_not_ready():
    s = _generic_session()
    with pytest.raises(LayoutNotReadyError, match="width_mm"):
        s.apply_oem("K1")
    k1 = s.sheet().component("K1")
    assert k1.properties.get("dim_source") != "catalog"
    _assert_not_ready_unplaced(s, "K1")


def test_agent_update_oem_without_sizes_stays_not_ready():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "OEM"})["ok"]
    assert agent.handle(
        {"id": 2, "op": "place", "library_id": "iec.contactors.ac3_9a", "ref": "K1", "x": 20, "y": 20}
    )["ok"]
    reply = agent.handle({"id": 3, "op": "update_oem", "ref": "K1"})
    assert reply["ok"] is False
    assert "crash" not in reply
    k1 = agent.session.sheet().component("K1")
    assert k1.properties.get("dim_source") != "catalog"
    _assert_not_ready_unplaced(agent.session, "K1")


def test_set_properties_dim_source_catalog_does_not_launder():
    s = _generic_session()
    s.set_component_properties("K1", {"dim_source": "catalog"})
    k1 = s.sheet().component("K1")
    assert k1.properties.get("dim_source") != "catalog"
    _assert_not_ready_unplaced(s, "K1")


def test_agent_set_properties_dim_source_catalog():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Props"})["ok"]
    assert agent.handle(
        {"id": 2, "op": "place", "library_id": "iec.contactors.ac3_9a", "ref": "K1", "x": 20, "y": 20}
    )["ok"]
    reply = agent.handle(
        {"id": 3, "op": "set_properties", "ref": "K1", "properties": {"dim_source": "catalog"}}
    )
    assert reply["ok"] is True
    k1 = agent.session.sheet().component("K1")
    assert k1.properties.get("dim_source") != "catalog"
    _assert_not_ready_unplaced(agent.session, "K1")


def test_place_caller_properties_cannot_launder_catalog():
    s = Session.create("Place props")
    s.add_sheet("enclosure", kind="panel_layout")
    with pytest.raises(LayoutNotReadyError, match="not layout-ready"):
        s.place(
            "iec.contactors.ac3_9a",
            ref="K1",
            x=40,
            y=40,
            sheet_id="enclosure",
            properties={"dim_source": "catalog", "width_mm": 45, "height_mm": 58, "depth_mm": 57},
        )
    assert s.sheet("enclosure").components == []


def test_agent_place_caller_properties_cannot_launder_catalog():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "PlaceJSONL"})["ok"]
    assert agent.handle({"id": 2, "op": "add_sheet", "sheet_id": "enclosure", "kind": "panel_layout"})["ok"]
    reply = agent.handle(
        {
            "id": 3,
            "op": "place",
            "library_id": "iec.contactors.ac3_9a",
            "ref": "K1",
            "x": 40,
            "y": 40,
            "sheet": "enclosure",
            "properties": {"dim_source": "catalog"},
        }
    )
    assert reply["ok"] is False
    assert "not layout-ready" in str(reply.get("error") or "")
    assert "crash" not in reply
    assert agent.session.sheet("enclosure").components == []


def test_dol_example_does_not_confirm_table_fills():
    s = build_dol()
    assigned = {}
    for sheet in s.project.sheets:
        for comp in sheet.components:
            assert comp.properties.get("envelope_confirmed_by") != "example"
            if comp.id in {"Q1", "K1", "F1", "G1", "A1"} and sheet.kind == "schematic":
                assigned[comp.id] = comp
    for ref in ("Q1", "K1", "F1", "G1", "A1"):
        assert ref in assigned
        device = s.catalog.get(assigned[ref].library_id)
        assert is_layout_ready(device, assigned[ref])
        assert assigned[ref].properties.get("catalog_key")
    not_ready = {row["ref"] for row in s.design_checks() if row["check"] == "layout_not_ready"}
    # Operators / HMI without a published body stay off the layout.
    assert "S0" in not_ready or all(
        c.id != "S0" for sheet in s.project.sheets if sheet.kind in {"panel_layout", "door_layout"} for c in sheet.components
    )


def test_confirmed_envelope_rejects_nan():
    s = _generic_session()
    with pytest.raises(LayoutNotReadyError, match="finite"):
        s.confirm_envelope("Q1", width_mm=math.nan, height_mm=80, depth_mm=70, mount="din_rail")
    with pytest.raises(LayoutNotReadyError, match="finite"):
        s.set_panel(
            "Q1",
            confirm=True,
            width_mm=18,
            height_mm=float("nan"),
            depth_mm=70,
            mount="din_rail",
        )
    q1 = s.sheet().component("Q1")
    assert q1.properties.get("envelope_confirmed") not in {True, "yes", "1"}
    assert not is_layout_ready(s.catalog.get(q1.library_id), q1)


def test_confirmed_envelope_rejects_near_zero():
    s = _generic_session()
    with pytest.raises(LayoutNotReadyError, match="at least 1"):
        s.confirm_envelope("Q1", width_mm=0.001, height_mm=0.001, depth_mm=0.001, mount="din_rail")
    q1 = s.sheet().component("Q1")
    assert not is_layout_ready(s.catalog.get(q1.library_id), q1)


def test_envelope_dialog_rejects_near_zero(monkeypatch):
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.envelope_dialog import EnvelopeDialog
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = _generic_session()
    win = MainWindow(s)
    dialog = EnvelopeDialog(s, "Q1", win)
    try:
        dialog.width.setValue(0.01)
        dialog.height.setValue(0.01)
        dialog.depth.setValue(0.01)
        dialog.mount.setCurrentText("din_rail")
        with pytest.raises((LayoutNotReadyError, ValueError), match="at least 1|finite"):
            dialog.apply()
        q1 = s.sheet().component("Q1")
        assert not is_layout_ready(s.catalog.get(q1.library_id), q1)
    finally:
        _dispose_qt(dialog, win)
        _ = app


def test_cli_place_prints_one_line_and_writes_no_crash(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.cli.main import app
    from panelcad.diag.paths import crashes_dir

    s = Session.create("CLI place")
    s.add_sheet("enclosure", kind="panel_layout")
    dest = tmp_path / "job.pcad.yaml"
    s.save(dest)
    before = {p.name for p in crashes_dir().glob("*.json")}
    result = CliRunner().invoke(
        app,
        ["place", str(dest), "--lib", "iec.contactors.ac3_9a", "--ref", "K1", "--sheet", "enclosure"],
    )
    assert result.exit_code == 1
    assert "not layout-ready" in (result.output or "")
    assert "Traceback" not in (result.output or "")
    after = {p.name for p in crashes_dir().glob("*.json")}
    assert after == before


def test_assign_part_without_catalog_depth_blanks_bom_depth():
    from panelcad.core.enclosure import collect_parts
    from panelcad.io.bom import instance_rows

    s = Session.create("Duct")
    s.place("iec.wire.duct", ref="WD1", x=20, y=20)
    s.assign_part("WD1", "T1-1522G-1", manufacturer="AutomationDirect")
    wd = s.sheet().component("WD1")
    device = s.catalog.get(wd.library_id)
    assert is_layout_ready(device, wd)
    row = next(r for r in instance_rows(s.project, s.catalog) if r["ref"] == "WD1")
    assert row["width_mm"] == "40"
    assert row["height_mm"] == "60"
    assert row["depth_mm"] == ""
    cabinet, _door = collect_parts(s.project, s.catalog)
    part = next(p for p in cabinet if p.ref == "WD1")
    assert part.depth_mm == 0.0


def test_strip_hardware_does_not_invent_generic_depth():
    from panelcad.core.enclosure import collect_strip_hardware

    s = Session.create("Strip")
    s.place("iec.terminals.x_2p5", ref="X1-1", x=40, y=20)
    s.confirm_envelope("X1-1", width_mm=5.2, height_mm=47, depth_mm=48, mount="din_rail", confirmed_by="test")
    parts = collect_strip_hardware(s.project, s.catalog)
    term = next(p for p in parts if p.ref == "X1-1")
    assert term.depth_mm == 48.0
    assert term.missing_size is False
    ends = [p for p in parts if "end_stop" in p.library_id or "end_plate" in p.library_id]
    for end in ends:
        assert end.width_mm == 0.0
        assert end.height_mm == 0.0
        assert end.depth_mm == 0.0
        assert end.missing_size is True


def test_cutout_published_sku_does_not_use_generic_body():
    from panelcad.core.cutout import catalog_cutout, collect_features
    from panelcad.core.models import Enclosure

    s = Session.create("Cut body")
    s.place("iec.operators.pb_no", ref="S1", x=20, y=20)
    s.assign_part("S1", "800FP-F3", manufacturer="Allen-Bradley")
    source = s.sheet().component("S1")
    s.add_sheet("door", kind="door_layout")
    door = s.sheet("door")
    door.enclosure = Enclosure(x=0, y=0, width_mm=400, height_mm=400, depth_mm=200)
    door.components.append(
        Component(
            id="S1",
            library_id=source.library_id,
            x=40,
            y=40,
            properties=dict(source.properties),
        )
    )
    device = s.catalog.get("iec.operators.pb_no")
    assert not is_layout_ready(device, door.component("S1"))
    published = catalog_cutout("Allen-Bradley", "800FP-F3")
    assert published is not None and not published.unknown
    grouped = collect_features(s.project, s.catalog)
    door_feats = grouped.get("door") or []
    feat = next(f for f in door_feats if f.ref == "S1")
    generic_w, generic_h = GENERIC_BY_CATEGORY["operator.pushbutton"][:2]
    assert feat.footprint_w != generic_w or feat.footprint_h != generic_h
    assert feat.cutout.diameter_mm == published.diameter_mm
    assert feat.footprint_w == pytest.approx(float(published.diameter_mm))
    assert feat.footprint_h == pytest.approx(float(published.diameter_mm))


def test_set_properties_forged_catalog_key_is_not_ready():
    s = _generic_session()
    s.set_component_properties(
        "K1",
        {"catalog_key": "fake|x", "catalog_verified": "full", "width_mm": 1, "height_mm": 1},
    )
    k1 = s.sheet().component("K1")
    device = s.catalog.get(k1.library_id)
    assert k1.properties.get("catalog_key") not in {"fake|x"}
    assert not is_layout_ready(device, k1)
    gap = layout_ready_gap(device, k1)
    assert "not a parts-library row" in gap or "generic" in gap or "not confirmed" in gap
    _assert_not_ready_unplaced(s, "K1")


def test_set_properties_forged_catalog_absurd_size_is_not_ready():
    s = _generic_session()
    s.set_component_properties(
        "K1",
        {
            "catalog_key": "fake|x",
            "catalog_verified": "full",
            "dim_source": "catalog",
            "width_mm": 99999,
            "height_mm": 0.001,
        },
    )
    k1 = s.sheet().component("K1")
    assert k1.properties.get("dim_source") != "catalog"
    assert k1.properties.get("catalog_key") not in {"fake|x"}
    assert not is_layout_ready(s.catalog.get(k1.library_id), k1)


def test_agent_set_properties_forged_catalog_key():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "ForgeKey"})["ok"]
    assert agent.handle(
        {"id": 2, "op": "place", "library_id": "iec.contactors.ac3_9a", "ref": "K1", "x": 20, "y": 20}
    )["ok"]
    reply = agent.handle(
        {
            "id": 3,
            "op": "set_properties",
            "ref": "K1",
            "properties": {
                "catalog_key": "fake|x",
                "catalog_verified": "full",
                "width_mm": 1,
                "height_mm": 1,
            },
        }
    )
    assert reply["ok"] is True
    k1 = agent.session.sheet().component("K1")
    assert k1.properties.get("catalog_key") not in {"fake|x"}
    _assert_not_ready_unplaced(agent.session, "K1")


def test_place_forged_catalog_key_refused_on_layout():
    s = Session.create("Place forge")
    s.add_sheet("enclosure", kind="panel_layout")
    with pytest.raises(LayoutNotReadyError, match="not layout-ready"):
        s.place(
            "iec.contactors.ac3_9a",
            ref="K1",
            x=40,
            y=40,
            sheet_id="enclosure",
            properties={
                "catalog_key": "fake|x",
                "catalog_verified": "full",
                "width_mm": 45,
                "height_mm": 58,
            },
        )
    assert s.sheet("enclosure").components == []


def test_agent_place_forged_catalog_key_refused():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "PlaceForge"})["ok"]
    assert agent.handle({"id": 2, "op": "add_sheet", "sheet_id": "enclosure", "kind": "panel_layout"})["ok"]
    reply = agent.handle(
        {
            "id": 3,
            "op": "place",
            "library_id": "iec.contactors.ac3_9a",
            "ref": "K1",
            "x": 40,
            "y": 40,
            "sheet": "enclosure",
            "properties": {
                "catalog_key": "fake|x",
                "catalog_verified": "yes",
                "width_mm": 45,
                "height_mm": 58,
            },
        }
    )
    assert reply["ok"] is False
    assert "not layout-ready" in str(reply.get("error") or "")
    assert agent.session.sheet("enclosure").components == []


def test_import_bom_forged_catalog_key_is_not_ready(tmp_path: Path):
    s = Session.create("BOM forge")
    csv_path = tmp_path / "forge.csv"
    csv_path.write_text(
        "ref,library_id,catalog_key,catalog_verified,width_mm,height_mm\n"
        "K1,iec.contactors.ac3_9a,x|y,full,2000,0.5\n",
        encoding="utf-8-sig",
    )
    result = s.import_bom(csv_path)
    assert "K1" in result["created"] or "K1" in result["updated"]
    k1 = s.sheet().component("K1")
    device = s.catalog.get(k1.library_id)
    assert k1.properties.get("catalog_key") not in {"x|y"}
    assert not is_layout_ready(device, k1)
    s.add_sheet("enclosure", kind="panel_layout")
    packed = s.layout_from_schematic()
    assert "K1" in {row["ref"] for row in packed["unplaced"]}
    assert all(c.id != "K1" for c in s.sheet("enclosure").components)


def test_set_properties_envelope_confirmed_alone_is_not_ready():
    s = _generic_session()
    s.set_component_properties("K1", {"envelope_confirmed": True})
    k1 = s.sheet().component("K1")
    assert k1.properties.get("envelope_confirmed") not in {True, "yes", "1"}
    assert not is_layout_ready(s.catalog.get(k1.library_id), k1)
    _assert_not_ready_unplaced(s, "K1")


def test_set_properties_envelope_confirmed_absurd_is_not_ready():
    s = _generic_session()
    s.set_component_properties(
        "K1",
        {
            "envelope_confirmed": "yes",
            "width_mm": 99999,
            "height_mm": 99999,
            "depth_mm": 99999,
            "mount": "din_rail",
        },
    )
    k1 = s.sheet().component("K1")
    device = s.catalog.get(k1.library_id)
    assert k1.properties.get("envelope_confirmed") not in {True, "yes", "1"}
    assert not is_layout_ready(device, k1)


def test_place_forged_envelope_refused_on_layout():
    s = Session.create("Place envelope")
    s.add_sheet("enclosure", kind="panel_layout")
    with pytest.raises(LayoutNotReadyError, match="not layout-ready"):
        s.place(
            "iec.contactors.ac3_9a",
            ref="K1",
            x=40,
            y=40,
            sheet_id="enclosure",
            properties={
                "envelope_confirmed": True,
                "width_mm": 45,
                "height_mm": 58,
                "depth_mm": 57,
                "mount": "din_rail",
            },
        )
    assert s.sheet("enclosure").components == []


def test_import_bom_forged_envelope_is_not_ready(tmp_path: Path):
    s = Session.create("BOM envelope")
    csv_path = tmp_path / "env.csv"
    csv_path.write_text(
        "ref,library_id,envelope_confirmed,width_mm,height_mm,depth_mm,mount\n"
        "K1,iec.contactors.ac3_9a,yes,45,58,57,din_rail\n",
        encoding="utf-8-sig",
    )
    s.import_bom(csv_path)
    k1 = s.sheet().component("K1")
    assert k1.properties.get("envelope_confirmed") not in {True, "yes", "1"}
    assert not is_layout_ready(s.catalog.get(k1.library_id), k1)


def test_loaded_confirmed_envelope_over_max_is_not_ready():
    s = _generic_session()
    k1 = s.sheet().component("K1")
    k1.properties["envelope_confirmed"] = True
    k1.properties["width_mm"] = 99999
    k1.properties["height_mm"] = 99999
    k1.properties["depth_mm"] = 99999
    k1.properties["mount"] = "din_rail"
    device = s.catalog.get(k1.library_id)
    assert not is_layout_ready(device, k1)
    assert "Confirmed envelope" in layout_ready_gap(device, k1)


def test_unconfirmed_set_panel_after_confirm_drops_ready():
    s = _generic_session()
    s.confirm_envelope(
        "K1", width_mm=45, height_mm=85, depth_mm=95, mount="din_rail", confirmed_by="george"
    )
    s.set_panel("K1", width_mm=2000, height_mm=2000, depth_mm=2000)
    k1 = s.sheet().component("K1")
    device = s.catalog.get(k1.library_id)
    assert k1.properties.get("envelope_confirmed") not in {True, "yes", "1"}
    assert not is_layout_ready(device, k1)


def test_unconfirmed_set_properties_after_confirm_drops_ready():
    s = _generic_session()
    s.confirm_envelope(
        "K1", width_mm=45, height_mm=85, depth_mm=95, mount="din_rail", confirmed_by="george"
    )
    s.set_component_properties("K1", {"width_mm": 99999})
    k1 = s.sheet().component("K1")
    assert k1.properties.get("envelope_confirmed") not in {True, "yes", "1"}
    assert not is_layout_ready(s.catalog.get(k1.library_id), k1)


def test_set_panel_one_by_one_after_confirm_drops_ready():
    s = _generic_session()
    s.confirm_envelope(
        "K1", width_mm=45, height_mm=85, depth_mm=95, mount="din_rail", confirmed_by="george"
    )
    s.set_panel("K1", width_mm=1, height_mm=1)
    k1 = s.sheet().component("K1")
    assert k1.properties.get("envelope_confirmed") not in {True, "yes", "1"}
    assert not is_layout_ready(s.catalog.get(k1.library_id), k1)


def test_assigned_part_size_override_drops_catalog_ready():
    s = _generic_session()
    s.assign_part("K1", "GH15BN-3-10A", manufacturer="AutomationDirect")
    k1 = s.sheet().component("K1")
    device = s.catalog.get(k1.library_id)
    assert is_layout_ready(device, k1)
    s.set_panel("K1", width_mm=99999, height_mm=0.001)
    assert not is_layout_ready(device, k1)
    assert "does not match published" in layout_ready_gap(device, k1)
    s.assign_part("K1", "GH15BN-3-10A", manufacturer="AutomationDirect")
    s.set_component_properties("K1", {"width_mm": 0.001, "height_mm": 0.001})
    assert not is_layout_ready(device, k1)


def test_ga500_fuse_holder_is_layout_ready():
    s = Session.create("GA500 fuse")
    s.insert_circuit(
        kind="vfd",
        drive="generic_1k5",
        manufacturer="Yaskawa",
        fla=4.8,
        voltage=480,
        control="local",
    )
    fuse = next(
        comp
        for sheet in s.project.sheets
        for comp in sheet.components
        if str(comp.properties.get("catalog") or "") == "FWH-70B"
    )
    device = s.catalog.get(fuse.library_id)
    assert is_layout_ready(device, fuse)
    assert layout_ready_gap(device, fuse) == ""
    assert fuse.properties.get("catalog_key")
    assert fuse.properties["width_mm"] == pytest.approx(161.94)
    assert fuse.properties["height_mm"] == pytest.approx(164.08)
    assert "no published width/height" not in layout_ready_gap(device, fuse)


def test_request_oem_update_prompt_requires_all_dims():
    s = _generic_session()
    captured: dict[str, str] = {}
    orig = s.submit_prompt

    def _capture(text: str):
        captured["text"] = text
        return orig(text)

    s.submit_prompt = _capture  # type: ignore[method-assign]
    s.request_oem_update(["K1"])
    text = captured["text"]
    assert "width_mm?" not in text
    assert "update_oem (ref, width_mm, height_mm, depth_mm" in text
    assert "unconfirmed measured" in text
    assert "confirm_envelope" in text or "verified SKU" in text


def _generic_fp(session: Session, ref: str):
    comp = session.sheet().component(ref)
    device = session.catalog.get(comp.library_id)
    return device, comp, effective_footprint(device, comp)


def test_set_properties_bare_catalog_is_not_ready():
    s = _generic_session()
    s.set_component_properties("K1", {"catalog": "MC-9b"})
    device, k1, fp = _generic_fp(s, "K1")
    assert k1.properties.get("catalog") == "MC-9b"
    assert not k1.properties.get("catalog_key")
    assert not is_layout_ready(device, k1)
    assert (fp.width_mm, fp.height_mm, fp.depth_mm) == (45.0, 58.0, 57.0)
    _assert_not_ready_unplaced(s, "K1")


def test_set_properties_part_number_and_manufacturer_is_not_ready():
    s = _generic_session()
    s.set_component_properties(
        "K1", {"part_number": "MC-9b", "manufacturer": "LS Electric"}
    )
    device, k1, fp = _generic_fp(s, "K1")
    assert not is_layout_ready(device, k1)
    assert (fp.width_mm, fp.height_mm) == (45.0, 58.0)
    s.set_component_properties(
        "K1", {"catalog": "GH15BN-3-10A", "manufacturer": "AutomationDirect"}
    )
    assert not is_layout_ready(device, s.sheet().component("K1"))


def test_agent_set_properties_bare_catalog_is_not_ready():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "BareSKU"})["ok"]
    assert agent.handle(
        {"id": 2, "op": "place", "library_id": "iec.contactors.ac3_9a", "ref": "K1", "x": 20, "y": 20}
    )["ok"]
    reply = agent.handle(
        {"id": 3, "op": "set_properties", "ref": "K1", "properties": {"catalog": "MC-9b"}}
    )
    assert reply["ok"] is True
    k1 = agent.session.sheet().component("K1")
    assert not k1.properties.get("catalog_key")
    _assert_not_ready_unplaced(agent.session, "K1")


def test_place_bare_catalog_refused_on_layout():
    s = Session.create("Place SKU")
    s.add_sheet("enclosure", kind="panel_layout")
    with pytest.raises(LayoutNotReadyError, match="not layout-ready"):
        s.place(
            "iec.contactors.ac3_9a",
            ref="K1",
            x=40,
            y=40,
            sheet_id="enclosure",
            properties={"catalog": "MC-9b"},
        )
    assert s.sheet("enclosure").components == []


def test_agent_place_bare_catalog_refused():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "PlaceSKU"})["ok"]
    assert agent.handle({"id": 2, "op": "add_sheet", "sheet_id": "enclosure", "kind": "panel_layout"})["ok"]
    reply = agent.handle(
        {
            "id": 3,
            "op": "place",
            "library_id": "iec.contactors.ac3_9a",
            "ref": "K1",
            "x": 40,
            "y": 40,
            "sheet": "enclosure",
            "properties": {"catalog": "MC-9b"},
        }
    )
    assert reply["ok"] is False
    assert "not layout-ready" in str(reply.get("error") or "")


def test_set_properties_wrong_category_sku_is_not_ready():
    s = _generic_session()
    s.set_component_properties("K1", {"catalog": "BH-0122"})
    device, k1, fp = _generic_fp(s, "K1")
    assert not is_layout_ready(device, k1)
    assert (fp.width_mm, fp.height_mm) == (45.0, 58.0)
    with pytest.raises(ValueError, match="does not fit"):
        s.assign_part("K1", "BH-0122", manufacturer="Eaton")
    k1 = s.sheet().component("K1")
    assert k1.properties.get("catalog") == "BH-0122"
    assert not is_layout_ready(device, k1)
    assert s.try_assign_sku("K1", "BH-0122", "Eaton") is None


def test_import_bom_catalog_assigns_published_footprint(tmp_path: Path):
    s = Session.create("BOM SKU")
    s.place("iec.contactors.ac3_9a", ref="K1", x=20, y=20)
    csv_path = tmp_path / "sku.csv"
    csv_path.write_text(
        "ref,library_id,catalog\nK1,iec.contactors.ac3_9a,MC-9b\n",
        encoding="utf-8-sig",
    )
    s.import_bom(csv_path)
    device, k1, fp = _generic_fp(s, "K1")
    assert is_layout_ready(device, k1)
    assert k1.properties.get("catalog_key")
    assert (fp.width_mm, fp.height_mm, fp.depth_mm) == (45.0, 73.5, 87.4)
    s.add_sheet("enclosure", kind="panel_layout")
    s.set_enclosure(width_mm=600, height_mm=800, depth_mm=250, sheet_id="enclosure")
    packed = s.layout_from_schematic()
    assert "K1" not in {row["ref"] for row in packed["unplaced"]}
    assert any(c.id == "K1" for c in s.sheet("enclosure").components)


def test_import_bom_wrong_category_sku_is_not_ready(tmp_path: Path):
    s = Session.create("BOM wrong")
    s.place("iec.contactors.ac3_9a", ref="K1", x=20, y=20)
    csv_path = tmp_path / "wrong.csv"
    csv_path.write_text(
        "ref,library_id,catalog\nK1,iec.contactors.ac3_9a,BH-0122\n",
        encoding="utf-8-sig",
    )
    s.import_bom(csv_path)
    device, k1, fp = _generic_fp(s, "K1")
    assert not is_layout_ready(device, k1)
    assert (fp.width_mm, fp.height_mm) == (45.0, 58.0)


def test_bom_dock_typed_sku_assigns_published_footprint():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.bom_dock import COLUMNS, BomDock

    app = QApplication.instance() or QApplication([])
    s = _generic_session()
    dock = BomDock()
    try:
        dock.set_session(s)
        cat_col = COLUMNS.index("catalog")
        ref_col = COLUMNS.index("ref")
        for row in range(dock.table.rowCount()):
            if dock.table.item(row, ref_col).text() != "K1":
                continue
            dock.table.item(row, cat_col).setText("MC-9b")
            break
        device, k1, fp = _generic_fp(s, "K1")
        assert is_layout_ready(device, k1)
        assert (fp.width_mm, fp.height_mm, fp.depth_mm) == (45.0, 73.5, 87.4)
    finally:
        _dispose_qt(dock)
        _ = app


def test_bom_dock_wrong_category_sku_stays_not_ready():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.bom_dock import COLUMNS, BomDock

    app = QApplication.instance() or QApplication([])
    s = _generic_session()
    dock = BomDock()
    try:
        dock.set_session(s)
        cat_col = COLUMNS.index("catalog")
        ref_col = COLUMNS.index("ref")
        for row in range(dock.table.rowCount()):
            if dock.table.item(row, ref_col).text() != "K1":
                continue
            dock.table.item(row, cat_col).setText("BH-0122")
            break
        device, k1, fp = _generic_fp(s, "K1")
        assert k1.properties.get("catalog") == "BH-0122"
        assert not is_layout_ready(device, k1)
        assert (fp.width_mm, fp.height_mm) == (45.0, 58.0)
    finally:
        _dispose_qt(dock)
        _ = app


def test_assigned_sku_nan_sizes_are_not_ready():
    s = _generic_session()
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    device = s.catalog.get("iec.contactors.ac3_9a")
    with pytest.raises(LayoutNotReadyError, match="finite"):
        s.set_component_properties("K1", {"width_mm": float("nan")})
    with pytest.raises(LayoutNotReadyError, match="finite"):
        s.set_component_properties("K1", {"width_mm": "nan"})
    with pytest.raises(LayoutNotReadyError, match="finite"):
        s.set_panel("K1", width_mm=float("nan"), height_mm=float("nan"))
    k1 = s.sheet().component("K1")
    k1.properties["width_mm"] = float("nan")
    k1.properties["height_mm"] = float("nan")
    assert not is_layout_ready(device, k1)
    s.add_sheet("enclosure", kind="panel_layout")
    packed = s.layout_from_schematic()
    assert "K1" in {row["ref"] for row in packed["unplaced"]}


def test_agent_set_properties_nan_on_assigned_sku():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "NaN"})["ok"]
    assert agent.handle(
        {"id": 2, "op": "place", "library_id": "iec.contactors.ac3_9a", "ref": "K1", "x": 20, "y": 20}
    )["ok"]
    assert agent.handle(
        {"id": 3, "op": "assign_part", "ref": "K1", "catalog": "MC-9b", "manufacturer": "LS Electric"}
    )["ok"]
    reply = agent.handle(
        {"id": 4, "op": "set_properties", "ref": "K1", "properties": {"width_mm": "NaN"}}
    )
    assert reply["ok"] is False
    assert "finite" in str(reply.get("error") or "")


def test_import_bom_size_change_clears_confirmation(tmp_path: Path):
    s = _generic_session()
    s.confirm_envelope(
        "K1", width_mm=45, height_mm=85, depth_mm=95, mount="din_rail", confirmed_by="george"
    )
    csv_path = tmp_path / "resize.csv"
    csv_path.write_text(
        "ref,library_id,width_mm,height_mm,depth_mm,mount\n"
        "K1,iec.contactors.ac3_9a,300,85,95,din_rail\n",
        encoding="utf-8-sig",
    )
    result = s.import_bom(csv_path)
    k1 = s.sheet().component("K1")
    device = s.catalog.get(k1.library_id)
    assert k1.properties.get("envelope_confirmed") not in {True, "yes", "1"}
    assert k1.properties.get("width_mm") == 300
    assert not is_layout_ready(device, k1)
    notes = result.get("warnings") or []
    assert any("envelope confirmation not imported" in note for note in notes)
    assert notes.count(next(note for note in notes if "envelope confirmation not imported" in note)) == 1


def test_assigned_sku_depth_or_mount_override_drops_ready():
    s = _generic_session()
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    device = s.catalog.get("iec.contactors.ac3_9a")
    k1 = s.sheet().component("K1")
    assert is_layout_ready(device, k1)
    s.set_component_properties("K1", {"depth_mm": 99999})
    assert not is_layout_ready(device, k1)
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    s.set_component_properties("K1", {"mount": "door"})
    assert not is_layout_ready(device, s.sheet().component("K1"))


def test_unconfirmed_measured_gap_points_to_confirm():
    s = _generic_session()
    s.set_panel("K1", width_mm=45, height_mm=85, depth_mm=95, mount="din_rail")
    device, k1, _fp = _generic_fp(s, "K1")
    gap = layout_ready_gap(device, k1)
    assert "not confirmed" in gap
    assert "confirm_envelope" in gap
    assert "generic/placeholder" not in gap


def test_import_bom_drops_forged_dim_source_catalog(tmp_path: Path):
    s = Session.create("BOM dim")
    s.place("iec.contactors.ac3_9a", ref="K1", x=20, y=20)
    csv_path = tmp_path / "dims.csv"
    csv_path.write_text(
        "ref,library_id,width_mm,height_mm,depth_mm,dim_source\n"
        "K1,iec.contactors.ac3_9a,45,85,95,catalog\n",
        encoding="utf-8-sig",
    )
    result = s.import_bom(csv_path)
    k1 = s.sheet().component("K1")
    device = s.catalog.get(k1.library_id)
    assert k1.properties.get("dim_source") != "catalog"
    assert not is_layout_ready(device, k1)
    assert "not confirmed" in layout_ready_gap(device, k1)
    assert not any("envelope confirmation not imported" in note for note in result.get("warnings") or [])


def test_duplicate_assigned_sku_stays_ready():
    s = _generic_session()
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    device = s.catalog.get("iec.contactors.ac3_9a")
    schematic = s.duplicate_component("K1")
    assert schematic.properties.get("catalog_key")
    assert schematic.properties.get("catalog") == "MC-9b"
    assert is_layout_ready(device, schematic)
    s.add_sheet("enclosure", kind="panel_layout")
    s.place_from_bom("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="enclosure")
    panel_copy = s.duplicate_component("K1", sheet_id="enclosure")
    assert panel_copy.properties.get("catalog_key")
    assert is_layout_ready(device, panel_copy)
    s.assign_part("K1", "GH15BN-3-10A", manufacturer="AutomationDirect")
    again = s.duplicate_component("K1")
    assert again.properties.get("catalog") == "GH15BN-3-10A"
    assert is_layout_ready(device, again)


def test_gui_duplicate_assigned_sku_stays_ready():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = _generic_session()
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    s.add_sheet("enclosure", kind="panel_layout")
    s.place_from_bom("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="enclosure")
    device = s.catalog.get("iec.contactors.ac3_9a")
    win = MainWindow(s)
    try:
        schematic_id = next(sheet.id for sheet in s.project.sheets if sheet.kind != "panel_layout")
        s.set_active_sheet(schematic_id)
        win.canvas.reload()
        win.canvas.select_ref("K1")
        win.duplicate_selected()
        copies = [comp for comp in s.sheet(schematic_id).components if comp.id != "K1" and comp.library_id == "iec.contactors.ac3_9a"]
        assert copies
        assert is_layout_ready(device, copies[0])
        assert copies[0].properties.get("catalog_key")
        s.set_active_sheet("enclosure")
        win.canvas.reload()
        win.canvas.select_ref("K1")
        win.duplicate_selected()
        panel_copies = [comp for comp in s.sheet("enclosure").components if comp.id != "K1"]
        assert panel_copies
        assert is_layout_ready(device, panel_copies[0])
    finally:
        _dispose_qt(win)
        _ = app


def test_assign_part_clears_confirmation_unknown_dock_sku_not_ready():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.bom_dock import COLUMNS, BomDock

    app = QApplication.instance() or QApplication([])
    s = _generic_session()
    s.confirm_envelope(
        "K1", width_mm=45, height_mm=85, depth_mm=95, mount="din_rail", confirmed_by="george"
    )
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    device, k1, fp = _generic_fp(s, "K1")
    assert k1.properties.get("envelope_confirmed") not in {True, "yes", "1"}
    assert is_layout_ready(device, k1)
    assert (fp.width_mm, fp.height_mm, fp.depth_mm) == (45.0, 73.5, 87.4)
    dock = BomDock()
    try:
        dock.set_session(s)
        cat_col = COLUMNS.index("catalog")
        ref_col = COLUMNS.index("ref")
        for row in range(dock.table.rowCount()):
            if dock.table.item(row, ref_col).text() != "K1":
                continue
            dock.table.item(row, cat_col).setText("ZZZ-1")
            break
        device, k1, fp = _generic_fp(s, "K1")
        assert k1.properties.get("catalog") == "ZZZ-1"
        assert not k1.properties.get("catalog_key")
        assert k1.properties.get("envelope_confirmed") not in {True, "yes", "1"}
        assert not is_layout_ready(device, k1)
    finally:
        _dispose_qt(dock)
        _ = app


def test_import_bom_warnings_match_what_happened(tmp_path: Path):
    s = _generic_session()
    s.confirm_envelope(
        "K1", width_mm=45, height_mm=85, depth_mm=95, mount="din_rail", confirmed_by="george"
    )
    same = tmp_path / "same.csv"
    same.write_text(
        "ref,library_id,width_mm,height_mm,depth_mm,mount\n"
        "K1,iec.contactors.ac3_9a,45,85,95,din_rail\n",
        encoding="utf-8-sig",
    )
    kept = s.import_bom(same)
    k1 = s.sheet().component("K1")
    device = s.catalog.get(k1.library_id)
    assert k1.properties.get("envelope_confirmed") in {True, "yes", "1"}
    assert is_layout_ready(device, k1)
    assert not any("envelope confirmation not imported" in note for note in kept.get("warnings") or [])

    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    resize = tmp_path / "resize.csv"
    resize.write_text(
        "ref,library_id,catalog,width_mm\nK1,iec.contactors.ac3_9a,MC-9b,300\n",
        encoding="utf-8-sig",
    )
    changed = s.import_bom(resize)
    notes = changed.get("warnings") or []
    assert any("size changed on a catalog SKU" in note for note in notes)
    assert not any("envelope confirmation not imported" in note for note in notes)
    assert notes.count(next(note for note in notes if "size changed on a catalog SKU" in note)) == 1
    assert not is_layout_ready(device, s.sheet().component("K1"))

    s.confirm_envelope(
        "K1", width_mm=45, height_mm=85, depth_mm=95, mount="din_rail", confirmed_by="george"
    )
    mount = tmp_path / "mount.csv"
    mount.write_text(
        "ref,library_id,mount\nK1,iec.contactors.ac3_9a,door\n",
        encoding="utf-8-sig",
    )
    cleared = s.import_bom(mount)
    assert s.sheet().component("K1").properties.get("envelope_confirmed") not in {True, "yes", "1"}
    assert any("envelope confirmation not imported" in note for note in cleared.get("warnings") or [])


def test_catalog_mismatch_gap_names_depth_or_mount():
    s = _generic_session()
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    device = s.catalog.get("iec.contactors.ac3_9a")
    s.set_component_properties("K1", {"depth_mm": 99999})
    gap = layout_ready_gap(device, s.sheet().component("K1"))
    assert "depth 99999" in gap
    assert "published 87.4" in gap
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    s.set_component_properties("K1", {"mount": "door"})
    gap = layout_ready_gap(device, s.sheet().component("K1"))
    assert "mount door" in gap
    assert "din_rail" in gap


def test_set_panel_wh_only_gap_is_incomplete():
    s = _generic_session()
    s.set_panel("K1", width_mm=45, height_mm=85)
    device, k1, _fp = _generic_fp(s, "K1")
    gap = layout_ready_gap(device, k1)
    assert "incomplete" in gap
    assert "not confirmed" in gap
    assert "generic/placeholder" not in gap


def test_set_properties_catalog_mismatch_drops_assignment_all_sheets():
    s = _generic_session()
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    s.add_sheet("enclosure", kind="panel_layout")
    s.place_from_bom("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="enclosure")
    s.set_component_properties("K1", {"catalog": "BH-0122"})
    device = s.catalog.get("iec.contactors.ac3_9a")
    for sheet in s.project.sheets:
        try:
            k1 = sheet.component("K1")
        except KeyError:
            continue
        assert k1.properties.get("catalog") == "BH-0122"
        assert not k1.properties.get("catalog_key")
        assert not is_layout_ready(device, k1)


def test_import_bom_catalog_mismatch_drops_assignment(tmp_path: Path):
    s = _generic_session()
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    csv_path = tmp_path / "swap.csv"
    csv_path.write_text(
        "ref,library_id,catalog\nK1,iec.contactors.ac3_9a,BH-0122\n",
        encoding="utf-8-sig",
    )
    s.import_bom(csv_path)
    device, k1, _fp = _generic_fp(s, "K1")
    assert k1.properties.get("catalog") == "BH-0122"
    assert not k1.properties.get("catalog_key")
    assert not is_layout_ready(device, k1)


def test_bom_dock_manufacturer_mismatch_drops_assignment():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.bom_dock import COLUMNS, BomDock

    app = QApplication.instance() or QApplication([])
    s = _generic_session()
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    dock = BomDock()
    try:
        dock.set_session(s)
        mfr_col = COLUMNS.index("manufacturer")
        ref_col = COLUMNS.index("ref")
        for row in range(dock.table.rowCount()):
            if dock.table.item(row, ref_col).text() != "K1":
                continue
            dock.table.item(row, mfr_col).setText("AutomationDirect")
            break
        device, k1, _fp = _generic_fp(s, "K1")
        assert k1.properties.get("manufacturer") == "AutomationDirect"
        assert not k1.properties.get("catalog_key")
        assert not is_layout_ready(device, k1)
    finally:
        _dispose_qt(dock)
        _ = app


def test_assign_footprint_less_sku_keeps_confirmed_envelope():
    s = Session.create("PB confirm")
    s.place("iec.operators.pb_no", ref="S1", x=20, y=20)
    s.confirm_envelope(
        "S1", width_mm=22, height_mm=22, depth_mm=50, mount="door", confirmed_by="george"
    )
    device = s.catalog.get("iec.operators.pb_no")
    assert is_layout_ready(device, s.sheet().component("S1"))
    s.assign_part("S1", "800FP-F3", manufacturer="Allen-Bradley")
    s1 = s.sheet().component("S1")
    fp = effective_footprint(device, s1)
    assert s1.properties.get("catalog") == "800FP-F3"
    assert s1.properties.get("envelope_confirmed") in {True, "yes", "1"}
    assert is_layout_ready(device, s1)
    assert (fp.width_mm, fp.height_mm, fp.depth_mm, fp.mount) == (22.0, 22.0, 50.0, "door")

    s = _generic_session()
    s.confirm_envelope(
        "K1", width_mm=30, height_mm=40, depth_mm=50, mount="din_rail", confirmed_by="george"
    )
    s.assign_part("K1", "SC-E02P-110VAC", manufacturer="AutomationDirect")
    device, k1, fp = _generic_fp(s, "K1")
    assert k1.properties.get("catalog") == "SC-E02P-110VAC"
    assert k1.properties.get("envelope_confirmed") in {True, "yes", "1"}
    assert is_layout_ready(device, k1)
    assert (fp.width_mm, fp.height_mm, fp.depth_mm, fp.mount) == (30.0, 40.0, 50.0, "din_rail")


def test_assign_footprint_less_sku_clears_confirmation_when_mount_differs():
    s = Session.create("PB mount")
    s.place("iec.operators.pb_no", ref="S1", x=20, y=20)
    s.confirm_envelope(
        "S1", width_mm=22, height_mm=22, depth_mm=50, mount="din_rail", confirmed_by="george"
    )
    s.assign_part("S1", "800FP-F3", manufacturer="Allen-Bradley")
    device = s.catalog.get("iec.operators.pb_no")
    s1 = s.sheet().component("S1")
    assert s1.properties.get("catalog") == "800FP-F3"
    assert s1.properties.get("mount") == "door"
    assert s1.properties.get("envelope_confirmed") not in {True, "yes", "1"}
    assert not is_layout_ready(device, s1)


def test_set_properties_part_number_mismatch_drops_assignment_all_sheets():
    s = _generic_session()
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    s.add_sheet("enclosure", kind="panel_layout")
    s.place_from_bom("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="enclosure")
    s.set_component_properties("K1", {"part_number": "BH-0122"})
    device = s.catalog.get("iec.contactors.ac3_9a")
    for sheet in s.project.sheets:
        try:
            k1 = sheet.component("K1")
        except KeyError:
            continue
        assert k1.properties.get("part_number") == "BH-0122"
        assert k1.properties.get("catalog") == "MC-9b"
        assert not k1.properties.get("catalog_key")
        assert not is_layout_ready(device, k1)


def test_import_bom_part_number_mismatch_drops_assignment(tmp_path: Path):
    s = _generic_session()
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    csv_path = tmp_path / "swap.csv"
    csv_path.write_text(
        "ref,library_id,part_number\nK1,iec.contactors.ac3_9a,BH-0122\n",
        encoding="utf-8-sig",
    )
    s.import_bom(csv_path)
    device, k1, _fp = _generic_fp(s, "K1")
    assert k1.properties.get("part_number") == "BH-0122"
    assert not k1.properties.get("catalog_key")
    assert not is_layout_ready(device, k1)


def test_bom_dock_part_number_mismatch_drops_assignment():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.bom_dock import COLUMNS, BomDock

    app = QApplication.instance() or QApplication([])
    s = _generic_session()
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    dock = BomDock()
    try:
        dock.set_session(s)
        pn_col = COLUMNS.index("part_number")
        ref_col = COLUMNS.index("ref")
        for row in range(dock.table.rowCount()):
            if dock.table.item(row, ref_col).text() != "K1":
                continue
            dock.table.item(row, pn_col).setText("BH-0122")
            break
        device, k1, _fp = _generic_fp(s, "K1")
        assert k1.properties.get("part_number") == "BH-0122"
        assert not k1.properties.get("catalog_key")
        assert not is_layout_ready(device, k1)
    finally:
        _dispose_qt(dock)
        _ = app


def test_import_bom_catalog_ready_skips_confirmation_warning(tmp_path: Path):
    s = _generic_session()
    s.confirm_envelope(
        "K1", width_mm=45, height_mm=85, depth_mm=95, mount="din_rail", confirmed_by="george"
    )
    csv_path = tmp_path / "sku.csv"
    csv_path.write_text(
        "ref,library_id,catalog\nK1,iec.contactors.ac3_9a,MC-9b\n",
        encoding="utf-8-sig",
    )
    result = s.import_bom(csv_path)
    device, k1, fp = _generic_fp(s, "K1")
    assert is_layout_ready(device, k1)
    assert k1.properties.get("catalog_key")
    assert (fp.width_mm, fp.height_mm, fp.depth_mm) == (45.0, 73.5, 87.4)
    notes = result.get("warnings") or []
    assert not any("envelope confirmation not imported" in note for note in notes)


def test_import_bom_updated_lists_ref_once_on_two_sheets(tmp_path: Path):
    s = _generic_session()
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    s.add_sheet("enclosure", kind="panel_layout")
    s.place_from_bom("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="enclosure")
    csv_path = tmp_path / "two.csv"
    csv_path.write_text(
        "ref,library_id,sheet,catalog\n"
        "K1,iec.contactors.ac3_9a,schematic,MC-9b\n"
        "K1,iec.contactors.ac3_9a,enclosure,MC-9b\n",
        encoding="utf-8-sig",
    )
    result = s.import_bom(csv_path)
    assert result.get("updated") == ["K1"]


def test_agent_assign_part_unfit_is_not_a_crash():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Unfit"})["ok"]
    assert agent.handle(
        {
            "id": 2,
            "op": "place",
            "library_id": "iec.contactors.ac3_9a",
            "ref": "K1",
            "x": 20,
            "y": 20,
        }
    )["ok"]
    reply = agent.handle(
        {
            "id": 3,
            "op": "assign_part",
            "ref": "K1",
            "catalog": "BH-0122",
            "manufacturer": "Eaton",
        }
    )
    assert reply["ok"] is False
    assert "does not fit" in str(reply.get("error") or "")
    assert "crash" not in reply


def test_cli_library_assign_unfit_prints_one_line_and_writes_no_crash(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.cli.main import app
    from panelcad.diag.paths import crashes_dir

    s = _generic_session()
    dest = tmp_path / "job.pcad.yaml"
    s.save(dest)
    before = {p.name for p in crashes_dir().glob("*.json")}
    result = CliRunner().invoke(
        app,
        ["library", "assign", str(dest), "K1", "BH-0122", "-m", "Eaton"],
    )
    assert result.exit_code == 1
    assert "does not fit" in (result.output or "")
    assert "Traceback" not in (result.output or "")
    after = {p.name for p in crashes_dir().glob("*.json")}
    assert after == before


def test_confirmed_flag_without_instance_sizes_is_not_ready():
    s = _generic_session()
    k1 = s.sheet().component("K1")
    k1.properties.update({"envelope_confirmed": True, "envelope_confirmed_by": "george", "mount": "din_rail"})
    device = s.catalog.get(k1.library_id)
    assert not is_layout_ready(device, k1)
    assert "Confirmed envelope" in layout_ready_gap(device, k1)


def test_manufacturer_alias_keeps_assignment():
    s = Session.create("Alias")
    s.place("iec.contactors.ac3_9a", ref="K1", x=20, y=20)
    s.assign_part("K1", "100-C09D10", manufacturer="Allen-Bradley")
    device = s.catalog.get("iec.contactors.ac3_9a")
    for alias in ("Allen Bradley", "Rockwell Automation"):
        s.set_component_properties("K1", {"manufacturer": alias})
        k1 = s.sheet().component("K1")
        assert k1.properties.get("catalog_key") == "Allen-Bradley|100-c09d10"
        assert is_layout_ready(device, k1)
    s.set_component_properties("K1", {"manufacturer": "Eaton"})
    assert not s.sheet().component("K1").properties.get("catalog_key")


def test_unchanged_size_edit_keeps_confirmation():
    s = _generic_session()
    s.confirm_envelope(
        "K1", width_mm=45, height_mm=85, depth_mm=95, mount="din_rail", confirmed_by="george"
    )
    s.set_component_properties("K1", {"width_mm": 45.0, "mount": "din_rail"})
    s.set_panel("K1", height_mm=85, mount="din_rail")
    device, k1, _fp = _generic_fp(s, "K1")
    assert k1.properties.get("envelope_confirmed") in {True, "yes", "1"}
    assert is_layout_ready(device, k1)
    s.set_panel("K1", height_mm=86)
    device, k1, _fp = _generic_fp(s, "K1")
    assert not is_layout_ready(device, k1)


def test_footprint_less_assign_drops_previous_catalog_size():
    s = _generic_session()
    s.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    s.assign_part("K1", "SC-E02P-110VAC", manufacturer="AutomationDirect")
    device, k1, _fp = _generic_fp(s, "K1")
    assert k1.properties.get("dim_source") != "catalog"
    assert k1.properties.get("width_mm") in (None, "")
    assert k1.properties.get("depth_mm") in (None, "")
    assert not is_layout_ready(device, k1)


def test_library_catalog_part_with_wrong_depth_is_not_ready():
    s = Session.create("Lib depth")
    c = s.place("io.siemens.sm1221_di16", ref="A1", x=40, y=40)
    device = s.catalog.get(c.library_id)
    assert is_layout_ready(device, c)
    s.set_component_properties("A1", {"depth_mm": 99999})
    assert not is_layout_ready(device, s.sheet().component("A1"))
    s.set_component_properties("A1", {"depth_mm": device.panel.depth_mm})
    assert is_layout_ready(device, s.sheet().component("A1"))


from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.checks import run_checks
from panelcad.io.bom import instance_rows
from panelcad.library.panel import is_layout_ready
from panelcad.reconstruct.fixtures import write_backpanel_photo
from panelcad.reconstruct.vision import FakeVision
from PySide6.QtWidgets import QApplication


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_n11_generic_depthless_and_empty_envelope_are_refused():
    session = Session.create("ReadyGate")
    session.add_sheet("panel", kind="panel_layout")

    try:
        session.place(
            "iec.contactors.ac3_9a",
            ref="KG",
            x=80,
            y=80,
            sheet_id="panel",
            properties={"dim_source": "catalog"},
        )
    except ValueError as exc:
        assert "layout-ready" in str(exc).lower() or "generic" in str(exc).lower()
    else:
        raise AssertionError("caller dim_source=catalog must not make a generic layout-ready")
    assert not session.project.sheet("panel").components

    try:
        session.place(
            "iec.contactors.ac3_9a",
            ref="KH",
            x=80,
            y=80,
            sheet_id="panel",
            properties={
                "dim_source": "measured",
                "envelope_confirmed": True,
                "width_mm": 50,
                "height_mm": 80,
                "mount": "din_rail",
            },
        )
    except ValueError:
        pass
    else:
        raise AssertionError("depthless measured envelope must be refused")
    assert not session.project.sheet("panel").components

    try:
        session.place("reconstruct.measured_envelope", ref="U0", x=40, y=40, sheet_id="panel")
    except ValueError:
        pass
    else:
        raise AssertionError("empty measured_envelope must be refused")
    assert not session.project.sheet("panel").components
    assert is_layout_ready(session.catalog.get("reconstruct.measured_envelope")) is False

    _app()
    from pathlib import Path
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as raw:
        tmp = Path(raw)
        photo = tmp / "back.png"
        meta = write_backpanel_photo(photo)
        job = tmp / "env.pcad.yaml"
        recon = Session.create("EnvBom")
        recon.save(job)
        recon = Session.open(job)
        recon.reconstruct_add_photos([photo], role="backpanel")
        recon.reconstruct_identify(
            vision=FakeVision(
                {
                    "p1": {
                        "devices": [
                            {
                                "tag": "U7",
                                "label_text": "??",
                                "nx": 0.2,
                                "ny": 0.2,
                                "nw": 0.1,
                                "nh": 0.1,
                                "confidence": 0.4,
                            }
                        ]
                    }
                }
            )
        )
        item = next(row for row in recon.project.reconstruct.items if row.role == "part")
        recon.reconstruct_confirm_item(
            item.id,
            envelope_w_mm=50,
            envelope_h_mm=80,
            envelope_d_mm=90,
            mount="backplate",
            tag="U7",
        )
        photo_id = recon.reconstruct_list_photos()["photos"][0]["id"]
        recon.reconstruct_set_corners(photo_id, meta["corners"])
        recon.reconstruct_set_scale(
            photo_id,
            "face",
            meta["corners"][0],
            meta["corners"][1],
            known_mm=meta.get("enclosure_width_mm") or 400,
            known_height_mm=meta.get("enclosure_height_mm") or 500,
        )
        recon.reconstruct_place_confirmed()
        rows = instance_rows(recon.project, recon.catalog)
        hit = next(row for row in rows if row["ref"] == "U7")
        assert hit["category"] != "connection.notes"
        assert "envelope" in str(hit["category"]).lower() or "mechanical" in str(hit["category"]).lower()


def test_defect4_place_from_bom_panel_skips_generic():
    session = Session.create("BomPanel")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    session.add_sheet("panel", kind="panel_layout")
    with pytest.raises(ValueError, match="not layout-ready"):
        session.place_from_bom("iec.contactors.ac3_9a", ref="K1", x=100, y=80, sheet_id="panel")
    assert not session.project.sheet("panel").components
    problems = run_checks(session.project, session.catalog)
    assert any(row.check == "layout_not_ready" and row.ref == "K1" for row in problems)
    agent = Agent()
    agent.session = session
    reply = agent.handle(
        {"op": "place_from_bom", "library_id": "iec.contactors.ac3_9a", "ref": "K1", "sheet": "panel"}
    )
    assert not session.project.sheet("panel").components
    assert reply.get("ok") is False or (reply.get("result") or {}).get("placed") is False


def test_defect4_place_from_bom_door_skips_generic():
    session = Session.create("BomDoor")
    session.place("iec.operators.pb_no", ref="PB1", x=40, y=40)
    session.add_sheet("door", kind="door_layout")
    with pytest.raises(ValueError, match="not layout-ready"):
        session.place_from_bom("iec.operators.pb_no", ref="PB1", x=80, y=80, sheet_id="door")
    assert not session.project.sheet("door").components
    assert any(row.check == "layout_not_ready" for row in run_checks(session.project, session.catalog))


def test_defect4_layout_from_schematic_skips_generics_and_does_not_size_from_them():
    session = Session.create("FromSch")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    session.place("iec.contactors.ac3_9a", ref="K2", x=80, y=40)
    session.add_sheet("panel", kind="panel_layout")
    before = session.project.sheet("panel").enclosure
    session.layout_from_schematic("panel")
    dest = session.project.sheet("panel")
    assert not [comp for comp in dest.components if comp.library_id == "iec.contactors.ac3_9a"]
    if dest.enclosure is not None and before is not None:
        assert dest.enclosure.width_mm == before.width_mm or dest.enclosure.width_mm != 600 or not dest.components
    problems = run_checks(session.project, session.catalog)
    assert any(row.check == "layout_not_ready" and row.ref in {"K1", "K2"} for row in problems)


def test_defect4_insert_circuit_dol_skips_generic_layout():
    session = Session.create("DolGate")
    session.insert_circuit(kind="dol")
    layouts = [sheet for sheet in session.project.sheets if sheet.kind in {"panel_layout", "door_layout"}]
    placed = [comp for sheet in layouts for comp in sheet.components]
    generic = [
        comp
        for comp in placed
        if str((comp.properties or {}).get("dim_source") or "") == "generic"
        or (
            comp.library_id.startswith("iec.")
            and not is_layout_ready(session.catalog.get(comp.library_id), comp)
        )
    ]
    assert generic == []
    problems = run_checks(session.project, session.catalog)
    assert any(row.check == "layout_not_ready" for row in problems)
    agent = Agent()
    agent.session = Session.create("DolAgent")
    reply = agent.handle({"op": "insert_circuit", "kind": "dol"})
    assert reply.get("ok") is True
    layouts = [sheet for sheet in agent.session.project.sheets if sheet.kind == "panel_layout"]
    for sheet in layouts:
        assert not [
            comp
            for comp in sheet.components
            if not is_layout_ready(agent.session.catalog.get(comp.library_id), comp)
            and str(comp.library_id) != "reconstruct.needs_info"
        ]


def test_place_strips_guessed_mm_on_needs_info(tmp_path: Path):
    """Guessed or photo millimetres must not stick to reconstruct.needs_info."""

    from panelcad.reconstruct.models import Photo, ensure_reconstruction

    guessed = {
        "width_mm": 45,
        "height_mm": 58,
        "depth_mm": 57,
        "mount": "din_rail",
        "dim_source": "photo",
    }
    session = Session.create("Needs info place")
    path = tmp_path / "needs-info.pcad.yaml"
    session.save(path)
    session = Session.open(path)
    store = ensure_reconstruction(session.project)
    store.photos.append(Photo(id="p1", path=str(tmp_path / "face.png"), role="backpanel"))
    session.add_sheet("panel", kind="panel_layout", make_active=False)

    def _assert_clean(live: Session, ref: str) -> None:
        comp = live.project.sheet("panel").component(ref)
        for key, value in guessed.items():
            assert comp.properties.get(key) in (None, "", False), (ref, key, comp.properties.get(key))
            assert comp.properties.get(key) != value
        fp = effective_footprint(live.catalog.get(comp.library_id), comp)
        assert fp.width_mm != 45
        assert fp.height_mm != 58
        assert fp.depth_mm != 57
        assert fp.mount != "din_rail"
        assert fp.dim_source != "photo"
        assert (fp.width_mm, fp.height_mm, fp.depth_mm) == (0.0, 0.0, 0.0)
        assert fp.mount == "none"
        assert fp.dim_source == "generic"

    session.place(
        "reconstruct.needs_info",
        ref="U1",
        x=12,
        y=18,
        sheet_id="panel",
        properties=dict(guessed),
        snap=False,
    )
    session.place(
        "reconstruct.needs_info",
        ref="U2",
        x=40,
        y=18,
        sheet_id="panel",
        properties=dict(guessed),
        snap=False,
        _sanitize=False,
    )
    _assert_clean(session, "U1")
    _assert_clean(session, "U2")
    session.save(path)
    text = path.read_text(encoding="utf-8")
    assert "dim_source: photo" not in text
    assert "width_mm: 45" not in text
    assert "height_mm: 58" not in text
    assert "depth_mm: 57" not in text
    reopened = Session.open(path)
    _assert_clean(reopened, "U1")
    _assert_clean(reopened, "U2")
