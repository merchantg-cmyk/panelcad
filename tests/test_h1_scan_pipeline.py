"""H1 scan-to-issue pipeline hardening: typed dispatcher, OCR logging, fine crops, rules, preserve quarantine."""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from panelcad.api.session import Session
from panelcad.importer.electrical_rules import (
    CONTROL_SECONDARY_HOT_WIRE,
    CONTROL_SECONDARY_RETURN_WIRE,
    validate_transformer_control_rules,
)
from panelcad.importer.preserve_mode import (
    default_scan_layout_mode,
    preserve_scan_issue_blocked,
    resolve_effective_layout_mode,
)
from panelcad.importer.scan_dispatch import ScanPageRoute, route_scan_page
from panelcad.ocr import ppocr

ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "scan_ladder"


def test_typed_scan_dispatcher_routes_schematic_panel_and_terminal() -> None:
    schem = Image.open(ROOT / "schematic" / "dpi_150.png").convert("RGB")
    s_route = route_scan_page(schem, 431.8, 279.4)
    assert isinstance(s_route, ScanPageRoute)
    assert s_route.reader_kind == "schematic"
    assert s_route.ocr_backend is None
    assert s_route.layout_mode == "ladder"

    term = Image.open(ROOT / "terminal" / "dpi_200.png").convert("RGB")
    t_route = route_scan_page(term, 431.8, 279.4)
    assert t_route.reader_kind == "terminal"
    assert t_route.ocr_backend == "tesseract"
    assert t_route.layout_mode == "preserve"

    panel_raw = Image.open(ROOT / "panel" / "clean_300.png")
    pdpi = panel_raw.info.get("dpi", (300, 300))
    px = float(pdpi[0] if isinstance(pdpi, tuple) else pdpi)
    panel = panel_raw.convert("RGB")
    p_route = route_scan_page(panel, panel.size[0] * 25.4 / px, panel.size[1] * 25.4 / px)
    assert p_route.reader_kind == "panel"
    assert p_route.ocr_backend == "tesseract"
    assert p_route.layout_mode == "preserve"


