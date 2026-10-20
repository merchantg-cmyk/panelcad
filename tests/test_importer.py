"""Print importer: each stage, both human and agent paths, and round-trip accuracy.

Vision in these tests is a recorded fake or the external submit protocol.
Scan scores come from the pixel reader with the PDF closed.
A live model is probed once and never required.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from panelcad.examples.dol import build_dol
from panelcad.importer.accuracy import score_projects, score_sheet_pair
from panelcad.importer.extract import normalize_submission, slice_facts
from panelcad.importer.imageutil import _bottom_ink, auto_rotate, degrade_scan, deskew
from panelcad.importer.ingest import rasterize_pdf
from panelcad.importer.parts import match_rows
from panelcad.importer.pipeline import ImportJob
from panelcad.importer.stitch import stitch_pages
from panelcad.importer.tile import draw_tile_overlay, merge_extractions, tile_page
from panelcad.importer.vision import probe_model
from panelcad.library.parts import default_parts_catalog

def _writable_artifact_dir() -> Path:
    """Screenshot dumps follow the import assertions.

    The cloud artifact store can reject creates (expired token) after the
    directory itself still stats. A dead mount must not fail the round trip.
    """

    primary = Path("/opt/cursor/artifacts/print-import")
    try:
        primary.mkdir(parents=True, exist_ok=True)
        probe = primary / ".write-probe"
        probe.write_bytes(b"ok")
        probe.unlink(missing_ok=True)
        return primary
    except OSError:
        fallback = Path(os.environ.get("PANELCAD_ARTIFACTS_DIR") or "/tmp/panelcad-artifacts")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


ART = _writable_artifact_dir()
EMPTY = {"devices": [], "wires": [], "xrefs": [], "texts": []}


def _white(path: Path, size: tuple[int, int] = (640, 400)) -> Path:
    Image.new("RGB", size, (255, 255, 255)).save(path)
    return path


def _inked_scan(path: Path) -> Path:
    """A scan with a mark inside the mocked K1 box, held upright by a title band."""

    width, height = 640, 400
    page_w, page_h = 420.0, 297.0

    def px(x_mm: float, y_mm: float) -> tuple[float, float]:
        return (x_mm / page_w * width, y_mm / page_h * height)

    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    x0, y0 = px(14, 22)
    x1, y1 = px(46, 62)
    draw.rectangle((x0, y0, x1, y1), outline=(15, 15, 15), width=4)
    cx, cy = ((x0 + x1) / 2, (y0 + y1) / 2)
    draw.ellipse((cx - 14, cy - 14, cx + 14, cy + 14), outline=(15, 15, 15), width=4)
    draw.line((x0, cy, x1, cy), fill=(15, 15, 15), width=3)
    draw.text((x0 + 6, y0 + 4), "K1", fill=(15, 15, 15), font=_font(22, bold=True))
    ax, ay = px(40.4, 70.3)
    bx, by = px(47.6, 73.6)
    draw.rectangle((ax, ay, bx, by), fill=(20, 20, 80))
    # A second mark in another tile, so a later vision call can fail on its own.
    mx, my = px(250, 70)
    draw.rectangle((mx, my, mx + 36, my + 28), outline=(15, 15, 15), width=4)
    draw.rectangle((8, int(height * 0.86), width - 8, int(height * 0.96)), fill=(20, 20, 20))
    image.save(path)
    return path


def _dark_fraction(pix) -> float:
    from PySide6.QtGui import QImage

    image = pix.toImage().convertToFormat(QImage.Format.Format_RGB32)
    if image.width() < 1 or image.height() < 1:
        return 0.0
    dark = 0
    total = 0
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            color = image.pixel(x, y)
            total += 1
            if ((color >> 16) & 255) < 180 and ((color >> 8) & 255) < 180 and (color & 255) < 180:
                dark += 1
    return dark / max(total, 1)


def _drive(job: ImportJob, handler) -> dict:
    for _ in range(80):
        status = job.status()
        if not status["pending"]:
            return status
        work = status["work"]
        job.submit(str(work["id"]), handler(work))
    raise AssertionError(f"import still pending at {job.manifest.get('pending')}")


def _tags(session) -> set[str]:
    found = set()
    for sheet in session.project.sheets:
        for comp in sheet.components:
            found.add(comp.id)
    return found


@pytest.fixture(scope="module")
def dol(tmp_path_factory):
    root = tmp_path_factory.mktemp("dol-print")
    session = build_dol()
    pdf = root / "dol.pdf"
    session.export_pdf(pdf)
    project = root / "dol.pcad.yaml"
    session.save(project)
    return {"session": session, "pdf": pdf, "project": project, "root": root}


def test_auto_rotate_restores_the_title_band():
    image = Image.new("RGB", (480, 320), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 278, 472, 314), fill=(0, 0, 0))
    turned = image.transpose(Image.Transpose.ROTATE_270)
    fixed, quarters = auto_rotate(turned)
    assert quarters == 3
    assert fixed.size[0] > fixed.size[1]
    assert _bottom_ink(fixed) > _bottom_ink(turned) + 0.02


def test_deskew_undoes_a_small_tilt():
    image = Image.new("RGB", (720, 400), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for y in (36, 96, 156, 216, 276, 340):
        draw.rectangle((16, y, 700, y + 5), fill=(0, 0, 0))
    tilted = image.rotate(2.0, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=(255, 255, 255))
    _fixed, angle = deskew(tilted)
    assert abs(angle - (-2.0)) <= 0.51


def test_parts_match_exact_catalog_only():
    parts = default_parts_catalog()
    rows = match_rows(
        [
            {"catalog": "LC1D09BD", "tag": "K1", "qty": "1", "confidence": 0.9},
            {"catalog": "lc1d09bd", "tag": "K2", "qty": "1"},
            {"catalog": "LC1D09", "tag": "K3", "qty": "1"},
            {"catalog": "NOT-A-PART", "tag": "K4", "qty": "1"},
            {"catalog": "???", "tag": "K5", "unreadable": True},
        ],
        parts,
    )
    by_tag = {row["tag"]: row for row in rows}
    assert by_tag["K1"]["matched"] is True
    assert by_tag["K1"]["library_id"] == "iec.contactors.ac3_9a"
    assert by_tag["K1"]["catalog"] == "LC1D09BD"
    assert by_tag["K2"]["matched"] is True
    assert by_tag["K3"]["matched"] is False
    assert by_tag["K3"]["flag"] == "unknown"
    assert by_tag["K3"]["catalog"] == "LC1D09"
    assert by_tag["K4"]["flag"] == "unknown"
    assert by_tag["K4"]["catalog"] == "NOT-A-PART"
    assert by_tag["K5"]["flag"] == "unreadable"
    assert by_tag["K5"]["catalog"] == ""


def test_unreadable_text_is_not_invented():
    piece = normalize_submission(
        {
            "devices": [{"tag": "K9", "unreadable": True, "x": 12, "y": 14, "type": "contactor"}],
            "wires": [{"number": "", "x": 20, "y": 20}],
            "texts": [{"text": "motor", "unreadable": True}],
        }
    )
    assert piece["devices"][0]["tag"] == ""
    assert piece["devices"][0]["type"] == ""
    assert piece["devices"][0]["unreadable"] is True
    assert piece["wires"][0]["number"] == ""
    assert piece["texts"][0]["text"] == ""


def test_overlap_tiles_collapse_to_one_device():
    device = {"tag": "Q1", "x": 100, "y": 40, "confidence": 0.7}
    again = {"tag": "Q1", "x": 104, "y": 42, "confidence": 0.95}
    apart = {"tag": "Q1", "x": 200, "y": 40, "confidence": 0.8}
    merged = merge_extractions(
        [
            {"devices": [device], "wires": [{"number": "101", "x": 90, "y": 50, "confidence": 0.5}]},
            {"devices": [again, apart], "wires": [{"number": "101", "x": 92, "y": 51, "confidence": 0.9}]},
        ]
    )
    assert len(merged["devices"]) == 2
    near = next(item for item in merged["devices"] if abs(item["x"] - 104) < 1)
    assert near["confidence"] == 0.95
    assert len(merged["wires"]) == 1
    assert merged["wires"][0]["confidence"] == 0.9


def test_unresolved_cross_reference_stays_unresolved():
    stitched = stitch_pages(
        [
            {
                "id": "page_001",
                "width_mm": 420,
                "height_mm": 297,
                "classification": {"kind": "schematic_power", "title": "Power", "standard": "IEC", "drawing_number": "E-001", "rev": "A"},
                "merged": {
                    "devices": [{"tag": "K1", "x": 40, "y": 40, "confidence": 0.9}],
                    "wires": [],
                    "xrefs": [{"kind": "offpage", "text": "TO Z9", "tag": "K1", "target": "Z9", "confidence": 0.8}],
                },
            }
        ]
    )
    assert stitched["title_block"]["drawing_number"] == "E-001"
    assert stitched["title_block"]["rev"] == "A"
    assert stitched["unresolved_xrefs"]
    assert stitched["unresolved_xrefs"][0]["target"] == "Z9"


def test_external_agent_sees_one_crop_and_builds(tmp_path: Path):
    image = _white(tmp_path / "scan.png")
    job = ImportJob.start([image], tmp_path / "ws", backend="external")
    status = job.status()
    assert status["pending"] is True
    work = status["work"]
    assert work["kind"] == "classify"
    assert Path(work["image"]).is_file()
    assert "schema" in work and work["schema"]["type"] == "object"
    assert "one" in work["instructions"].lower() or "crop" in work["instructions"].lower()
    assert "known_tags" in work["summary"]
    resumed = ImportJob.resume(tmp_path / "ws", backend="external")
    assert resumed.manifest["pending"] == work["id"]

    def handler(item: dict) -> dict:
        if item["kind"] == "classify":
            return {
                "kind": "schematic_power",
                "title": "Power",
                "drawing_number": "E-010",
                "rev": "B",
                "standard": "IEC",
                "confidence": 0.8,
            }
        if item["tile_id"] == "tile_00":
            return {
                "devices": [
                    {"tag": "Q1", "type": "breaker", "x": 40, "y": 30, "confidence": 0.92},
                    {"tag": "ZZ9", "unreadable": True, "x": 70, "y": 30, "type": "relay"},
                ],
                "wires": [{"number": "L1", "x": 48, "y": 36, "confidence": 0.88}],
                "xrefs": [],
                "texts": [],
            }
        return EMPTY

    done = _drive(resumed, handler)
    assert done["pending"] is False
    built = resumed.build(out=tmp_path / "built.pcad.yaml")
    assert (tmp_path / "built.pcad.yaml").is_file()
    assert "Q1" in _tags(built)
    assert "ZZ9" not in _tags(built)
    assert built.project.title_block.drawing_number == "E-010"
    assert built.project.title_block.revisions[0].rev == "B"
    q1 = built.project.sheet("power").component("Q1")
    assert q1.properties["print_import_crop"]
    assert Path(q1.properties["print_import_crop"]).is_file()


def test_bom_matches_exact_numbers_and_flags_the_rest(tmp_path: Path):
    first = _white(tmp_path / "schematic.png")
    second = _white(tmp_path / "bom.png")
    job = ImportJob.start([first, second], tmp_path / "ws", backend="external")

    def handler(item: dict) -> dict:
        if item["kind"] == "classify":
            if item["page_id"] == "page_002":
                return {"kind": "bom", "title": "Parts list", "confidence": 0.85}
            return {"kind": "schematic_control", "title": "Control", "standard": "IEC", "confidence": 0.9}
        if item["kind"] == "bom":
            return {
                "rows": [
                    {"qty": "1", "catalog": "LC1D09BD", "description": "contactor", "tag": "K1", "confidence": 0.95},
                    {"qty": "1", "catalog": "NOT-A-PART", "description": "mystery", "tag": "K9", "confidence": 0.6},
                    {"qty": "1", "catalog": "LC1D09", "description": "partial", "tag": "K8", "confidence": 0.4},
                ]
            }
        if item["page_id"] == "page_001" and item["tile_id"] == "tile_00":
            return {"devices": [{"tag": "K1", "type": "contactor", "x": 50, "y": 40, "confidence": 0.9}], "wires": [], "xrefs": [], "texts": []}
        return EMPTY

    _drive(job, handler)
    built = job.build()
    comp = built.project.sheet("control").component("K1")
    assert comp.library_id == "iec.contactors.ac3_9a"
    assert comp.properties["catalog"] == "LC1D09BD"
    assert comp.properties["manufacturer"]
    flags = {
        item["text"]: item["reason"]
        for item in built.project.properties["print_import"]["items"]
        if item.get("reason") == "unknown"
    }
    assert flags["NOT-A-PART"] == "unknown"
    assert flags["LC1D09"] == "unknown"
    assert "K9" not in _tags(built)


def test_photos_propose_a_layout_and_leave_the_schematic(tmp_path: Path):
    image = _white(tmp_path / "sheet.png")
    photo = _white(tmp_path / "panel.jpg", (320, 240))
    job = ImportJob.start([image], tmp_path / "ws", backend="external")

    def handler(item: dict) -> dict:
        if item["kind"] == "classify":
            return {"kind": "schematic_power", "title": "Power", "standard": "IEC", "confidence": 0.9}
        if item["kind"] == "photo":
            return {"devices": [{"tag": "K1", "nx": 0.25, "ny": 0.8, "confidence": 0.7}], "rails": [], "ducts": [], "strips": []}
        if item["tile_id"] == "tile_00":
            return {
                "devices": [{"tag": "K1", "type": "contactor", "x": 42, "y": 55, "confidence": 0.93}],
                "wires": [],
                "xrefs": [],
                "texts": [],
            }
        return EMPTY

    _drive(job, handler)
    status = job.add_photos(photo)
    assert status["pending"] is True
    assert status["work"]["kind"] == "photo"
    _drive(job, handler)
    built = job.build()
    schematic = built.project.sheet("power").component("K1")
    assert abs(schematic.x - 42) < 1.0
    assert abs(schematic.y - 55) < 1.0
    layout = next(sheet for sheet in built.project.sheets if sheet.kind == "panel_layout")
    assert all(comp.library_id != schematic.library_id for comp in layout.components)
    review = built.project.properties["print_import"]["items"]
    assert any(item.get("text") == "K1" and item.get("reason") == "layout_not_ready" for item in review)


def test_review_can_reject_fix_and_rerun_one_tile(tmp_path: Path):
    image = _white(tmp_path / "scan.png")

    def handler(item: dict) -> dict:
        if item["kind"] == "classify":
            return {"kind": "schematic_power", "title": "Power", "standard": "IEC", "confidence": 0.8}
        if item["tile_id"] == "tile_00":
            return {
                "devices": [
                    {"tag": "Q1", "x": 36, "y": 28, "confidence": 0.9},
                    {"tag": "K1", "x": 80, "y": 28, "confidence": 0.4},
                ],
                "wires": [{"number": "101", "x": 50, "y": 40, "confidence": 0.85}],
                "xrefs": [],
                "texts": [],
            }
        return EMPTY

    # Fake with no recordings marks the page unreadable. Drive it externally instead.
    job = ImportJob.start([image], tmp_path / "ws2", backend="external")
    _drive(job, handler)
    items = job.review_items()
    k1 = next(item for item in items if item["text"] == "K1")
    q1 = next(item for item in items if item["text"] == "Q1")
    wire = next(item for item in items if item["kind"] == "wire" and item["text"] == "101")
    assert wire["payload"]["ambiguous"] is True
    assert wire["confidence"] < 0.6
    job.decide(k1["id"], "reject")
    job.decide(q1["id"], "fix", "Q1A")
    with pytest.raises(ValueError):
        job.decide(q1["id"], "fix", None)
    built = job.build()
    assert "K1" not in _tags(built)
    assert "Q1A" in _tags(built)
    assert "Q1" not in _tags(built)
    assert not any(w.number == "101" for sheet in built.project.sheets for w in sheet.wires)

    again = ImportJob.resume(tmp_path / "ws2", backend="external")
    again.rerun(tile_id="tile_00")
    status = again.status()
    assert status["pending"] is True
    assert status["work"]["id"] == "page_001:tile_00"
    assert status["work"]["kind"] == "tile"


def test_jsonl_ops_round_trip_a_vector_print(dol, tmp_path: Path):
    from panelcad.cli.agent import OPS, Agent

    for name in ("import_start", "import_next", "import_submit", "import_status", "import_build", "import_add_photos"):
        assert name in OPS
    agent = Agent()
    started = agent.handle(
        {
            "id": "req-1",
            "op": "import_start",
            "path": str(dol["pdf"]),
            "workspace": str(tmp_path / "ws"),
            "backend": "external",
        }
    )
    assert started["ok"] is True
    assert started["id"] == "req-1"
    assert started["result"]["pending"] is False
    assert started["result"]["stage"] == "ready"
    photo = _white(tmp_path / "door.png", (200, 160))
    added = agent.handle({"id": "req-2", "op": "import_add_photos", "path": str(photo)})
    assert added["ok"] is True
    assert added["result"]["pending"] is True
    assert added["result"]["work"]["kind"] == "photo"
    nxt = agent.handle({"id": "req-3", "op": "import_next"})
    assert nxt["result"]["work"]["id"] == added["result"]["work"]["id"]
    submitted = agent.handle(
        {
            "id": "req-4",
            "op": "import_submit",
            "data": {"devices": [{"tag": "NO-SUCH", "nx": 0.5, "ny": 0.5, "confidence": 0.2}]},
        }
    )
    assert submitted["ok"] is True
    assert submitted["result"]["pending"] is False
    status = agent.handle({"id": "req-5", "op": "import_status"})
    assert status["result"]["summary"]["known_tags"]
    built = agent.handle({"id": "req-6", "op": "import_build", "path": str(tmp_path / "out.pcad.yaml")})
    assert built["ok"] is True
    assert built["result"]["sheets"] >= 6
    assert (tmp_path / "out.pcad.yaml").is_file()
    assert agent.session is not None
    assert "Q1" in _tags(agent.session)


def test_cli_imports_a_print_and_a_project(dol, tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    runner = CliRunner()
    out = tmp_path / "from-print.pcad.yaml"
    printed = runner.invoke(
        app,
        ["import", str(dol["pdf"]), "--external", "--workspace", str(tmp_path / "ws"), "--out", str(out)],
    )
    assert printed.exit_code == 0, printed.output
    assert out.is_file()
    assert "imported print" in printed.output

    paused = runner.invoke(
        app,
        ["import", str(_white(tmp_path / "scan.png")), "--external", "--workspace", str(tmp_path / "scan-ws")],
    )
    assert paused.exit_code == 2, paused.stdout
    body = json.loads(paused.stdout)
    assert body["paused"] is True
    assert body["work"]["kind"] == "classify"
    assert paused.stderr.startswith("summary tiles ")

    legacy = tmp_path / "legacy.pcad.yaml"
    loaded = runner.invoke(app, ["import", str(dol["project"]), "--out", str(legacy)])
    assert loaded.exit_code == 0, loaded.output
    assert legacy.is_file()
    assert "imported" in loaded.output


def test_cli_import_summary_exits_zero_and_a_real_failure_does_not(dol, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A saved drawing exits 0 with counts. A missing file exits non-zero with a reason."""

    from typer.testing import CliRunner

    from panelcad.cli.main import app
    from panelcad.importer.pipeline import ImportJob

    runner = CliRunner()
    out = tmp_path / "counted.pcad.yaml"
    printed = runner.invoke(
        app,
        ["import", str(dol["pdf"]), "--external", "--workspace", str(tmp_path / "count-ws"), "--out", str(out)],
    )
    text = printed.output + (printed.stderr or "")
    assert printed.exit_code == 0, text
    assert out.is_file()
    assert "imported print" in printed.output
    for label in ("devices ", "wires ", "needs-info ", "unparseable ", "tiles read ", "skipped ", "cost $"):
        assert label in printed.output, text
    assert "import failed" not in text
    from panelcad.api.session import Session

    saved = Session.open(out)
    drawing_devices = sum(len(sheet.components) for sheet in saved.project.sheets)
    drawing_wires = sum(len(sheet.wires) for sheet in saved.project.sheets)
    assert f"devices {drawing_devices}  " in printed.output
    assert f"wires {drawing_wires}  " in printed.output
    from panelcad.reconstruct.placeholder import is_needs_info_component

    drawing_needs = sum(
        1
        for sheet in saved.project.sheets
        for comp in sheet.components
        if is_needs_info_component(comp)
    )
    assert f"needs-info {drawing_needs}  " in printed.output

    missing = runner.invoke(
        app,
        ["import", str(tmp_path / "missing.pdf"), "--out", str(tmp_path / "nope.pcad.yaml")],
    )
    missed = missing.output + (missing.stderr or "")
    assert missing.exit_code != 0
    assert "import failed: file not found:" in missed
    assert "missing.pdf" in missed
    assert "imported print" not in missed
    assert "Traceback" not in missed

    def explode(self, session):
        raise RuntimeError("preview exploded")

    monkeypatch.setattr(ImportJob, "_stamp_rebuilt", explode)
    warned = runner.invoke(
        app,
        ["import", str(dol["pdf"]), "--external", "--workspace", str(tmp_path / "warn-ws"), "--out", str(tmp_path / "warned.pcad.yaml")],
    )
    warned_text = warned.output + (warned.stderr or "")
    assert warned.exit_code == 0, warned_text
    assert (tmp_path / "warned.pcad.yaml").is_file()
    assert "imported print" in warned.output
    assert "devices " in warned.output
    assert "import warning: preview exploded" in warned_text


