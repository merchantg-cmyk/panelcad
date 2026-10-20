"""Vendor-native strip export: library mapping, XML structure, WMC CSV."""

from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
from typer.testing import CliRunner

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.cli.main import app
from panelcad.io.vendor_strip.xmlcheck import (
    PHOENIX_NS,
    WAGO_NS,
    validate_phoenix_xml,
    validate_wago_xml,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "vendor_strip"


def _wired_strip() -> Session:
    session = Session.create("Strip drawing")
    session.place("iec.terminals.x_2p5", ref="X1-3", x=40, y=20)
    session.place("iec.terminals.x_2p5", ref="X1-1", x=40, y=80)
    session.place("iec.terminals.x_2p5", ref="X1-2", x=40, y=50)
    session.wire("X1-1.A", "X1-2.A", net="INT")
    session.wire("X1-1.B", "X1-3.B", net="FIELD")
    session.number_wires(start=10)
    session.open_terminal_strip("X1")
    return session


@pytest.mark.confirm_generic_layout
def test_mapped_csv_uses_library_articles(tmp_path: Path):
    session = _wired_strip()
    phoenix = session.export_terminal_strip(tmp_path / "phoenix.csv", "X1", "phoenix")
    wago = session.export_terminal_strip(tmp_path / "wago.csv", "X1", "wago")
    wmc = session.export_terminal_strip(tmp_path / "wmc.csv", "X1", "weidmueller")
    assert phoenix["unmapped"] == 0
    assert wago["unmapped"] == 0
    assert wmc["unmapped"] == 0
    phx = (tmp_path / "phoenix.csv").read_text(encoding="utf-8")
    assert "3044076" in phx
    assert "3022218" in phx
    assert "3047028" in phx
    assert "iec.accessories.end_stop" not in phx
    wago_text = (tmp_path / "wago.csv").read_text(encoding="utf-8")
    assert "2002-1201" in wago_text
    assert "249-116" in wago_text
    wmc_text = (tmp_path / "wmc.csv").read_text(encoding="utf-8")
    assert wmc["delimiter"] == ";"
    assert "1020000000" in wmc_text
    assert "1061200000" in wmc_text
    assert wmc["confidence"] == "high"


@pytest.mark.confirm_generic_layout
def test_component_part_number_overrides_library_map(tmp_path: Path):
    session = Session.create("Override")
    session.place("iec.terminals.x_2p5", ref="X1-1", x=40, y=40)
    session.set_component_properties("X1-1", {"manufacturer": "Wago", "part_number": "2002-1204"})
    session.open_terminal_strip("X1")
    wago = session.export_terminal_strip(tmp_path / "wago.csv", "X1", "wago")
    assert wago["unmapped"] == 0
    body = (tmp_path / "wago.csv").read_text(encoding="utf-8")
    assert "2002-1204" in body


@pytest.mark.confirm_generic_layout
def test_unmapped_article_is_warned(tmp_path: Path):
    session = Session.create("Custom")
    session.place("nema.terminals.pe", ref="X1-1", x=40, y=40)
    session.open_terminal_strip("X1")
    result = session.export_terminal_strip(tmp_path / "phoenix.csv", "X1", "phoenix")
    assert result["unmapped"] >= 1
    refs = {item["ref"] for item in result["warnings"]}
    assert "X1-1" in refs
    text = (tmp_path / "phoenix.csv").read_text(encoding="utf-8")
    assert "nema.terminals.pe" in text


@pytest.mark.confirm_generic_layout
def test_phoenix_clipx_xml_structure(tmp_path: Path):
    session = _wired_strip()
    result = session.export_terminal_strip(tmp_path / "x1.xml", "X1", "clipx")
    assert result["format"] == "xml"
    assert result["confidence"] == "unconfirmed"
    assert "clipx ENGINEER" in result["product"]
    path = tmp_path / "x1.xml"
    assert validate_phoenix_xml(path) == []
    root = ET.parse(path).getroot()
    items = list(root.findall(f"{{{PHOENIX_NS}}}TerminalStrip/{{{PHOENIX_NS}}}Item"))
    assert len(items) == result["rows"]
    articles = [item.findtext(f"{{{PHOENIX_NS}}}Article") for item in items]
    assert "3044076" in articles
    assert "3022218" in articles
    assert "3047028" in articles
    kinds = [item.findtext(f"{{{PHOENIX_NS}}}Kind") for item in items]
    assert kinds[0] == "end_stop"
    assert "terminal" in kinds
    wires = [item.findtext(f"{{{PHOENIX_NS}}}InternalWire") or "" for item in items]
    assert any("10" in text for text in wires)


@pytest.mark.confirm_generic_layout
def test_wago_xml_from_suffix_and_vendor(tmp_path: Path):
    session = _wired_strip()
    by_suffix = session.export_terminal_strip(tmp_path / "rail.xml", "X1", "wago")
    assert by_suffix["format"] == "xml"
    assert by_suffix["confidence"] == "unconfirmed"
    assert validate_wago_xml(tmp_path / "rail.xml") == []
    by_alias = session.export_terminal_strip(tmp_path / "alias.xml", "X1", "wagoxml")
    assert by_alias["format"] == "xml"
    root = ET.parse(tmp_path / "alias.xml").getroot()
    assert root.tag == f"{{{WAGO_NS}}}WagoSmartDesignerProject"
    articles = [node.text for node in root.findall(f".//{{{WAGO_NS}}}Article")]
    assert "2002-1201" in articles


@pytest.mark.confirm_generic_layout
def test_weidmueller_rejects_xml(tmp_path: Path):
    session = _wired_strip()
    with pytest.raises(ValueError, match="no published XML"):
        session.export_terminal_strip(tmp_path / "wmc.xml", "X1", "weidmueller", file_format="xml")


@pytest.mark.confirm_generic_layout
def test_agent_and_cli_export_xml(tmp_path: Path):
    job = tmp_path / "job.pcad.yaml"
    session = _wired_strip()
    session.save(job)
    agent = Agent()
    opened = agent.handle({"id": 1, "op": "open", "path": str(job)})
    assert opened["ok"]
    dest = tmp_path / "agent.xml"
    exported = agent.handle(
        {
            "id": 2,
            "op": "export_terminal_strip",
            "path": str(dest),
            "strip": "X1",
            "vendor": "clipx",
        }
    )
    assert exported["ok"]
    assert exported["result"]["format"] == "xml"
    assert dest.is_file()
    help_result = agent.handle({"id": 3, "op": "help"})
    assert "clipx" in help_result["result"]["help"]["export_terminal_strip"]
    runner = CliRunner()
    cli_out = tmp_path / "cli.xml"
    ran = runner.invoke(
        app,
        [
            "electrical",
            "strip-export",
            str(job),
            "--strip",
            "X1",
            "--vendor",
            "wagoxml",
            "-o",
            str(cli_out),
        ],
    )
    assert ran.exit_code == 0, ran.output
    assert validate_wago_xml(cli_out) == []


def test_committed_samples_match_structure():
    assert validate_phoenix_xml(EXAMPLES / "phoenix_clipx.xml") == []
    assert validate_wago_xml(EXAMPLES / "wago_smartdesigner.xml") == []
    phoenix_csv = (EXAMPLES / "phoenix_project_complete.csv").read_text(encoding="utf-8").splitlines()
    assert phoenix_csv[0] == "Strip,Position,Tag,Article,Internal,External,Jumper,Accessory"
    assert "3044076" in "\n".join(phoenix_csv)
    assert "3047028" in "\n".join(phoenix_csv)
    wago_csv = (EXAMPLES / "wago_smartdesigner.csv").read_text(encoding="utf-8")
    assert "2002-1201" in wago_csv
    wmc = (EXAMPLES / "weidmueller_wmc.csv").read_text(encoding="utf-8").splitlines()
    assert wmc[0].count(";") == 2
    assert "1020000000" in "\n".join(wmc)
    for name in ("phoenix_clipx.xsd", "wago_smartdesigner.xsd"):
        tree = ET.parse(EXAMPLES / name)
        assert tree.getroot().tag.endswith("schema")


def test_xsd_files_are_well_formed():
    for name in ("phoenix_clipx.xsd", "wago_smartdesigner.xsd"):
        root = ET.parse(EXAMPLES / name).getroot()
        assert "schema" in root.tag
