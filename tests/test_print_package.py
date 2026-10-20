from pathlib import Path

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.examples.dol import build_dol
from panelcad.render.print_package import (
    extract_pdf_text,
    plan_print_package,
    render_package_page_png,
    wrap_lines,
)


def _qt_app():
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])


def _pdf_pages(path: Path) -> int:
    from PySide6.QtPdf import QPdfDocument

    _qt_app()
    doc = QPdfDocument()
    assert doc.load(str(path)) == QPdfDocument.Error.None_
    return doc.pageCount()


def _outline_titles(path: Path) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))

    def walk(items) -> list[str]:
        out: list[str] = []
        for item in items or []:
            if isinstance(item, list):
                out.extend(walk(item))
            else:
                title = getattr(item, "title", None)
                out.append(str(title if title is not None else item))
        return out

    return walk(reader.outline)


def _long_job() -> Session:
    s = Session.create("Long Reports")
    s.set_title_block(
        company="Shop",
        customer="Plant",
        location="MCC-2",
        drawing_number="E-LONG",
        rev="A",
        date="2026-10-07",
        rev_description="Issue",
    )
    for i in range(80):
        placed = s.place("iec.terminals.x_2p5", ref=f"X{i + 1}", x=20.0, y=20.0 + (i % 20) * 8.0)
        s.set_component_properties(placed.id, {"part_number": f"2002-{i:04d}"})
    for i in range(0, 70, 2):
        s.wire(f"X{i + 1}.A", f"X{i + 2}.A")
    return s


def test_plan_dol_section_order_and_sheet_numbers():
    s = build_dol()
    plan = s.plan_print_package()
    assert plan["sections"][0] == "cover"
    assert "index" in plan["sections"]
    assert "revisions" in plan["sections"]
    assert "drawings" in plan["sections"]
    assert plan["sections"][-7:] == ["bom", "wires", "from_to", "xref", "plc_io", "checks", "thermal"]
    assert "terminals" not in plan["sections"]
    assert plan["drawing_count"] == 6
    numbers = [row["number"] for row in plan["sheets"]]
    assert numbers == [1, 2, 3, 4, 5, 6]
    assert [row["id"] for row in plan["sheets"]] == [sh.id for sh in s.project.sheets]
    first = plan["sheets"][0]
    assert first["id"] == "power"
    assert first["title"] == "Power"
    assert first["count"] == 6
    order = plan["order"]
    assert order[:4] == ["cover", "index", "revisions", "nameplate"]
    assert order.count("drawings") == 6
    index_titles = [row["title"] for row in plan["index"]]
    assert index_titles[0] == "Nameplate"
    assert index_titles[1:7] == [sh.title or sh.id for sh in s.project.sheets]
    assert "Bill of Materials" in index_titles
    assert "Wire List" in index_titles
    assert "Wire List (From-To)" in index_titles
    assert "Device Index" in index_titles
    assert "PLC I/O List" in index_titles
    assert "Design Checks" in index_titles
    assert "Terminal Strips" not in index_titles
    for row in plan["index"]:
        assert int(row["page"]) >= 1
        assert int(row["page"]) <= plan["pages"]
    power_idx = next(row for row in plan["index"] if row["title"] == "Power")
    assert power_idx["page"] == plan["sheets"][0]["page"]
    bom_idx = next(row for row in plan["index"] if row["title"] == "Bill of Materials")
    assert bom_idx["page"] == order.index("bom") + 1


def test_dol_print_package_pages_bookmarks_and_text(tmp_path: Path):
    _qt_app()
    s = build_dol()
    dest = tmp_path / "dol_package.pdf"
    first = s.print_package(dest)
    second = s.print_package(tmp_path / "dol_package_b.pdf")
    assert dest.is_file()
    assert dest.stat().st_size > 2000
    assert first["pages"] == second["pages"]
    assert first["order"] == second["order"]
    assert first["pages"] == _pdf_pages(dest)
    assert first["pages"] > 6
    titles = [item["title"] for item in first["bookmarks"]]
    assert titles[:4] == ["Cover", "Drawing Index", "Revision History", "Nameplate"]
    assert "Drawings" in titles
    assert "Bill of Materials" in titles
    assert "Wire List" in titles
    assert "Wire List (From-To)" in titles
    assert "Device Index" in titles
    assert "PLC I/O List" in titles
    assert "Terminal Strips" not in titles
    assert "Design Checks" in titles
    drawing = next(item for item in first["bookmarks"] if item["title"] == "Drawings")
    child_titles = [child["title"] for child in drawing["children"]]
    assert child_titles == [sh.title or sh.id for sh in s.project.sheets]
    pdf_titles = _outline_titles(dest)
    assert "Cover" in pdf_titles
    assert "Power" in pdf_titles
    assert "Bill of Materials" in pdf_titles
    cover = extract_pdf_text(dest, 0)
    index = extract_pdf_text(dest, 1)
    blob = f"{cover}\n{index}"
    assert "DOL Motor Starter" in blob
    assert "Riverside Pump Station" in blob
    assert "E-001" in blob
    assert "Nameplate" in index
    assert "Power" in index
    assert "Control" in index
    assert "Enclosure" in index
    assert "PAGE" in index
    assert "Bill of Materials" in index
    assert "Wire List" in index
    assert "PLC I/O List" in index
    assert "Design Checks" in index
    assert "Terminal Strips" not in index
    power_page = first["sheets"][0]["page"] - 1
    power_text = extract_pdf_text(dest, power_page)
    assert "1 / 6" in power_text or "1 /6" in power_text
    bom_page = first["order"].index("bom")
    bom_text = extract_pdf_text(dest, bom_page)
    assert "BILL OF MATERIALS" in bom_text or "Bill of Materials" in bom_text
    assert "Q1" in bom_text or "ITEM" in bom_text
    assert "CLICK 8-pt DC sinking output" in bom_text
    assert "DURApulse GS20 0.5 hp 230V" in bom_text
    assert "MT-32" in bom_text or "Metasol" in bom_text
    assert "…" not in bom_text
    assert "CATALOG" not in bom_text


