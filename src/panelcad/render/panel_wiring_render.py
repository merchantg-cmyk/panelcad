"""Painter rendering for backpanel wire routing, zip-tie waypoints,
Technician Wiring Walkthrough mode, and Wire-by-Wire Cut Schedule sheets.
Works across QtPainter (PNG/PDF/GUI) and SvgPainter (SVG).
"""

from __future__ import annotations

import math
from panelcad.core.models import Project, Sheet
from panelcad.core.panel_wiring import (
    PanelWireRoute,
    ZipTieWaypoint,
    build_wire_by_wire_schedule,
    get_panel_wiring_mode,
    get_walkthrough_step,
)
from panelcad.library.catalog import Catalog
from panelcad.render.titleblock import MARGIN, TITLE_H, ZONE

def usable_rect(sheet_or_page):
    page = getattr(sheet_or_page, 'page', sheet_or_page)
    pw, ph = page.dimensions()
    return (MARGIN + ZONE, MARGIN + ZONE, pw - 2 * MARGIN - 2 * ZONE, ph - 2 * MARGIN - ZONE - TITLE_H)
from panelcad.render.painter import Painter

_WIRE_COLOR_HEX: dict[str, str] = {
    "BK": "#1e293b",
    "BLACK": "#1e293b",
    "RD": "#dc2626",
    "RED": "#dc2626",
    "BU": "#2563eb",
    "BLUE": "#2563eb",
    "BL": "#2563eb",
    "WH": "#0284c7",
    "WHITE": "#0284c7",
    "GN": "#16a34a",
    "GREEN": "#16a34a",
    "GNYE": "#15803d",
    "YE": "#ca8a04",
    "YELLOW": "#ca8a04",
    "OG": "#ea580c",
    "ORANGE": "#ea580c",
    "BN": "#92400e",
    "BROWN": "#92400e",
    "VT": "#7c3aed",
    "VIOLET": "#7c3aed",
    "GY": "#475569",
    "GRAY": "#475569",
}

_FALLBACK_PALETTE: tuple[str, ...] = (
    "#2563eb",
    "#dc2626",
    "#0d9488",
    "#7c3aed",
    "#ea580c",
    "#0284c7",
    "#16a34a",
    "#b45309",
)


def wire_color_hex(color_code: str, step_index: int = 0) -> str:
    token = str(color_code or "").strip().upper()
    if token in _WIRE_COLOR_HEX:
        return _WIRE_COLOR_HEX[token]
    return _FALLBACK_PALETTE[step_index % len(_FALLBACK_PALETTE)]


def _paint_text(
    painter: Painter,
    x: float,
    y: float,
    text: str,
    size_mm: float,
    *,
    align: str = "start",
    bold: bool = False,
    color: str = "#111111",
    rotate: float = 0.0,
) -> None:
    painter.set_layer("TEXT")
    try:
        painter.text(x, y, text, size_mm, align=align, rotate=rotate, bold=bold, color=color)  # type: ignore[call-arg]
    except TypeError:
        painter.text(x, y, text, size_mm, align=align, rotate=rotate)