def test_saved_import_does_not_start_qt(dol, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Building the project must not construct a QApplication."""

    from PySide6.QtWidgets import QApplication

    from panelcad.importer.pipeline import ImportJob, _preview_paint_allowed

    monkeypatch.setattr(QApplication, "instance", staticmethod(lambda: None))
    assert _preview_paint_allowed() is False
    painted: list[str] = []

    def deny(*_args, **_kwargs):
        painted.append("paint")
        raise AssertionError("preview paint")

    monkeypatch.setattr("panelcad.importer.preview.attach_rebuilt_previews", deny)
    job = ImportJob.start([dol["pdf"]], tmp_path / "qt-ws", backend="external")
    built = job.build(out=tmp_path / "qt.pcad.yaml")
    assert built.project.sheets
    assert (tmp_path / "qt.pcad.yaml").is_file()
    assert painted == []
    assert job.summary()["devices"] > 0


def test_wizard_paints_rebuilt_previews_after_the_worker(dol, tmp_path: Path):
    """The worker does not paint. The wizard paints on the GUI thread once it finishes."""

    from PySide6.QtWidgets import QApplication

    from panelcad.gui.import_wizard import ImportWizard, _ImportThread
    from panelcad.importer.pipeline import ImportJob

    QApplication.instance() or QApplication([])
    job = ImportJob.prepare([dol["pdf"]], tmp_path / "wizard-ws", backend="external")
    thread = _ImportThread(job)
    thread.start()
    assert thread.wait(180_000)
    assert thread.built is not None
    before = [item for item in job.review_items() if item.get("kind") in {"device", "wire"}]
    assert before
    assert not any(item.get("rebuilt") for item in before)

    wizard = ImportWizard(workspace=tmp_path / "wizard-ws")
    wizard.job = job
    wizard._thread = thread
    wizard._import_ready()

    after = [item for item in job.review_items() if item.get("kind") in {"device", "wire"}]
    assert after
    assert all(item.get("rebuilt") and Path(item["rebuilt"]).is_file() for item in after)


def test_summary_counts_follow_the_saved_drawing_not_repeated_report_rows(tmp_path: Path):
    """A contactor-to-overload link can be listed more than once. The drawing keeps one wire."""

    from panelcad.api.session import Session

    session = Session.create("Count")
    session.place("iec.contactors.coil", ref="K1", x=20, y=20)
    session.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    session.wire("K1.A2", "H1.X1", number="101")
    path = tmp_path / "count.pcad.yaml"
    session.save(path)
    saved = Session.open(path)
    drawing_devices = sum(len(sheet.components) for sheet in saved.project.sheets)
    drawing_wires = sum(len(sheet.wires) for sheet in saved.project.sheets)
    assert drawing_devices == 2
    assert drawing_wires == 1

    job = ImportJob(tmp_path / "ws")
    repeated = {"number": "101", "sheet": "s1", "from": "K1.A2", "to": "H1.X1"}
    job._remember_result(
        session,
        {
            "placed": [{"tag": "K1"}, {"tag": "H1"}, {"tag": "K1"}],
            "wires": [repeated, dict(repeated), {"number": "", "sheet": "s1", "from": "K1.A2", "to": "H1.X1"}],
            "skipped": [],
        },
    )
    summary = job.summary()
    assert summary["devices"] == drawing_devices
    assert summary["wires"] == drawing_wires


def test_old_cost_guard_tiles_without_a_read_count_as_skipped(tmp_path: Path):
    """Tiles the cost guard never sent have no read body. They are skipped, not read."""

    root = tmp_path / "old-guard"
    folder = root / "pages" / "page_001" / "tiles"
    folder.mkdir(parents=True)
    (folder / "tile_00.json").write_text(
        json.dumps({"devices": [{"tag": "K1"}], "read": {"error": "", "parsed": [{"tag": "K1"}]}}),
        encoding="utf-8",
    )
    (folder / "tile_01.json").write_text(
        json.dumps({"devices": [{"tag": "OCR", "vision_fallback": "page cost guard"}], "wires": []}),
        encoding="utf-8",
    )
    for index in range(2, 24):
        (folder / f"tile_{index:02d}.json").write_text(
            json.dumps({"devices": [], "wires": [], "xrefs": [], "texts": []}),
            encoding="utf-8",
        )
    # A vector slice in the same job still counts as read.
    (folder / "tile_24.json").write_text(
        json.dumps({"devices": [{"tag": "Q1"}], "wires": [{"number": "101"}]}),
        encoding="utf-8",
    )
    (root / "pages" / "page_001" / "page.json").write_text(
        json.dumps({"id": "page_001"}),
        encoding="utf-8",
    )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "stage": "built",
                "pages": ["page_001"],
                "cost_guard": {
                    "warned": True,
                    "continued": False,
                    "remaining_tiles": 23,
                    "reason": "page cost guard",
                },
            }
        ),
        encoding="utf-8",
    )
    job = ImportJob(root)
    assert job.summary()["tiles_read"] == 2
    assert job.summary()["tiles_skipped"] == 23


def test_dxf_text_recovers_a_device_tag(dol, tmp_path: Path):
    dxf = tmp_path / "power.dxf"
    dol["session"].export_dxf(dxf, sheet_id="power")
    job = ImportJob.start([dxf], tmp_path / "ws", backend="external")
    status = job.status()
    assert status["pending"] is False
    assert status["pages"][0]["vector"] is True
    built = job.build()
    assert "Q1" in _tags(built)


def test_design_checks_list_import_warnings(dol, tmp_path: Path):
    from panelcad.api.session import Session
    from panelcad.core.checks import run_checks

    plain = Session.create("Plain")
    plain.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    plain_codes = {item.check for item in run_checks(plain.project, plain.catalog)}
    assert not any(code.startswith("print_") for code in plain_codes)

    job = ImportJob.start([dol["pdf"]], tmp_path / "ws", backend="external")
    built = job.build()
    payload = built.project.properties["print_import"]
    payload["items"] = list(payload.get("items") or [])
    payload["items"].append(
        {"kind": "wire", "text": "999", "sheet": "power", "reason": "ambiguous", "confidence": 0.4}
    )
    codes = {item.check for item in run_checks(built.project, built.catalog)}
    assert "print_wire" in codes


def test_drawing_agent_exposes_import_tools(tmp_path: Path):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from panelcad.ai.drawing import drawing_agent, open_drawing

    tools: dict[str, object] = {}

    def model(messages, info: AgentInfo) -> ModelResponse:
        for tool in info.function_tools:
            tools[tool.name] = tool
        return ModelResponse(parts=[TextPart(content="ready")])

    drawing = open_drawing(name="Import tools")
    with drawing_agent.override(model=FunctionModel(model)):
        result = drawing_agent.run_sync("List import tools.", deps=drawing)
    assert result.output == "ready"
    for name in ("import_start", "import_next", "import_submit", "import_status", "import_build", "import_add_photos"):
        assert name in tools
    submit = tools["import_submit"]
    props = submit.parameters_json_schema["properties"]
    assert "data" in props
    assert "work_id" in props
    start_props = tools["import_start"].parameters_json_schema["properties"]
    assert "vision_backend" in start_props
    assert "vision_model" in start_props


def test_gui_wizard_review_panel_and_menus(tmp_path: Path):
    from PySide6.QtWidgets import QApplication, QDockWidget

    from panelcad.api.session import Session
    from panelcad.gui.import_review import ImportReviewPanel
    from panelcad.gui.import_wizard import ImportWizard
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    crop = _white(tmp_path / "crop.png", (240, 180))
    panel = ImportReviewPanel()
    decisions: list[tuple[str, str, str]] = []
    reruns: list[tuple[str, str]] = []
    panel.decision_made.connect(lambda item_id, action, text: decisions.append((item_id, action, text)))
    panel.rerun_requested.connect(lambda tile_id, page_id: reruns.append((tile_id, page_id)))
    panel.set_items(
        [
            {
                "id": "page_001-dev-0",
                "kind": "device",
                "text": "K1",
                "confidence": 0.42,
                "status": "pending",
                "crop": str(crop),
                "page_id": "page_001",
                "tile_id": "tile_03",
                "unreadable": False,
                "source": "symbol",
            }
        ]
    )
    assert "low-confidence" in panel.caption.text()
    assert "symbol match" in panel.list.item(0).text()
    assert "0.42" in panel.list.item(0).text()
    assert "symbol match" in panel.detail.text()
    assert str(crop) not in panel.detail.text()
    assert "page_001" in panel.detail.text()
    assert "tile_03" in panel.detail.text()
    assert "/tmp" not in panel.detail.text()
    panel.edit.clear()
    panel.fix_btn.click()
    assert decisions == []
    assert "not a guess" in panel.detail.text()
    panel.edit.setText("K1A")
    panel.fix_btn.click()
    panel.accept_btn.click()
    panel.reject_btn.click()
    panel.rerun_tile_btn.click()
    panel.rerun_page_btn.click()
    assert ("page_001-dev-0", "fix", "K1A") in decisions
    assert ("page_001-dev-0", "accept", "K1A") in decisions
    assert ("page_001-dev-0", "reject", "K1A") in decisions
    assert reruns == [("tile_03", ""), ("", "page_001")]

    wizard = ImportWizard()
    wizard.files.setText(str(tmp_path / "missing.pdf"))
    wizard.photos.setText(str(tmp_path))
    wizard.standard.setCurrentText("IEC")
    assert wizard.source_paths() == [tmp_path / "missing.pdf"]
    wizard.show()
    app.processEvents()

    window = MainWindow(Session.create("Menus"))
    labels = []

    def walk(menu):
        for action in menu.actions():
            labels.append(action.text().replace("&", ""))
            if action.menu() is not None:
                walk(action.menu())

    for action in window.menuBar().actions():
        if action.menu() is not None:
            walk(action.menu())
    assert "Import print…" in labels
    assert "Print review" in labels
    assert "Scan vision…" in labels
    dock = window.findChild(QDockWidget, "printReviewDock")
    assert dock is not None
    assert dock.windowTitle() == "Print review"
    window.close()
    wizard.close()


def test_dol_starter_control_sheet_survives_a_vector_import(tmp_path: Path):
    """Moving H2's tag must not hide the lamp from a vector re-import."""

    from panelcad.api.session import Session

    source = Session.open(Path(__file__).resolve().parents[1] / "examples" / "dol_starter.pcad.yaml")
    pdf = source.export_pdf(tmp_path / "dol-starter.pdf")
    job = ImportJob.start([pdf], tmp_path / "imported", backend="external")
    built = job.build()
    expected = {comp.id for comp in source.project.sheet("control").components}
    recovered = {comp.id for comp in built.project.sheet("control").components}
    assert expected <= recovered, sorted(expected - recovered)


def test_prefix_guess_is_not_a_sure_symbol():
    from panelcad.importer.symbols import PREFIX_CONFIDENCE, resolve_symbol
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    guessed = resolve_symbol(catalog, tag="Q1", type_text="", standard="IEC")
    assert guessed["guess"] is True
    assert guessed["confidence"] <= PREFIX_CONFIDENCE < 0.6
    named = resolve_symbol(catalog, tag="Q1", type_text="breaker", standard="IEC")
    assert named["symbol"] == "breaker"
    assert "mpcb" not in named["library_id"]


def test_vector_and_fake_scan_round_trip(dol, tmp_path: Path):
    from panelcad.library.catalog import default_catalog

    truth = dol["session"]
    catalog = default_catalog()
    vector_ws = tmp_path / "vector"
    vector_job = ImportJob.start([dol["pdf"]], vector_ws, backend="external")
    vector_status = vector_job.status()
    assert vector_status["pending"] is False
    kinds = {row["id"]: row["kind"] for row in vector_status["pages"]}
    assert kinds["page_001"] == "schematic_power"
    assert "schematic_control" in kinds.values()
    assert "plc_io" in kinds.values()
    assert "panel_layout" in kinds.values()
    assert "network" in kinds.values()
    vector_built = vector_job.build()
    block = vector_built.project.title_block
    assert block.company == "Acme Process Controls"
    assert vector_built.project.name == "DOL Motor Starter"
    assert block.customer == "Riverside Pump Station"
    assert block.location == "MCC-1 / Pump 1"
    assert block.drawn_by == "PanelCAD"
    assert block.scale == "NTS"
    assert block.drawing_number == "E-001"
    assert block.standard == "IEC"
    assert block.drawing_style == "schematic"
    assert block.revisions and block.revisions[0].rev == "A"
    assert block.revisions[0].date == "2026-08-19"
    assert block.revisions[0].description == "Initial issue"
    power = vector_built.project.sheet("power")
    assert power.description == "Motor power circuit"
    assert vector_built.project.properties["print_import"]["sheet_count"] == "6"
    q1 = power.component("Q1")
    assert q1.library_id == "iec.breakers.mcb_3p_32a"
    assert abs(q1.x - 40) < 1.5 and abs(q1.y - 22) < 1.5
    assert power.component("F1").library_id == "iec.protection.overload_9a"
    assert power.component("M1").library_id == "iec.loads.motor_3ph"
    vector_score = score_projects(truth.project, vector_built.project, catalog=catalog)
    assert vector_score["netlist_match"] is True
    # A generic symbol is not a catalog footprint. The layout sheet keeps the
    # real modules and lists the others in the review instead of dropping them.
    for sheet in truth.project.sheets:
        if sheet.kind in {"panel_layout", "door_layout"}:
            continue
        built_sheet = next(
            item for item in vector_built.project.sheets if item.id == sheet.id or item.title == sheet.title
        )
        assert score_sheet_pair(sheet, built_sheet, catalog, sheet=sheet.id)["symbol_class"] == 1.0
    layout = next(sheet for sheet in vector_built.project.sheets if sheet.kind == "panel_layout")
    placed_layout = {comp.id for comp in layout.components}
    assert {"A2", "A3", "A4", "U1"} <= placed_layout
    for tag in ("Q1", "K1", "F1", "G1", "A1"):
        assert tag not in placed_layout
    review = vector_built.project.properties["print_import"]["items"]
    for tag in ("Q1", "K1", "F1", "G1", "A1"):
        assert any(item.get("text") == tag and item.get("reason") == "layout_not_ready" for item in review)
    assert vector_score["connectivity_precision"] == 1.0
    assert vector_score["connectivity_recall"] == 1.0
    assert vector_score["tags"] == 1.0
    assert vector_score["wire_numbers"] == 1.0
    _assert_geometry(truth.project, vector_built.project, catalog)
    vector_sources = {item.get("source") for item in vector_job.review_items() if item.get("kind") in {"device", "wire"}}
    assert "vector" in vector_sources

    rendered = rasterize_pdf(dol["pdf"], tmp_path / "rasters", dpi=160)
    scan = degrade_scan(Image.open(rendered[0]), quarter_turns=1, skew_deg=1.5, jpeg_quality=42)
    scan_path = tmp_path / "fake-scan.png"
    scan.save(scan_path, format="PNG")
    with _pdf_closed():
        scan_job = ImportJob.start([scan_path], tmp_path / "scan", backend="raster")
    assert scan_job.status()["pending"] is False
    prepared = scan_job._page("page_001")
    assert float(prepared["width_mm"]) > float(prepared["height_mm"])
    assert prepared.get("vector") is False
    assert (scan_job.ws.root / "pages" / "page_001" / "raster.json").is_file()
    assert scan_job.ws.read_json("pages", "page_001", "words.json") == []
    scan_built = scan_job.build()
    scan_score = score_sheet_pair(truth.project.sheet("power"), _find_sheet(scan_built, "power"), catalog, sheet="power")
    _assert_pixel_sources(scan_job)
    # A prefix guess is not reported as a symbol match.
    for item in scan_job.review_items():
        payload = item.get("payload") or {}
        if payload.get("guess") and item.get("source") != "symbol":
            assert float(item.get("confidence") or 0) <= 0.48

    hard_paths = []
    hard_dir = tmp_path / "hard-rasters"
    for index, image_path in enumerate(rasterize_pdf(dol["pdf"], hard_dir, dpi=150)):
        page_id = f"page_{index + 1:03d}"
        degraded = degrade_scan(
            Image.open(image_path),
            quarter_turns=1,
            skew_deg=4.0,
            jpeg_quality=28,
            blur=1.6,
            stain=True,
            fold=True,
        )
        dest = tmp_path / f"hard-{page_id}.jpg"
        degraded.save(dest, format="JPEG", quality=28)
        hard_paths.append(dest)
    with _pdf_closed():
        hard_job = ImportJob.start(hard_paths, tmp_path / "hard", backend="raster")
    assert hard_job.status()["pending"] is False
    assert hard_job.ws.read_json("pages", "page_001", "words.json") == []
    _assert_pixel_sources(hard_job)
    from panelcad.importer.pipeline import EmptyImport

    try:
        hard_built = hard_job.build()
    except EmptyImport as exc:
        # Blur, stain, and fold left no confirmed symbol. Prefix junk such as
        # T0 or T535 is listed and not placed.
        assert hard_job.manifest["stage"] == "empty"
        report = hard_job.ws.read_json("build.json") or {}
        assert "vision backend" in str(exc).lower()
        assert report.get("placed") == []
        allowed = {"needs_info", "title_block", "tag_unparsed", "unlocalised", "unreadable"}
        reasons = [row.get("reason") for row in report.get("skipped") or []]
        assert set(reasons) <= allowed
        assert "unconfirmed_symbol" not in reasons
        from panelcad.api.session import Session

        hard_built = Session.create("Hard scan")
    hard_score = score_projects(truth.project, hard_built.project, catalog=catalog)

    _write_artifacts(
        scan_path=scan_path,
        scan_job=scan_job,
        scan_built=scan_built,
        hard_job=hard_job,
        hard_built=hard_built,
        vector_score=vector_score,
        scan_score=scan_score,
        hard_score=hard_score,
    )


def test_live_model_is_optional(tmp_path: Path):
    from panelcad.importer.vision import _default_spec

    spec = _default_spec()
    reachable = probe_model(spec)
    report = {"spec": spec, "reachable": reachable, "ran": False, "error": ""}
    if reachable:
        image = _white(tmp_path / "live.png", (320, 200))
        job = ImportJob.start([image], tmp_path / "ws", backend="configured")
        # start() drains a reachable model. A finished job has no pending crop.
        if not str(job.manifest.get("pending") or ""):
            report["ran"] = True
            report["finished"] = str(job.manifest.get("stage") or "")
        else:
            work = job.current_work()
            try:
                from panelcad.importer.vision import ConfiguredVision

                data = ConfiguredVision(spec).extract(work)
                report["ran"] = True
                report["keys"] = sorted(data)
            except Exception as exc:  # noqa: BLE001 — CI must not depend on a model
                report["error"] = str(exc)
    ART.mkdir(parents=True, exist_ok=True)
    (ART / "live-trial.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert "reachable" in report


def test_unreadable_fields_accept_bool_string_or_list():
    """A model that returns true or false for unreadable_fields must not crash."""

    from panelcad.importer.extract import normalize_classification

    blank = normalize_classification(
        {"kind": "schematic_power", "title": "MOTOR", "drawing_number": "380L3503", "unreadable_fields": True}
    )
    assert blank["kind"] == "other"
    assert blank["title"] == ""
    assert blank["drawing_number"] == ""
    assert blank["unreadable_fields"] == ["*"]

    kept = normalize_classification(
        {"kind": "schematic_power", "title": "MOTOR", "drawing_number": "380L3503", "unreadable_fields": False}
    )
    assert kept["kind"] == "schematic_power"
    assert kept["title"] == "MOTOR"
    assert kept["unreadable_fields"] == []

    one = normalize_classification({"kind": "schematic_power", "title": "MOTOR", "unreadable_fields": "title"})
    assert one["title"] == ""
    assert one["kind"] == "schematic_power"
    assert one["unreadable_fields"] == ["title"]

    listed = normalize_classification(
        {"kind": "schematic_power", "title": "MOTOR", "rev": "A", "unreadable_fields": ["rev", True, "", 3]}
    )
    assert listed["rev"] == ""
    assert listed["title"] == "MOTOR"
    assert listed["unreadable_fields"] == ["rev"]
    assert normalize_classification({"unreadable_fields": None})["unreadable_fields"] == []


def test_detection_schema_and_wire_number_collapse():
    from panelcad.importer.raster import collapse_wire_numbers
    from panelcad.importer.vision import VisionFallback, validate_detections

    rows = validate_detections(
        {
            "detections": [
                {
                    "tag": "K1",
                    "wire_number": "",
                    "symbol_class": "contactor",
                    "bbox": [10, 20, 30, 40],
                    "confidence": 0.91,
                }
            ]
        }
    )
    assert rows[0]["tag"] == "K1"
    assert validate_detections({"devices": [], "conductors": [], "wire_numbers": [], "buses": []}) == []
    with pytest.raises(VisionFallback):
        validate_detections({"notes": []})
    with pytest.raises(VisionFallback):
        validate_detections(
            {
                "detections": [
                    {
                        "tag": "K1",
                        "wire_number": "",
                        "symbol_class": "contactor",
                        "bbox": [1, 2, 3],
                        "confidence": 0.5,
                    }
                ]
            }
        )
    collapsed = collapse_wire_numbers(
        [
            {
                "number": "103",
                "confidence": 0.9,
                "from_pin": "T3",
                "to_pin": "5",
                "from_tag": "K1",
                "to_tag": "F1",
                "ambiguous": False,
            },
            {"number": "103", "confidence": 0.9, "ambiguous": True, "from_pin": "", "to_pin": ""},
            {"number": "104", "confidence": 0.4, "ambiguous": True},
        ]
    )
    numbers = [wire["number"] for wire in collapsed]
    assert numbers.count("103") == 1
    assert numbers.count("104") == 1
    kept = next(wire for wire in collapsed if wire["number"] == "103")
    assert kept["from_pin"] == "T3"


def test_vision_backend_is_mocked_and_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import os

    from panelcad.importer.vision import VisionFallback, OpenAICompatibleVision
    from panelcad.importer.vision_config import VisionSettings, save_settings

    secret = "panelcad-test-key-not-real"
    monkeypatch.setenv("PANELCAD_TEST_VISION_KEY", secret)
    settings = VisionSettings(
        preset="custom",
        base_url="https://example.test/v1",
        model="mock-vision",
        api_key_env="PANELCAD_TEST_VISION_KEY",
        timeout_s=5,
        page_timeout_s=30,
        max_usd=1,
        workers=1,
    )
    stored = save_settings(settings, tmp_path / "vision.json")
    saved_text = stored.read_text(encoding="utf-8")
    assert secret not in saved_text
    assert '"api_key"' not in saved_text
    assert "Authorization" not in saved_text

    posts: list[int] = []

    def transport(method, url, payload, headers, timeout):
        blob = json.dumps({"url": url, "payload": payload}, default=str)
        assert secret not in blob
        assert headers.get("Authorization") == "Bearer " + secret
        if method == "GET":
            return {"data": [{"id": "mock-vision"}]}
        posts.append(1)
        if len(posts) == 1:
            body = {
                "detections": [
                    {
                        "tag": "K1",
                        "wire_number": "",
                        "symbol_class": "contactor",
                        "bbox": [12, 18, 36, 48],
                        "confidence": 0.91,
                    },
                    {
                        "tag": "",
                        "wire_number": "103",
                        "symbol_class": "",
                        "bbox": [40, 70, 8, 4],
                        "confidence": 0.88,
                    },
                    {
                        "tag": "",
                        "wire_number": "103",
                        "symbol_class": "",
                        "bbox": [44, 74, 8, 4],
                        "confidence": 0.4,
                    },
                ]
            }
            return {"choices": [{"message": {"content": json.dumps(body)}}], "usage": {"cost_in_usd_ticks": 0}}
        return {"choices": [{"message": {"content": "this is not json"}}]}

    ok, message = OpenAICompatibleVision(settings, transport=transport).test_connection()
    assert ok is True
    assert secret not in message
    assert "mock-vision" in message

    image = _inked_scan(tmp_path / "scan.png")
    job = ImportJob.start(
        [image],
        tmp_path / "ws",
        vision=settings,
        transport=transport,
    )
    status = job.status()
    assert status["pending"] is False
    assert status["vision"]["preset"] == "custom"
    assert status["vision"]["model"] == "mock-vision"
    assert secret not in json.dumps(status)
    assert "api_key" not in status["vision"]
    manifest = (tmp_path / "ws" / "manifest.json").read_text(encoding="utf-8")
    assert secret not in manifest
    assert '"api_key"' not in manifest
    assert job.ws.read_json("pages", "page_001", "words.json") == []
    items = job.review_items()
    models = [item for item in items if item.get("source") == "model"]
    assert models
    assert all(item.get("status") == "pending" for item in models)
    assert all(item.get("model_assisted") for item in models)
    assert sum(1 for item in items if item.get("text") == "103") == 1
    notes = [item for item in items if item.get("kind") == "note"]
    assert notes
    assert "malformed JSON" in notes[0]["text"]
    assert len(posts) >= 2

    from PySide6.QtWidgets import QApplication

    from panelcad.gui.import_review import ImportReviewPanel

    QApplication.instance() or QApplication([])
    panel = ImportReviewPanel()
    panel.set_items(items)
    assert "/tmp" not in panel.detail.text()
    assert "pytest" not in panel.detail.text()
    labels = [panel.list.item(row).text() for row in range(panel.list.count())]
    assert sum("103" in label for label in labels) == 1
    assert any("model-assisted" in label and "K1" in label for label in labels)
    assert any("vision fallback" in label and "malformed JSON" in label for label in labels)
    assert all(len(label) < 140 for label in labels)
    assert panel.list.count() == 3
    app = QApplication.instance()
    seen: set[str] = set()
    for row in range(panel.list.count()):
        panel.list.setCurrentRow(row)
        panel._show_row(row)
        app.processEvents()
        pix = panel.crop.pixmap()
        kind = panel._items[row]["kind"]
        assert pix is not None and not pix.isNull(), kind
        seen.add(kind)
        if kind == "device" and "K1" in panel.list.item(row).text():
            assert _dark_fraction(pix) > 0.08
        if kind == "note":
            assert panel._items[row].get("crop")
            assert not panel._items[row].get("crop_box")
    assert seen == {"device", "wire", "note"}
    panel.list.setCurrentRow(0)
    panel.resize(1600, 860)
    panel.show()
    app.processEvents()
    from PySide6.QtWidgets import QSplitter

    split = panel.findChild(QSplitter)
    assert panel.caption.geometry().top() < 12
    assert panel.caption.geometry().height() < 32
    assert split is not None
    assert split.geometry().top() - panel.caption.geometry().bottom() <= 8
    _grab(panel, ART / "vision-review.png", 1600, 860)
    panel.close()

    client = OpenAICompatibleVision(settings, transport=transport)
    client.cancel()
    with pytest.raises(VisionFallback, match="cancelled"):
        client.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 10, "h": 10}})

    def costly(method, url, payload, headers, timeout):
        return {
            "choices": [{"message": {"content": json.dumps({"detections": []})}}],
            "usage": {"cost_in_usd_ticks": 30_000_000_000},
        }

    guarded = OpenAICompatibleVision(settings, transport=costly)
    guarded.begin_page()
    guarded.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 10, "h": 10}})
    with pytest.raises(VisionFallback, match="page cost guard"):
        guarded.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 10, "h": 10}})


def test_review_crop_pixmap_is_present_for_every_row_type(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.import_review import ImportReviewPanel
    from panelcad.importer.review import build_review, stamp_review_crop

    image = Image.new("RGB", (320, 200), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 100, 80), fill=(10, 10, 10))
    image.save(tmp_path / "tile.png")
    tile = {
        "id": "tile_00",
        "page_id": "page_001",
        "x": 0,
        "y": 0,
        "w": 160,
        "h": 100,
        "crop": str(tmp_path / "tile.png"),
    }
    # 2 px per millimetre. The black block is millimetres (10, 10, 40, 30).
    model_device = {
        "tag": "K1",
        "x": 10,
        "y": 10,
        "w": 40,
        "h": 30,
        "confidence": 0.91,
        "source": "model",
        "model_assisted": True,
        "crop_box": [10, 10, 40, 30],
        "crop_span": [160, 100],
    }
    pixel_device = {"tag": "F1", "x": 10, "y": 10, "confidence": 0.8, "source": "symbol"}
    model_wire = {
        "number": "103",
        "x": 22,
        "y": 16,
        "confidence": 0.88,
        "source": "model",
        "model_assisted": True,
        "ambiguous": True,
        "crop_box": [14, 12, 16, 8],
        "crop_span": [160, 100],
    }
    pixel_wire = {"number": "104", "x": 30, "y": 20, "confidence": 0.7, "source": "ocr"}
    note = {
        "text": "vision fallback: malformed JSON (tile_00)",
        "role": "vision_fallback",
        "vision_fallback": "malformed JSON (tile_00)",
        "confidence": 1.0,
        "source": "ocr",
        "crop": str(tmp_path / "tile.png"),
        "tile_id": "tile_00",
    }
    stamp_review_crop(model_device, tile, tile["crop"])
    assert model_device["crop_box"] == [10, 10, 40, 30]
    stamp_review_crop(pixel_device, tile, tile["crop"])
    stamp_review_crop(model_wire, tile, tile["crop"])
    stamp_review_crop(pixel_wire, tile, tile["crop"])
    assert pixel_device["crop_box"]
    assert pixel_wire["crop_box"]
    assert model_wire["crop_box"] == [14, 12, 16, 8]
    page = {
        "id": "page_001",
        "image": tile["crop"],
        "merged": {
            "devices": [model_device, pixel_device],
            "wires": [model_wire, pixel_wire],
            "texts": [note],
        },
    }
    items = build_review([page], [], [])
    QApplication.instance() or QApplication([])
    panel = ImportReviewPanel()
    panel.set_items(items)
    kinds = []
    for row in range(panel.list.count()):
        panel.list.setCurrentRow(row)
        panel._show_row(row)
        pix = panel.crop.pixmap()
        item = panel._items[row]
        assert pix is not None and not pix.isNull(), item["kind"]
        kinds.append((item["kind"], item.get("source")))
        if item["kind"] == "note":
            assert _dark_fraction(pix) < 0.25
        else:
            assert _dark_fraction(pix) > 0.4
    assert ("device", "model") in kinds
    assert ("device", "symbol") in kinds
    assert ("wire", "model") in kinds
    assert ("wire", "ocr") in kinds
    assert ("note", "ocr") in kinds
    panel.close()


def test_vector_pdf_does_not_call_the_vision_model(dol, tmp_path: Path):
    from panelcad.importer.vision_config import VisionSettings

    def transport(*_args, **_kwargs):
        raise AssertionError("vector import called the vision model")

    job = ImportJob.start(
        [dol["pdf"]],
        tmp_path / "vector-vision",
        vision=VisionSettings(preset="grok", model="grok-4.7"),
        transport=transport,
    )
    assert job.status()["pending"] is False
    built = job.build()
    assert "Q1" in _tags(built)
    assert built.project.sheet("power").component("Q1").library_id == "iec.breakers.mcb_3p_32a"


def test_cli_jsonl_and_gui_select_the_vision_backend(tmp_path: Path):
    import os

    from typer.testing import CliRunner

    from panelcad.cli.agent import Agent
    from panelcad.cli.main import app
    from panelcad.gui.import_wizard import ImportWizard
    from panelcad.gui.vision_settings import VisionSettingsDialog

    image = _white(tmp_path / "plain.png", (320, 200))
    runner = CliRunner()
    none = runner.invoke(
        app,
        [
            "import",
            str(image),
            "--vision-backend",
            "none",
            "--vision-model",
            "grok-4.7",
            "--workers",
            "4",
            "--workspace",
            str(tmp_path / "none-ws"),
            "--out",
            str(tmp_path / "none.pcad.yaml"),
        ],
    )
    none_text = none.output + (getattr(none, "stderr", None) or "")
    assert none.exit_code == 1, none_text
    assert "none was chosen" in none_text
    assert none_text.lower().count("a scan needs a vision backend") <= 2
    none_manifest = json.loads((tmp_path / "none-ws" / "manifest.json").read_text(encoding="utf-8"))
    assert none_manifest["vision"]["workers"] == 4
    assert "paused" not in none.output
    grok = runner.invoke(
        app,
        [
            "import",
            str(image),
            "--vision-backend",
            "grok",
            "--vision-model",
            "grok-4.7",
            "--workspace",
            str(tmp_path / "grok-ws"),
            "--out",
            str(tmp_path / "grok.pcad.yaml"),
        ],
    )
    grok_text = grok.output + (getattr(grok, "stderr", None) or "")
    assert grok.exit_code == 1, grok_text
    assert "did not build a drawing" in grok_text
    manifest = json.loads((tmp_path / "grok-ws" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["vision"]["preset"] == "grok"
    assert manifest["vision"]["model"] == "grok-4.7"
    assert manifest["vision"]["base_url"] == "https://api.x.ai/v1"
    assert manifest["vision"]["api_key_env"] == "XAI_API_KEY"
    assert manifest["vision"]["workers"] == 3
    assert "api_key" not in manifest["vision"]
    assert '"api_key"' not in json.dumps(manifest)
    blob = json.dumps(manifest)
    assert os.environ.get("XAI_API_KEY", "no-key-set") not in blob or not os.environ.get("XAI_API_KEY")

    agent = Agent()
    started = agent.handle(
        {
            "id": "vision-1",
            "op": "import_start",
            "path": str(image),
            "workspace": str(tmp_path / "jsonl"),
            "vision_backend": "none",
            "vision_model": "ignored-for-none",
            "vision_workers": 5,
        }
    )
    assert started["ok"] is True
    assert started["result"]["pending"] is False
    assert started["result"]["vision"]["preset"] == "none"
    assert started["result"]["vision"]["workers"] == 5
    grok_agent = Agent()
    grok_started = grok_agent.handle(
        {
            "id": "vision-2",
            "op": "import_start",
            "path": str(image),
            "workspace": str(tmp_path / "jsonl-grok"),
            "vision_backend": "ollama",
            "vision_model": "local-model",
            "vision_base_url": "http://127.0.0.1:9/v1",
        }
    )
    assert grok_started["ok"] is True
    assert grok_started["result"]["vision"]["model"] == "local-model"
    assert grok_started["result"]["vision"]["base_url"] == "http://127.0.0.1:9/v1"
    notes = [item["text"] for item in grok_agent.print_import.review_items() if item.get("kind") == "note"]
    assert notes
    assert "vision fallback" in notes[0]

    from PySide6.QtWidgets import QApplication

    qt = QApplication.instance() or QApplication([])
    dialog = VisionSettingsDialog(path=tmp_path / "settings.json")
    dialog.preset.setCurrentIndex(dialog.preset.findData("grok"))
    assert dialog.model.text() == "grok-4.7"
    assert "docs.x.ai" in dialog.docs.text()
    assert "api.x.ai" in dialog.base_url.text()
    dialog.test_connection()
    assert "XAI_API_KEY" in dialog.status.text()
    assert os.environ.get("XAI_API_KEY", "") not in dialog.status.text() or not os.environ.get("XAI_API_KEY")
    dialog._save()
    disk = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "grok-4.7" in disk
    assert '"api_key"' not in disk

    wizard = ImportWizard()
    wizard.vision.setCurrentIndex(wizard.vision.findData("grok"))
    wizard.files.setText(str(image))
    assert wizard.test_btn.text() == "Test connection"
    assert wizard.cancel_btn.text() == "Cancel"
    assert wizard.vision_settings().model == "grok-4.7"
    assert wizard.vision_settings().base_url == "https://api.x.ai/v1"
    assert wizard.workers.value() == 3
    dialog.preset.setCurrentIndex(dialog.preset.findData("grok-build"))
    assert dialog.model.text() == ""
    assert dialog.timeout.value() == 600
    assert dialog.page_timeout.value() == 3600
    assert dialog.clean_sessions.isChecked()
    assert dialog.tools_none.isChecked() is False
    docs = dialog.docs.text()
    assert "dontAsk" in docs
    assert "--prompt-file" in docs
    assert "--no-plan" in docs
    assert "WebSearch" in docs
    assert "no --no-mcp" in docs.lower() or "No --no-mcp" in docs
    assert "blender" in docs.lower()
    assert "xAI API" in docs
    assert "600 s" in docs
    assert dialog.workers.value() == 3
    dialog.test_connection()
    assert "credential" in dialog.status.text().lower()
    import threading
    import time

    from panelcad.importer.vision_config import VisionSettings

    release = threading.Event()
    calls = {"n": 0}
    lock = threading.Lock()

    def slow_runner(argv, cwd, timeout):
        with lock:
            calls["n"] += 1
            index = calls["n"]
        if index > 1:
            release.wait(8)
        body = {"detections": []}
        return 0, json.dumps({"structuredOutput": body, "total_cost_usd": 0.021}), ""

    wizard.transport = slow_runner
    wizard._vision_saved = VisionSettings(preset="grok-build", timeout_s=8, page_timeout_s=60, workers=2)
    wizard.workers.setValue(2)
    wizard.vision.setCurrentIndex(wizard.vision.findData("grok-build"))
    wizard.files.setText(str(image))
    wizard.workspace.setText(str(tmp_path / "wizard-live"))
    wizard.run_import()
    deadline = time.time() + 10
    while time.time() < deadline:
        qt.processEvents()
        label = wizard.progress.text()
        if label.startswith("Tiles 1/"):
            break
        time.sleep(0.02)
    assert wizard.progress.text().startswith("Tiles 1/")
    assert "fallbacks" in wizard.progress.text()
    _grab(dialog, ART / "vision-settings.png", 960, 1080)
    wizard.cancel_import()
    release.set()
    deadline = time.time() + 20
    while time.time() < deadline:
        qt.processEvents()
        if wizard.progress.text().startswith("Cancelled at"):
            break
        time.sleep(0.02)
    assert wizard.progress.text().startswith("Cancelled at"), wizard.progress.text()
    assert "Cancel requested" in wizard.log.toPlainText()
    _grab(wizard, ART / "vision-import-dialog.png", 1000, 820)
    wizard.close()
    dialog.close()
    thread = wizard._thread
    if thread is not None:
        assert thread.wait(15000)
    qt.processEvents()


def test_vision_wire_numbers_snap_or_stay_as_stubs():
    from panelcad.importer.extract import normalize_submission
    from panelcad.importer.raster import WIRE_ATTACH_MM, attach_vision_wires, stitch_wire_seams

    assert WIRE_ATTACH_MM >= 12
    image = Image.new("RGB", (400, 200), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    # 80 mm long, short of the 110 mm segment cap, so Hough keeps it.
    draw.line((80, 100, 240, 100), fill=(0, 0, 0), width=4)
    near = {
        "number": "104",
        "x": 80.0,
        "y": 40.0,
        "source": "model",
        "model_assisted": True,
        "ambiguous": False,
        "stub": True,
        "confidence": 0.9,
        "from_pin": "",
        "to_pin": "",
        "from_tag": "",
        "to_tag": "",
    }
    # The line is y=50 mm. The number is 10 mm off, inside the 12 mm bbox error.
    snapped = attach_vision_wires([near], image, 200, 100, [], None, [])
    assert snapped[0]["attached"] is True
    assert snapped[0]["ambiguous"] is False
    assert snapped[0]["stub"] is False
    far = dict(near, number="105", y=5.0)
    missed = attach_vision_wires([far], image, 200, 100, [], None, [])
    assert missed[0]["stub"] is True
    assert missed[0]["ambiguous"] is False
    assert missed[0]["needs_review"] is True
    cleaned = normalize_submission(
        {
            "wires": [
                {
                    "number": "105",
                    "x": 80,
                    "y": 5,
                    "stub": True,
                    "ambiguous": False,
                    "confidence": 0.9,
                    "source": "model",
                }
            ]
        }
    )
    assert cleaned["wires"][0]["ambiguous"] is False
    assert cleaned["wires"][0]["stub"] is True
    assert cleaned["wires"][0]["confidence"] == 0.9
    left = {
        "number": "101",
        "x": 100,
        "y": 40,
        "from_tag": "Q1",
        "from_pin": "2",
        "to_tag": "",
        "to_pin": "",
        "ambiguous": False,
        "attached": True,
        "confidence": 0.8,
        "tile_x": 0,
        "tile_y": 0,
        "tile_w": 130,
        "tile_h": 130,
    }
    right = {
        "number": "101",
        "x": 110,
        "y": 40,
        "from_tag": "",
        "from_pin": "",
        "to_tag": "K1",
        "to_pin": "L1",
        "ambiguous": False,
        "attached": True,
        "confidence": 0.8,
        "tile_x": 102,
        "tile_y": 0,
        "tile_w": 130,
        "tile_h": 130,
    }
    seamed = stitch_wire_seams([left, right])
    assert len(seamed) == 1
    assert seamed[0]["from_pin"] == "2"
    assert seamed[0]["to_pin"] == "L1"
    assert seamed[0]["ambiguous"] is False


def test_vision_hallucinations_are_dropped_or_flagged():
    from panelcad.importer.vision import filter_vision_detections
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    tile = {"x": 0, "y": 0, "w": 130, "h": 130}

    def row(tag: str, symbol: str = "", bbox: list[float] | None = None, confidence: float = 0.9) -> dict:
        return {
            "tag": tag,
            "wire_number": "",
            "symbol_class": symbol,
            "bbox": bbox or [10, 10, 16, 12],
            "confidence": confidence,
        }

    kept = filter_vision_detections(
        [
            row("K"),
            row("M"),
            row("PLC"),
            row("HMI"),
            row("K", "contactor"),
            row("K1", "contactor"),
            row("X1"),
            row("Y2"),
            {"tag": "", "wire_number": "104", "symbol_class": "", "bbox": [4, 4, 8, 4], "confidence": 0.8},
        ],
        catalog,
        tile,
        [],
    )
    tags = [item["tag"] for item in kept if item["tag"]]
    held = {item["tag"]: item for item in kept if item.get("tag_unparsed")}
    for bare in ("K", "M", "PLC"):
        assert bare in tags
        assert held[bare]["needs_review"] is True
        assert held[bare]["confidence"] <= 0.4
    # HMI is a free-form NEMA name with no digits. It is a device, not an unparsed word.
    assert "HMI" in tags
    assert "HMI" not in held
    assert "K1" in tags
    assert any(item["wire_number"] == "104" for item in kept)
    flagged = {item["tag"]: item for item in kept if item["tag"] in {"X1", "Y2"}}
    assert flagged["X1"]["confidence"] < 0.6
    assert flagged["Y2"]["confidence"] < 0.6
    near = filter_vision_detections(
        [row("X1", bbox=[8, 8, 8, 6], confidence=0.92)],
        catalog,
        tile,
        [{"tag": "XT", "x": 16, "y": 14, "symbol": "terminal", "library_id": "connection.terminal"}],
    )
    assert near[0]["confidence"] == 0.92

    class _One:
        library_id = "demo.control.plc"
        category = "control.plc"
        ref_prefix = "PLC"
        pins: list = []
        standard = "IEC"

    class _Catalog:
        def __init__(self) -> None:
            self.devices = {"demo.control.plc": _One()}

        def search(self, _query: str, standard: str | None = None) -> list:
            return [self.devices["demo.control.plc"]]

    supported = filter_vision_detections([row("PLC", "plc")], _Catalog(), tile, [])
    assert [item["tag"] for item in supported] == ["PLC"]


def test_grok_build_prompt_file_schema_and_structured_output(tmp_path: Path):
    from panelcad.importer.grok_build import GrokBuildVision, parse_grok_result, safe_extra_argv
    from panelcad.importer.vision_config import VisionSettings

    image = _white(tmp_path / "tile.png", (32, 32))
    captured: list[list[str]] = []

    def runner(argv: list[str], cwd: str | None, timeout: float) -> tuple[int, str, str]:
        captured.append(list(argv))
        assert "--prompt-json" not in argv
        assert "--disallowed-tools" not in argv
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
        assert argv[argv.index("--output-format") + 1] == "json"
        assert argv[argv.index("--effort") + 1] == "low"
        assert argv[argv.index("--max-turns") + 1] == "5"
        assert "Bash" in argv and "MCPTool" in argv
        path = Path(argv[argv.index("--prompt-file") + 1])
        blocks = json.loads(path.read_text(encoding="utf-8"))
        assert [block["type"] for block in blocks] == ["text", "image"]
        assert blocks[1]["mimeType"] == "image/png"
        assert blocks[1]["data"]
        assert blocks[1]["data"] not in " ".join(argv)
        schema = json.loads(argv[argv.index("--json-schema") + 1])
        assert schema["required"] == ["devices", "conductors", "wire_numbers", "buses"]
        device_item = schema["properties"]["devices"]["items"]
        assert device_item["required"] == ["tag", "symbol_class", "bbox"]
        assert schema["properties"]["conductors"]["items"]["properties"]["to_pin"]["maxLength"] == 12
        assert "oneOf" not in schema
        body = {
            "detections": [
                {
                    "tag": "K1",
                    "wire_number": "",
                    "symbol_class": "contactor",
                    "bbox": [1, 2, 8, 6],
                    "confidence": 0.8,
                }
            ]
        }
        return 0, json.dumps({"structuredOutput": body, "total_cost_usd": 0.021}), ""

    settings = VisionSettings(preset="grok-build", grok_extra_args="--sandbox workspace --permission-mode accept")
    client = GrokBuildVision(settings, runner=runner)
    data = client.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 10, "h": 10}})
    assert data["detections"][0]["tag"] == "K1"
    assert client.calls == 1
    assert client.spent_usd == pytest.approx(0.021)
    assert client.input_tokens == 0
    assert "--tools" not in captured[0]
    armed = GrokBuildVision(VisionSettings(preset="grok-build", grok_tools_none=True), runner=runner)
    armed.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 10, "h": 10}})
    assert captured[1][captured[1].index("--tools") + 1] == "none"
    assert "Bash" in captured[1]
    # ``--sandbox strict`` is unsupported on Windows: the CLI warns and the
    # import still runs. The flag stays on Linux and macOS.
    if sys.platform == "win32":
        assert "--sandbox" not in captured[0]
    else:
        assert captured[0][captured[0].index("--sandbox") + 1] == "strict"
        assert captured[0].count("--sandbox") == 1
    assert "workspace" not in captured[0]
    assert "accept" not in captured[0]
    assert "--no-plan" in captured[0]
    assert "--system-prompt-override" in captured[0]
    assert "--session-id" in captured[0]
    for denied in ("Bash", "Read", "Grep", "Glob", "Edit", "Write", "WebFetch", "WebSearch", "MCPTool"):
        assert denied in captured[0]
    snake, _payload, parsed = parse_grok_result('{"structured_output": {"detections": []}}')
    assert parsed is True
    assert snake == {"detections": []}
    streamed, _payload, parsed = parse_grok_result(
        '\n'.join(
            [
                '{"type":"progress"}',
                json.dumps({"structuredOutput": {"detections": []}, "total_cost_usd": 0.01}),
            ]
        )
    )
    assert parsed is True
    assert streamed == {"detections": []}
    assert safe_extra_argv("--token SECRET") == []
    assert VisionSettings(preset="local grok").resolved().preset == "grok-build"
    assert VisionSettings(preset="local").resolved().preset == "ollama"
    assert VisionSettings(preset="grok-build").resolved().timeout_s == 600
    assert VisionSettings(preset="grok-build", timeout_s=240).resolved().timeout_s == 240
    assert VisionSettings(preset="grok-build", timeout_s=900).resolved().timeout_s == 900
    assert VisionSettings(preset="grok-build").resolved().clean_grok_sessions is True
    assert VisionSettings(preset="grok-build").resolved().grok_tools_none is False
    assert VisionSettings.from_dict({"preset": "grok-build", "grok_tools_none": True}).resolved().grok_tools_none is True
    assert VisionSettings(preset="grok-build").resolved().page_timeout_s == 3600
    assert VisionSettings(workers=30).resolved().workers == 8
    assert VisionSettings(workers=0).resolved().workers == 1
    stored = VisionSettings(preset="grok-build", grok_extra_args="--token SECRET").public_dict()
    assert stored["grok_extra_args"] == ""
    assert "api_key" not in stored


