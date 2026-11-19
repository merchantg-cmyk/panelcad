"""A terminal-strip row is mounting order, and a green block is ground."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from panelcad.importer.raster import _upscale_low_res
from panelcad.importer.strip_scan import find_block_row, read_terminal_strip


_EXPECTED_STRIP_WIRES: dict[str, tuple[str, str]] = {
    "X1-1": ("201", "301"),
    "X1-2": ("202", "302"),
    "X1-3": ("202", "303"),
    "X1-4": ("202", "304"),
}
_TRUTH_WIRE_NUMBERS: set[str] = {"201", "202", "301", "302", "303", "304"}
_LADDER_TERMINAL_DIR = Path(__file__).resolve().parent / "fixtures" / "scan_ladder" / "terminal"


def _render_demo_strip(dpi: int = 200) -> tuple[Image.Image, float, float]:
    """Render the demo terminal-strip sheet via Qt without external fixtures."""

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from panelcad.api.session import Session
    from panelcad.render.page import paint_sheet

    QApplication.instance() or QApplication([])
    root = Path(__file__).resolve().parents[1]
    session = Session.open(root / "examples" / "scan_ladder_demo.pcad.yaml")
    sheet = session.project.sheet("strip")
    width_mm, height_mm = sheet.page.dimensions()
    scale = dpi / 25.4
    qimg = QImage(
        max(1, int(round(width_mm * scale))),
        max(1, int(round(height_mm * scale))),
        QImage.Format.Format_RGB888,
    )
    qimg.fill(Qt.GlobalColor.white)
    painter = QPainter(qimg)
    painter.scale(scale, scale)
    paint_sheet(painter, session.project, sheet, session.catalog, sheet_index=3, sheet_count=3)
    painter.end()
    buf = bytes(qimg.constBits())
    image = Image.frombuffer(
        "RGB",
        (qimg.width(), qimg.height()),
        buf,
        "raw",
        "RGB",
        qimg.bytesPerLine(),
        1,
    ).copy()
    image.info["dpi"] = (dpi, dpi)
    return image, float(width_mm), float(height_mm)


def test_blocks_are_left_to_right_and_a_green_body_is_ground():
    gray = np.full((420, 700), 255, np.uint8)
    rgb = np.full((420, 700, 3), 255, np.uint8)
    ppm = 10.0
    edges = [40, 120, 200, 280, 360]
    for x in edges:
        cv2.line(gray, (x, 40), (x, 360), 0, 2)
    # The second gap is a ground block.
    rgb[50:350, 124:196] = (30, 170, 40)
    blocks = find_block_row(gray, rgb, ppm)
    assert [round(block["x_mm"], 1) for block in blocks] == [4.2, 12.2, 20.2, 28.2]
    assert [block["ground"] for block in blocks] == [False, True, False, False]


def test_a_coarse_scan_is_enlarged_without_changing_sheet_millimetres():
    # About 100 dpi on an A3 sheet. The millimetres stay 420 x 297.
    image = Image.new("RGB", (1654, 1169), "white")
    larger = _upscale_low_res(image, 420.0, 297.0)
    assert larger.width > image.width
    assert abs(larger.width / 420.0 - 200.0 / 25.4) < 0.4
    same = _upscale_low_res(Image.new("RGB", (3300, 2300), "white"), 420.0, 297.0)
    assert same.size == (3300, 2300)


def test_strip_wire_numbers_attach_to_right_terminals_across_degradations():
    """Real wire numbers above and below strip blocks attach to the right terminals."""

    from panelcad.ocr.backend import page_engine_ready, tesseract_required

    full_tag_ocr = page_engine_ready() or tesseract_required()
    expected_by_index = [
        ("X1-1", "201", "301", False),
        ("X1-2", "202", "302", False),
        ("X1-3", "202", "303", False),
        ("X1-4", "202", "304", True),
    ]
    base_200, width_mm, height_mm = _render_demo_strip(dpi=200)
    base_300, _, _ = _render_demo_strip(dpi=300)

    faded_arr = np.clip(np.asarray(base_200).astype(np.float32) * 0.42 + 148.0, 0, 255).astype(np.uint8)
    faded = Image.fromarray(faded_arr)

    buf = BytesIO()
    base_200.save(buf, format="JPEG", quality=30)
    buf.seek(0)
    jpeg = Image.open(buf).convert("RGB")

    for name, img in (("clean_300", base_300), ("dpi_200", base_200), ("faded", faded), ("jpeg", jpeg)):
        normalized = _upscale_low_res(img, width_mm, height_mm)
        rgb = np.asarray(normalized.convert("RGB"))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        ppm = rgb.shape[1] / width_mm
        devices = read_terminal_strip(gray, rgb, ppm)
        by_index = {int(dev["strip_index"]): dev for dev in devices}
        assert sorted(by_index) == [0, 1, 2, 3], (name, devices)
        found_numbers: set[str] = set()
        for idx, (want_tag, want_top, want_bottom, want_ground) in enumerate(expected_by_index):
            dev = by_index[idx]
            if full_tag_ocr or idx < 3:
                assert dev["tag"] == want_tag, (name, idx, dev)
            assert bool(dev["ground"]) is want_ground, (name, idx, dev)
            assert dev["wire_top"] == want_top, (name, want_tag, dev)
            assert dev["wire_bottom"] == want_bottom, (name, want_tag, dev)
            found_numbers.add(str(dev["wire_top"]))
            found_numbers.add(str(dev["wire_bottom"]))
        recall = len(found_numbers & _TRUTH_WIRE_NUMBERS) / len(_TRUTH_WIRE_NUMBERS)
        assert recall == 1.0, (name, found_numbers)


def test_blank_terminal_wire_slots_stay_unreadable_despite_title_screws_and_rail():
    """Unprinted wire slots stay '?' instead of reading the title, screws, or DIN rail."""

    from panelcad.ocr.backend import page_engine_ready, tesseract_required

    base_200, width_mm, _height_mm = _render_demo_strip(dpi=200)
    rgb = np.asarray(base_200.convert("RGB")).copy()
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    ppm = rgb.shape[1] / width_mm
    blocks = find_block_row(gray, rgb, ppm)
    assert len(blocks) == 4

    # Wipe X1-2's top wire number only (leaving the title above and top screw inside).
    b2 = blocks[1]
    y_top0 = max(0, int(b2["y"] - 4.8 * ppm))
    y_top1 = max(0, int(b2["y"] - 0.5 * ppm))
    rgb[y_top0:y_top1, int(b2["x"] + 2) : int(b2["x"] + b2["w"] - 2)] = 255

    # Wipe X1-3's bottom wire number only (leaving the bottom screw inside and DIN rail below).
    b3 = blocks[2]
    y_bot0 = min(rgb.shape[0], int(b3["y"] + b3["h"] + 0.5 * ppm))
    y_bot1 = min(rgb.shape[0], int(b3["y"] + b3["h"] + 3.5 * ppm))
    rgb[y_bot0:y_bot1, int(b3["x"] + 2) : int(b3["x"] + b3["w"] - 2)] = 255

    gray_mod = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    devices = read_terminal_strip(gray_mod, rgb, ppm)
    by_index = {int(dev["strip_index"]): dev for dev in devices}
    assert sorted(by_index) == [0, 1, 2, 3]
    if page_engine_ready() or tesseract_required():
        assert [by_index[i]["tag"] for i in range(4)] == ["X1-1", "X1-2", "X1-3", "X1-4"]
    assert by_index[1]["wire_top"] == "?"
    assert by_index[1]["wire_bottom"] == "302"
    assert by_index[2]["wire_top"] == "202"
    assert by_index[2]["wire_bottom"] == "?"
    assert by_index[0]["wire_top"] == "201"
    assert by_index[0]["wire_bottom"] == "301"
    assert by_index[3]["wire_top"] == "202"
    assert by_index[3]["wire_bottom"] == "304"


def test_terminal_fixture_import_wire_number_recall_floor(tmp_path: Path):
    """End-to-end terminal-strip scan import pins a high wire-number recall floor."""

    from panelcad.importer.pipeline import ImportJob
    from panelcad.ocr.backend import page_engine_ready, tesseract_required

    if not page_engine_ready() and not tesseract_required():
        pytest.skip("full-page terminal fixture import requires bundled ONNX weights or Tesseract")

    variants = ("clean_300", "dpi_200", "dpi_150", "dpi_100", "jpeg", "faded", "speckle", "fax")
    fixture_paths: dict[str, Path] = {}
    for variant in variants:
        found = None
        for suffix in (".png", ".jpg"):
            candidate = _LADDER_TERMINAL_DIR / f"{variant}{suffix}"
            if candidate.is_file():
                found = candidate
                break
        if found is not None:
            fixture_paths[variant] = found

    if len(fixture_paths) < len(variants):
        # Render any missing variants on the fly so the test still runs in a clean checkout.
        import importlib.util

        script = Path(__file__).resolve().parents[1] / "scripts" / "scan_ladder.py"
        spec = importlib.util.spec_from_file_location("panelcad_scan_ladder_script", script)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        by_id = {item["id"]: item for item in mod.VARIANTS}
        masters: dict[int, Image.Image] = {}
        for variant in variants:
            if variant in fixture_paths:
                continue
            cfg = by_id[variant]
            dpi = int(cfg["dpi"])
            if dpi not in masters:
                masters[dpi], _, _ = _render_demo_strip(dpi=dpi)
            degraded, opts = mod.degrade(masters[dpi], "terminal", cfg)
            ext = ".jpg" if opts["format"] == "JPEG" else ".png"
            dest = tmp_path / "fixtures" / f"{variant}{ext}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            degraded.save(dest, **opts)
            fixture_paths[variant] = dest

    strict_full = {"clean_300", "dpi_200", "dpi_150", "dpi_100", "faded", "fax"}
    for variant in variants:
        ws = tmp_path / f"ws_{variant}"
        job = ImportJob.start([fixture_paths[variant]], ws, backend="raster")
        built = job.build(layout_mode="preserve", target_standard="NEMA/ANSI")
        built_numbers: set[str] = set()
        comps_by_id = {}
        for sheet in built.project.sheets:
            for wire in sheet.wires:
                val = str(wire.number or "").strip()
                if val and not val.startswith("?"):
                    built_numbers.add(val)
            for comp in sheet.components:
                comps_by_id[comp.id] = comp
                props = comp.properties or {}
                for key in ("wire_top", "wire_bottom"):
                    val = str(props.get(key) or "").strip()
                    if val.isdigit():
                        built_numbers.add(val)
        recall = len(built_numbers & _TRUTH_WIRE_NUMBERS) / len(_TRUTH_WIRE_NUMBERS)
        floor = 1.0 if variant in strict_full else (5.0 / 6.0)
        assert recall >= floor, (variant, recall, sorted(_TRUTH_WIRE_NUMBERS - built_numbers))
        if variant == "clean_300":
            for tag, (want_top, want_bottom) in _EXPECTED_STRIP_WIRES.items():
                assert tag in comps_by_id
                props = comps_by_id[tag].properties or {}
                assert props.get("wire_top") == want_top, (tag, props)
                assert props.get("wire_bottom") == want_bottom, (tag, props)
