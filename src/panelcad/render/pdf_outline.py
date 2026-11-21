"""Add PDF bookmarks and internal GoTo links after QPdfWriter finishes."""

from __future__ import annotations

import io
import logging
import os
import tempfile
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
    tmp: Path | None = None
    try:
        # Read bytes into memory first so PdfReader never holds an open OS file
        # handle on ``dest`` when ``os.replace(tmp, dest)`` runs (WinError 32).
        raw = dest.read_bytes()
        if not raw:
            return False
        reader = PdfReader(io.BytesIO(raw))
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
        fd, raw_tmp = tempfile.mkstemp(
            prefix=f".{dest.name}.",
            suffix=".outline.partial",
            dir=str(dest.parent),
        )
        os.close(fd)
        tmp = Path(raw_tmp)
        with tmp.open("wb") as fh:
            writer.write(fh)
        if tmp.stat().st_size == 0:
            raise OSError("outline rewrite produced an empty PDF")
        os.replace(tmp, dest)
        tmp = None
    except Exception as exc:  # noqa: BLE001 — package PDF is still valid without outlines
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        log.warning("could not add PDF bookmarks or index links: %s", exc)
        return False
    return True


def _add_bookmarks(writer, bookmarks: list[Bookmark]) -> None:
    for item in bookmarks:
        parent = writer.add_outline_item(title=item.title, page_number=item.page)
        for child in item.children:
            writer.add_outline_item(title=child.title, page_number=child.page, parent=parent)
