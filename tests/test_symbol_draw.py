from panelcad.gui.symbol_draw import DrawModel


def test_library_editor_draw_tools_feed_spec():
    from PySide6.QtWidgets import QApplication

    from panelcad.api.session import Session
    from panelcad.gui.library_editor import LibraryEditor
    from panelcad.gui.symbol_canvas import TOOLS

    app = QApplication.instance() or QApplication([])
    dlg = LibraryEditor(Session.create("Lib"))
    labels = [dlg._tool_group.button(i).text() for i in range(len(TOOLS))]
    assert labels == ["Select", "Line", "Box", "Circle", "Points", "Attribute", "Connection"]
    assert dlg.windowTitle() == "Symbol Builder"
    dlg.canvas.model.add_line(0, 8, 28, 8)
    dlg.canvas.model.add_rect(6, 2, 16, 10)
    dlg.canvas.model.add_circle(14, 7, 3)
    dlg.canvas.model.add_polyline([(0, 0), (4, 4), (8, 0)])
    dlg.canvas.model.add_text(14, 14, "FU")
    dlg.part_id.setText("demo_fuse")
    spec = dlg._spec()
    assert spec["block"] == "parent"
    assert any(a["tag"] == "TAG1" for a in spec["attributes"])
    assert spec["draw"][0]["line"] == [0, 8, 28, 8]
    assert spec["draw"][1]["rect"] == [6, 2, 16, 10]
    assert spec["draw"][2]["circle"] == [14, 7, 3]
    assert spec["draw"][3]["polyline"] == [[0, 0], [4, 4], [8, 0]]
    assert spec["draw"][4]["text"][2] == "FU"
    dlg._tool_group.button(3).click()
    assert dlg.canvas.tool == "circle"
    dlg.close()
    app.processEvents()


def test_draw_model_line_box_circle_polyline_label():
    m = DrawModel()
    m.add_line(0, 8, 28, 8)
    m.add_rect(6, 2, 16, 10)
    m.add_circle(14, 7, 3)
    m.add_polyline([(0, 0), (4, 4), (8, 0)])
    m.add_text(14, 14, "FU", 2.0)
    draw = m.to_draw()
    assert draw[0] == {"line": [0, 8, 28, 8]}
    assert draw[1]["rect"] == [6, 2, 16, 10]
    assert draw[2]["circle"] == [14, 7, 3]
    assert draw[3]["polyline"] == [[0, 0], [4, 4], [8, 0]]
    assert draw[4]["text"][2] == "FU"
    m.delete(1)
    assert len(m.to_draw()) == 4
    assert m.hit_test(14, 7) is not None


def test_draw_model_load_roundtrip():
    items = [{"line": [0, 7, 28, 7]}, {"text": [14, 13, "F", 2.0]}]
    m = DrawModel.from_draw(items)
    assert m.to_draw() == items


def test_draw_model_snaps():
    m = DrawModel(snap=1.0)
    m.add_line(0.4, 7.6, 27.6, 8.2)
    assert m.to_draw()[0]["line"] == [0, 8, 28, 8]


def test_draw_model_move_translates_every_kind():
    m = DrawModel()
    m.add_line(0, 8, 28, 8)
    m.add_rect(6, 2, 16, 10)
    m.add_circle(14, 7, 3)
    m.add_polyline([(0, 0), (4, 4), (8, 0)])
    m.add_text(14, 14, "FU", 2.0)
    m.move(0, 2, -1)
    assert m.to_draw()[0]["line"] == [2, 7, 30, 7]
    m.move(1, 1, 1)
    assert m.to_draw()[1]["rect"] == [7, 3, 16, 10]
    m.move(2, 0, 2)
    assert m.to_draw()[2]["circle"] == [14, 9, 3]
    m.move(3, 1, 0)
    assert m.to_draw()[3]["polyline"] == [[1, 0], [5, 4], [9, 0]]
    m.move(4, 0, -2)
    assert m.to_draw()[4]["text"][0:2] == [14, 12]


def test_draw_model_replace_translated_does_not_accumulate():
    m = DrawModel()
    m.add_line(0, 8, 28, 8)
    origin = {"line": [0, 8, 28, 8]}
    m.replace_translated(0, origin, 2.4, -0.6)
    m.replace_translated(0, origin, 2.4, -0.6)
    assert m.to_draw()[0]["line"] == [2, 7, 30, 7]
