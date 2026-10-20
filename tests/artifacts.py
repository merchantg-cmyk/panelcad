"""Screenshot and report files written by GUI tests.

Tests stay inside ``tmp_path`` unless ``PANELCAD_ARTIFACTS_DIR`` is set.
"""

from __future__ import annotations

import os
from pathlib import Path


def artifact_dir(tmp_path: Path, *parts: str) -> Path:
    raw = os.environ.get("PANELCAD_ARTIFACTS_DIR", "").strip()
    base = Path(raw) if raw else Path(tmp_path) / "artifacts"
    dest = base.joinpath(*parts) if parts else base
    dest.mkdir(parents=True, exist_ok=True)
    return dest
