from pathlib import Path

from panelcad.examples.dol import build_dol


def test_dol_svg_and_csv(tmp_path: Path):
    s = build_dol()
    errors = s.validate()
    assert errors == [], errors
    svg_path = tmp_path / "dol.svg"
    s.export_svg(svg_path)
    svg = svg_path.read_text(encoding="utf-8")
    assert "<svg" in svg
    assert "Q1" in svg
    assert "K1" in svg
    assert "COMPANY" in svg
    assert "Acme Process Controls" in svg
    assert "E-001" in svg
    assert "SHEET" in svg
    assert "REV" in svg
    assert "DRAWN" in svg
    assert "Power" in svg
    s.set_active_sheet("power")
    s.set_wire_label(frm="Q1.2", to="K1.L1", label="L1")
    svg2 = s.export_svg(tmp_path / "dol_l1.svg").read_text(encoding="utf-8")
    assert "L1" in svg2
    assert len(s.project.sheets) >= 2
    bom = tmp_path / "bom.csv"
    s.export_bom(bom)
    text = bom.read_text(encoding="utf-8")
    assert "Q1" in text
    assert "iec.contactors.ac3_9a" in text
    wires = tmp_path / "wires.csv"
    s.export_wire_list(wires)
    assert "K1.L1" in wires.read_text(encoding="utf-8")
