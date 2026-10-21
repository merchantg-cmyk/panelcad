"""Drive paths and paths longer than the classic Windows limit.

A synced folder (``G:\\My Drive\\PanelCAD\\Projects\\...`` or
``D:\\Shared\\PanelCAD\\...``) is an ordinary Windows path. A path of 260
characters or more is opened with the ``\\\\?\\`` prefix. Settings and the
window keep the path a person can read. The prefix is only for the OS call.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

# Classic MAX_PATH, including the terminator. Longer paths need ``\\?\``.
_MAX_PATH = 260


def human_path(path: Path | str) -> str:
    """Path without a ``\\\\?\\`` prefix. Empty when ``path`` is blank."""

    text = str(path or "").strip().strip('"')
    lowered = text.lower()
    if lowered.startswith("\\\\?\\unc\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    if lowered.startswith("//?/unc/"):
        return "\\\\" + text[8:].replace("/", "\\")
    if lowered.startswith("//?/"):
        return text[4:]
    return text


def stored_path(path: Path | str) -> str:
    """Form written to settings and shown in the menu.

    A Windows path uses backslashes, so
    ``G:/My Drive/PanelCAD/Projects/job.pcad.yaml`` and
    ``G:\\My Drive\\PanelCAD\\Projects\\job.pcad.yaml`` are one entry. A POSIX
    path is unchanged.
    """

    text = human_path(path)
    if not text:
        return ""
    if _is_windows(text):
        return text.replace("/", "\\")
    return text


def display_name(path: Path | str) -> str:
    """File name. Backslashes count, including on Linux."""

    text = stored_path(path)
    if not text:
        return ""
    if _is_windows(text):
        return text.rstrip("\\").rsplit("\\", 1)[-1]
    return text.rstrip("/").rsplit("/", 1)[-1]


def path_key(path: Path | str) -> str:
    """Identity for the recent list. Windows paths are case-insensitive."""

    text = stored_path(path)
    if not text:
        return ""
    if _is_windows(text):
        return text.rstrip("\\").casefold()
    return os.path.normcase(os.path.normpath(text))


def extended_windows_path(path: Path | str) -> str:
    """``\\\\?\\`` form of a long Windows path. Short paths stay readable.

    This does not look at the host OS. Callers that open a file use
    :func:`io_path`, which applies the prefix only on Windows.
    """

    human = stored_path(path)
    if not human or not _is_windows(human):
        return str(path or "")
    if len(human) < _MAX_PATH:
        return human
    if human.startswith("\\\\"):
        return "\\\\?\\UNC\\" + human[2:]
    return "\\\\?\\" + human


def io_path(path: Path | str) -> str:
    """Path the OS can open. Non-Windows hosts keep the string as given."""

    text = str(path or "")
    if os.name != "nt":
        if text.startswith("\\\\?\\") or text.lower().startswith("//?/"):
            return human_path(text)
        return text
    if not _is_windows(human_path(text)):
        return text
    return extended_windows_path(text)


def project_presence(path: Path | str) -> str:
    """``file``, ``missing``, or ``unavailable``.

    ``unavailable`` means the check itself failed (permission, or a path the
    OS rejected). That is not the same as a file which is gone.
    """

    text = str(path or "").strip()
    if not text:
        return "missing"
    # A ``G:\`` or ``\\server`` path is not a file on this computer. Asking
    # Linux to stat it throws "file name too long" because ``\`` is not a
    # separator there. On Windows the check below uses the real path.
    if os.name != "nt" and _is_windows(human_path(text)):
        return "missing"
    try:
        candidate = Path(io_path(text))
    except (OSError, ValueError):
        return "unavailable"
    try:
        if candidate.is_file():
            return "file"
    except OSError as exc:
        err = getattr(exc, "errno", None)
        if err in {errno.ENOENT, errno.ENOTDIR}:
            return "missing"
        return "unavailable"
    return "missing"


def _is_windows(text: str) -> bool:
    if text.startswith(("\\\\", "//")):
        return True
    return len(text) >= 2 and text[0].isalpha() and text[1] == ":"