def test_drawings_only_keeps_sheet_numbering(tmp_path: Path):
    _qt_app()
    s = build_dol()
    dest = tmp_path / "drawings.pdf"
    result = s.print_package(dest, sections=["drawings"])
    assert result["pages"] == 6
    assert result["sheets"][0]["number"] == 1
    assert result["sheets"][0]["count"] == 6
    assert result["sheets"][-1]["id"] == "serial"
    text = extract_pdf_text(dest, 0)
    assert "1 / 6" in text or "1 /6" in text


def test_long_bom_and_wire_list_paginate(tmp_path: Path):
    _qt_app()
    s = _long_job()
    plan = plan_print_package(s)
    bom_pages = [page for page in plan.pages if page.section == "bom"]
    wire_pages = [page for page in plan.pages if page.section == "wires"]
    assert len(bom_pages) >= 3
    assert bom_pages[0].row_count > 0
    assert bom_pages[0].row_start == 0
    assert bom_pages[1].row_start == bom_pages[0].row_count
    assert sum(page.row_count for page in bom_pages) == 80
    assert len(wire_pages) >= 2
    dest = tmp_path / "long.pdf"
    result = s.print_package(dest, sections=["bom", "wires"])
    assert result["pages"] == len(bom_pages) + len(wire_pages)
    assert _pdf_pages(dest) == result["pages"]
    header = extract_pdf_text(dest, 0)
    assert "ITEM" in header or "QTY" in header or "BILL OF MATERIALS" in header


def test_single_paper_size_letter(tmp_path: Path):
    _qt_app()
    s = build_dol()
    dest = tmp_path / "letter.pdf"
    result = s.print_package(dest, paper="letter", sections=["cover", "drawings"])
    assert result["paper"] == "letter"
    assert result["pages"] == 7
    from PySide6.QtPdf import QPdfDocument

    doc = QPdfDocument()
    assert doc.load(str(dest)) == QPdfDocument.Error.None_
    size = doc.pagePointSize(0)
    assert abs(float(size.width()) - 612.0) < 6.0
    assert abs(float(size.height()) - 792.0) < 6.0


def test_session_and_export_package_use_print_pdf(tmp_path: Path):
    _qt_app()
    s = build_dol()
    pack = tmp_path / "pack"
    s.export_package(pack, pdf=True)
    pdf = pack / "print.pdf"
    assert pdf.is_file()
    assert _pdf_pages(pdf) == s.plan_print_package()["pages"]
    assert (pack / "checks.csv").is_file()
    assert (pack / "audit.csv").is_file()


def test_cli_package_pdf(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    _qt_app()
    src = tmp_path / "job.pcad.yaml"
    build_dol().save(src)
    dest = tmp_path / "out.pdf"
    result = CliRunner().invoke(app, ["package", str(src), "--pdf", str(dest)])
    assert result.exit_code == 0, result.output
    assert dest.is_file()
    assert "pages=" in result.output
    assert _pdf_pages(dest) > 6


def test_agent_print_package(tmp_path: Path):
    _qt_app()
    src = tmp_path / "job.pcad.yaml"
    build_dol().save(src)
    dest = tmp_path / "agent.pdf"
    agent = Agent()
    opened = agent.handle({"id": 1, "op": "open", "path": str(src)})
    assert opened["ok"], opened
    help_r = agent.handle({"id": 2, "op": "help"})
    assert "print_package" in help_r["result"]["ops"]
    assert "print_package" in help_r["result"]["help"]
    ran = agent.handle({"id": 3, "op": "print_package", "path": str(dest), "paper": "mixed"})
    assert ran["ok"], ran
    assert ran["result"]["pages"] > 6
    assert Path(ran["result"]["path"]).is_file()


def test_gui_print_package_menu_and_dialog():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow
    from panelcad.gui.package_dialog import PrintPackageDialog

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Session.create("Menu"))
    window.show()
    app.processEvents()
    titles: list[str] = []
    for action in window.menuBar().actions():
        menu = action.menu()
        if menu and menu.title().replace("&", "") == "File":
            titles = [item.text().replace("&", "") for item in menu.actions()]
    assert "Print Package…" in titles
    dialog = PrintPackageDialog(suggested="job_package.pdf")
    vals = dialog.values()
    assert vals["paper"] == "mixed"
    assert vals["sections"][:5] == ["cover", "index", "revisions", "nameplate", "drawings"]
    assert "bom" in vals["sections"]
    assert str(vals["path"]).endswith("job_package.pdf")
    dialog.close()
    window.close()


