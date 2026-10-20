"""3-phase power colors: palettes, phase swaps, windings, GUI, agent, and DXF."""

from __future__ import annotations

import ezdxf
from ezdxf.colors import rgb2int

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.phase_color import legible_pen, palette_for, wire_phase_pens
from panelcad.io.yaml_format import dump_project


def _id(result: dict, role: str) -> str:
    return next(c["id"] for c in result["components"] if c["role"] == role)


def _between(session: Session, sheet_id: str, a: str, b: str):
    for wire in session.sheet(sheet_id).wires:
        if {wire.frm, wire.to} == {a, b}:
            return wire
    ends = [(wire.frm, wire.to) for wire in session.sheet(sheet_id).wires]
    raise AssertionError((a, b, ends))


def _pens(session: Session, sheet_id: str) -> dict:
    sheet = session.sheet(sheet_id)
    return wire_phase_pens(session.project, sheet, session.catalog)


def test_phase_strokes_share_one_width_and_pale_colors_stay_solid():
    """Grey and yellow are solid mid tones. Every phase uses the power-wire width."""

    yellow = legible_pen("#c9a000", phase="L3")
    grey = legible_pen("grey", phase="L3")
    brown = legible_pen("brown", phase="L1")
    white = legible_pen("#f2f2f2", phase="N")
    assert yellow.stroke == "#c9a000"
    assert yellow.outline is None
    assert yellow.width == 0.45
    assert grey.stroke == "#7f7f7f"
    assert grey.outline is None
    assert grey.width == 0.45
    assert brown.stroke == "#6b3e23"
    assert brown.outline is None
    assert brown.width == 0.45
    assert white.stroke == "#a6a6a6"
    assert white.outline is None
    assert white.width == 0.45


def test_yaml_omits_the_default_palette():
    session = Session.create("phase yaml")
    assert "phase_colors" not in dump_project(session.project)
    session.set_phase_colors(system_voltage=240)
    text = dump_project(session.project)
    assert "phase_colors" in text
    assert "240" in text
    loaded = Session.open(_write(session))
    assert loaded.phase_colors()["system_voltage"] == 240


def _write(session: Session):
    import tempfile
    from pathlib import Path

    dest = Path(tempfile.mkdtemp()) / "phase.pcad.yaml"
    session.save(dest)
    return dest


def test_nema_reversing_colors_follow_the_l1_l3_swap(tmp_path):
    """480 V is brown/orange/yellow. The swapped REV leg keeps the phase that feeds it."""

    session = Session.create("phase rev")
    result = session.insert_circuit(kind="reversing", control="local")
    sheet_id = result["sheet"]
    name, _palette = palette_for(session.project, session.sheet(sheet_id))
    assert name == "nema_high"
    pens = _pens(session, sheet_id)
    breaker = _id(result, "breaker")
    rev = _id(result, "rev")
    overload = _id(result, "overload")
    swapped = _between(session, sheet_id, f"{breaker}.2", f"{rev}.L3")
    crossed = _between(session, sheet_id, f"{rev}.T1", f"{overload}.T1")
    assert pens[swapped.id].phase == "L1"
    assert pens[swapped.id].stroke == "#6b3e23"
    assert pens[swapped.id].outline is None
    assert pens[crossed.id].phase == "L3"
    assert pens[crossed.id].stroke == "#c9a000"
    assert pens[crossed.id].outline is None
    assert pens[crossed.id].width == 0.45
    assert _pens(session, result["control_sheet"]) == {}

    dest = tmp_path / "rev.dxf"
    session.export_dxf(dest, sheet_id=sheet_id)
    doc = ezdxf.readfile(dest)
    colors = {
        entity.dxf.true_color
        for entity in doc.modelspace()
        if entity.dxftype() == "LWPOLYLINE" and entity.dxf.layer == "WIRES"
    }
    assert rgb2int((0x6B, 0x3E, 0x23)) in colors
    assert rgb2int((0xC9, 0xA0, 0x00)) in colors
    assert rgb2int((0x7F, 0x7F, 0x7F)) not in colors

    session.set_phase_colors(system_voltage=240)
    name, _palette = palette_for(session.project, session.sheet(sheet_id))
    assert name == "nema_low"
    low = _pens(session, sheet_id)
    assert low[swapped.id].stroke == "#111111"
    assert low[_between(session, sheet_id, f"{breaker}.4", f"{rev}.L2").id].stroke == "#c42020"
    straight_l3 = _between(session, sheet_id, f"{breaker}.6", f"{_id(result, 'fwd')}.L3")
    assert low[straight_l3.id].stroke == "#1d4e9e"

    session.set_phase_colors(nema_low="green,red,blue")
    assert _pens(session, sheet_id)[swapped.id].stroke == "#1e8a34"
    session.set_phase_colors(enabled=False)
    assert _pens(session, sheet_id) == {}


