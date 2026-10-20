"""Small pictures of what the import placed, for the review panel."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from panelcad.api.session import Session


def attach_rebuilt_previews(session: Session, items: list[dict[str, Any]], dest_dir: Path | str) -> None:
    """Paint each rebuilt sheet once and crop the region around the item."""

    folder = Path(dest_dir)
    folder.mkdir(parents=True, exist_ok=True)
    cache: dict[str, tuple[Any, float]] = {}
    for item in items:
        if item.get("kind") not in {"device", "wire"}:
            continue
        tag = str(item.get("text") or "")
        sheet, x, y = _anchor(session, item, tag)
        if sheet is None or x is None or y is None:
            continue
        try:
            image, scale = cache[sheet.id]
        except KeyError:
            image, scale = _paint(session, sheet)
            cache[sheet.id] = (image, scale)
        safe = str(item.get("id") or "item").replace(":", "_").replace("/", "_")
        dest = folder / f"{safe}.png"
        _crop(image, scale, x, y, dest)
        item["rebuilt"] = str(dest)


def _anchor(session: Session, item: dict[str, Any], tag: str) -> tuple[Any, float | None, float | None]:
    payload = item.get("payload") or {}
    if item.get("kind") == "device" and tag:
        for sheet in session.project.sheets:
            try:
                comp = sheet.component(tag)
            except KeyError:
                continue
            return sheet, float(comp.x), float(comp.y)
    x = payload.get("x")
    y = payload.get("y")
    page_id = str(item.get("page_id") or "")
    sheet = _sheet_for_page(session, page_id)
    if sheet is None or x is None or y is None:
        return None, None, None
    return sheet, float(x), float(y)


def _sheet_for_page(session: Session, page_id: str) -> Any:
    if not page_id:
        return session.project.sheets[0] if session.project.sheets else None
    # Built sheet ids are title slugs. The page id is still on the component properties.
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if str(comp.properties.get("print_import_page") or "") == page_id:
                return sheet
    return session.project.sheets[0] if session.project.sheets else None


def _paint(session: Session, sheet: Any) -> tuple[Any, float]:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from panelcad.render.page import paint_sheet

    QApplication.instance() or QApplication([])
    width_mm, height_mm = sheet.page.dimensions()
    scale = 5.0
    image = QImage(max(1, int(width_mm * scale)), max(1, int(height_mm * scale)), QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    painter.scale(scale, scale)
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
    return image, scale


def sheet_frame_index(project: Any, sheet: Any) -> tuple[int, int]:
    """Sheet number and count recovered from the print, when the import stored them."""

    info = project.properties.get("print_import") or {}
    numbers = info.get("sheet_numbers") or {}
    raw = str(numbers.get(sheet.id) or "")
    if raw.isdigit():
        index = max(0, int(raw) - 1)
    else:
        index = next(i for i, item in enumerate(project.sheets) if item.id == sheet.id)
    count_raw = str(info.get("sheet_count") or "")
    count = int(count_raw) if count_raw.isdigit() else len(project.sheets)
    return index, max(count, index + 1)


def _crop(image: Any, scale: float, x: float, y: float, dest: Path) -> None:
    width_mm, height_mm = 90.0, 70.0
    left = int((x - 14.0) * scale)
    top = int((y - 16.0) * scale)
    width = int(width_mm * scale)
    height = int(height_mm * scale)
    left = max(0, min(left, image.width() - 1))
    top = max(0, min(top, image.height() - 1))
    width = min(width, image.width() - left)
    height = min(height, image.height() - top)
    image.copy(left, top, max(1, width), max(1, height)).save(str(dest))