def _offset_polyline_for_bundle(
    pts: list[tuple[float, float]],
    ties: list[ZipTieWaypoint],
    strand_offset: float,
) -> list[tuple[float, float]]:
    """Offset interior curve points along the smooth normal vector so parallel wires in a
    bundle are visually distinct, while pinching smoothly to zero offset at both terminal
    landings and at every ZipTieWaypoint collar (like `@cmzw_`'s zip-ties prototype).
    """

    if len(pts) <= 2 or abs(strand_offset) < 1e-4:
        return list(pts)

    tie_coords = [(float(t.x), float(t.y)) for t in ties]
    cum_len: list[float] = [0.0]
    for i in range(len(pts) - 1):
        cum_len.append(cum_len[-1] + math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]))
    total_len = cum_len[-1]
    if total_len < 1.0:
        return list(pts)

    out: list[tuple[float, float]] = [pts[0]]
    ramp_mm = min(14.0, total_len * 0.25)
    tie_pinch_mm = 11.0

    for i in range(1, len(pts) - 1):
        x, y = pts[i]
        s = cum_len[i]
        # Smooth cosine ramp from 0 at start/end terminals
        end_dist = min(s, total_len - s)
        if end_dist >= ramp_mm:
            env_end = 1.0
        else:
            env_end = 0.5 * (1.0 - math.cos(math.pi * (end_dist / max(1e-3, ramp_mm))))

        # Smooth cosine pinch to 0.12 at each zip-tie collar so wires cinch tightly inside the strap
        env_tie = 1.0
        for tx, ty in tie_coords:
            d_tie = math.hypot(x - tx, y - ty)
            if d_tie < tie_pinch_mm:
                pinch = 0.12 + 0.88 * (0.5 * (1.0 - math.cos(math.pi * (d_tie / tie_pinch_mm))))
                env_tie = min(env_tie, pinch)

        scale = env_end * env_tie
        prev_x, prev_y = pts[max(0, i - 2)]
        next_x, next_y = pts[min(len(pts) - 1, i + 2)]
        tx_vec = next_x - prev_x
        ty_vec = next_y - prev_y
        tnorm = math.hypot(tx_vec, ty_vec)
        if tnorm < 1e-6:
            out.append((x, y))
            continue
        nx = -ty_vec / tnorm
        ny = tx_vec / tnorm
        out.append((round(x + nx * strand_offset * scale, 3), round(y + ny * strand_offset * scale, 3)))

    out.append(pts[-1])
    return out


def _draw_arrows_along_polyline(
    painter: Painter,
    pts: list[tuple[float, float]],
    color: str,
    size_mm: float = 2.1,
) -> None:
    """Place directional chevrons along the smooth curve at 25%, 50%, and 75% arc length."""

    if len(pts) < 2:
        return
    cum: list[float] = [0.0]
    for i in range(len(pts) - 1):
        cum.append(cum[-1] + math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]))
    total = cum[-1]
    if total < 14.0:
        return

    fractions = [0.5] if total < 45.0 else [0.24, 0.52, 0.78]
    painter.set_stroke(color, 0.25)
    painter.set_fill(color)
    for frac in fractions:
        target = total * frac
        for i in range(len(pts) - 1):
            if cum[i] <= target <= cum[i + 1]:
                seg_l = cum[i + 1] - cum[i]
                if seg_l < 1e-5:
                    break
                t = (target - cum[i]) / seg_l
                mx = pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t
                my = pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t
                i0 = max(0, i - 1)
                i1 = min(len(pts) - 1, i + 2)
                dx = pts[i1][0] - pts[i0][0]
                dy = pts[i1][1] - pts[i0][1]
                dn = math.hypot(dx, dy)
                if dn < 1e-5:
                    break
                ux, uy = dx / dn, dy / dn
                px, py = -uy, ux
                tip = (mx + ux * size_mm * 0.72, my + uy * size_mm * 0.72)
                left = (
                    mx - ux * size_mm * 0.55 + px * size_mm * 0.54,
                    my - uy * size_mm * 0.55 + py * size_mm * 0.54,
                )
                right = (
                    mx - ux * size_mm * 0.55 - px * size_mm * 0.54,
                    my - uy * size_mm * 0.55 - py * size_mm * 0.54,
                )
                painter.polygon([tip, left, right])
                break
    painter.set_fill(None)