def test_iec_star_delta_winding_keeps_the_feeding_phase():
    """IEC ignores the 480 V cutoff. U2 takes V1's phase. The star bridge is not a neutral."""

    session = Session.create("phase star")
    session.set_title_block(standard="IEC")
    result = session.insert_circuit(kind="star_delta", control="local")
    sheet_id = result["sheet"]
    name, _palette = palette_for(session.project, session.sheet(sheet_id))
    assert name == "iec"
    pens = _pens(session, sheet_id)
    breaker = _id(result, "breaker")
    main = _id(result, "main")
    motor = _id(result, "motor")
    delta = _id(result, "delta")
    star = _id(result, "star")
    feed = _between(session, sheet_id, f"{breaker}.2", f"{main}.L1")
    assert pens[feed.id].phase == "L1"
    assert pens[feed.id].stroke == "#6b3e23"
    u2 = _between(session, sheet_id, f"{motor}.U2", f"{delta}.L2")
    assert pens[u2.id].phase == "L2", pens[u2.id]
    assert pens[u2.id].stroke == "#111111"
    w2 = _between(session, sheet_id, f"{motor}.W2", f"{delta}.L1")
    assert pens[w2.id].phase == "L1"
    assert pens[w2.id].stroke == "#6b3e23"
    assert pens[w2.id].outline is None
    assert pens[w2.id].width == 0.45
    bridge = _between(session, sheet_id, f"{star}.T1", f"{star}.T2")
    assert bridge.id not in pens
    assert _pens(session, result["control_sheet"]) == {}


def test_iec_ground_is_green_with_a_yellow_edge():
    session = Session.create("phase pe")
    session.set_title_block(standard="IEC")
    session.place("iec.contactors.coil", ref="K1", x=40, y=40)
    session.place("iec.contactors.coil", ref="K2", x=110, y=40)
    ground = session.wire("K1.A2", "K2.A1", net="PE")
    neutral = session.wire("K1.A1", "K2.A2", net="N")
    pens = _pens(session, session.sheet().id)
    assert pens[ground.id].phase == "PE"
    assert pens[ground.id].stroke == "#1e8a34"
    assert pens[ground.id].outline == "#a68400"
    assert pens[neutral.id].phase == "N"
    assert pens[neutral.id].stroke == "#5eb0e8"
    assert pens[neutral.id].outline is None
    assert pens[neutral.id].width == 0.45


