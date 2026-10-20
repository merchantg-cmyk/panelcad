from __future__ import annotations

import os
from pathlib import Path

from panelcad.core.models import Project, Sheet
from panelcad.library.catalog import Catalog


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

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.partial-{os.getpid()}")
    writer = None
    try:
        writer = QPdfWriter(str(tmp))
        writer.setResolution(300)
        writer.setTitle(project.name)
        paint_project_on_device(writer, project, catalog, sheets=sheets, base_dir=base_dir)
        writer = None
        os.replace(tmp, dest)
    except Exception:
        writer = None
        tmp.unlink(missing_ok=True)
        raise
    finally:
        # Drop the writer before replace so Qt closes the file.
        del writer


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
    if project is None:
        project = Project(name=title or sheet.title or sheet.id, sheets=[sheet])
        sheet_index, sheet_count = 0, 1
    project_to_pdf(project, catalog, path, sheets=[sheet], base_dir=base_dir)