def _draw_zip_tie_marker(
    painter: Painter,
    tie: ZipTieWaypoint,
    *,
    highlighted: bool = True,
    badge_index: int = 0,
) -> None:
    cx, cy = float(tie.x), float(tie.y)
    strap_fill = "#f59e0b" if highlighted else "#fcd34d"
    strap_border = "#78350f" if highlighted else "#92400e"

    if tie.slice_line is not None and highlighted:
        x1, y1, x2, y2 = tie.slice_line
        painter.set_stroke("#d97706", 0.3)
        painter.line(x1, y1, x2, y2)

    painter.set_stroke(strap_border, 0.35)
    painter.set_fill(strap_fill)
    if tie.orientation == "v":
        painter.rect(cx - 1.1, cy - 3.6, 2.2, 7.2)
        painter.set_fill("#b45309")
        painter.rect(cx - 1.5, cy - 1.3, 3.0, 2.6)
    else:
        painter.rect(cx - 3.6, cy - 1.1, 7.2, 2.2)
        painter.set_fill("#b45309")
        painter.rect(cx - 1.3, cy - 1.5, 2.6, 3.0)

    if highlighted:
        label_txt = f"{tie.id.upper()} ({len(tie.wire_numbers)}w)"
        bw = max(14.0, len(label_txt) * 1.4 + 2.6)
        bh = 3.8
        stagger = (badge_index % 2) * 4.5
        bx = cx - bw / 2.0
        by = cy - 6.4 - stagger
        painter.set_fill(None)
        painter.set_stroke(strap_border, 0.28)
        painter.line(cx, cy - 1.2, cx, by + bh)

        painter.set_fill("#fef3c7")
        painter.set_stroke(strap_border, 0.32)
        painter.rect(bx, by, bw, bh)
        _paint_text(
            painter,
            bx + bw / 2.0,
            by + bh * 0.74,
            label_txt,
            2.1,
            align="middle",
            bold=True,
            color="#78350f",
        )
    painter.set_fill(None)


def _draw_all_bundles_banner(
    painter: Painter,
    sheet: Sheet,
    routes: list[PanelWireRoute],
    ties: list[ZipTieWaypoint],
) -> None:
    ux, uy, uw, _uh = usable_rect(sheet.page)
    banner_x = ux + 8.0
    banner_y = uy + 3.5
    banner_w = min(240.0, uw - 16.0)
    banner_h = 15.0

    total_mm = sum(r.cut_length_mm for r in routes)
    total_in = sum(r.cut_length_in for r in routes)

    painter.set_stroke("#0f172a", 0.4)
    painter.set_fill("#f8fafc")
    painter.rect(banner_x, banner_y, banner_w, banner_h)

    painter.set_stroke("#0f172a", 0.2)
    painter.set_fill("#0f172a")
    painter.rect(banner_x, banner_y, banner_w, 5.6)

    hdr_text = (
        f"BACKPANEL WIRE ROUTING & ZIP-TIE BUNDLES   |   "
        f"{len(routes)} Routed Conductors   |   {len(ties)} Zip-Tie Waypoints"
    )
    _paint_text(
        painter,
        banner_x + 3.5,
        banner_y + 4.0,
        hdr_text,
        2.5,
        align="start",
        bold=True,
        color="#f8fafc",
    )

    tie_summary = (
        ", ".join(f"{t.id.upper()}: {len(t.wire_numbers)} wires @ ({t.x:.0f},{t.y:.0f})" for t in ties[:4])
        if ties
        else "Standard Panduit wire-duct centerline routing"
    )
    body_text = (
        f"Total Cut Length: {total_in:.1f}\" ({total_mm:.0f} mm)     |     "
        f"Bundles: {tie_summary}"
    )
    _paint_text(
        painter,
        banner_x + 3.5,
        banner_y + 11.6,
        body_text,
        2.4,
        align="start",
        bold=True,
        color="#1e293b",
    )
    painter.set_fill(None)