def test_gui_jsonl_drawing_agent_and_cli_set_phase_colors(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QDialog, QPushButton
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from panelcad.ai.drawing import drawing_agent, open_drawing
    from panelcad.cli.main import app
    from panelcad.gui.docks import PropertiesDock
    from panelcad.gui.phase_dialog import PhaseColorDialog

    def _response(tool_name: str, args: dict, call_id: str) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args, tool_call_id=call_id)])

    qt = QApplication.instance() or QApplication([])
    session = Session.create("phase gui")
    changed: list[bool] = []
    dock = PropertiesDock(on_change=lambda: changed.append(True))
    dock.show_project(session)
    button = next(btn for btn in dock.findChildren(QPushButton) if btn.text() == "Phase colors…")

    def accept(self):
        self.voltage.setText("208")
        self.nema_low[0].setText("black")
        self.nema_low[1].setText("red")
        self.nema_low[2].setText("blue")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(PhaseColorDialog, "exec", accept)
    button.click()
    qt.processEvents()
    assert session.phase_colors()["system_voltage"] == 208
    assert session.phase_colors()["nema_low"] == {"l1": "#111111", "l2": "#c42020", "l3": "#1d4e9e"}
    assert changed

    dialog = PhaseColorDialog(session.phase_colors())
    dialog.voltage.setText("")
    dialog.iec[2].setText("gray")
    fields = dialog.values()
    assert fields["system_voltage"] == "auto"
    session.set_phase_colors(**fields)
    assert session.phase_colors()["system_voltage"] is None
    assert session.phase_colors()["iec"]["l3"] == "#7f7f7f"
    dialog.close()

    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Phase JSONL"})["ok"]
    help_result = agent.handle({"id": 2, "op": "help"})
    assert "phase_colors" in help_result["result"]["ops"]
    assert "set_phase_colors" in help_result["result"]["ops"]
    schema = help_result["result"]["schemas"]["set_phase_colors"]
    assert schema["op"] == "set_phase_colors"
    assert "nema_high" in schema["args"]
    wrote = agent.handle(
        {"id": 3, "op": "set_phase_colors", "system_voltage": "480", "nema_high": "brown,orange,yellow"}
    )
    assert wrote["ok"], wrote
    assert wrote["result"]["system_voltage"] == 480
    assert wrote["result"]["nema_high"]["l3"] == "#c9a000"
    read = agent.handle({"id": 4, "op": "phase_colors"})
    assert read["result"]["iec"]["l1"] == "#6b3e23"

    seen: dict[str, object] = {}

    def capture(messages, info):
        for tool in info.function_tools:
            seen[tool.name] = tool
        return ModelResponse(parts=[TextPart(content="ready")])

    drawing = open_drawing(name="Phase tool")
    with drawing_agent.override(model=FunctionModel(capture)):
        drawing_agent.run_sync("List tools.", deps=drawing)
    tool = seen["set_phase_colors"]
    fields_schema = (getattr(tool, "parameters_json_schema", {}) or {}).get("properties") or {}
    assert "system_voltage" in fields_schema
    assert "iec" in fields_schema
    description = str(getattr(tool, "description", "") or "").lower()
    assert "480" in description or "brown" in description

    def model(messages, info):
        return _response("set_phase_colors", {"system_voltage": "240", "nema_low": "black,red,blue"}, "colors")

    steps = {"n": 0}

    def sequenced(messages, info):
        steps["n"] += 1
        if steps["n"] == 1:
            return model(messages, info)
        return ModelResponse(parts=[TextPart(content="Set 240 V colors.")])

    drawing = open_drawing(name="Phase write")
    with drawing_agent.override(model=FunctionModel(sequenced)):
        result = drawing_agent.run_sync("Use the 240 V palette.", deps=drawing)
    assert "240" in result.output
    assert drawing.operator.session is not None
    assert drawing.operator.session.phase_colors()["system_voltage"] == 240

    path = tmp_path / "job.pcad.yaml"
    Session.create("CLI phase").save(path)
    from typer.testing import CliRunner

    runner = CliRunner()
    shown = runner.invoke(app, ["electrical", "phase-colors", str(path)])
    assert shown.exit_code == 0, shown.output
    assert "enabled=True" in shown.output
    assert "phase_colors" not in path.read_text(encoding="utf-8")
    done = runner.invoke(
        app,
        ["electrical", "phase-colors", str(path), "--voltage", "240", "--nema240", "black,red,blue"],
    )
    assert done.exit_code == 0, done.output
    assert "voltage=240" in done.output
    loaded = Session.open(path)
    assert loaded.phase_colors()["system_voltage"] == 240
    assert loaded.phase_colors()["nema_low"]["l2"] == "#c42020"


