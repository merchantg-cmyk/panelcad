"""Symbol Builder blocks: attributes fill in on insert, and the footprint is its own geometry."""

from pathlib import Path

import yaml

from panelcad import Session
from panelcad.core.block import attribute_value, missing_attributes


def test_parent_block_roundtrip_and_inserted_tag(tmp_path: Path):
    s = Session.create("Blocks")
    s.save(tmp_path / "job.pcad.yaml")
    saved = s.save_library_device(
        {
            "id": "coil_block",
            "name": "Coil block",
            "category": "switching.contactor",
            "ref_prefix": "K",
            "block": "parent",
            "width": 28,
            "height": 28,
            "pins": [
                {"id": "A1", "x": 0, "y": 14, "side": "left"},
                {"id": "A2", "x": 28, "y": 14, "side": "right"},
            ],
            "draw": [{"circle": [14, 14, 8]}, {"line": [0, 14, 6, 14]}, {"line": [22, 14, 28, 14]}],
            "attributes": [
                {"tag": "TAG1", "x": 4, "y": -3, "h": 2.4},
                {"tag": "DESC1", "x": 4, "y": 32, "h": 2.2},
                {"tag": "MFG", "x": 4, "y": 36, "h": 2.0},
            ],
            "footprint_draw": [{"rect": [2, 2, 40, 50]}],
            "footprint_attributes": [{"tag": "P_TAG1", "x": 6, "y": 8, "h": 2.4}],
            "panel": {"w": 45, "h": 58, "d": 57, "din": True, "in_panel": True},
        }
    )
    raw = yaml.safe_load(Path(saved["path"]).read_text(encoding="utf-8"))
    device_raw = raw["devices"][0]
    assert device_raw["block"] == "parent"
    assert device_raw["schematic"]["attributes"][0]["tag"] == "TAG1"
    assert device_raw["panel"]["draw"][0]["rect"] == [2, 2, 40, 50]
    assert device_raw["panel"]["attributes"][0]["tag"] == "P_TAG1"

    again = Session.open(tmp_path / "job.pcad.yaml")
    device = again.catalog.get(saved["library_id"])
    assert device.block_kind == "parent"
    assert device.attributes[1].tag == "DESC1"
    assert len(device.footprint_primitives) == 1
    comp = again.place(saved["library_id"], ref="K1", x=40, y=40, properties={"description": "Run coil", "manufacturer": "ABB"})
    assert attribute_value("TAG1", device, comp) == "K1"
    assert attribute_value("DESC1", device, comp) == "Run coil"
    assert attribute_value("MFG", device, comp) == "ABB"
    assert attribute_value("P_TAG1", device, comp) == "K1"
    svg = again.export_svg(tmp_path / "block.svg").read_text(encoding="utf-8")
    assert "K1" in svg
    assert "Run coil" in svg
    assert "ABB" in svg


def test_child_and_terminal_required_attributes():
    assert missing_attributes("child", []) == ["TAG2"]
    assert missing_attributes("child", [{"tag": "TAG2", "x": 0, "y": 0}]) == []
    assert missing_attributes("terminal", [{"tag": "DESC1", "x": 0, "y": 0}]) == ["TAGSTRIP"]
    assert missing_attributes("footprint", [], [{"tag": "P_TAG1", "x": 1, "y": 1}]) == []
    assert missing_attributes("parent", [{"tag": "TAG1", "x": 0, "y": -3}]) == []


def test_symbol_builder_switches_to_the_footprint_block():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.library_editor import SymbolBuilder

    app = QApplication.instance() or QApplication([])
    dlg = SymbolBuilder(Session.create("Blocks"))
    dlg.part_id.setText("k_block")
    dlg.canvas.model.add_line(0, 14, 28, 14)
    dlg.view.setCurrentIndex(dlg.view.findData("footprint"))
    dlg.canvas.model.add_rect(1, 1, 20, 30)
    dlg.canvas.pending_attr = "P_TAG1"
    dlg.canvas._place_attribute(4, 6)
    spec = dlg._spec()
    assert spec["draw"][0]["line"] == [0, 14, 28, 14]
    assert spec["footprint_draw"][0]["rect"] == [1, 1, 20, 30]
    assert spec["footprint_attributes"][0]["tag"] == "P_TAG1"
    assert any(a["tag"] == "TAG1" for a in spec["attributes"])
    dlg.kind.setCurrentIndex(dlg.kind.findData("child"))
    labels = [btn.text() for btn in dlg._attr_buttons]
    assert "TAG2" in labels
    assert "TAG1" not in labels
    dlg.close()
    app.processEvents()