def _draw_walkthrough_endpoint_callout(
    painter: Painter,
    x: float,
    y: float,
    text: str,
    *,
    is_source: bool,
    prefer_above: bool,
    horizontal_shift: float = 0.0,
) -> None:
    primary = "#16a34a" if is_source else "#dc2626"
    bg_fill = "#dcfce7" if is_source else "#fee2e2"
    text_col = "#14532d" if is_source else "#7f1d1d"

    painter.set_stroke(primary, 0.55)
    painter.set_fill(bg_fill)
    painter.circle(x, y, 2.2)
    painter.set_fill(primary)
    painter.circle(x, y, 0.9)

    bw = max(23.0, len(text) * 1.65 + 4.2)
    bh = 4.8
    bx = x - bw / 2.0 + horizontal_shift
    by = (y - 9.2 - bh) if prefer_above else (y + 9.2)

    anchor_y = by + bh if prefer_above else by
    painter.set_fill(None)
    painter.set_stroke(primary, 0.45)
    painter.line(x, y, bx + bw / 2.0, anchor_y)

    painter.set_fill(bg_fill)
    painter.set_stroke(primary, 0.45)
    painter.rect(bx, by, bw, bh)

    _paint_text(
        painter,
        bx + bw / 2.0,
        by + bh * 0.74,
        text,
        2.35,
        align="middle",
        bold=True,
        color=text_col,
    )
    painter.set_fill(None)


def _draw_walkthrough_hud_card(
    painter: Painter,
    sheet: Sheet,
    route: PanelWireRoute,
    total_steps: int,
) -> None:
    ux, uy, uw, _uh = usable_rect(sheet.page)
    card_x = ux + 8.0
    card_y = uy + 3.0
    card_w = min(244.0, uw - 16.0)
    card_h = 22.0

    painter.set_stroke("#0f172a", 0.5)
    painter.set_fill("#ffffff")
    painter.rect(card_x, card_y, card_w, card_h)

    painter.set_stroke("#0f172a", 0.2)
    painter.set_fill("#0f172a")
    painter.rect(card_x, card_y, card_w, 6.2)

    status_str = "INSTALLED [x]" if route.installed else "NEXT TO WIRE [ ]"
    hdr_txt = (
        f"TECHNICIAN WIRING WALKTHROUGH   —   "
        f"STEP {route.step_index + 1} OF {total_steps}   |   "
        f"WIRE #{route.wire_number}   |   {status_str}"
    )
    _paint_text(
        painter,
        card_x + 3.5,
        card_y + 4.4,
        hdr_txt,
        2.6,
        align="start",
        bold=True,
        color="#38bdf8",
    )

    pill_w = 66.0
    pill_h = 12.2
    pill_x = card_x + card_w - pill_w - 3.5
    pill_y = card_y + 7.8
    painter.set_stroke("#0284c7", 0.45)
    painter.set_fill("#e0f2fe")
    painter.rect(pill_x, pill_y, pill_w, pill_h)

    _paint_text(
        painter,
        pill_x + pill_w / 2.0,
        pill_y + 3.8,
        "SHOP CUT LENGTH",
        2.1,
        align="middle",
        bold=True,
        color="#0369a1",
    )
    _paint_text(
        painter,
        pill_x + pill_w / 2.0,
        pill_y + 9.6,
        f"{route.cut_length_in:.1f}\"  ({route.cut_length_mm:.0f} mm)",
        3.1,
        align="middle",
        bold=True,
        color="#0c4a6e",
    )

    main_line = (
        f"FROM: {route.from_endpoint}   --->   TO: {route.to_endpoint}     |     "
        f"WIRE: #{route.wire_number} ({route.gauge} {route.color} {route.wire_type})"
    )
    _paint_text(
        painter,
        card_x + 4.0,
        card_y + 12.0,
        main_line,
        2.75,
        align="start",
        bold=True,
        color="#0f172a",
    )

    sub_line = (
        f"ROUTE VIA: {route.via_path}     |     "
        f"2D Path: {route.path_2d_mm:.0f} mm + Z-Drop ({route.z_drop_from_mm:.0f}+{route.z_drop_to_mm:.0f} mm) + Duct/Slack"
    )
    _paint_text(
        painter,
        card_x + 4.0,
        card_y + 18.2,
        sub_line,
        2.3,
        align="start",
        bold=False,
        color="#334155",
    )
    painter.set_fill(None)


