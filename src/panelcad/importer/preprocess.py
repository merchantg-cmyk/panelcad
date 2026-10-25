from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


_STANDARD_SHEETS_MM: tuple[tuple[str, float, float], ...] = (
    ("ANSI A", 279.4, 215.9),
    ("ANSI B", 431.8, 279.4),
    ("A3", 420.0, 297.0),
    ("A4", 297.0, 210.0),
)


@dataclass
class PreparedScan:
    image: Image.Image
    quarter_turns: int = 0
    deskew_deg: float = 0.0
    rectified: bool = False
    perspective_quad: list[tuple[float, float]] | None = None
    homography: list[list[float]] | None = None
    illumination_normalized: bool = False
    effective_dpi: tuple[float, float] | None = None
    source_dpi: tuple[float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "quarter_turns": int(self.quarter_turns),
            "deskew_deg": round(float(self.deskew_deg), 2),
            "rectified": bool(self.rectified),
            "illumination_normalized": bool(self.illumination_normalized),
        }
        if self.perspective_quad is not None:
            data["perspective_quad"] = [
                [round(float(x), 2), round(float(y), 2)] for x, y in self.perspective_quad
            ]
        if self.homography is not None:
            data["homography"] = [
                [round(float(v), 6) for v in row] for row in self.homography
            ]
        if self.effective_dpi is not None:
            data["effective_dpi"] = [
                round(float(self.effective_dpi[0]), 2),
                round(float(self.effective_dpi[1]), 2),
            ]
        if self.source_dpi is not None:
            data["source_dpi"] = [
                round(float(self.source_dpi[0]), 2),
                round(float(self.source_dpi[1]), 2),
            ]
        return data

    def __iter__(self):
        yield self.image
        yield self.to_dict()

    def __getitem__(self, index: int) -> Any:
        return (self.image, self.to_dict())[index]

    def map_point(self, x: float, y: float) -> tuple[float, float]:
        if not self.homography:
            return float(x), float(y)
        H = np.asarray(self.homography, dtype=np.float64)
        pt = np.array([[[float(x), float(y)]]], dtype=np.float64)
        mapped = cv2.perspectiveTransform(pt, H)[0, 0]
        return float(mapped[0]), float(mapped[1])

    def unmap_point(self, x: float, y: float) -> tuple[float, float]:
        if not self.homography:
            return float(x), float(y)
        H = np.asarray(self.homography, dtype=np.float64)
        inv = np.linalg.inv(H)
        pt = np.array([[[float(x), float(y)]]], dtype=np.float64)
        mapped = cv2.perspectiveTransform(pt, inv)[0, 0]
        return float(mapped[0]), float(mapped[1])


def order_quad_corners(pts: np.ndarray) -> np.ndarray:
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    ordered = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).reshape(4)
    ordered[0] = pts[np.argmin(s)]     # top-left
    ordered[2] = pts[np.argmax(s)]     # bottom-right
    ordered[1] = pts[np.argmin(diff)]  # top-right
    ordered[3] = pts[np.argmax(diff)]  # bottom-left
    return ordered


