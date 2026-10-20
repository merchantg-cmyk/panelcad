from pathlib import Path

from panelcad import Session
from panelcad.examples.dol import build_dol
from panelcad.io.exchange import sniff


def test_package_roundtrip(tmp_path: Path):
    s = build_dol()
    pack = tmp_path / "pack"
    s.export_package(pack, pdf=True)
    assert (pack / "project.pcad.yaml").is_file()
    assert (pack / "project.json").is_file()
    assert (pack / "bom.csv").is_file()
    assert (pack / "wires.csv").is_file()
    assert (pack / "sheets" / "power.yaml").is_file()
    assert (pack / "svg" / "power.svg").is_file()
    assert (pack / "dxf" / "power.dxf").is_file()
    assert (pack / "dxf" / "enclosure.dxf").is_file()
    assert sniff(pack) == "package"
    t = Session.from_any(pack)
    assert t.project.name == s.project.name
    assert [sh.id for sh in t.project.sheets] == [sh.id for sh in s.project.sheets]
    assert t.sheet("power").component("Q1").library_id == "iec.breakers.mcb_3p_32a"
    assert t.validate() == []


def test_json_roundtrip(tmp_path: Path):
    s = build_dol()
    path = tmp_path / "job.json"
    s.export_json(path)
    t = Session.from_any(path)
    assert t.project.name == s.project.name
    assert len(t.project.sheets) == 6


def test_wire_import_skips_duplicates(tmp_path: Path):
    s = build_dol()
    path = tmp_path / "wires.csv"
    s.export_wire_list(path)
    n = len(s.sheet("power").wires)
    result = s.import_wires(path)
    assert len(s.sheet("power").wires) == n
    assert result["added"] == []


def test_import_sheet_and_library(tmp_path: Path):
    s = build_dol()
    pack = tmp_path / "pack"
    s.export_package(pack, pdf=False)
    blank = Session.create("Blank")
    sid = blank.import_sheet(pack / "sheets" / "io.yaml")
    assert sid == "io"
    assert blank.sheet("io").component("U1").library_id == "adc.durapulse.gs21_20p5"
    libs = list((pack / "library").glob("*.yaml"))
    assert libs
    ids = blank.import_library(libs[0])
    assert ids
