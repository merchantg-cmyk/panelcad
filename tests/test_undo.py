from panelcad import Session
from panelcad.cli.agent import Agent


def test_undo_place_and_redo():
    s = Session.create("Undo")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    assert s.sheet().component("S1").library_id == "iec.operators.pb_no"
    assert s.can_undo()
    assert s.undo()
    assert [c.id for c in s.sheet().components] == []
    assert s.can_redo()
    assert s.redo()
    assert s.sheet().component("S1").x == 40


def test_undo_two_steps_then_new_edit_clears_redo():
    s = Session.create("Undo2")
    s.place("iec.operators.pb_no", ref="S1", x=10, y=10)
    s.place("iec.operators.pb_nc", ref="S2", x=20, y=20)
    assert s.undo()
    assert [c.id for c in s.sheet().components] == ["S1"]
    assert s.can_redo()
    s.place("iec.contactors.coil", ref="K1", x=30, y=30)
    assert not s.can_redo()
    assert {c.id for c in s.sheet().components} == {"S1", "K1"}
    assert s.undo()
    assert [c.id for c in s.sheet().components] == ["S1"]


def test_undo_wire():
    s = Session.create("UndoW")
    s.place("iec.operators.pb_no", ref="S1", x=10, y=10)
    s.place("iec.contactors.coil", ref="K1", x=40, y=10)
    s.wire("S1.13", "K1.A1")
    assert s.sheet().wires
    assert s.undo()
    assert s.sheet().wires == []
    assert s.sheet().component("K1")
    assert s.redo()
    assert s.sheet().wires[0].frm == "S1.13"


def test_empty_undo_is_false():
    s = Session.create("Empty")
    assert not s.can_undo()
    assert not s.undo()
    assert not s.redo()


def test_agent_undo():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "U"})["ok"]
    assert agent.handle({"id": 2, "op": "place", "library_id": "iec.breakers.mcb_3p_32a", "ref": "Q1"})["ok"]
    r = agent.handle({"id": 3, "op": "undo"})
    assert r["ok"] and r["result"]["undone"] is True
    assert agent._need().sheet().components == []
    r2 = agent.handle({"id": 4, "op": "redo"})
    assert r2["ok"] and r2["result"]["redone"] is True
    assert agent._need().sheet().component("Q1").id == "Q1"


def test_gui_multi_delete_undo_redo_is_one_step():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import ComponentItem, WireItem
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = Session.create("MultiDel")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    s.place("iec.operators.pb_nc", ref="S2", x=80, y=40)
    s.place("iec.contactors.coil", ref="K1", x=120, y=40)
    s.wire("S1.14", "K1.A1")
    win = MainWindow(s)
    win.show()
    canvas = win.canvas
    for item in canvas.scene.items():
        if isinstance(item, (ComponentItem, WireItem)):
            item.setSelected(True)
    canvas._delete_selected()
    assert s.sheet().components == []
    assert s.sheet().wires == []
    assert s.undo()
    assert {c.id for c in s.sheet().components} == {"S1", "S2", "K1"}
    assert len(s.sheet().wires) == 1
    assert s.redo()
    assert s.sheet().components == []
    assert s.sheet().wires == []
    win.close()