def _quad_metrics(quad: np.ndarray, width: int, height: int) -> dict[str, float] | None:
    pts = order_quad_corners(quad)
    area = float(cv2.contourArea(pts))
    img_area = float(max(1, width * height))
    area_ratio = area / img_area
    if area_ratio < 0.22 or area_ratio > 0.985:
        return None
    if not cv2.isContourConvex(pts.astype(np.int32)):
        return None

    top_vec = pts[1] - pts[0]
    right_vec = pts[2] - pts[1]
    bottom_vec = pts[2] - pts[3]
    left_vec = pts[3] - pts[0]

    top_len = float(np.linalg.norm(top_vec))
    bottom_len = float(np.linalg.norm(bottom_vec))
    left_len = float(np.linalg.norm(left_vec))
    right_len = float(np.linalg.norm(right_vec))
    if min(top_len, bottom_len, left_len, right_len) < min(width, height) * 0.25:
        return None

    # Interior angles must be reasonably close to a rectangle (50..130 deg)
    edges = [top_vec, right_vec, pts[3] - pts[2], -left_vec]
    for i in range(4):
        u = -edges[(i - 1) % 4]
        v = edges[i]
        denom = float(np.linalg.norm(u) * np.linalg.norm(v))
        if denom <= 1e-6:
            return None
        cos_a = float(np.clip(np.dot(u, v) / denom, -1.0, 1.0))
        ang = math.degrees(math.acos(cos_a))
        if ang < 50.0 or ang > 130.0:
            return None

    top_ang = math.degrees(math.atan2(float(top_vec[1]), float(top_vec[0])))
    bottom_ang = math.degrees(math.atan2(float(bottom_vec[1]), float(bottom_vec[0])))
    left_ang = math.degrees(math.atan2(float(left_vec[0]), float(left_vec[1])))
    right_ang = math.degrees(math.atan2(float(right_vec[0]), float(right_vec[1])))

    horiz_convergence = abs(top_ang - bottom_ang)
    vert_convergence = abs(left_ang - right_ang)
    max_tilt = max(abs(top_ang), abs(bottom_ang), abs(left_ang), abs(right_ang))
    keystone = max(
        abs(top_len - bottom_len) / max(top_len, bottom_len, 1.0),
        abs(left_len - right_len) / max(left_len, right_len, 1.0),
    )

    margin = max(3.0, min(width, height) * 0.004)
    if (
        (pts[:, 0] < -margin).any()
        or (pts[:, 1] < -margin).any()
        or (pts[:, 0] > width + margin).any()
        or (pts[:, 1] > height + margin).any()
    ):
        return None

    return {
        "area_ratio": area_ratio,
        "keystone": keystone,
        "convergence_deg": max(horiz_convergence, vert_convergence),
        "max_tilt_deg": max_tilt,
        "top_len": top_len,
        "bottom_len": bottom_len,
        "left_len": left_len,
        "right_len": right_len,
    }


def _approx_quad_from_contour(cnt: np.ndarray) -> np.ndarray | None:
    hull = cv2.convexHull(cnt)
    peri = cv2.arcLength(hull, True)
    if peri <= 0:
        return None
    for eps_scale in (0.015, 0.02, 0.03, 0.04, 0.05, 0.06):
        approx = cv2.approxPolyDP(hull, eps_scale * peri, True)
        if len(approx) == 4:
            return order_quad_corners(approx.reshape(4, 2))
    return None


def _line_intersection(
    line1: tuple[float, float, float, float],
    line2: tuple[float, float, float, float],
) -> tuple[float, float] | None:
    x1, y1, x2, y2 = line1
    x3, y3, x4, y4 = line2
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-6:
        return None
    px = ((x1 * y2 - y1 * x2) * (x3 - x4) - (x1 - x2) * (x3 * y4 - y3 * x4)) / denom
    py = ((x1 * y2 - y1 * x2) * (y3 - y4) - (y1 - y2) * (x3 * y4 - y3 * x4)) / denom
    return float(px), float(py)


def _detect_paper_sheet_quad(gray: np.ndarray) -> tuple[np.ndarray, dict[str, float]] | None:
    h, w = gray.shape[:2]
    border_strip = np.concatenate(
        [gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]]
    )
    # A paper sheet photographed on a desk/floor/panel door has non-white border pixels
    if float(np.percentile(border_strip, 25)) > 235.0:
        return None

    scale = min(1.0, 1000.0 / float(max(h, w)))
    small = (
        cv2.resize(gray, (int(round(w * scale)), int(round(h * scale))), interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else gray
    )
    sh, sw = small.shape[:2]
    blur = cv2.GaussianBlur(small, (7, 7), 0)

    candidates: list[tuple[float, np.ndarray, dict[str, float]]] = []
    otsu_val, _ = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thresholds = sorted({int(otsu_val), int(np.percentile(blur, 35)), int(np.percentile(blur, 45))})
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))

    for thr in thresholds:
        if thr < 60 or thr > 235:
            continue
        mask = (blur >= thr).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in sorted(cnts, key=cv2.contourArea, reverse=True)[:3]:
            quad_small = _approx_quad_from_contour(cnt)
            if quad_small is None:
                continue
            quad = quad_small / float(scale)
            metrics = _quad_metrics(quad, w, h)
            if metrics is None:
                continue
            # Require either perspective keystone/convergence or a clear sheet tilt on background
            if (
                metrics["keystone"] < 0.012
                and metrics["convergence_deg"] < 0.75
                and metrics["max_tilt_deg"] < 0.6
                and metrics["area_ratio"] > 0.94
            ):
                continue
            score = metrics["area_ratio"] * (1.0 + min(0.5, metrics["keystone"] * 4.0 + metrics["convergence_deg"] * 0.05))
            candidates.append((score, quad, metrics))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, best_quad, best_metrics = candidates[0]
    return best_quad, best_metrics