def test_phase_follows_every_pole_from_supply_to_the_motor():
    """Each series device keeps one phase color. No conductor is left on the default ink."""

    from panelcad.core.phase_color import pen_for_phase
    from panelcad.render.phase_ink import pole_runs

    session = Session.create("pole walk")
    result = session.insert_circuit(kind="reversing", control="local")
    session.set_phase_colors(system_voltage=240)
    sheet_id = result["sheet"]
    sheet = session.sheet(sheet_id)
    pens = _pens(session, sheet_id)
    runs = pole_runs(session.project, sheet, session.catalog)
    by_ref: dict[str, list] = {}
    for run in runs:
        by_ref.setdefault(run.ref, []).append(run)

    breaker = _id(result, "breaker")
    fwd = _id(result, "fwd")
    rev = _id(result, "rev")
    overload = _id(result, "overload")
    motor = _id(result, "motor")

    def _expect(ref: str, phase: str) -> None:
        found = by_ref.get(ref) or []
        assert found, (ref, "pole graphic stayed on the default ink")
        want = pen_for_phase(session.project, sheet, phase)
        assert want is not None
        for run in found:
            assert run.pen.phase == phase, (ref, run.pen.phase, phase)
            assert run.pen.stroke == want.stroke, (ref, run.pen.stroke, want.stroke)
            assert run.pen.width == 0.45
            assert run.pen.stroke != "#0b4ea2"

    # L2 is red straight through the forward contactor. L1 and L3 swap on M2.
    for ref in (f"{breaker}.4", f"{fwd}.L2", f"{fwd}.T2", f"{overload}.T2", f"{overload}.4", f"{motor}.T2"):
        _expect(ref, "L2")
    for ref in (f"{breaker}.2", f"{rev}.L3", f"{rev}.T3"):
        _expect(ref, "L1")
    for ref in (f"{rev}.L1", f"{rev}.T1"):
        _expect(ref, "L3")
    # After the contactors rejoin, the motor lead keeps the phase of that wire.
    for src, dst in ((f"{overload}.2", f"{motor}.T1"), (f"{overload}.6", f"{motor}.T3")):
        feed = _between(session, sheet_id, src, dst)
        assert pens[feed.id].phase in {"L1", "L3"}
        _expect(dst, pens[feed.id].phase)
    assert pens[_between(session, sheet_id, f"{overload}.2", f"{motor}.T1").id].phase != pens[
        _between(session, sheet_id, f"{overload}.6", f"{motor}.T3").id
    ].phase
    l2 = _between(session, sheet_id, f"{breaker}.4", f"{fwd}.L2")
    assert pens[l2.id].phase == "L2"
    assert pens[l2.id].stroke == "#c42020"
    swapped = _between(session, sheet_id, f"{breaker}.2", f"{rev}.L3")
    assert pens[swapped.id].phase == "L1"
    assert pens[swapped.id].stroke == "#111111"
    for wire in sheet.wires:
        ends = {wire.frm.split(".", 1)[0], wire.to.split(".", 1)[0]}
        if not ends & {breaker, fwd, rev, overload, motor}:
            continue
        if wire.id not in pens:
            continue
        assert pens[wire.id].stroke != "#0b4ea2"

    iec = Session.create("pole star")
    iec.set_title_block(standard="IEC")
    star = iec.insert_circuit(kind="star_delta", control="local")
    star_sheet = iec.sheet(star["sheet"])
    star_runs = pole_runs(iec.project, star_sheet, iec.catalog)
    star_refs: dict[str, list] = {}
    for run in star_runs:
        star_refs.setdefault(run.ref, []).append(run)
    motor_id = _id(star, "motor")
    delta = _id(star, "delta")
    star_id = _id(star, "star")
    main = _id(star, "main")
    feeder = _id(star, "overload")

    def _star(ref: str, phase: str) -> None:
        found = star_refs.get(ref) or []
        assert found, (ref, "uncolored pole")
        want = pen_for_phase(iec.project, star_sheet, phase)
        assert want is not None
        for run in found:
            assert run.pen.phase == phase, (ref, run.pen.phase, phase)
            assert run.pen.stroke == want.stroke

    _star(f"{motor_id}.U1", "L1")
    _star(f"{motor_id}.V1", "L2")
    _star(f"{motor_id}.W1", "L3")
    # Delta joins W2-U1, U2-V1, V2-W1, so the winding end takes that phase.
    _star(f"{motor_id}.W2", "L1")
    _star(f"{motor_id}.U2", "L2")
    _star(f"{motor_id}.V2", "L3")
    _star(f"{main}.T1", "L1")
    _star(f"{feeder}.2", "L1")
    _star(f"{delta}.L1", "L1")
    _star(f"{delta}.T1", "L1")
    _star(f"{delta}.L2", "L2")
    _star(f"{delta}.T2", "L2")
    _star(f"{delta}.L3", "L3")
    _star(f"{delta}.T3", "L3")
    _star(f"{star_id}.L1", "L1")
    _star(f"{star_id}.L2", "L2")
    assert f"{star_id}.T1" not in star_refs
    assert f"{star_id}.T2" not in star_refs
    u2 = _between(iec, star["sheet"], f"{motor_id}.U2", f"{delta}.L2")
    assert _pens(iec, star["sheet"])[u2.id].phase == "L2"
    assert _pens(iec, star["sheet"])[u2.id].stroke == "#111111"
    w2 = _between(iec, star["sheet"], f"{motor_id}.W2", f"{delta}.L1")
    assert _pens(iec, star["sheet"])[w2.id].phase == "L1"
    assert _pens(iec, star["sheet"])[w2.id].stroke == "#6b3e23"


