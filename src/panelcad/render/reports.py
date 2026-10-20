from __future__ import annotations

import csv
from pathlib import Path

from panelcad.core.models import Project
from panelcad.io.bom import instance_rows
from panelcad.library.catalog import Catalog


def bom_rows(project: Project, catalog: Catalog) -> list[dict[str, str]]:
    return instance_rows(project, catalog)


def wire_rows(project: Project) -> list[dict[str, str]]:
    from panelcad.core.wire_numbering import wire_list_rows

    return wire_list_rows(project)


def write_csv(path: Path | str, rows: list[dict[str, str]]) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        dest.write_text("", encoding="utf-8")
        return
    # newline="" keeps csv's \r\n from being doubled by Windows text mode.
    with dest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()), lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
