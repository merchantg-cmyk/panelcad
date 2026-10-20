"""Import workspace. One JSON file per page or region, plus image crops."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _safe(name: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "item")).strip("_")
    return text or "item"


class Workspace:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, *parts: str) -> Path:
        dest = self.root.joinpath(*parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest

    def read_json(self, *parts: str, default: Any = None) -> Any:
        file = self.root.joinpath(*parts)
        if not file.is_file():
            return default
        return json.loads(file.read_text(encoding="utf-8"))

    def write_json(self, data: Any, *parts: str) -> Path:
        file = self.path(*parts)
        file.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return file

    def manifest(self) -> dict[str, Any]:
        data = self.read_json("manifest.json", default=None)
        if isinstance(data, dict):
            return data
        return {
            "stage": "ingest",
            "sources": [],
            "standard": "",
            "queue": [],
            "pending": "",
            "pages": [],
            "photos": [],
        }

    def save_manifest(self, data: dict[str, Any]) -> None:
        self.write_json(data, "manifest.json")

    def page_dir(self, page_id: str) -> Path:
        dest = self.root / "pages" / _safe(page_id)
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    def work_path(self, work_id: str) -> Path:
        return self.path("work", _safe(work_id) + ".json")