def draw_panel_wiring_overlay(
    painter: Painter,
    sheet: Sheet,
    catalog: Catalog | None,
    project: Project | None,
) -> None:
    """Render backpanel wire bundles (`all`) or single-wire Technician Walkthrough (`walkthrough`)."""

    if project is None:
        return
    mode = get_panel_wiring_mode(sheet, project)
    raw_ties = (sheet.properties or {}).get("zip_ties") or []
    if mode == "off" and not raw_ties:
        return
    if mode == "off" and raw_ties:
        mode = "all"

    sched = build_wire_by_wire_schedule(project, sheet.id, catalog)
    routes = sched.rows
    ties = sched.zip_ties
    if not routes:
        return

    if mode == "all":
        painter.set_fill(None)
        painter.set_stroke("#f8fafc", 0.90)
        for route in routes:
            if len(route.points) >= 2:
                painter.polyline(route.points)

        for route in routes:
            col = wire_color_hex(route.color, route.step_index)
            painter.set_fill(None)
            painter.set_stroke(col, 0.54)
            painter.polyline(route.points)

            painter.set_stroke("#ffffff", 0.18)
            painter.set_fill(col)
            painter.circle(route.points[0][0], route.points[0][1], 0.72)
            painter.circle(route.points[-1][0], route.points[-1][1], 0.72)

        for b_idx, tie in enumerate(ties):
            _draw_zip_tie_marker(painter, tie, highlighted=True, badge_index=b_idx)

        _draw_all_bundles_banner(painter, sheet, routes, ties)

    elif mode == "walkthrough":
        active_idx = get_walkthrough_step(sheet, len(routes))
        active = routes[active_idx]

        painter.set_fill(None)
        for route in routes:
            if route.step_index == active_idx:
                continue
            ghost_col = "#86efac" if route.installed else "#cbd5e1"
            painter.set_stroke(ghost_col, 0.24)
            painter.polyline(route.points)

        for b_idx, tie in enumerate(ties):
            is_used = tie.id in active.via or active.wire_number in tie.wire_numbers
            _draw_zip_tie_marker(painter, tie, highlighted=is_used, badge_index=b_idx)

        active_col = wire_color_hex(active.color, active.step_index)
        if active.color.upper() in ("BK", "BLACK", "GY", "GRAY"):
            active_col = "#0284c7"

        painter.set_fill(None)
        painter.set_stroke("#bae6fd", 1.95)
        painter.polyline(active.points)

        painter.set_stroke(active_col, 0.98)
        painter.polyline(active.points)

        _draw_arrows_along_polyline(painter, active.points, active_col, size_mm=2.1)

        sx, sy = active.points[0]
        ex, ey = active.points[-1]
        from_above = active.from_landing.side == "top" if active.from_landing else (sy <= ey)
        to_above = active.to_landing.side == "top" if active.to_landing else (ey < sy)

        from_shift = 0.0
        to_shift = 0.0
        if from_above == to_above and abs(sx - ex) < 34.0 and abs(sy - ey) < 18.0:
            if sx <= ex:
                from_shift = -14.0
                to_shift = 14.0
            else:
                from_shift = 14.0
                to_shift = -14.0

        _draw_walkthrough_endpoint_callout(
            painter,
            sx,
            sy,
            f"FROM: {active.from_endpoint}",
            is_source=True,
            prefer_above=from_above,
            horizontal_shift=from_shift,
        )
        _draw_walkthrough_endpoint_callout(
            painter,
            ex,
            ey,
            f"TO: {active.to_endpoint}",
            is_source=False,
            prefer_above=to_above,
            horizontal_shift=to_shift,
        )

        _draw_walkthrough_hud_card(painter, sheet, active, len(routes))