def test_grok_build_retries_a_tool_attempt_then_falls_back(tmp_path: Path):
    from panelcad.importer.grok_build import REMINDER, SIGN_IN_MESSAGE, GrokBuildVision
    from panelcad.importer.vision import VisionFallback
    from panelcad.importer.vision_config import VisionSettings

    image = _white(tmp_path / "tile.png", (32, 32))
    prompts: list[list[dict]] = []

    def runner(argv: list[str], cwd: str | None, timeout: float) -> tuple[int, str, str]:
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
        blocks = json.loads(Path(argv[argv.index("--prompt-file") + 1]).read_text(encoding="utf-8"))
        prompts.append(blocks)
        if len(prompts) == 1:
            return 0, json.dumps({"stopReason": "tool_use", "text": "run_terminal powershell"}), ""
        return 0, json.dumps({"stopReason": "tool_use"}), ""

    client = GrokBuildVision(VisionSettings(preset="grok-build"), runner=runner)
    with pytest.raises(VisionFallback, match="no structured answer"):
        client.extract({"image": str(image), "page_mm": {"x": 1, "y": 1, "w": 8, "h": 8}})
    assert len(prompts) == 2
    assert prompts[1][-1]["text"] == REMINDER
    assert all(block.get("text") != REMINDER for block in prompts[0])

    def signed_out(argv: list[str], cwd: str | None, timeout: float) -> tuple[int, str, str]:
        return 1, "", "You are not signed in"

    signed = GrokBuildVision(VisionSettings(preset="grok-build"), runner=signed_out)
    with pytest.raises(VisionFallback, match="not signed in") as caught:
        signed.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 4, "h": 4}})
    assert "grok login" in str(caught.value)
    assert "does not store credentials" in str(caught.value)
    assert str(caught.value) == SIGN_IN_MESSAGE

    cancelled_prompts: list[list[dict]] = []

    def cancelled(argv: list[str], cwd: str | None, timeout: float) -> tuple[int, str, str]:
        blocks = json.loads(Path(argv[argv.index("--prompt-file") + 1]).read_text(encoding="utf-8"))
        cancelled_prompts.append(blocks)
        return 0, json.dumps({"stopReason": "cancelled"}), ""

    refused = GrokBuildVision(VisionSettings(preset="grok-build"), runner=cancelled)
    with pytest.raises(VisionFallback, match="no structured answer"):
        refused.extract({"image": str(image), "page_mm": {"x": 2, "y": 2, "w": 6, "h": 6}})
    assert len(cancelled_prompts) == 2
    assert cancelled_prompts[1][-1]["text"] == REMINDER

    def answered(argv: list[str], cwd: str | None, timeout: float) -> tuple[int, str, str]:
        body = {"detections": []}
        return 0, json.dumps({"stopReason": "cancelled", "structuredOutput": body}), ""

    kept = GrokBuildVision(VisionSettings(preset="grok-build"), runner=answered)
    assert kept.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 4, "h": 4}})["detections"] == []
    assert kept.calls == 1


def test_worker_pool_keeps_tile_order_and_stops_on_the_cost_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import threading
    import time

    from panelcad.importer.vision_config import VisionSettings

    monkeypatch.setenv("PANELCAD_TEST_VISION_KEY", "pool-key")
    image = _white(tmp_path / "pool.png", (640, 400))
    order_root = tmp_path / "order"
    finished: list[int] = []
    lock = threading.Lock()

    def ordered(method, url, payload, headers, timeout):
        if method == "GET":
            return {"data": [{"id": "mock-vision"}]}
        text = next(part["text"] for part in payload["messages"][1]["content"] if part.get("type") == "text")
        marker = "page millimetres "
        start = text.index(marker) + len(marker)
        box = json.loads(text[start : text.index("}", start) + 1])
        tiles = json.loads((order_root / "pages" / "page_001" / "tiles.json").read_text(encoding="utf-8"))
        index = 0
        for position, tile in enumerate(tiles):
            if abs(float(tile["x"]) - float(box["x"])) < 0.2 and abs(float(tile["y"]) - float(box["y"])) < 0.2:
                index = position
                break
        if index == 0:
            time.sleep(0.35)
        with lock:
            finished.append(index)
        body = {
            "detections": [
                {
                    "tag": f"N{index}",
                    "wire_number": "",
                    "symbol_class": "relay",
                    "bbox": [12, 12, 20, 14],
                    "confidence": 0.9,
                }
            ]
        }
        return {"choices": [{"message": {"content": json.dumps(body)}}], "usage": {"cost_in_usd_ticks": 0}}

    ordered_job = ImportJob.start(
        [image],
        order_root,
        vision=VisionSettings(
            preset="custom",
            base_url="https://example.test/v1",
            model="mock-vision",
            api_key_env="PANELCAD_TEST_VISION_KEY",
            timeout_s=5,
            page_timeout_s=60,
            max_usd=5,
            workers=3,
        ),
        transport=ordered,
    )
    tiles = ordered_job.ws.read_json("pages", "page_001", "tiles.json")
    merged = ordered_job.ws.read_json("pages", "page_001", "merged.json")
    assert [device["tag"] for device in merged["devices"]] == [f"N{i}" for i in range(len(tiles))]
    assert finished[0] != 0
    assert finished != sorted(finished)

    calls = {"n": 0}

    def costly(method, url, payload, headers, timeout):
        if method == "GET":
            return {"data": [{"id": "mock-vision"}]}
        with lock:
            calls["n"] += 1
        body = {"detections": []}
        return {
            "choices": [{"message": {"content": json.dumps(body)}}],
            "usage": {"cost_in_usd_ticks": 10_000_000_000},
        }

    guarded = ImportJob.start(
        [image],
        tmp_path / "guard",
        vision=VisionSettings(
            preset="custom",
            base_url="https://example.test/v1",
            model="mock-vision",
            api_key_env="PANELCAD_TEST_VISION_KEY",
            timeout_s=5,
            page_timeout_s=60,
            max_usd=0.5,
            workers=2,
        ),
        transport=costly,
    )
    tile_count = len(guarded.ws.read_json("pages", "page_001", "tiles.json"))
    assert tile_count > 2
    assert 1 <= calls["n"] <= 2
    assert calls["n"] < tile_count
    assert guarded._backend.calls == calls["n"]
    assert "page cost guard" in (guarded._page("page_001").get("vision_fallback") or "")


def test_grok_build_tool_failure_falls_back_to_pixels(tmp_path: Path):
    from panelcad.importer.grok_build import GrokBuildVision
    from panelcad.importer.vision_config import VisionSettings

    image = _white(tmp_path / "scan.png", (640, 400))
    seen: list[list[str]] = []

    def runner(argv: list[str], cwd: str | None, timeout: float) -> tuple[int, str, str]:
        seen.append(list(argv))
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
        path = Path(argv[argv.index("--prompt-file") + 1])
        blocks = json.loads(path.read_text(encoding="utf-8"))
        assert blocks[0]["type"] == "text" and blocks[1]["type"] == "image"
        return 0, json.dumps({"stopReason": "tool_use", "text": "powershell"}), ""

    job = ImportJob.start(
        [image],
        tmp_path / "grok-ws",
        vision=VisionSettings(preset="grok-build", workers=1),
        transport=runner,
    )
    assert isinstance(job._backend, GrokBuildVision)
    page = job._page("page_001")
    assert "no structured answer" in (page.get("vision_fallback") or "")
    tiles = job.ws.read_json("pages", "page_001", "tiles.json")
    assert len(seen) == len(tiles) * 2
    assert job.status()["vision"]["preset"] == "grok-build"
    blob = json.dumps(job.status())
    assert "api_key" not in job.status()["vision"]
    assert "Bearer" not in blob