def _detect_border_frame_quad(gray: np.ndarray) -> tuple[np.ndarray, dict[str, float]] | None:
    h, w = gray.shape[:2]
    scale = min(1.0, 1200.0 / float(max(h, w)))
    small = (
        cv2.resize(gray, (int(round(w * scale)), int(round(h * scale))), interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else gray
    )
    sh, sw = small.shape[:2]
    norm_small, _ = normalize_illumination(small, force=True)
    inv = cv2.adaptiveThreshold(
        norm_small,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        31,
        10,
    )
    close_k = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    closed = cv2.morphologyEx(inv, cv2.MORPH_CLOSE, close_k)

    # 1. Try contour approximation of the outer title-block/sheet frame
    cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for cnt in sorted(cnts, key=cv2.contourArea, reverse=True)[:5]:
        quad_small = _approx_quad_from_contour(cnt)
        if quad_small is None:
            continue
        quad = quad_small / float(scale)
        metrics = _quad_metrics(quad, w, h)
        if metrics is None or metrics["area_ratio"] < 0.35 or metrics["area_ratio"] > 0.96:
            continue
        # Only perspective-warp a frame when it actually has keystone or non-parallel edges
        # (pure 2D rotations on white background have parallel opposite edges and are handled by deskew).
        if metrics["keystone"] >= 0.015 or metrics["convergence_deg"] >= 0.85:
            return quad, metrics

    # 2. Fallback to outer extremal Hough lines (top, bottom, left, right border segments)
    lines = cv2.HoughLinesP(
        closed,
        1.0,
        np.pi / 180.0,
        threshold=max(60, int(min(sw, sh) * 0.18)),
        minLineLength=max(60, int(min(sw, sh) * 0.22)),
        maxLineGap=max(12, int(min(sw, sh) * 0.03)),
    )
    if lines is None:
        return None

    top_lines: list[tuple[float, tuple[float, float, float, float]]] = []
    bottom_lines: list[tuple[float, tuple[float, float, float, float]]] = []
    left_lines: list[tuple[float, tuple[float, float, float, float]]] = []
    right_lines: list[tuple[float, tuple[float, float, float, float]]] = []

    for raw in lines.reshape(-1, 4):
        x1, y1, x2, y2 = [float(v) / float(scale) for v in raw]
        length = math.hypot(x2 - x1, y2 - y1)
        ang = math.degrees(math.atan2(y2 - y1, x2 - x1))
        if abs(ang) <= 25.0 or abs(abs(ang) - 180.0) <= 25.0:
            ymid = 0.5 * (y1 + y2)
            if ymid < h * 0.32:
                top_lines.append((length - ymid * 0.5, (x1, y1, x2, y2)))
            elif ymid > h * 0.68:
                bottom_lines.append((length + ymid * 0.5, (x1, y1, x2, y2)))
        elif 65.0 <= abs(ang) <= 115.0:
            xmid = 0.5 * (x1 + x2)
            if xmid < w * 0.32:
                left_lines.append((length - xmid * 0.5, (x1, y1, x2, y2)))
            elif xmid > w * 0.68:
                right_lines.append((length + xmid * 0.5, (x1, y1, x2, y2)))

    if not (top_lines and bottom_lines and left_lines and right_lines):
        return None

    top_l = max(top_lines, key=lambda item: item[0])[1]
    bottom_l = max(bottom_lines, key=lambda item: item[0])[1]
    left_l = max(left_lines, key=lambda item: item[0])[1]
    right_l = max(right_lines, key=lambda item: item[0])[1]

    tl = _line_intersection(top_l, left_l)
    tr = _line_intersection(top_l, right_l)
    br = _line_intersection(bottom_l, right_l)
    bl = _line_intersection(bottom_l, left_l)
    if None in (tl, tr, br, bl):
        return None

    quad = order_quad_corners(np.array([tl, tr, br, bl], dtype=np.float32))
    metrics = _quad_metrics(quad, w, h)
    if metrics is None or metrics["area_ratio"] < 0.35 or metrics["area_ratio"] > 0.96:
        return None
    if metrics["keystone"] >= 0.015 or metrics["convergence_deg"] >= 0.85:
        return quad, metrics
    return None


def detect_sheet_quad(image_or_gray: Image.Image | np.ndarray) -> np.ndarray | None:
    if isinstance(image_or_gray, Image.Image):
        rgb = np.asarray(image_or_gray.convert("RGB"), dtype=np.uint8)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    elif image_or_gray.ndim == 3:
        gray = cv2.cvtColor(image_or_gray, cv2.COLOR_RGB2GRAY)
    else:
        gray = image_or_gray

    paper = _detect_paper_sheet_quad(gray)
    if paper is not None:
        return paper[0]
    border = _detect_border_frame_quad(gray)
    if border is not None:
        return border[0]
    return None


def rectify_perspective(
    image: Image.Image,
    quad: np.ndarray | None = None,
) -> tuple[Image.Image, list[tuple[float, float]] | None, list[list[float]] | None, bool]:
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape[:2]

    if quad is None:
        quad = detect_sheet_quad(gray)
    if quad is None:
        return image, None, None, False

    src_pts = order_quad_corners(quad)
    metrics = _quad_metrics(src_pts, w, h)
    if metrics is None:
        return image, None, None, False

    dst_w = int(round(max(metrics["top_len"], metrics["bottom_len"])))
    dst_h = int(round(max(metrics["left_len"], metrics["right_len"])))
    if dst_w < 64 or dst_h < 64:
        return image, None, None, False

    dst_pts = np.array(
        [
            [0.0, 0.0],
            [float(dst_w - 1), 0.0],
            [float(dst_w - 1), float(dst_h - 1)],
            [0.0, float(dst_h - 1)],
        ],
        dtype=np.float32,
    )
    H = cv2.getPerspectiveTransform(src_pts, dst_pts)
    warped = cv2.warpPerspective(
        rgb,
        H,
        (dst_w, dst_h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    quad_list = [(float(pt[0]), float(pt[1])) for pt in src_pts]
    h_list = [[float(v) for v in row] for row in H]
    return Image.fromarray(warped), quad_list, h_list, True


def normalize_illumination(
    gray_or_image: np.ndarray | Image.Image,
    *,
    force: bool = False,
) -> tuple[np.ndarray, bool]:
    if isinstance(gray_or_image, Image.Image):
        rgb = np.asarray(gray_or_image.convert("RGB"), dtype=np.uint8)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    elif gray_or_image.ndim == 3:
        gray = cv2.cvtColor(gray_or_image, cv2.COLOR_RGB2GRAY)
    else:
        gray = gray_or_image

    h, w = gray.shape[:2]
    ksize = max(31, (min(h, w) // 16) | 1)
    if ksize % 2 == 0:
        ksize += 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize, ksize))
    bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
    bg = cv2.GaussianBlur(bg, (0, 0), sigmaX=max(8.0, ksize * 0.25))

    p98 = float(np.percentile(bg, 98))
    p5 = float(np.percentile(bg, 5))
    bg_spread = p98 - p5
    if not force and bg_spread < 12.0 and p5 >= 238.0:
        return gray, False

    bg_safe = np.maximum(bg, 32)
    divided = cv2.divide(gray, bg_safe, scale=255)
    clahe = cv2.createCLAHE(clipLimit=1.8, tileGridSize=(8, 8))
    enhanced = clahe.apply(divided)
    # Blend divided and CLAHE so crisp background stays pure white while faint lines under shadows pop
    blended = cv2.addWeighted(divided, 0.65, enhanced, 0.35, 0)
    blended[divided >= 234] = 255
    return blended, True


def is_default_camera_dpi(
    dpi: tuple[float, float] | None,
    width_px: int,
    height_px: int,
    *,
    min_long_edge_px: int = 1600,
) -> bool:
    if max(int(width_px), int(height_px)) < min_long_edge_px:
        return False
    if dpi is None or dpi[0] is None or dpi[1] is None:
        return True
    xdpi, ydpi = float(dpi[0]), float(dpi[1])
    if xdpi <= 0 or ydpi <= 0:
        return True
    return xdpi <= 96.5 and ydpi <= 96.5


def normalize_camera_dpi(
    width_px: int,
    height_px: int,
    dpi: tuple[float, float] | None,
    *,
    sheet_mm: tuple[float, float] | None = None,
    min_long_edge_px: int = 1600,
) -> tuple[tuple[float, float] | None, tuple[float, float] | None, bool]:
    source_dpi = (
        (round(float(dpi[0]), 2), round(float(dpi[1]), 2))
        if dpi and dpi[0] > 0 and dpi[1] > 0
        else None
    )
    if not is_default_camera_dpi(source_dpi, width_px, height_px, min_long_edge_px=min_long_edge_px):
        return source_dpi, source_dpi, False

    if sheet_mm is None:
        portrait = height_px > width_px
        long_px = max(width_px, height_px)
        short_px = min(width_px, height_px)
        ratio = long_px / max(short_px, 1)
        _, best_long, best_short = min(
            _STANDARD_SHEETS_MM,
            key=lambda item: abs((item[1] / item[2]) - ratio),
        )
        sheet_mm = (best_short, best_long) if portrait else (best_long, best_short)

    eff_x = round(float(width_px) * 25.4 / max(float(sheet_mm[0]), 1e-6), 2)
    eff_y = round(float(height_px) * 25.4 / max(float(sheet_mm[1]), 1e-6), 2)
    return (eff_x, eff_y), source_dpi, True


def fallback_text_score(gray: np.ndarray) -> float:
    """Estimate horizontal readable text density without Tesseract.

    Horizontal text lines form horizontal chains of character-sized connected
    components, whereas a 90-degree rotated page turns those text lines into
    vertical chains. Combining horizontal glyph-chain ratio with bottom-right
    title-block density cleanly separates 0/90/180/270 orientations.
    """
    h, w = gray.shape[:2]
    scale = min(1.0, 1200.0 / float(max(h, w)))
    small = (
        cv2.resize(gray, (int(round(w * scale)), int(round(h * scale))), interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else gray
    )
    sh, sw = small.shape[:2]
    norm, _ = normalize_illumination(small)
    ink = (norm < 185).astype(np.uint8)

    y0, y1 = int(sh * 0.08), int(sh * 0.92)
    x0, x1 = int(sw * 0.08), int(sw * 0.92)
    roi = ink[y0:y1, x0:x1]
    num, _, stats, _ = cv2.connectedComponentsWithStats(roi)
    glyph_mask = np.zeros_like(roi)
    glyph_count = 0
    for i in range(1, num):
        cw = int(stats[i, cv2.CC_STAT_WIDTH])
        ch = int(stats[i, cv2.CC_STAT_HEIGHT])
        area = int(stats[i, cv2.CC_STAT_AREA])
        if 2 <= cw <= 26 and 4 <= ch <= 32 and 5 <= area <= 360:
            gx = int(stats[i, cv2.CC_STAT_LEFT])
            gy = int(stats[i, cv2.CC_STAT_TOP])
            glyph_mask[gy : gy + ch, gx : gx + cw] = 255
            glyph_count += 1

    if glyph_count == 0:
        return 0.0

    h_close = cv2.morphologyEx(
        glyph_mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (13, 2)),
    )
    v_close = cv2.morphologyEx(
        glyph_mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (2, 13)),
    )

    h_num, _, h_stats, _ = cv2.connectedComponentsWithStats(h_close)
    h_chains = sum(
        1
        for i in range(1, h_num)
        if h_stats[i, cv2.CC_STAT_WIDTH] >= 2.2 * max(1, h_stats[i, cv2.CC_STAT_HEIGHT])
        and 6 <= h_stats[i, cv2.CC_STAT_HEIGHT] <= 34
        and h_stats[i, cv2.CC_STAT_WIDTH] >= 18
    )

    v_num, _, v_stats, _ = cv2.connectedComponentsWithStats(v_close)
    v_chains = sum(
        1
        for i in range(1, v_num)
        if v_stats[i, cv2.CC_STAT_HEIGHT] >= 2.2 * max(1, v_stats[i, cv2.CC_STAT_WIDTH])
        and 6 <= v_stats[i, cv2.CC_STAT_WIDTH] <= 34
        and v_stats[i, cv2.CC_STAT_HEIGHT] >= 18
    )

    chain_ratio = float(h_chains + 1) / float(v_chains + 1)
    bottom_half = float(ink[sh // 2 :, :].mean()) + 1e-6
    top_half = float(ink[: sh // 2, :].mean()) + 1e-6
    right_half = float(ink[:, sw // 2 :].mean()) + 1e-6
    left_half = float(ink[:, : sw // 2].mean()) + 1e-6
    tb_bias = 1.0 + 0.15 * min(1.5, bottom_half / top_half) + 0.10 * min(1.5, right_half / left_half)
    return float(h_chains) * chain_ratio * tb_bias


def _find_font_path() -> str | None:
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "C:/Windows/Fonts/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    )
    for c in candidates:
        if Path(c).exists():
            return c
    return None


@lru_cache(maxsize=8)
def _char_templates(font_px: int) -> dict[str, np.ndarray]:
    font_path = _find_font_path()
    font = (
        ImageFont.truetype(font_path, size=max(8, int(font_px)))
        if font_path
        else ImageFont.load_default()
    )
    templates: dict[str, np.ndarray] = {}
    for ch in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ+-/.:":
        canvas = Image.new("L", (64, 64), 0)
        draw = ImageDraw.Draw(canvas)
        draw.text((8, 8), ch, fill=255, font=font)
        arr = np.asarray(canvas, dtype=np.uint8)
        ys, xs = np.where(arr > 48)
        if len(xs) == 0:
            continue
        crop = arr[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
        templates[ch] = (crop > 96).astype(np.float32)
    return templates


def _classify_glyph(crop_bin: np.ndarray, templates: dict[str, np.ndarray]) -> tuple[str, float]:
    gh, gw = crop_bin.shape[:2]
    aspect = gw / float(max(gh, 1))
    best_ch = ""
    best_score = -1.0
    for ch, tmpl in templates.items():
        th, tw = tmpl.shape[:2]
        t_aspect = tw / float(max(th, 1))
        if abs(aspect - t_aspect) > 0.48:
            continue
        resized = cv2.resize(crop_bin.astype(np.float32), (tw, th), interpolation=cv2.INTER_LINEAR)
        a = resized - resized.mean()
        b = tmpl - tmpl.mean()
        denom = float(np.linalg.norm(a) * np.linalg.norm(b))
        if denom <= 1e-6:
            continue
        score = float(np.sum(a * b) / denom)
        if score > best_score:
            best_score = score
            best_ch = ch
    return best_ch, best_score


def fallback_ocr_tokens(
    rgb: np.ndarray,
    width_mm: float,
    height_mm: float,
) -> list[dict[str, Any]]:
    """Pure-OpenCV glyph template OCR fallback when Tesseract is not installed."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY) if rgb.ndim == 3 else rgb
    norm, _ = normalize_illumination(gray)
    h, w = norm.shape[:2]
    ppm = w / max(float(width_mm), 1.0)
    if ppm < 3.0:
        return []

    font_px = max(10, int(round(3.15 * ppm)))
    templates_main = _char_templates(font_px)
    templates_small = _char_templates(max(9, int(round(2.6 * ppm))))
    if not templates_main:
        return []

    ink = (norm < 190).astype(np.uint8) * 255
    num, _, stats, _ = cv2.connectedComponentsWithStats(ink)
    min_h = max(6, int(round(1.6 * ppm)))
    max_h = max(18, int(round(4.6 * ppm)))
    min_w = max(2, int(round(0.35 * ppm)))
    max_w = max(16, int(round(4.2 * ppm)))

    glyphs: list[dict[str, Any]] = []
    for i in range(1, num):
        gx = int(stats[i, cv2.CC_STAT_LEFT])
        gy = int(stats[i, cv2.CC_STAT_TOP])
        gw = int(stats[i, cv2.CC_STAT_WIDTH])
        gh = int(stats[i, cv2.CC_STAT_HEIGHT])
        area = int(stats[i, cv2.CC_STAT_AREA])
        if not (min_w <= gw <= max_w and min_h <= gh <= max_h and area >= 5):
            continue
        crop = (norm[gy : gy + gh, gx : gx + gw] < 195).astype(np.uint8)
        ch1, sc1 = _classify_glyph(crop, templates_main)
        ch2, sc2 = _classify_glyph(crop, templates_small)
        ch, sc = (ch1, sc1) if sc1 >= sc2 else (ch2, sc2)
        if sc >= 0.56 and ch:
            glyphs.append(
                {
                    "x": gx,
                    "y": gy,
                    "w": gw,
                    "h": gh,
                    "ch": ch,
                    "score": sc,
                }
            )

    if not glyphs:
        return []

    glyphs.sort(key=lambda g: (g["y"] // max(4, min_h // 2), g["x"]))
    used = [False] * len(glyphs)
    words: list[list[dict[str, Any]]] = []
    for i, g in enumerate(glyphs):
        if used[i]:
            continue
        used[i] = True
        line = [g]
        for j in range(i + 1, len(glyphs)):
            if used[j]:
                continue
            cand = glyphs[j]
            last = line[-1]
            same_baseline = abs((cand["y"] + cand["h"] * 0.5) - (last["y"] + last["h"] * 0.5)) <= max(4.0, last["h"] * 0.45)
            similar_h = abs(cand["h"] - last["h"]) <= max(4, int(last["h"] * 0.45))
            gap = cand["x"] - (last["x"] + last["w"])
            if same_baseline and similar_h and -2 <= gap <= max(6, int(last["h"] * 0.65)):
                line.append(cand)
                used[j] = True
        words.append(line)

    tokens: list[dict[str, Any]] = []
    for word in words:
        word.sort(key=lambda g: g["x"])
        text = "".join(g["ch"] for g in word).strip(".:/")
        if not text:
            continue
        avg_score = sum(g["score"] for g in word) / len(word)
        if len(text) == 1 and not text.isdigit():
            continue
        if avg_score < 0.62:
            continue
        x0 = min(g["x"] for g in word)
        y0 = min(g["y"] for g in word)
        x1 = max(g["x"] + g["w"] for g in word)
        y1 = max(g["y"] + g["h"] for g in word)
        tokens.append(
            {
                "text": text,
                "x": round(x0 / ppm, 2),
                "y": round(y0 / ppm, 2),
                "w": round((x1 - x0) / ppm, 2),
                "h": round((y1 - y0) / ppm, 2),
                "confidence": round(min(0.95, max(0.55, avg_score)), 2),
            }
        )
    return tokens


def prepare_scan(
    image: Image.Image,
    *,
    rectify: bool = True,
    normalize_lighting: bool = False,
) -> PreparedScan:
    from .imageutil import auto_rotate, deskew

    raw_dpi = image.info.get("dpi")
    dpi_tuple: tuple[float, float] | None = None
    if isinstance(raw_dpi, tuple) and len(raw_dpi) == 2:
        try:
            dpi_tuple = (float(raw_dpi[0]), float(raw_dpi[1]))
        except (TypeError, ValueError):
            dpi_tuple = None

    turned, quarter_turns = auto_rotate(image)
    work_img = turned
    quad_list: list[tuple[float, float]] | None = None
    homography: list[list[float]] | None = None
    rectified = False

    if rectify:
        rect_img, quad_list, homography, rectified = rectify_perspective(turned)
        if rectified:
            work_img = rect_img

    # Always run fine Hough/row-variance deskew: either as the primary fallback when
    # no 4-corner perspective quad was present, or to remove any tiny residual tilt.
    fixed_img, skew_deg = deskew(work_img)

    illum_applied = False
    if normalize_lighting:
        rgb = np.asarray(fixed_img.convert("RGB"), dtype=np.uint8)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        norm_gray, illum_applied = normalize_illumination(gray)
        if illum_applied:
            fixed_img = Image.fromarray(cv2.cvtColor(norm_gray, cv2.COLOR_GRAY2RGB))

    eff_dpi, src_dpi, _ = normalize_camera_dpi(fixed_img.width, fixed_img.height, dpi_tuple)
    if eff_dpi is not None:
        fixed_img.info["dpi"] = eff_dpi

    return PreparedScan(
        image=fixed_img,
        quarter_turns=quarter_turns,
        deskew_deg=round(float(skew_deg), 2),
        rectified=rectified,
        perspective_quad=quad_list,
        homography=homography,
        illumination_normalized=illum_applied,
        effective_dpi=eff_dpi,
        source_dpi=src_dpi,
    )


@lru_cache(maxsize=16)
def _probe_char_templates(font_px: int, bold: bool = False) -> dict[str, np.ndarray]:
    if bold:
        candidates = (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "C:/Windows/Fonts/DejaVuSans-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        )
    else:
        candidates = (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "C:/Windows/Fonts/DejaVuSans.ttf",
            "C:/Windows/Fonts/arial.ttf",
        )
    font_path = next((c for c in candidates if Path(c).exists()), None)
    font = (
        ImageFont.truetype(font_path, size=max(10, int(font_px)))
        if font_path
        else ImageFont.load_default()
    )
    tmpls: dict[str, np.ndarray] = {}
    for ch in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        canvas = Image.new("L", (96, 96), 0)
        ImageDraw.Draw(canvas).text((8, 8), ch, fill=255, font=font)
        arr = np.asarray(canvas, dtype=np.uint8)
        ys, xs = np.where(arr > 48)
        if len(xs):
            tmpls[ch] = (arr[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1] > 96).astype(np.float32)
    return tmpls


def fallback_probe_text_score(probe: Image.Image, tag_re: Any, title_re: Any) -> float:
    gray = np.asarray(probe.convert("L"), dtype=np.uint8)
    ink = (gray < 190).astype(np.uint8) * 255
    num, _, stats, _ = cv2.connectedComponentsWithStats(ink)
    if num <= 1:
        return 0.0
    glyphs: list[tuple[int, int, int, int]] = []
    for i in range(1, num):
        gx = int(stats[i, cv2.CC_STAT_LEFT])
        gy = int(stats[i, cv2.CC_STAT_TOP])
        gw = int(stats[i, cv2.CC_STAT_WIDTH])
        gh = int(stats[i, cv2.CC_STAT_HEIGHT])
        area = int(stats[i, cv2.CC_STAT_AREA])
        if 3 <= gw <= 160 and 10 <= gh <= 180 and area >= 10 and gh < gray.shape[0] * 0.4 and gw < gray.shape[1] * 0.25:
            glyphs.append((gx, gy, gw, gh))
    if not glyphs or len(glyphs) > 600:
        return 0.0

    med_h = int(np.median([g[3] for g in glyphs]))
    t_reg = _probe_char_templates(max(16, min(72, med_h)), False)
    t_bold = _probe_char_templates(max(16, min(72, med_h)), True)
    decoded: list[dict[str, Any]] = []
    for gx, gy, gw, gh in glyphs:
        crop = (gray[gy : gy + gh, gx : gx + gw] < 195).astype(np.float32)
        best_ch, best_sc = "", -1.0
        for tmpls in (t_reg, t_bold):
            for ch, tmpl in tmpls.items():
                th, tw = tmpl.shape[:2]
                if abs((gw / max(gh, 1)) - (tw / max(th, 1))) > 0.20:
                    continue
                res = cv2.resize(crop, (tw, th), interpolation=cv2.INTER_LINEAR)
                a, b = res - res.mean(), tmpl - tmpl.mean()
                denom = float(np.linalg.norm(a) * np.linalg.norm(b))
                if denom > 1e-6:
                    sc = float(np.sum(a * b) / denom)
                    if sc > best_sc:
                        best_sc, best_ch = sc, ch
        if best_sc >= 0.78 and best_ch:
            decoded.append({"x": gx, "y": gy, "w": gw, "h": gh, "ch": best_ch, "sc": best_sc})
    if not decoded:
        return 0.0

    decoded.sort(key=lambda g: (g["y"] // max(6, med_h // 2), g["x"]))
    used = [False] * len(decoded)
    words: list[list[dict[str, Any]]] = []
    for i, g in enumerate(decoded):
        if used[i]:
            continue
        used[i] = True
        line = [g]
        for j in range(i + 1, len(decoded)):
            if used[j]:
                continue
            cand = decoded[j]
            last = line[-1]
            same_y = abs((cand["y"] + cand["h"] * 0.5) - (last["y"] + last["h"] * 0.5)) <= max(4.0, last["h"] * 0.35)
            sim_h = abs(cand["h"] - last["h"]) <= max(4, int(last["h"] * 0.35))
            gap = cand["x"] - (last["x"] + last["w"])
            if same_y and sim_h and -2 <= gap <= max(3, int(last["h"] * 0.26)):
                line.append(cand)
                used[j] = True
        words.append(line)

    total = 0.0
    for w in words:
        w.sort(key=lambda g: g["x"])
        txt = "".join(g["ch"] for g in w)
        avg_sc = sum(g["sc"] for g in w) / len(w)
        min_sc = min(g["sc"] for g in w)
        if (tag_re.match(txt) or title_re.match(txt)) and avg_sc >= 0.82 and min_sc >= 0.78:
            total += avg_sc * 100.0
    return total
