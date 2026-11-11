"""Locate the Tesseract binary the raster reader shells out to.

Order: an explicit env override, the Tesseract 5 runtime shipped beside
this install, then PATH and the usual system folders. When the shipped
copy is selected, every pytesseract invocation gets ``--tessdata-dir``
pointing at that copy's tessdata so a system model cannot replace it.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

# src/panelcad/importer/this_file.py -> repository or install root.
_INSTALL_ROOT = Path(__file__).resolve().parents[3]
_RUNTIME_REL = Path("resources") / "ocr" / "tesseract"
_TESSDATA_FLAG = "--tessdata-dir"
_TESSERACT_NAMES = {"tesseract.exe", "tesseract"}

_pop_lock = threading.Lock()
_pop_depth = 0
_pop_saved = None
_active_tessdata: str | None = None


def install_root() -> Path:
    """Directory that contains ``resources/`` and ``src/`` for this install."""

    return _INSTALL_ROOT


def bundled_runtime_dir() -> Path | None:
    """App-relative Tesseract folder when ``tesseract.exe`` is there."""

    directory = install_root() / _RUNTIME_REL
    if (directory / "tesseract.exe").is_file():
        return directory
    return None


def bundled_executable() -> Path | None:
    directory = bundled_runtime_dir()
    if directory is None:
        return None
    executable = directory / "tesseract.exe"
    if executable.is_file():
        return executable
    return None


def bundled_tessdata_dir() -> Path | None:
    directory = bundled_runtime_dir()
    if directory is None:
        return None
    tessdata = directory / "tessdata"
    if tessdata.is_dir():
        return tessdata
    return None


def _env_override(key: str) -> str:
    value = str(os.environ.get(key) or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value


def _system_tesseract_candidates() -> list[str]:
    found: list[str] = []
    located = shutil.which("tesseract")
    if located:
        found.append(located)
    found.extend(
        [
            "/usr/bin/tesseract",
            "/usr/local/bin/tesseract",
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
    )
    return found


def _existing(path: str) -> str:
    candidate = Path(path)
    if path and candidate.is_file():
        return str(candidate)
    return ""


def find_tesseract() -> str:
    """Tesseract binary, or empty when none of the lookup steps finds a file."""

    for key in ("TESSERACT_CMD", "PANELCAD_TESSERACT"):
        found = _existing(_env_override(key))
        if found:
            return found
    bundled = bundled_executable()
    if bundled is not None:
        return str(bundled)
    for candidate in _system_tesseract_candidates():
        found = _existing(candidate)
        if found:
            return found
    return ""


def _same_path(left: str, right: Path) -> bool:
    return os.path.normcase(str(Path(left))) == os.path.normcase(str(right))


def command_with_tessdata(cmd: list[str], tessdata: str) -> list[str]:
    """Append ``--tessdata-dir`` as its own argv element.

    pytesseract splits the config string with ``shlex`` in non-POSIX mode on
    Windows, which keeps quote characters inside the path. A separate argv
    element keeps a user-profile path that contains spaces intact.
    """

    if _TESSDATA_FLAG in cmd:
        return list(cmd)
    return [*cmd, _TESSDATA_FLAG, tessdata]


def _is_tesseract_command(executable: object) -> bool:
    return Path(str(executable)).name.lower() in _TESSERACT_NAMES


def _popen_with_tessdata(cmd, *args, **kwargs):
    if (
        _active_tessdata
        and isinstance(cmd, (list, tuple))
        and cmd
        and _is_tesseract_command(cmd[0])
    ):
        cmd = command_with_tessdata(list(cmd), _active_tessdata)
    if _pop_saved is None:
        raise RuntimeError("tesseract subprocess hook is not installed")
    return _pop_saved(cmd, *args, **kwargs)


def _wrap_run(original, tessdata: str):
    def run_tesseract(*args, **kwargs):
        global _pop_depth, _pop_saved
        import subprocess

        with _pop_lock:
            if _pop_depth == 0:
                _pop_saved = subprocess.Popen
                subprocess.Popen = _popen_with_tessdata
            _pop_depth += 1
        try:
            return original(*args, **kwargs)
        finally:
            with _pop_lock:
                _pop_depth -= 1
                if _pop_depth == 0 and _pop_saved is not None:
                    subprocess.Popen = _pop_saved
                    _pop_saved = None

    run_tesseract.__panelcad_original__ = original  # type: ignore[attr-defined]
    run_tesseract.__panelcad_tessdata__ = tessdata  # type: ignore[attr-defined]
    return run_tesseract


def _activate(pytesseract, tessdata: Path) -> None:
    global _active_tessdata
    module = pytesseract.pytesseract
    current = module.run_tesseract
    original = getattr(current, "__panelcad_original__", current)
    text = str(tessdata)
    with _pop_lock:
        _active_tessdata = text
    module.run_tesseract = _wrap_run(original, text)


def _deactivate(pytesseract) -> None:
    global _active_tessdata
    module = pytesseract.pytesseract
    current = module.run_tesseract
    original = getattr(current, "__panelcad_original__", None)
    with _pop_lock:
        _active_tessdata = None
    if original is not None:
        module.run_tesseract = original


def _deactivate_if_loaded() -> None:
    import sys

    module = sys.modules.get("pytesseract")
    if module is not None:
        _deactivate(module)


def configure_tesseract() -> str:
    """Point pytesseract at :func:`find_tesseract`. Empty when nothing is installed."""

    path = find_tesseract()
    if not path:
        _deactivate_if_loaded()
        return ""
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = path
    bundled = bundled_executable()
    tessdata = bundled_tessdata_dir()
    if bundled is not None and tessdata is not None and _same_path(path, bundled):
        _activate(pytesseract, tessdata)
    else:
        _deactivate(pytesseract)
    return path
