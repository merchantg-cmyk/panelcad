"""Physical terminal-strip picture: one renderer, project data, readable blocks."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication, QStyleOptionGraphicsItem

from panelcad import Session
from panelcad.core.models import SheetKind
from panelcad.core.terminal_strip import layout_terminal_strip
from panelcad.render.contents import draw_sheet_body
from panelcad.render.svg import SvgPainter, sheet_to_svg
from panelcad.render.terminal_strip import (
    MIN_TAG_MM,
    PE_FILL,
    STRIP_SHEET_KINDS,
    draw_physical_scene,
    drawn_block_refs,
    layout_physical_pages,
    model_block_refs,
    physical_scene_for_sheet,
    rects_overlap,
    terminal_strip_caption,
    text_rects,
)

ROOT = Path(__file__).resolve().parents[1]


def _assert_drawn_matches_model(session: Session) -> None:
    model = model_block_refs(session.project, session.catalog)
    drawn = drawn_block_refs(session.project, session.catalog)
    assert model
    assert sorted(drawn) == sorted(model)
    assert len(drawn) == len(set(drawn)) == len(model)


def _hardware(session: Session, strip: str, after: str) -> None:
    """End stop, end plate, and partition recorded on ``strip``."""

    session.place(
        "iec.accessories.end_stop",
        ref=f"{strip}-XS",
        x=0,
        y=0,
        properties={"strip": strip, "accessory": "end_stop", "end": "left"},
        snap=False,
    )
    session.place(
        "iec.accessories.end_plate",
        ref=f"{strip}-XP",
        x=0,
        y=0,
        properties={"strip": strip, "accessory": "end_plate", "end": "left"},
        snap=False,
    )
    session.place(
        "iec.accessories.partition",
        ref=f"{strip}-XD",
        x=0,
        y=0,
        properties={"strip": strip, "accessory": "partition", "after": after},
        snap=False,
    )


def _place_strip(session: Session, name: str, count: int, *, library_id: str = "iec.terminals.x_2p5", y0: float = 20.0) -> None:
    for index in range(1, count + 1):
        session.place(library_id, ref=f"{name}-{index}", x=40.0, y=y0 + index * 12.0)


def _wired_board() -> tuple[Session, str]:
    session = Session.create("Board")
    _place_strip(session, "XA", 3)
    _place_strip(session, "XPE", 1, library_id="iec.terminals.pe_4", y0=80.0)
    session.set_wire_numbering(enabled=False)
    session.wire("XA-1.A", "XA-2.A", net="PANEL")
    session.wire("XA-1.B", "XA-3.B", net="FIELD")
    session.number_wires(start=10, scheme="sequential")
    sheet = session.add_sheet("terminals", title="Terminals", kind="terminal", make_active=False)
    return session, sheet.id


@pytest.mark.confirm_generic_layout
def test_block_count_and_order_match_the_model():
    session, sheet_id = _wired_board()
    sheet = session.project.sheet(sheet_id)
    scene = physical_scene_for_sheet(session.project, session.catalog, sheet)
    drawn = [block["ref"] for block in scene["blocks"]]
    expect: list[str] = []
    for name in ("XA", "XPE"):
        layout = layout_terminal_strip(session.project, session.catalog, name)
        expect.extend(item["ref"] for item in layout["items"])
    assert drawn == expect
    assert len(drawn) == len(expect) == 4
    assert len({round(block["w"], 3) for block in scene["blocks"]}) == 1
    assert len({round(block["h"], 3) for block in scene["blocks"]}) == 1


@pytest.mark.confirm_generic_layout
def test_groups_are_separate_labeled_strips():
    session, sheet_id = _wired_board()
    scene = physical_scene_for_sheet(session.project, session.catalog, session.project.sheet(sheet_id))
    groups: dict[str, list[dict]] = {}
    for group in scene["groups"]:
        groups.setdefault(group["strip"], []).append(group)
    assert set(groups) == {"XA", "XPE"}
    assert groups["XA"][0]["title"].startswith("Terminal strip XA")
    assert groups["XPE"][0]["title"].startswith("Terminal strip XPE")
    for comp in session.project.sheets[0].components:
        if str(comp.id).startswith("XA"):
            comp.properties["function"] = "PLC I/O"
    titled = physical_scene_for_sheet(session.project, session.catalog, session.project.sheet(sheet_id))
    xa = next(group["title"] for group in titled["groups"] if group["strip"] == "XA")
    assert xa.startswith("XA PLC I/O")
    assert "Terminal strip" not in xa
    assert "XA-1" not in groups["XPE"][0]["block_refs"]
    assert groups["XPE"][0]["block_refs"] == ["XPE-1"]
    rails = scene["rails"]
    assert len(rails) >= 2
    for left, right in zip(rails, rails[1:]):
        a = (left["x"], left["y"], left["w"], left["h"])
        b = (right["x"], right["y"], right["w"], right["h"])
        assert not rects_overlap(a, b, gap=0.2)
    pe = next(block for block in scene["blocks"] if block["ref"] == "XPE-1")
    assert pe["pe"] is True
    assert pe["fill"].lower() == PE_FILL.lower()
    assert len(pe["screws"]) == 2
    plain = next(block for block in scene["blocks"] if block["ref"] == "XA-1")
    assert plain["fill"].lower() != PE_FILL.lower()
    assert len(plain["screws"]) == 2


@pytest.mark.confirm_generic_layout
def test_labels_do_not_overlap_and_wires_sit_on_their_sides():
    session = Session.create("Labels")
    _place_strip(session, "XB", 6)
    session.place("iec.terminals.pe_4", ref="XPE-1", x=40, y=200)
    session.set_wire_numbering(enabled=False)
    session.wire("XB-1.A", "XB-2.A", net="P")
    session.wire("XB-1.B", "XB-2.B", net="F")
    session.number_wires(start=21, scheme="sequential")
    session.jumper_terminals(["XB-3", "XB-5"])
    sheet = session.add_sheet("terminals", title="Terminals", kind="terminal", make_active=False)
    scene = physical_scene_for_sheet(session.project, session.catalog, sheet)
    rects = text_rects(scene)
    assert len(rects) >= 8
    for index, left in enumerate(rects):
        for right in rects[index + 1 :]:
            assert not rects_overlap(left, right, gap=0.05), (left, right)
    by_ref = {block["ref"]: block for block in scene["blocks"]}
    for label in scene["texts"]:
        if label["role"] not in {"panel", "field", "tag"}:
            continue
        block = next(
            (item for item in scene["blocks"] if item["ref"] == label["text"] or label["text"] in {
                item["internal"],
                item["external"],
            }),
            None,
        )
        if label["role"] == "tag":
            block = by_ref[label["text"]]
            assert label["rotate"] == -90.0
            assert block["y"] < label["y"] < block["y"] + block["h"]
        elif label["role"] == "panel" and block is not None:
            assert label["y"] < block["y"]
        elif label["role"] == "field" and block is not None:
            assert label["y"] > block["y"] + block["h"]
    assert any(label["role"] == "legend" for label in scene["texts"])
    assert any(label["text"] == "Internal" for label in scene["texts"])
    assert any(label["text"] == "Field" for label in scene["texts"])
    assert any(label["text"] == scene["rail_name"] for label in scene["texts"])
    joined = " ".join(label["text"] for label in scene["texts"])
    assert "panel-side" in joined
    assert "PE" in joined


@pytest.mark.confirm_generic_layout
def test_picture_scales_from_the_project_and_wraps():
    small = Session.create("Two")
    _place_strip(small, "T1", 2)
    small_sheet = small.add_sheet("terminals", title="Terminals", kind="terminal", make_active=False)
    small_scene = physical_scene_for_sheet(small.project, small.catalog, small_sheet)
    assert len(small_scene["blocks"]) == 2

    wide = Session.create("Many")
    _place_strip(wide, "T9", 48)
    wide_sheet = wide.add_sheet("terminals", title="Terminals", kind="terminal", make_active=False)
    wide_scene = physical_scene_for_sheet(wide.project, wide.catalog, wide_sheet)
    assert len(wide_scene["blocks"]) == 48
    rows = {round(block["y"], 2) for block in wide_scene["blocks"]}
    assert len(rows) >= 2

    tight = Session.create("Pages")
    _place_strip(tight, "T3", 24)
    tight_sheet = tight.add_sheet("terminals", title="Terminals", kind="terminal", make_active=False)
    tight_sheet.page.size = "custom"
    tight_sheet.page.width_mm = 200.0
    tight_sheet.page.height_mm = 150.0
    pages = layout_physical_pages(tight.project, tight.catalog, tight_sheet, fit_pages=None)
    assert len(pages) >= 2
    assert sum(len(page["blocks"]) for page in pages) == 24
    seen: set[str] = set()
    for page in pages:
        refs = [block["ref"] for block in page["blocks"]]
        assert not seen.intersection(refs)
        seen.update(refs)


@pytest.mark.confirm_generic_layout
def test_continuation_sheets_cover_the_model_without_duplicating():
    session = Session.create("Long strip")
    session.place("iec.terminals.x_2p5", ref="X1-1", x=40, y=20)
    opened = session.open_terminal_strip("X1")
    page = session.project.sheet(opened["sheet"]).page
    page.size = "custom"
    page.width_mm = 90.0
    page.height_mm = 220.0
    for _ in range(17):
        session.add_terminal("X1")
    session.open_terminal_strip("X1")
    layout = layout_terminal_strip(session.project, session.catalog, "X1")
    model = [item["ref"] for item in layout["items"]]
    sheets = [
        sheet
        for sheet in session.project.sheets
        if sheet.kind == "terminal" and sheet.strip == "X1"
    ]
    assert len(sheets) >= 2
    drawn: list[str] = []
    for sheet in sheets:
        scene = physical_scene_for_sheet(session.project, session.catalog, sheet)
        refs = [block["ref"] for block in scene["blocks"]]
        assert refs
        drawn.extend(refs)
        rects = text_rects(scene)
        for index, left in enumerate(rects):
            for right in rects[index + 1 :]:
                assert not rects_overlap(left, right, gap=0.02)
    assert sorted(drawn) == sorted(model)
    assert len(drawn) == len(set(drawn)) == len(model)


def test_renderer_has_no_project_specific_names():
    source = (ROOT / "src/panelcad/render/terminal_strip.py").read_text(encoding="utf-8")
    # Pieces stay split so a shop name is not written into the tree.
    needles = [
        "Voya" + "ger",
        "Techni" + "mark",
        "TB" + "PIC",
        "XI" + "O",
        "Chari" + "ty",
        "Key" + "ence",
    ]
    for banned in needles:
        assert banned not in source
    assert "BLOCK_W" in source


@pytest.mark.confirm_generic_layout
def test_wiring_list_bytes_stay_put(tmp_path: Path):
    """Drawing the strip does not change the wire list or the wire-by-wire sheet."""

    from panelcad.convert.scorecard import verify_golden_nema_from_to_csvs
    from panelcad.core.panel_wiring import add_wire_by_wire_sheet, export_wire_by_wire_csv

    session = Session.create("Wires")
    _place_strip(session, "X1", 2)
    session.set_wire_numbering(enabled=False)
    session.wire("X1-1.A", "X1-2.B", net="N1")
    session.number_wires(start=4, scheme="sequential")
    rows = session.wire_list_rows()
    session.export_wire_list(tmp_path / "wires.csv")
    wire_bytes = (tmp_path / "wires.csv").read_bytes()
    session.add_sheet("layout1", title="Layout", kind="panel_layout", make_active=False)
    wbw = add_wire_by_wire_sheet(session, sheet_id="wire_by_wire", layout_sheet_id="layout1")
    export_wire_by_wire_csv(session, tmp_path / "wbw.csv")
    wbw_bytes = (tmp_path / "wbw.csv").read_bytes()
    wbw_svg = sheet_to_svg(wbw, session.catalog, session.project)

    board = session.add_sheet("terminals", title="Terminals", kind="terminal", make_active=False)
    picture = sheet_to_svg(board, session.catalog, session.project)
    assert "Terminal strip X1" in picture
    assert "X1-1" in picture

    assert session.wire_list_rows() == rows
    session.export_wire_list(tmp_path / "wires-after.csv")
    assert (tmp_path / "wires-after.csv").read_bytes() == wire_bytes
    export_wire_by_wire_csv(session, tmp_path / "wbw-after.csv")
    assert (tmp_path / "wbw-after.csv").read_bytes() == wbw_bytes
    assert sheet_to_svg(session.project.sheet("wire_by_wire"), session.catalog, session.project) == wbw_svg

    golden = verify_golden_nema_from_to_csvs(ROOT / "tests" / "golden" / "wire_list")
    assert golden == {"dol": True, "reversing": True, "plc": True}


@pytest.mark.confirm_generic_layout
def test_every_strip_sheet_uses_the_physical_renderer(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []
    real = draw_physical_scene

    def spy(painter, scene) -> None:
        calls.append(str(scene.get("sheet_id") or ""))
        real(painter, scene)

    monkeypatch.setattr("panelcad.render.terminal_strip.draw_physical_scene", spy)

    session = Session.create("Kinds")
    _place_strip(session, "X1", 2)
    made = {}
    for kind in get_args(SheetKind):
        made[kind] = session.add_sheet(f"page-{kind}", title=kind, kind=kind, make_active=False)
    terminal = made["terminal"]
    terminal.strip = "X1"

    for kind, sheet in made.items():
        before = len(calls)
        painter = SvgPainter()
        try:
            draw_sheet_body(painter, sheet, session.catalog, project=session.project)
        except Exception:
            if kind in STRIP_SHEET_KINDS:
                raise
        if kind in STRIP_SHEET_KINDS:
            assert len(calls) > before, kind
        else:
            assert len(calls) == before, kind

    before = len(calls)
    sheet_to_svg(terminal, session.catalog, session.project)
    assert len(calls) == before + 1

    from panelcad.render.page import paint_sheet

    app = QApplication.instance() or QApplication([])
    image = QImage(32, 32, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    paint_sheet(painter, session.project, terminal, session.catalog, sheet_index=0, sheet_count=1)
    painter.end()
    assert len(calls) == before + 2

    from panelcad.gui.main_window import MainWindow

    window = MainWindow(session)
    window.session.set_active_sheet(terminal.id)
    window.show()
    app.processEvents()
    window._refresh_from_session(fit=True)
    app.processEvents()
    scene_items = [item for item in window.canvas.scene.items() if item.__class__.__name__ == "StripSceneItem"]
    assert scene_items
    image = QImage(64, 64, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    scene_items[0].paint(painter, QStyleOptionGraphicsItem())
    painter.end()
    assert len(calls) > before + 2
    window.close()

    body = (ROOT / "src/panelcad/render/contents.py").read_text(encoding="utf-8")
    canvas = (ROOT / "src/panelcad/gui/canvas.py").read_text(encoding="utf-8")
    strip = (ROOT / "src/panelcad/gui/strip_canvas.py").read_text(encoding="utf-8")
    assert "draw_terminal_sheet" in body
    assert "physical_scene_for_sheet" in canvas
    assert "draw_physical_scene" in strip
    assert "StripJumperItem" in strip
    assert "draw_wire_by_wire_sheet" in body


@pytest.mark.confirm_generic_layout
def test_pe_ground_mark_and_end_hardware_marks():
    session = Session.create("Marks")
    session.place("iec.terminals.x_2p5", ref="X1-1", x=40, y=20)
    session.place("iec.terminals.pe_4", ref="X1-2", x=40, y=50)
    session.open_terminal_strip("X1")
    sheet = next(page for page in session.project.sheets if page.kind == "terminal" and page.strip == "X1")
    scene = physical_scene_for_sheet(session.project, session.catalog, sheet)
    pe = next(block for block in scene["blocks"] if block["pe"])
    assert len(pe["ground"]) == 3
    widths = [abs(line[2] - line[0]) for line in pe["ground"]]
    assert widths[0] > widths[1] > widths[2]
    stop = next(block for block in scene["blocks"] if block["kind"] == "end_stop")
    assert len(stop["marks"]) == 2
    assert any(label["text"] == "END" and label["role"] == "mark" for label in scene["texts"])
    assert any(label["text"] == "STOP" and label["role"] == "mark" for label in scene["texts"])
    rects = text_rects(scene)
    for index, left in enumerate(rects):
        for right in rects[index + 1 :]:
            assert not rects_overlap(left, right, gap=0.05)


@pytest.mark.confirm_generic_layout
def test_adjacent_short_jumpers_do_not_collide_and_keep_ticks():
    session = Session.create("Jumpers")
    _place_strip(session, "X1", 6)
    session.jumper_terminals(["X1-1", "X1-2"])
    session.jumper_terminals(["X1-3", "X1-4"])
    session.open_terminal_strip("X1")
    sheet = next(page for page in session.project.sheets if page.kind == "terminal" and int(page.strip_part or 1) == 1)
    scene = physical_scene_for_sheet(session.project, session.catalog, sheet)
    labels = [label for label in scene["texts"] if label["role"] == "jumper"]
    assert len(labels) >= 2
    for index, left in enumerate(labels):
        for right in labels[index + 1 :]:
            assert not rects_overlap(left["rect"], right["rect"], gap=0.4)
    assert all(len(jumper.get("ticks") or []) >= 2 for jumper in scene["jumpers"])
    tipped = next(jumper for jumper in scene["jumpers"] if jumper.get("part_number") or jumper.get("catalog"))
    app = QApplication.instance() or QApplication([])
    del app
    from panelcad.gui.strip_canvas import StripJumperItem

    item = StripJumperItem(tipped)
    tip = item.toolTip()
    assert tipped["id"] in tip
    if tipped.get("part_number"):
        assert tipped["part_number"] in tip
    if tipped.get("catalog"):
        assert tipped["catalog"] in tip
    assert "–" in tip


@pytest.mark.confirm_generic_layout
def test_long_strip_continues_before_tags_shrink_below_2mm():
    session = Session.create("Readable")
    session.place("iec.terminals.x_2p5", ref="X1-1", x=40, y=20)
    opened = session.open_terminal_strip("X1")
    page = session.project.sheet(opened["sheet"]).page
    page.size = "custom"
    page.width_mm = 120.0
    page.height_mm = 180.0
    for _ in range(28):
        session.add_terminal("X1")
    session.open_terminal_strip("X1")
    sheets = [
        sheet
        for sheet in session.project.sheets
        if sheet.kind == "terminal" and sheet.strip == "X1"
    ]
    assert len(sheets) >= 2
    seen: set[str] = set()
    for sheet in sheets:
        scene = physical_scene_for_sheet(session.project, session.catalog, sheet)
        assert scene["blocks"]
        assert float(scene["tag_size"]) + 1e-6 >= MIN_TAG_MM
        refs = [block["ref"] for block in scene["blocks"]]
        assert not seen.intersection(refs)
        seen.update(refs)
        assert f"sheet {scene['page_index']}" in terminal_strip_caption(
            "X1",
            sum(1 for block in scene["blocks"] if block["kind"] == "terminal"),
            int(scene["page_index"]),
            int(scene["page_count"]),
        )
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(session)
    window.show()
    app.processEvents()
    continued = next(sheet for sheet in sheets if int(sheet.strip_part or 1) > 1)
    window.session.set_active_sheet(continued.id)
    window._refresh_from_session(fit=True)
    app.processEvents()
    assert f"sheet {int(continued.strip_part)}" in window.canvas._strip_caption
    window.close()


@pytest.mark.confirm_generic_layout
def test_every_model_block_is_drawn_across_sheets(tmp_path: Path):
    """Nothing past page 1 is dropped: unnamed board, a strip grown later, and reload."""

    board = Session.create("Unnamed board")
    _place_strip(board, "XA", 70)
    _place_strip(board, "XB", 50, y0=800)
    _place_strip(board, "XC", 40, y0=1400)
    board.place("iec.terminals.pe_4", ref="XPE-1", x=40, y=1200)
    _hardware(board, "XA", "XA-1")
    board.add_sheet("terminals", title="Terminals", kind="terminal", make_active=False)
    _assert_drawn_matches_model(board)
    scenes = [
        physical_scene_for_sheet(board.project, board.catalog, sheet)
        for sheet in board.project.sheets
        if sheet.kind == "terminal"
    ]
    assert len(scenes) >= 2
    blocks = [block for scene in scenes for block in scene["blocks"]]
    assert any(len(block.get("ground") or []) == 3 for block in blocks)
    assert any(block["kind"] == "end_stop" and len(block.get("marks") or []) == 2 for block in blocks)
    texts = [label["text"] for scene in scenes for label in scene["texts"] if label["role"] == "mark"]
    assert "END" in texts and "STOP" in texts and "PART" in texts
    assert any(block["ref"].startswith("XC-") for block in blocks)

    grown = Session.create("Grown")
    _place_strip(grown, "X1", 4)
    grown.open_terminal_strip("X1")
    for index in range(5, 45):
        grown.place("iec.terminals.x_2p5", ref=f"X1-{index}", x=40, y=20 + index * 8)
    _assert_drawn_matches_model(grown)
    path = tmp_path / "grown.pcad.yaml"
    grown.save(path)
    again = Session.open(path)
    _assert_drawn_matches_model(again)
