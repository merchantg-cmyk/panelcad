"""Customer drawing revisions: snapshots, title block, clouds, compare, GUI, agent, CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QMenu

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.revisions import bump_letters, next_rev_token
from panelcad.gui.canvas import RevisionCloudItem, SchematicView
from panelcad.gui.main_window import MainWindow
from panelcad.gui.revision_dialog import (
    CompareRevisionsDialog,
    IssueRevisionDialog,
    RevisionHistoryDialog,
)
from panelcad.io.yaml_format import dump_project, load_project


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _row(report: dict, **want: str) -> dict:
    for row in report["rows"]:
        if all(str(row.get(key) or "") == value for key, value in want.items()):
            return row
    raise AssertionError(f"no row matching {want}")


def customer_job() -> Session:
    """Two issued revs with device, wire, terminal, BOM, and layout changes."""

    s = Session.create("Harbor MCC", sheet_id="main")
    s.set_title_block(
        company="Northwind Controls",
        customer="Harbor Plant",
        drawing_number="E-100",
        standard="IEC",
    )
    sch = "main"
    s.place(
        "iec.breakers.mcb_3p_32a",
        ref="Q1",
        x=40,
        y=40,
        sheet_id=sch,
        properties={"part_number": "SAMPLE-MCB-16A", "In": "16A"},
    )
    s.place("iec.contactors.coil", ref="K1", x=120, y=40, sheet_id=sch)
    s.place("iec.operators.pilot_light", ref="H1", x=200, y=40, sheet_id=sch)
    s.wire("K1.A2", "H1.X1", sheet_id=sch, number="101")
    s.wire("K1.A1", "H1.X2", sheet_id=sch, number="102")
    s.place("iec.terminals.x_2p5", ref="X1-1", x=40, y=150, sheet_id=sch)
    s.place("iec.operators.pb_no", ref="PB1", x=300, y=140, sheet_id=sch)
    s.place("iec.operators.pb_no", ref="S2", x=300, y=200, sheet_id=sch)
    panel = s.add_sheet("panel", title="Panel", kind="panel_layout")
    s.place("iec.contactors.coil", ref="K9", x=60, y=80, sheet_id=panel.id, snap=False)

    s.issue_revision(
        description="Issued for construction",
        date="2026-10-01",
        drawn="GH",
        checked="AK",
        approved="JS",
        auto_clouds=False,
    )
    s.set_component_properties("Q1", {"part_number": "SAMPLE-MCB-32A", "In": "32A"}, sheet_id=sch)
    s.set_wire_spec(frm="K1.A2", to="H1.X1", sheet_id=sch, number="202")
    gone = s.find_wire(frm="K1.A1", to="H1.X2", sheet_id=sch)
    s.delete_wire(gone.id, sheet_id=sch)
    s.set_component_properties("X1-1", {"terminal": "4", "jumper": "1-2"}, sheet_id=sch)
    s.sheet(sch).component("PB1").id = "PB2"
    s.delete_component("S2", sheet_id=sch)
    s.place("iec.operators.pilot_light", ref="H9", x=300, y=40, sheet_id=sch)
    s.place("iec.breakers.mcb_3p_32a", ref="Q2", x=84, y=40, sheet_id=sch)
    s.wire("K1.A1", "H9.X1", sheet_id=sch, number="303")
    s.move("K9", 180, 90, sheet_id=panel.id, snap=False)
    s.issue_revision(
        description="Added run light; Q1 32A",
        date="2026-10-08",
        drawn="GH",
        checked="AK",
        approved="JS",
    )
    return s


def test_bump_letter_and_number_tokens():
    assert bump_letters("Z") == "AA"
    assert bump_letters("AZ") == "BA"
    assert next_rev_token([], "letter") == "A"
    assert next_rev_token(["A", "B"], "letter") == "C"
    assert next_rev_token(["C", "A"], "letter") == "D"
    assert next_rev_token(["Z"], "letter") == "AA"
    assert next_rev_token([], "number") == "0"
    assert next_rev_token(["0", "2"], "number") == "3"


def test_issue_appends_the_next_letter_after_a_typed_rev():
    letters = Session.create("Letters")
    letters.set_title_block(rev="A", date="2026-10-16", rev_description="Typed in the inspector")
    from panelcad.core.revisions import preview_next_rev

    assert preview_next_rev(letters.project) == "B"
    issued = letters.issue_revision(
        description="Issued for construction",
        date="2026-10-17",
        drawn="GH",
        checked="AK",
        approved="JS",
        auto_clouds=False,
    )
    assert issued["rev"] == "B"
    rows = {row.rev: row for row in letters.project.title_block.revisions}
    assert rows["A"].date == "2026-10-16"
    assert rows["A"].description == "Typed in the inspector"
    assert rows["B"].description == "Issued for construction"
    assert rows["B"].drawn == "GH"
    listed = letters.list_revisions()
    assert [row["rev"] for row in listed["revisions"]] == ["A", "B"]
    assert listed["revisions"][0]["description"] == "Typed in the inspector"
    assert listed["current"] == "B"
    with pytest.raises(ValueError, match="already on the title block"):
        letters.issue_revision(rev="A", description="again")

    numbers = Session.create("Numbers")
    numbers.set_title_block(rev="A")
    first = numbers.issue_revision(
        scheme="number",
        description="Numeric zero",
        date="2026-10-01",
        auto_clouds=False,
    )
    second = numbers.issue_revision(description="Numeric one", date="2026-10-02", auto_clouds=False)
    assert first["rev"] == "0"
    assert second["rev"] == "1"
    assert numbers.list_revisions()["current"] == "1"
    assert numbers.project.title_block.revisions[0].rev == "1"


def test_duplicate_rev_and_tiny_cloud_do_not_push_undo():
    s = Session.create("Guards")
    s.issue_revision(rev="A", description="Issued for construction", date="2026-10-01", auto_clouds=False)
    assert s.can_undo()
    with pytest.raises(ValueError, match="already issued"):
        s.issue_revision(rev="A", description="again", date="2026-10-02")
    assert [row.rev for row in s.project.revision_control.issued] == ["A"]
    s.undo()
    assert s.project.revision_control is None or not s.project.revision_control.issued

    bare = Session.create("Tiny")
    assert not bare.can_undo()
    with pytest.raises(ValueError, match="3 mm"):
        bare.add_revision_cloud(0, 0, 1, 1)
    assert not bare.can_undo()


@pytest.mark.confirm_generic_layout
def test_yaml_omits_unused_store_and_snapshots_do_not_nest(tmp_path: Path):
    fresh = Session.create("Empty")
    assert "revision_control" not in dump_project(fresh.project)
    dol = load_project(Path(__file__).resolve().parents[1] / "examples/dol_starter.pcad.yaml")
    assert "revision_control" not in dump_project(dol)

    s = customer_job()
    for row in s.project.revision_control.issued:
        control = row.snapshot.get("revision_control") or {}
        assert "issued" not in control
    text = dump_project(s.project)
    assert "revision_control" in text
    assert "Issued for construction" in text
    path = tmp_path / "job.pcad.yaml"
    path.write_text(text, encoding="utf-8")
    loaded = load_project(path)
    assert [row.rev for row in loaded.revision_control.issued] == ["A", "B"]
    assert loaded.revision_control.issued[0].snapshot["title_block"]["revisions"][0]["rev"] == "A"


@pytest.mark.confirm_generic_layout
def test_compare_devices_wires_terminals_bom_and_layout():
    s = customer_job()
    report = s.compare_revisions("A", "B")
    counts = report["counts"]
    assert counts["devices_added"] >= 2
    assert counts["devices_removed"] >= 1
    assert counts["devices_changed"] >= 1
    assert counts["wires_added"] >= 1
    assert counts["wires_removed"] >= 1
    assert counts["wires_renumbered"] >= 1
    assert counts["terminals_changed"] >= 1
    assert counts["layout"] >= 1
    assert counts["bom"] >= 1

    _row(report, section="device", change="added", tag="H9")
    _row(report, section="device", change="added", tag="Q2")
    _row(report, section="device", change="removed", tag="S2")
    renamed = _row(report, section="device", change="changed", tag="PB2", field="tag")
    assert renamed["before"] == "PB1"
    part = _row(report, section="device", change="changed", tag="Q1", field="part_number")
    assert part["before"] == "SAMPLE-MCB-16A"
    assert part["after"] == "SAMPLE-MCB-32A"
    rating = _row(report, section="device", change="changed", tag="Q1", field="rating")
    assert "16A" in rating["before"]
    assert "32A" in rating["after"]
    renumbered = _row(report, section="wire", change="renumbered", field="number")
    assert renumbered["before"] == "101"
    assert renumbered["after"] == "202"
    removed = _row(report, section="wire", change="removed")
    assert removed["before"] == "102"
    _row(report, section="wire", change="added")
    level = _row(report, section="terminal", change="changed", tag="X1-1", field="level")
    assert level["before"] == "1"
    assert level["after"] == "4"
    jumper = _row(report, section="terminal", change="changed", tag="X1-1", field="jumper")
    assert jumper["after"] == "1-2"
    moved = _row(report, section="layout", change="moved", tag="K9")
    assert moved["before"].startswith("60")
    assert moved["after"].startswith("180")
    pilot = _row(report, section="bom", change="qty", detail="iec.operators.pilot_light")
    assert int(pilot["before"]) < int(pilot["after"])
    bom_part = _row(report, section="bom", change="part", tag="Q1", field="part_number")
    assert bom_part["before"] == "SAMPLE-MCB-16A"
    assert bom_part["after"] == "SAMPLE-MCB-32A"
    from panelcad.render.revision_report import display_item

    removed_bom = _row(report, section="bom", change="qty", detail="SAMPLE-MCB-16A")
    assert removed_bom["before"] == "1"
    assert removed_bom["after"] == "0"
    removed_item = display_item(removed_bom)
    assert removed_item.startswith("MCB 3P 16A")
    assert "In=16A" in removed_item
    assert "32A" not in removed_item
    assert "iec.breakers.mcb_3p_32a" in removed_item
    added_bom = _row(report, section="bom", change="qty", detail="SAMPLE-MCB-32A")
    assert added_bom["before"] == "0"
    assert added_bom["after"] == "1"
    added_item = display_item(added_bom)
    assert added_item.startswith("MCB 3P 32A")
    assert "In=32A" in added_item

    alias = s.compare_revisions("A", "working")
    assert alias["counts"] == report["counts"]
    assert s.project.title_block.revisions[0].rev == "B"
    assert s.project.title_block.revisions[1].rev == "A"
    assert s.list_revisions()["order"] == "oldest_first"
    assert [row["rev"] for row in s.list_revisions()["revisions"]] == ["A", "B"]


def test_auto_clouds_merge_when_close_and_keep_a_far_cloud():
    s = Session.create("Clouds", sheet_id="main")
    s.issue_revision(description="Issued for construction", date="2026-10-01", auto_clouds=False)
    s.place("iec.breakers.mcb_3p_32a", ref="Q1", x=40, y=40)
    s.place("iec.breakers.mcb_3p_32a", ref="Q2", x=84, y=40)
    s.place("iec.breakers.mcb_3p_32a", ref="Q9", x=300, y=40)
    issued = s.issue_revision(description="Added breakers", date="2026-10-08", drawn="GH")
    clouds = [c for c in s.project.revision_control.clouds if c.sheet == "main"]
    assert len(clouds) == 2
    assert all(c.rev == issued["rev"] == "B" for c in clouds)

    def covers(cloud, x: float, y: float) -> bool:
        return cloud.x - 0.2 <= x <= cloud.x + cloud.w + 0.2 and cloud.y - 0.2 <= y <= cloud.y + cloud.h + 0.2

    close = [c for c in clouds if covers(c, 58, 67.5) and covers(c, 102, 67.5)]
    far = [c for c in clouds if covers(c, 318, 67.5)]
    assert len(close) == 1
    assert len(far) == 1
    assert close[0].id != far[0].id
    assert not close[0].manual


def test_clear_on_issue_drops_old_clouds_and_stamps_blank_manual_ones():
    s = Session.create("Clear", sheet_id="main")
    s.issue_revision(rev="A", description="Issued for construction", date="2026-10-01", auto_clouds=False)
    s.add_revision_cloud(20, 30, 40, 18, rev="A")
    s.add_revision_cloud(90, 30, 36, 18)
    s.place("iec.operators.pilot_light", ref="H9", x=200, y=40)
    issued = s.issue_revision(
        description="Added run light",
        date="2026-10-08",
        clear_clouds=True,
    )
    clouds = s.project.revision_control.clouds
    assert clouds
    assert all(c.rev == issued["rev"] for c in clouds)
    assert not any(abs(c.x - 20) < 0.5 for c in clouds)
    assert any(c.manual and abs(c.x - 90) < 0.5 for c in clouds)


def _red_pixels(image: QImage) -> int:
    converted = image.convertToFormat(QImage.Format.Format_ARGB32)
    raw = bytes(converted.bits())
    stride = converted.bytesPerLine()
    count = 0
    for y in range(converted.height()):
        row = y * stride
        for x in range(converted.width()):
            i = row + x * 4
            blue, green, red = raw[i], raw[i + 1], raw[i + 2]
            if red > 150 and red > green + 40 and red > blue + 40 and green < 130 and blue < 130:
                count += 1
    return count


def _render_sheet(session: Session, sheet_id: str, dpi: float = 140.0) -> QImage:
    from PySide6.QtGui import QPainter

    from panelcad.render.page import paint_sheet

    _app()
    sheet = session.sheet(sheet_id)
    width, height = sheet.page.dimensions()
    scale = dpi / 25.4
    image = QImage(max(1, int(width * scale)), max(1, int(height * scale)), QImage.Format.Format_ARGB32)
    image.fill(0xFFFFFFFF)
    painter = QPainter(image)
    painter.scale(scale, scale)
    index = session.project.sheets.index(sheet)
    paint_sheet(
        painter,
        session.project,
        sheet,
        session.catalog,
        sheet_index=index,
        sheet_count=len(session.project.sheets),
    )
    painter.end()
    return image


@pytest.mark.confirm_generic_layout
def test_clouds_and_rev_table_export_pdf_svg_dxf_and_print_package(tmp_path: Path):
    s = customer_job()
    image = _render_sheet(s, "main", dpi=130)
    assert _red_pixels(image) > 40

    svg = s.export_svg(tmp_path / "main.svg", sheet_id="main")
    assert "#b00020" in svg.read_text(encoding="utf-8")

    import ezdxf

    schematic = s.export_dxf(tmp_path / "main.dxf", sheet_id="main")
    doc = ezdxf.readfile(schematic)
    assert "REVCLOUD" in [layer.dxf.name for layer in doc.layers]
    cloud_ents = [ent for ent in doc.modelspace() if ent.dxf.layer == "REVCLOUD"]
    assert cloud_ents
    texts = [ent.dxf.text for ent in doc.modelspace() if ent.dxftype() == "TEXT"]
    assert any("Added run light" in text for text in texts)
    assert any("Issued for construction" in text for text in texts)
    assert any(text == "S2 deleted" for text in texts)

    layout = s.export_dxf(tmp_path / "panel.dxf", sheet_id="panel")
    layout_doc = ezdxf.readfile(layout)
    layout_text = [ent.dxf.text for ent in layout_doc.modelspace() if ent.dxftype() == "TEXT"]
    assert any("Added run light" in text for text in layout_text)
    assert any(text == "K9 moved" for text in layout_text)
    assert any(ent.dxf.layer == "REVCLOUD" for ent in layout_doc.modelspace())

    from pypdf import PdfReader

    pdf = s.export_pdf(tmp_path / "drawings.pdf")
    reader = PdfReader(str(pdf))
    assert len(reader.pages) >= 2
    drawing_text = []
    for page in reader.pages:
        text = " ".join((page.extract_text() or "").split())
        drawing_text.append(text)
        assert "Issued for construction" in text
        assert "Added run light" in text
    packed_drawings = " ".join(drawing_text)
    assert "S2 deleted" in packed_drawings
    assert "K9 moved" in packed_drawings
    assert "IEC" in packed_drawings

    package = tmp_path / "package.pdf"
    s.print_package(package, sections="drawings,revisions")
    from panelcad.render.print_package import extract_pdf_text

    packed = " ".join(extract_pdf_text(package).split())
    assert "Issued for construction" in packed
    assert "Added run light" in packed

    report = s.compare_revisions(
        "A",
        "B",
        csv_path=tmp_path / "compare.csv",
        pdf_path=tmp_path / "compare.pdf",
    )
    raw = Path(report["csv"]).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    header = raw.decode("utf-8-sig").splitlines()[0]
    assert header == "section,change,sheet,tag,field,before,after,detail"
    assert "H9" in raw.decode("utf-8-sig")
    compare_text = extract_pdf_text(report["pdf"])
    assert "REVISION COMPARE" in compare_text
    assert "DEVICES" in compare_text
    assert "H9" in compare_text


def test_agent_jsonl_issue_list_compare_and_cloud(tmp_path: Path):
    agent = Agent()
    created = agent.handle({"id": 1, "op": "create_project", "name": "Rev Agent"})
    assert created["ok"]
    assert agent.handle({"op": "place", "library_id": "iec.operators.pilot_light", "ref": "H1", "x": 40, "y": 40})["ok"]
    issued = agent.handle(
        {
            "op": "issue_revision",
            "description": "Issued for construction",
            "date": "2026-10-01",
            "drawn": "GH",
            "checked": "AK",
            "approved": "JS",
            "clouds": False,
        }
    )
    assert issued["ok"]
    assert issued["result"]["rev"] == "A"
    assert agent.handle({"op": "place", "library_id": "iec.operators.pilot_light", "ref": "H9", "x": 160, "y": 40})["ok"]
    second = agent.handle(
        {
            "op": "issue_revision",
            "description": "Added run light",
            "date": "2026-10-08",
            "clouds": True,
            "clear_clouds": True,
        }
    )
    assert second["ok"]
    assert second["result"]["rev"] == "B"
    assert second["result"]["clouds"] >= 1
    listed = agent.handle({"op": "list_revisions"})
    assert [row["rev"] for row in listed["result"]["revisions"]] == ["A", "B"]
    assert listed["result"]["current"] == "B"
    compared = agent.handle({"op": "compare_revisions", "from_rev": "A", "to_rev": "working"})
    assert compared["ok"]
    assert compared["result"]["counts"]["devices_added"] >= 1
    csv_path = tmp_path / "agent.csv"
    pdf_path = tmp_path / "agent.pdf"
    filed = agent.handle(
        {"op": "compare_revisions", "left": "A", "right": "B", "path": str(csv_path), "pdf": str(pdf_path)}
    )
    assert filed["ok"]
    assert csv_path.is_file() and pdf_path.is_file()
    manual = agent.handle({"op": "add_revision_cloud", "x": 12, "y": 18, "width": 28, "height": 14, "rev": "B"})
    assert manual["ok"]
    assert manual["result"]["manual"] is True
    again = agent.handle({"op": "issue_revision", "rev": "B", "description": "duplicate"})
    assert not again["ok"]
    assert "already issued" in again["error"]
    help_reply = agent.handle({"op": "help"})
    for name in ("issue_revision", "list_revisions", "compare_revisions", "add_revision_cloud"):
        assert name in help_reply["result"]["ops"]
        assert name in help_reply["result"]["help"]


def test_drawing_agent_issues_a_revision():
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from panelcad.ai.drawing import drawing_agent, open_drawing
    from tests.test_drawing_agent import _response

    seen: list[str] = []
    steps = [
        _response(
            "issue_revision",
            {
                "description": "Issued for construction",
                "date": "2026-10-01",
                "drawn": "GH",
                "checked": "AK",
                "approved": "JS",
                "clouds": False,
            },
            "issue",
        ),
        _response("list_revisions", {}, "list"),
        _response(
            "add_revision_cloud",
            {"x": 30, "y": 30, "w": 40, "h": 18, "rev": "A"},
            "cloud",
        ),
        ModelResponse(parts=[TextPart(content="Issued revision A.")]),
    ]

    def model(messages, info):
        seen.extend(tool.name for tool in info.function_tools)
        return steps.pop(0)

    drawing = open_drawing(name="Agent Rev")
    with drawing_agent.override(model=FunctionModel(model)):
        result = drawing_agent.run_sync("Issue the customer revision.", deps=drawing)
    assert "A" in result.output
    assert {
        "issue_revision",
        "compare_revisions",
        "add_revision_cloud",
        "list_revisions",
    } <= set(seen)
    session = drawing.operator.session
    assert session is not None
    assert session.list_revisions()["current"] == "A"
    assert session.project.revision_control.clouds[0].manual is True


def test_cli_rev_issue_list_compare(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    src = tmp_path / "job.pcad.yaml"
    s = Session.create("CLI Rev")
    s.place("iec.operators.pilot_light", ref="H1", x=40, y=40)
    s.save(src)
    runner = CliRunner()
    issued = runner.invoke(
        app,
        [
            "rev",
            "issue",
            str(src),
            "-d",
            "Issued for construction",
            "--date",
            "2026-10-01",
            "--drawn",
            "GH",
            "--checked",
            "AK",
            "--approved",
            "JS",
            "--no-clouds",
        ],
    )
    assert issued.exit_code == 0, issued.output
    assert "issued A" in issued.output
    opened = Session.open(src)
    opened.place("iec.operators.pilot_light", ref="H9", x=140, y=40)
    opened.save(src)
    again = runner.invoke(
        app,
        ["rev", "issue", str(src), "-d", "Added run light", "--date", "2026-10-08", "--scheme", "letter"],
    )
    assert again.exit_code == 0, again.output
    assert "issued B" in again.output
    listed = runner.invoke(app, ["rev", "list", str(src)])
    assert listed.exit_code == 0
    assert "A" in listed.output and "B" in listed.output
    assert "current B" in listed.output
    csv_path = tmp_path / "cli.csv"
    pdf_path = tmp_path / "cli.pdf"
    compared = runner.invoke(
        app,
        ["rev", "compare", str(src), "A", "B", "--csv", str(csv_path), "--pdf", str(pdf_path)],
    )
    assert compared.exit_code == 0, compared.output
    assert "devices +" in compared.output
    assert csv_path.is_file() and pdf_path.is_file()
    duplicate = runner.invoke(app, ["rev", "issue", str(src), "--rev", "B"])
    assert duplicate.exit_code == 1
    assert "already issued" in duplicate.output


def test_gui_dialogs_cloud_tool_and_revisions_menu():
    _app()
    s = Session.create("GUI Rev", sheet_id="main")
    s.place("iec.operators.pilot_light", ref="H1", x=50, y=50)
    dialog = IssueRevisionDialog(s)
    dialog.description.setText("Issued for construction")
    dialog.date.setText("2026-10-01")
    dialog.drawn.setText("GH")
    dialog.checked.setText("AK")
    dialog.approved.setText("JS")
    dialog.accept()
    assert dialog.result_info is not None
    assert dialog.result_info["rev"] == "A"
    assert s.list_revisions()["current"] == "A"

    history = RevisionHistoryDialog(s)
    assert history.table.rowCount() == 1
    assert history.table.item(0, 0).text() == "A"
    assert history.table.item(0, 2).text() == "Issued for construction"
    history.close()

    s.place("iec.operators.pilot_light", ref="H9", x=160, y=50)
    compare = CompareRevisionsDialog(s)
    assert compare.report is not None
    assert compare.table.rowCount() >= 1
    tags = " ".join(compare.table.item(r, 2).text() for r in range(compare.table.rowCount()))
    assert "H9 added (Pilot light)" in tags
    compare.close()

    view = SchematicView(s)
    view.set_cloud_mode()
    assert view.mode == "cloud"
    info = view.commit_cloud(30, 28, 48, 20, rev="A")
    assert info["manual"] is True
    assert any(isinstance(item, RevisionCloudItem) for item in view.scene.items())
    menu = QMenu()
    view.fill_context_menu(menu, QPoint(8, 8))
    assert any(action.text() == "Revision cloud" for action in menu.actions())
    view.close()

    window = MainWindow(s)
    names = [action.text().replace("&", "") for action in window.menuBar().actions()]
    assert "Revisions" in names
    revisions = next(action for action in window.menuBar().actions() if action.text().replace("&", "") == "Revisions")
    labels = [action.text().replace("&", "") for action in revisions.menu().actions()]
    assert "Issue revision…" in labels
    assert "Revision history…" in labels
    assert "Compare revisions…" in labels
    assert "Clear clouds" in labels
    cloud = next(action for action in revisions.menu().actions() if action.text().replace("&", "") == "Revision cloud")
    assert cloud.shortcut().toString() == "C"
    before = len(s.project.revision_control.clouds)
    window.clear_revision_clouds()
    assert len(s.project.revision_control.clouds) == 0
    assert before > 0
    window.close()


def _rect_hit(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _bary(point, tri) -> tuple[float, float, float] | None:
    (x, y), (x1, y1), (x2, y2), (x3, y3) = point, tri[0], tri[1], tri[2]
    den = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if abs(den) < 1e-9:
        return None
    w1 = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / den
    w2 = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / den
    return w1, w2, 1.0 - w1 - w2


def _strict_inside(point, tri, eps: float = 1e-4) -> bool:
    weights = _bary(point, tri)
    return weights is not None and all(weight > eps for weight in weights)


@pytest.mark.confirm_generic_layout
def test_compare_cells_wrap_full_text_and_read_as_shop_items(tmp_path: Path):
    _app()
    s = customer_job()
    assert s.project.title_block.standard == "IEC"
    report = s.compare_revisions("A", "B")
    from panelcad.render.painter import Painter
    from panelcad.render.revision_report import (
        display_cells,
        display_item,
        paginate_compare,
        paint_compare_page,
        prepare_compare,
    )

    added = display_item(_row(report, section="device", change="added", tag="H9"))
    removed = display_item(_row(report, section="device", change="removed", tag="S2"))
    retag = display_item(_row(report, section="device", change="changed", tag="PB2", field="tag"))
    assert added == "H9 added (Pilot light)"
    assert removed == "S2 removed (Pushbutton NO)"
    assert retag == "PB1 retagged PB2"
    assert display_item(_row(report, section="layout", change="moved", tag="K9")) == "K9 moved"
    h9 = display_cells(_row(report, section="device", change="added", tag="H9"))
    assert h9[4] == "iec.operators.pilot_light"
    pilot = _row(report, section="bom", change="qty", detail="iec.operators.pilot_light")
    assert "iec.operators.pilot_light" in display_item(pilot)
    assert display_cells(pilot)[4] == pilot["after"]

    class Capture(Painter):
        def __init__(self) -> None:
            self.texts: list[str] = []

        def set_stroke(self, color: str, width: float) -> None:
            return

        def set_fill(self, color: str | None) -> None:
            return

        def rect(self, x: float, y: float, w: float, h: float) -> None:
            return

        def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
            return

        def circle(self, x: float, y: float, r: float) -> None:
            return

        def polyline(self, points: list[tuple[float, float]]) -> None:
            return

        def text(self, x, y, text, size, align="middle", rotate=0) -> None:
            self.texts.append(str(text))

    header_h, prepared = prepare_compare(s.project, report)
    pages = paginate_compare(prepared, 279.4, header_h)
    capture = Capture()
    for index, page in enumerate(pages):
        paint_compare_page(
            capture,
            s.project,
            report,
            page,
            page_index=index,
            page_count=len(pages),
        )
    for text in capture.texts:
        assert "…" not in text
        assert not text.endswith("...")
    blob = "".join(capture.texts)
    assert "iec.operators.pilot_light" in blob
    assert "iec.breakers.mcb_3p_32a" in blob
    assert "iec.operators.pb_no" in blob
    assert "H9 added (Pilot light)" in capture.texts
    assert "layout moves" in blob
    assert "BOM" in blob

    csv_path = tmp_path / "compare.csv"
    pdf_path = tmp_path / "compare.pdf"
    from panelcad.render.revision_report import write_compare_csv, write_compare_pdf

    write_compare_csv(csv_path, report)
    raw = csv_path.read_text(encoding="utf-8-sig")
    assert "iec.operators.pilot_light" in raw
    assert "…" not in raw
    assert raw.splitlines()[0] == "section,change,sheet,tag,field,before,after,detail"
    write_compare_pdf(pdf_path, report, s.project)
    from panelcad.render.print_package import extract_pdf_text

    compact = "".join(extract_pdf_text(pdf_path).split())
    assert "…" not in compact
    assert "..." not in compact
    assert "iec.operators.pilot_light" in compact
    assert "iec.breakers.mcb_3p_32a" in compact
    assert "iec.operators.pb_no" in compact
    assert "layoutmoves" in compact


@pytest.mark.confirm_generic_layout
def test_removal_and_moved_clouds_note_the_empty_mark():
    _app()
    s = customer_job()
    clouds = s.project.revision_control.clouds
    notes = {cloud.note for cloud in clouds}
    assert "S2 deleted" in notes
    assert "K9 moved" in notes
    assert all(cloud.rev == "B" for cloud in clouds if cloud.note)
    from panelcad.core.revisions import RevisionControl, _component_box, _device, clouds_for_areas
    from panelcad.render.clouds import choose_delta, cloud_obstacles, drawn_box, note_bounds
    from panelcad.render.titleblock import title_block_rect

    for cloud in clouds:
        if not cloud.note:
            continue
        sheet = s.sheet(cloud.sheet)
        x, y, w, h = drawn_box(cloud, sheet)
        bounds = note_bounds(x, y, w, h, cloud.note)
        assert bounds is not None
        assert bounds[0] >= x + 0.6
        assert bounds[1] >= y + 0.6
        assert bounds[0] + bounds[2] <= x + w - 0.6
        assert bounds[1] + bounds[3] <= y + h - 0.6
        assert not _rect_hit(bounds, title_block_rect(sheet))
        tri = choose_delta(x, y, w, h, cloud_obstacles(sheet, s.catalog), rev=cloud.rev)
        assert not any(_strict_inside((px, py), tri) for px, py in (
            (bounds[0], bounds[1]),
            (bounds[0] + bounds[2], bounds[1]),
            (bounds[0], bounds[1] + bounds[3]),
            (bounds[0] + bounds[2], bounds[1] + bounds[3]),
        ))
        for comp in sheet.components:
            box = _component_box(sheet, comp, _device(s.catalog, comp.library_id))
            assert not _rect_hit(bounds, box), cloud.note
        for rail in sheet.rails:
            half = float(rail.height_mm) / 2.0
            rail_box = (rail.x, rail.y - half - 4.0, rail.length_mm, rail.height_mm + 4.0)
            assert not _rect_hit(bounds, rail_box), cloud.note

    store = RevisionControl()
    merged = clouds_for_areas(
        store,
        [
            {"sheet": "main", "x": 0, "y": 0, "w": 20, "h": 20, "note": "S1 deleted"},
            {"sheet": "main", "x": 24, "y": 0, "w": 20, "h": 20, "note": ""},
        ],
        "B",
    )
    assert len(merged) == 1
    assert merged[0].note == ""
    ghosts = clouds_for_areas(
        store,
        [
            {"sheet": "main", "x": 0, "y": 80, "w": 20, "h": 20, "note": "S1 deleted"},
            {"sheet": "main", "x": 24, "y": 80, "w": 20, "h": 20, "note": "S2 deleted"},
        ],
        "B",
    )
    assert len(ghosts) == 1
    assert "S1 deleted" in ghosts[0].note
    assert "S2 deleted" in ghosts[0].note


@pytest.mark.confirm_generic_layout
def test_suppressed_note_draws_no_empty_frame_or_box():
    _app()
    from panelcad.core.revisions import RevisionCloud, RevisionControl, clouds_for_areas
    from panelcad.render.clouds import paint_revision_cloud
    from panelcad.render.painter import Painter
    from panelcad.render.xref import draw_cross_references

    class Capture(Painter):
        def __init__(self) -> None:
            self.rects: list[tuple[float, float, float, float]] = []
            self.texts: list[str] = []

        def set_stroke(self, color: str, width: float) -> None:
            return

        def set_fill(self, color: str | None) -> None:
            return

        def rect(self, x: float, y: float, w: float, h: float) -> None:
            self.rects.append((float(x), float(y), float(w), float(h)))

        def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
            return

        def circle(self, x: float, y: float, r: float) -> None:
            return

        def polyline(self, points: list[tuple[float, float]]) -> None:
            return

        def polygon(self, points: list[tuple[float, float]]) -> None:
            return

        def text(self, x, y, text, size, align="middle", rotate=0) -> None:
            self.texts.append(str(text))

    s = customer_job()
    sheet = s.sheet("main")
    blank = RevisionCloud(id="cloud-blank", sheet="main", x=10, y=10, w=40, h=24, rev="B", note="")
    capture = Capture()
    paint_revision_cloud(capture, blank, sheet, s.catalog)
    assert capture.rects == []
    assert capture.texts == ["B"]

    merged = clouds_for_areas(
        RevisionControl(),
        [
            {"sheet": "main", "x": 0, "y": 0, "w": 20, "h": 20, "note": "S1 deleted"},
            {"sheet": "main", "x": 24, "y": 0, "w": 20, "h": 20, "note": ""},
        ],
        "B",
    )
    assert merged[0].note == ""
    dropped = Capture()
    paint_revision_cloud(dropped, merged[0], sheet, s.catalog)
    assert dropped.rects == []
    assert all(text.strip() for text in dropped.texts)
    assert dropped.texts == ["B"]

    painted = Capture()
    placed = draw_cross_references(painted, s.project, sheet, s.catalog)
    assert painted.rects == []
    assert not any(not text.strip() or text.strip() == "—" for text in painted.texts)
    assert not any(ann.owner == "K1" for ann, _x, _y, _align in placed)


@pytest.mark.confirm_generic_layout
def test_delta_triangle_touches_the_corner_and_clears_the_scallops():
    _app()
    import math

    from panelcad.render.clouds import cloud_polyline, delta_text_point, delta_triangle
    from panelcad.render.qt_painter import text_width_mm

    def check(x, y, w, h, *, above: bool) -> None:
        tri = delta_triangle(x, y, w, h)
        corner = (x + w, y if above else y + h)
        assert any(abs(p[0] - corner[0]) < 1e-6 and abs(p[1] - corner[1]) < 1e-6 for p in tri)
        others = [p for p in tri if abs(p[0] - corner[0]) > 1e-6 or abs(p[1] - corner[1]) > 1e-6]
        assert len(others) == 2
        for px, py in others:
            assert px > corner[0] + 1.0
            assert (py < corner[1] - 1.0) if above else (py > corner[1] + 1.0)
            nearest = min(math.hypot(px - sx, py - sy) for sx, sy in cloud_polyline(x, y, w, h))
            assert nearest > 2.0
        samples = cloud_polyline(x, y, w, h)
        for sample in samples:
            if math.hypot(sample[0] - corner[0], sample[1] - corner[1]) <= 1.2:
                continue
            assert not _strict_inside(sample, tri)
        size = 3.2
        cx, cy = delta_text_point(tri, size)
        tw = text_width_mm("B", size)
        glyph = (cx - tw / 2.0, cy - 0.93 * size, tw, 1.17 * size)
        corners = (
            (glyph[0], glyph[1]),
            (glyph[0] + glyph[2], glyph[1]),
            (glyph[0], glyph[1] + glyph[3]),
            (glyph[0] + glyph[2], glyph[1] + glyph[3]),
        )
        for corner_pt in corners:
            assert _strict_inside(corner_pt, tri)
            nearest = min(math.hypot(corner_pt[0] - sx, corner_pt[1] - sy) for sx, sy in samples)
            assert nearest > 1.2

    check(40, 30, 50, 40, above=True)
    check(10, 1.0, 36, 28, above=False)

    from panelcad.render.clouds import (
        choose_delta,
        cloud_obstacles,
        delta_glyph_box,
        drawn_box,
        triangle_hits_rect,
    )

    bare = choose_delta(40, 30, 50, 40, [])
    assert bare == delta_triangle(40, 30, 50, 40)
    low = choose_delta(10, 1.0, 36, 28, [])
    assert low == delta_triangle(10, 1.0, 36, 28)

    s = customer_job()
    for cloud in s.project.revision_control.clouds:
        if cloud.sheet != "panel":
            continue
        sheet = s.sheet(cloud.sheet)
        x, y, w, h = drawn_box(cloud, sheet)
        obstacles = cloud_obstacles(sheet, s.catalog)
        tri = choose_delta(x, y, w, h, obstacles, rev=cloud.rev)
        corners = ((x + w, y), (x, y), (x + w, y + h), (x, y + h))
        touched = [
            corner
            for corner in corners
            if any(abs(p[0] - corner[0]) < 1e-6 and abs(p[1] - corner[1]) < 1e-6 for p in tri)
        ]
        assert len(touched) == 1
        corner = touched[0]
        others = [p for p in tri if abs(p[0] - corner[0]) > 1e-6 or abs(p[1] - corner[1]) > 1e-6]
        assert len(others) == 2
        for px, py in others:
            assert (px - corner[0]) * (corner[0] - x - w / 2.0) > 0
            assert (py - corner[1]) * (corner[1] - y - h / 2.0) > 0
            nearest = min(math.hypot(px - sx, py - sy) for sx, sy in cloud_polyline(x, y, w, h))
            assert nearest > 2.0
        for sample in cloud_polyline(x, y, w, h):
            if math.hypot(sample[0] - corner[0], sample[1] - corner[1]) <= 1.2:
                continue
            assert not _strict_inside(sample, tri)
        glyph = delta_glyph_box(tri, cloud.rev or "B", 3.2)
        for box in obstacles:
            assert not triangle_hits_rect(tri, box), cloud.note or cloud.id
            assert not _rect_hit(glyph, box), cloud.note or cloud.id
        assert corner[1] > y + h - 0.1
