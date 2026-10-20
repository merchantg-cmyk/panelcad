"""The Pydantic AI drawing agent places real symbols and orthogonal wires offline."""

from __future__ import annotations

from pathlib import Path

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from panelcad.ai.drawing import drawing_agent, open_drawing
from panelcad.core.route import path_hits_obstacles
from panelcad.render.sheet import iter_wire_points, parse_pin_ref
from panelcad.render.wiring import component_obstacle


def _response(tool_name: str, args: dict, call_id: str) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args, tool_call_id=call_id)])


def test_agent_exposes_drawing_tools():
    seen: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(tool.name for tool in info.function_tools)
        return ModelResponse(parts=[TextPart(content="ready")])

    drawing = open_drawing(name="Tools")
    with drawing_agent.override(model=FunctionModel(model)):
        result = drawing_agent.run_sync("What can you draw?", deps=drawing)
    assert result.output == "ready"
    assert {
        "library_search",
        "parts_search",
        "assign_part",
        "confirm_envelope",
        "place",
        "wire",
        "set_drawing_style",
        "save",
        "print_package",
        "update_cross_references",
        "set_cross_reference",
        "report_bug",
        "set_phase_colors",
        "convert_standard",
        "convert_standard_plan",
        "convert_standard_next",
        "convert_standard_apply",
    } <= set(seen)


def test_drawing_agent_parts_tools_expose_amp_range_filters():
    tools: dict[str, object] = {}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for tool in info.function_tools:
            tools[tool.name] = tool
        return ModelResponse(parts=[TextPart(content="ready")])

    drawing = open_drawing(name="Part tools")
    with drawing_agent.override(model=FunctionModel(model)):
        drawing_agent.run_sync("List catalog tools.", deps=drawing)

    search = tools["parts_search"]
    assign = tools["assign_part"]
    assert search.description
    assert "amps_min" in search.description
    assert "amps_max" in search.description
    assert "verified" in search.description.lower()
    search_props = search.parameters_json_schema["properties"]
    assert "amps_min" in search_props
    assert "amps_max" in search_props
    assert "query" in search_props
    assert "manufacturer" in search_props
    assert assign.description
    assert "catalog" in assign.description.lower() or "SKU" in assign.description
    assign_props = assign.parameters_json_schema["properties"]
    assert "ref" in assign_props
    assert "catalog" in assign_props
    assert {"ref", "catalog"} <= set(assign.parameters_json_schema.get("required") or [])


def test_drawing_agent_parts_search_filters_range_and_assigns():
    steps = [
        _response(
            "place",
            {"library_id": "iec.breakers.mpcb_3p", "x": 40, "y": 22, "ref": "Q1"},
            "place-mpcb",
        ),
        _response(
            "parts_search",
            {"query": "3RV", "amps_min": 2.2, "amps_max": 3.2},
            "search-range",
        ),
        _response(
            "assign_part",
            {"ref": "Q1", "catalog": "3RV2011-1DA25", "manufacturer": "Siemens"},
            "assign-3rv",
        ),
        ModelResponse(parts=[TextPart(content="Assigned 3RV2011-1DA25 to Q1.")]),
    ]

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return steps.pop(0)

    drawing = open_drawing(name="Assign SKU")
    with drawing_agent.override(model=FunctionModel(model)):
        result = drawing_agent.run_sync(
            "Place a motor protector and assign 3RV2011-1DA25.",
            deps=drawing,
        )
    assert "3RV2011-1DA25" in result.output
    session = drawing.operator.session
    assert session is not None
    props = session.sheet().component("Q1").properties
    assert props["catalog"] == "3RV2011-1DA25"
    assert props["amps_min"] == 2.2
    assert props["amps_max"] == 3.2
    assert props["range"] == "2.2-3.2A"


