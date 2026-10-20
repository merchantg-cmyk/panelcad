"""0.4.0 features are reachable from the GUI and from the in-app drawing agent."""

from __future__ import annotations

from unittest.mock import patch

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from panelcad import Session
from panelcad.ai.drawing import drawing_agent, open_drawing
from panelcad.cli.agent import Agent
from panelcad.gui.circuit_dialog import CircuitBuilderDialog
from panelcad.gui.enclosure_dialog import SizeEnclosureDialog
from panelcad.gui.main_window import MainWindow
from panelcad.gui.package_dialog import PrintPackageDialog

_DRAWING_TOOLS = {
    "recommend_enclosure",
    "apply_enclosure",
    "parts_search",
    "assign_part",
    "print_package",
    "update_cross_references",
    "set_cross_reference",
    "device_index",
    "lookup_zone",
    "trace_wire",
}
_CIRCUIT_KINDS = {"reversing", "star_delta", "soft_starter", "plc_motor"}


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _menu_actions(win: MainWindow) -> list:
    found = []

    def walk(menu) -> None:
        for act in menu.actions():
            child = act.menu()
            if child is not None:
                walk(child)
            elif not act.isSeparator():
                found.append(act)

    bar = win.menuBar()
    for act in bar.actions():
        menu = act.menu()
        if menu is not None:
            walk(menu)
        elif not act.isSeparator():
            found.append(act)
    return found


def _action(win: MainWindow, label: str):
    want = label.replace("&", "")
    for act in _menu_actions(win):
        if act.text().replace("&", "") == want:
            return act
    raise AssertionError(f"menu action not found: {label}")


def _rejecting(cls):
    class Probe(cls):
        opened: list = []

        def exec(self):  # noqa: A003
            type(self).opened.append(self)
            return QDialog.DialogCode.Rejected

    Probe.opened = []
    return Probe


def test_release_features_are_in_the_gui_and_the_drawing_agent():
    """Menus open the new dialogs, and the drawing agent plus insert_circuit schema list every new tool."""

    app = _app()
    session = Session.create("0.4.0 features")
    session.place("nema.contactors.ac3_9a", ref="M1", x=40, y=40)
    win = MainWindow(session)
    try:
        enclosure = _rejecting(SizeEnclosureDialog)
        package = _rejecting(PrintPackageDialog)
        circuit = _rejecting(CircuitBuilderDialog)
        with (
            patch("panelcad.gui.enclosure_dialog.SizeEnclosureDialog", enclosure),
            patch("panelcad.gui.package_dialog.PrintPackageDialog", package),
            patch("panelcad.gui.circuit_dialog.CircuitBuilderDialog", circuit),
            patch("panelcad.gui.part_picker.pick_catalog_part", return_value=None) as pick,
        ):
            _action(win, "Size Enclosure…").trigger()
            _action(win, "Print Package…").trigger()
            _action(win, "Circuit Builder…").trigger()
            catalog_buttons = [
                btn for btn in win.library_dock.findChildren(QPushButton) if btn.text() == "Catalog parts…"
            ]
            assert catalog_buttons
            catalog_buttons[0].click()
            assert pick.called
            win.props.show_component(session, "M1")
            inspector = [
                btn for btn in win.props.findChildren(QPushButton) if btn.text() == "Catalog part…"
            ]
            assert inspector
            pick.reset_mock()
            inspector[0].click()
            assert pick.called

        assert enclosure.opened, "Size Enclosure… did not construct SizeEnclosureDialog"
        assert package.opened, "Print Package… did not construct PrintPackageDialog"
        assert circuit.opened, "Circuit Builder… did not construct CircuitBuilderDialog"
        kinds = {
            circuit.opened[0].kind.itemData(i) for i in range(circuit.opened[0].kind.count())
        }
        assert _CIRCUIT_KINDS <= kinds

        session.set_cross_reference(rung_pitch_mm=6.35)
        _action(win, "NEMA line pitch: 0.5 in (12.7 mm)").trigger()
        assert session.cross_reference_settings()["rung_pitch_mm"] == 12.7
        _action(win, "Update cross-references").trigger()
        assert "cross-reference" in win.statusBar().currentMessage()
    finally:
        win.close()

    seen: set[str] = set()

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.update(tool.name for tool in info.function_tools)
        for tool in info.function_tools:
            assert isinstance(tool.parameters_json_schema, dict)
        return ModelResponse(parts=[TextPart(content="ready")])

    drawing = open_drawing(name="0.4.0 tools")
    with drawing_agent.override(model=FunctionModel(model)):
        result = drawing_agent.run_sync("Which release tools are registered?", deps=drawing)
    assert result.output == "ready"
    assert _DRAWING_TOOLS <= seen

    help_result = Agent().handle({"id": 1, "op": "help"})
    assert help_result["ok"] is True
    schema = help_result["result"]["schemas"]["insert_circuit"]
    assert schema["op"] == "insert_circuit"
    assert _CIRCUIT_KINDS <= set(schema["kinds"])
    for name in (
        "parts_search",
        "assign_part",
        "print_package",
        "update_cross_references",
        "set_cross_reference",
        "device_index",
        "lookup_zone",
        "trace_wire",
        "size_enclosure",
        "recommend_enclosure",
        "apply_enclosure",
    ):
        assert name in help_result["result"]["ops"]
