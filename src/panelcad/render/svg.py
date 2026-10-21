from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from xml.sax.saxutils import escape

from panelcad.core.models import Project, Sheet
from panelcad.library.catalog import Catalog
from panelcad.render.contents import draw_sheet_body
from panelcad.render.painter import Painter
from panelcad.render.titleblock import draw_sheet_frame


class SvgPainter(Painter):
    def __init__(self) -> None:
        self.parts: list[str] = []
        self._stroke = "#111111"
        self._sw = 0.35
        self._fill = "none"

    def set_stroke(self, color: str, width: float) -> None:
        self._stroke = color
        self._sw = width

    def set_fill(self, color: str | None) -> None:
        self._fill = color if color else "none"

    def _style(self) -> str:
        return f'stroke="{self._stroke}" stroke-width="{self._sw}" fill="{self._fill}" stroke-linejoin="round" stroke-linecap="round"'

    def rect(self, x: float, y: float, w: float, h: float) -> None:
        self.parts.append(f'<rect x="{x:.3f}" y="{y:.3f}" width="{w:.3f}" height="{h:.3f}" {self._style()} />')

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.parts.append(
            f'<line x1="{x1:.3f}" y1="{y1:.3f}" x2="{x2:.3f}" y2="{y2:.3f}" {self._style()} />'
        )

    def circle(self, x: float, y: float, r: float) -> None:
        self.parts.append(f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{r:.3f}" {self._style()} />')

    def arc(self, cx: float, cy: float, radius: float, start_deg: float, sweep_deg: float) -> None:
        from panelcad.core.primitives import arc_point

        if radius <= 0 or abs(sweep_deg) < 1e-9:
            return
        x1, y1 = arc_point(cx, cy, radius, start_deg)
        x2, y2 = arc_point(cx, cy, radius, start_deg + sweep_deg)
        large = 1 if abs(sweep_deg) > 180.0 else 0
        # SVG sweep-flag 1 is clockwise on the page. Our positive sweep is the other way.
        sweep_flag = 0 if sweep_deg > 0 else 1
        self.parts.append(
            f'<path d="M {x1:.3f} {y1:.3f} A {radius:.3f} {radius:.3f} 0 {large} {sweep_flag} {x2:.3f} {y2:.3f}" '
            f'{self._style()} />'
        )

    def polyline(self, points: list[tuple[float, float]]) -> None:
        if len(points) < 2:
            return
        d = " ".join(f"{x:.3f},{y:.3f}" for x, y in points)
        self.parts.append(f'<polyline points="{d}" {self._style()} />')

    def polygon(self, points: list[tuple[float, float]]) -> None:
        if len(points) < 3:
            return
        d = " ".join(f"{x:.3f},{y:.3f}" for x, y in points)
        self.parts.append(f'<polygon points="{d}" {self._style()} />')

    def text(
        self,
        x: float,
        y: float,
        text: str,
        size: float,
        align: str = "middle",
        rotate: float = 0,
    ) -> None:
        if not text:
            return
        anchor = {"start": "start", "middle": "middle", "end": "end"}.get(align, "middle")
        transform = f' transform="rotate({rotate:.3f} {x:.3f} {y:.3f})"' if rotate else ""
        self.parts.append(
            f'<text x="{x:.3f}" y="{y:.3f}" font-size="{size}" font-family="Segoe UI, Helvetica, Arial, sans-serif" '
            f'text-anchor="{anchor}" fill="#111111"{transform}>{escape(text)}</text>'
        )

    def image(self, path: str, x: float, y: float, w: float, h: float) -> None:
        file = Path(path)
        if not file.is_file():
            return
        mime = mimetypes.guess_type(file.name)[0] or "image/png"
        payload = base64.standard_b64encode(file.read_bytes()).decode("ascii")
        href = f"data:{mime};base64,{payload}"
        self.parts.append(
            f'<image href="{href}" x="{x:.3f}" y="{y:.3f}" width="{w:.3f}" height="{h:.3f}" '
            f'preserveAspectRatio="xMidYMid meet" />'
        )


def sheet_to_svg(
    sheet: Sheet,
    catalog: Catalog,
    project: Project | None = None,
    sheet_index: int = 0,
    sheet_count: int = 1,
    title: str = "",
    base_dir: Path | None = None,
) -> str:
    width, height = sheet.page.dimensions()
    painter = SvgPainter()
    if project is None:
        project = Project(name=title or sheet.title or sheet.id, sheets=[sheet])
    draw_sheet_frame(
        painter,
        project,
        sheet,
        sheet_index=sheet_index,
        sheet_count=sheet_count,
        base_dir=base_dir,
        catalog=catalog,
    )

    from panelcad.core.ladder import sheet_drawing_style

    coil = getattr(project.title_block, "coil_style", "circle") or "circle"
    style = sheet_drawing_style(sheet, project)
    draw_sheet_body(
        painter, sheet, catalog, base_dir=base_dir, coil_style=coil, drawing_style=style, project=project
    )
    from panelcad.render.clouds import draw_revision_clouds

    draw_revision_clouds(painter, project, sheet, catalog)

    body = "\n".join(painter.parts)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm" height="{height}mm" '
        f'viewBox="0 0 {width} {height}">\n'
        f'<rect width="100%" height="100%" fill="#f7f4ea"/>\n'
        f"{body}\n</svg>\n"
    )
