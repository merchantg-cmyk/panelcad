"""As-built reconstruction from real (synthetic phone-like) panel photos."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from panelcad import Session
from panelcad.cli.agent import OPS, Agent, run_agent
from panelcad.cli.main import app
from panelcad.core.checks import CHECK_IDS, run_checks
from panelcad.library.panel import is_layout_ready
from panelcad.reconstruct.fixtures import (
    write_backpanel_photo,
    write_drawing_scan,
    write_nameplate_photo,
)
from panelcad.reconstruct.geometry import mm_per_px, photo_to_mm
from panelcad.reconstruct.models import item_is_layout_ready
from panelcad.reconstruct.prints import classify_print_path, load_print_index
from panelcad.reconstruct.vision import PANEL_PHOTO_KIND, FakeVision, StubVision


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _photo_job(tmp_path: Path) -> tuple[Session, Path, dict]:
    _app()
    shot = tmp_path / "backpanel.png"
    meta = write_backpanel_photo(shot)
    job = tmp_path / "asbuilt.pcad.yaml"
    session = Session.create("As-built panel")
    session.save(job)
    session = Session.open(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    session.save(job)
    return session, job, meta


def _recordings(meta: dict) -> dict:
    region = meta["part_region"]
    w = float(meta["width_px"])
    h = float(meta["height_px"])
    return {
        "p1": {
            "devices": [
                {
                    "tag": "K1",
                    "brand": "LS Electric",
                    "part_number": "MC-9b",
                    "label_text": "MC-9b",
                    "nx": region["x"] / w,
                    "ny": region["y"] / h,
                    "nw": region["w"] / w,
                    "nh": region["h"] / h,
                    "confidence": 0.9,
                    "unreadable": False,
                }
            ],
            "strips": [
                {
                    "tag": "X1",
                    "terminals": [{"id": "1", "nx": 0.55, "ny": 0.35}],
                    "wire_numbers": [{"number": "104", "terminal": "1", "nx": 0.45, "ny": 0.52}],
                }
            ],
            "rails": [],
            "ducts": [],
            "texts": [],
            "unreadable": False,
        }
    }


def _face_scale(session, photo_id: str, meta: dict) -> dict:
    session.reconstruct_set_corners(photo_id, meta["corners"])
    return session.reconstruct_set_scale(
        photo_id,
        "face",
        meta["corners"][0],
        meta["corners"][1],
        known_mm=meta.get("enclosure_width_mm") or 400,
        known_height_mm=meta.get("enclosure_height_mm") or 500,
    )


def test_scale_from_din_rail_after_perspective(tmp_path: Path):
    session, _job, meta = _photo_job(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    payload = _face_scale(session, photo_id, meta)
    assert payload["scaled"] is True
    assert payload["face_mm"][0] == meta["enclosure_width_mm"]
    photo = session.project.reconstruct.photo(photo_id)
    ratio = mm_per_px(photo)
    assert ratio is not None
    assert ratio > 0
    mid = (
        (meta["rail_p1"][0] + meta["rail_p2"][0]) / 2.0,
        (meta["rail_p1"][1] + meta["rail_p2"][1]) / 2.0,
    )
    mm = photo_to_mm(photo, *mid)
    assert mm is not None
    assert mm[0] > 0
    assert mm[1] > 0


def test_scale_refuses_invented_duct_width(tmp_path: Path):
    session, _job, meta = _photo_job(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    try:
        session.reconstruct_set_scale(photo_id, "wire_duct", meta["rail_p1"], meta["rail_p2"])
    except ValueError as exc:
        assert "tech-entered" in str(exc) or "measured" in str(exc).lower() or "needs" in str(exc)
    else:
        raise AssertionError("duct scale must not invent a width")


def test_identify_matches_catalog_with_stubbed_vision(tmp_path: Path):
    session, _job, meta = _photo_job(tmp_path)
    result = session.reconstruct_identify(vision=FakeVision(_recordings(meta)))
    assert result["backend"] == "fake"
    parts = [item for item in result["items"] if item["role"] == "part"]
    assert parts
    item = parts[0]
    assert item["part_number"] == "MC-9b"
    assert item["status"] == "needs_info"
    assert item["layout_ready"] is False
    assert item["matches"]
    assert item["matches"][0]["catalog"] == "MC-9b"
    assert item["matches"][0]["manufacturer"] == "LS Electric"
    assert item["matches"][0]["confidence"] >= 0.85


def test_no_generic_placeholder_on_layout(tmp_path: Path):
    session, job, meta = _photo_job(tmp_path)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta)))
    item = next(row for row in session.reconstruct_unconfirmed()["items"] if row["role"] == "part")
    placed = session.reconstruct_place_confirmed()
    assert placed["placed"] == []
    assert any(row["reason"] == "unconfirmed" for row in placed["skipped"])
    findings = run_checks(session.project, session.catalog)
    assert any(row.check in {"reconstruct_unconfirmed", "reconstruct_needs_info"} for row in findings)
    assert "reconstruct_unconfirmed" in CHECK_IDS
    assert "reconstruct_needs_info" in CHECK_IDS

    session.reconstruct_confirm_item(item["id"], catalog="MC-9b", manufacturer="LS Electric", tag="K1")
    ready = session.reconstruct_status()["items"]
    confirmed = next(row for row in ready if row["id"] == item["id"])
    assert confirmed["layout_ready"] is True
    assert confirmed["status"] == "catalog"
    assert item_is_layout_ready(session.project.reconstruct.item(item["id"]))

    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    placed = session.reconstruct_place_confirmed()
    assert placed["placed"]
    ref = placed["placed"][0]["ref"]
    sheet = session.project.sheet(placed["placed"][0]["sheet"])
    assert sheet.kind == "panel_layout"
    comp = sheet.component(ref)
    assert is_layout_ready(session.catalog.get(comp.library_id), comp)
    assert comp.properties.get("dim_source") == "catalog"
    assert comp.properties.get("reconstruct_item") == item["id"]
    assert comp.properties.get("catalog") == "MC-9b"

    session.save(job)
    text = job.read_text(encoding="utf-8")
    assert "reconstruct:" in text
    assert "photos:" in text
    reopened = Session.open(job)
    assert reopened.project.reconstruct.photos
    crop = reopened.reconstruct_photo_region(ref=ref)
    assert crop["photo"]["id"] == photo_id
    assert Path(crop["path"]).is_file()


def test_confirmed_envelope_is_layout_ready(tmp_path: Path):
    session, _job, meta = _photo_job(tmp_path)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta)))
    item = next(row for row in session.reconstruct_unconfirmed()["items"] if row["role"] == "part")
    confirmed = session.reconstruct_confirm_item(
        item["id"],
        envelope_w_mm=45,
        envelope_h_mm=73.5,
        envelope_d_mm=87.4,
        mount="din_rail",
        library_id="iec.contactors.ac3_9a",
        tag="K9",
    )
    assert confirmed["status"] == "envelope"
    assert confirmed["layout_ready"] is True
    assert confirmed["envelope_confirmed"] is True


def test_checklist_trace_and_inferred_wire(tmp_path: Path):
    session, _job, meta = _photo_job(tmp_path)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta)))
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(item["id"], catalog="MC-9b", tag="K1")
    session.reconstruct_checklist()
    nxt = session.reconstruct_next_question()
    assert nxt["done"] is False
    steps = {step["kind"]: step for step in session.reconstruct_checklist()["checklist"]}
    assert "trace_wire" in steps
    trace = next(step for step in session.reconstruct_checklist()["checklist"] if step["kind"] == "trace_wire")
    assert "104" in trace["prompt"]
    answered = session.reconstruct_answer(trace["id"], "K1:A1")
    assert answered["status"] == "answered"
    assert answered["to_ref"]
    wires = [wire for sheet in session.project.sheets for wire in sheet.wires if wire.inferred]
    assert wires
    assert any(wire.caption().startswith("?") for wire in wires)
    findings = run_checks(session.project, session.catalog)
    assert any(row.check == "reconstruct_inferred_wire" for row in findings)
    session.reconstruct_confirm_connection(wire_id=wires[0].id)
    assert wires[0].inferred is False


def test_compare_secondary_print(tmp_path: Path):
    session, _job, meta = _photo_job(tmp_path)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta)))
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(item["id"], catalog="MC-9b", tag="K1")
    print_yaml = tmp_path / "old-print.yaml"
    print_yaml.write_text(
        """