def test_drawing_agent_exposes_recommend_and_apply_enclosure():
    tools: dict[str, str] = {}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for tool in info.function_tools:
            tools[tool.name] = tool.description or ""
        return ModelResponse(parts=[TextPart(content="ready")])

    drawing = open_drawing(name="Enclosure tools")
    drawing.operator.handle({"op": "place", "library_id": "iec.breakers.mcb_1p_16a", "ref": "Q1", "x": 20, "y": 20})
    before = [sh.enclosure for sh in drawing.operator.session.project.sheets if sh.kind == "panel_layout"]
    with drawing_agent.override(model=FunctionModel(model)):
        result = drawing_agent.run_sync("What enclosure tools do you have?", deps=drawing)
    assert result.output == "ready"
    assert "recommend_enclosure" in tools
    assert "apply_enclosure" in tools
    assert "does not write" in tools["recommend_enclosure"].lower()
    assert "undo" in tools["apply_enclosure"].lower()
    after = [sh.enclosure for sh in drawing.operator.session.project.sheets if sh.kind == "panel_layout"]
    assert after == before

    steps = [
        _response("recommend_enclosure", {}, "rec"),
        ModelResponse(parts=[TextPart(content="Recommended a box.")]),
    ]

    def caller(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return steps.pop(0)

    still = [sh.enclosure for sh in drawing.operator.session.project.sheets if sh.kind == "panel_layout"]
    with drawing_agent.override(model=FunctionModel(caller)):
        rec = drawing_agent.run_sync("Recommend an enclosure.", deps=drawing)
    assert "Recommended" in rec.output
    assert [sh.enclosure for sh in drawing.operator.session.project.sheets if sh.kind == "panel_layout"] == still

    apply_steps = [
        _response("apply_enclosure", {"place_parts": False}, "app"),
        ModelResponse(parts=[TextPart(content="Applied the enclosure.")]),
    ]

    def applier(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return apply_steps.pop(0)

    with drawing_agent.override(model=FunctionModel(applier)):
        applied = drawing_agent.run_sync("Apply the enclosure.", deps=drawing)
    assert "Applied" in applied.output
    panel = next(sh for sh in drawing.operator.session.project.sheets if sh.kind == "panel_layout")
    assert panel.enclosure is not None
    assert panel.enclosure.width_mm > 0

def test_drawing_agent_print_package_schema_has_path_paper_sections():
    tools: dict[str, object] = {}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools.update({tool.name: tool for tool in info.function_tools})
        return ModelResponse(parts=[TextPart(content="ready")])

    drawing = open_drawing(name="Print schema")
    with drawing_agent.override(model=FunctionModel(model)):
        drawing_agent.run_sync("List your print tools.", deps=drawing)

    tool = tools.get("print_package")
    assert tool is not None
    description = (getattr(tool, "description", None) or "").lower()
    assert "pdf" in description
    schema = getattr(tool, "parameters_json_schema", None) or {}
    props = schema.get("properties") or {}
    assert {"path", "paper", "sections"} <= set(props)
    path_desc = str(props["path"].get("description") or "").lower()
    paper_desc = str(props["paper"].get("description") or "").lower()
    sections_desc = str(props["sections"].get("description") or "").lower()
    assert "output" in path_desc or "pdf" in path_desc
    assert "mixed" in paper_desc and "letter" in paper_desc
    assert "bom" in sections_desc and "cover" in sections_desc
    required = set(schema.get("required") or [])
    assert "path" in required
    assert "paper" not in required
    assert "sections" not in required


def test_scripted_place_and_wire_stay_orthogonal(tmp_path: Path):
    steps = [
        _response(
            "place",
            {"library_id": "iec.contactors.coil", "x": 40, "y": 40, "ref": "CR1"},
            "place-coil",
        ),
        _response(
            "place",
            {"library_id": "iec.operators.pilot_light", "x": 160, "y": 40, "ref": "LT1"},
            "place-light",
        ),
        _response("wire", {"frm": "CR1.A2", "to": "LT1.X1", "net": "RUN"}, "wire-run"),
        ModelResponse(parts=[TextPart(content="Placed CR1 and LT1 and wired CR1.A2 to LT1.X1.")]),
    ]

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return steps.pop(0)

    dest = tmp_path / "coil.pcad.yaml"
    drawing = open_drawing(dest, name="Coil and light")
    with drawing_agent.override(model=FunctionModel(model)):
        result = drawing_agent.run_sync(
            "Place coil CR1 and pilot light LT1 and wire the coil to the light.",
            deps=drawing,
        )
    assert "CR1" in result.output and "LT1" in result.output
    session = drawing.operator.session
    assert session is not None
    sheet = session.sheet()
    libs = {c.id: c.library_id for c in sheet.components}
    assert libs["CR1"] == "iec.contactors.coil"
    assert libs["LT1"] == "iec.operators.pilot_light"
    wire = sheet.wires[0]
    pts = iter_wire_points(sheet, session.catalog, wire)
    for a, b in zip(pts, pts[1:]):
        assert abs(a.x - b.x) < 0.8 or abs(a.y - b.y) < 0.8
    ends = {parse_pin_ref(wire.frm)[0], parse_pin_ref(wire.to)[0]}
    blockers = [
        component_obstacle(sheet, session.catalog, c) for c in sheet.components if c.id not in ends
    ]
    assert not path_hits_obstacles(pts, blockers)
    session.save(dest)
    assert dest.is_file()


def test_generic_load_box_is_retried_as_a_coil():
    calls = {"n": 0}

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return _response(
                "place",
                {"library_id": "iec.power.load_24v", "x": 20, "y": 20, "ref": "U1"},
                "bad-load",
            )
        if calls["n"] == 2:
            assert "not a device" in str(messages[-1]).lower()
            return _response(
                "place",
                {"library_id": "iec.contactors.coil", "x": 20, "y": 20, "ref": "CR1"},
                "coil",
            )
        return ModelResponse(parts=[TextPart(content="Placed CR1, a coil.")])

    drawing = open_drawing(name="Retry")
    with drawing_agent.override(model=FunctionModel(model)):
        result = drawing_agent.run_sync("Place the relay.", deps=drawing)
    assert "CR1" in result.output
    session = drawing.operator.session
    assert session is not None
    assert [c.library_id for c in session.sheet().components] == ["iec.contactors.coil"]


def test_drawing_agent_xref_tools_write_and_set_pitch():
    schema: dict[str, object] = {}

    def capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for tool in info.function_tools:
            schema[tool.name] = tool
        return ModelResponse(parts=[TextPart(content="ready")])

    drawing = open_drawing(name="XrefTools")
    with drawing_agent.override(model=FunctionModel(capture)):
        drawing_agent.run_sync("List tools.", deps=drawing)
    write = schema["update_cross_references"]
    pitch = schema["set_cross_reference"]
    write_desc = (getattr(write, "description", None) or "").lower()
    pitch_desc = (getattr(pitch, "description", None) or "").lower()
    pitch_props = getattr(pitch, "parameters_json_schema", {}) or {}
    pitch_fields = pitch_props.get("properties") or {}
    assert write_desc
    assert "xref" in write_desc or "cross-reference" in write_desc or "persist" in write_desc
    assert pitch_desc
    assert "rung_pitch_mm" in pitch_fields
    pitch_param = pitch_fields["rung_pitch_mm"]
    pitch_param_desc = str(pitch_param.get("description") or "").lower()
    assert "rung_pitch" in pitch_desc or "12.7" in pitch_desc or "pitch" in pitch_desc
    assert "12.7" in pitch_param_desc or "pitch" in pitch_param_desc or "rung" in pitch_param_desc

    steps = [
        _response(
            "place",
            {"library_id": "iec.contactors.coil", "x": 40, "y": 40, "ref": "K1"},
            "place-coil",
        ),
        _response(
            "place",
            {"library_id": "iec.contactors.aux_no", "x": 40, "y": 100, "ref": "K1-13"},
            "place-aux",
        ),
        _response("set_cross_reference", {"rung_pitch_mm": 25.4}, "pitch"),
        _response("update_cross_references", {}, "write-xref"),
        ModelResponse(parts=[TextPart(content="Wrote K1 cross-references.")]),
    ]

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return steps.pop(0)

    drawing = open_drawing(name="XrefWrite")
    with drawing_agent.override(model=FunctionModel(model)):
        result = drawing_agent.run_sync("Set 1 in pitch and write cross-references.", deps=drawing)
    assert "K1" in result.output
    session = drawing.operator.session
    assert session is not None
    assert abs(float(session.cross_reference_settings()["rung_pitch_mm"]) - 25.4) < 0.01
    assert session.sheet().component("K1").properties.get("xref")
