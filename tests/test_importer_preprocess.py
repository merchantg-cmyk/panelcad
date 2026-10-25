"""Tests for Patch 2: 4-corner perspective rectification, illumination/shadow/crease
normalization, and phone-camera 72/96 DPI normalization.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from panelcad.examples.dol import build_dol
from panelcad.importer.imageutil import prepare_scan
from panelcad.importer.ingest import _ingest_image, ingest_paths, rasterize_pdf, resolve_sheet_size_mm, sheet_size_mm
from panelcad.importer.preprocess import (
    PreparedScan,
    detect_sheet_quad,
    is_default_camera_dpi,
    normalize_camera_dpi,
    normalize_illumination,
    rectify_perspective,
)
from panelcad.importer.raster import _ink, detect_frame_insets, read_pixels
from panelcad.importer.workspace import Workspace
from panelcad.library.catalog import default_catalog


def _apply_shadow_and_crease(rgb: np.ndarray) -> np.ndarray:
    """Apply a dark corner vignette shadow (down to <180 gray) and a paper fold crease."""
    h, w = rgb.shape[:2]
    ys = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    xs = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
    # Uneven lighting gradient dropping to 0.56 in bottom-right (255 * 0.56 = 142 < 180 threshold!)
    lighting = 1.0 - 0.26 * xs - 0.18 * ys
    # Soft diagonal fold crease shadow across the middle of the sheet
    crease_dist = np.abs((xs * 0.85 + ys * 0.52) - 0.68)
    crease = 1.0 - 0.24 * np.exp(-((crease_dist / 0.018) ** 2))
    factor = (lighting * crease)[:, :, None]
    out = np.clip(rgb.astype(np.float32) * factor, 0, 255).astype(np.uint8)
    return out


def _warp_sheet_onto_desk(
    sheet_rgb: np.ndarray,
    *,
    canvas_size: tuple[int, int] = (3200, 2400),
    dst_quad: list[tuple[float, float]] | None = None,
    desk_gray: int = 62,
) -> tuple[np.ndarray, np.ndarray, list[tuple[float, float]]]:
    """Perspective-warp a rendered schematic sheet onto a darker desk/panel-door background."""
    sh, sw = sheet_rgb.shape[:2]
    cw, ch = canvas_size
    if dst_quad is None:
        dst_quad = [
            (cw * 0.09, ch * 0.11),
            (cw * 0.90, ch * 0.06),
            (cw * 0.94, ch * 0.91),
            (cw * 0.06, ch * 0.87),
        ]
    src_pts = np.array([[0, 0], [sw - 1, 0], [sw - 1, sh - 1], [0, sh - 1]], dtype=np.float32)
    dst_pts = np.array(dst_quad, dtype=np.float32)
    H = cv2.getPerspectiveTransform(src_pts, dst_pts)

    desk = np.full((ch, cw, 3), desk_gray, dtype=np.uint8)
    warped_sheet = cv2.warpPerspective(
        sheet_rgb,
        H,
        (cw, ch),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(desk_gray, desk_gray, desk_gray),
    )
    mask = np.full((sh, sw), 255, dtype=np.uint8)
    warped_mask = cv2.warpPerspective(mask, H, (cw, ch), flags=cv2.INTER_NEAREST)
    canvas = np.where(warped_mask[:, :, None] > 127, warped_sheet, desk)
    return canvas, H, dst_quad


def test_prepared_scan_tuple_and_object_interface():
    """PreparedScan works both as a 2-tuple (clean, meta) and as a rich object with homography mapping."""
    img = Image.new("RGB", (400, 300), (255, 255, 255))
    H = [[1.0, 0.0, 10.0], [0.0, 1.0, -5.0], [0.0, 0.0, 1.0]]
    res = PreparedScan(
        image=img,
        quarter_turns=1,
        deskew_deg=1.25,
        rectified=True,
        perspective_quad=[(10.0, 10.0), (390.0, 12.0), (388.0, 290.0), (12.0, 288.0)],
        homography=H,
        illumination_normalized=True,
        effective_dpi=(300.0, 300.0),
        source_dpi=(72.0, 72.0),
    )
    clean, meta = res
    assert clean is img
    assert meta["quarter_turns"] == 1
    assert meta["deskew_deg"] == 1.25
    assert meta["rectified"] is True
    assert meta["illumination_normalized"] is True
    assert tuple(meta["effective_dpi"]) == (300.0, 300.0)
    assert tuple(meta["source_dpi"]) == (72.0, 72.0)

    mx, my = res.map_point(25.0, 40.0)
    assert abs(mx - 35.0) < 1e-5
    assert abs(my - 35.0) < 1e-5
    ux, uy = res.unmap_point(mx, my)
    assert abs(ux - 25.0) < 1e-5
    assert abs(uy - 40.0) < 1e-5


def test_four_corner_perspective_rectification_and_fallback():
    """Keystoned sheet on a desk is rectified via 4-corner homography; clean scan falls back without warping."""
    # 1. Clean scan should NOT trigger perspective rectification
    clean_sheet = Image.new("RGB", (1200, 850), (255, 255, 255))
    draw = ImageDraw.Draw(clean_sheet)
    draw.rectangle((40, 40, 1160, 810), outline=(0, 0, 0), width=4)
    draw.line((120, 120, 1080, 120), fill=(0, 0, 0), width=3)
    draw.line((120, 720, 1080, 720), fill=(0, 0, 0), width=3)

    rect_img, quad, H, was_rect = rectify_perspective(clean_sheet)
    assert was_rect is False
    assert quad is None
    assert H is None
    assert rect_img.size == clean_sheet.size

    # 2. Keystoned sheet on a desk background SHOULD trigger 4-corner perspective rectification
    sheet_np = np.asarray(clean_sheet)
    warped_np, _H_true, expected_quad = _warp_sheet_onto_desk(
        sheet_np,
        canvas_size=(1600, 1200),
        dst_quad=[(150.0, 130.0), (1440.0, 75.0), (1500.0, 1090.0), (95.0, 1040.0)],
    )
    warped_pil = Image.fromarray(warped_np)
    detected_quad = detect_sheet_quad(warped_pil)
    assert detected_quad is not None
    for (dx, dy), (ex, ey) in zip(detected_quad, expected_quad):
        assert np.hypot(dx - ex, dy - ey) < 25.0

    rect2, quad2, H2, was_rect2 = rectify_perspective(warped_pil)
    assert was_rect2 is True
    assert quad2 is not None
    assert H2 is not None
    # Rectified sheet should be overwhelmingly white paper, with desk surround removed
    rect_gray = np.asarray(rect2.convert("L"))
    assert float(np.mean(rect_gray > 200)) > 0.90


def test_border_frame_perspective_rectification_on_white_background():
    """A keystoned outer CAD border frame on a white background is detected and rectified."""
    w, h = 1600, 1100
    img = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    quad = [(110, 95), (1490, 50), (1530, 1015), (75, 970)]
    draw.polygon(quad, outline=(0, 0, 0), width=5)
    # Inner schematic lines warped similarly
    draw.line((180, 220, 1420, 185), fill=(0, 0, 0), width=3)
    draw.line((170, 840, 1450, 875), fill=(0, 0, 0), width=3)

    rect_img, det_quad, H, was_rect = rectify_perspective(img)
    assert was_rect is True
    assert det_quad is not None
    assert H is not None
    for (dx, dy), (ex, ey) in zip(det_quad, quad):
        assert np.hypot(dx - ex, dy - ey) < 25.0


def test_illumination_shadow_and_crease_normalization():
    """Morphological background division + CLAHE removes false shadow ink while preserving schematic strokes."""
    w, h = 1200, 850
    clean = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(clean)
    draw.rectangle((40, 40, w - 40, h - 40), outline=(0, 0, 0), width=3)
    for y in range(120, h - 120, 80):
        draw.line((80, y, w - 80, y), fill=(0, 0, 0), width=2)
    for x in range(160, w - 160, 160):
        draw.line((x, 80, x, h - 80), fill=(0, 0, 0), width=2)

    clean_gray = np.asarray(clean.convert("L"))
    clean_ink_ratio = float(np.mean((clean_gray < 180)))

    shadowed_rgb = _apply_shadow_and_crease(np.asarray(clean))
    shadowed_gray = cv2.cvtColor(shadowed_rgb, cv2.COLOR_RGB2GRAY)

    # Naive fixed threshold (< 180) turns the shadowed corner & crease into massive false ink (>25% of page)
    naive_ink_ratio = float(np.mean(shadowed_gray < 180))
    assert naive_ink_ratio > 0.25

    # Illumination normalization restores clean white paper background
    norm_gray, applied = normalize_illumination(shadowed_gray)
    assert applied is True
    norm_ink_ratio = float(np.mean(norm_gray < 180))
    assert abs(norm_ink_ratio - clean_ink_ratio) < 0.015

    # And raster._ink(shadowed_gray) automatically normalizes so line detection sees no shadow blob
    raster_ink = _ink(shadowed_gray)
    assert abs(float(np.mean(raster_ink > 0)) - clean_ink_ratio) < 0.015

    # True Schematic line recovery check: horizontal Hough lines before vs after
    naive_bw = (cv2.medianBlur(shadowed_gray, 3) < 180).astype(np.uint8) * 255
    norm_bw = (cv2.medianBlur(norm_gray, 3) < 180).astype(np.uint8) * 255
    # In the shadowed bottom-right quadrant, naive_bw is almost solid black, destroying line contrast
    br_naive_density = float(np.mean(naive_bw[h // 2 :, w // 2 :] > 0))
    br_norm_density = float(np.mean(norm_bw[h // 2 :, w // 2 :] > 0))
    assert br_naive_density > 0.50
    assert br_norm_density < 0.05


def test_phone_camera_72_and_96_dpi_normalization(tmp_path: Path):
    """High-resolution phone photos with 72/96 DPI tags normalize to engineering sheet dimensions."""
    # 4000x3000 phone photo tagged with 72 DPI (previously computed a huge 1411.1 x 1058.3 mm sheet!)
    assert is_default_camera_dpi((72.0, 72.0), 4000, 3000) is True
    assert is_default_camera_dpi((96.0, 96.0), 3200, 2400) is True
    assert is_default_camera_dpi((None, None), 4000, 3000) is True
    # Legitimate scan/render DPIs or small images are NOT treated as camera default DPI
    assert is_default_camera_dpi((150.0, 150.0), 2480, 1754) is False
    assert is_default_camera_dpi((200.0, 200.0), 3300, 2200) is False
    assert is_default_camera_dpi((300.0, 300.0), 4960, 3508) is False
    assert is_default_camera_dpi((72.0, 72.0), 792, 612) is False

    eff_dpi, src_dpi, changed = normalize_camera_dpi(4000, 3000, (72.0, 72.0))
    assert changed is True
    assert 240.0 <= eff_dpi[0] <= 370.0
    assert 240.0 <= eff_dpi[1] <= 370.0

    # Verify resolve_sheet_size_mm on 4000x3000 and 2200x1700 at 72 DPI and 96 DPI
    for px_size in ((4000, 3000), (2200, 1700), (3300, 2550)):
        for bad_dpi in (72.0, 96.0):
            img = Image.new("RGB", px_size, (255, 255, 255))
            p = tmp_path / f"photo_{px_size[0]}x{px_size[1]}_{int(bad_dpi)}.jpg"
            img.save(p, dpi=(bad_dpi, bad_dpi))
            with Image.open(p) as loaded:
                (w_mm, h_mm), _note = resolve_sheet_size_mm(loaded)
                assert 210.0 <= w_mm <= 440.0, f"Unexpected width_mm={w_mm} for {px_size}@{bad_dpi}"
                assert 210.0 <= h_mm <= 440.0, f"Unexpected height_mm={h_mm} for {px_size}@{bad_dpi}"

    # Verify EXIF XResolution/YResolution = 72 without image.info["dpi"]
    exif_img = Image.new("RGB", (3200, 2400), (255, 255, 255))
    exif = exif_img.getexif()
    exif[282] = 72  # XResolution
    exif[283] = 72  # YResolution
    exif[296] = 2   # inches
    exif_path = tmp_path / "exif_72.jpg"
    exif_img.save(exif_path, exif=exif.tobytes())
    with Image.open(exif_path) as loaded:
        (w_mm, h_mm), _note = resolve_sheet_size_mm(loaded)
        assert (w_mm, h_mm) == (279.4, 215.9)

    # Verify 150/200/300 DPI test renders preserve exact DPI-derived dimensions
    render_img = Image.new("RGB", (3300, 2550), (255, 255, 255))
    render_path = tmp_path / "render_300.png"
    render_img.save(render_path, dpi=(300, 300))
    with Image.open(render_path) as loaded:
        (w_mm, h_mm), _note = resolve_sheet_size_mm(loaded)
        assert abs(w_mm - 279.4) < 0.2
        assert abs(h_mm - 215.9) < 0.2


def test_synthetic_perspective_shadowed_72dpi_schematic_photo_recovery(tmp_path: Path):
    """End-to-end benchmark on a synthetic perspective-warped + shadowed + 72-DPI photo of DOL starter."""
    session = build_dol()
    pdf_path = tmp_path / "dol.pdf"
    session.export_pdf(pdf_path)
    rendered = rasterize_pdf(pdf_path, tmp_path / "rasters", dpi=200)
    clean_pil = Image.open(rendered[0]).convert("RGB")
    clean_rgb = np.asarray(clean_pil)

    # Apply uneven camera lighting + fold crease, then warp onto a dark desk at 3200x2200 and save with 72 DPI
    lit_rgb = _apply_shadow_and_crease(clean_rgb)
    photo_rgb, _H, _quad = _warp_sheet_onto_desk(
        lit_rgb,
        canvas_size=(3200, 2200),
        dst_quad=[(240.0, 190.0), (2960.0, 110.0), (3060.0, 2040.0), (140.0, 1940.0)],
        desk_gray=55,
    )
    photo_path = tmp_path / "dol_phone_photo_72dpi.jpg"
    Image.fromarray(photo_rgb).save(photo_path, quality=88, dpi=(72, 72))

    # --- BEFORE Patch 2 (raw unrectified image at naive 72 DPI = 1128.9 x 776.1 mm and naive threshold) ---
    raw_pil = Image.open(photo_path)
    naive_w_mm = round(raw_pil.width * 25.4 / 72.0, 2)
    naive_h_mm = round(raw_pil.height * 25.4 / 72.0, 2)
    assert naive_w_mm > 1000.0 and naive_h_mm > 700.0
    raw_gray = cv2.cvtColor(np.asarray(raw_pil.convert("RGB")), cv2.COLOR_RGB2GRAY)
    naive_binary = (cv2.medianBlur(raw_gray, 3) < 180).astype(np.uint8)
    before_insets = detect_frame_insets(naive_binary, naive_w_mm, naive_h_mm)
    before_facts = read_pixels(
        raw_pil,
        naive_w_mm,
        naive_h_mm,
        default_catalog(),
        allow_missing_ocr=True,
    )

    # --- AFTER Patch 2 (prepare_scan + resolve_sheet_size_mm via _image_page) ---
    ws = Workspace(tmp_path / "ws")
    page_manifest = _ingest_image(photo_path, ws, index=0, dpi=None)
    after_w_mm = float(page_manifest["width_mm"])
    after_h_mm = float(page_manifest["height_mm"])
    assert 270.0 <= after_w_mm <= 440.0
    assert 210.0 <= min(after_w_mm, after_h_mm) <= 305.0 and 270.0 <= max(after_w_mm, after_h_mm) <= 440.0
    assert page_manifest["rectified"] is True
    assert page_manifest["homography"] is not None
    assert page_manifest["perspective_quad"] is not None

    prepared_pil = Image.open(page_manifest["image"])
    prep_gray = cv2.cvtColor(np.asarray(prepared_pil.convert("RGB")), cv2.COLOR_RGB2GRAY)
    after_binary = _ink(prep_gray)
    after_insets = detect_frame_insets(after_binary, after_w_mm, after_h_mm)

    # Frame insets fail on the keystoned+shadowed raw photo (or are wildly wrong),
    # and succeed cleanly on the rectified + illumination-normalized scan!
    assert before_insets is None
    assert after_insets is not None

    # Compare horizontal & vertical schematic edge alignment (axis-aligned within 1.2 deg)
    def _count_axis_aligned_lines(bin_mask: np.ndarray) -> tuple[int, int]:
        edges = cv2.Canny((bin_mask > 0).astype(np.uint8) * 255, 50, 150)
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180.0, 100, minLineLength=250, maxLineGap=6)
        if lines is None:
            return 0, 0
        horiz = 0
        vert = 0
        for x1, y1, x2, y2 in lines.reshape(-1, 4):
            ang = abs(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
            if ang <= 1.2 or abs(ang - 180.0) <= 1.2:
                horiz += 1
            elif abs(ang - 90.0) <= 1.2:
                vert += 1
        return horiz, vert

    before_h, before_v = _count_axis_aligned_lines(naive_binary)
    after_h, after_v = _count_axis_aligned_lines(after_binary)
    assert before_h == 0 and before_v == 0
    assert after_h >= 12
    assert after_v >= 10
    assert isinstance(before_facts, dict)