kind: print_index
tags:
  - {tag: K1, catalog: MC-12b, manufacturer: LS Electric}
  - {tag: Q1, catalog: BKN 3P C6A}
wires:
  - {number: "104", from: K1:A1, to: X1:1}
  - {number: "201", from: K1:A2, to: X1:2}
""",
        encoding="utf-8",
    )
    result = session.reconstruct_compare_print(print_yaml)
    assert result["extracted"] is True
    checks = {row["check"] for row in result["mismatches"]}
    assert "reconstruct_print_catalog_mismatch" in checks
    assert "reconstruct_print_missing_on_print" in checks or "reconstruct_print_missing_on_photos" in checks
    assert "reconstruct_print_wire_missing" in checks


def test_session_cli_agent_parity(tmp_path: Path):
    _app()
    shot = tmp_path / "panel.png"
    write_backpanel_photo(shot)
    job = tmp_path / "parity.pcad.yaml"
    session = Session.create("Parity")
    session.save(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    session.save(job)

    runner = CliRunner()
    listed = runner.invoke(app, ["reconstruct", "photos", str(job)])
    assert listed.exit_code == 0, listed.output
    assert "p1" in listed.output

    agent = Agent()
    opened = agent.handle({"id": 1, "op": "open", "path": str(job)})
    assert opened["ok"], opened
    tagged = agent.handle({"id": 2, "op": "reconstruct_tag_photo", "photo_id": "p1", "role": "nameplate"})
    assert tagged["ok"], tagged
    assert tagged["result"]["role"] == "nameplate"
    help_ops = agent.handle({"id": 3, "op": "help"})
    assert help_ops["ok"]
    assert "reconstruct_next_question" in help_ops["result"]["ops"]
    assert "reconstruct_add_photos" in OPS

    stdin = StringIO(
        json.dumps({"id": 1, "op": "open", "path": str(job)})
        + "\n"
        + json.dumps({"id": 2, "op": "reconstruct_unconfirmed"})
        + "\n"
    )
    stdout = StringIO()
    run_agent(stdin, stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert lines[0]["ok"]
    assert lines[1]["ok"]


def test_drawing_agent_exposes_reconstruct_tools():
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from panelcad.ai.drawing import drawing_agent, open_drawing

    seen: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(tool.name for tool in info.function_tools)
        return ModelResponse(parts=[TextPart(content="ready")])

    drawing = open_drawing(name="Reconstruct tools")
    with drawing_agent.override(model=FunctionModel(model)):
        drawing_agent.run_sync("List reconstruct tools.", deps=drawing)
    assert {
        "reconstruct_add_photos",
        "reconstruct_next_question",
        "reconstruct_answer",
        "reconstruct_photo_region",
        "reconstruct_unconfirmed",
        "reconstruct_identify",
        "reconstruct_confirm_item",
        "reconstruct_place_confirmed",
        "reconstruct_place_needs_info",
        "reconstruct_set_corners",
        "reconstruct_set_scale",
        "reconstruct_compare_print",
    } <= set(seen)


def test_stub_vision_does_not_guess(tmp_path: Path):
    session, _job, _meta = _photo_job(tmp_path)
    result = session.reconstruct_identify(vision=StubVision())
    assert result["items"] == [] or all(item.get("unreadable") for item in result["items"])


def test_wizard_lists_added_photos(tmp_path: Path):
    from panelcad.gui.reconstruct_wizard import ReconstructWizard

    session, _job, _meta = _photo_job(tmp_path)
    app = _app()
    dialog = ReconstructWizard(session)
    dialog.show()
    app.processEvents()
    assert dialog.photo_list.count() == 1
    dialog.close()


def test_layout_ready_helper_matches_envelope_flag():
    from panelcad.core.models import Component

    from panelcad.library.catalog import default_catalog

    device = default_catalog().get("iec.contactors.ac3_9a")
    generic = Component(id="A", library_id="iec.contactors.ac3_9a", x=0, y=0)
    assert is_layout_ready(device, generic) is False
    catalog = Component(
        id="B",
        library_id="iec.contactors.ac3_9a",
        x=0,
        y=0,
        properties={"dim_source": "catalog", "width_mm": 45, "height_mm": 73.5, "depth_mm": 87.4},
    )
    assert is_layout_ready(device, catalog) is False
    from panelcad import Session

    ready_device = Session.create("Ready").catalog.get("adc.stride.sio_mb12cdr")
    assert is_layout_ready(ready_device) is True
    envelope = Component(
        id="C",
        library_id="iec.contactors.ac3_9a",
        x=0,
        y=0,
        properties={
            "dim_source": "measured",
            "envelope_confirmed": "yes",
            "width_mm": 40,
            "height_mm": 80,
            "depth_mm": 90,
            "mount": "din_rail",
        },
    )
    assert is_layout_ready(device, envelope) is True


def test_fixture_photos_are_perspective_warped_and_noisy(tmp_path: Path):
    """Fixtures stand in for phone shots: warped quad, speckle, not a flat scan."""

    _app()
    dest = tmp_path / "backpanel.png"
    meta = write_backpanel_photo(dest)
    corners = meta["corners"]
    assert len(corners) == 4
    top_dy = abs(corners[0][1] - corners[1][1])
    bot_dy = abs(corners[3][1] - corners[2][1])
    left_dx = abs(corners[0][0] - corners[3][0])
    assert top_dy > 1 or bot_dy > 1 or left_dx > 1
    assert min(c[0] for c in corners) > 8
    assert min(c[1] for c in corners) > 8
    from PySide6.QtGui import QImage

    image = QImage(str(dest))
    assert not image.isNull()
    # JPEG + grain: the panel face is not a single flat CAD fill.
    samples = [image.pixelColor(220 + i * 18, 170 + (i * 11) % 80).rgb() for i in range(16)]
    assert len(set(samples)) > 4
    close = tmp_path / "nameplate.png"
    plate = write_nameplate_photo(close)
    assert plate["role"] == "nameplate"
    assert plate["kind"] == "panel_photo"
    scan = tmp_path / "old-ladder.png"
    drawing = write_drawing_scan(scan)
    assert drawing["kind"] == "drawing_scan"
    assert drawing["corners"][0] == (0, 0)


def test_drawing_scan_is_secondary_print_not_photo_input(tmp_path: Path):
    session, _job, meta = _photo_job(tmp_path)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta)))
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(item["id"], catalog="MC-9b", tag="K1")

    scan = tmp_path / "old-ladder.png"
    write_drawing_scan(scan)
    assert classify_print_path(scan) == "scan"
    attached = session.reconstruct_attach_print(scan)
    assert attached["kind"] == "scan"
    photos = {row["id"] for row in session.reconstruct_list_photos()["photos"]}
    assert attached["id"] not in photos
    assert attached["path"].startswith("prints/") or "prints" in attached["path"]

    # Without the print-import pipeline a scan stays unextracted; photos stay authoritative.
    compared = session.reconstruct_compare_print()
    assert compared["extracted"] is False
    assert any(row["check"] == "reconstruct_print_unextracted" for row in compared["mismatches"])
    photo_items = [row for row in session.reconstruct_status()["items"] if row["role"] == "part"]
    assert all(row["part_number"] == "MC-9b" for row in photo_items)


def test_pdf_is_rejected_as_panel_photo(tmp_path: Path):
    session, job, _meta = _photo_job(tmp_path)
    fake_pdf = tmp_path / "old-print.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n")
    try:
        session.reconstruct_add_photos([fake_pdf], role="backpanel")
    except ValueError as exc:
        assert "That's a PDF" in str(exc)
        assert "Old drawing" in str(exc)
    else:
        raise AssertionError("a PDF must not enter the panel-photo intake")
    attached = session.reconstruct_attach_print(fake_pdf)
    assert attached["kind"] == "pdf"
    assert attached["extracted"] is False


def test_identify_sends_photo_kind_and_ignores_prints(tmp_path: Path):
    session, _job, meta = _photo_job(tmp_path)
    scan = tmp_path / "old-ladder.png"
    write_drawing_scan(scan)
    session.reconstruct_attach_print(scan)
    seen: list[dict] = []

    def spy(work: dict) -> dict:
        seen.append(dict(work))
        return _recordings(meta)["p1"]

    from panelcad.reconstruct.vision import CallableVision

    result = session.reconstruct_identify(vision=CallableVision(spy))
    assert seen
    assert all(row.get("kind") == PANEL_PHOTO_KIND for row in seen)
    assert all(Path(str(row.get("image") or "")).suffix.lower() != ".pdf" for row in seen)
    assert all("prints/" not in str(row.get("image") or "") for row in seen)
    assert any(item["part_number"] == "MC-9b" for item in result["items"])


def test_print_import_pipeline_reused_for_secondary_scan(tmp_path: Path, monkeypatch):
    import panelcad.importer.pipeline as pipeline_mod

    _app()
    scan = tmp_path / "old-ladder.png"
    write_drawing_scan(scan)
    called: dict[str, str] = {}

    def extract_print(path):
        called["path"] = str(path)
        return {
            "kind": "scan",
            "extracted": True,
            "extractor": "importer.pipeline",
            "tags": [{"tag": "K1", "catalog": "MC-12b"}],
            "wires": [{"number": "201"}],
        }

    monkeypatch.setattr(pipeline_mod, "extract_print", extract_print)

    index = load_print_index(scan)
    assert called["path"] == str(scan)
    assert index["extracted"] is True
    assert index["extractor"] == "importer.pipeline"
    assert index["tags"][0]["catalog"] == "MC-12b"


def test_extract_print_reads_vector_pdf_tags(tmp_path: Path):
    """Reconstruct's secondary-print path calls importer.pipeline.extract_print."""

    import pymupdf

    from panelcad.importer.pipeline import extract_print

    pt = 25.4 / 72.0
    path = tmp_path / "nema-ladder.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=420 / pt, height=297 / pt)

    def mm(x, y):
        return pymupdf.Point(x / pt, y / pt)

    page.draw_line(mm(40, 30), mm(40, 180), color=(0, 0, 0), width=1.2)
    page.draw_line(mm(220, 30), mm(220, 180), color=(0, 0, 0), width=1.2)
    page.draw_line(mm(40, 55), mm(220, 55), color=(0, 0, 0), width=0.8)
    page.draw_circle(mm(88, 55), 8 / pt, color=(0, 0, 0), width=0.8)
    page.insert_text(mm(80, 40), "PB1", fontsize=9, color=(0, 0, 0))
    page.insert_text(mm(50, 51), "101", fontsize=9, color=(0, 0, 0))
    page.insert_text(mm(40, 250), "DWG NO: N-TEST-1", fontsize=8, color=(0, 0, 0))
    doc.save(path)
    doc.close()

    payload = extract_print(path)
    assert payload["extractor"] == "importer.pipeline"
    tags = {str(row.get("tag") or "") for row in payload.get("tags") or []}
    assert "PB1" in tags
    assert payload["extracted"] is True

    index = load_print_index(path)
    assert index["extracted"] is True
    assert any(str(row.get("tag") or "") == "PB1" for row in index.get("tags") or [])