def test_supply_to_motor_walk_keeps_one_color_per_phase():
    """Walk every power conductor from the supply poles to the motor.

    A series device passes the phase through its pole map, including a
    reversing swap and a drive output. No segment on that walk is left on
    the default ink. Star-delta winding ends keep the delta phase.
    """

    from panelcad.core.phase_color import _POLES, pen_for_phase

    default_ink = "#0b4ea2"

    def _walk(session: Session, sheet_id: str, supply: str, motor: str) -> None:
        sheet = session.sheet(sheet_id)
        pens = _pens(session, sheet_id)
        catalog = session.catalog

        def _device(ref: str):
            return catalog.get(sheet.component(ref).library_id)

        def _star(ref: str) -> bool:
            try:
                ids = {pin.id for pin in _device(ref).pins}
            except KeyError:
                return False
            if not {"T1", "T2", "T3"} <= ids:
                return False
            for wire in sheet.wires:
                ends = [
                    end.split(".", 1)[1]
                    for end in (wire.frm, wire.to)
                    if end.startswith(ref + ".")
                ]
                if len(ends) == 2 and set(ends) <= {"T1", "T2", "T3"}:
                    return True
            return False

        poles: dict[str, set[str]] = {}
        for comp in sheet.components:
            if comp.id == motor or _star(comp.id):
                continue
            try:
                device = _device(comp.id)
            except KeyError:
                continue
            if str(getattr(device, "category", "") or "").startswith("load.motor"):
                continue
            ids = {pin.id for pin in device.pins}
            for src, dst in _POLES:
                if src in ids and dst in ids:
                    poles.setdefault(f"{comp.id}.{src}", set()).add(f"{comp.id}.{dst}")
                    poles.setdefault(f"{comp.id}.{dst}", set()).add(f"{comp.id}.{src}")

        adj: dict[str, list[tuple[str, object]]] = {}
        for wire in sheet.wires:
            if "." not in wire.frm or "." not in wire.to:
                continue
            adj.setdefault(wire.frm, []).append((wire.to, wire))
            adj.setdefault(wire.to, []).append((wire.frm, wire))

        supply_ids = {pin.id for pin in _device(supply).pins}
        seeds = [
            f"{supply}.{pin}"
            for pin in ("2", "4", "6", "T1", "T2", "T3")
            if pin in supply_ids and adj.get(f"{supply}.{pin}")
        ]
        assert len(seeds) >= 3, (supply, seeds)
        visited: dict[str, str] = {}
        reached: set[str] = set()
        for seed in seeds:
            phases = {
                pens[wire.id].phase
                for _other, wire in adj[seed]
                if wire.id in pens and pens[wire.id].phase in {"L1", "L2", "L3"}
            }
            assert len(phases) == 1, (seed, phases)
            phase = next(iter(phases))
            want = pen_for_phase(session.project, sheet, phase)
            assert want is not None
            pending = [seed]
            seen: set[str] = set()
            while pending:
                ref = pending.pop()
                if ref in seen:
                    continue
                seen.add(ref)
                if ref.startswith(motor + "."):
                    reached.add(phase)
                for other, wire in adj.get(ref, []):
                    pen = pens.get(wire.id)
                    if pen is None or pen.phase != phase:
                        continue
                    assert pen.stroke == want.stroke, (wire.frm, wire.to, pen.stroke, want.stroke)
                    assert pen.stroke != default_ink
                    assert pen.width == 0.45
                    assert pen.outline is None
                    previous = visited.get(wire.id)
                    assert previous in {None, phase}, (wire.frm, wire.to, previous, phase)
                    visited[wire.id] = phase
                    pending.append(other)
                pending.extend(poles.get(ref, ()))
        assert reached == {"L1", "L2", "L3"}
        strokes = {phase: pen_for_phase(session.project, sheet, phase).stroke for phase in reached}
        assert len(set(strokes.values())) == 3
        phased = {wire_id for wire_id, pen in pens.items() if pen.phase in {"L1", "L2", "L3"}}
        assert phased == set(visited), phased - set(visited)
        for wire_id, pen in pens.items():
            assert pen.stroke != default_ink, (wire_id, pen.phase, pen.stroke)
            assert pen.width == 0.45

    low = Session.create("walk rev")
    rev = low.insert_circuit(kind="reversing", control="local")
    low.set_phase_colors(system_voltage=240)
    _walk(low, rev["sheet"], _id(rev, "breaker"), _id(rev, "motor"))
    assert _pens(low, rev["sheet"])[
        _between(low, rev["sheet"], f"{_id(rev, 'breaker')}.2", f"{_id(rev, 'rev')}.L3").id
    ].stroke == "#111111"

    high = Session.create("walk rev 480")
    rev_high = high.insert_circuit(kind="reversing", control="local")
    high.set_phase_colors(system_voltage=480)
    _walk(high, rev_high["sheet"], _id(rev_high, "breaker"), _id(rev_high, "motor"))

    iec = Session.create("walk star")
    iec.set_title_block(standard="IEC")
    star = iec.insert_circuit(kind="star_delta", control="local")
    _walk(iec, star["sheet"], _id(star, "breaker"), _id(star, "motor"))
    motor = _id(star, "motor")
    delta = _id(star, "delta")
    pens = _pens(iec, star["sheet"])
    assert pens[_between(iec, star["sheet"], f"{motor}.W2", f"{delta}.L1").id].phase == "L1"
    assert pens[_between(iec, star["sheet"], f"{motor}.U2", f"{delta}.L2").id].phase == "L2"
    assert pens[_between(iec, star["sheet"], f"{motor}.V2", f"{delta}.L3").id].phase == "L3"

    drive = Session.create("walk vfd")
    vfd = drive.insert_circuit(kind="vfd", control="local", drive="generic_1k5")
    _walk(drive, vfd["sheet"], _id(vfd, "breaker"), _id(vfd, "motor"))

    soft = Session.create("walk soft")
    started = soft.insert_circuit(kind="soft_starter", control="local", bypass=True)
    _walk(soft, started["sheet"], _id(started, "breaker"), _id(started, "motor"))