def test_offset_vision_wire_numbers_survive_on_the_mild_scan(
    dol, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Mild-scan wire numbers with boxes shifted by about 10 mm stay in the review list."""

    from panelcad.importer.vision_config import VisionSettings

    monkeypatch.setenv("PANELCAD_TEST_VISION_KEY", "mild-key")

    rendered = rasterize_pdf(dol["pdf"], tmp_path / "rasters", dpi=160)
    mild = degrade_scan(Image.open(rendered[0]), quarter_turns=1, skew_deg=1.5, jpeg_quality=42)
    mild_path = tmp_path / "mild.png"
    mild.save(mild_path)
    root = tmp_path / "mild-ws"
    from panelcad.importer.raster import _on_terminal_label, _single_letter_wire
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    expected: set[str] = set()
    dropped: set[str] = set()

    def _junk(number: str, x: float, y: float, devices: list) -> bool:
        if _single_letter_wire(number):
            return True
        if x < 18 or x > 420 - 18 or y < 20 or y > 297 - 52:
            return True
        return _on_terminal_label(number, x, y, devices, catalog)

    def transport(method, url, payload, headers, timeout):
        if method == "GET":
            return {"data": [{"id": "mock-vision"}]}
        text = next(part["text"] for part in payload["messages"][1]["content"] if part.get("type") == "text")
        marker = "page millimetres "
        start = text.index(marker) + len(marker)
        box = json.loads(text[start : text.index("}", start) + 1])
        raster = json.loads((root / "pages" / "page_001" / "raster.json").read_text(encoding="utf-8"))
        detections = []
        for wire in raster.get("wires") or []:
            number = str(wire.get("number") or "")
            if not number or wire.get("x") is None:
                continue
            local_x = float(wire["x"]) - float(box["x"])
            local_y = float(wire["y"]) - float(box["y"])
            if local_x < 0 or local_y < 0 or local_x > float(box["w"]) or local_y > float(box["h"]):
                continue
            # Shift the box by 10 mm and keep it on this crop. That is the measured bbox error.
            shifted = min(max(local_x + 10.0, 4.0), float(box["w"]) - 4.0)
            shifted_y = min(max(local_y, 4.0), float(box["h"]) - 4.0)
            detections.append(
                {
                    "tag": "",
                    "wire_number": number,
                    "symbol_class": "",
                    "bbox": [round(shifted - 3.0, 2), round(shifted_y - 2.0, 2), 6.0, 4.0],
                    "confidence": 0.86,
                }
            )
            page_x = float(box["x"]) + shifted
            page_y = float(box["y"]) + shifted_y
            if _junk(number, page_x, page_y, raster.get("devices") or []):
                dropped.add(number)
            else:
                expected.add(number)
                dropped.discard(number)
        if not detections and float(box["x"]) < 1 and float(box["y"]) < 1:
            for number, origin in (("101", 18.0), ("104", 36.0), ("L1", 54.0)):
                detections.append(
                    {
                        "tag": "",
                        "wire_number": number,
                        "symbol_class": "",
                        "bbox": [origin, 16.0, 8.0, 4.0],
                        "confidence": 0.8,
                    }
                )
                if _junk(number, float(box["x"]) + origin + 4.0, float(box["y"]) + 18.0, []):
                    dropped.add(number)
                else:
                    expected.add(number)
                    dropped.discard(number)
        body = {"detections": detections}
        return {"choices": [{"message": {"content": json.dumps(body)}}], "usage": {"cost_in_usd_ticks": 0}}

    job = ImportJob.start(
        [mild_path],
        root,
        vision=VisionSettings(
            preset="custom",
            base_url="https://example.test/v1",
            model="mock-vision",
            api_key_env="PANELCAD_TEST_VISION_KEY",
            timeout_s=5,
            page_timeout_s=120,
            max_usd=5,
            workers=1,
        ),
        transport=transport,
    )
    assert expected
    rows = [item for item in job.review_items() if item.get("kind") == "wire" and item.get("text") in expected]
    found = {item["text"] for item in rows}
    assert expected <= found
    junk_rows = {item["text"] for item in job.review_items() if item.get("kind") == "wire" and item.get("text") in dropped}
    assert junk_rows == set()
    for item in rows:
        payload = item["payload"]
        assert item["status"] == "pending"
        assert payload["ambiguous"] is False
        assert payload.get("attached") or payload.get("stub")


def test_reject_border_letters_and_terminal_digits():
    from panelcad.importer.raster import reject_junk_wire_numbers
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    device = {"tag": "K1", "library_id": "iec.contactors.ac3_9a", "x": 40.0, "y": 80.0, "guess": False}
    wires = [
        {"number": "11", "x": 80.0, "y": 100.0, "source": "model", "model_assisted": True},
        {"number": "101", "x": 50.0, "y": 150.0, "source": "model", "model_assisted": True},
        {"number": "C", "x": 200.0, "y": 100.0, "source": "model", "model_assisted": True},
        {"number": "G", "x": 8.0, "y": 40.0, "source": "model", "model_assisted": True},
        {"number": "N", "x": 200.0, "y": 110.0, "source": "model", "model_assisted": True},
        {"number": "12", "x": 40.0, "y": 12.0, "source": "model", "model_assisted": True},
        {"number": "L1", "x": 50.0, "y": 80.0, "source": "ocr"},
        {"number": "L1", "x": 58.0, "y": 18.0, "source": "model", "model_assisted": True},
        {"number": "A1", "x": 200.0, "y": 120.0, "source": "model", "model_assisted": True},
        {"number": "K1", "x": 90.0, "y": 120.0, "source": "model", "model_assisted": True},
    ]
    kept = [wire["number"] for wire in reject_junk_wire_numbers(wires, [device], catalog, 420, 297)]
    assert kept == ["101", "N", "L1"]


def test_grok_build_retries_one_timeout_then_falls_back(tmp_path: Path):
    import time

    from panelcad.importer.grok_build import GrokBuildVision
    from panelcad.importer.vision import VisionFallback
    from panelcad.importer.vision_config import VisionSettings

    image = _white(tmp_path / "tile.png", (32, 32))
    calls = {"n": 0}
    sizes: list[int] = []

    def once(argv, cwd, timeout):
        calls["n"] += 1
        blocks = json.loads(Path(argv[argv.index("--prompt-file") + 1]).read_text(encoding="utf-8"))
        sizes.append(len(blocks[1]["data"]))
        assert argv[argv.index("--effort") + 1] == "low"
        if calls["n"] == 1:
            raise TimeoutError("slow")
        body = {
            "detections": [
                {
                    "tag": "K1",
                    "wire_number": "",
                    "symbol_class": "contactor",
                    "bbox": [1, 2, 8, 6],
                    "confidence": 0.8,
                }
            ]
        }
        return 0, json.dumps({"structuredOutput": body}), ""

    client = GrokBuildVision(VisionSettings(preset="grok-build", timeout_s=8), runner=once)
    data = client.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 8, "h": 8}})
    assert data["detections"][0]["tag"] == "K1"
    assert calls["n"] == 2
    assert sizes[1] < sizes[0]

    def always(argv, cwd, timeout):
        calls["n"] += 1
        raise TimeoutError("slow")

    calls["n"] = 0
    stalled = GrokBuildVision(VisionSettings(preset="grok-build", timeout_s=8), runner=always)
    with pytest.raises(VisionFallback, match="request timed out"):
        stalled.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 8, "h": 8}})
    assert calls["n"] == 2
    assert stalled.estimated_usd == pytest.approx(0.04)
    assert stalled.spent_usd == pytest.approx(0.04)

    calls["n"] = 0
    budget = GrokBuildVision(VisionSettings(preset="grok-build", timeout_s=8, page_timeout_s=30), runner=always)
    budget.begin_job()
    budget.settings.page_timeout_s = 1

    def expire(argv, cwd, timeout):
        calls["n"] += 1
        budget.page_started = time.monotonic() - 10
        raise TimeoutError("slow")

    budget._runner = expire
    with pytest.raises(VisionFallback, match="page timeout"):
        budget.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 8, "h": 8}})
    assert calls["n"] == 1
    budget.finish_job()


def test_grok_build_omits_sandbox_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from panelcad.importer import grok_build as gb
    from panelcad.importer.vision_config import VisionSettings

    monkeypatch.setattr(gb, "_windows", lambda: True)
    image = _white(tmp_path / "tile.png", (32, 32))
    captured: list[list[str]] = []

    def runner(argv, cwd, timeout):
        captured.append(list(argv))
        return 0, json.dumps({"structuredOutput": {"detections": []}}), ""

    client = gb.GrokBuildVision(
        VisionSettings(preset="grok-build", grok_extra_args="--sandbox workspace"),
        runner=runner,
    )
    client.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 4, "h": 4}})
    assert "--sandbox" not in captured[0]
    assert "--no-auto-update" in captured[0]
    assert "--no-memory" in captured[0]
    assert "--no-plan" in captured[0]


def test_grok_build_deletes_only_its_session_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import uuid

    from panelcad.importer import grok_build as gb
    from panelcad.importer.vision_config import VisionSettings

    home = tmp_path / "home"
    uid = "11111111-1111-1111-1111-111111111111"
    other = "22222222-2222-2222-2222-222222222222"
    monkeypatch.setattr(gb.Path, "home", lambda: home)
    monkeypatch.setattr(gb.uuid, "uuid4", lambda: uuid.UUID(uid))
    bare = home / ".grok" / "sessions" / other
    bare.mkdir(parents=True)
    (bare / "keep.txt").write_text("bare", encoding="utf-8")
    image = _white(tmp_path / "tile.png", (32, 32))
    seen: dict[str, str] = {}

    def runner(argv, cwd, timeout):
        assert argv[argv.index("--session-id") + 1] == uid
        seen["cwd"] = str(cwd)
        parent = home / ".grok" / "sessions" / gb.encoded_cwd_name(cwd)
        try:
            parent.mkdir(parents=True)
        except OSError as exc:
            pytest.skip(f"encoded session path cannot be created: {exc}")
        (parent / uid).mkdir()
        (parent / uid / "note.txt").write_text("tile", encoding="utf-8")
        (parent / other).mkdir()
        (parent / other / "keep.txt").write_text("keep", encoding="utf-8")
        return 0, json.dumps({"structuredOutput": {"detections": []}}), ""

    client = gb.GrokBuildVision(VisionSettings(preset="grok-build"), runner=runner)
    client.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 4, "h": 4}})
    parent = home / ".grok" / "sessions" / gb.encoded_cwd_name(seen["cwd"])
    assert not (parent / uid).exists()
    assert (parent / other / "keep.txt").is_file()
    assert parent.is_dir()
    assert (bare / "keep.txt").is_file()
    assert gb.delete_grok_session("../../etc") is False
    assert gb.delete_grok_session(other) is False
    assert gb.delete_grok_session(other, seen["cwd"]) is True
    assert not parent.exists()
    assert (bare / "keep.txt").is_file()
    outside = tmp_path / "outside"
    outside.mkdir()
    link_id = "33333333-3333-3333-3333-333333333333"
    job = tmp_path / "linked-job"
    job.mkdir()
    link = home / ".grok" / "sessions" / gb.encoded_cwd_name(job)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        # WinError 1314: creating a symlink needs a privilege this process does not have.
        # The folder delete above already ran.
        return
    assert gb.delete_grok_session(link_id, job) is False
    assert outside.is_dir()
    assert link.is_symlink()


def test_grok_models_reports_signed_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from panelcad.importer.grok_build import SIGN_IN_MESSAGE, GrokBuildVision
    from panelcad.importer.vision_config import VisionSettings

    monkeypatch.setattr("panelcad.importer.grok_build.find_grok", lambda: Path("/tmp/grok-test-bin"))
    seen: list[list[str]] = []

    def signed_out(argv, cwd, timeout):
        seen.append(list(argv))
        return 1, "", "You are not signed in"

    client = GrokBuildVision(VisionSettings(preset="grok-build"), runner=signed_out)
    ok, message = client.test_connection()
    assert seen and seen[0][1] == "models"
    assert ok is False
    assert message == SIGN_IN_MESSAGE

    def unauthenticated(argv, cwd, timeout):
        return 0, "You are not authenticated.\n", ""

    signed_out_zero = GrokBuildVision(VisionSettings(preset="grok-build"), runner=unauthenticated)
    ok, message = signed_out_zero.test_connection()
    assert ok is False
    assert message == SIGN_IN_MESSAGE

    def listed(argv, cwd, timeout):
        return 0, "grok-4\n", ""

    signed = GrokBuildVision(VisionSettings(preset="grok-build"), runner=listed)
    ok, message = signed.test_connection()
    assert ok is True
    assert message.startswith("Signed in.")
    assert "does not store credentials" in message


def test_cli_and_agent_report_tile_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from typer.testing import CliRunner

    from panelcad.cli.agent import Agent
    from panelcad.cli.main import app

    monkeypatch.setenv("PANELCAD_TEST_VISION_KEY", "progress-key")
    image = _white(tmp_path / "plain.png", (320, 200))

    def transport(method, url, payload, headers, timeout):
        if method == "GET":
            return {"data": [{"id": "mock-vision"}]}
        body = {"detections": []}
        return {
            "choices": [{"message": {"content": json.dumps(body)}}],
            "usage": {"cost_in_usd_ticks": 2_100_000_000},
        }

    monkeypatch.setattr("panelcad.importer.vision.http_transport", transport)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "import",
            str(image),
            "--vision-backend",
            "custom",
            "--vision-model",
            "mock-vision",
            "--vision-base-url",
            "https://example.test/v1",
            "--vision-api-key-env",
            "PANELCAD_TEST_VISION_KEY",
            "--workers",
            "2",
            "--workspace",
            str(tmp_path / "cli-progress"),
            "--out",
            str(tmp_path / "progress.pcad.yaml"),
        ],
    )
    text = result.output + (getattr(result, "stderr", None) or "")
    assert result.exit_code == 1, text
    assert "tiles " in text
    assert "fallbacks" in text
    assert "summary tiles " in text
    assert "did not build a drawing" in text

    agent = Agent()
    started = agent.handle(
        {
            "id": "progress-1",
            "op": "import_start",
            "path": str(image),
            "workspace": str(tmp_path / "agent-progress"),
            "vision_backend": "custom",
            "vision_model": "mock-vision",
            "vision_base_url": "https://example.test/v1",
            "vision_api_key_env": "PANELCAD_TEST_VISION_KEY",
            "vision_workers": 2,
            "clean_grok_sessions": False,
        }
    )
    assert started["ok"] is True, started
    summary = started["result"]["summary"]
    assert summary["tiles_total"] >= 1
    assert summary["tiles_done"] == summary["tiles_total"]
    assert "spent_usd" in summary
    assert "fallbacks" in summary
    assert started["result"]["vision"]["clean_grok_sessions"] is False
    built = agent.handle({"id": "progress-2", "op": "import_build"})
    assert built["ok"] is False, built
    assert "did not build a drawing" in built["error"]
    assert "tiles_done" in summary


def test_missing_tesseract_is_a_clear_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import pytesseract
    from typer.testing import CliRunner

    from panelcad.cli.agent import Agent
    from panelcad.cli.main import app
    from panelcad.importer.raster import TESSERACT_MESSAGE, TesseractMissing
    from panelcad.importer.vision_config import VisionSettings

    def boom(*_args, **_kwargs):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "image_to_data", boom)
    monkeypatch.setattr(pytesseract, "image_to_string", boom)
    image = _white(tmp_path / "scan.png", (320, 200))
    with pytest.raises(TesseractMissing, match="github.com/tesseract-ocr/tesseract"):
        ImportJob.start([image], tmp_path / "pixels", vision=VisionSettings(preset="none"))

    runner = CliRunner()
    cli = runner.invoke(
        app,
        [
            "import",
            str(image),
            "--vision-backend",
            "none",
            "--workspace",
            str(tmp_path / "cli-ocr"),
            "--out",
            str(tmp_path / "ocr.pcad.yaml"),
        ],
    )
    cli_text = cli.output + (getattr(cli, "stderr", None) or "")
    assert cli.exit_code == 1
    assert "TesseractNotFoundError" not in cli_text
    assert "Traceback" not in cli_text
    assert "https://github.com/tesseract-ocr/tesseract" in cli_text
    assert "https://tesseract-ocr.github.io/tessdoc/Downloads.html" in cli_text

    agent = Agent()
    reply = agent.handle(
        {
            "id": "ocr-1",
            "op": "import_start",
            "path": str(image),
            "workspace": str(tmp_path / "agent-ocr"),
            "vision_backend": "none",
        }
    )
    assert reply["ok"] is False
    assert "Traceback" not in reply["error"]
    assert TESSERACT_MESSAGE in reply["error"]

    monkeypatch.setenv("PANELCAD_TEST_VISION_KEY", "ocr-key")

    def transport(method, url, payload, headers, timeout):
        if method == "GET":
            return {"data": [{"id": "mock-vision"}]}
        body = {
            "detections": [
                {
                    "tag": "K1",
                    "wire_number": "",
                    "symbol_class": "contactor",
                    "bbox": [20, 20, 24, 30],
                    "confidence": 0.9,
                }
            ]
        }
        return {"choices": [{"message": {"content": json.dumps(body)}}], "usage": {"cost_in_usd_ticks": 0}}

    monkeypatch.setattr("panelcad.importer.vision.http_transport", transport)
    job = ImportJob.start(
        [image],
        tmp_path / "vision-ocr",
        vision=VisionSettings(
            preset="custom",
            base_url="https://example.test/v1",
            model="mock-vision",
            api_key_env="PANELCAD_TEST_VISION_KEY",
            timeout_s=5,
            page_timeout_s=60,
            max_usd=2,
            workers=1,
        ),
    )
    page = job._page("page_001")
    assert "github.com/tesseract-ocr/tesseract" in (page.get("ocr_skipped") or "")
    assert page["classification"]["kind"] == "schematic_power"
    assert job.summary()["ocr_skipped"]
    tags = [item.get("text") for item in job.review_items() if item.get("kind") == "device"]
    assert "K1" in tags


def test_cancel_kills_inflight_grok_without_blocking_the_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import os
    import time

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.import_wizard import ImportWizard
    from panelcad.importer.vision_config import VisionSettings

    # A ``#!/bin/sh`` file is not an executable on Windows, so the process never
    # appears in ``_procs``. This sleeper is Python, which ignores grok's flags.
    sleeper = tmp_path / "sleep_grok.py"
    sleeper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr("panelcad.importer.grok_build.find_grok", lambda: sleeper)

    def sleeper_argv(self, prompt_path, cwd, session_id):
        return [sys.executable, str(sleeper)]

    monkeypatch.setattr("panelcad.importer.grok_build.GrokBuildVision._argv", sleeper_argv)
    image = _white(tmp_path / "scan.png", (320, 200))
    qt = QApplication.instance() or QApplication([])
    wizard = ImportWizard()
    wizard.files.setText(str(image))
    wizard.workspace.setText(str(tmp_path / "cancel-ws"))
    wizard.vision.setCurrentIndex(wizard.vision.findData("grok-build"))
    wizard.workers.setValue(2)
    wizard._vision_saved = VisionSettings(preset="grok-build", timeout_s=8, page_timeout_s=30, workers=2)
    fired: list[float] = []
    timer = QTimer()
    timer.setInterval(30)
    timer.timeout.connect(lambda: fired.append(time.monotonic()))
    timer.start()
    started = time.monotonic()
    wizard.run_import()
    while time.monotonic() - started < 0.45:
        qt.processEvents()
        time.sleep(0.01)
    assert fired, "the wizard blocked the UI thread"
    assert fired[0] - started < 0.45
    procs = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        qt.processEvents()
        backend = wizard.job._backend if wizard.job is not None else None
        procs = list(getattr(backend, "_procs", []) or [])
        if procs:
            break
        time.sleep(0.02)
    assert procs, "grok did not start"
    held = list(procs)
    wizard.cancel_import()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        qt.processEvents()
        if all(proc.poll() is not None for proc in held):
            break
        time.sleep(0.02)
    assert all(proc.poll() is not None for proc in held)
    for proc in held:
        if proc.poll() is None:
            os.kill(proc.pid, 9)
    timer.stop()
    wizard.close()
    thread = wizard._thread
    if thread is not None:
        assert thread.wait(8000)


_MILD_PAIRS = (
    ("L1", "Q1", "2", "K1", "L1"),
    ("L2", "Q1", "4", "K1", "L2"),
    ("L3", "Q1", "6", "K1", "L3"),
    ("101", "K1", "T1", "F1", "1"),
    ("102", "K1", "T2", "F1", "3"),
    ("103", "K1", "T3", "F1", "5"),
    ("104", "F1", "2", "M1", "U1"),
    ("105", "F1", "4", "M1", "V1"),
    ("106", "F1", "6", "M1", "W1"),
)


def test_mild_vision_wire_numbers_become_built_wires(
    dol, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Recorded mild-scan replies: at least 8 of the 9 wire numbers build with the right ends."""

    from panelcad.importer.vision_config import VisionSettings
    from panelcad.library.catalog import default_catalog

    monkeypatch.setenv("PANELCAD_TEST_VISION_KEY", "mild-key")
    catalog = default_catalog()
    rendered = rasterize_pdf(dol["pdf"], tmp_path / "rasters", dpi=160)
    mild = degrade_scan(Image.open(rendered[0]), quarter_turns=1, skew_deg=1.5, jpeg_quality=42)
    mild_path = tmp_path / "mild.png"
    mild.save(mild_path)
    root = tmp_path / "mild-built"

    def _pin(device: dict, pin_id: str) -> tuple[float, float]:
        part = catalog.get(str(device["library_id"]))
        pin = next(item for item in part.pins if str(item.id) == pin_id)
        return float(device["x"]) + float(pin.x), float(device["y"]) + float(pin.y)

    def _inside(x: float, y: float, box: dict) -> bool:
        return float(box["x"]) <= x <= float(box["x"]) + float(box["w"]) and float(box["y"]) <= y <= float(box["y"]) + float(box["h"])

    def transport(method, url, payload, headers, timeout):
        if method == "GET":
            return {"data": [{"id": "mock-vision"}]}
        text = next(part["text"] for part in payload["messages"][1]["content"] if part.get("type") == "text")
        marker = "page millimetres "
        start = text.index(marker) + len(marker)
        box = json.loads(text[start : text.index("}", start) + 1])
        raster = json.loads((root / "pages" / "page_001" / "raster.json").read_text(encoding="utf-8"))
        by_tag = {str(device.get("tag") or ""): device for device in raster.get("devices") or []}
        k1 = by_tag["K1"]
        placed = {
            "K1": k1,
            "F1": by_tag["F1"],
            "M1": by_tag["M1"],
            "Q1": {
                "tag": "Q1",
                "library_id": "iec.breakers.mcb_3p_32a",
                "x": float(k1["x"]) + 2.0,
                "y": float(k1["y"]) - 60.0,
            },
        }
        detections = []
        symbols = {"Q1": "breaker", "K1": "contactor", "F1": "overload", "M1": "motor"}
        for tag, symbol in symbols.items():
            origin_x = float(placed[tag]["x"])
            origin_y = float(placed[tag]["y"])
            if not _inside(origin_x, origin_y, box):
                continue
            detections.append(
                {
                    "tag": tag,
                    "wire_number": "",
                    "symbol_class": symbol,
                    "bbox": [
                        round(origin_x - float(box["x"]), 2),
                        round(origin_y - float(box["y"]), 2),
                        30.0,
                        40.0,
                    ],
                    "confidence": 0.93,
                }
            )
        for number, a_tag, a_pin, b_tag, b_pin in _MILD_PAIRS:
            ax, ay = _pin(placed[a_tag], a_pin)
            bx, by = _pin(placed[b_tag], b_pin)
            cx, cy = (ax + bx) / 2.0 + 2.5, (ay + by) / 2.0 + 3.5
            if not _inside(cx, cy, box):
                continue
            detections.append(
                {
                    "tag": "",
                    "wire_number": number,
                    "symbol_class": "",
                    "bbox": [round(cx - float(box["x"]) - 3.0, 2), round(cy - float(box["y"]) - 2.0, 2), 6.0, 4.0],
                    "confidence": 0.92,
                }
            )
        for number, pin_id in (("11", "13"), ("13", "14")):
            px, py = _pin(placed["K1"], pin_id)
            if not _inside(px, py, box):
                continue
            detections.append(
                {
                    "tag": "",
                    "wire_number": number,
                    "symbol_class": "",
                    "bbox": [round(px - float(box["x"]) - 2.0, 2), round(py - float(box["y"]) - 1.5, 2), 4.0, 3.0],
                    "confidence": 0.8,
                }
            )
        for number, px, py in (("C", 6.0, 80.0), ("G", 120.0, 90.0)):
            if not _inside(px, py, box):
                continue
            detections.append(
                {
                    "tag": "",
                    "wire_number": number,
                    "symbol_class": "",
                    "bbox": [round(px - float(box["x"]) - 2.0, 2), round(py - float(box["y"]) - 2.0, 2), 4.0, 4.0],
                    "confidence": 0.7,
                }
            )
        body = {"detections": detections}
        return {"choices": [{"message": {"content": json.dumps(body)}}], "usage": {"cost_in_usd_ticks": 0}}

    job = ImportJob.start(
        [mild_path],
        root,
        vision=VisionSettings(
            preset="custom",
            base_url="https://example.test/v1",
            model="mock-vision",
            api_key_env="PANELCAD_TEST_VISION_KEY",
            timeout_s=8,
            page_timeout_s=180,
            max_usd=5,
            workers=1,
        ),
        transport=transport,
    )
    built = job.build()
    found: dict[str, set[frozenset[str]]] = {}
    for sheet in built.project.sheets:
        for wire in sheet.wires:
            ends = frozenset({str(wire.frm).upper(), str(wire.to).upper()})
            found.setdefault(str(wire.number), set()).add(ends)
    matched = []
    for number, a_tag, a_pin, b_tag, b_pin in _MILD_PAIRS:
        wanted = frozenset({f"{a_tag}.{a_pin}".upper(), f"{b_tag}.{b_pin}".upper()})
        if wanted in found.get(number, set()):
            matched.append(number)
    report = job.ws.read_json("build.json", default={}) or {}
    assert len(matched) >= 8, {"matched": matched, "wires": found, "unresolved": report.get("unresolved_wires")}
    for junk in ("11", "13", "C", "G"):
        assert junk not in found
    rows = {item["text"]: item for item in job.review_items() if item.get("kind") == "wire"}
    for number in matched:
        assert rows[number]["status"] == "pending"
    l1 = rows["L1"]["payload"]
    assert l1.get("inferred") is True
    assert float(l1.get("confidence") or 1) <= 0.55
    assert l1.get("needs_review") is True


def test_quiet_config_toml_matches_a_recorded_inspect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import logging

    from panelcad.importer import grok_build as gb
    from panelcad.importer.vision_config import VisionSettings

    sample = {
        "plugins": [
            {"name": "browser-use", "enabled": True},
            {"name": "chrome-devtools", "enabled": True},
            {"name": "skill-pack", "enabled": False},
            {"name": "name-with-dash", "enabled": True},
            {"name": "name.with.dot", "enabled": True},
        ],
        "mcpServers": [
            {"name": "blender", "source": "configToml"},
            {"name": "steve", "source": {"kind": "configToml"}},
            {"name": "ignored", "source": "project"},
            {"name": "server-with-dash", "source": "configToml"},
            {"name": "server.with.dot", "source": "configToml"},
            {"name": "off", "source": "configToml", "enabled": False},
        ],
    }
    expected = (
        "[plugins]\n"
        'disabled = ["browser-use", "chrome-devtools", "name-with-dash", "name.with.dot"]\n'
        "\n"
        "[mcp_servers.blender]\n"
        'command = "cmd"\n'
        "enabled = false\n"
        "[mcp_servers.steve]\n"
        'command = "cmd"\n'
        "enabled = false\n"
        "[mcp_servers.ignored]\n"
        'command = "cmd"\n'
        "enabled = false\n"
        "[mcp_servers.server-with-dash]\n"
        'command = "cmd"\n'
        "enabled = false\n"
        '[mcp_servers."server.with.dot"]\n'
        'command = "cmd"\n'
        "enabled = false\n"
    )
    assert gb.quiet_config_from_inspect(sample) == expected
    assert gb.quiet_config_from_inspect(json.dumps(sample)) == expected
    quoted = gb.quiet_config_from_inspect({"plugins": [{"name": 'say"hi', "enabled": True}]})
    assert 'disabled = ["say\\"hi"]' in quoted
    as_dict = gb.quiet_config_from_inspect(
        {
            "plugins": [{"name": "browser-use", "enabled": True}],
            "mcpServers": {
                "blender": {"source": "configToml"},
                "steve": {"source": "user"},
                "shaped": {"source": {"kind": "configToml", "path": "/tmp/x"}},
                "off": {"enabled": False, "source": "configToml"},
            },
        }
    )
    assert "[mcp_servers.blender]" in as_dict
    assert "[mcp_servers.steve]" in as_dict
    assert "[mcp_servers.shaped]" in as_dict
    assert "off" not in as_dict

    monkeypatch.setattr(gb, "run_grok_inspect", lambda client: json.dumps(sample))
    image = _white(tmp_path / "quiet.png", (320, 200))
    cwds: list[str] = []

    def runner(argv, cwd, timeout):
        cwds.append(str(cwd))
        text = (Path(cwd) / ".grok" / "config.toml").read_text(encoding="utf-8")
        assert text == expected
        assert "GROK_HOME" not in " ".join(argv)
        body = {"detections": []}
        return 0, json.dumps({"structuredOutput": body, "usage": {"input_tokens": 14}}), ""

    job = ImportJob.start(
        [image],
        tmp_path / "quiet-ws",
        vision=VisionSettings(preset="grok-build", workers=1, timeout_s=8),
        transport=runner,
    )
    assert cwds and len(set(cwds)) == 1
    assert job.summary()["input_tokens"] == 14 * len(cwds)

    def broken(client):
        raise RuntimeError("inspect broke")

    monkeypatch.setattr(gb, "run_grok_inspect", broken)

    def bare(argv, cwd, timeout):
        assert not (Path(cwd) / ".grok" / "config.toml").exists()
        return 0, json.dumps({"structuredOutput": {"detections": []}}), ""

    cap: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            cap.append(record.getMessage())

    handler = _Capture()
    gb.log.addHandler(handler)
    gb.log.setLevel(logging.WARNING)
    try:
        ImportJob.start(
            [image],
            tmp_path / "quiet-fail",
            vision=VisionSettings(preset="grok-build", workers=1, timeout_s=8),
            transport=bare,
        )
    finally:
        gb.log.removeHandler(handler)
    assert any("inspect broke" in line for line in cap)

    def weird(client):
        return '{"plugins": "nope"}'

    monkeypatch.setattr(gb, "run_grok_inspect", weird)
    cap.clear()
    gb.log.addHandler(handler)
    try:
        ImportJob.start(
            [image],
            tmp_path / "quiet-shape",
            vision=VisionSettings(preset="grok-build", workers=1, timeout_s=8),
            transport=bare,
        )
    finally:
        gb.log.removeHandler(handler)
    assert any("unexpected" in line for line in cap)


def test_cancel_during_ocr_is_not_cleared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from panelcad.importer.vision_config import VisionSettings

    monkeypatch.setenv("PANELCAD_TEST_VISION_KEY", "cancel-key")
    image = _white(tmp_path / "early.png", (320, 200))
    calls = {"n": 0}

    def transport(method, url, payload, headers, timeout):
        if method == "GET":
            return {"data": [{"id": "mock-vision"}]}
        calls["n"] += 1
        return {"choices": [{"message": {"content": json.dumps({"detections": []})}}], "usage": {"cost_in_usd_ticks": 0}}

    real = ImportJob._read_raster

    def wrapped(self, page):
        self.cancel()
        return real(self, page)

    monkeypatch.setattr(ImportJob, "_read_raster", wrapped)
    job = ImportJob.start(
        [image],
        tmp_path / "early-cancel",
        vision=VisionSettings(
            preset="custom",
            base_url="https://example.test/v1",
            model="mock-vision",
            api_key_env="PANELCAD_TEST_VISION_KEY",
            timeout_s=5,
            page_timeout_s=60,
            max_usd=2,
            workers=2,
        ),
        transport=transport,
    )
    assert job.manifest["cancelled"] is True
    assert job._backend.cancelled is True
    assert calls["n"] == 0
    assert job._page("page_001").get("vision_fallback") == "cancelled"


def test_tesseract_error_names_the_language_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import pytesseract
    from typer.testing import CliRunner

    from panelcad.cli.agent import Agent
    from panelcad.cli.main import app
    from panelcad.importer.raster import TESSDATA_MESSAGE

    class TesseractError(RuntimeError):
        pass

    monkeypatch.setattr(pytesseract, "TesseractError", TesseractError, raising=False)

    def boom(*_args, **_kwargs):
        raise TesseractError("Error opening data file eng.traineddata")

    monkeypatch.setattr(pytesseract, "image_to_data", boom)
    monkeypatch.setattr(pytesseract, "image_to_string", boom)
    image = _white(tmp_path / "scan.png", (160, 100))
    runner = CliRunner()
    cli = runner.invoke(
        app,
        ["import", str(image), "--vision-backend", "none", "--workspace", str(tmp_path / "cli"), "--out", str(tmp_path / "out.yaml")],
    )
    text = cli.output + (getattr(cli, "stderr", None) or "")
    assert cli.exit_code == 1
    assert "Traceback" not in text
    assert "TesseractError" not in text
    assert "eng.traineddata" in text
    assert "https://github.com/tesseract-ocr/tessdata" in text
    assert TESSDATA_MESSAGE in text
    agent = Agent()
    reply = agent.handle(
        {"id": "tess-1", "op": "import_start", "path": str(image), "workspace": str(tmp_path / "agent"), "vision_backend": "none"}
    )
    assert reply["ok"] is False
    assert "eng.traineddata" in reply["error"]
    assert "Traceback" not in reply["error"]


def test_potential_rails_replace_placeholder_devices():
    from panelcad.importer.build import build_session
    from panelcad.library.catalog import default_catalog

    stitched = {
        "standard": "IEC",
        "drawing_style": "schematic",
        "title_block": {"project": "pot"},
        "sheets": [
            {
                "id": "control",
                "title": "Control",
                "kind": "schematic",
                "page_kind": "schematic_control",
                "drawing_style": "schematic",
                "devices": [
                    {"tag": "G1", "library_id": "iec.power.psu_24v_5a", "x": 40, "y": 40, "guess": False},
                    {
                        "tag": "WN106",
                        "library_id": "iec.connection.node",
                        "x": 90,
                        "y": 40,
                        "endpoint": True,
                        "guess": False,
                    },
                ],
                "wires": [
                    {
                        "number": "24V",
                        "from_tag": "G1",
                        "from_pin": "plus",
                        "to_tag": "",
                        "to_pin": "",
                        "attached": True,
                    },
                    {
                        "number": "0V",
                        "from_tag": "G1",
                        "from_pin": "minus",
                        "to_tag": "WN106",
                        "to_pin": "A",
                        "attached": True,
                    },
                ],
            }
        ],
    }
    session, report = build_session(stitched, catalog=default_catalog())
    sheet = session.project.sheet("control")
    assert sheet.rail_hot == "24V"
    assert sheet.rail_return == "0V"
    assert all(not str(comp.id).startswith("WN") for comp in sheet.components)
    ends = {(str(wire.frm), str(wire.to), str(wire.number)) for wire in sheet.wires}
    assert ("G1.plus", "24V", "24V") in ends or ("24V", "G1.plus", "24V") in ends
    assert ("G1.minus", "0V", "0V") in ends or ("0V", "G1.minus", "0V") in ends
    assert not any(str(wire.frm).startswith("WN") or str(wire.to).startswith("WN") for wire in sheet.wires)
    assert report["placed"]


def test_label_prefers_the_segment_it_sits_beside_not_the_nearest_endpoint():
    from panelcad.importer.raster import _convention_score

    left = _convention_score(50.5, 83.0, (48.0, 70.0), (48.0, 82.0), 14.0)
    right = _convention_score(50.5, 83.0, (58.0, 70.0), (58.0, 82.0), 14.0)
    assert left is not None
    assert right is not None
    assert left < right
    past = _convention_score(50.5, 90.0, (48.0, 70.0), (48.0, 82.0), 14.0)
    assert past is None


# Seeds are the symbol origins measured on this degraded power page. Each search
# stays in its own band so the breaker template cannot lock onto the overload.
_HARD_PARTS = (
    ("Q1", "iec.breakers.mcb_3p_32a", "breaker", 47.0, 34.0),
    ("K1", "iec.contactors.ac3_9a", "contactor", 45.0, 91.0),
    ("F1", "iec.protection.overload_9a", "overload", 47.0, 161.0),
    ("M1", "iec.loads.motor_3ph", "motor", 49.0, 213.0),
)


def test_hard_page_wire_numbers_meet_seven_of_nine(dol, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Recorded hard-page replies, shifted 4 mm, still build at least 7 of 9 ends.

    OCR on this scan does not tag Q1, K1, or M1. The replies are the four power
    symbols located on the deskewed page, plus the nine wire numbers.
    """

    import cv2
    import numpy as np

    from panelcad.importer.raster import _ink, _search_template
    from panelcad.importer.vision_config import VisionSettings
    from panelcad.library.catalog import default_catalog

    monkeypatch.setenv("PANELCAD_TEST_VISION_KEY", "hard-key")
    catalog = default_catalog()
    rendered = rasterize_pdf(dol["pdf"], tmp_path / "rasters", dpi=150)
    hard = degrade_scan(
        Image.open(rendered[0]),
        quarter_turns=1,
        skew_deg=4.0,
        jpeg_quality=28,
        blur=1.6,
        stain=True,
        fold=True,
    )
    hard_path = tmp_path / "hard-1.jpg"
    hard.save(hard_path, format="JPEG", quality=28)
    root = tmp_path / "hard-built"
    located: dict[str, dict] = {}

    def _pin(device: dict, pin_id: str) -> tuple[float, float]:
        part = catalog.get(str(device["library_id"]))
        pin = next(item for item in part.pins if str(item.id) == pin_id)
        return float(device["x"]) + float(pin.x), float(device["y"]) + float(pin.y)

    def _inside(x: float, y: float, box: dict) -> bool:
        return float(box["x"]) <= x <= float(box["x"]) + float(box["w"]) and float(box["y"]) <= y <= float(box["y"]) + float(box["h"])

    def _locate() -> dict[str, dict]:
        if located:
            return located
        page = json.loads((root / "pages" / "page_001" / "page.json").read_text(encoding="utf-8"))
        image = Image.open(page["image"]).convert("RGB")
        rgb = np.asarray(image)
        width_mm = float(page["width_mm"])
        ppm = rgb.shape[1] / width_mm if width_mm else 1.0
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        binary = cv2.dilate(_ink(gray), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
        for tag, library_id, symbol, seed_x, seed_y in _HARD_PARTS:
            part = catalog.get(library_id)
            score, ox, oy = _search_template(
                binary,
                part,
                ppm,
                binary.shape,
                seed_x,
                seed_y,
                x_span=(-18.0, 18.0),
                y_span=(-18.0, 18.0),
                cover_origin=True,
            )
            located[tag] = {
                "tag": tag,
                "library_id": library_id,
                "symbol": symbol,
                "x": ox,
                "y": oy,
                "score": round(score, 3),
            }
        return located

    def transport(method, url, payload, headers, timeout):
        if method == "GET":
            return {"data": [{"id": "mock-vision"}]}
        text = next(part["text"] for part in payload["messages"][1]["content"] if part.get("type") == "text")
        marker = "page millimetres "
        start = text.index(marker) + len(marker)
        box = json.loads(text[start : text.index("}", start) + 1])
        placed = _locate()
        detections = []
        symbols = {"Q1": "breaker", "K1": "contactor", "F1": "overload", "M1": "motor"}
        for tag, symbol in symbols.items():
            origin_x = float(placed[tag]["x"])
            origin_y = float(placed[tag]["y"])
            if not _inside(origin_x, origin_y, box):
                continue
            detections.append(
                {
                    "tag": tag,
                    "wire_number": "",
                    "symbol_class": symbol,
                    "bbox": [round(origin_x - float(box["x"]), 2), round(origin_y - float(box["y"]), 2), 30.0, 40.0],
                    "confidence": 0.9,
                }
            )
        for number, a_tag, a_pin, b_tag, b_pin in _MILD_PAIRS:
            ax, ay = _pin(placed[a_tag], a_pin)
            bx, by = _pin(placed[b_tag], b_pin)
            cx, cy = (ax + bx) / 2.0 + 4.0, (ay + by) / 2.0 + 3.5
            if not _inside(cx, cy, box):
                continue
            detections.append(
                {
                    "tag": "",
                    "wire_number": number,
                    "symbol_class": "",
                    "bbox": [round(cx - float(box["x"]) - 3.0, 2), round(cy - float(box["y"]) - 2.0, 2), 6.0, 4.0],
                    "confidence": 0.9,
                }
            )
        if _inside(120.0, 80.0, box):
            detections.append(
                {
                    "tag": "",
                    "wire_number": "A1",
                    "symbol_class": "",
                    "bbox": [round(120.0 - float(box["x"]), 2), round(80.0 - float(box["y"]), 2), 6.0, 4.0],
                    "confidence": 0.8,
                }
            )
        return {"choices": [{"message": {"content": json.dumps({"detections": detections})}}], "usage": {"cost_in_usd_ticks": 0}}

    job = ImportJob.start(
        [hard_path],
        root,
        vision=VisionSettings(
            preset="custom",
            base_url="https://example.test/v1",
            model="mock-vision",
            api_key_env="PANELCAD_TEST_VISION_KEY",
            timeout_s=8,
            page_timeout_s=180,
            max_usd=5,
            workers=1,
        ),
        transport=transport,
    )
    built = job.build()
    found: dict[str, set[frozenset[str]]] = {}
    tags = []
    for sheet in built.project.sheets:
        tags.extend(str(comp.id) for comp in sheet.components)
        for wire in sheet.wires:
            ends = frozenset({str(wire.frm).upper(), str(wire.to).upper()})
            found.setdefault(str(wire.number), set()).add(ends)
    matched = []
    for number, a_tag, a_pin, b_tag, b_pin in _MILD_PAIRS:
        wanted = frozenset({f"{a_tag}.{a_pin}".upper(), f"{b_tag}.{b_pin}".upper()})
        if wanted in found.get(number, set()):
            matched.append(number)
    report = job.ws.read_json("build.json", default={}) or {}
    assert len(matched) >= 7, {
        "matched": matched,
        "wires": {number: [sorted(ends) for ends in groups] for number, groups in found.items()},
        "unresolved": report.get("unresolved_wires"),
        "located": located,
    }
    assert "A1" not in found
    assert not any(tag.startswith("WN") for tag in tags)


@pytest.mark.skipif(not __import__("os").environ.get("XAI_API_KEY"), reason="XAI_API_KEY is not set")
def test_live_grok_on_mild_and_hard_scans(dol, tmp_path: Path):
    """Real Grok on the scan fixtures. Skipped unless XAI_API_KEY is in the environment."""

    import os

    from panelcad.importer.accuracy import score_projects, score_sheet_pair
    from panelcad.importer.vision_config import VisionSettings
    from panelcad.library.catalog import default_catalog

    assert os.environ.get("XAI_API_KEY")
    catalog = default_catalog()
    truth = dol["session"]
    rendered = rasterize_pdf(dol["pdf"], tmp_path / "rasters", dpi=160)
    mild = degrade_scan(Image.open(rendered[0]), quarter_turns=1, skew_deg=1.5, jpeg_quality=42)
    mild_path = tmp_path / "mild.png"
    mild.save(mild_path)
    settings = VisionSettings(preset="grok")
    mild_job = ImportJob.start([mild_path], tmp_path / "mild", vision=settings)
    mild_built = mild_job.build()
    mild_score = score_sheet_pair(
        truth.project.sheet("power"),
        _find_sheet(mild_built, "power"),
        catalog,
        sheet="power",
    )
    hard_paths = []
    for index, image_path in enumerate(rasterize_pdf(dol["pdf"], tmp_path / "hard", dpi=150)):
        degraded = degrade_scan(
            Image.open(image_path),
            quarter_turns=1,
            skew_deg=4.0,
            jpeg_quality=28,
            blur=1.6,
            stain=True,
            fold=True,
        )
        dest = tmp_path / f"hard-{index + 1}.jpg"
        degraded.save(dest, format="JPEG", quality=28)
        hard_paths.append(dest)
    hard_job = ImportJob.start(hard_paths, tmp_path / "hard-ws", vision=settings)
    hard_built = hard_job.build()
    hard_score = score_projects(truth.project, hard_built.project, catalog=catalog)
    report = {"mild": mild_score, "hard": hard_score, "model": settings.resolved().model}
    ART.mkdir(parents=True, exist_ok=True)
    (ART / "grok-live.json").write_text(json.dumps(report, default=str, indent=2), encoding="utf-8")
    assert "tags" in mild_score and "tags" in hard_score


@contextmanager
def _pdf_closed():
    """The raster reader is given an image. Opening the PDF is a test failure."""

    import pymupdf

    from panelcad.importer import read as read_mod
    from panelcad.importer import vectors as vectors_mod

    real_open = pymupdf.open
    real_tokens = read_mod.tokens_from_pdf
    real_strokes = vectors_mod.strokes_from_pdf

    def blocked(*_args, **_kwargs):
        raise AssertionError("pixel raster path opened a PDF")

    pymupdf.open = blocked
    read_mod.tokens_from_pdf = blocked
    vectors_mod.strokes_from_pdf = blocked
    try:
        yield
    finally:
        pymupdf.open = real_open
        read_mod.tokens_from_pdf = real_tokens
        vectors_mod.strokes_from_pdf = real_strokes


def _assert_pixel_sources(job: ImportJob) -> None:
    allowed = {"ocr", "symbol", "model", ""}
    for item in job.review_items():
        if item.get("kind") not in {"device", "wire", "text"}:
            continue
        source = str(item.get("source") or "")
        assert source != "vector"
        if item.get("kind") in {"device", "wire"}:
            assert source in {"ocr", "symbol", "model"}, item
        else:
            assert source in allowed, item
        if item.get("model_assisted"):
            assert source == "model"


def _assert_geometry(truth, built, catalog) -> None:
    from panelcad.importer.geometry import geometry_problems

    def kinds(project) -> dict[str, set[str]]:
        titles = {sheet.id: sheet.title for sheet in project.sheets}
        found: dict[str, set[str]] = {}
        for row in geometry_problems(project, catalog):
            title = titles.get(row["sheet"], row["sheet"])
            found.setdefault(title, set()).add(row["kind"])
        return found

    truth_kinds = kinds(truth)
    built_kinds = kinds(built)
    assert not built_kinds.get("Power")
    for title, problems in built_kinds.items():
        assert problems <= truth_kinds.get(title, set()), (title, problems, truth_kinds.get(title))


def _recordings_for_page(job: ImportJob, page_id: str) -> dict:
    page = job._page(page_id)
    info = page.get("classification") or {}
    src_w, src_h = float(page["width_mm"]), float(page["height_mm"])
    dst_w, dst_h = (420.0, 297.0) if src_w >= src_h else (297.0, 420.0)
    merged = job.ws.read_json("pages", page_id, "merged.json") or {}
    scaled = _scale_facts(merged, src_w, src_h, dst_w, dst_h)
    scan_page = {"id": page_id, "width_mm": dst_w, "height_mm": dst_h}
    recordings = {
        f"{page_id}:classify": {
            "kind": info.get("kind") or "other",
            "title": info.get("title") or "",
            "project": info.get("project") or "",
            "description": info.get("description") or "",
            "company": info.get("company") or "",
            "address": info.get("address") or "",
            "customer": info.get("customer") or "",
            "location": info.get("location") or "",
            "drawn_by": info.get("drawn_by") or "",
            "scale": info.get("scale") or "",
            "date": info.get("date") or "",
            "rev_description": info.get("rev_description") or "",
            "drawing_number": info.get("drawing_number") or "",
            "rev": info.get("rev") or "",
            "sheet_number": info.get("sheet_number") or "",
            "sheet_count": info.get("sheet_count") or "",
            "standard": info.get("standard") or "",
            "confidence": float(info.get("confidence") or 0.9),
        }
    }
    for tile in tile_page(scan_page):
        recordings[f"{page_id}:{tile['id']}"] = slice_facts(scaled, tile)
    return recordings


def _scale_facts(facts: dict, src_w: float, src_h: float, dst_w: float, dst_h: float) -> dict:
    sx = dst_w / src_w if src_w else 1.0
    sy = dst_h / src_h if src_h else 1.0
    out = {"devices": [], "wires": [], "xrefs": [], "texts": []}
    for key in out:
        for item in facts.get(key) or []:
            copy = dict(item)
            if copy.get("x") is not None and copy.get("y") is not None:
                copy["x"] = round(float(copy["x"]) * sx, 2)
                copy["y"] = round(float(copy["y"]) * sy, 2)
            out[key].append(copy)
    return out


def _write_artifacts(
    *,
    scan_path: Path,
    scan_job: ImportJob,
    scan_built,
    hard_job: ImportJob,
    hard_built,
    vector_score: dict,
    scan_score: dict,
    hard_score: dict,
) -> None:
    ART.mkdir(parents=True, exist_ok=True)
    Image.open(scan_path).save(ART / "fake-scan.png")
    page = scan_job._page("page_001")
    tiles = scan_job.ws.read_json("pages", "page_001", "tiles.json")
    draw_tile_overlay(page, tiles, ART / "tile-overlay.png")
    _grab_review(scan_job, ART / "review-panel.png")
    _grab_wizard(scan_path, ART / "wizard.png")
    rows = [
        ("vector PDF (full DOL)", vector_score),
        ("pixel scan (power sheet)", scan_score),
        ("harder pixel scan (full DOL)", hard_score),
    ]
    payload = {
        "rows": [
            {
                "source": label,
                "devices": score.get("devices"),
                "tags": score.get("tags"),
                "wire_numbers": score.get("wire_numbers"),
                "symbol_class": score.get("symbol_class"),
                "connectivity_precision": score.get("connectivity_precision"),
                "connectivity_recall": score.get("connectivity_recall"),
                "netlist_match": score.get("netlist_match"),
                "missing_tags": score.get("missing_tags", []),
                "missing_wires": score.get("missing_wires", []),
                "missing_edges": score.get("missing_edges", []),
                "extra_edges": score.get("extra_edges", []),
                "extra_tags": score.get("extra_tags", []),
                "extra_wires": score.get("extra_wires", []),
                "truth_devices": score.get("truth_devices"),
                "recovered_devices": score.get("recovered_devices"),
                "truth_wires": score.get("truth_wires"),
                "recovered_wires": score.get("recovered_wires"),
            }
            for label, score in rows
        ]
    }
    (ART / "accuracy.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with (ART / "accuracy.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "source",
                "tag_recall",
                "wire_number_recall",
                "symbol_class",
                "connectivity_precision",
                "connectivity_recall",
                "netlist_match",
            ]
        )
        for row in payload["rows"]:
            writer.writerow(
                [
                    row["source"],
                    _pct(row["tags"]),
                    _pct(row["wire_numbers"]),
                    _pct(row["symbol_class"]),
                    _pct(row["connectivity_precision"]),
                    _pct(row["connectivity_recall"]),
                    "yes" if row["netlist_match"] else "no",
                ]
            )
    _accuracy_png(payload["rows"], ART / "accuracy-table.png")
    _paint_sheet(scan_built, "power", ART / "rebuilt-sheet.png")
    prepared = Path(scan_job._page("page_001")["image"])
    _side_by_side(prepared, ART / "rebuilt-sheet.png", ART / "side-by-side.png", left="pixel scan", right="rebuilt from pixels")
    _paint_sheet(hard_built, "power", ART / "rebuilt-hard.png")
    hard_page = Path(hard_job._page("page_001")["image"])
    _side_by_side(hard_page, ART / "rebuilt-hard.png", ART / "side-by-side-hard.png", left="harder pixel scan", right="rebuilt from pixels")


def _find_sheet(session, name: str):
    try:
        return session.project.sheet(name)
    except KeyError:
        for sheet in session.project.sheets:
            if sheet.title == name or sheet.id == name:
                return sheet
    if session.project.sheets:
        return session.project.sheets[0]
    raise KeyError(name)


def _paint_sheet(session, sheet_id: str, dest: Path) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from panelcad.importer.preview import sheet_frame_index

    QApplication.instance() or QApplication([])
    sheet = _find_sheet(session, sheet_id)
    width_mm, height_mm = sheet.page.dimensions()
    scale = 110 / 25.4
    image = QImage(max(1, int(width_mm * scale)), max(1, int(height_mm * scale)), QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    painter.scale(scale, scale)
    from panelcad.render.page import paint_sheet

    index, count = sheet_frame_index(session.project, sheet)
    paint_sheet(
        painter,
        session.project,
        sheet,
        session.catalog,
        sheet_index=index,
        sheet_count=count,
    )
    painter.end()
    image.save(str(dest))


def _grab(widget, dest: Path, width: int, height: int) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    widget.resize(width, height)
    widget.show()
    app.processEvents()
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.white)
    widget.render(image)
    image.save(str(dest))


def _grab_review(job: ImportJob, dest: Path) -> None:
    from panelcad.gui.import_review import ImportReviewPanel

    panel = ImportReviewPanel()
    panel.set_items(job.review_items())
    _grab(panel, dest, 1500, 820)


def _grab_wizard(scan_path: Path, dest: Path) -> None:
    from panelcad.gui.import_wizard import ImportWizard

    wizard = ImportWizard()
    wizard.files.setText(str(scan_path))
    wizard.photos.setText("")
    wizard.standard.setCurrentText("auto")
    wizard.workspace.setText("import-workspace")
    wizard.log.setPlainText("Vector text is read from the file. A scan uses the vision backend you pick.")
    wizard.progress.setText("Tiles 0/0  $0.000")
    _grab(wizard, dest, 900, 720)
    wizard.close()


def _pct(value: object) -> str:
    if value is None:
        return ""
    return f"{float(value) * 100:.1f}%"


def _font(size: int, *, bold: bool = False):
    """A scalable face shipped with the tests. No system font is required."""

    from PIL import ImageFont

    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    bundled = Path(__file__).resolve().parent / "fonts" / name
    system = Path("/usr/share/fonts/truetype/dejavu") / name
    for path in (bundled, system):
        if not path.is_file():
            continue
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _side_by_side(scan: Path, rebuilt: Path, dest: Path, *, left: str = "pixel scan", right: str = "rebuilt sheet") -> None:
    """Place the scan and the rebuilt sheet on one image at the rebuilt sheet's height."""

    left_image = Image.open(scan).convert("RGB")
    right_image = Image.open(rebuilt).convert("RGB")
    height = right_image.height

    def fit(image: Image.Image) -> Image.Image:
        if image.height == height:
            return image
        if image.height < height:
            canvas = Image.new("RGB", (image.width, height), (255, 255, 255))
            canvas.paste(image, (0, (height - image.height) // 2))
            return canvas
        width = max(1, int(image.width * height / image.height))
        return image.resize((width, height), Image.Resampling.LANCZOS)

    left_image = fit(left_image)
    right_image = fit(right_image)
    label_h = 48
    gap = 28
    canvas = Image.new("RGB", (left_image.width + gap + right_image.width, height + label_h), (248, 246, 240))
    canvas.paste(left_image, (0, label_h))
    canvas.paste(right_image, (left_image.width + gap, label_h))
    draw = ImageDraw.Draw(canvas)
    font = _font(28, bold=True)
    draw.text((16, 8), left, fill=(20, 20, 20), font=font)
    draw.text((left_image.width + gap + 16, 8), right, fill=(20, 20, 20), font=font)
    canvas.save(dest)


def _accuracy_png(rows: list[dict], dest: Path) -> None:
    title_font = _font(26, bold=True)
    head_font = _font(18, bold=True)
    body_font = _font(18)
    note_font = _font(16)
    headers = [
        "source",
        "tags",
        "wire numbers",
        "symbol class",
        "conn. precision",
        "conn. recall",
        "netlist",
    ]
    xs = [24, 360, 500, 700, 900, 1120, 1320]
    width, height = 1500, 560
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width - 1, height - 1), outline=(40, 40, 40))
    complete = all(row.get("netlist_match") for row in rows)
    title = "Print import accuracy — netlist matches" if complete else "Print import accuracy"
    draw.text((24, 18), title, fill=(0, 0, 0), font=title_font)
    for x, header in zip(xs, headers, strict=True):
        draw.text((x, 70), header, fill=(0, 0, 0), font=head_font)
    draw.line((16, 104, width - 16, 104), fill=(180, 180, 180))
    for index, row in enumerate(rows):
        y = 124 + index * 44
        matched = bool(row.get("netlist_match"))
        values = [
            str(row["source"]),
            _pct(row.get("tags")),
            _pct(row.get("wire_numbers")),
            _pct(row.get("symbol_class")),
            _pct(row.get("connectivity_precision")),
            _pct(row.get("connectivity_recall")),
            "match" if matched else "differs",
        ]
        fill = (0, 0, 0) if matched else (140, 40, 20)
        for x, value in zip(xs, values, strict=True):
            draw.text((x, y), value, fill=fill, font=body_font)
    draw.text((24, 270), "Pixel rows are read from the scan. Misses:", fill=(40, 40, 40), font=note_font)
    y = 300
    for row in rows:
        bits = []
        for key, label in (
            ("missing_tags", "tags"),
            ("missing_wires", "wires"),
            ("extra_wires", "extra wires"),
            ("missing_edges", "edges"),
            ("extra_edges", "extra edges"),
        ):
            values = row.get(key) or []
            if values:
                shown = ", ".join(str(item) for item in values[:8])
                if len(values) > 8:
                    shown += f" (+{len(values) - 8})"
                bits.append(f"{label}: {shown}")
        line = f"{row['source']}: " + ("; ".join(bits) if bits else "none")
        draw.text((24, y), line[:170], fill=(40, 40, 40), font=note_font)
        y += 48
    image.save(dest)


_US_TAGS = ("10M-G", "10OL-G", "10G", "1M", "2CR", "1OL", "1PB", "3TR", "11M-G", "12OL-G")


def test_number_first_tags_border_and_title_block():
    from panelcad.importer.read import (
        TAG_RE,
        classify_text,
        is_cross_ref,
        is_device_tag,
        is_wire_label,
        label_tokens,
        read_title_block,
    )

    for tag in _US_TAGS:
        assert TAG_RE.match(tag), tag
        assert is_device_tag(tag)
        assert not is_wire_label(tag)
    assert is_wire_label("10T1") and not is_device_tag("10T1")
    assert is_wire_label("9B16B") and not is_device_tag("9B16B")
    assert is_cross_ref("[9B34]") and is_cross_ref("9B34")
    assert not is_wire_label("[9B34]")
    assert not is_device_tag("24V")
    assert is_wire_label("L1")

    width, height = 279.4, 431.8
    labeled = label_tokens(
        [
            {"text": "10OL-G", "x": 50, "y": 40, "w": 14, "h": 4.2},
            {"text": "10T1", "x": 62, "y": 90, "w": 8, "h": 2.4},
            {"text": "9B16B", "x": 90, "y": 36, "w": 10, "h": 2.2},
            {"text": "01", "x": 8, "y": 120, "w": 4, "h": 2.0},
            {"text": "A", "x": 40, "y": 6, "w": 3, "h": 2.0},
            {"text": "[9B34]", "x": 80, "y": 70, "w": 12, "h": 2.4},
            {"text": "10OL-G", "x": 100, "y": 110, "w": 12, "h": 2.2},
        ],
        width,
        height,
    )
    tags = {item["text"] for item in labeled["tags"]}
    wires = {item["text"] for item in labeled["wires"]}
    assert tags == {"10OL-G"}
    assert "10T1" in wires and "9B16B" in wires
    assert "01" not in wires and "A" not in wires and "[9B34]" not in wires
    assert "10OL-G" not in wires

    band_y = 400.0
    tokens = [
        {"text": text, "x": 30 + index * 8, "y": band_y, "h": 3.0, "w": 8}
        for index, text in enumerate(
            "ELEMENTARY DIAGRAM MOTOR STARTERS JOB NO. 380L3503 SH.NO. 9C DATE 8-3-92 DRAWN BY RODNEY JAMES".split()
        )
    ]
    block = read_title_block(tokens, height)
    assert block["drawing_number"] == "380L3503"
    assert block["sheet_number"] == "9C"
    assert block["title"] == "MOTOR STARTERS"
    assert block["date"] == "8-3-92"
    assert block["standard"] == "NEMA/ANSI"
    assert "RODNEY" in block["drawn_by"]
    noisy = [
        {"text": text, "x": 24 + index * 12, "y": band_y, "h": 3.0, "w": 10}
        for index, text in enumerate(
            "ELEMENTARY DIAGRAM MOTOR STARTERS FILE NAME DRAWN BY ROONEY JAMES WEAN DATE 8-3-92 JOE NO 380L3503 S5.NO. 9C".split()
        )
    ]
    noisy_block = read_title_block(noisy, height)
    assert noisy_block["drawing_number"] == "380L3503"
    assert noisy_block["drawing_number"] != "DRAWN"
    assert noisy_block["sheet_number"] == "9C"
    assert noisy_block["date"] == "8-3-92"
    assert noisy_block["drawn_by"] == "ROONEY JAMES"
    assert "WEAN" not in noisy_block["drawn_by"]
    assert classify_text(block["title"], block["text"]) == "schematic_power"
    iec_tokens = [
        {"text": "ELEMENTARY", "x": 40, "y": band_y, "h": 3, "w": 20},
        {"text": "DIAGRAM", "x": 70, "y": band_y, "h": 3, "w": 16},
        {"text": "STANDARD", "x": 40, "y": band_y + 6, "h": 3, "w": 18},
        {"text": "IEC", "x": 70, "y": band_y + 10, "h": 3, "w": 8},
    ]
    assert read_title_block(iec_tokens, height)["standard"] == "IEC"


def test_png_dpi_is_not_treated_as_a3(tmp_path: Path):
    from panelcad.core.geometry import match_page_size
    from panelcad.importer.ingest import ingest_paths, sheet_size_mm
    from panelcad.importer.workspace import Workspace

    small = tmp_path / "crop.png"
    Image.new("RGB", (600, 900), (255, 255, 255)).save(small, dpi=(300, 300))
    width, height = sheet_size_mm(Image.open(small))
    assert abs(width - 50.8) < 0.05
    assert abs(height - 76.2) < 0.05
    tabloid = tmp_path / "eleven.png"
    Image.new("RGB", (1100, 1700), (255, 255, 255)).save(tabloid, dpi=(100, 100))
    pages = ingest_paths([tabloid], Workspace(tmp_path / "dpi-ws"))
    assert abs(float(pages[0]["width_mm"]) - 279.4) < 0.2
    assert abs(float(pages[0]["height_mm"]) - 431.8) < 0.2
    name, orient = match_page_size(float(pages[0]["width_mm"]), float(pages[0]["height_mm"]))
    assert (name, orient) == ("TABLOID", "portrait")


def test_text_orientation_undoes_180_and_records_it(tmp_path: Path):
    from panelcad.importer.imageutil import auto_rotate
    from panelcad.importer.ingest import ingest_paths
    from panelcad.importer.workspace import Workspace

    path = _upside_down_starters(tmp_path / "turned.png", dpi=80)
    upside = Image.open(path)
    fixed, quarters = auto_rotate(upside)
    assert quarters == 2
    assert fixed.size == upside.size
    pages = ingest_paths([path], Workspace(tmp_path / "rot-ws"))
    assert int(pages[0]["rotation"]) == 180
    assert float(pages[0]["height_mm"]) > float(pages[0]["width_mm"])


def test_title_rule_beats_a_sideways_osd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A confident OSD of 270° must not leave an upside-down title band on its side."""

    from panelcad.importer import imageutil

    path = _upside_down_starters(tmp_path / "osd.png", dpi=100)
    upside = Image.open(path)
    monkeypatch.setattr(imageutil, "_dark_fraction", lambda _image: 0.2)
    monkeypatch.setattr(imageutil, "_text_scores", lambda _image: [0.0, 0.0, 0.0, 0.0])
    monkeypatch.setattr(imageutil, "_osd_reading", lambda _image: (270, 12.0))
    _fixed, quarters = imageutil.auto_rotate(upside)
    assert quarters == 2


def _line_schematic() -> Image.Image:
    """Thin rules plus a title block. A BOX thumbnail of this page is nearly blank."""

    image = Image.new("RGB", (1700, 2200), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    font = _font(64, bold=True)
    for y in range(280, 1800, 180):
        draw.line((80, y, 1620, y), fill=(0, 0, 0), width=2)
    draw.rectangle((40, 1900, 1660, 2140), outline=(0, 0, 0), width=4)
    draw.text((80, 1940), "MOTOR STARTERS", fill=(0, 0, 0), font=font)
    draw.text((80, 2020), "K1", fill=(0, 0, 0), font=font)
    return image


def test_line_drawing_rotations_come_back_upright() -> None:
    """A thin schematic turned 90, 180, or 270 is put back upright."""

    from panelcad.importer.imageutil import auto_rotate, degrade_scan

    upright = _line_schematic()
    _fixed, quarters = auto_rotate(upright)
    assert quarters == 0
    for turns in (1, 2, 3):
        scan = degrade_scan(upright, quarter_turns=turns, skew_deg=0, jpeg_quality=90, blur=0)
        _turned, found = auto_rotate(scan)
        assert found == (4 - turns) % 4
        clean = upright
        for _ in range(turns):
            clean = clean.transpose(Image.Transpose.ROTATE_270)
        _restored, found = auto_rotate(clean)
        assert found == (4 - turns) % 4


def test_wide_line_drawing_stays_upright_and_rotated_copies_return(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wide hairline sheet is read at a large probe, not an 800 px thumbnail."""

    from panelcad.importer import imageutil

    image = Image.new("RGB", (3600, 2400), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    font = _font(48, bold=True)
    draw.line((40, 70, 3500, 70), fill=(0, 0, 0), width=3)
    draw.text((400, 900), "MOTOR STARTERS", fill=(0, 0, 0), font=font)
    draw.text((400, 1000), "K1", fill=(0, 0, 0), font=font)
    monkeypatch.setattr(imageutil, "_osd_reading", lambda _image: (None, 0.0))
    _fixed, quarters = imageutil.auto_rotate(image)
    assert quarters == 0
    for turns in (1, 2, 3):
        turned = image
        for _ in range(turns):
            turned = turned.transpose(Image.Transpose.ROTATE_270)
        _restored, found = imageutil.auto_rotate(turned)
        assert found == (4 - turns) % 4


def test_small_scan_with_a_top_rule_stays_upright(monkeypatch: pytest.MonkeyPatch) -> None:
    """A short scan is enlarged before OCR, so a top rule does not turn it over."""

    from panelcad.importer import imageutil

    image = Image.new("RGB", (900, 580), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    font = _font(28, bold=True)
    draw.line((20, 18, 880, 18), fill=(0, 0, 0), width=3)
    draw.text((80, 240), "MOTOR STARTERS", fill=(0, 0, 0), font=font)
    draw.text((80, 290), "K1", fill=(0, 0, 0), font=font)
    monkeypatch.setattr(imageutil, "_osd_reading", lambda _image: (None, 0.0))
    titled = imageutil._decisive_title(image)
    assert titled is not None and titled[1] == 2
    _fixed, quarters = imageutil.auto_rotate(image)
    assert quarters == 0


def test_sample_print_rotations_come_back_upright(tmp_path: Path) -> None:
    from panelcad.importer.imageutil import auto_rotate, degrade_scan
    from panelcad.importer.ingest import rasterize_pdf

    rendered = rasterize_pdf(Path("examples/dol_starter.pdf"), tmp_path / "rasters", dpi=110)
    upright = Image.open(rendered[0])
    _fixed, quarters = auto_rotate(upright)
    assert quarters == 0
    for turns in (1, 2, 3):
        scan = degrade_scan(upright, quarter_turns=turns, skew_deg=0, jpeg_quality=90, blur=0)
        _turned, found = auto_rotate(scan)
        assert found == (4 - turns) % 4


def test_upright_line_drawing_stays_upright_when_a_rule_looks_like_a_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """Readable title words keep 0° even when a top rule would score as a 180° title band."""

    from panelcad.importer import imageutil

    image = Image.new("RGB", (1700, 2200), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    font = _font(72, bold=True)
    draw.line((60, 90, 1640, 90), fill=(0, 0, 0), width=3)
    draw.text((200, 900), "MOTOR STARTERS", fill=(0, 0, 0), font=font)
    draw.text((200, 1020), "K1", fill=(0, 0, 0), font=font)
    monkeypatch.setattr(imageutil, "_osd_reading", lambda _image: (None, 0.0))
    titled = imageutil._decisive_title(image)
    assert titled is not None and titled[1] == 2
    _fixed, quarters = imageutil.auto_rotate(image)
    assert quarters == 0


def test_text_reading_beats_a_false_title_flip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A decisive upright reading wins before a title band that would turn the page 180."""

    from panelcad.importer import imageutil

    image = Image.new("RGB", (400, 600), (255, 255, 255))
    monkeypatch.setattr(imageutil, "_dark_fraction", lambda _image: 0.0)
    monkeypatch.setattr(imageutil, "_osd_reading", lambda _image: (180, 0.4))
    monkeypatch.setattr(imageutil, "_text_scores", lambda _page: [200.0, 0.0, 0.0, 0.0])

    def title(_image):
        return _image.transpose(Image.Transpose.ROTATE_180), 2

    monkeypatch.setattr(imageutil, "_decisive_title", title)
    fixed, quarters = imageutil.auto_rotate(image)
    assert quarters == 0
    assert fixed.size == image.size


def test_sparse_page_with_confident_osd_still_turns(monkeypatch: pytest.MonkeyPatch):
    from panelcad.importer import imageutil

    image = Image.new("RGB", (400, 600), (255, 255, 255))
    monkeypatch.setattr(imageutil, "_dark_fraction", lambda _image: 0.0)
    monkeypatch.setattr(imageutil, "_osd_reading", lambda _image: (180, 9.0))
    _fixed, quarters = imageutil.auto_rotate(image)
    assert quarters == 2


def test_chat_vision_retries_a_timeout_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from panelcad.importer.vision import OpenAICompatibleVision, VisionFallback
    from panelcad.importer.vision_config import VisionSettings

    monkeypatch.setenv("PANELCAD_TEST_VISION_KEY", "retry-key")
    image = _white(tmp_path / "tile.png", (80, 60))
    calls = {"n": 0}

    def transport(method, url, payload, headers, timeout):
        calls["n"] += 1
        raise TimeoutError("slow")

    client = OpenAICompatibleVision(
        VisionSettings(
            preset="custom",
            base_url="https://example.test/v1",
            model="mock-vision",
            api_key_env="PANELCAD_TEST_VISION_KEY",
            timeout_s=2,
        ),
        transport=transport,
    )
    with pytest.raises(VisionFallback, match="timed out"):
        client.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 8, "h": 6}})
    assert calls["n"] == 2

    calls["n"] = 0

    def recover(method, url, payload, headers, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("slow")
        body = {
            "detections": [
                {
                    "tag": "K1",
                    "wire_number": "",
                    "symbol_class": "contactor",
                    "bbox": [2, 2, 4, 4],
                    "confidence": 0.9,
                }
            ]
        }
        return {"choices": [{"message": {"content": json.dumps(body)}}]}

    client._transport = recover
    data = client.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 8, "h": 6}})
    assert data["detections"][0]["tag"] == "K1"
    assert calls["n"] == 2


def test_grok_session_parent_drops_prompt_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import tempfile

    from panelcad.importer.grok_build import delete_grok_session, encoded_cwd_name

    home = tmp_path / "home"
    monkeypatch.setattr("panelcad.importer.grok_build.Path.home", lambda: home)
    uid = "44444444-4444-4444-4444-444444444444"

    def plant(job: Path) -> Path:
        parent = home / ".grok" / "sessions" / encoded_cwd_name(job)
        (parent / uid).mkdir(parents=True)
        (parent / uid / "note.txt").write_text("tile", encoding="utf-8")
        (parent / "prompt_history.jsonl").write_text("{}\n", encoding="utf-8")
        return parent

    job = tmp_path / "job"
    job.mkdir()
    try:
        parent = plant(job)
    except OSError:
        # The url-encoded cwd is one filename. A long pytest path can exceed
        # the Windows 255-character component limit and mkdir raises FileNotFoundError.
        short = Path(tempfile.mkdtemp(prefix="g"))
        job = short
        try:
            parent = plant(job)
        except OSError as exc:
            pytest.skip(f"encoded session path cannot be created: {exc}")
    assert delete_grok_session(uid, job) is True
    assert not parent.exists()


def test_empty_import_is_not_built(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.cli.agent import Agent
    from panelcad.cli.main import app
    from panelcad.importer.pipeline import EmptyImport
    from panelcad.importer.vision_config import VisionSettings

    image = _white(tmp_path / "blank.png", (240, 160))
    job = ImportJob.start([image], tmp_path / "empty-ws", vision=VisionSettings(preset="none"))
    with pytest.raises(EmptyImport, match="vision backend") as empty:
        job.build()
    assert "none was chosen" in str(empty.value)
    assert str(empty.value).lower().count("vision backend") == 1
    assert job.manifest["stage"] == "empty"
    assert job.summary()["stage"] == "empty"
    assert "built" not in str(job.summary()["stage"])
    assert job.summary()["page_seconds"]
    assert float(job.summary()["page_seconds"][0]["seconds"]) >= 0

    runner = CliRunner()
    cli = runner.invoke(
        app,
        ["import", str(image), "--vision-backend", "none", "--workspace", str(tmp_path / "cli-empty"), "--out", str(tmp_path / "empty.pcad.yaml")],
    )
    text = cli.output or ""
    stderr = getattr(cli, "stderr", None) or ""
    assert cli.exit_code == 1
    assert "Traceback" not in text and "Traceback" not in stderr
    assert "none was chosen" in text
    assert text.lower().count("a scan needs a vision backend") == 1
    assert stderr.lower().count("a scan needs a vision backend") <= 1
    assert "stage built" not in text
    assert "page page_001" in text

    agent = Agent()
    started = agent.handle(
        {
            "id": "empty-1",
            "op": "import_start",
            "path": str(image),
            "workspace": str(tmp_path / "agent-empty"),
            "vision_backend": "none",
        }
    )
    assert started["ok"] is True
    built = agent.handle({"id": "empty-2", "op": "import_build"})
    assert built["ok"] is False
    assert "Traceback" not in built["error"]
    assert "none was chosen" in built["error"]
    assert "Check the review list" not in built["error"]
    status = agent.handle({"id": "empty-3", "op": "import_status"})
    assert status["result"]["stage"] == "empty"

    from PySide6.QtWidgets import QApplication

    qt = QApplication.instance() or QApplication([])
    from panelcad.gui.import_wizard import ImportWizard

    wizard = ImportWizard()
    wizard.files.setText(str(image))
    wizard.workspace.setText(str(tmp_path / "gui-empty"))
    wizard.vision.setCurrentIndex(wizard.vision.findData("none"))
    wizard._vision_saved = VisionSettings(preset="none")
    wizard.run_import()
    deadline = __import__("time").monotonic() + 60
    while __import__("time").monotonic() < deadline:
        qt.processEvents()
        if not wizard._running and "none was chosen" in wizard.log.toPlainText():
            break
        __import__("time").sleep(0.02)
    log = wizard.log.toPlainText()
    wizard.close()
    assert "none was chosen" in log
    assert log.lower().count("a scan needs a vision backend") == 1
    assert not log.startswith("Built ")


def test_pdf_and_png_import_errors_are_one_line(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.api.session import Session
    from panelcad.cli.agent import Agent
    from panelcad.cli.main import app

    png = _white(tmp_path / "scan.png", (40, 30))
    session = Session.create("holder")
    with pytest.raises(ValueError, match="expected YAML") as sheet_error:
        session.import_sheet(png)
    assert "utf-8" not in str(sheet_error.value)
    with pytest.raises(ValueError, match="print import") as any_error:
        session.import_any(png)
    assert "Traceback" not in str(any_error.value)

    agent = Agent()
    agent.handle({"id": "hold", "op": "create_project", "name": "Holder"})
    reply = agent.handle({"id": "sheet", "op": "import_sheet", "path": str(png)})
    assert reply["ok"] is False
    assert "utf-8" not in reply["error"]
    assert "Traceback" not in reply["error"]
    assert "\n" not in reply["error"]

    mystery = tmp_path / "notes.dat"
    mystery.write_bytes(b"\x89not-a-project")
    runner = CliRunner()
    cli = runner.invoke(app, ["import", str(mystery), "--out", str(tmp_path / "nope.pcad.yaml")])
    text = (cli.output or "") + (getattr(cli, "stderr", None) or "")
    assert cli.exit_code == 1
    assert "Traceback" not in text
    assert "cannot detect format" in text


def test_import_diagnostics_trace_redacts_keys_and_labels_tag_re(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Prompt version, schema failures, and a letter-first TAG_RE miss are visible without a scan."""

    from typer.testing import CliRunner

    from panelcad.cli.main import app
    from panelcad.importer.diagnostics import HISTORICAL_TAG_RE, score_tag_rule
    from panelcad.importer.vision import (
        PROMPT_VERSION,
        TILE_PROMPT,
        OpenAICompatibleVision,
        VisionFallback,
        partition_detections,
        reset_vision_trace,
        set_vision_trace,
        validate_detections,
    )
    from panelcad.importer.vision_config import VisionSettings

    assert "number-first" in TILE_PROMPT
    assert "Put a device tag only in tag" in TILE_PROMPT
    assert "not tags printed on this crop" in TILE_PROMPT
    assert "[9B34]" in TILE_PROMPT
    assert "Omit empty fields" not in TILE_PROMPT
    assert "tag is required on every device row" in TILE_PROMPT
    assert "wire_number" in TILE_PROMPT and "from_tag" in TILE_PROMPT and "to_pin" in TILE_PROMPT
    assert '{"detections":[{"bbox":' not in TILE_PROMPT
    for echo in ("K1", "CR2", "1CR", "2OL", "10M-G"):
        assert echo not in TILE_PROMPT
    assert "bbox" in TILE_PROMPT
    assert PROMPT_VERSION
    valid, bad = partition_detections(
        {
            "detections": [
                {
                    "tag": "K1",
                    "wire_number": "",
                    "symbol_class": "contactor",
                    "bbox": [1, 2, 3, 4],
                    "confidence": 0.9,
                },
                {
                    "tag": "Q1",
                    "wire_number": "",
                    "symbol_class": "breaker",
                    "bbox": [1, 2, 3],
                    "confidence": 0.4,
                },
            ]
        }
    )
    assert valid[0]["tag"] == "K1"
    assert bad and bad[0]["loss"] == "parse_fail" and bad[0]["rule"] == "schema"
    with pytest.raises(VisionFallback):
        validate_detections({"detections": [{"tag": "Q1", "wire_number": "", "symbol_class": "", "bbox": [1], "confidence": 0.2}]})

    historical = score_tag_rule(
        [
            {
                "tag": "10M-G",
                "wire_number": "",
                "symbol_class": "contactor",
                "bbox": [10, 20, 28, 36],
                "confidence": 0.95,
            }
        ],
        HISTORICAL_TAG_RE,
        tmp_path / "hist",
    )
    row = historical["items"][0]
    assert row["loss"] == "code_reject"
    assert row["rule"] == "TAG_RE"
    assert row["reported"] is True and row["parsed"] is True and row["built"] is False

    secret = "panelcad-diag-key"
    monkeypatch.setenv("PANELCAD_DIAG_KEY", secret)
    image = _white(tmp_path / "tile.png", (32, 32))
    calls = {"n": 0}

    def transport(method, url, payload, headers, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError()
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "detections": [
                                    {
                                        "tag": "K1",
                                        "wire_number": "",
                                        "symbol_class": "contactor",
                                        "bbox": [1, 2, 8, 6],
                                        "confidence": 0.8,
                                    }
                                ]
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "cost_in_usd_ticks": 100_000_000},
        }

    caught: list[dict] = []
    token = set_vision_trace(caught.append)
    try:
        data = OpenAICompatibleVision(
            VisionSettings(
                preset="custom",
                base_url="https://example.test/v1",
                model="mock-vision",
                api_key_env="PANELCAD_DIAG_KEY",
                timeout_s=5,
            ),
            transport=transport,
        ).extract({"image": str(image), "page_mm": {"x": 1, "y": 2, "w": 3, "h": 4}})
    finally:
        reset_vision_trace(token)
    assert data["detections"][0]["tag"] == "K1"
    assert caught and caught[0]["timeout"] is True
    assert caught[0]["timeout_s"] == 5
    assert caught[0]["retry"]["count"] == 1
    assert caught[0]["retry"]["status"] == "ok"
    assert caught[0]["tokens"]["input"] == 12
    assert caught[0]["tokens"]["output"] == 4
    assert caught[0]["cost_usd"] == pytest.approx(0.01)
    assert caught[0]["prompt_version"] == PROMPT_VERSION
    assert caught[0]["backend"] == "custom"
    blob = json.dumps(caught[0])
    assert secret not in blob
    assert "Authorization" not in blob
    assert "Bearer" not in blob
    assert "base64" not in blob

    from panelcad.importer.diagnostics import ImportDiagnostics

    diag = ImportDiagnostics(tmp_path / "ws", run_id="unit")
    diag.record_exchange(caught[0], page={"id": "page_001", "rotation": 0, "dpi": 300}, tile={"id": "tile_00", "x": 0, "y": 0, "w": 10, "h": 10})
    diag.ingest_tile(
        page_id="page_001",
        tile_id="tile_00",
        tile={"id": "tile_00", "x": 0, "y": 0, "w": 10, "h": 10},
        reported_rows=data["_reported"],
        violations=[],
        filtered_rows=data["detections"],
    )
    diag.settle_page(
        page_id="page_001",
        before_devices=[{"tag": "K1"}],
        after_devices=[{"tag": "K1"}],
        before_wires=[],
        after_wires=[],
        width_mm=100,
        height_mm=100,
        catalog=None,
    )
    diag.mark_built({"placed": [{"tag": "K1"}], "wires": [], "skipped": []})
    truth = tmp_path / "truth.json"
    truth.write_text(json.dumps({"devices": ["K1", "ZZ9"]}), encoding="utf-8")
    scored = diag.score_truth(truth)
    by_text = {item["text"]: item for item in scored["truth"]}
    assert by_text["K1"]["stage"] == "built"
    assert by_text["ZZ9"]["loss"] == "model_miss"
    cli = CliRunner().invoke(app, ["import", "diag", str(diag.root), "--truth", str(truth)])
    assert cli.exit_code == 0, cli.output
    assert "model_miss" in cli.stdout
    assert secret not in cli.stdout


def test_sheet_9c_starters_from_a_mocked_vision_reply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Synthetic 11×17 elementary. Mocked vision builds the nine starters."""

    from panelcad.importer.pipeline import EmptyImport
    from panelcad.importer.vision_config import VisionSettings
    from panelcad.library.catalog import default_catalog

    monkeypatch.setenv("PANELCAD_TEST_VISION_KEY", "starter-key")
    scan = _upside_down_starters(tmp_path / "starters.png", dpi=100)
    catalog = default_catalog()
    devices = _starter_devices()
    wires = _starter_wires()

    def _pin(tag: str, pin_id: str) -> tuple[float, float]:
        tag_name, _symbol, origin_x, origin_y, library_id = next(item for item in devices if item[0] == tag)
        part = catalog.get(library_id)
        pin = next(item for item in part.pins if str(item.id) == pin_id)
        return origin_x + float(pin.x), origin_y + float(pin.y)

    def _inside(x: float, y: float, box: dict) -> bool:
        return float(box["x"]) <= x <= float(box["x"]) + float(box["w"]) and float(box["y"]) <= y <= float(box["y"]) + float(box["h"])

    def transport(method, url, payload, headers, timeout):
        if method == "GET":
            return {"data": [{"id": "mock-vision"}]}
        text = next(part["text"] for part in payload["messages"][1]["content"] if part.get("type") == "text")
        marker = "page millimetres "
        start = text.index(marker) + len(marker)
        box = json.loads(text[start : text.index("}", start) + 1])
        detections = []
        for tag, symbol, origin_x, origin_y, _library in devices:
            if not _inside(origin_x, origin_y, box):
                continue
            detections.append(
                {
                    "tag": tag,
                    "wire_number": "",
                    "symbol_class": symbol,
                    "bbox": [round(origin_x - float(box["x"]), 2), round(origin_y - float(box["y"]), 2), 28.0, 36.0],
                    "confidence": 0.95,
                }
            )
        for number, a_tag, a_pin, b_tag, b_pin in wires:
            ax, ay = _pin(a_tag, a_pin)
            bx, by = _pin(b_tag, b_pin)
            cx, cy = (ax + bx) / 2.0, (ay + by) / 2.0
            if not _inside(cx, cy, box):
                continue
            detections.append(
                {
                    "tag": "",
                    "wire_number": number,
                    "symbol_class": "",
                    "bbox": [round(cx - float(box["x"]) - 2.0, 2), round(cy - float(box["y"]) - 1.5, 2), 6.0, 3.0],
                    "confidence": 0.93,
                    "from_tag": a_tag,
                    "to_tag": b_tag,
                    "from_pin": a_pin,
                    "to_pin": b_pin,
                }
            )
        if _inside(8, 40, box):
            detections.append(
                {
                    "tag": "",
                    "wire_number": "01",
                    "symbol_class": "",
                    "bbox": [round(8 - float(box["x"]), 2), round(40 - float(box["y"]), 2), 4.0, 3.0],
                    "confidence": 0.8,
                }
            )
        if _inside(80, 170, box):
            detections.append(
                {
                    "tag": "",
                    "wire_number": "[9B34]",
                    "symbol_class": "",
                    "bbox": [round(80 - float(box["x"]), 2), round(170 - float(box["y"]), 2), 10.0, 4.0],
                    "confidence": 0.8,
                }
            )
            detections.append(
                {
                    "tag": "PLC",
                    "wire_number": "",
                    "symbol_class": "",
                    "bbox": [round(90 - float(box["x"]), 2), round(180 - float(box["y"]), 2), 12.0, 8.0],
                    "confidence": 0.7,
                }
            )
            detections.append(
                {
                    "tag": "",
                    "wire_number": "10OL-G",
                    "symbol_class": "",
                    "bbox": [round(100 - float(box["x"]), 2), round(190 - float(box["y"]), 2), 12.0, 4.0],
                    "confidence": 0.7,
                }
            )
        body = {"detections": detections}
        return {"choices": [{"message": {"content": json.dumps(body)}}], "usage": {"cost_in_usd_ticks": 0}}

    job = ImportJob.start(
        [scan],
        tmp_path / "starter-ws",
        vision=VisionSettings(
            preset="custom",
            base_url="https://example.test/v1",
            model="mock-vision",
            api_key_env="PANELCAD_TEST_VISION_KEY",
            timeout_s=8,
            page_timeout_s=180,
            max_usd=5,
            workers=1,
        ),
        transport=transport,
    )
    page = job._page("page_001")
    assert int(page.get("rotation") or 0) == 180
    assert abs(float(page["width_mm"]) - 279.4) < 1.0
    assert abs(float(page["height_mm"]) - 431.8) < 1.0
    info = page.get("classification") or {}
    assert info.get("standard") == "NEMA/ANSI", info
    assert info.get("drawing_number") == "380L3503", info
    assert str(info.get("sheet_number") or "") == "9C", info
    assert "MOTOR" in str(info.get("title") or "").upper(), info
    assert info.get("kind") == "schematic_power", info
    assert "8-3-92" in str(info.get("date") or info.get("text") or ""), info
    tiles = job.ws.read_json("pages", "page_001", "tiles.json") or []
    assert tiles
    assert all(float(tile["y"]) + float(tile["h"]) <= float(page["height_mm"]) - 55 for tile in tiles)
    reads = [job.ws.read_json("pages", "page_001", "tiles", f"{tile['id']}.json") for tile in tiles]
    assert reads and all(isinstance(item, dict) and isinstance(item.get("read"), dict) for item in reads)
    from panelcad.importer.tile import _grid

    grid = _grid(page, float(page["width_mm"]), float(page["height_mm"]))
    kept_ids = {tile["id"] for tile in tiles}
    assert any(cell["id"] == "tile_10" for cell in grid)
    for cell in grid:
        body = job.ws.read_json("pages", "page_001", "tiles", f"{cell['id']}.json")
        assert isinstance(body, dict), cell["id"]
        if cell["id"] in kept_ids:
            assert not body.get("skipped"), cell["id"]
            continue
        assert body.get("skipped") is True, cell["id"]
        assert body.get("reason") in {"blank", "title_band"}
        assert body["read"]["drops"][0]["reason"] == body["reason"]
    sample = next(item["read"] for item in reads if item["read"].get("parsed"))
    assert set(sample["tile_box"]) == {"x", "y", "w", "h"}
    assert sample["image"]["width_px"] > 0 and sample["image"]["height_px"] > 0
    assert "detections" in sample["raw_text"]
    assert isinstance(sample["parsed"], list) and sample["parsed"]
    assert isinstance(sample["drops"], list)
    assert job.summary()["page_seconds"]
    built = job.build()
    assert job.manifest["stage"] == "built"
    found = _tags(built)
    assert found == {item[0] for item in devices}
    assert "CR055" not in found and "PLC" not in found
    sheet = next(item for item in built.project.sheets if item.components)
    assert sheet.page.size == "TABLOID"
    assert sheet.page.orientation == "portrait"
    assert built.project.title_block.standard == "NEMA/ANSI"
    assert built.project.title_block.drawing_number == "380L3503"
    numbers = built.project.properties["print_import"]["sheet_numbers"]
    assert "9C" in numbers.values()
    pairs = {
        frozenset({str(wire.frm).upper(), str(wire.to).upper()})
        for item in built.project.sheets
        for wire in item.wires
    }
    present = []
    for number, a_tag, a_pin, b_tag, b_pin in wires:
        ends = frozenset({f"{a_tag}.{a_pin}".upper(), f"{b_tag}.{b_pin}".upper()})
        if ends in pairs:
            present.append((number, ends))
    assert len(present) >= 25, {"got": len(present), "wanted": len(wires)}
    by_number: dict[str, set[frozenset[str]]] = {}
    for item in built.project.sheets:
        for wire in item.wires:
            by_number.setdefault(str(wire.number), set()).add(frozenset({str(wire.frm).upper(), str(wire.to).upper()}))
    for number in ("10T1", "10T2", "10T3", "11T1", "11T2", "11T3", "12T1", "12T2", "12T3"):
        assert number in by_number
    for junk in ("[9B34]", "9B34", "01", "10OL-G", "CR055"):
        assert junk not in by_number
    assert any(item.get("text") == "PLC" and item.get("kind") == "device" for item in job.review_items())

    from typer.testing import CliRunner

    from panelcad.cli.agent import Agent
    from panelcad.cli.main import app
    from panelcad.gui.import_review import ImportReviewPanel
    from panelcad.importer.diagnostics import HISTORICAL_TAG_RE, score_tag_rule

    report = job.diagnostics_public()
    assert report["prompt_version"]
    assert report["counts"]["built"] >= 9
    traces = sorted((job.diagnostics.root / "tiles").glob("*.json"))
    assert traces
    raw_trace = "\n".join(path.read_text(encoding="utf-8") for path in traces)
    assert "starter-key" not in raw_trace
    assert "Authorization" not in raw_trace
    assert "Bearer" not in raw_trace
    trace = json.loads(traces[0].read_text(encoding="utf-8"))
    assert trace["prompt_version"] == report["prompt_version"]
    assert trace["schema_version"] == report["prompt_version"]
    assert int(trace["rotation"]) == 180
    assert abs(float(trace["dpi"]) - 100) < 1.0
    assert trace["backend"] == "custom"
    assert trace["model"] == "mock-vision"
    assert {"x", "y", "w", "h"} <= set(trace["bbox"])
    assert "number-first" in trace["request"]["prompt"]
    assert "[9B34]" in trace["request"]["prompt"]
    assert "1CR" not in trace["request"]["prompt"]
    assert "2OL" not in trace["request"]["prompt"]
    assert isinstance(trace["latency_s"], (int, float))
    assert "status" in trace["retry"]

    def _stage(kind: str, text: str) -> str:
        rows = [
            item
            for item in report["items"]
            if item.get("kind") == kind and str(item.get("text") or "").upper() == text.upper()
        ]
        if any(item.get("built") for item in rows):
            return "built"
        if not rows:
            return ""
        return str(rows[0].get("loss") or "")

    for tag, _symbol, _x, _y, _library in devices:
        assert _stage("device", tag) == "built", tag
    plc_rows = [item for item in report["items"] if item.get("kind") == "device" and item.get("text") == "PLC"]
    assert plc_rows and all(item.get("loss") == "code_reject" and item.get("rule") == "TAG_RE" for item in plc_rows)
    assert any(
        item.get("text") == "[9B34]" and item.get("loss") == "code_reject" and item.get("rule") == "XREF_RE"
        for item in report["items"]
    )

    historical = score_tag_rule(
        [
            {
                "tag": tag,
                "wire_number": "",
                "symbol_class": symbol,
                "bbox": [origin_x, origin_y, 28.0, 36.0],
                "confidence": 0.95,
            }
            for tag, symbol, origin_x, origin_y, _library in devices
        ],
        HISTORICAL_TAG_RE,
        tmp_path / "historical",
        catalog=catalog,
    )
    for tag, _symbol, _x, _y, _library in devices:
        row = next(item for item in historical["items"] if item.get("text") == tag)
        assert row["loss"] == "code_reject" and row["rule"] == "TAG_RE"
        assert row["reported"] is True and row["parsed"] is True and row["built"] is False

    truth = tmp_path / "starters-truth.yaml"
    truth.write_text("devices:\n  - 10M-G\n  - 99Z\nbuses:\n  - 9B16B\n", encoding="utf-8")
    scored = job.diagnostics.score_truth(truth)
    truth_rows = {(row["kind"], row["text"]): row for row in scored["truth"]}
    assert truth_rows[("device", "10M-G")]["stage"] == "built"
    assert truth_rows[("device", "10M-G")]["lost_at"] == ""
    assert truth_rows[("device", "99Z")]["loss"] == "model_miss"
    assert truth_rows[("device", "99Z")]["lost_at"] == "reported"
    assert truth_rows[("bus", "9B16B")]["stage"] == "built"

    assert built.import_diagnostics()["run_id"] == report["run_id"]
    agent = Agent()
    agent.print_import = job
    agent.session = built
    via_agent = agent.handle({"id": "diag-9c", "op": "import_diagnostics", "truth": str(truth)})
    assert via_agent["ok"] is True
    assert any(row.get("text") == "99Z" and row.get("loss") == "model_miss" for row in via_agent["result"]["truth"])
    cli = CliRunner().invoke(app, ["import", "diag", str(job.diagnostics.root)])
    assert cli.exit_code == 0, cli.output
    assert "built" in cli.stdout
    assert "code_reject" in cli.stdout

    from PySide6.QtWidgets import QApplication

    qt = QApplication.instance() or QApplication([])
    panel = ImportReviewPanel()
    panel.set_diagnostics(job.diagnostics.public())
    panel.tabs.setCurrentIndex(1)
    assert "code_reject" in panel.diag_counts.text()
    labels = [panel.diag_list.item(index).text() for index in range(panel.diag_list.count())]
    assert any("TAG_RE" in label and "code_reject" in label for label in labels)
    chosen = next(index for index, label in enumerate(labels) if "TAG_RE" in label or "XREF_RE" in label)
    panel.diag_list.setCurrentRow(chosen)
    qt.processEvents()
    assert panel.diag_reply.toPlainText()
    pixmap = panel.diag_crop.pixmap()
    assert pixmap is not None and not pixmap.isNull()

    pixel = ImportJob.start([scan], tmp_path / "pixel-ws", vision=VisionSettings(preset="none"))
    try:
        pixel_built = pixel.build()
    except EmptyImport:
        assert pixel.manifest["stage"] == "empty"
        return
    extra = _tags(pixel_built) - {item[0] for item in devices}
    assert not extra
    assert "CR055" not in _tags(pixel_built)


def test_ollama_native_chat_think_and_truncated_json(tmp_path: Path):
    """Ollama uses /api/chat. A thinking model can disable think. Truncated JSON is a parse failure."""

    import base64

    from panelcad.importer.diagnostics import ImportDiagnostics
    from panelcad.importer.vision import (
        DETECTION_SCHEMA,
        PROMPT_VERSION,
        TILE_PROMPT,
        OpenAICompatibleVision,
        VisionFallback,
        reset_vision_trace,
        set_vision_trace,
    )
    from panelcad.importer.vision_config import VisionSettings

    assert "wrong field" in TILE_PROMPT
    assert "page millimetres" not in TILE_PROMPT
    assert PROMPT_VERSION
    device_schema = DETECTION_SCHEMA["properties"]["devices"]["items"]
    assert device_schema["properties"]["tag"]["description"]
    assert device_schema["required"] == ["tag", "symbol_class", "bbox"]
    image = _white(tmp_path / "tile.png", (80, 60))
    encoded = base64.standard_b64encode(image.read_bytes()).decode("ascii")
    work = {"image": str(image), "page_id": "page_001", "page_mm": {"x": 0, "y": 0, "w": 80, "h": 60}}
    posts: list[dict] = []

    def native(method, url, payload, headers, timeout):
        posts.append({"url": url, "payload": payload})
        assert method == "POST"
        assert url.endswith("/api/chat")
        assert "/v1/" not in url
        assert "max_completion_tokens" not in payload
        assert "max_tokens" not in payload
        assert payload["think"] is False
        assert payload["format"]["required"] == ["devices", "conductors", "wire_numbers", "buses"]
        assert payload["options"]["num_ctx"] == 16384
        assert payload["options"]["num_predict"] == 4096
        assert payload["options"]["temperature"] == 0
        images = payload["messages"][1]["images"]
        assert images == [encoded]
        assert not images[0].startswith("data:")
        body = {
            "detections": [
                {
                    "tag": "10M-G",
                    "wire_number": "",
                    "symbol_class": "contactor",
                    "bbox": [4, 6, 18, 20],
                    "confidence": 0.9,
                }
            ]
        }
        return {
            "message": {"role": "assistant", "content": json.dumps(body), "thinking": "ignored"},
            "done_reason": "stop",
            "prompt_eval_count": 40,
            "eval_count": 11,
        }

    caught: list[dict] = []
    token = set_vision_trace(caught.append)
    try:
        data = OpenAICompatibleVision(VisionSettings(preset="ollama"), transport=native).extract(work)
    finally:
        reset_vision_trace(token)
    assert data["detections"][0]["tag"] == "10M-G"
    assert data["detections"][0]["bbox_unit"] == "crop_mm"
    assert caught[0]["bbox_units"] == ["crop_mm"]
    assert caught[0]["think"] is False
    assert caught[0]["num_ctx"] == 16384
    assert caught[0]["tokens"]["input"] == 40
    assert caught[0]["tokens"]["output"] == 11
    assert "page millimetres" in posts[0]["payload"]["messages"][1]["content"]
    assert "Put a device tag only in tag" in posts[0]["payload"]["messages"][1]["content"]

    thinking_posts: list[bool] = []

    def thinking(method, url, payload, headers, timeout):
        thinking_posts.append(payload["think"])
        body = {"detections": []}
        return {"message": {"role": "assistant", "content": json.dumps(body), "thinking": "reasoned"}}

    OpenAICompatibleVision(VisionSettings(preset="ollama", think=True), transport=thinking).extract(work)
    assert thinking_posts == [True]

    replies = ['{"detections":[', json.dumps({"detections": []})]
    prompts: list[str] = []

    def truncated(method, url, payload, headers, timeout):
        prompts.append(payload["messages"][1]["content"])
        content = replies.pop(0)
        return {"message": {"role": "assistant", "content": content}, "done_reason": "length"}

    traces: list[dict] = []
    token = set_vision_trace(traces.append)
    try:
        recovered = OpenAICompatibleVision(VisionSettings(preset="ollama"), transport=truncated).extract(work)
    finally:
        reset_vision_trace(token)
    assert recovered["detections"] == []
    assert len(prompts) == 2
    assert "nothing else" in prompts[1]
    assert "page millimetres" in prompts[1]
    assert "Put a device tag only in tag" in prompts[1]
    assert "wrong field" in prompts[1]
    assert traces[0]["truncated"] is True
    assert traces[0]["retry"]["count"] == 1
    assert traces[0]["retry"]["status"] == "ok"
    assert '{"detections":[' in str(traces[0].get("truncated_reply") or "")

    def always_cut(method, url, payload, headers, timeout):
        prompts.append("cut")
        return {"message": {"role": "assistant", "content": '{"detections":['}, "done_reason": "length"}

    prompts.clear()
    diag = ImportDiagnostics(tmp_path / "trunc", run_id="trunc")

    def hook(record: dict) -> None:
        diag.record_exchange(
            record,
            page={"id": "page_001"},
            tile={"id": "tile_00", "x": 0, "y": 0, "w": 80, "h": 60},
        )

    token = set_vision_trace(hook)
    try:
        with pytest.raises(VisionFallback, match="malformed JSON") as caught_error:
            OpenAICompatibleVision(VisionSettings(preset="ollama"), transport=always_cut).extract(work)
    finally:
        reset_vision_trace(token)
    assert "request failed" not in str(caught_error.value)
    assert len(prompts) == 2
    diag.note_fallback(
        {"id": "page_001"},
        {"id": "tile_00", "x": 0, "y": 0, "w": 80, "h": 60},
        "malformed JSON",
    )
    losses = {(item["loss"], item["rule"]) for item in diag.public()["items"]}
    assert ("parse_fail", "malformed_json") in losses
    assert all(item["loss"] != "code_reject" for item in diag.public()["items"])


def test_ollama_rows_bbox_units_and_field_placement(tmp_path: Path):
    """One bad row is dropped, {} is an empty tile, and a wire number in tag is wrong_field."""

    from panelcad.importer.diagnostics import ImportDiagnostics
    from panelcad.importer.vision import (
        OpenAICompatibleVision,
        VisionFallback,
        apply_field_placement,
        detections_to_facts,
        filter_vision_detections,
        list_ollama_vision_models,
        ollama_has_vision,
        partition_detections,
        prepare_detection_rows,
        rescale_bbox,
        suggest_plotter_od,
    )
    from panelcad.importer.vision_config import VisionSettings

    assert partition_detections({}) == ([], [])
    image = _white(tmp_path / "tile.png", (40, 30))
    calls = {"n": 0}

    def transport(method, url, payload, headers, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"message": {"role": "assistant", "content": "{}"}}
        if calls["n"] == 2:
            return {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "detections": [
                                {"tag": "K1", "wire_number": "", "symbol_class": "contactor", "bbox": [1], "confidence": 0.4},
                                {
                                    "tag": "K1",
                                    "wire_number": "",
                                    "symbol_class": "contactor",
                                    "bbox": [2, 2, 8, 6],
                                    "confidence": 0.9,
                                },
                            ]
                        }
                    ),
                }
            }
        return {"message": {"role": "assistant", "content": json.dumps({"foo": 1})}}

    client = OpenAICompatibleVision(VisionSettings(preset="ollama", timeout_s=5), transport=transport)
    work = {"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 80, "h": 60}}
    assert client.extract(work)["detections"] == []
    mixed = client.extract(work)
    assert [row["tag"] for row in mixed["detections"]] == ["K1"]
    assert mixed["_schema_violations"][0]["loss"] == "parse_fail"
    with pytest.raises(VisionFallback, match="malformed JSON"):
        client.extract(work)
    assert calls["n"] == 3

    scaled, unit = rescale_bbox([0.1, 0.2, 0.3, 0.4], tile_w=80, tile_h=60)
    assert unit == "normalized"
    assert scaled == [8.0, 12.0, 24.0, 24.0]
    kept, unit = rescale_bbox([2, 2, 4, 4], tile_w=80, tile_h=60, tile_px_w=400, tile_px_h=300)
    assert (kept, unit) == ([2.0, 2.0, 4.0, 4.0], "crop_mm")
    pixels, unit = rescale_bbox(
        [10, 10, 80, 40],
        tile_w=20,
        tile_h=15,
        tile_px_w=200,
        tile_px_h=150,
        sheet_w=200,
        sheet_h=150,
    )
    assert unit == "tile_px"
    assert pixels == [1.0, 1.0, 8.0, 4.0]
    page, unit = rescale_bbox(
        [400, 200, 80, 40],
        tile_w=20,
        tile_h=15,
        tile_px_w=40,
        tile_px_h=30,
        sheet_w=200,
        sheet_h=150,
        sheet_px_w=1000,
        sheet_px_h=750,
        origin_x=10,
        origin_y=5,
    )
    assert unit == "page_px"
    assert page == [70.0, 35.0, 16.0, 8.0]
    moved, unit = rescale_bbox(
        [40, 30, 8, 6],
        tile_w=20,
        tile_h=15,
        tile_px_w=200,
        tile_px_h=150,
        sheet_w=200,
        sheet_h=150,
        origin_x=10,
        origin_y=5,
    )
    assert unit == "page_mm"
    assert moved == [30.0, 25.0, 8.0, 6.0]

    assert suggest_plotter_od("10DL-G") == "10OL-G"
    assert suggest_plotter_od("10OL-G") == ""
    assert suggest_plotter_od("10M-G") == ""
    assert suggest_plotter_od("K1") == ""
    reported = [
        {"tag": "101", "wire_number": "", "symbol_class": "", "bbox": [2, 2, 8, 4], "confidence": 0.9},
        {"tag": "10T1", "wire_number": "", "symbol_class": "", "bbox": [12, 2, 10, 4], "confidence": 0.9},
        {"tag": "", "wire_number": "10OL-G", "symbol_class": "", "bbox": [24, 2, 12, 4], "confidence": 0.8},
        {"tag": "PLC", "wire_number": "", "symbol_class": "", "bbox": [40, 2, 12, 8], "confidence": 0.7},
        {"tag": "10DL-G", "wire_number": "", "symbol_class": "overload", "bbox": [54, 2, 16, 10], "confidence": 0.9},
    ]
    prepared, units = prepare_detection_rows(reported, {"page_mm": {"x": 0, "y": 0, "w": 80, "h": 60}})
    assert units == ["crop_mm"] * 5
    assert prepared[0]["wire_number"] == "101" and prepared[0]["tag"] == "" and prepared[0]["wrong_field"] == "tag"
    assert prepared[1]["wire_number"] == "10T1" and prepared[1]["wrong_field"] == "tag"
    assert prepared[2]["wire_number"] == "10OL-G" and prepared[2]["tag"] == "" and prepared[2]["wrong_field"] == "wire_number"
    assert "wrong_field" not in apply_field_placement([{"tag": "10M-G", "wire_number": "", "bbox": [1, 1, 4, 4]}])[0]
    filtered = filter_vision_detections(prepared, None, {"x": 0, "y": 0, "w": 80, "h": 60})
    overload = next(row for row in filtered if row.get("tag") == "10DL-G")
    assert overload["suggested_tag"] == "10OL-G"
    assert overload["suggestion"] == "plotter O/D"
    assert overload["tag"] == "10DL-G"
    from panelcad.library.catalog import default_catalog

    facts = detections_to_facts(filtered, {"x": 0, "y": 0, "w": 80, "h": 60}, default_catalog())
    assert {item["tag"] for item in facts["devices"]} == {"PLC", "10DL-G"}
    assert any(item["tag"] == "10DL-G" and item["suggested_tag"] == "10OL-G" for item in facts["devices"])
    assert {item["number"] for item in facts["wires"]} >= {"101", "10T1", "10OL-G"}
    diag = ImportDiagnostics(tmp_path / "fields", run_id="fields")
    diag.ingest_tile(
        page_id="page_001",
        tile_id="tile_00",
        tile={"x": 0, "y": 0, "w": 80, "h": 60},
        reported_rows=reported,
        violations=[],
        filtered_rows=filtered,
    )
    by_text = {(item["text"], item["loss"], item["rule"]) for item in diag.public()["items"]}
    assert ("101", "wrong_field", "tag") in by_text
    assert ("10T1", "wrong_field", "tag") in by_text
    assert ("10OL-G", "wrong_field", "wire_number") in by_text
    assert ("PLC", "code_reject", "TAG_RE") in by_text
    assert ("10DL-G", "", "") in by_text or any(item["text"] == "10DL-G" and item["loss"] == "" for item in diag.public()["items"])

    def show(method, url, payload, headers, timeout):
        if url.endswith("/api/tags"):
            return {"models": [{"name": "gemma3:4b"}, {"name": "llama3.2:3b"}, {"name": "qwen3-vl:8b-instruct"}]}
        name = (payload or {}).get("model")
        if name == "llama3.2:3b":
            return {"capabilities": ["completion"]}
        if name == "qwen3-vl:8b-instruct":
            return {"model_info": {"qwen3vl.vision.projector": {}}}
        return {"capabilities": ["completion", "vision"]}

    assert list_ollama_vision_models("http://127.0.0.1:11434/v1", transport=show) == [
        "gemma3:4b",
        "qwen3-vl:8b-instruct",
    ]
    assert ollama_has_vision({"capabilities": ["vision"]}) is True
    assert ollama_has_vision({"capabilities": ["completion"]}) is False

    def blind(method, url, payload, headers, timeout):
        if url.endswith("/api/show"):
            raise RuntimeError("show failed")
        return {"models": [{"name": "gemma3:4b"}]}

    assert list_ollama_vision_models("http://127.0.0.1:11434/v1", transport=blind) == []


def test_vision_timeouts_and_backends_stay_in_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """CLI, JSONL, Session, and the GUI expose the same request and page clocks."""

    import inspect

    from typer.testing import CliRunner

    from panelcad.ai.drawing import import_start
    from panelcad.api.session import Session
    from panelcad.cli.agent import Agent
    from panelcad.cli.main import app
    from panelcad.gui.import_wizard import describe_import_result
    from panelcad.importer.pipeline import EmptyImport, ImportJob
    from panelcad.importer.vision import OpenAICompatibleVision
    from panelcad.importer.vision_config import VisionSettings, vision_from_options

    ollama = VisionSettings(preset="ollama").resolved()
    assert ollama.model == "gemma3:4b"
    assert ollama.timeout_s == 300
    assert ollama.page_timeout_s == 3600
    assert ollama.num_ctx == 16384
    assert ollama.think is False
    assert ollama.num_predict == 4096
    assert vision_from_options(preset="ollama").workers == 1
    assert vision_from_options(preset="ollama", workers=3).workers == 3
    assert vision_from_options(preset="grok-build", workers=2).workers == 2
    explicit = VisionSettings(preset="ollama", timeout_s=240, page_timeout_s=900, num_ctx=8192).resolved()
    assert (explicit.timeout_s, explicit.page_timeout_s, explicit.num_ctx) == (240, 900, 8192)
    assert VisionSettings(preset="ollama", timeout_s=91).resolved().timeout_s == 91
    assert vision_from_options(preset="ollama", think="off").think is False
    assert vision_from_options(preset="ollama", think="on").think is True

    image = _white(tmp_path / "clock.png", (32, 24))
    client = OpenAICompatibleVision(VisionSettings(preset="ollama", page_timeout_s=5))
    client.begin_job()
    client.begin_page("page_001")
    client._page_clocks["page_001"] = __import__("time").monotonic() - 100
    client.begin_page("page_002")
    assert client.over_budget("page_001") == "page timeout"
    assert client.over_budget("page_002") == ""
    assert client.job_stop() == ""

    seen: dict[str, dict] = {}

    def chat(method, url, payload, headers, timeout):
        seen["url"] = url
        seen["payload"] = payload
        return {"choices": [{"message": {"content": json.dumps({"detections": []})}}]}

    monkeypatch.setenv("PANELCAD_TEST_VISION_KEY", "clock-key")
    tile = _white(tmp_path / "chat.png", (32, 24))
    OpenAICompatibleVision(
        VisionSettings(
            preset="custom",
            base_url="https://example.test/v1",
            model="mock-vision",
            api_key_env="PANELCAD_TEST_VISION_KEY",
            timeout_s=5,
            reasoning_effort="low",
            num_predict=640,
        ),
        transport=chat,
    ).extract({"image": str(tile), "page_mm": {"x": 0, "y": 0, "w": 8, "h": 6}})
    assert seen["url"].endswith("/chat/completions")
    assert seen["payload"]["max_tokens"] == 640
    assert "max_completion_tokens" not in seen["payload"]
    assert seen["payload"]["reasoning_effort"] == "low"
    OpenAICompatibleVision(
        VisionSettings(
            preset="grok",
            model="grok-4.7",
            api_key_env="PANELCAD_TEST_VISION_KEY",
            timeout_s=5,
        ),
        transport=chat,
    ).extract({"image": str(tile), "page_mm": {"x": 0, "y": 0, "w": 8, "h": 6}})
    assert seen["payload"]["max_completion_tokens"] == 4096
    assert "max_tokens" not in seen["payload"]
    assert "reasoning_effort" not in seen["payload"]

    signature = inspect.signature(import_start)
    for name in (
        "vision_timeout_s",
        "vision_page_timeout_s",
        "num_ctx",
        "num_predict",
        "think",
        "reasoning_effort",
    ):
        assert name in signature.parameters

    runner = CliRunner()
    cli = runner.invoke(
        app,
        [
            "import",
            str(image),
            "--vision-backend",
            "none",
            "--workspace",
            str(tmp_path / "cli-ws"),
            "--out",
            str(tmp_path / "cli.pcad.yaml"),
            "--timeout",
            "240",
            "--page-timeout",
            "900",
            "--num-ctx",
            "8192",
            "--num-predict",
            "800",
            "--think",
            "off",
            "--reasoning-effort",
            "low",
        ],
    )
    assert cli.exit_code == 1
    cli_vision = json.loads((tmp_path / "cli-ws" / "manifest.json").read_text(encoding="utf-8"))["vision"]
    assert cli_vision["timeout_s"] == 240
    assert cli_vision["page_timeout_s"] == 900
    assert cli_vision["num_ctx"] == 8192
    assert cli_vision["num_predict"] == 800
    assert cli_vision["think"] is False
    assert cli_vision["reasoning_effort"] == "low"

    agent = Agent()
    started = agent.handle(
        {
            "id": "parity-1",
            "op": "import_start",
            "path": str(image),
            "workspace": str(tmp_path / "jsonl-ws"),
            "vision_backend": "none",
            "vision_timeout_s": 240,
            "vision_page_timeout_s": 900,
            "num_ctx": 8192,
            "num_predict": 800,
            "think": False,
            "reasoning_effort": "low",
        }
    )
    assert started["ok"] is True
    jsonl_vision = started["result"]["vision"]
    assert jsonl_vision["timeout_s"] == 240
    assert jsonl_vision["page_timeout_s"] == 900
    assert jsonl_vision["num_ctx"] == 8192
    assert jsonl_vision["num_predict"] == 800
    assert jsonl_vision["think"] is False
    assert jsonl_vision["reasoning_effort"] == "low"

    session = Session.create("parity")
    with pytest.raises(EmptyImport):
        session.import_print(
            image,
            workspace=tmp_path / "session-ws",
            vision_backend="none",
            vision_timeout_s=240,
            vision_page_timeout_s=900,
            num_ctx=8192,
            num_predict=800,
            think=False,
            reasoning_effort="low",
        )
    session_vision = json.loads((tmp_path / "session-ws" / "manifest.json").read_text(encoding="utf-8"))["vision"]
    assert session_vision["timeout_s"] == 240
    assert session_vision["page_timeout_s"] == 900
    assert session_vision["num_predict"] == 800

    job = ImportJob.prepare([image], tmp_path / "progress", vision=VisionSettings(preset="none"))
    job._spent_usd = 1.25
    job._input_tokens = 9
    job._estimated_usd = 0.4
    job._fallbacks = 2
    job._fallback_reasons = ["tile_00: malformed JSON", "tile_01: page timeout"]

    def client_for(preset: str):
        class Holder:
            pass

        holder = Holder()
        holder.preset = preset
        backend = Holder()
        backend.settings = holder
        return backend

    job._vision_client = lambda: client_for("ollama")
    local = job._tile_progress(1, 4)
    assert "$" not in local
    assert "fallbacks 2" in local
    job._vision_client = lambda: client_for("custom")
    assert "$" in job._tile_progress(1, 4)
    assert "fallback_reasons" in job.summary()
    assert job.summary()["fallback_reasons"][0].startswith("tile_00:")

    built = describe_import_result(
        name="Sheet",
        pages="page_001",
        stage="built",
        fallbacks=0,
        devices=2,
        reasons=[],
        preset="custom",
        project="out.pcad.yaml",
    )
    assert built.startswith("Built Sheet")
    missed = describe_import_result(
        name="Sheet",
        pages="page_001",
        stage="built",
        fallbacks=2,
        devices=2,
        reasons=["tile_00: malformed JSON"],
        preset="ollama",
        project="out.pcad.yaml",
    )
    assert not missed.startswith("Built")
    assert "tile_00: malformed JSON" in missed
    assert "$" not in missed
    empty = describe_import_result(
        name="Sheet",
        pages="page_001",
        stage="empty",
        fallbacks=0,
        devices=0,
        reasons=[],
        preset="none",
        project="",
    )
    assert empty.startswith("Import did not build a sheet")

    monkeypatch.setattr(
        "panelcad.gui.import_wizard.list_ollama_vision_models",
        lambda *args, **kwargs: ["gemma3:4b", "qwen3-vl:8b-instruct"],
    )
    monkeypatch.setattr(
        "panelcad.gui.vision_settings.list_ollama_vision_models",
        lambda *args, **kwargs: ["gemma3:4b", "qwen3-vl:8b-instruct"],
    )
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.import_review import ImportReviewPanel
    from panelcad.gui.import_wizard import ImportWizard
    from panelcad.gui.vision_settings import VisionSettingsDialog

    QApplication.instance() or QApplication([])
    dialog = VisionSettingsDialog(path=tmp_path / "vision.json")
    dialog.preset.setCurrentIndex(dialog.preset.findData("ollama"))
    assert dialog.model.text() == "gemma3:4b"
    assert dialog.timeout.value() == 300
    assert dialog.page_timeout.value() == 3600
    assert dialog.num_ctx.value() == 16384
    assert dialog.think.isChecked() is False
    assert dialog.preset.accessibleName() == "Vision backend"
    assert dialog.model.accessibleName() == "Vision model"
    assert "gemma3:4b" in dialog.docs.text()
    dialog.model.setText("qwen3-vl:8b-instruct")
    dialog._refresh_models()
    assert dialog.model.text() == "qwen3-vl:8b-instruct"
    dialog.close()

    wizard = ImportWizard()
    wizard.vision.setCurrentIndex(wizard.vision.findData("none"))
    wizard.vision_model.setText("")
    wizard.vision.setCurrentIndex(wizard.vision.findData("ollama"))
    assert wizard.vision_model.text() == "gemma3:4b"
    assert wizard.vision.accessibleName() == "Vision backend"
    assert wizard.vision_model.accessibleName() == "Vision model"
    assert wizard.timeout.value() == 300
    assert wizard.page_timeout.value() == 3600
    wizard.timeout.setValue(240)
    wizard.page_timeout.setValue(900)
    wizard.num_ctx.setValue(8192)
    wizard.num_predict.setValue(800)
    wizard.think.setChecked(True)
    wizard.effort.setCurrentText("low")
    chosen = wizard.vision_settings()
    assert chosen.model == "gemma3:4b"
    assert chosen.timeout_s == 240
    assert chosen.page_timeout_s == 900
    assert chosen.num_ctx == 8192
    assert chosen.num_predict == 800
    assert chosen.think is True
    assert chosen.reasoning_effort == "low"
    wizard.close()

    panel = ImportReviewPanel()
    panel.set_items(
        [
            {
                "kind": "device",
                "text": "10DL-G",
                "status": "pending",
                "confidence": 0.9,
                "source": "model",
                "model_assisted": True,
                "payload": {"suggested_tag": "10OL-G", "needs_review": True, "suggestion": "plotter O/D"},
            }
        ]
    )
    label = panel.list.item(0).text()
    assert "10DL-G" in label and "suggest 10OL-G" in label
    assert len(label) < 140
    panel.set_diagnostics(
        {
            "prompt_version": "2026-10-09.2",
            "counts": {},
            "loss_counts": {"wrong_field": 2, "code_reject": 1},
            "items": [{"loss": "wrong_field", "rule": "tag", "kind": "device", "text": "101"}],
        }
    )
    assert "wrong_field 2" in panel.diag_counts.text()
    panel.close()


def _starter_devices() -> list[tuple[str, str, float, float, str]]:
    columns = (46.0, 128.0, 208.0)
    rows = (("M-G", "contactor", 55.0, "nema.contactors.ac3_32a"), ("OL-G", "overload", 155.0, "nema.protection.overload"), ("G", "motor", 255.0, "nema.loads.motor_3ph"))
    devices = []
    for prefix, x in zip(("10", "11", "12"), columns):
        for suffix, symbol, y, library_id in rows:
            devices.append((f"{prefix}{suffix}", symbol, x, y, library_id))
    return devices


def _starter_wires() -> list[tuple[str, str, str, str, str]]:
    wires: list[tuple[str, str, str, str, str]] = []
    phases = (("L1", "T1", "2", "T1", "B"), ("L2", "T2", "4", "T2", "C"), ("L3", "T3", "6", "T3", "D"))
    for index, prefix in enumerate(("10", "11", "12")):
        contactor = f"{prefix}M-G"
        overload = f"{prefix}OL-G"
        motor = f"{prefix}G"
        for line, load, element, motor_pin, _bus in phases:
            wires.append(("", contactor, load, overload, load))
            wires.append((f"{prefix}T{load[-1]}", overload, element, motor, motor_pin))
        for line, _load, _element, _motor_pin, bus in phases:
            left = f"{prefix}M-G"
            # Phase ties are added once, below.
            _ = (index, line, bus, left)
    for line, _load, _element, _motor_pin, bus in phases:
        number = f"9B16{bus}"
        tags = [f"{prefix}M-G" for prefix in ("10", "11", "12")]
        wires.append((number, tags[0], line, tags[1], line))
        wires.append((number, tags[1], line, tags[2], line))
        wires.append((number, tags[0], line, tags[2], line))
    return wires


def _upside_down_starters(path: Path, *, dpi: int) -> Path:
    width_mm, height_mm = 279.4, 431.8

    def px(mm: float) -> int:
        return int(round(mm / 25.4 * dpi))

    image = Image.new("RGB", (px(width_mm), px(height_mm)), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    font = _font(max(18, dpi // 4), bold=True)
    small = _font(max(14, dpi // 5))
    for index, letter in enumerate("ABCDEFGHJKLMN"):
        draw.text((px(28 + index * 16), px(8)), letter, fill=(0, 0, 0), font=small)
    for index in range(1, 12):
        draw.text((px(6), px(24 + index * 22)), f"{index:02d}", fill=(0, 0, 0), font=small)
    for _tag, _symbol, origin_x, origin_y, _library in _starter_devices():
        draw.rectangle((px(origin_x), px(origin_y), px(origin_x + 30), px(origin_y + 40)), outline=(10, 10, 10), width=3)
        draw.text((px(origin_x + 2), px(origin_y + 8)), "M", fill=(0, 0, 0), font=small)
    draw.text((px(78), px(168)), "[9B34]", fill=(0, 0, 0), font=font)
    top = px(height_mm - 52)
    draw.rectangle((px(16), top, image.width - px(10), image.height - px(6)), outline=(0, 0, 0), width=3)
    draw.text((px(24), top + 6), "ELEMENTARY DIAGRAM", fill=(0, 0, 0), font=font)
    draw.text((px(24), top + px(8)), "MOTOR STARTERS", fill=(0, 0, 0), font=font)
    draw.text((px(24), top + px(16)), "JOB NO. 380L3503   SH.NO. 9C", fill=(0, 0, 0), font=small)
    draw.text((px(24), top + px(24)), "DATE 8-3-92   DRAWN BY RODNEY JAMES", fill=(0, 0, 0), font=small)
    draw.text((px(24), top + px(32)), "CR055", fill=(0, 0, 0), font=font)
    image.transpose(Image.Transpose.ROTATE_180).save(path, dpi=(dpi, dpi))
    return path


def _customer_sheet_fixture(name: str) -> Path:
    path = Path(__file__).resolve().parent / "fixtures" / name
    if not path.is_file():
        pytest.skip(f"customer sheet fixture is not in this tree: {name}")
    return path


def test_sheet_9c_funnel_scores_wrong_pins_offline(tmp_path: Path):
    """Sheet 9C ground truth, recorded rows, and a tiny frame. No live model."""

    import json

    import numpy as np
    import pymupdf
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.import_review import ImportReviewPanel
    from panelcad.importer.diagnostics import (
        ImportDiagnostics,
        load_truth,
        normalize_truth,
        resolve_run_dir,
    )
    from panelcad.importer.ingest import ingest_paths
    from panelcad.importer.raster import (
        _mark_caption_tags,
        _pin_points,
        detect_frame_insets,
        orient_ladder_devices,
    )
    from panelcad.importer.symbols import resolve_symbol
    from panelcad.importer.tile import _merge_device
    from panelcad.importer.vision import apply_field_placement, detections_to_facts, prepare_detection_rows, rescale_bbox
    from panelcad.importer.workspace import Workspace
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    recorded = json.loads(_customer_sheet_fixture("sheet_9c_replies.json").read_text(encoding="utf-8"))
    truth = _customer_sheet_fixture("sheet_9c_truth.yaml")
    rows = recorded["rows"]

    facts = detections_to_facts(rows, {"x": 0, "y": 0, "w": 80, "h": 60, "id": "tile_00"}, catalog)
    assert any(device["tag"] == "10OL-G" for device in facts["devices"])
    assert any(wire["number"] == "10T1" for wire in facts["wires"])

    scaled, unit = rescale_bbox(
        recorded["qwen_bbox"],
        tile_w=130,
        tile_h=100,
    )
    assert unit == "qwen_1000"
    assert abs(scaled[0] - 80.6) < 0.05
    echo, echo_units = prepare_detection_rows(
        [{"tag": "", "wire_number": "12T2", "bbox": recorded["crop_echo_bbox"], "confidence": 0.4}],
        {"page_mm": {"x": 102, "y": 204, "w": 130, "h": 130}},
    )
    assert echo_units == ["unlocalised"]
    assert echo[0]["unlocalised"] is True

    contact = resolve_symbol(catalog, tag="10M-G", type_text="contact", standard="NEMA/ANSI")
    assert "contactor" in contact["library_id"]
    assert "coil" not in contact["library_id"]
    motor = resolve_symbol(catalog, tag="10G", type_text="coil", standard="NEMA/ANSI")
    assert "motor" in motor["library_id"]
    merged_devices: list[dict] = []
    _merge_device(
        merged_devices,
        {"tag": "10M-G", "symbol": "contact", "library_id": "iec.relays.k_4co_24vdc", "confidence": 0.95, "x": 10, "y": 10},
    )
    _merge_device(
        merged_devices,
        {
            "tag": "10M-G",
            "symbol": "contactor",
            "library_id": "nema.contactors.ac3_32a",
            "confidence": 0.4,
            "x": 40,
            "y": 10,
        },
    )
    assert len(merged_devices) == 2
    assert {item["library_id"] for item in merged_devices} == {
        "iec.relays.k_4co_24vdc",
        "nema.contactors.ac3_32a",
    }

    horizontal = [
        {"tag": "10M-G", "library_id": "nema.contactors.ac3_32a", "symbol": "contactor", "x": 100, "y": 80},
        {"tag": "10OL-G", "library_id": "nema.protection.overload", "symbol": "overload", "x": 130, "y": 82},
        {"tag": "10G", "library_id": "nema.loads.motor_3ph", "symbol": "motor", "x": 155, "y": 81},
    ]
    orient_ladder_devices(horizontal, catalog, spread=True)
    assert [device["rotation"] for device in horizontal] == [270, 270, 270]
    assert horizontal[1]["x"] >= 179
    assert horizontal[0]["y"] == 80
    vertical = [
        {"tag": "10M-G", "library_id": "nema.contactors.ac3_32a", "symbol": "contactor", "x": 46, "y": 55},
        {"tag": "10OL-G", "library_id": "nema.protection.overload", "symbol": "overload", "x": 46, "y": 155},
        {"tag": "10G", "library_id": "nema.loads.motor_3ph", "symbol": "motor", "x": 46, "y": 255},
    ]
    orient_ladder_devices(vertical, catalog, spread=True)
    assert all(int(device.get("rotation") or 0) == 0 for device in vertical)
    assert [device["y"] for device in vertical] == [55, 155, 255]
    pins = _pin_points(
        [{"tag": "10M-G", "library_id": "nema.contactors.ac3_32a", "x": 100, "y": 80, "rotation": 270}],
        catalog,
    )
    line = next(pin for pin in pins if pin[1] == "L1")
    load = next(pin for pin in pins if pin[1] == "T1")
    assert abs(line[2] - 100) < 1
    assert load[2] > 160

    caption = [{"tag": "CR055", "guess": True, "x": 40, "y": 40, "library_id": "iec.relays.k_4co_24vdc"}]
    prose = [
        {"text": "CROSS", "x": 20, "y": 40},
        {"text": "CHIP", "x": 55, "y": 40},
        {"text": "CONVEYOR", "x": 80, "y": 40},
    ]
    _mark_caption_tags(caption, prose)
    assert caption[0]["title_ink"] is True and caption[0]["library_id"] == ""
    power = [{"tag": "10M-G", "guess": True, "x": 40, "y": 40, "library_id": "nema.contactors.ac3_32a"}]
    _mark_caption_tags(power, prose)
    assert not power[0].get("title_ink")

    printed = normalize_truth(
        {"connections": [{"number": "10T1", "from": "10OL-G.T1", "to": "10G.T1", "style": "printed"}]}
    )
    assert "10OL-G.2" in printed[0]["pins"]
    library_pins = normalize_truth({"connections": [{"number": "10T1", "from": "10OL-G.T1", "to": "10G.T1"}]})
    assert "10OL-G.T1" in library_pins[0]["pins"]

    workspace = tmp_path / "sheet9c"
    diag = ImportDiagnostics(workspace, run_id="recorded")
    diag.ingest_tile(
        page_id="page_001",
        tile_id="tile_04",
        tile={"id": "tile_04", "x": 0, "y": 0, "w": 80, "h": 60},
        reported_rows=rows,
        violations=[],
        filtered_rows=apply_field_placement([dict(row) for row in rows]),
    )
    items = diag.public()["items"]
    assert any(item["kind"] == "bus" and item["text"] == "9B16B" and item["loss"] == "wrong_field" for item in items)
    assert any(item["kind"] == "device" and item["text"] == "10OL-G" and item["loss"] == "" for item in items)
    assert any(item["kind"] == "wire" and item["text"] == "10T1" and item["loss"] == "" for item in items)
    diag.settle_page(
        page_id="page_001",
        before_devices=[{"tag": "10OL-G"}, {"tag": "10M-G"}],
        after_devices=[{"tag": "10OL-G"}, {"tag": "10M-G"}],
        before_wires=[{"number": "10T1", "x": 24, "y": 34, "source": "model"}],
        after_wires=[{"number": "10T1", "x": 24, "y": 34}],
        width_mm=279.4,
        height_mm=431.8,
        catalog=catalog,
    )
    settled = {(item["text"], item["rule"]) for item in diag.public()["items"]}
    assert ("12T3", "dropped_field") in settled
    assert ("10T1", "dedupe") not in settled
    diag.truth_path = str(truth)
    diag.mark_built(recorded["wrong_pin_report"])
    scored = diag.write()
    wire = next(item for item in scored["items"] if item.get("kind") == "wire" and item.get("text") == "10T1")
    assert wire["built"] is False
    assert wire["loss"] == "wrong_connection"
    assert wire["rule"] == "pins"
    truth_wire = next(item for item in scored["truth"] if item.get("kind") == "wire" and item.get("text") == "10T1")
    assert truth_wire["loss"] == "wrong_connection"
    bus = next(item for item in scored["truth"] if item.get("kind") == "bus" and item.get("text") == "9B16B")
    assert bus["loss"] != "model_miss"

    pixel = ImportDiagnostics(workspace, run_id="pixel")
    pixel.ingest_pixel(
        page_id="page_001",
        devices=[{"tag": "10M-G", "library_id": "nema.contactors.ac3_32a"}],
        wires=[{"number": "10T1"}],
    )
    pixel.truth_path = str(truth)
    pixel.mark_built({"placed": [{"tag": "10M-G"}], "wires": [], "skipped": []})
    pixel_doc = pixel.write()
    missed = next(item for item in pixel_doc["truth"] if item.get("text") == "11M-G")
    assert missed["loss"] == "ocr_miss"
    assert missed["rule"] == "ocr"
    assert missed["loss"] != "build_fail"
    assert missed["loss"] != "model_miss"
    built = next(item for item in pixel_doc["items"] if item.get("text") == "10M-G" and item.get("source") == "pixel")
    assert built["built"] is True
    assert resolve_run_dir(workspace).name in {"recorded", "pixel"}
    assert (resolve_run_dir(workspace) / "funnel.json").is_file()

    blank = np.zeros((200, 300), dtype=np.uint8)
    assert detect_frame_insets(blank, 100, 80) is None
    frame = np.zeros((400, 600), dtype=np.uint8)
    ppm = 2
    inset_px = int(31 * ppm)
    frame[inset_px, inset_px:-inset_px] = 255
    frame[-inset_px, inset_px:-inset_px] = 255
    frame[inset_px:-inset_px, inset_px] = 255
    frame[inset_px:-inset_px, -inset_px] = 255
    found = detect_frame_insets(frame, 300, 200)
    assert found is not None
    assert found[0] >= 40

    doc = pymupdf.open()
    page = doc.new_page(width=72, height=72)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 300), 0)
    pix.clear_with(255)
    page.insert_image(page.rect, pixmap=pix)
    scan_pdf = tmp_path / "scan.pdf"
    doc.save(str(scan_pdf))
    doc.close()
    pages = ingest_paths([scan_pdf], Workspace(tmp_path / "pdf-ws"))
    assert pages[0]["vector"] is False
    assert int(pages[0]["dpi"]) >= 150

    qt = QApplication.instance() or QApplication([])
    panel = ImportReviewPanel()
    panel.set_items(
        [{"kind": "device", "text": "10M-G", "confidence": 0.2, "source": "symbol", "payload": {"needs_review": True}}]
    )
    panel.set_diagnostics({"prompt_version": "2026-10-09.3", "path": str(pixel.root), "counts": {}, "loss_counts": {}, "items": []})
    assert "Nothing was lost" not in panel.diag_reply.toPlainText()
    panel.set_items([])
    panel.set_diagnostics(
        {"prompt_version": "2026-10-09.3", "counts": {}, "loss_counts": {"model_miss": 9}, "items": []}
    )
    assert "Nothing was lost" not in panel.diag_reply.toPlainText()
    panel.set_diagnostics({"prompt_version": "2026-10-09.3", "counts": {}, "loss_counts": {}, "items": []})
    assert panel.diag_reply.toPlainText() == "Nothing was lost."
    panel.truth_path.setText(str(truth))
    panel._diag = {"path": str(pixel.root)}
    panel._apply_truth_path()
    assert "11M-G" in panel.diag_counts.text() or panel.diag_list.count() > 0
    assert load_truth(truth)
    panel.close()
    qt.processEvents()


def _tile04() -> dict:
    return json.loads(_customer_sheet_fixture("sheet_9c_tile04.json").read_text(encoding="utf-8"))


def _reply_facts(payload: dict, catalog, *, page_mm: dict | None = None):
    """The same partition, prepare, and filter the vision backends use."""

    from panelcad.importer.vision import (
        detections_to_facts,
        filter_vision_detections,
        partition_detections,
        prepare_detection_rows,
    )

    valid, violations = partition_detections(payload)
    tile = page_mm or {"x": 0, "y": 0, "w": 200, "h": 250}
    prepared, units = prepare_detection_rows(valid, {"page_mm": tile})
    filtered = filter_vision_detections(prepared, catalog, tile, standard="NEMA/ANSI")
    facts = detections_to_facts(filtered, tile, catalog, standard="NEMA/ANSI")
    return facts, violations, units


def _build_facts(facts: dict, catalog, *, width_mm: float = 279.4, height_mm: float = 431.8):
    from panelcad.importer.build import build_session

    stitched = {
        "standard": "NEMA/ANSI",
        "title_block": {"standard": "NEMA/ANSI", "drawing_number": "380L3503"},
        "sheets": [
            {
                "id": "power",
                "kind": "schematic",
                "width_mm": width_mm,
                "height_mm": height_mm,
                "devices": facts["devices"],
                "wires": facts["wires"],
            }
        ],
    }
    return build_session(stitched, catalog=catalog)


def _pin_pairs(session) -> set[frozenset[str]]:
    return {
        frozenset({str(wire.frm).upper(), str(wire.to).upper()})
        for sheet in session.project.sheets
        for wire in sheet.wires
    }


def test_sheet_9c_tile04_grok_reply_builds_27_connections():
    """The saved Grok tile draws no wire between a contactor and its overload.

    Those nine T1–T3 links are added because the symbols sit together.
    Numbered overload-to-motor wires and the three buses stay as Grok read them.
    """

    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    payload = _tile04()["grok_tile_04"]
    facts, violations, _units = _reply_facts(payload, catalog)
    assert violations == []
    tags = {device["tag"] for device in facts["devices"]}
    assert tags == {"10M-G", "11M-G", "12M-G", "10OL-G", "11OL-G", "12OL-G", "10G", "11G", "12G"}
    session, report = _build_facts(facts, catalog)
    sheet = next(item for item in session.project.sheets if item.components)
    placed = {comp.id for comp in sheet.components}
    assert placed == tags
    for comp in sheet.components:
        assert int(comp.rotation) == 270
        part = catalog.get(comp.library_id)
        assert float(comp.x) + float(part.schematic_height) <= 279.4 - 6
    for prefix in ("10", "11", "12"):
        rung = [comp for comp in sheet.components if comp.id.startswith(prefix)]
        assert len({round(float(comp.y), 2) for comp in rung}) == 1
    assert "contactor" in sheet.component("10M-G").library_id
    assert "overload" in sheet.component("10OL-G").library_id
    assert "motor" in sheet.component("10G").library_id
    pairs = _pin_pairs(session)
    numbered = {
        frozenset({f"{prefix}OL-G.{element}".upper(), f"{prefix}G.{lead}".upper()})
        for prefix in ("10", "11", "12")
        for element, lead in (("2", "T1"), ("4", "T2"), ("6", "T3"))
    }
    jumpers = {
        frozenset({f"{prefix}M-G.{pin}".upper(), f"{prefix}OL-G.{pin}".upper()})
        for prefix in ("10", "11", "12")
        for pin in ("T1", "T2", "T3")
    }
    buses = set()
    for line in ("L1", "L2", "L3"):
        owners = [f"{prefix}M-G.{line}".upper() for prefix in ("10", "11", "12")]
        buses.add(frozenset({owners[0], owners[1]}))
        buses.add(frozenset({owners[1], owners[2]}))
        buses.add(frozenset({owners[0], owners[2]}))
    wanted = numbered | jumpers | buses
    assert wanted <= pairs, {"missing": sorted(str(item) for item in wanted - pairs), "unresolved": report["unresolved_wires"]}
    assert len(wanted) == 27
    by_number = {}
    for item in session.project.sheets:
        for wire in item.wires:
            by_number.setdefault(str(wire.number), set()).add(frozenset({str(wire.frm).upper(), str(wire.to).upper()}))
    for prefix in ("10", "11", "12"):
        for element, lead, digit in (("2", "T1", "1"), ("4", "T2", "2"), ("6", "T3", "3")):
            ends = frozenset({f"{prefix}OL-G.{element}".upper(), f"{prefix}G.{lead}".upper()})
            assert ends in by_number[f"{prefix}T{digit}"]
    for label, line in (("9B16B", "L1"), ("9B16C", "L2"), ("9B16D", "L3")):
        assert any(f"10M-G.{line}".upper() in ends and f"12M-G.{line}".upper() in ends for ends in by_number[label])
    mount_rows = [
        row
        for row in payload["detections"]
        if not str(row.get("tag") or "").strip()
        and not str(row.get("wire_number") or "").strip()
        and str(row.get("from_tag") or "").endswith("M-G")
    ]
    assert mount_rows == []


def test_direct_mount_connects_adjacent_overload_and_skips_a_long_run():
    """26 mm is a direct mount. A long gap is not. IEC line pins are 1/3/5."""

    from panelcad.importer.build import build_session
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    devices = [
        {"tag": "10M-G", "library_id": "nema.contactors.ac3_32a", "symbol": "contactor", "x": 46, "y": 55},
        {"tag": "10OL-G", "library_id": "nema.protection.overload", "symbol": "overload", "x": 46, "y": 155},
        {"tag": "10G", "library_id": "nema.loads.motor_3ph", "symbol": "motor", "x": 46, "y": 255},
        {"tag": "11M-G", "library_id": "nema.contactors.ac3_32a", "symbol": "contactor", "x": 128, "y": 55},
        {"tag": "11OL-G", "library_id": "nema.protection.overload", "symbol": "overload", "x": 128, "y": 280},
        {"tag": "1K", "library_id": "iec.contactors.ac3_9a", "symbol": "contactor", "x": 40, "y": 40},
        {"tag": "1F", "library_id": "iec.protection.overload_9a", "symbol": "overload", "x": 40, "y": 116},
    ]
    session, _report = build_session(
        {"standard": "IEC", "sheets": [{"id": "power", "kind": "schematic", "width_mm": 279.4, "height_mm": 431.8, "devices": devices}]},
        catalog=catalog,
    )
    pairs = _pin_pairs(session)
    for pin in ("T1", "T2", "T3"):
        assert frozenset({f"10M-G.{pin}".upper(), f"10OL-G.{pin}".upper()}) in pairs
        assert frozenset({f"11M-G.{pin}".upper(), f"11OL-G.{pin}".upper()}) not in pairs
    for phase, line in (("T1", "1"), ("T2", "3"), ("T3", "5")):
        assert frozenset({f"1K.{phase}".upper(), f"1F.{line}".upper()}) in pairs
    motor_jumpers = [
        ends
        for ends in pairs
        if any(pin.startswith("10M-G.") for pin in ends) and any(pin.startswith("10G.") for pin in ends)
    ]
    assert motor_jumpers == []


def test_failed_template_does_not_place_prefix_guesses():
    """CR055 and M6 are not parts. A confirmed symbol still places. Layouts stay generic-free."""

    import numpy as np

    from panelcad.importer.build import build_session
    from panelcad.importer.pipeline import _fallback_slice, fallback_note
    from panelcad.importer.raster import _match_symbol
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    binary = np.zeros((80, 80), dtype=np.uint8)
    cr = _match_symbol(
        {"text": "CR055", "x": 20, "y": 20, "w": 8, "h": 3, "confidence": 0.8},
        binary,
        4.0,
        (80, 80, 3),
        catalog,
        "IEC",
        height_mm=200,
    )
    motor = _match_symbol(
        {"text": "M6", "x": 20, "y": 40, "w": 6, "h": 3, "confidence": 0.8},
        binary,
        4.0,
        (80, 80, 3),
        catalog,
        "NEMA/ANSI",
        height_mm=200,
    )
    assert cr["library_id"] == "" and cr["unconfirmed"] is True
    assert motor["library_id"] == "" and motor["unconfirmed"] is True
    assert "did not confirm a symbol" in cr["review_note"]
    confirmed = {
        "tag": "K1",
        "library_id": "iec.contactors.ac3_9a",
        "symbol": "contactor",
        "x": 40,
        "y": 40,
        "source": "symbol",
        "guess": False,
    }
    session, report = build_session(
        {
            "standard": "IEC",
            "sheets": [
                {
                    "id": "power",
                    "kind": "schematic",
                    "width_mm": 420,
                    "height_mm": 297,
                    "devices": [cr, motor, confirmed],
                }
            ],
        },
        catalog=catalog,
    )
    placed = {comp.id: comp.library_id for sheet in session.project.sheets for comp in sheet.components}
    assert set(placed) == {"K1", "CR055", "M6"}
    assert "contactor" in placed["K1"]
    assert placed["CR055"] == "reconstruct.needs_info"
    assert placed["M6"] == "reconstruct.needs_info"
    assert "width_mm" not in (session.project.sheet("power").component("M6").properties or {})
    skipped = {row["tag"]: row["reason"] for row in report["skipped"]}
    details = {row["tag"]: str(row.get("detail") or "") for row in report["skipped"]}
    assert skipped["CR055"] == "needs_info"
    assert skipped["M6"] == "needs_info"
    assert "NOT FOR BUILD" in details["M6"]
    assert "No millimetres" in details["M6"]
    assert cr["library_id"] == "" and "aux" not in str(cr.get("library_id"))
    layout, layout_report = build_session(
        {
            "standard": "IEC",
            "sheets": [
                {
                    "id": "layout",
                    "kind": "panel_layout",
                    "width_mm": 600,
                    "height_mm": 400,
                    "devices": [dict(motor), {"tag": "Q1", "library_id": "iec.breakers.mcb_1p_16a", "type": "breaker", "x": 80, "y": 40}],
                }
            ],
        },
        catalog=catalog,
    )
    layout_parts = {comp.id: comp for comp in layout.project.sheet("layout").components}
    assert set(layout_parts) == {"M6"}
    assert layout_parts["M6"].library_id == "reconstruct.needs_info"
    assert "width_mm" not in (layout_parts["M6"].properties or {})
    reasons = {row["tag"]: row["reason"] for row in layout_report["skipped"]}
    assert reasons["M6"] == "needs_info"
    assert reasons["Q1"] == "layout_not_ready"
    tile = {"x": 0, "y": 0, "w": 100, "h": 100, "id": "tile_04"}
    piece = _fallback_slice(
        {"devices": [cr, confirmed], "wires": [], "xrefs": [], "texts": []},
        tile,
        "HTTP 500",
    )
    assert {item["tag"] for item in piece["devices"]} == {"K1"}
    assert fallback_note(piece["devices"], "HTTP 500") == "the model read nothing. vision fallback: HTTP 500"
    assert fallback_note([{"tag": "12G", "source": "model"}], "HTTP 500") == "vision fallback: HTTP 500"


def _assert_sheet_9c_connections(session, report) -> None:
    """9 devices, 9 numbered wires, 3 buses, 27 connections. Not a live score."""

    tags = {"10M-G", "11M-G", "12M-G", "10OL-G", "11OL-G", "12OL-G", "10G", "11G", "12G"}
    sheet = next(item for item in session.project.sheets if item.components)
    real = {comp.id for comp in sheet.components if comp.library_id != "reconstruct.needs_info"}
    assert real == tags
    for comp in sheet.components:
        if comp.library_id == "reconstruct.needs_info":
            assert "width_mm" not in (comp.properties or {})
    pairs = _pin_pairs(session)
    numbered = {
        frozenset({f"{prefix}OL-G.{element}".upper(), f"{prefix}G.{lead}".upper()})
        for prefix in ("10", "11", "12")
        for element, lead in (("2", "T1"), ("4", "T2"), ("6", "T3"))
    }
    jumpers = {
        frozenset({f"{prefix}M-G.{pin}".upper(), f"{prefix}OL-G.{pin}".upper()})
        for prefix in ("10", "11", "12")
        for pin in ("T1", "T2", "T3")
    }
    buses = set()
    for line in ("L1", "L2", "L3"):
        owners = [f"{prefix}M-G.{line}".upper() for prefix in ("10", "11", "12")]
        buses.add(frozenset({owners[0], owners[1]}))
        buses.add(frozenset({owners[1], owners[2]}))
        buses.add(frozenset({owners[0], owners[2]}))
    wanted = numbered | jumpers | buses
    assert wanted <= pairs, {
        "missing": sorted(str(item) for item in wanted - pairs),
        "unresolved": report["unresolved_wires"],
    }
    assert len(wanted) == 27
    assert len(numbered) == 9
    assert len(buses) == 9
    by_number = {}
    for item in session.project.sheets:
        for wire in item.wires:
            by_number.setdefault(str(wire.number), set()).add(
                frozenset({str(wire.frm).upper(), str(wire.to).upper()})
            )
    for prefix in ("10", "11", "12"):
        for element, lead, digit in (("2", "T1", "1"), ("4", "T2", "2"), ("6", "T3", "3")):
            ends = frozenset({f"{prefix}OL-G.{element}".upper(), f"{prefix}G.{lead}".upper()})
            assert ends in by_number[f"{prefix}T{digit}"]
    for label, line in (("9B16B", "L1"), ("9B16C", "L2"), ("9B16D", "L3")):
        assert any(
            f"10M-G.{line}".upper() in ends and f"12M-G.{line}".upper() in ends
            for ends in by_number[label]
        )


def test_grok_null_and_empty_structured_output_keep_the_tile_text(tmp_path: Path):
    """The two headless envelopes that dropped sheet 9C on the live runs.

    Run 1 left ``structuredOutput`` null. The import recorded tile_04 as
    ``no structured answer``. Run 2 returned an empty structured object, so
    every tile counted as answered and the stage stayed empty. In both, the
    tile JSON was in ``text``. The Windows stdout files are not in this repo.
    This wraps the captured tile body in those envelopes. It is not a live score.
    """

    from panelcad.importer.grok_build import GrokBuildVision, parse_grok_result
    from panelcad.importer.vision import VisionFallback
    from panelcad.importer.vision_config import VisionSettings
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    body = _tile04()["grok_tile_04"]
    encoded = json.dumps(body)
    envelopes = {
        "null_structured": {
            "text": encoded,
            "structuredOutput": None,
            "stopReason": "end_turn",
            "sessionId": "live-run-1",
        },
        "empty_structured": {
            "text": encoded,
            "structuredOutput": {"detections": []},
            "stopReason": "end_turn",
            "sessionId": "live-run-2",
        },
        "result_line": {
            "type": "result",
            "subtype": "success",
            "result": encoded,
            "structured_output": None,
            "stop_reason": "end_turn",
        },
    }
    # The old parser stopped at the first object that merely had the key.
    streamed = "\n".join(
        [
            json.dumps({"structuredOutput": None, "text": ""}),
            json.dumps(envelopes["empty_structured"]),
        ]
    )
    image = _white(tmp_path / "tile.png", (32, 32))
    for name, payload in envelopes.items():
        found, event, parsed = parse_grok_result(json.dumps(payload))
        assert parsed is True, name
        assert found["detections"] == body["detections"], name
        assert event.get("text") == encoded or event.get("result") == encoded
        facts, violations, _units = _reply_facts(found, catalog)
        assert violations == []
        session, report = _build_facts(facts, catalog)
        _assert_sheet_9c_connections(session, report)

    found, _event, parsed = parse_grok_result(streamed)
    assert parsed is True
    assert [row["tag"] for row in found["detections"] if row.get("tag")][:1] == ["10M-G"]
    facts, _violations, _units = _reply_facts(found, catalog)
    session, report = _build_facts(facts, catalog)
    _assert_sheet_9c_connections(session, report)

    def runner(argv: list[str], cwd: str | None, timeout: float) -> tuple[int, str, str]:
        return 0, json.dumps(envelopes["null_structured"]), ""

    client = GrokBuildVision(VisionSettings(preset="grok-build"), runner=runner)
    try:
        data = client.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 220, "h": 280}})
    except VisionFallback as exc:
        raise AssertionError(f"null structuredOutput was dropped: {exc}") from exc
    tags = {row.get("tag") for row in data["detections"]}
    assert {"10M-G", "10OL-G", "10G", "11M-G", "12M-G"} <= tags

    def empty_runner(argv: list[str], cwd: str | None, timeout: float) -> tuple[int, str, str]:
        return 0, json.dumps(envelopes["empty_structured"]), ""

    empty = GrokBuildVision(VisionSettings(preset="grok-build"), runner=empty_runner)
    data = empty.extract({"image": str(image), "page_mm": {"x": 0, "y": 0, "w": 220, "h": 280}})
    assert {row.get("tag") for row in data["detections"]} >= {"10M-G", "12G"}


def test_unsupported_print_tags_stay_needs_info_off_the_build(tmp_path: Path):
    """A1, R1, 12T, 10C, and an unconfirmed M6 are not generic library parts."""

    from panelcad.importer.build import build_session
    from panelcad.importer.checks import findings_from_payload
    from panelcad.importer.symbols import print_tag_placeable
    from panelcad.importer.vision import detections_to_facts
    from panelcad.io.bom import grouped_rows
    from panelcad.library.catalog import default_catalog
    from panelcad.quote.build import build_quote
    from panelcad.render.print_package import extract_pdf_text

    catalog = default_catalog()
    tile = {"x": 0, "y": 0, "w": 200, "h": 160, "id": "tile_00"}
    rows = [
        {"tag": "A1", "wire_number": "", "symbol_class": "contactor", "bbox": [12, 20, 16, 10], "confidence": 0.9},
        {"tag": "R1", "wire_number": "", "symbol_class": "contactor", "bbox": [40, 20, 16, 10], "confidence": 0.9},
        {"tag": "12T", "wire_number": "", "symbol_class": "contactor", "bbox": [70, 20, 16, 10], "confidence": 0.88},
        {"tag": "10C", "wire_number": "", "symbol_class": "contactor", "bbox": [100, 20, 16, 10], "confidence": 0.88},
        {"tag": "M6", "wire_number": "", "symbol_class": "contactor", "bbox": [130, 20, 16, 10], "confidence": 0.4},
    ]
    facts = detections_to_facts(rows, tile, catalog, standard="NEMA/ANSI")
    for device in facts["devices"]:
        if device["tag"] == "M6":
            device["unconfirmed"] = True
            device["library_id"] = ""
            device["source"] = "ocr"
        assert print_tag_placeable(device) is False
        if device["tag"] == "M6":
            assert device["library_id"] == ""
    session, report = build_session(
        {
            "standard": "NEMA/ANSI",
            "sheets": [
                {
                    "id": "power",
                    "kind": "schematic",
                    "width_mm": 279.4,
                    "height_mm": 431.8,
                    "devices": facts["devices"],
                    "wires": [],
                }
            ],
        },
        catalog=catalog,
    )
    placed_parts = [comp for sheet in session.project.sheets for comp in sheet.components]
    assert {comp.id for comp in placed_parts} == {"A1", "R1", "12T", "10C", "M6"}
    assert all(comp.library_id == "reconstruct.needs_info" for comp in placed_parts)
    assert all("width_mm" not in (comp.properties or {}) for comp in placed_parts)
    skipped = {row["tag"]: row for row in report["skipped"]}
    for tag in ("A1", "R1", "12T", "10C", "M6"):
        assert skipped[tag]["reason"] == "needs_info"
        assert "NOT FOR BUILD" in skipped[tag]["detail"]
        assert "No millimetres" in skipped[tag]["detail"]
    assert session.project.properties["print_import"]["not_for_build"] == "yes"
    assert report["wires"] == []
    refs = " ".join(str(row.get("ref") or "") for row in grouped_rows(session.project, catalog))
    for tag in ("A1", "R1", "12T", "10C", "M6"):
        assert tag not in refs
    quote = build_quote(session.project, catalog, session.parts)
    quoted = " ".join(str(row.get("refs") or "") for row in quote["parts"])
    for tag in ("A1", "R1", "12T", "10C", "M6"):
        assert tag not in quoted
    cutouts = json.dumps(session.cutouts_generate())
    for tag in ("A1", "R1", "12T", "10C", "M6"):
        assert tag not in cutouts
    findings = findings_from_payload(session.project.properties["print_import"])
    text = " ".join(item.message for item in findings)
    assert "NOT FOR BUILD" in text
    assert "A1" in text and "does not match a tag this sheet supports" in text

    kept = detections_to_facts(
        [
            {"tag": "10M-G", "wire_number": "", "symbol_class": "contactor", "bbox": [20, 30, 18, 12], "confidence": 0.93},
            {"tag": "K1", "wire_number": "", "symbol_class": "contactor", "bbox": [80, 30, 18, 12], "confidence": 0.93},
            rows[0],
        ],
        tile,
        catalog,
        standard="NEMA/ANSI",
    )
    mixed, mixed_report = build_session(
        {
            "standard": "NEMA/ANSI",
            "sheets": [
                {
                    "id": "power",
                    "kind": "schematic",
                    "width_mm": 279.4,
                    "height_mm": 431.8,
                    "devices": kept["devices"],
                    "wires": [],
                }
            ],
        },
        catalog=catalog,
    )
    mixed_ids = {comp.id: comp.library_id for sheet in mixed.project.sheets for comp in sheet.components}
    assert {"10M-G", "K1"} <= set(mixed_ids)
    assert mixed_ids["A1"] == "reconstruct.needs_info"
    assert "contactor" in mixed.project.sheet("power").component("10M-G").library_id
    assert mixed.project.properties["print_import"]["not_for_build"] == "yes"
    assert any(row["tag"] == "A1" and row["reason"] == "needs_info" for row in mixed_report["skipped"])
    dest = tmp_path / "needs-info.pdf"
    printed = mixed.print_package(dest, sections="drawings")
    assert printed.get("not_for_build") is True
    assert "NOT FOR BUILD" in extract_pdf_text(dest)


def test_sheet_9c_tile04_buses_in_tag_still_tie():
    """9B16B stored in tag is the wrong field, and it still ties the contactor line pins."""

    from panelcad.importer.vision import apply_field_placement
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    payload = _tile04()
    devices = [row for row in payload["grok_tile_04"]["detections"] if row.get("tag")]
    moved = apply_field_placement(payload["buses_in_tag"])
    assert all(row.get("wrong_field") == "tag" and row.get("bus") for row in moved)
    facts, _violations, _units = _reply_facts({"detections": devices + payload["buses_in_tag"]}, catalog)
    session, report = _build_facts(facts, catalog)
    by_number = {}
    for item in session.project.sheets:
        for wire in item.wires:
            by_number.setdefault(str(wire.number), set()).add(frozenset({str(wire.frm).upper(), str(wire.to).upper()}))
    for label, line in (("9B16B", "L1"), ("9B16C", "L2"), ("9B16D", "L3")):
        owners = [f"{prefix}M-G.{line}".upper() for prefix in ("10", "11", "12")]
        wanted = {
            frozenset({owners[0], owners[1]}),
            frozenset({owners[1], owners[2]}),
            frozenset({owners[0], owners[2]}),
        }
        assert wanted <= by_number.get(label, set()), {"label": label, "got": by_number.get(label), "unresolved": report["unresolved_wires"]}


def test_sheet_9c_tile04_local_models_do_not_invent_devices():
    """Gemma echoes the crop. Qwen 27B returns classes with no tag. Qwen3-VL keeps 12G and its leads."""

    from panelcad.importer.vision import partition_detections, prepare_detection_rows
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    payload = _tile04()
    gemma = payload["gemma_tile_04"]
    valid, violations = partition_detections(gemma)
    assert valid == [] and violations == []
    prepared, units = prepare_detection_rows(gemma["detections"], {"page_mm": gemma["page_mm"]})
    assert units == ["unlocalised"]
    assert prepared[0]["unlocalised"] is True
    facts, _violations, _units = _reply_facts(gemma, catalog, page_mm=gemma["page_mm"])
    assert facts["devices"] == [] and facts["wires"] == []

    qwen = payload["qwen38_tile_04"]
    valid, violations = partition_detections(qwen)
    assert valid == []
    assert len(violations) == len(qwen["detections"]) == 21
    assert all(row["loss"] == "parse_fail" and row["reason"] == "schema_incomplete" for row in violations)
    facts, _violations, _units = _reply_facts(qwen, catalog)
    assert facts["devices"] == []

    vl = payload["qwen3vl_tile_04"]
    facts, violations, _units = _reply_facts(vl, catalog)
    assert violations == []
    placed_tags = {device["tag"] for device in facts["devices"] if not device.get("tag_unparsed")}
    assert placed_tags == {"12G"}
    assert "09" not in placed_tags and "10" not in placed_tags and "2 H.P." not in placed_tags
    numbers = {wire["number"] for wire in facts["wires"]}
    assert {"12T2", "12T3"} <= numbers
    session, _report = _build_facts(facts, catalog)
    built = {comp.id for sheet in session.project.sheets for comp in sheet.components}
    assert built == {"12G"}


def _bbox_at(x: float, y: float, w: float = 8.0, h: float = 4.0) -> list[float]:
    return [round(x - w / 2.0, 2), round(y - h / 2.0, 2), w, h]


def _starter_world_pin(catalog, tag: str, pin_id: str) -> tuple[float, float]:
    _name, _symbol, origin_x, origin_y, library_id = next(item for item in _starter_devices() if item[0] == tag)
    part = catalog.get(library_id)
    pin = next(item for item in part.pins if str(item.id) == pin_id)
    return origin_x + float(pin.x), origin_y + float(pin.y)


def _starter_device_rows() -> list[dict]:
    rows = []
    for tag, symbol, origin_x, origin_y, _library in _starter_devices():
        rows.append(
            {
                "tag": tag,
                "wire_number": "",
                "symbol_class": symbol,
                "bbox": [origin_x, origin_y, 28.0, 36.0],
                "confidence": 0.93,
            }
        )
    return rows


def test_dotted_and_slashed_title_dates_use_hyphens():
    """Tesseract reads the hyphens in 8-3-92 as dots. The title block still stores 8-3-92."""

    from panelcad.importer.read import read_title_block

    height = 431.8
    y = 400.0

    def block(*words: str) -> dict:
        tokens = [{"text": word, "x": 24.0 + index * 14.0, "y": y, "h": 3.0} for index, word in enumerate(words)]
        return read_title_block(tokens, height)

    assert block("DATE", "8.3.92")["date"] == "8-3-92"
    assert block("DATE", "8/3/92")["date"] == "8-3-92"
    assert block("DATE", "8", ".", "3", ".", "92")["date"] == "8-3-92"
    assert block("DATE", "8", "/", "3", "/", "92")["date"] == "8-3-92"


def test_supported_model_confidence_is_not_prefix_floored():
    """10M-G and 12G at 0.93 stay 0.93. A shared family is not a low-confidence read."""

    from panelcad.importer.vision import detections_to_facts
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    tile = {"x": 0, "y": 0, "w": 200, "h": 160, "id": "tile_00"}
    rows = [
        {"tag": "10M-G", "wire_number": "", "symbol_class": "contactor", "bbox": [20, 30, 18, 12], "confidence": 0.93},
        {"tag": "12G", "wire_number": "", "symbol_class": "motor", "bbox": [80, 30, 18, 12], "confidence": 0.93},
        {"tag": "11M-G", "wire_number": "", "symbol_class": "", "bbox": [20, 80, 18, 12], "confidence": 0.93},
        {"tag": "11G", "wire_number": "", "symbol_class": "", "bbox": [80, 80, 18, 12], "confidence": 0.93},
    ]
    facts = detections_to_facts(rows, tile, catalog, standard="NEMA/ANSI")
    by_tag = {device["tag"]: device for device in facts["devices"]}
    for tag in ("10M-G", "12G", "11M-G", "11G"):
        assert by_tag[tag]["confidence"] == 0.93
        assert by_tag[tag]["confidence"] >= 0.6
        assert by_tag[tag]["library_id"]


def test_sheet_9c_sparse_replies_build_connections_without_inventing_tags():
    """Live tile rows: pins without tags, a prose pin, and bbox-only wire numbers and buses.

    These are the reply shapes from the sheet 9C runs. They are not a live score.
    """

    from panelcad.importer.pipeline import _detection_drops
    from panelcad.importer.vision import partition_detections
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    page_mm = {"x": 0, "y": 0, "w": 279.4, "h": 431.8}
    phases = (("2", "T1", "1", "B", "L1"), ("4", "T2", "2", "C", "L2"), ("6", "T3", "3", "D", "L3"))
    pins_only = []
    for prefix in ("10", "11", "12"):
        for element, lead, _digit, _bus, _line in phases:
            ax, ay = _starter_world_pin(catalog, f"{prefix}OL-G", element)
            bx, by = _starter_world_pin(catalog, f"{prefix}G", lead)
            row = {
                "bbox": _bbox_at((ax + bx) / 2.0, (ay + by) / 2.0),
                "confidence": 0.9,
                "from_pin": element,
                "to_pin": lead,
            }
            pins_only.append(row)
    pins_only[0]["from_pin"] = 2
    wide = {"detections": list(pins_only) * 4}
    valid, violations = partition_detections(wide)
    assert len(valid) == 36 and violations == []
    assert all(row.get("from_pin") and row.get("to_pin") and not row.get("tag") for row in valid)

    prose = {
        "bbox": [12, 40, 20, 6],
        "confidence": 0.4,
        "from_tag": "10OL-G",
        "from_pin": "2",
        "to_pin": "continues to the motor",
    }
    valid, violations = partition_detections({"detections": [pins_only[0], prose, pins_only[1]]})
    assert len(valid) == 2
    assert len(violations) == 1
    assert violations[0]["loss"] == "parse_fail"
    assert violations[0]["rule"] == "schema_incomplete"
    assert violations[0]["reason"] == "schema_incomplete"
    drops = _detection_drops([pins_only[0], prose, pins_only[1]], violations, valid)
    assert drops[0]["reason"] == "schema_incomplete"
    assert "malformed JSON" not in str(drops)

    device_rows = _starter_device_rows()
    pin_facts, pin_violations, _units = _reply_facts(
        {"detections": device_rows + pins_only + [prose]},
        catalog,
        page_mm=page_mm,
    )
    assert len(pin_violations) == 1 and pin_violations[0]["reason"] == "schema_incomplete"
    assert {device["tag"] for device in pin_facts["devices"]} == {row["tag"] for row in device_rows}
    pin_session, pin_report = _build_facts(pin_facts, catalog)
    pin_sheet = next(item for item in pin_session.project.sheets if item.components)
    assert {comp.id for comp in pin_sheet.components} == {row["tag"] for row in device_rows}
    pin_pairs = _pin_pairs(pin_session)
    numbered = {
        frozenset({f"{prefix}OL-G.{element}".upper(), f"{prefix}G.{lead}".upper()})
        for prefix in ("10", "11", "12")
        for element, lead, _digit, _bus, _line in phases
    }
    assert numbered <= pin_pairs, {
        "missing": sorted(str(item) for item in numbered - pin_pairs),
        "unresolved": pin_report["unresolved_wires"],
    }

    lead_x, lead_y = _starter_world_pin(catalog, "10G", "T1")
    element_x, element_y = _starter_world_pin(catalog, "10OL-G", "2")
    motor_end = {
        "tag": "",
        "wire_number": "10T1",
        "symbol_class": "",
        "bbox": _bbox_at((element_x + lead_x) / 2.0, (element_y + lead_y) / 2.0),
        "confidence": 0.9,
        "from_tag": "10G",
        "from_pin": "T1",
        "to_pin": "2",
    }
    motor_facts, motor_violations, _units = _reply_facts(
        {"detections": [row for row in device_rows if row["tag"].startswith("10")] + [motor_end]},
        catalog,
        page_mm=page_mm,
    )
    assert motor_violations == []
    motor_session, _motor_report = _build_facts(motor_facts, catalog)
    assert frozenset({"10OL-G.2", "10G.T1"}) in _pin_pairs(motor_session)
    motor_numbers = {str(wire.number) for sheet in motor_session.project.sheets for wire in sheet.wires}
    assert "10T1" in motor_numbers

    labels = []
    for prefix in ("10", "11", "12"):
        for element, lead, digit, _bus, _line in phases:
            ax, ay = _starter_world_pin(catalog, f"{prefix}OL-G", element)
            bx, by = _starter_world_pin(catalog, f"{prefix}G", lead)
            labels.append(
                {
                    "tag": "",
                    "wire_number": f"{prefix}T{digit}",
                    "symbol_class": "",
                    "bbox": _bbox_at((ax + bx) / 2.0, (ay + by) / 2.0),
                    "confidence": 0.93,
                }
            )
    for element, _lead, _digit, bus, line in phases:
        points = [_starter_world_pin(catalog, f"{prefix}M-G", line) for prefix in ("10", "11", "12")]
        labels.append(
            {
                "tag": "",
                "wire_number": "",
                "bus": f"9B16{bus}",
                "symbol_class": "",
                "bbox": _bbox_at(
                    sum(point[0] for point in points) / 3.0,
                    sum(point[1] for point in points) / 3.0,
                    12.0,
                    3.0,
                ),
                "confidence": 0.86,
            }
        )
    far = {"tag": "", "wire_number": "10T1", "symbol_class": "", "bbox": [2, 70, 8, 4], "confidence": 0.93}
    junk = {"tag": "", "wire_number": "[9B34]", "symbol_class": "", "bbox": [2, 8, 10, 4], "confidence": 0.4}
    stray = {"tag": "A1", "wire_number": "", "symbol_class": "contactor", "bbox": [2, 24, 12, 8], "confidence": 0.9}
    bare = {"tag": "", "wire_number": "101", "symbol_class": "", "bbox": _bbox_at(70, 230), "confidence": 0.8}
    facts, violations, _units = _reply_facts(
        {"detections": device_rows + labels + [far, junk, stray, bare]},
        catalog,
        page_mm=page_mm,
    )
    assert violations == []
    session, report = _build_facts(facts, catalog)
    _assert_sheet_9c_connections(session, report)
    placed = {comp.id for sheet in session.project.sheets for comp in sheet.components}
    assert {row["tag"] for row in device_rows} <= placed
    assert session.project.sheet("power").component("A1").library_id == "reconstruct.needs_info"
    numbers = {str(wire.number) for sheet in session.project.sheets for wire in sheet.wires}
    assert "[9B34]" not in numbers and "9B34" not in numbers and "A1" not in numbers
    unresolved = {str(row.get("number") or "") for row in report["unresolved_wires"]}
    assert "101" in unresolved and "[9B34]" in unresolved
    skipped = {row["tag"]: row["reason"] for row in report["skipped"]}
    assert skipped["A1"] == "needs_info"
    parked = [
        item
        for item in session.project.properties["print_import"]["items"]
        if item.get("text") == "10T1" and item.get("reason") == "unmatched"
    ]
    assert parked and parked[0]["distance_mm"] > 22
    assert parked[0]["confidence"] == 0.93
    low = {item.get("text") for item in session.project.properties["print_import"]["items"] if item.get("reason") == "low_confidence"}
    assert "10M-G" not in low and "12G" not in low


def test_sheet_9c_grid_caption_and_page_edge():
    """Row numbers, 0S, and 9834 stay off the sheet. A horizontal rung stays inside the page."""

    from panelcad.importer.raster import _mark_caption_tags, orient_ladder_devices
    from panelcad.importer.read import label_tokens
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    width, height = 279.4, 431.8
    tokens = [{"text": f"{index:02d}", "x": 39.0, "y": 40.0 + index * 8, "h": 2.4} for index in range(1, 12)]
    tokens.append({"text": "0S", "x": 39.0, "y": 30.0, "h": 4.0})
    tokens.append({"text": "10OL-G", "x": 80.0, "y": 80.0, "h": 4.0})
    tokens.append({"text": "9834", "x": 39.0, "y": 200.0, "h": 2.2})
    tokens.append({"text": "9834", "x": 90.0, "y": 210.0, "h": 2.2})
    tokens.append({"text": "[9B34]", "x": 96.0, "y": 210.0, "h": 2.2})
    tokens.append({"text": "2156", "x": 140.0, "y": 180.0, "h": 2.0})
    labelled = label_tokens(tokens, width, height)
    wire_text = {tok["text"] for tok in labelled["wires"]}
    tag_text = {tok["text"] for tok in labelled["tags"]}
    assert "01" not in wire_text and "09" not in wire_text
    assert "0S" not in tag_text and "9834" not in wire_text
    assert "10OL-G" in tag_text
    assert "2156" in wire_text

    caption = [{"tag": "CR055", "guess": True, "x": 40, "y": 40, "library_id": "iec.relays.k_4co_24vdc"}]
    _mark_caption_tags(caption, [{"text": "CONVEYOR", "x": 70, "y": 40}])
    assert caption[0]["title_ink"] is True
    power = [{"tag": "10M-G", "guess": True, "x": 40, "y": 40, "library_id": "nema.contactors.ac3_32a"}]
    _mark_caption_tags(power, [{"text": "CONVEYOR", "x": 70, "y": 40}])
    assert "title_ink" not in power[0]

    rung = [
        {"tag": "10M-G", "library_id": "nema.contactors.ac3_32a", "symbol": "contactor", "x": 200, "y": 80},
        {"tag": "10OL-G", "library_id": "nema.protection.overload", "symbol": "overload", "x": 230, "y": 80},
        {"tag": "10G", "library_id": "nema.loads.motor_3ph", "symbol": "motor", "x": 250, "y": 80},
    ]
    orient_ladder_devices(rung, catalog, spread=True, page_width=width)
    assert [device["rotation"] for device in rung] == [270, 270, 270]
    assert [device["y"] for device in rung] == [80, 80, 80]
    for device in rung:
        part = catalog.get(device["library_id"])
        assert float(device["x"]) + float(part.schematic_height) <= width - 6
    again = [dict(device) for device in rung]
    orient_ladder_devices(again, catalog, spread=True, page_width=width)
    assert [device["x"] for device in again] == [device["x"] for device in rung]
    vertical = [
        {"tag": "10M-G", "library_id": "nema.contactors.ac3_32a", "symbol": "contactor", "x": 46, "y": 55},
        {"tag": "10OL-G", "library_id": "nema.protection.overload", "symbol": "overload", "x": 46, "y": 155},
        {"tag": "10G", "library_id": "nema.loads.motor_3ph", "symbol": "motor", "x": 46, "y": 255},
    ]
    orient_ladder_devices(vertical, catalog, spread=True, page_width=width)
    assert all(int(device.get("rotation") or 0) == 0 for device in vertical)
    assert [device["y"] for device in vertical] == [55, 155, 255]


def test_imported_layout_part_without_catalog_size_stays_visible(tmp_path: Path):
    """A generic footprint is a review row. It is not dropped, and a photo import does not abort."""

    from panelcad.api.session import Session
    from panelcad.importer.build import _hold_unready_layout, build_session
    from panelcad.importer.diagnostics import ImportDiagnostics
    from panelcad.library.catalog import default_catalog
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    catalog = default_catalog()
    device = {
        "tag": "Q1",
        "library_id": "iec.breakers.mcb_1p_16a",
        "type": "breaker",
        "x": 40,
        "y": 40,
    }
    schematic, schematic_report = build_session(
        {
            "standard": "IEC",
            "sheets": [{"id": "power", "kind": "schematic", "width_mm": 420, "height_mm": 297, "devices": [device]}],
        },
        catalog=catalog,
    )
    assert schematic.project.sheet("power").component("Q1").library_id == "iec.breakers.mcb_1p_16a"
    assert schematic_report["skipped"] == []

    layout, layout_report = build_session(
        {
            "standard": "IEC",
            "sheets": [{"id": "layout", "kind": "panel_layout", "width_mm": 600, "height_mm": 400, "devices": [dict(device)]}],
        },
        catalog=catalog,
    )
    assert layout.project.sheet("layout").components == []
    assert layout_report["skipped"][0]["reason"] == "layout_not_ready"
    assert layout_report["skipped"][0]["tag"] == "Q1"

    photo, photo_report = build_session(
        {
            "standard": "IEC",
            "sheets": [{"id": "power", "kind": "schematic", "width_mm": 420, "height_mm": 297, "devices": [dict(device)]}],
        },
        catalog=catalog,
        photo_targets=[{"tag": "Q1", "nx": 0.3, "ny": 0.4, "confidence": 0.8}],
    )
    photo_layout = next(sheet for sheet in photo.project.sheets if sheet.kind == "panel_layout")
    assert all(comp.library_id != "iec.breakers.mcb_1p_16a" for comp in photo_layout.components)
    assert any(row["reason"] == "layout_not_ready" and row["tag"] == "Q1" for row in photo_report["skipped"])

    session = Session.create("Reconstruct")
    session.catalog = catalog
    session.project.reconstruct = type("Store", (), {"photos": [object()], "items": []})()
    session.add_sheet("layout", title="Layout", kind="panel_layout")
    report = {"placed": [], "wires": [], "skipped": [], "unresolved_wires": [], "photos": []}
    _hold_unready_layout(session, "layout", "Q1", "iec.breakers.mcb_1p_16a", 50, 60, report, "catalog size")
    mark = session.project.sheet("layout").component("Q1")
    assert mark.library_id == NEEDS_INFO_LIBRARY_ID
    assert mark.properties.get("description") == "INFO NEEDED"
    assert report["skipped"][0]["reason"] == "needs_info"

    truth = tmp_path / "truth.yaml"
    truth.write_text("devices:\n  - 11M-G\n", encoding="utf-8")
    diag = ImportDiagnostics(tmp_path, "tile-run")
    diag.record_exchange({"request": {"prompt": "read"}}, page={"id": "page_001"}, tile={"id": "tile_04", "x": 0, "y": 0, "w": 10, "h": 10})
    diag.write()
    reloaded = ImportDiagnostics(tmp_path, "tile-run")
    reloaded.load()
    assert reloaded._model_used is True
    scored = reloaded.score_truth(truth)
    missed = [item for item in scored["items"] if item.get("text") == "11M-G"]
    assert missed and missed[0]["loss"] == "model_miss"
    assert missed[0]["loss"] != "build_fail"


# The e and e2 tile replies stuffed the tag and part type into to_pin. The
# attachment with the raw JSON was not in this workspace; this is that shape.
_STUFFED_10M = "2T1T2T3L1L2L3from_tagto_tagwire_numberbus symbol_class contactor tag 10M-G"


def test_stuffed_pin_recovers_only_a_supported_device():
    """e/e2 left tag and part type out of the device fields and wrote them into to_pin.

    Recover only when that text has a real tag and the part type that tag supports.
    This replay is not a live score.
    """

    from panelcad.api.session import Session
    from panelcad.importer.build import _stamp_properties
    from panelcad.importer.grok_build import parse_grok_result
    from panelcad.importer.tile import merge_extractions
    from panelcad.importer.vision import partition_detections
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    stuffed = {
        "detections": [
            {"bbox": [20, 30, 18, 12], "confidence": 0.93, "to_pin": _STUFFED_10M},
            {
                "bbox": [20, 80, 18, 12],
                "confidence": 0.9,
                "to_pin": "symbol_class overload tag 10OL-G",
            },
            {
                "bbox": [80, 30, 18, 12],
                "confidence": 0.91,
                "to_pin": "symbol_class motor tag 12G",
            },
            {
                "bbox": [2, 24, 12, 8],
                "confidence": 0.4,
                "to_pin": "symbol_class contactor tag A1",
            },
            {
                "bbox": [4, 40, 12, 8],
                "confidence": 0.4,
                "to_pin": "symbol_class widget tag 10M-G",
            },
            {"bbox": [12, 40, 20, 6], "confidence": 0.4, "from_pin": "2", "to_pin": "continues to the motor"},
            {"bbox": [4, 8, 10, 4], "confidence": 0.93, "wire_number": "10T1"},
            {"bbox": [4, 14, 12, 4], "confidence": 0.93, "bus": "9B16B"},
        ]
    }
    valid, violations = partition_detections(stuffed)
    recovered = [row for row in valid if row.get("recovered")]
    assert {row["tag"] for row in recovered} == {"10M-G", "10OL-G", "12G"}
    assert {row["symbol_class"] for row in recovered} == {"contactor", "overload", "motor"}
    assert all(row.get("recovered") for row in recovered)
    assert all("to_pin" not in row for row in recovered)
    assert not any(row.get("tag") == "A1" for row in valid)
    assert len(violations) == 3
    assert all(item["reason"] == "schema_incomplete" for item in violations)

    listed = {
        "devices": [
            {"tag": "10M-G", "symbol_class": "contactor", "bbox": [20, 30, 18, 12], "confidence": 0.93}
        ],
        "conductors": [{"bbox": [30, 40, 8, 4], "from_pin": "T1", "to_pin": "2", "confidence": 0.9}],
        "wire_numbers": [{"bbox": [30, 48, 8, 3], "wire_number": "10T1", "confidence": 0.93}],
        "buses": [{"bbox": [10, 10, 12, 3], "bus": "9B16B", "confidence": 0.93}],
    }
    listed_rows, listed_bad = partition_detections(listed)
    assert listed_bad == []
    assert any(row.get("tag") == "10M-G" and not row.get("recovered") for row in listed_rows)
    assert any(row.get("from_pin") == "T1" and row.get("to_pin") == "2" for row in listed_rows)
    assert any(row.get("wire_number") == "10T1" for row in listed_rows)
    assert any(row.get("bus") == "9B16B" for row in listed_rows)
    parsed, _payload, ok = parse_grok_result(json.dumps({"structuredOutput": listed}))
    assert ok is True
    assert parsed["detections"][0]["tag"] == "10M-G"

    facts, fact_violations, _units = _reply_facts(stuffed, catalog, page_mm={"x": 0, "y": 0, "w": 200, "h": 160})
    assert len(fact_violations) == 3
    by_tag = {device["tag"]: device for device in facts["devices"]}
    assert set(by_tag) == {"10M-G", "10OL-G", "12G"}
    assert by_tag["10M-G"]["recovered"] is True
    assert by_tag["10M-G"]["confidence"] == 0.93
    session, report = _build_facts(facts, catalog)
    placed = {comp.id for sheet in session.project.sheets for comp in sheet.components}
    assert {"10M-G", "10OL-G", "12G"} <= placed
    assert "A1" not in placed
    parked = [
        item
        for item in session.project.properties["print_import"]["items"]
        if item.get("text") in {"10T1", "9B16B"}
    ]
    assert parked
    assert all(item["confidence"] == 0.93 for item in parked)

    stamped = Session.create("Parked confidence")
    _stamp_properties(
        stamped,
        {"sheets": []},
        {
            "skipped": [],
            "unresolved_wires": [
                {"number": "9B16B", "reason": "bus", "sheet": "power", "confidence": 0.93},
                {"number": "10T1", "reason": "unmatched", "sheet": "power", "confidence": 0.93},
            ],
        },
    )
    stored = {item["text"]: item for item in stamped.project.properties["print_import"]["items"]}
    assert stored["9B16B"]["confidence"] == 0.93
    assert stored["10T1"]["confidence"] == 0.93
    assert "distance_mm" not in stored["9B16B"]

    direct = {
        "tag": "10M-G",
        "symbol": "contactor",
        "type": "contactor",
        "x": 20,
        "y": 30,
        "confidence": 0.8,
        "library_id": by_tag["10M-G"]["library_id"],
    }
    recovered_device = {
        "tag": "10M-G",
        "symbol": "contactor",
        "type": "contactor",
        "x": 22,
        "y": 31,
        "confidence": 0.99,
        "recovered": True,
        "library_id": by_tag["10M-G"]["library_id"],
    }
    kept = merge_extractions(
        [
            {"devices": [direct], "wires": []},
            {"devices": [recovered_device], "wires": []},
            {"devices": [], "wires": []},
        ]
    )
    assert len(kept["devices"]) == 1
    assert kept["devices"][0]["confidence"] == 0.8
    assert not kept["devices"][0].get("recovered")
    replaced = merge_extractions(
        [{"devices": [recovered_device], "wires": []}, {"devices": [direct], "wires": []}]
    )
    assert replaced["devices"][0]["confidence"] == 0.8
    assert not replaced["devices"][0].get("recovered")


def test_inked_tile_is_reread_once_when_it_has_no_devices(tmp_path: Path):
    """A content-heavy crop with no usable row is asked once more. A bus, a wire number, or low ink is not."""

    from PIL import Image, ImageDraw

    from panelcad.importer.vision import OpenAICompatibleVision
    from panelcad.importer.vision_config import VisionSettings

    image = tmp_path / "ink.png"
    canvas = Image.new("RGB", (80, 60), (255, 255, 255))
    ImageDraw.Draw(canvas).rectangle((8, 8, 70, 50), fill=(0, 0, 0))
    canvas.save(image)
    blank = _white(tmp_path / "blank.png", (80, 60))
    work = {"image": str(image), "page_id": "page_001", "page_mm": {"x": 0, "y": 0, "w": 80, "h": 60}}
    empty = {"devices": [], "conductors": [], "wire_numbers": [], "buses": []}
    device = {
        "devices": [
            {"tag": "10M-G", "symbol_class": "contactor", "bbox": [4, 6, 18, 12], "confidence": 0.93}
        ],
        "conductors": [],
        "wire_numbers": [],
        "buses": [],
    }
    calls = {"n": 0}

    def transport(method, url, payload, headers, timeout):
        calls["n"] += 1
        body = empty if calls["n"] == 1 else device
        return {"message": {"role": "assistant", "content": json.dumps(body)}, "done_reason": "stop"}

    from panelcad.importer.vision import set_vision_trace, reset_vision_trace

    traces: list[dict] = []
    token = set_vision_trace(traces.append)
    try:
        data = OpenAICompatibleVision(VisionSettings(preset="ollama"), transport=transport).extract(work)
    finally:
        reset_vision_trace(token)
    assert calls["n"] == 2
    assert data["detections"][0]["tag"] == "10M-G"
    assert not data["detections"][0].get("recovered")
    assert traces[0]["retry"]["status"] == "retried"
    assert traces[0]["retry"]["count"] == 1
    assert traces[0]["retry"]["first_rows"] == 0
    assert traces[0]["retry"]["second_rows"] >= 1

    calls["n"] = 0

    def still_empty(method, url, payload, headers, timeout):
        calls["n"] += 1
        return {"message": {"role": "assistant", "content": json.dumps(empty)}, "done_reason": "stop"}

    OpenAICompatibleVision(VisionSettings(preset="ollama"), transport=still_empty).extract(work)
    assert calls["n"] == 2

    calls["n"] = 0

    def already_named(method, url, payload, headers, timeout):
        calls["n"] += 1
        return {"message": {"role": "assistant", "content": json.dumps(device)}, "done_reason": "stop"}

    named = OpenAICompatibleVision(VisionSettings(preset="ollama"), transport=already_named).extract(work)
    assert calls["n"] == 1
    assert named["detections"][0]["tag"] == "10M-G"

    prose = {
        "detections": [
            {"bbox": [4, 4, 10, 4], "confidence": 0.4, "to_pin": "continues across the rung"},
            {"bbox": [4, 12, 10, 4], "confidence": 0.4, "to_pin": "down to the next device"},
            {"bbox": [4, 20, 10, 4], "confidence": 0.4, "to_pin": "not a pin name here"},
        ]
    }
    calls["n"] = 0
    sequence = {"n": 0}

    def half_incomplete(method, url, payload, headers, timeout):
        sequence["n"] += 1
        body = prose if sequence["n"] == 1 else device
        return {"message": {"role": "assistant", "content": json.dumps(body)}, "done_reason": "stop"}

    white_work = {"image": str(blank), "page_id": "page_001", "page_mm": {"x": 0, "y": 0, "w": 80, "h": 60}}
    skipped = OpenAICompatibleVision(VisionSettings(preset="ollama"), transport=half_incomplete).extract(white_work)
    assert sequence["n"] == 1
    assert skipped["detections"] == []

    sequence["n"] = 0

    def buses_only(method, url, payload, headers, timeout):
        sequence["n"] += 1
        return {
            "message": {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "devices": [],
                        "conductors": [],
                        "wire_numbers": [],
                        "buses": [{"bbox": [4, 4, 12, 3], "bus": "9B16B", "confidence": 0.93}],
                    }
                ),
            },
            "done_reason": "stop",
        }

    buses = OpenAICompatibleVision(VisionSettings(preset="ollama"), transport=buses_only).extract(work)
    assert sequence["n"] == 1
    assert buses["detections"][0]["bus"] == "9B16B"

    sequence["n"] = 0
    inked = OpenAICompatibleVision(VisionSettings(preset="ollama"), transport=half_incomplete).extract(work)
    assert sequence["n"] == 2
    assert inked["detections"][0]["tag"] == "10M-G"


def test_pinless_wire_number_keeps_the_conductor_pins():
    """tile_04's conductor is 10T1 at 0.88. The wire_numbers label is the same number at 0.90.

    The label must not blank 10OL-G.2 to 10G.T1. Both orders, and the same pair
    split across the tile_01 / tile_04 overlap, still build the connection and the number.
    """

    from panelcad.importer.tile import merge_extractions
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    devices = [
        {"tag": "10OL-G", "symbol_class": "overload", "bbox": [40, 20, 22, 16], "confidence": 0.93},
        {"tag": "10G", "symbol_class": "motor", "bbox": [40, 70, 22, 16], "confidence": 0.93},
    ]
    conductor = {
        "wire_number": "10T1",
        "bbox": [44, 48, 12, 4],
        "confidence": 0.88,
        "from_tag": "10OL-G",
        "from_pin": "2",
        "to_tag": "10G",
        "to_pin": "T1",
    }
    label = {"wire_number": "10T1", "bbox": [46, 50, 8, 3], "confidence": 0.90}
    tile = {"id": "tile_04", "x": 0, "y": 0, "w": 180, "h": 140}

    def _one(device_rows: list[dict], conductors: list[dict], numbers: list[dict], box: dict) -> dict:
        facts, violations, _units = _reply_facts(
            {"devices": device_rows, "conductors": conductors, "wire_numbers": numbers, "buses": []},
            catalog,
            page_mm=box,
        )
        assert violations == []
        return facts

    def _built(merged: dict) -> None:
        assert len(merged["wires"]) == 1
        wire = merged["wires"][0]
        assert wire["number"] == "10T1"
        assert wire["confidence"] == 0.90
        assert wire["from_tag"] == "10OL-G" and wire["from_pin"] == "2"
        assert wire["to_tag"] == "10G" and wire["to_pin"] == "T1"
        session, _report = _build_facts(merged, catalog)
        assert "10T1" in {str(item.number) for sheet in session.project.sheets for item in sheet.wires}
        assert frozenset({"10OL-G.2", "10G.T1"}) in _pin_pairs(session)

    # One tile_04 reply lists the conductor, then the higher-confidence label.
    _built(merge_extractions([_one(devices, [conductor], [label], tile)]))
    # The label can also be merged first. Two one-row replies cover that order.
    label_only = _one([], [], [label], tile)
    conductor_only = _one(devices, [conductor], [], tile)
    _built(merge_extractions([conductor_only, label_only]))
    _built(merge_extractions([label_only, conductor_only]))

    # The label sits more than 8 mm from the conductor, on the overlapping tile.
    tile_04 = {"id": "tile_04", "x": 0.0, "y": 0.0, "w": 180.0, "h": 140.0}
    tile_01 = {"id": "tile_01", "x": 40.0, "y": 0.0, "w": 180.0, "h": 140.0}
    far_label = {"wire_number": "10T1", "bbox": [80, 70, 8, 3], "confidence": 0.90}
    far_only = _one([], [], [far_label], tile_01)
    held = _one(devices, [conductor], [], tile_04)
    _built(merge_extractions([held, far_only]))
    _built(merge_extractions([far_only, held]))


def test_family_mismatch_is_needs_info_and_overlap_misread_is_deduped(tmp_path: Path):
    """10M read as a motor is not a starter. A lower-confidence overlap of that symbol is dropped."""

    from panelcad.importer.diagnostics import ImportDiagnostics
    from panelcad.importer.symbols import print_tag_placeable
    from panelcad.importer.tile import merge_extractions
    from panelcad.importer.vision import detections_to_facts
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    tile = {"id": "tile_04", "x": 0, "y": 0, "w": 200, "h": 160}
    rows = [
        {"tag": "10M", "wire_number": "", "symbol_class": "motor", "bbox": [20, 30, 22, 16], "confidence": 0.62},
        {"tag": "10G", "wire_number": "", "symbol_class": "motor", "bbox": [80, 30, 22, 16], "confidence": 0.93},
        {"tag": "10M-G", "wire_number": "", "symbol_class": "contactor", "bbox": [20, 80, 22, 16], "confidence": 0.93},
    ]
    facts = detections_to_facts(rows, tile, catalog, standard="NEMA/ANSI")
    by_tag = {device["tag"]: device for device in facts["devices"]}
    assert print_tag_placeable(by_tag["10M"]) is False
    assert print_tag_placeable(by_tag["10G"]) is True
    assert print_tag_placeable(by_tag["10M-G"]) is True
    # 1F is the IEC overload (ref F). F1 and M1 keep the drawn class.
    assert print_tag_placeable({"tag": "1F", "symbol": "overload", "library_id": "iec.protection.overload_9a"}) is True
    assert print_tag_placeable({"tag": "F1", "symbol": "overload"}) is True
    assert print_tag_placeable({"tag": "M1", "symbol": "motor"}) is True
    session, report = _build_facts(facts, catalog)
    placed = {comp.id for sheet in session.project.sheets for comp in sheet.components}
    assert {"10G", "10M-G", "10M"} <= placed
    assert session.project.sheet("power").component("10M").library_id == "reconstruct.needs_info"
    assert "width_mm" not in (session.project.sheet("power").component("10M").properties or {})
    assert "motor" in session.project.sheet("power").component("10G").library_id
    skipped = {row["tag"]: row["reason"] for row in report["skipped"]}
    assert skipped["10M"] == "needs_info"

    motor = {
        "tag": "10G",
        "symbol": "motor",
        "type": "motor",
        "x": 140.0,
        "y": 48.0,
        "w": 22.0,
        "h": 16.0,
        "confidence": 0.93,
        "tile_id": "tile_04",
        "library_id": "nema.loads.motor_3ph",
        "source": "model",
    }
    junk = {
        "tag": "10M",
        "symbol": "motor",
        "type": "motor",
        "x": 146.0,
        "y": 50.0,
        "w": 22.0,
        "h": 16.0,
        "confidence": 0.62,
        "tile_id": "tile_01",
        "library_id": "nema.loads.motor_3ph",
        "source": "model",
    }
    for parts in (
        [{"devices": [motor], "wires": []}, {"devices": [junk], "wires": []}],
        [{"devices": [junk], "wires": []}, {"devices": [motor], "wires": []}],
    ):
        merged = merge_extractions(parts)
        assert [device["tag"] for device in merged["devices"]] == ["10G"]
        assert merged["spatial_drops"][0]["rule"] == "dedupe_spatial"
        assert merged["spatial_drops"][0]["tag"] == "10M"
    same_tile = dict(junk)
    same_tile["tile_id"] = "tile_04"
    kept = merge_extractions([{"devices": [motor, same_tile], "wires": []}])
    assert {device["tag"] for device in kept["devices"]} == {"10G", "10M"}
    assert kept["spatial_drops"] == []

    diag = ImportDiagnostics(tmp_path / "spatial", run_id="spatial")
    diag.ingest_tile(
        page_id="page_001",
        tile_id="tile_01",
        tile={"id": "tile_01", "x": 0, "y": 0, "w": 130, "h": 130},
        reported_rows=[{"tag": "10M", "symbol_class": "motor", "bbox": [40, 40, 22, 16], "confidence": 0.62}],
        violations=[],
        filtered_rows=[{"tag": "10M", "symbol_class": "motor", "bbox": [40, 40, 22, 16], "confidence": 0.62}],
    )
    diag.settle_page(
        page_id="page_001",
        before_devices=[junk],
        after_devices=[motor],
        before_wires=[],
        after_wires=[],
        width_mm=279.4,
        height_mm=431.8,
        catalog=catalog,
        spatial_drops=[{"tag": "10M", "rule": "dedupe_spatial"}],
    )
    lost = [item for item in diag.public()["items"] if item.get("text") == "10M"]
    assert lost and lost[0]["loss"] == "merge_loss" and lost[0]["rule"] == "dedupe_spatial"


def test_reimported_ethernet_wire_does_not_cut_a_device(dol, tmp_path: Path):
    """A lane nudge must not send the re-imported Ethernet cable through the HMI."""

    from panelcad.importer.geometry import geometry_problems
    from panelcad.library.catalog import default_catalog

    built = ImportJob.start([dol["pdf"]], tmp_path / "ethernet-body", backend="external").build()
    titles = {sheet.id: sheet.title for sheet in built.project.sheets}
    bodies = [
        titles.get(row["sheet"], row["sheet"])
        for row in geometry_problems(built.project, default_catalog())
        if row["kind"] == "body" and titles.get(row["sheet"], row["sheet"]) == "Ethernet"
    ]
    assert bodies == []
