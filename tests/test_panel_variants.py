"""Regression tests for degraded panel-layout scan reading.

Covers the preprocessing, scaling, orientation, deskew, height-cutoff, and
layout-routing root causes that previously dropped degraded ANSI D panel-layout
variants (dpi_200, dpi_150, dpi_100, jpeg, skew, speckle, fax) to ~0% device
recall, plus end-to-end recall floors on every non-phone panel variant.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from panelcad.importer.imageutil import (
    _glyph_quarter,
    _is_large_format_sheet,
    _raise_coarse_scan,
    denoise,
)
from panelcad.importer.raster import _skew_angle, _upscale_low_res
from panelcad.importer.read import _tag_glyph_height, _tag_height_cutoff, label_tokens
from panelcad.importer.tesseract_runtime import find_tesseract
from panelcad.ocr.backend import active_backend, page_engine_ready

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "scan_ladder"

# Non-phone panel-layout variants and their minimum device recall floors (out of 9 truth devices).
PANEL_RECALL_FLOORS: tuple[tuple[str, float], ...] = (
    ("clean_300", 5 / 9),
    ("dpi_200", 5 / 9),
    ("dpi_150", 5 / 9),
    ("dpi_100", 4 / 9),
    ("jpeg", 4 / 9),
    ("skew", 5 / 9),
    ("faded", 5 / 9),
    ("speckle", 5 / 9),
    ("fax", 5 / 9),
)


def test_tag_height_cutoff_keeps_panel_cluster_despite_single_tall_outlier():
    # 5 panel device tags at ~1.78 mm plus 1 taller misread token at 2.48 mm.
    heights = [1.71, 1.78, 1.78, 1.78, 1.78, 2.48]
    cutoff = _tag_height_cutoff(heights)
    assert cutoff <= 1.71
    assert all(h >= cutoff for h in heights[:5])


def test_tag_height_cutoff_keeps_tall_tags_over_crowd_of_tiny_labels():
    # Two real tags above three tiny point labels must not drop to the tiny size.
    heights = [1.2, 1.2, 1.2, 3.0, 3.0]
    cutoff = _tag_height_cutoff(heights)
    assert 1.2 < cutoff <= 3.0


def test_vertical_terminal_tags_do_not_drop_horizontal_panel_tags():
    tokens = [
        {"text": "DISC1", "x": 144.0, "y": 144.0, "w": 7.4, "h": 3.0, "confidence": 0.9},
        {"text": "FU1", "x": 203.0, "y": 144.0, "w": 5.0, "h": 3.1, "confidence": 0.9},
        {"text": "M1", "x": 258.0, "y": 144.0, "w": 4.3, "h": 3.2, "confidence": 0.9},
        {"text": "PS1", "x": 140.0, "y": 274.0, "w": 5.0, "h": 2.9, "confidence": 0.9},
        {"text": "CR1", "x": 191.0, "y": 274.0, "w": 5.4, "h": 3.0, "confidence": 0.9},
        # Vertical 90-deg terminal labels: string length is in h (~7.5 mm), glyph height is in w (~3.8 mm).
        {"text": "X1-1", "x": 119.0, "y": 400.0, "w": 3.8, "h": 7.6, "confidence": 0.9},
        {"text": "X1-2", "x": 130.0, "y": 457.0, "w": 3.8, "h": 7.3, "confidence": 0.9},
        {"text": "X1-3", "x": 141.0, "y": 400.0, "w": 3.9, "h": 7.5, "confidence": 0.9},
        {"text": "X1-4", "x": 152.0, "y": 457.0, "w": 4.0, "h": 7.4, "confidence": 0.9},
    ]
    assert _tag_glyph_height(tokens[-1]) == pytest.approx(4.0)
    labeled = label_tokens(tokens, 558.8, 863.6)
    found = {tok["text"] for tok in labeled["tags"]}
    assert {"DISC1", "FU1", "M1", "PS1", "CR1", "X1-1", "X1-2", "X1-3", "X1-4"} <= found


def test_raise_coarse_scan_enlarges_ansi_d_100_dpi_but_not_camera_72_dpi():
    ansi_d_100 = Image.new("RGB", (2200, 3400), "white")
    raised, new_dpi = _raise_coarse_scan(ansi_d_100, (100.0, 100.0))
    assert raised.size == (4400, 6800)
    assert new_dpi is not None and abs(new_dpi[0] - 200.0) < 0.5
    assert _is_large_format_sheet(raised, new_dpi) is True

    phone_72 = Image.new("RGB", (2200, 3400), "white")
    same, same_dpi = _raise_coarse_scan(phone_72, (72.0, 72.0))
    assert same.size == (2200, 3400)
    assert same_dpi == (72.0, 72.0)


def test_upscale_low_res_enlarges_ansi_d_100_dpi():
    ansi_d_100 = Image.new("RGB", (2200, 3400), "white")
    enlarged = _upscale_low_res(ansi_d_100, 558.8, 863.6)
    assert enlarged.width > ansi_d_100.width
    assert abs(enlarged.width / 558.8 - 200.0 / 25.4) < 0.5


def test_denoise_preserve_thin_strokes_keeps_lines_and_clears_speckle():
    arr = np.full((300, 400, 3), 255, dtype=np.uint8)
    # 1-pixel thin text-like glyph stroke.
    arr[40:60, 50] = 0
    arr[40, 50:65] = 0
    arr[50, 50:62] = 0
    # Sprinkle >250 isolated 1x1 and 2x2 dark speckles far from the stroke.
    rng = np.random.default_rng(7)
    ys = rng.integers(80, 290, size=320)
    xs = rng.integers(10, 390, size=320)
    arr[ys, xs] = 0
    arr[ys[:80], np.minimum(xs[:80] + 1, 399)] = 0

    cleaned = np.asarray(denoise(Image.fromarray(arr), preserve_thin_strokes=True))
    # The 1-px stroke survives intact.
    assert int((cleaned[40:60, 50, 0] < 60).sum()) >= 18
    assert int((cleaned[40, 50:65, 0] < 60).sum()) >= 13
    # Isolated background speckles are removed.
    remaining = int((cleaned[80:290, 10:390, 0] < 120).sum())
    assert remaining < 40


def test_skew_angle_ignores_shaded_panel_wire_ducts_and_detects_real_tilt():
    # Straight shaded panel with gray wire ducts (similar to ANSI D backplate).
    straight = np.full((1600, 1100), 255, dtype=np.uint8)
    cv2.rectangle(straight, (40, 40), (1060, 1560), 0, 2)
    # Shaded backplate + horizontal/vertical wire duct bands with fingers.
    straight[100:1100, 80:980] = 242
    straight[100:170, 80:980] = 205
    straight[1030:1100, 80:980] = 205
    for x in range(100, 960, 24):
        cv2.line(straight, (x, 100), (x, 165), 80, 2)
        cv2.line(straight, (x, 1035), (x, 1100), 80, 2)
    cv2.rectangle(straight, (80, 100), (980, 1100), 60, 2)
    assert abs(_skew_angle(straight)) < 0.25

    # Rotate the same sheet by 2.2 degrees and verify the detector catches it.
    tilted_img = Image.fromarray(straight).rotate(
        2.2, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=255
    )
    detected = _skew_angle(np.asarray(tilted_img))
    assert abs(abs(detected) - 2.2) < 0.7


def test_glyph_quarter_handles_faded_low_contrast_sheet():
    img = Image.new("RGB", (900, 1200), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Draw faded top-heavy capital "T" glyphs at luminance ~195 (above the 160 cutoff).
    for row in range(3):
        y0 = 200 + row * 120
        for col in range(6):
            x0 = 150 + col * 90
            draw.rectangle((x0, y0, x0 + 50, y0 + 10), fill=(192, 192, 192))
            draw.rectangle((x0 + 20, y0 + 10, x0 + 30, y0 + 55), fill=(192, 192, 192))
    assert _glyph_quarter(img) == 0


def test_read_pixels_layout_page_does_not_overwrite_panel_tag_with_ol1(monkeypatch):
    from panelcad.importer import raster as raster_mod
    from panelcad.library.catalog import default_catalog

    called = {"land_body": 0}

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        called["land_body"] += 1

    monkeypatch.setattr(raster_mod, "_land_body_symbols", _boom)
    monkeypatch.setattr(raster_mod, "_coils_from_ink", lambda *a, **k: [])
    monkeypatch.setattr(raster_mod, "_fuses_from_ink", lambda *a, **k: [])
    monkeypatch.setattr(
        raster_mod,
        "_ocr_tokens",
        lambda *a, **k: [
            {"text": "DISC1", "x": 144.0, "y": 144.0, "w": 6.5, "h": 1.8, "confidence": 0.85, "source": "ocr"},
            {"text": "FU1", "x": 203.0, "y": 144.0, "w": 4.0, "h": 1.8, "confidence": 0.85, "source": "ocr"},
            {"text": "M1", "x": 258.0, "y": 144.0, "w": 2.8, "h": 1.8, "confidence": 0.85, "source": "ocr"},
            {"text": "PS1", "x": 140.0, "y": 274.0, "w": 4.0, "h": 1.8, "confidence": 0.85, "source": "ocr"},
            {"text": "CR1", "x": 191.0, "y": 274.0, "w": 4.2, "h": 1.8, "confidence": 0.85, "source": "ocr"},
        ],
    )
    monkeypatch.setattr(
        raster_mod,
        "_title_tokens",
        lambda *a, **k: [
            {"text": "TITLE", "x": 100.0, "y": 820.0, "w": 8.0, "h": 1.5, "confidence": 0.9, "source": "ocr"},
            {"text": "Backplate", "x": 104.0, "y": 828.0, "w": 12.0, "h": 2.0, "confidence": 0.9, "source": "ocr"},
        ],
    )
    monkeypatch.setattr(raster_mod, "ocr_title_sheet", lambda *a, **k: {})

    blank = Image.new("RGB", (1100, 1700), "white")
    facts = raster_mod.read_pixels(blank, 558.8, 863.6, default_catalog(), allow_missing_ocr=True)
    assert facts["kind"] == "panel_layout"
    assert called["land_body"] == 0
    tags = {d["tag"] for d in facts["devices"]}
    assert {"DISC1", "FU1", "M1", "PS1", "CR1"} <= tags
    assert "OL1" not in tags


def _load_scan_ladder_module():
    path = ROOT / "scripts" / "scan_ladder.py"
    spec = importlib.util.spec_from_file_location("_panelcad_scan_ladder", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ocr_available() -> bool:
    if active_backend() == "tesseract":
        return bool(find_tesseract())
    return page_engine_ready() or bool(find_tesseract())


@pytest.fixture(scope="module")
def panel_variant_paths(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], dict[str, Path]]:
    """Return panel truth dict and image paths for all non-phone panel variants."""

    ladder = _load_scan_ladder_module()
    truth_path = FIXTURE_ROOT / "truth" / "panel.json"
    panel_dir = FIXTURE_ROOT / "panel"
    if truth_path.is_file() and panel_dir.is_dir():
        paths: dict[str, Path] = {}
        for variant_id, _floor in PANEL_RECALL_FLOORS:
            for suffix in (".png", ".jpg"):
                candidate = panel_dir / f"{variant_id}{suffix}"
                if candidate.is_file():
                    paths[variant_id] = candidate
                    break
        if len(paths) == len(PANEL_RECALL_FLOORS):
            return json.loads(truth_path.read_text(encoding="utf-8")), paths

    # Generate panel variants on the fly when scan_ladder fixtures are absent.
    from panelcad.api.session import Session

    work = tmp_path_factory.mktemp("panel-variants")
    source = next(item for item in ladder.SOURCES if item["id"] == "panel")
    session = Session.open(ROOT / source["project"])
    truth = ladder.sheet_truth(session, source)
    wanted = {variant_id for variant_id, _floor in PANEL_RECALL_FLOORS}
    variants = [item for item in ladder.VARIANTS if item["id"] in wanted]
    masters: dict[int, Image.Image] = {}
    for dpi in sorted({int(item["dpi"]) for item in variants}):
        raster = ladder.render_sheet(ROOT / source["project"], source["sheet"], work / "renders", dpi)
        masters[dpi] = Image.open(raster).convert("RGB")
    paths = {}
    for variant in variants:
        image, options = ladder.degrade(masters[int(variant["dpi"])], "panel", variant)
        suffix = ".jpg" if options["format"] == "JPEG" else ".png"
        dest = work / "panel" / f"{variant['id']}{suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        image.save(dest, **options)
        paths[variant["id"]] = dest
    return truth, paths


@pytest.mark.parametrize(("variant_id", "min_recall"), PANEL_RECALL_FLOORS)
def test_degraded_panel_variant_meets_device_recall_floor(
    variant_id: str,
    min_recall: float,
    panel_variant_paths: tuple[dict[str, Any], dict[str, Path]],
    tmp_path: Path,
) -> None:
    if not _ocr_available():
        pytest.skip("neither bundled electrical OCR weights nor Tesseract binary is installed")

    from panelcad.importer.pipeline import ImportJob

    truth, paths = panel_variant_paths
    image_path = paths[variant_id]
    job = ImportJob.start([image_path], tmp_path / variant_id, backend="raster")
    built = job.build(layout_mode="preserve", target_standard=str(truth.get("standard") or "auto"))
    built_tags = {
        comp.id
        for sheet in built.project.sheets
        for comp in sheet.components
        if "offpage" not in str(comp.library_id or "")
        and comp.id != "?"
        and not str(comp.id).startswith("?")
    }
    truth_tags = set(truth["devices"])
    recall = len(truth_tags & built_tags) / len(truth_tags)
    assert recall >= min_recall, (
        f"panel/{variant_id} recall {recall:.3f} < {min_recall:.3f}; "
        f"found={sorted(truth_tags & built_tags)} missing={sorted(truth_tags - built_tags)}"
    )
