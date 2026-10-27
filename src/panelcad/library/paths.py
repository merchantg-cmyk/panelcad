from __future__ import annotations

import os
from pathlib import Path


_REPO_ROOT: Path | None = None


def repo_root() -> Path:
    """C:\\programming when running from source; falls back to package parent."""
    global _REPO_ROOT
    if _REPO_ROOT is not None:
        return _REPO_ROOT
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "libraries" / "builtin").is_dir() and (parent / "pyproject.toml").is_file():
            _REPO_ROOT = parent
            return parent
    return Path.cwd()


def user_library_dir() -> Path:
    return Path.home() / ".panelcad" / "library"


def builtin_library_dir() -> Path:
    return repo_root() / "libraries" / "builtin"


def is_builtin_path(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        path.resolve().relative_to(builtin_library_dir().resolve())
        return True
    except (ValueError, OSError):
        return False


def library_search_paths(project_dir: Path | None = None) -> list[Path]:
    paths: list[Path] = []
    extra = os.environ.get("PANELCAD_LIBRARY_PATH", "")
    if extra:
        paths.extend(Path(p) for p in extra.split(os.pathsep) if p)
    if project_dir is not None:
        local = project_dir / "library"
        if local.is_dir():
            paths.append(local)
    user = user_library_dir()
    if user.is_dir():
        paths.append(user)
    builtin = builtin_library_dir()
    if builtin.is_dir():
        paths.append(builtin)
    return paths


def parts_overlay_dirs(project_dir: Path | None = None) -> list[Path]:
    """Manufacturer-part YAML dirs, builtin first so project/user overlays win.

    Search order (later replaces the same manufacturer+catalog):
    ``libraries/builtin/parts``, ``PANELCAD_LIBRARY_PATH`` entries (and their
    ``parts/`` subfolders), ``~/.panelcad/library`` and ``…/parts``,
    ``<project>/library`` and ``…/parts``.
    """

    dirs: list[Path] = []
    builtin_parts = builtin_library_dir() / "parts"
    if builtin_parts.is_dir():
        dirs.append(builtin_parts)
    extra = os.environ.get("PANELCAD_LIBRARY_PATH", "")
    if extra:
        for raw in extra.split(os.pathsep):
            if not raw:
                continue
            path = Path(raw)
            dirs.append(path)
            nested = path / "parts"
            if nested.is_dir():
                dirs.append(nested)
    user = user_library_dir()
    dirs.append(user)
    if (user / "parts").is_dir():
        dirs.append(user / "parts")
    if project_dir is not None:
        local = Path(project_dir) / "library"
        dirs.append(local)
        if (local / "parts").is_dir():
            dirs.append(local / "parts")
    return dirs
