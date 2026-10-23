"""Device tag prefixes: IEC 81346 from the library, JIC/NFPA 79 for NEMA.

Library YAML keeps ``ref_prefix`` as the IEC (or OEM) default. When the
sheet or title block is NEMA/ANSI, tags come from ``ref_prefixes.yaml``
and optional ``project.ref_prefixes``. Loading a saved project never
rewrites tags — only ``place``, Circuit Builder, and an explicit retag
with ``apply_prefixes`` assign new ones.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from panelcad.core.models import Device, Project, Sheet
from panelcad.library import yaml_load as _lib_yaml
from panelcad.library.catalog import normalize_standard
from panelcad.library.paths import builtin_library_dir, library_search_paths, user_library_dir

PREFIX_FILENAME = "ref_prefixes.yaml"
NEMA_STANDARD = "NEMA/ANSI"


def resolve_drawing_standard(session: Any, sheet_id: str | None = None) -> str:
    """Sheet standard, else title block, else NEMA/ANSI."""

    raw = ""
    sheet = None
    if sheet_id:
        try:
            sheet = session.sheet(sheet_id)
        except KeyError:
            sheet = None
    if sheet is None:
        try:
            sheet = session.sheet()
        except Exception:  # noqa: BLE001
            sheet = None
    if sheet is not None:
        raw = str(getattr(sheet, "standard", "") or "").strip()
    if not raw:
        project = getattr(session, "project", None)
        tb = getattr(project, "title_block", None) if project is not None else None
        raw = str(getattr(tb, "standard", "") or "").strip()
    if not raw:
        return NEMA_STANDARD
    return normalize_standard(raw, default=NEMA_STANDARD)


def sheet_drawing_standard(project: Project, sheet: Sheet | None = None) -> str:
    raw = str(getattr(sheet, "standard", "") or "").strip() if sheet is not None else ""
    if not raw:
        raw = str(getattr(project.title_block, "standard", "") or "").strip()
    if not raw:
        return NEMA_STANDARD
    return normalize_standard(raw, default=NEMA_STANDARD)


def _as_prefix_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        prefix = str(value or "").strip()
        token = str(key or "").strip()
        if token and prefix:
            out[token] = prefix
    return out


def _read_prefix_file(path: Path) -> dict[str, dict[str, str]]:
    data = _lib_yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {}
    block = data.get("standards")
    if not isinstance(block, dict):
        block = {key: value for key, value in data.items() if key not in {"kind", "version", "devices"}}
    maps: dict[str, dict[str, str]] = {}
    for raw_std, raw_map in (block or {}).items():
        std = normalize_standard(str(raw_std), default="")
        if std != NEMA_STANDARD:
            continue
        mapped = _as_prefix_map(raw_map)
        if mapped:
            maps[std] = mapped
    return maps


def _merge_prefix_maps(dest: dict[str, dict[str, str]], incoming: dict[str, dict[str, str]]) -> None:
    for std, table in incoming.items():
        dest.setdefault(std, {}).update(table)


def _prefix_search_dirs(project_dir: Path | None = None) -> list[Path]:
    """Builtin first, then user, project, and PANELCAD_LIBRARY_PATH (later wins)."""

    ordered: list[Path] = []
    builtin = builtin_library_dir()
    if builtin.is_dir():
        ordered.append(builtin)
    user = user_library_dir()
    if user.is_dir():
        ordered.append(user)
    if project_dir is not None:
        local = Path(project_dir) / "library"
        if local.is_dir():
            ordered.append(local)
    extra = library_search_paths(project_dir)
    for directory in extra:
        if directory not in ordered:
            ordered.append(directory)
    return ordered


def load_ref_prefix_maps(project_dir: Path | None = None) -> dict[str, dict[str, str]]:
    """Merge ``ref_prefixes.yaml`` from builtin → user → project → extra paths."""

    merged: dict[str, dict[str, str]] = {}
    for directory in _prefix_search_dirs(project_dir):
        path = directory / PREFIX_FILENAME
        if path.is_file():
            _merge_prefix_maps(merged, _read_prefix_file(path))
    return merged


def is_prefix_library_file(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if str(data.get("kind") or "") == "ref_prefixes":
        return True
    return "standards" in data and not data.get("devices")


def _lookup_mapped_prefix(device: Device, table: dict[str, str]) -> str:
    category = str(device.category or "").strip()
    for tag in device.tags:
        key = f"{category}.{tag}"
        if key in table:
            return table[key]
    if category in table:
        return table[category]
    return ""


def resolve_ref_prefix(
    device: Device,
    *,
    standard: str,
    maps: dict[str, dict[str, str]] | None = None,
    overrides: dict[str, str] | None = None,
) -> str:
    """Prefix ``place`` / Circuit Builder will use for this device on this standard."""

    lib = str(device.ref_prefix or "A").strip() or "A"
    std = normalize_standard(standard, default=NEMA_STANDARD)
    if std != NEMA_STANDARD:
        return lib
    table = dict((maps or {}).get(NEMA_STANDARD) or {})
    table.update(_as_prefix_map(overrides))
    mapped = _lookup_mapped_prefix(device, table)
    return mapped or lib


def next_tag(prefix: str, used: set[str]) -> str:
    token = str(prefix or "A").strip() or "A"
    n = 1
    while f"{token}{n}" in used:
        n += 1
    tag = f"{token}{n}"
    used.add(tag)
    return tag