def test_ppocr_logs_and_records_page_failure_instead_of_silent_except(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from panelcad.ocr import weights

    monkeypatch.setattr(ppocr, "available", lambda: True)
    monkeypatch.setattr(weights, "weights_ready", lambda: True)

    def _boom():
        raise RuntimeError("simulated onnxruntime load error")

    monkeypatch.setattr(ppocr, "_page", _boom)
    img = np.full((1000, 1000, 3), 255, dtype=np.uint8)
    with caplog.at_level(logging.WARNING, logger="panelcad.ocr.ppocr"):
        result = ppocr.read_print(img)
    assert result is None
    assert "RuntimeError: simulated onnxruntime load error" in ppocr.last_error()
    assert any("PP-OCR page read failed" in rec.message for rec in caplog.records)


def test_fine_crops_handles_speckled_image_over_6000_components() -> None:
    """Noise speckles > 6000 components must not disable fine-crop enrichment."""

    rng = np.random.default_rng(42)
    img = np.full((1400, 2000, 3), 255, dtype=np.uint8)
    # Add > 7000 tiny 1-2 px dust speckles (previously caused count > 6000 early exit).
    ys = rng.integers(10, 1380, size=7500)
    xs = rng.integers(10, 1980, size=7500)
    img[ys, xs] = 0
    # Paint a dense cluster of 55 glyph-sized rectangles (4x10 px, area=40) in top-left.
    for row in range(5):
        for col in range(11):
            x0 = 120 + col * 14
            y0 = 120 + row * 22
            cv2.rectangle(img, (x0, y0), (x0 + 4, y0 + 10), (0, 0, 0), -1)

    crops = ppocr._fine_crops(img)
    assert len(crops) >= 1
    (ox, oy), enlarged, scale = crops[0]
    assert ox <= 120 and oy <= 120
    assert enlarged.size > 0 and scale > 1.0


def test_fine_crops_uses_exact_720x500_windows_not_coarse_cell_bins() -> None:
    """An 80 px cell grid over-counted sideways pages; keep exact 720×500 windows."""

    img = np.full((2000, 3000, 3), 255, dtype=np.uint8)
    # Dense 4×10 glyph cluster whose densest exact window starts at (400, 200).
    for row in range(8):
        for col in range(20):
            x0 = 420 + col * 14
            y0 = 220 + row * 22
            cv2.rectangle(img, (x0, y0), (x0 + 4, y0 + 10), (0, 0, 0), -1)
    # Secondary weaker cluster far away (must not win).
    for row in range(4):
        for col in range(8):
            x0 = 2200 + col * 14
            y0 = 900 + row * 22
            cv2.rectangle(img, (x0, y0), (x0 + 4, y0 + 10), (0, 0, 0), -1)

    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    _thr, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    _count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(ink, 8)
    height, width = img.shape[:2]
    ceiling = int(height * 0.72)
    body = stats[1:]
    mask = (
        (body[:, cv2.CC_STAT_AREA] >= 8)
        & (body[:, cv2.CC_STAT_HEIGHT] >= 4)
        & (body[:, cv2.CC_STAT_HEIGHT] <= 18)
        & (body[:, cv2.CC_STAT_WIDTH] >= 1)
        & (body[:, cv2.CC_STAT_WIDTH] <= 40)
        & (body[:, cv2.CC_STAT_TOP] < ceiling)
    )
    gx = body[mask, cv2.CC_STAT_LEFT]
    gy = body[mask, cv2.CC_STAT_TOP]
    win_w, win_h, step = 720, 500, 80
    best_count = 0
    best_xy = (0, 0)
    for top in range(0, max(1, ceiling - 40), step):
        for left in range(0, max(1, width - 80), step):
            hits = int(((gx >= left) & (gx < left + win_w) & (gy >= top) & (gy < top + win_h)).sum())
            if hits > best_count:
                best_count = hits
                best_xy = (left, top)
    assert best_count >= 40
    crops = ppocr._fine_crops(img)
    assert len(crops) >= 1
    (ox, oy), _enlarged, _scale = crops[0]
    assert (ox, oy) == (max(0, best_xy[0] - 8), max(0, best_xy[1] - 8))


def test_panel_faded_stays_upright_with_exact_fine_crops() -> None:
    """Faded panel scan must not flip 180° because sideways fine-crops inflate tag scores."""

    from panelcad.ocr.weights import weights_ready

    if not weights_ready():
        pytest.skip("electrical OCR weights are not installed")
    from panelcad.importer.imageutil import _glyph_quarter, _text_scores, choose_quarter

    img = Image.open(ROOT / "panel" / "faded.png").convert("RGB")
    quarter = choose_quarter(_text_scores(img), _glyph_quarter(img))
    assert quarter in {0, None}


def test_centralized_transformer_control_rules_enforce_shop_invariants() -> None:
    assert CONTROL_SECONDARY_HOT_WIRE == "102"
    assert CONTROL_SECONDARY_RETURN_WIRE == "104"

    # Valid wiring: H1/H2 off-page, X1 -> FU3 -> L1 (102), X2 -> L2 (104).
    good = Session.create("Valid CPT")
    good.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", rails="L1/L2")
    good.place("nema.power.control_transformer", ref="T1", x=60, y=40)
    good.place("nema.protection.fuse", ref="FU3", x=120, y=50)
    good.place("iec.connection.offpage", ref="OFF1", x=20, y=40)
    good.place("iec.connection.offpage", ref="OFF2", x=20, y=60)
    good.wire("OFF1.A", "T1.H1", number="L1A")
    good.wire("OFF2.A", "T1.H2", number="L2A")
    good.wire("T1.X1", "FU3.1", number="102A")
    good.wire("FU3.2", "L1", number="102")
    good.wire("T1.X2", "L2", number="104")
    assert validate_transformer_control_rules(good.sheet()) == []

    # Illegal wiring: primary H1 to L1 AND secondary X1 straight to L1.
    bad = Session.create("Bad CPT")
    bad.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", rails="L1/L2")
    bad.place("nema.power.control_transformer", ref="T1", x=60, y=40)
    bad.wire("T1.H1", "L1", number="101")
    bad.wire("T1.X1", "L1", number="102")
    codes = {v.code for v in validate_transformer_control_rules(bad.sheet())}
    assert "transformer_primary_on_control_rail" in codes
    assert "transformer_x1_direct_to_l1" in codes


def test_preserve_mode_quarantine_defaults_and_issue_block() -> None:
    assert default_scan_layout_mode({"sheets": [{"kind": "schematic", "page_kind": "schematic_control"}]}) == "ladder"
    assert default_scan_layout_mode({"sheets": [{"kind": "panel_layout", "page_kind": "panel_layout"}]}) == "preserve"
    assert default_scan_layout_mode({"sheets": [{"kind": "terminal", "page_kind": "terminal_plan"}]}) == "preserve"
    assert (
        resolve_effective_layout_mode(
            requested="",
            stamped="",
            scan_reader=True,
            stitched={"sheets": [{"kind": "schematic", "page_kind": "schematic_control"}]},
        )
        == "ladder"
    )
    session = Session.create("Preserve Scan")
    session.project.properties["print_import"] = {"from_scan": "yes", "layout_mode": "preserve"}
    assert preserve_scan_issue_blocked(session.project) is True
    session.project.properties["print_import"]["layout_mode"] = "ladder"
    assert preserve_scan_issue_blocked(session.project) is False


def test_nema_transformer_primary_pin_tags_are_not_classified_as_pilot_lights() -> None:
    """H1..H4 on a NEMA/ANSI sheet are transformer primary terminals, never pilot lights."""

    from panelcad.importer.ladder_synth import _classify_device_role_and_library

    catalog = Session.create("H-tags").catalog
    for tag in ("H1", "H2", "H3", "H4"):
        role_nema, lid_nema, _pins = _classify_device_role_and_library(
            tag, "", "", "", [], "NEMA/ANSI", catalog
        )
        assert "pilot_light" not in lid_nema, (tag, role_nema, lid_nema)
    role_iec, lid_iec, _pins = _classify_device_role_and_library(
        "H1", "", "", "", [], "IEC", catalog
    )
    assert lid_iec == "iec.operators.pilot_light"


def test_panel_and_terminal_readers_fall_back_to_electrical_when_tesseract_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a Tesseract binary, bundled electrical OCR must still route and read panel/terminal scans."""

    from panelcad.importer import scan_dispatch, tesseract_runtime
    from panelcad.ocr.backend import active_backend, page_engine_ready

    if not page_engine_ready():
        pytest.skip("electrical OCR weights are not installed")

    monkeypatch.setattr(tesseract_runtime, "find_tesseract", lambda: None)
    seen_backends: list[str] = []

    def _fake_read_pixels(*_args, **_kwargs):
        seen_backends.append(active_backend())
        return {"devices": [], "wires": [], "xrefs": []}

    from panelcad.importer import raster

    monkeypatch.setattr(raster, "read_pixels", _fake_read_pixels)
    dummy = Image.new("RGB", (200, 100), "white")
    scan_dispatch.read_panel_scan(dummy, width_mm=200.0, height_mm=100.0, catalog=None)
    scan_dispatch.read_terminal_scan(dummy, width_mm=200.0, height_mm=100.0, catalog=None)
    assert seen_backends == ["electrical", "electrical"]
