"""Add PDF bookmarks and internal GoTo links after QPdfWriter finishes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from panelcad.render.print_package import Bookmark, LinkHotspot, PackagePage

log = logging.getLogger("panelcad.print_package")


def apply_outlines_and_links(
    path: Path,
    bookmarks: list[Bookmark],
    hotspots: list[LinkHotspot],
    pages: list[PackagePage],
) -> bool:
    """Return True when bookmarks/links were written. Missing pypdf is a warning."""

    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.annotations import Link
    except ImportError:
        log.warning("pypdf is not installed; print package has no bookmarks or index links")
        return False

    dest = Path(path)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        reader = PdfReader(str(path))
        writer = PdfWriter()
        writer.append(reader)
        _add_bookmarks(writer, bookmarks)
        for spot in hotspots:
            if spot.page < 0 or spot.page >= len(writer.pages):
                continue
            if spot.target < 0 or spot.target >= len(writer.pages):
                continue
            page = pages[spot.page]
            height_pts = page.height_mm * 72.0 / 25.4
            scale = 72.0 / 25.4
            x1 = spot.x * scale
            x2 = (spot.x + spot.w) * scale
            y2 = height_pts - spot.y * scale
            y1 = height_pts - (spot.y + spot.h) * scale
            annotation = Link(rect=(x1, y1, x2, y2), target_page_index=spot.target)
            writer.add_annotation(page_number=spot.page, annotation=annotation)
        with tmp.open("wb") as fh:
            writer.write(fh)
        tmp.replace(dest)
    except Exception as exc:  # noqa: BLE001 — package PDF is still valid without outlines
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        log.warning("could not add PDF bookmarks or index links: %s", exc)
        return False
    return True


def _add_bookmarks(writer, bookmarks: list[Bookmark]) -> None:
    for item in bookmarks:
        parent = writer.add_outline_item(item.title, item.page)
        for child in item.children:
            writer.add_outline_item(child.title, child.page, parent=parent)