def test_extract_print_png_does_not_raise_without_tesseract(tmp_path: Path):
    from PIL import Image

    from panelcad.importer.pipeline import extract_print

    scan = tmp_path / "blank-scan.png"
    Image.new("RGB", (640, 480), (255, 255, 255)).save(scan)
    payload = extract_print(scan)
    assert payload["kind"] == "scan"
    assert "tags" in payload
    assert payload["extractor"] == "importer.pipeline"

    index = load_print_index(scan)
    assert index["kind"] == "scan"
    assert isinstance(index.get("tags"), list)


def test_unreadable_glare_does_not_invent_parts(tmp_path: Path):
    session, _job, _meta = _photo_job(tmp_path)
    glare = {
        "p1": {
            "devices": [
                {
                    "tag": "",
                    "brand": "",
                    "part_number": "",
                    "label_text": "",
                    "unreadable": True,
                    "confidence": 0.1,
                }
            ],
            "rails": [],
            "ducts": [],
            "strips": [],
            "texts": [],
            "unreadable": True,
        }
    }
    result = session.reconstruct_identify(vision=FakeVision(glare))
    parts = [item for item in result["items"] if item["role"] == "part"]
    assert all(item.get("unreadable") or not item.get("part_number") for item in parts)
    assert all(item.get("layout_ready") is False for item in parts)
