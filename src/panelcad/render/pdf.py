from __future__ import annotations

import io
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from panelcad.core.models import Project, Sheet
from panelcad.library.catalog import Catalog

log = logging.getLogger("panelcad.render.pdf")


def _new_sibling_temp(dest: Path, suffix: str = ".partial") -> Path:
    """Create and immediately close a unique sibling temp file for atomic PDF replace."""

    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=suffix, dir=str(dest.parent))
    os.close(fd)
    return Path(raw)


@contextmanager
def atomic_pdf_path(path: Path | str) -> Iterator[Path]:
    """Yield a sibling temp path; atomically replace ``path`` only if non-empty and error-free."""

    dest = Path(path)
    tmp = _new_sibling_temp(dest)
    try:
        yield tmp
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise OSError(f"PDF export produced an empty file: {dest}")
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextmanager
def atomic_pdf_writer(
    path: Path | str,
    *,
    resolution: int = 300,
    title: str = "",
    creator: str | None = None,
    width_mm: float | None = None,
    height_mm: float | None = None,
) -> Iterator[tuple[Any, Any]]:
    """Open a QPdfWriter + QPainter on a temp file, close handles cleanly, then os.replace."""

    from PySide6.QtGui import QPainter, QPdfWriter

    from panelcad.render.page import configure_full_page

    with atomic_pdf_path(path) as tmp:
        writer: Any = QPdfWriter(str(tmp))
        painter: Any = None
        try:
            writer.setResolution(int(resolution))
            if title:
                writer.setTitle(title)
            if creator and hasattr(writer, "setCreator"):
                writer.setCreator(creator)
            if width_mm is not None and height_mm is not None:
                configure_full_page(writer, width_mm, height_mm)
            painter = QPainter(writer)
            yield writer, painter
        finally:
            if painter is not None:
                if painter.isActive():
                    painter.end()
                del painter
            del writer


def _embed_project_metadata(pdf_path: Path, project: Project, sheets: list[Sheet] | None) -> None:
    """Embed serialized project metadata into ``pdf_path`` without holding ``pdf_path`` open."""

    from pypdf import PdfReader, PdfWriter

    raw = pdf_path.read_bytes()
    if not raw:
        return
    reader = PdfReader(io.BytesIO(raw))
    writer = PdfWriter()
    writer.append(reader)
    proj_copy = project.model_copy(deep=True)
    if sheets is not None:
        proj_copy.sheets = list(sheets)
    writer.add_metadata(
        {
            "/Title": project.name or "PanelCAD Project",
            "/Producer": "PanelCAD",
            "/PanelCADProject": json.dumps(proj_copy.model_dump(mode="json"), separators=(",", ":")),
        }
    )
    meta_tmp = _new_sibling_temp(pdf_path, suffix=".meta.partial")
    try:
        with meta_tmp.open("wb") as fh:
            writer.write(fh)
        if meta_tmp.stat().st_size == 0:
            raise OSError("metadata rewrite produced an empty PDF")
        os.replace(meta_tmp, pdf_path)
    finally:
        meta_tmp.unlink(missing_ok=True)


def project_to_pdf(
    project: Project,
    catalog: Catalog,
    path: Path | str,
    *,
    sheets: list[Sheet] | None = None,
    base_dir: Path | None = None,
) -> None:
    """Multi-page PDF: one page per sheet, full title block on each.

    The file is replaced only after the pages paint. A failure leaves no partial PDF.
    """
    from PySide6.QtGui import QPdfWriter

    from panelcad.render.page import paint_project_on_device

    with atomic_pdf_path(path) as tmp:
        writer: Any = QPdfWriter(str(tmp))
        try:
            writer.setResolution(300)
            writer.setTitle(project.name)
            paint_project_on_device(writer, project, catalog, sheets=sheets, base_dir=base_dir)
        finally:
            # Drop the writer before reading or replacing so Qt closes the OS handle.
            del writer
        try:
            _embed_project_metadata(tmp, project, sheets)
        except Exception as exc:  # noqa: BLE001 — painted PDF stays intact without metadata
            log.warning("could not embed PanelCAD metadata into PDF %s: %s", path, exc)


def sheet_to_pdf(
    sheet: Sheet,
    catalog: Catalog,
    path: Path | str,
    project: Project | None = None,
    sheet_index: int = 0,
    sheet_count: int = 1,
    title: str = "",
    base_dir: Path | None = None,
) -> None:
    del sheet_index, sheet_count
    if project is None:
        project = Project(name=title or sheet.title or sheet.id, sheets=[sheet])
    project_to_pdf(project, catalog, path, sheets=[sheet], base_dir=base_dir)
