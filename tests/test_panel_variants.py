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
    _apply_quarter_turns,
    _glyph_quarter,
    _is_large_format_sheet,
    _raise_coarse_scan,
    denoise,
    despeckle_preserving_strokes,
)
from panelcad.importer.raster import _skew_angle, _upscale_low_res, prepare_opencv
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


def test_denoise_preserve_thin_strokes_keeps_clean_counters_and_cleans_small_bands():
    # Clean 100-DPI-like glyph stroke with a 1-px interior white counter (surrounded by 8 dark px).
    clean = np.full((180, 300, 3), 255, dtype=np.uint8)
    clean[40:60, 40:60] = 0
    clean[48, 48] = 255
    untouched = np.asarray(denoise(Image.fromarray(clean), preserve_thin_strokes=True))
    assert int(untouched[48, 48, 0]) >= 220

    # Large non-speckled sheet with >200 scattered 1-px dots (<250/MP) must not
    # false-trigger despeckling or fill 1-px glyph counters (e.g. post-median schematic).
    sparse = np.full((1000, 1500, 3), 255, dtype=np.uint8)
    sparse[40:60, 40:60] = 0
    sparse[48, 48] = 255
    for idx in range(250):
        sparse[100 + (idx // 25) * 30, 100 + (idx % 25) * 40] = 0
    sparse_out = np.asarray(denoise(Image.fromarray(sparse), preserve_thin_strokes=True))
    assert int(sparse_out[48, 48, 0]) >= 220

    # Cropped band smaller than full-page (>=200) cutoff, yet densely speckled (~1500/MP):
    # dark speckles and interior salt holes on strokes must both be cleaned.
    band = clean.copy()
    rng = np.random.default_rng(19)
    ys = rng.integers(80, 175, size=80)
    xs = rng.integers(5, 295, size=80)
    band[ys, xs] = 0
    cleaned = np.asarray(denoise(Image.fromarray(band), preserve_thin_strokes=True))
    assert int(cleaned[48, 48, 0]) < 60
    assert int((cleaned[80:175, 5:295, 0] < 120).sum()) < 15


def test_auto_rotate_speckle_probe_cleans_speckle_without_mutating_source(monkeypatch):
    from panelcad.importer import imageutil

    img = Image.new("RGB", (300, 400), "white")
    arr = np.asarray(img).copy()
    arr[40:90, 60] = 0
    arr[40, 60:95] = 0
    rng = np.random.default_rng(23)
    ys = rng.integers(120, 380, size=140)
    xs = rng.integers(10, 290, size=140)
    arr[ys, xs] = 0
    speckled = Image.fromarray(arr)

    seen_dark: list[int] = []

    def _fake_scores(probe: Image.Image, **_kwargs: Any) -> list[float]:
        g = np.asarray(probe.convert("L"))
        seen_dark.append(int((g[120:380, 10:290] < 120).sum()))
        return [0.0, 180.0, 0.0, 0.0]

    monkeypatch.setattr(imageutil, "_text_scores", _fake_scores)
    monkeypatch.setattr(imageutil, "_glyph_quarter", lambda _img: None)

    turned, quarter = imageutil.auto_rotate(speckled, large_format=False)
    assert quarter == 1
    assert seen_dark == [0]
    # Original pixels remain untouched (merely rotated) before prepare_scan handles sharp/clean split.
    assert turned.size == (400, 300)
    restored = np.asarray(turned.transpose(Image.Transpose.ROTATE_90))
    assert np.array_equal(restored, arr)


def test_speckle_panel_peek_and_route_under_electrical_backend(
    panel_variant_paths: tuple[dict[str, Any], dict[str, Path]],
):
    if not find_tesseract():
        pytest.skip("Tesseract binary is not installed")

    from panelcad.importer.scan_dispatch import peek_layout_page_kind, route_scan_page
    from panelcad.ocr.backend import ocr_scope

    truth, paths = panel_variant_paths
    with Image.open(paths["speckle"]) as src:
        image = src.convert("RGB")
    width_mm, height_mm = float(truth["page_mm"][0]), float(truth["page_mm"][1])

    assert peek_layout_page_kind(image, width_mm, height_mm) == "panel_layout"
    with ocr_scope("electrical"):
        route = route_scan_page(image, width_mm, height_mm)
    assert route.reader_kind == "panel"
    assert route.page_kind == "panel_layout"
    assert route.ocr_backend == "tesseract"
    assert route.layout_mode == "preserve"


def test_read_panel_scan_recovers_raw_speckled_layout_and_uprights(
    panel_variant_paths: tuple[dict[str, Any], dict[str, Path]],
):
    if not find_tesseract():
        pytest.skip("Tesseract binary is not installed")

    from panelcad.importer.imageutil import auto_rotate
    from panelcad.importer.scan_dispatch import read_panel_scan
    from panelcad.library.catalog import default_catalog
    from panelcad.ocr.backend import ocr_scope

    truth, paths = panel_variant_paths
    with Image.open(paths["speckle"]) as src:
        dpi = src.info.get("dpi")
        image = src.convert("RGB")
    width_mm, height_mm = float(truth["page_mm"][0]), float(truth["page_mm"][1])

    facts = read_panel_scan(image, width_mm=width_mm, height_mm=height_mm, catalog=default_catalog())
    assert facts["kind"] == "panel_layout"
    found = {
        tag
        for dev in (facts.get("devices") or [])
        if not dev.get("title_ink")
        for tag in (str(dev.get("tag") or "").strip().upper(),)
        if tag and tag != "?" and not tag.startswith("?")
    }
    truth_tags = set(truth["devices"])
    assert len(found & truth_tags) / len(truth_tags) >= 5 / 9

    sideways = image.transpose(Image.Transpose.ROTATE_270)
    if isinstance(dpi, tuple) and len(dpi) == 2:
        sideways.info["dpi"] = (dpi[1], dpi[0])
    with ocr_scope("tesseract"):
        turned, quarter = auto_rotate(sideways, large_format=True)
    assert quarter == 3
    assert turned.size == image.size
    assert np.array_equal(np.asarray(turned), np.asarray(image))


def test_auto_rotate_uprights_sideways_speckled_panel_under_electrical_backend(
    panel_variant_paths: tuple[dict[str, Any], dict[str, Path]],
):
    if not page_engine_ready():
        pytest.skip("bundled electrical OCR weights are not installed")

    from panelcad.importer.imageutil import auto_rotate
    from panelcad.ocr.backend import ocr_scope

    _truth, paths = panel_variant_paths
    with Image.open(paths["speckle"]) as src:
        dpi = src.info.get("dpi")
        image = src.convert("RGB")
    assert isinstance(dpi, tuple) and len(dpi) == 2

    sideways = image.transpose(Image.Transpose.ROTATE_270)
    sideways.info["dpi"] = (dpi[1], dpi[0])
    assert _is_large_format_sheet(sideways, sideways.info["dpi"]) is True

    with ocr_scope("electrical"):
        turned, quarter = auto_rotate(sideways, large_format=None)
    assert quarter == 3
    assert turned.size == image.size
    # Despeckle runs only on the probe; the returned image keeps unfiltered pixels.
    assert np.array_equal(np.asarray(turned), np.asarray(image))


def test_despeckle_preserving_strokes_guards_shapes_dtypes_and_contract():
    # Empty array and <5 px crops return the input unchanged with changed=False.
    empty = np.empty((0, 0, 3), dtype=np.uint8)
    out_empty, changed_empty = despeckle_preserving_strokes(empty)
    assert changed_empty is False
    assert out_empty is empty

    for shape in ((4, 20, 3), (20, 4, 3)):
        tiny_crop = np.full(shape, 255, dtype=np.uint8)
        out_tiny, changed_tiny = despeckle_preserving_strokes(tiny_crop)
        assert changed_tiny is False
        assert out_tiny is tiny_crop

    # Invalid ndim, channel counts, or non-numeric dtypes never raise cv2.error.
    for invalid in (
        np.zeros((100,), dtype=np.uint8),
        np.zeros((1, 20, 20, 3), dtype=np.uint8),
        np.zeros((20, 20, 2), dtype=np.uint8),
        np.zeros((20, 20, 5), dtype=np.uint8),
        np.full((20, 20, 3), "x", dtype="<U1"),
    ):
        out_inv, changed_inv = despeckle_preserving_strokes(invalid)
        assert changed_inv is False
        assert out_inv is invalid

    # Clean 3-channel uint8 RGB returns (arr, False) untouched.
    clean = np.full((120, 160, 3), 255, dtype=np.uint8)
    clean[20:40, 20:40] = 0
    clean[30, 30] = 255
    out_clean, changed_clean = despeckle_preserving_strokes(clean)
    assert changed_clean is False
    assert np.array_equal(out_clean, clean)

    # Speckled 3-channel uint8 RGB returns (out, True) and removes speckles.
    speckled = clean.copy()
    rng = np.random.default_rng(31)
    ys = rng.integers(50, 115, size=90)
    xs = rng.integers(5, 155, size=90)
    speckled[ys, xs] = 0
    out_spec, changed_spec = despeckle_preserving_strokes(speckled)
    assert changed_spec is True
    assert out_spec.dtype == np.uint8 and out_spec.shape == speckled.shape
    assert int((out_spec[50:115, 5:155, 0] < 120).sum()) < 15

    # Grayscale (2D and HxWx1), RGBA (HxWx4), and non-uint8 numeric arrays convert to 3-ch uint8 RGB.
    gray_2d = speckled[:, :, 0]
    out_gray, changed_gray = despeckle_preserving_strokes(gray_2d)
    assert changed_gray is True
    assert out_gray.dtype == np.uint8 and out_gray.shape == (120, 160, 3)

    gray_1ch = gray_2d[:, :, None]
    out_1ch, changed_1ch = despeckle_preserving_strokes(gray_1ch)
    assert changed_1ch is True
    assert out_1ch.dtype == np.uint8 and out_1ch.shape == (120, 160, 3)

    rgba = np.dstack([speckled, np.full((120, 160), 255, dtype=np.uint8)])
    out_rgba, changed_rgba = despeckle_preserving_strokes(rgba)
    assert changed_rgba is True
    assert out_rgba.dtype == np.uint8 and out_rgba.shape == (120, 160, 3)

    float01 = speckled.astype(np.float32) / 255.0
    out_f01, changed_f01 = despeckle_preserving_strokes(float01)
    assert changed_f01 is True
    assert out_f01.dtype == np.uint8 and out_f01.shape == (120, 160, 3)

    float255 = speckled.astype(np.float64)
    out_f255, changed_f255 = despeckle_preserving_strokes(float255)
    assert changed_f255 is True
    assert out_f255.dtype == np.uint8 and out_f255.shape == (120, 160, 3)

    u16 = speckled.astype(np.uint16) * 257
    out_u16, changed_u16 = despeckle_preserving_strokes(u16)
    assert changed_u16 is True
    assert out_u16.dtype == np.uint8 and out_u16.shape == (120, 160, 3)

    i32 = clean.astype(np.int32)
    out_i32, changed_i32 = despeckle_preserving_strokes(i32)
    assert changed_i32 is False
    assert out_i32.dtype == np.uint8 and out_i32.shape == (120, 160, 3)

    bool_img = clean[:, :, 0] > 128
    out_bool, changed_bool = despeckle_preserving_strokes(bool_img)
    assert changed_bool is False
    assert out_bool.dtype == np.uint8 and out_bool.shape == (120, 160, 3)

    # Degenerate case: >= speckle_floor 1x1 dark centers where med5[speck] == rgb[speck]
    # and no pixel exceeds 220 (so salt is empty) must return changed=False.
    degen = np.zeros((120, 120, 3), dtype=np.uint8)
    for row in range(8):
        for col in range(8):
            cy, cx = 10 + row * 12, 10 + col * 12
            degen[cy - 1 : cy + 2, cx - 1 : cx + 2] = 200
            degen[cy, cx] = 0
    out_degen, changed_degen = despeckle_preserving_strokes(degen)
    assert changed_degen is False
    assert np.array_equal(out_degen, degen)


def test_prepare_opencv_despeckles_before_bilateral_and_skew_and_keeps_clean_untouched(monkeypatch):
    from panelcad.importer import raster as raster_mod

    bilateral_inputs: list[np.ndarray] = []
    skew_inputs: list[np.ndarray] = []
    real_bilateral = cv2.bilateralFilter
    real_skew = raster_mod._skew_angle

    def _spy_bilateral(src: np.ndarray, *args: Any, **kwargs: Any) -> np.ndarray:
        bilateral_inputs.append(src.copy())
        return real_bilateral(src, *args, **kwargs)

    def _spy_skew(gray: np.ndarray) -> float:
        skew_inputs.append(gray.copy())
        return real_skew(gray)

    monkeypatch.setattr(cv2, "bilateralFilter", _spy_bilateral)
    monkeypatch.setattr(raster_mod, "_skew_angle", _spy_skew)

    # Speckled sheet: isolated dots must be removed before bilateralFilter and _skew_angle.
    speckled = np.full((300, 400, 3), 255, dtype=np.uint8)
    cv2.rectangle(speckled, (20, 20), (380, 280), (0, 0, 0), 2)
    rng = np.random.default_rng(41)
    ys = rng.integers(60, 240, size=180)
    xs = rng.integers(60, 340, size=180)
    speckled[ys, xs] = 0

    cleaned_img, meta = prepare_opencv(Image.fromarray(speckled))
    assert "deskew_deg" in meta
    assert len(bilateral_inputs) == 1 and len(skew_inputs) == 1
    assert int((bilateral_inputs[0][60:240, 60:340, 0] < 120).sum()) < 20
    assert int((skew_inputs[0][60:240, 60:340] < 120).sum()) < 20
    # Frame border ink survives.
    assert int((np.asarray(cleaned_img)[20, 20:380, 0] < 120).sum()) > 300

    # Clean sheet with a 1-px interior white counter stays untouched entering bilateralFilter.
    clean = np.full((300, 400, 3), 255, dtype=np.uint8)
    cv2.rectangle(clean, (20, 20), (380, 280), (0, 0, 0), 2)
    clean[90:110, 90:110] = 0
    clean[100, 100] = 255

    prepare_opencv(Image.fromarray(clean))
    assert len(bilateral_inputs) == 2
    assert np.array_equal(bilateral_inputs[1], clean)


def _make_speckled_probe_fixture(width: int = 300, height: int = 400, count: int = 160) -> tuple[np.ndarray, Image.Image]:
    arr = np.full((height, width, 3), 255, dtype=np.uint8)
    arr[20:70, 30] = 0
    arr[20, 30:75] = 0
    rng = np.random.default_rng(53)
    ys = rng.integers(90, height - 10, size=count)
    xs = rng.integers(10, width - 10, size=count)
    arr[ys, xs] = 0
    return arr, Image.fromarray(arr)


def test_apply_quarter_turns_matches_clockwise_quarter_steps():
    arr = np.arange(3 * 4 * 3, dtype=np.uint8).reshape((3, 4, 3))
    img = Image.fromarray(arr)
    for quarter, expected_transpose in (
        (0, None),
        (1, Image.Transpose.ROTATE_270),
        (2, Image.Transpose.ROTATE_180),
        (3, Image.Transpose.ROTATE_90),
        (4, None),
        (-1, Image.Transpose.ROTATE_90),
    ):
        turned, turns = _apply_quarter_turns(img, quarter)
        assert turns == quarter % 4
        expected = img if expected_transpose is None else img.transpose(expected_transpose)
        assert np.array_equal(np.asarray(turned), np.asarray(expected))


@pytest.mark.parametrize("fallback", ("deskew_retry", "decisive_title", "osd_reading", "ink_rotate"))
def test_auto_rotate_fallback_paths_probe_despeckled_and_return_unfiltered_original(
    monkeypatch: pytest.MonkeyPatch,
    fallback: str,
):
    from panelcad.importer import imageutil

    if fallback == "deskew_retry":
        arr, speckled = _make_speckled_probe_fixture(width=2700, height=160, count=360)
        large_format = True
    else:
        arr, speckled = _make_speckled_probe_fixture(width=300, height=400, count=160)
        large_format = False

    def _speckle_count(probe: Image.Image) -> int:
        g = np.asarray(probe.convert("L"))
        return int((g[90 : g.shape[0] - 10, 10 : g.shape[1] - 10] < 120).sum())

    probed_dark: list[int] = []
    monkeypatch.setattr(imageutil, "_glyph_quarter", lambda _img: None)

    if fallback == "deskew_retry":
        def _fake_scores(probe: Image.Image, **kwargs: Any) -> list[float]:
            if kwargs.get("target_long") == 3600:
                probed_dark.append(_speckle_count(probe))
                return [0.0, 0.0, 180.0, 0.0]
            return [0.0, 0.0, 0.0, 0.0]

        def _fake_deskew(probe: Image.Image, **_kwargs: Any) -> tuple[Image.Image, float]:
            probed_dark.append(_speckle_count(probe))
            return probe, 0.0

        monkeypatch.setattr(imageutil, "_text_scores", _fake_scores)
        monkeypatch.setattr(imageutil, "deskew", _fake_deskew)
        expected_quarter = 2
        restore_transpose = Image.Transpose.ROTATE_180
    else:
        monkeypatch.setattr(imageutil, "_text_scores", lambda _img, **_kw: [0.0, 0.0, 0.0, 0.0])
        if fallback == "decisive_title":
            def _fake_title(probe: Image.Image) -> tuple[Image.Image, int]:
                probed_dark.append(_speckle_count(probe))
                return probe, 3

            monkeypatch.setattr(imageutil, "_decisive_title", _fake_title)
            expected_quarter = 3
            restore_transpose = Image.Transpose.ROTATE_270
        elif fallback == "osd_reading":
            def _fake_osd(probe: Image.Image) -> tuple[int, float]:
                probed_dark.append(_speckle_count(probe))
                return 90, 6.5

            monkeypatch.setattr(imageutil, "_decisive_title", lambda _img: None)
            monkeypatch.setattr(imageutil, "_osd_reading", _fake_osd)
            expected_quarter = 3
            restore_transpose = Image.Transpose.ROTATE_270
        else:
            def _fake_ink(probe: Image.Image) -> tuple[Image.Image, int]:
                probed_dark.append(_speckle_count(probe))
                return probe, 1

            monkeypatch.setattr(imageutil, "_decisive_title", lambda _img: None)
            monkeypatch.setattr(imageutil, "_osd_reading", lambda _img: (None, 0.0))
            monkeypatch.setattr(imageutil, "_ink_rotate", _fake_ink)
            expected_quarter = 1
            restore_transpose = Image.Transpose.ROTATE_90

    turned, quarter = imageutil.auto_rotate(speckled, large_format=large_format)
    assert quarter == expected_quarter
    assert probed_dark and all(count < 15 for count in probed_dark)
    restored = np.asarray(turned.transpose(restore_transpose))
    assert np.array_equal(restored, arr)