def draw_wire_by_wire_sheet(
    painter: Painter,
    sheet: Sheet,
    catalog: Catalog | None,
    project: Project | None,
) -> None:
    """Render a dedicated `wire_by_wire` Cut-Length & Routing Schedule sheet."""

    if project is None:
        return
    layout_id = str((sheet.properties or {}).get("layout_sheet_id") or "").strip() or None
    sched = build_wire_by_wire_schedule(project, sheet_id=layout_id, catalog=catalog)
    routes = sched.rows
    ties = sched.zip_ties

    ux, uy, uw, uh = usable_rect(sheet.page)
    margin = 6.0
    tx = ux + margin
    ty = uy + margin
    tw = uw - 2.0 * margin

    hdr_h = 18.0
    painter.set_stroke("#0f172a", 0.45)
    painter.set_fill("#f8fafc")
    painter.rect(tx, ty, tw, hdr_h)

    painter.set_stroke("#0f172a", 0.2)
    painter.set_fill("#0f172a")
    painter.rect(tx, ty, tw, 6.8)

    _paint_text(
        painter,
        tx + 3.5,
        ty + 4.8,
        f"WIRE-BY-WIRE CUT & ROUTING SCHEDULE   —   {project.name.upper()} (LAYOUT: {sched.sheet_id.upper()})",
        3.0,
        align="start",
        bold=True,
        color="#ffffff",
    )

    tie_info = f"{len(ties)} Active ({', '.join(t.id.upper() for t in ties)})" if ties else "0 (Duct Centerline Auto-Route)"
    meta_line1 = (
        f"Total Conductors: {sched.total_wires}     |     "
        f"Zip-Tie Bundles: {tie_info}     |     "
        f"Total Cut Length: {sched.total_length_in:.1f}\" ({sched.total_length_mm:.0f} mm)"
    )
    _paint_text(
        painter,
        tx + 3.5,
        ty + 11.2,
        meta_line1,
        2.5,
        align="start",
        bold=True,
        color="#1e293b",
    )

    note_line = (
        "Cut Length Formula: 2D Backpanel Routed Length (scaled to panel mm) + "
        "Source/Dest Device Z-Drop + 2x Duct Depth Allowance (50 mm) + 2x Strip/Termination Slack (60 mm), "
        "rounded up to next 0.5\" / 10 mm shop increment."
    )
    _paint_text(
        painter,
        tx + 3.5,
        ty + 15.8,
        note_line,
        2.15,
        align="start",
        bold=False,
        color="#475569",
    )

    table_y = ty + hdr_h + 3.5
    col_specs: list[tuple[str, float, str]] = [
        ("STEP", 0.055, "middle"),
        ("WIRE #", 0.075, "middle"),
        ("FROM (TAG:PIN)", 0.125, "start"),
        ("TO (TAG:PIN)", 0.125, "start"),
        ("GAUGE / TYPE", 0.115, "middle"),
        ("COLOR", 0.075, "start"),
        ("ROUTING / ZIP-TIE PATH", 0.185, "start"),
        ("2D PATH", 0.065, "end"),
        ("CUT (IN)", 0.070, "end"),
        ("CUT (MM)", 0.065, "end"),
        ("INST", 0.045, "middle"),
    ]

    th_h = 6.2
    painter.set_stroke("#1e293b", 0.35)
    painter.set_fill("#1e293b")
    painter.rect(tx, table_y, tw, th_h)

    cx = tx
    col_boxes: list[tuple[float, float, str]] = []
    for title, frac, align in col_specs:
        cw = tw * frac
        col_boxes.append((cx, cw, align))
        _paint_text(
            painter,
            cx + cw / 2.0,
            table_y + 4.3,
            title,
            2.3,
            align="middle",
            bold=True,
            color="#f8fafc",
        )
        cx += cw

    avail_h = max(40.0, (uy + uh - 36.0) - (table_y + th_h))
    max_rows = max(1, len(routes))
    row_h = min(6.2, max(4.2, avail_h / (max_rows + 1.5)))
    font_sz = 2.25 if row_h >= 5.0 else 2.0

    ry = table_y + th_h
    for idx, r in enumerate(routes):
        if ry + row_h > uy + uh - 32.0:
            break
        bg = "#ffffff" if idx % 2 == 0 else "#f1f5f9"
        painter.set_stroke("#cbd5e1", 0.25)
        painter.set_fill(bg)
        painter.rect(tx, ry, tw, row_h)

        cut_in_x, cut_in_w, _ = col_boxes[8]
        cut_mm_x, cut_mm_w, _ = col_boxes[9]
        painter.set_fill("#e0f2fe" if idx % 2 == 0 else "#bae6fd")
        painter.rect(cut_in_x, ry, cut_in_w + cut_mm_w, row_h)

        cells = [
            f"#{r.step_index + 1}",
            r.wire_number,
            r.from_endpoint,
            r.to_endpoint,
            f"{r.gauge} {r.wire_type}",
            r.color,
            r.via_path,
            f"{r.path_2d_mm:.0f} mm",
            f"{r.cut_length_in:.1f}\"",
            f"{r.cut_length_mm:.0f} mm",
            "[x]" if r.installed else "[ ]",
        ]

        baseline_y = ry + row_h * 0.70
        for c_idx, val in enumerate(cells):
            cell_x, cell_w, align = col_boxes[c_idx]
            is_bold = c_idx in (1, 8, 9)
            t_col = "#0c4a6e" if c_idx in (8, 9) else "#0f172a"

            if c_idx == 5:
                sw_col = wire_color_hex(r.color, r.step_index)
                painter.set_stroke("#334155", 0.2)
                painter.set_fill(sw_col)
                painter.circle(cell_x + 3.2, ry + row_h / 2.0, 1.0)
                _paint_text(
                    painter,
                    cell_x + 5.2,
                    baseline_y,
                    val,
                    font_sz,
                    align="start",
                    bold=False,
                    color="#1e293b",
                )
            else:
                if align == "middle":
                    tx_pos = cell_x + cell_w / 2.0
                elif align == "end":
                    tx_pos = cell_x + cell_w - 2.2
                else:
                    tx_pos = cell_x + 2.2
                _paint_text(
                    painter,
                    tx_pos,
                    baseline_y,
                    val,
                    font_sz,
                    align=align,
                    bold=is_bold,
                    color=t_col,
                )
            painter.set_fill(None)
            painter.set_stroke("#cbd5e1", 0.22)
            painter.line(cell_x, ry, cell_x, ry + row_h)

        ry += row_h

    footer_h = row_h + 0.8
    painter.set_stroke("#0f172a", 0.4)
    painter.set_fill("#e2e8f0")
    painter.rect(tx, ry, tw, footer_h)

    _paint_text(
        painter,
        tx + 3.0,
        ry + footer_h * 0.72,
        f"TOTAL SHOP WIRE CUT REQUIREMENT ({sched.total_wires} CONDUCTORS):",
        font_sz + 0.15,
        align="start",
        bold=True,
        color="#0f172a",
    )
    cut_in_x, cut_in_w, _ = col_boxes[8]
    cut_mm_x, cut_mm_w, _ = col_boxes[9]
    _paint_text(
        painter,
        cut_in_x + cut_in_w - 2.2,
        ry + footer_h * 0.72,
        f"{sched.total_length_in:.1f}\"",
        font_sz + 0.15,
        align="end",
        bold=True,
        color="#0c4a6e",
    )
    _paint_text(
        painter,
        cut_mm_x + cut_mm_w - 2.2,
        ry + footer_h * 0.72,
        f"{sched.total_length_mm:.0f} mm",
        font_sz + 0.15,
        align="end",
        bold=True,
        color="#0c4a6e",
    )
    painter.set_fill(None)