def test_a_terminal_keeps_the_phase():
    """A feed-through block is a pole: the color enters on A and leaves on B."""

    session = Session.create("term phase")
    session.place("nema.breakers.mcb_3p_32a", ref="CB1", x=40, y=30)
    session.place("iec.terminals.x_2p5", ref="X1", x=80, y=120)
    session.place("nema.loads.motor_3ph", ref="MTR1", x=40, y=170)
    incoming = session.wire("CB1.2", "X1.A")
    outgoing = session.wire("X1.B", "MTR1.T1")
    pens = _pens(session, session.sheet().id)
    assert pens[incoming.id].phase == "L1"
    assert pens[outgoing.id].phase == "L1"
    assert pens[outgoing.id].stroke == pens[incoming.id].stroke


def test_line_side_runs_color_the_breaker_leads():
    """Incoming L1/L2/L3 are symbol leads, not wires. Color still starts above Q1."""

    from panelcad.render.phase_ink import line_side_runs

    session = Session.create("phase leads")
    session.set_title_block(standard="IEC")
    result = session.insert_circuit(kind="star_delta", control="local")
    sheet = session.sheet(result["sheet"])
    runs = line_side_runs(session.project, sheet, session.catalog)
    phases = sorted(run.pen.phase for run in runs)
    assert phases == ["L1", "L2", "L3"]
    for run in runs:
        assert run.pen.outline is None
        assert run.pen.width == 0.45
        assert len(run.points) >= 3
        top = min(point[1] for point in run.points)
        pin = run.points[1][1]
        assert pin - top >= 2.4
    assert line_side_runs(session.project, session.sheet(result["control_sheet"]), session.catalog) == []