def test_wrap_lines_never_ellipsizes():
    long = "CLICK 8-pt DC sinking output module extra words keep wrapping onto more lines"
    lines = wrap_lines(long, 28.0, 2.15)
    assert len(lines) >= 3
    assert all("…" not in line for line in lines)
    assert "wrapping" in " ".join(lines)


def test_empty_terminals_skipped_keep_placeholder_off():
    s = build_dol()
    assert s.terminal_strip_rows() == []
    plan = plan_print_package(s)
    assert all(page.section != "terminals" for page in plan.pages)
    only = plan_print_package(s, sections=["terminals"])
    assert only.pages == []
    assert only.sections == []


def test_package_pages_use_white_paper(tmp_path: Path):
    from PySide6.QtGui import QImage

    from panelcad.render.page import PAPER, PRINT_PAPER

    _qt_app()
    s = build_dol()
    dest = tmp_path / "white.pdf"
    result = s.print_package(dest)
    cover = tmp_path / "cover.png"
    drawing = tmp_path / "power.png"
    render_package_page_png(dest, 0, cover, width_px=600)
    render_package_page_png(dest, result["sheets"][0]["page"] - 1, drawing, width_px=600)
    cream = (PAPER.red(), PAPER.green(), PAPER.blue())
    white = (PRINT_PAPER.red(), PRINT_PAPER.green(), PRINT_PAPER.blue())
    for path in (cover, drawing):
        img = QImage(str(path))
        assert not img.isNull()
        sample = img.pixelColor(8, 8)
        rgb = (sample.red(), sample.green(), sample.blue())
        assert rgb != cream
        assert all(channel >= 250 for channel in rgb)
        assert rgb == white or min(rgb) >= 250


def test_print_package_without_pypdf_still_writes_pdf(tmp_path: Path, caplog):
    import logging
    import sys

    _qt_app()
    s = build_dol()
    dest = tmp_path / "no_outline.pdf"

    class _BlockPypdf:
        def find_spec(self, name, path, target=None):  # noqa: ARG002
            if name == "pypdf" or name.startswith("pypdf."):
                raise ModuleNotFoundError("No module named 'pypdf'")
            return None

    blocker = _BlockPypdf()
    saved = {key: sys.modules.pop(key) for key in list(sys.modules) if key == "pypdf" or key.startswith("pypdf.")}
    sys.meta_path.insert(0, blocker)
    try:
        caplog.set_level(logging.WARNING)
        result = s.print_package(dest, sections=["cover", "index", "drawings"])
        assert dest.is_file()
        assert dest.stat().st_size > 2000
        assert result["pages"] == _pdf_pages(dest)
        assert result.get("outlines") is False
        assert any("pypdf" in record.getMessage().lower() for record in caplog.records)
    finally:
        if blocker in sys.meta_path:
            sys.meta_path.remove(blocker)
        sys.modules.update(saved)

    assert dest.is_file()
    assert _outline_titles(dest) == []
    assert "DOL Motor Starter" in extract_pdf_text(dest, 0)


def test_wrapped_bom_paginates_long_description(tmp_path: Path):
    _qt_app()
    s = Session.create("Wrap BOM")
    placed = s.place("iec.terminals.x_2p5", ref="X1", x=20.0, y=20.0)
    s.set_component_properties(placed.id, {"part_number": "2002-1201", "catalog": "2002-1201"})
    device = s.catalog.get("iec.terminals.x_2p5")
    device.name = (
        "WAGO 2002-1201 2-conductor through terminal block with an unusually long "
        "catalog description that must wrap onto several lines instead of truncating"
    )
    dest = tmp_path / "wrap.pdf"
    result = s.print_package(dest, sections=["bom"])
    assert result["pages"] >= 1
    text = extract_pdf_text(dest, 0)
    assert "2-conductor" in text
    assert "catalog description" in text
    assert "truncating" in text
    assert "…" not in text
    assert "CATALOG" not in text