def test_a_crossing_hops_the_horizontal():
    from panelcad.core.geometry import Point
    from panelcad.render.crossings import hop_routes

    hopped = hop_routes(
        {
            "h": [Point(x=0, y=10), Point(x=30, y=10)],
            "v": [Point(x=12, y=0), Point(x=12, y=24)],
        }
    )
    assert any(point[1] < 9.4 for point in hopped["h"])
    assert hopped["v"] == [(12.0, 0.0), (12.0, 24.0)]
    tee = hop_routes(
        {
            "h": [Point(x=0, y=10), Point(x=12, y=10)],
            "v": [Point(x=12, y=10), Point(x=12, y=24)],
        }
    )
    assert all(abs(point[1] - 10.0) < 0.05 for point in tee["h"])


def test_iec_neutral_is_light_blue_and_a_blue_line_warns(tmp_path):
    """IEC neutral is blue (default light blue). NEMA neutral stays white/grey. A blue line warns."""

    from PySide6.QtWidgets import QApplication
    from typer.testing import CliRunner

    from panelcad.cli.agent import Agent
    from panelcad.cli.main import app
    from panelcad.core.models import PhaseColors
    from panelcad.gui.phase_dialog import PhaseColorDialog

    QApplication.instance() or QApplication([])
    blue = PhaseColors().iec_neutral
    iec = Session.create("iec neutral")
    iec.set_title_block(standard="IEC")
    iec.place("iec.contactors.coil", ref="K1", x=40, y=40)
    iec.place("iec.contactors.coil", ref="K2", x=110, y=40)
    neutral = iec.wire("K1.A1", "K2.A2", net="N")
    assert _pens(iec, iec.sheet().id)[neutral.id].stroke == blue
    nema = Session.create("nema neutral")
    nema.place("nema.contactors.coil", ref="K1", x=40, y=40)
    nema.place("nema.contactors.coil", ref="K2", x=110, y=40)
    nema_n = nema.wire("K1.A1", "K2.A2", net="N")
    assert _pens(nema, nema.sheet().id)[nema_n.id].stroke == "#a6a6a6"

    assert iec.phase_colors()["warnings"] == []
    iec.set_phase_colors(iec="blue,black,grey")
    warnings = iec.phase_colors()["warnings"]
    assert warnings and "neutral" in warnings[0].lower()
    assert "60445" not in warnings[0]
    iec.set_phase_colors(iec="brown,black,grey")
    assert iec.phase_colors()["warnings"] == []
    bare = Session.create("no neutral")
    bare.set_title_block(standard="IEC")
    bare.set_phase_colors(iec="blue,black,grey")
    assert bare.phase_colors()["warnings"] == []

    from PySide6.QtWidgets import QLabel

    dialog = PhaseColorDialog(iec.phase_colors())
    text = " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert "permitted" in text.lower()
    assert "shop convention" in text.lower()
    assert "blue (default light blue" in text.lower()
    assert "60445" in text and "unverified" in text.lower()
    assert "NEC" not in text
    assert dialog.values()["iec_neutral"] == blue
    dialog.close()

    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Neutral JSONL", "standard": "IEC"})["ok"]
    help_result = agent.handle({"id": 2, "op": "help"})
    schema = help_result["result"]["schemas"]["set_phase_colors"]
    assert "iec_neutral" in schema["args"]
    wrote = agent.handle({"id": 3, "op": "set_phase_colors", "iec_neutral": "lightblue"})
    assert wrote["ok"], wrote
    assert wrote["result"]["iec_neutral"] == blue

    path = tmp_path / "neutral.pcad.yaml"
    Session.create("CLI neutral").save(path)
    runner = CliRunner()
    done = runner.invoke(app, ["electrical", "phase-colors", str(path), "--iec-neutral", "#3d8fd4"])
    assert done.exit_code == 0, done.output
    assert Session.open(path).phase_colors()["iec_neutral"] == "#3d8fd4"

    # The motor star point is T1–T3 tied together. It is not a system neutral,
    # so a blue line color does not warn and the bridge stays ordinary ink.
    # A transformer primary pin named N is a real neutral and still warns.
    star = Session.create("star bridge is not a neutral")
    star.set_title_block(standard="IEC")
    star.place("iec.contactors.ac3_9a", ref="K3", x=80, y=80)
    bridge = star.wire("K3.T1", "K3.T2", net="STAR")
    star.wire("K3.T2", "K3.T3", net="STAR")
    star.set_phase_colors(iec="blue,black,grey")
    assert star.phase_colors()["warnings"] == []
    assert bridge.id not in _pens(star, star.sheet().id)


def test_phase_color_warning_shows_in_the_cli_and_gui_and_the_canvas_repaints(tmp_path, monkeypatch):
    """Session and JSONL already returned the warning. The GUI, CLI, and canvas must too."""

    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

    from panelcad.cli.main import app
    from panelcad.gui.canvas import WireItem
    from panelcad.gui.main_window import MainWindow
    from panelcad.gui.phase_dialog import PhaseColorDialog
    from typer.testing import CliRunner

    QApplication.instance() or QApplication([])
    session = Session.create("phase warn")
    session.set_title_block(standard="IEC")
    session.place("iec.contactors.coil", ref="K1", x=40, y=40)
    session.place("iec.contactors.coil", ref="K2", x=110, y=40)
    neutral = session.wire("K1.A1", "K2.A2", net="N")
    path = tmp_path / "warn.pcad.yaml"
    session.save(path)
    runner = CliRunner()
    shown = runner.invoke(app, ["electrical", "phase-colors", str(path), "--iec", "blue,black,grey"])
    assert shown.exit_code == 0, shown.output
    assert "warning:" in shown.output.lower()
    assert "neutral" in shown.output.lower()

    session.set_phase_colors(iec="brown,black,grey", iec_neutral="#5eb0e8")
    window = MainWindow(session)

    def _stroke() -> str:
        for item in window.canvas.scene.items():
            if isinstance(item, WireItem) and item.wire_id == neutral.id:
                return QColor(item._base_pen.color()).name().lower()
        raise AssertionError("neutral wire is not on the canvas")

    assert _stroke() == "#5eb0e8"
    captured: dict[str, str] = {}

    def _warning(_parent, _title, text) -> None:
        captured["text"] = text

    monkeypatch.setattr(QMessageBox, "warning", _warning)
    monkeypatch.setattr(PhaseColorDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    original = PhaseColorDialog.values

    def _values(self):
        data = original(self)
        data["iec"] = "#1d4e9e,#111111,#7f7f7f"
        data["iec_neutral"] = "#3d85c6"
        return data

    monkeypatch.setattr(PhaseColorDialog, "values", _values)
    window.props._edit_phase_colors()
    assert "neutral" in captured.get("text", "").lower()
    assert _stroke() == "#3d85c6"
    window.close()
