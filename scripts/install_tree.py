#!/usr/bin/env python3
"""Copy a PanelCAD tree onto an existing install.

This is the copy step used by ``scripts/Install-PanelCAD.ps1``. It is the
equivalent of ``robocopy /E /IS /IT``: every shipped file is written even
when the destination already has the same size and the same timestamp.
``robocopy /E`` alone treats those files as unchanged and skips them. A
release zip that stamped every entry with one date then left same-size
edits (``version.py`` ``0.3.0`` vs ``0.4.0``, docs, ``AGENTS.md``) stale.

After the copy, ``__pycache__`` directories and ``*.pyc`` / ``*.pyo`` files
are removed only under the destination's ``src``, ``tests``, and ``docs``
trees. A matching timestamp on ``version.py`` makes Python trust a stale
``version.cpython-*.pyc`` and keep reporting the old version. Drawings and
any other files outside those three trees are not deleted. This is not a
mirror: extra files already in the install folder stay.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

# Same skips the Windows installer used with robocopy /XD and /XF.
# Extra files already in the destination (jobs, notes) are never removed.
EXCLUDE_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        ".venvs",
        "__pycache__",
        "reference",
        ".pytest_cache",
        ".ruff_cache",
        "panelcad.egg-info",
        "dist",
        ".sharpen",
        ".grok",
        ".mypy_cache",
    }
)
BYTECODE_SUFFIXES = frozenset({".pyc", ".pyo"})
PURGE_ROOTS = ("src", "tests", "docs")
# Activity journals next to shipped examples are user history. A release may
# carry a fresh copy; an upgrade must not replace one the shop already has.
_EXAMPLE_JOURNAL_SUFFIXES = (
    ".ai.jsonl",
    ".ai-in.jsonl",
    ".ai-prompt.jsonl",
)


@dataclass(frozen=True)
class UpgradeStats:
    copied: int
    pyc_files: int
    cache_dirs: int


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _skip_file(path: Path) -> bool:
    name = path.name
    suffix = path.suffix.lower()
    if suffix in BYTECODE_SUFFIXES:
        return True
    # A leftover setup archive sitting next to the extracted tree must not
    # be copied back into the install folder.
    if suffix in {".zip", ".sha256"}:
        return True
    if name == "PanelCAD-Setup.tar.gz":
        return True
    if name.endswith(".panelcad-new"):
        return True
    return False


def _atomic_write(source: Path, target: Path) -> None:
    """Always replace ``target`` bytes, including same size and same mtime."""
    tmp = target.with_name(target.name + ".panelcad-new")
    try:
        tmp.write_bytes(source.read_bytes())
        shutil.copystat(source, tmp, follow_symlinks=False)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def _copy_file(source_file: Path, dest_root: Path, rel: Path) -> None:
    current = dest_root
    for part in rel.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise OSError(
                f"Refusing to copy through symlink {current}. "
                "The install folder must not redirect program files elsewhere."
            )
        if current.exists() and not current.is_dir():
            raise OSError(f"{current} is a file; expected a directory.")
        current.mkdir(exist_ok=True)
    parent_real = current.resolve()
    if not _is_within(parent_real, dest_root):
        raise OSError(f"Refusing to write outside {dest_root}")
    target = current / rel.name
    if target.is_symlink():
        # Drop the link itself. Do not follow it and edit the target.
        target.unlink()
    _atomic_write(source_file, target)


def _existing_example_journal(rel: Path, dest_root: Path) -> bool:
    """True when dest already has this examples/ AI journal and it should stay."""
    if not rel.parts or rel.parts[0] != "examples":
        return False
    if not any(rel.name.endswith(suffix) for suffix in _EXAMPLE_JOURNAL_SUFFIXES):
        return False
    target = dest_root.joinpath(rel)
    return target.is_file() and not target.is_symlink()


def _copy_tree(source: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    dest_root = dest.resolve()
    copied = 0
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(dirpath)
        kept: list[str] = []
        for dirname in dirnames:
            if dirname in EXCLUDE_DIR_NAMES:
                continue
            child = current / dirname
            if child.is_symlink():
                continue
            kept.append(dirname)
        dirnames[:] = kept
        for filename in filenames:
            path = current / filename
            if path.is_symlink() or _skip_file(path):
                continue
            rel = path.relative_to(source)
            if _existing_example_journal(rel, dest_root):
                continue
            _copy_file(path, dest_root, rel)
            copied += 1
    return copied


def _purge_bytecode(dest: Path) -> tuple[int, int]:
    """Delete interpreter caches under src, tests, and docs only."""
    dest_root = dest.resolve()
    pyc_count = 0
    dir_count = 0
    for name in PURGE_ROOTS:
        root = dest / name
        if not root.exists() or root.is_symlink():
            continue
        root_real = root.resolve()
        if not _is_within(root_real, dest_root):
            continue
        cache_dirs: list[Path] = []
        pyc_files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            kept = []
            for dirname in dirnames:
                child = current / dirname
                if child.is_symlink():
                    continue
                if dirname == "__pycache__":
                    cache_dirs.append(child)
                kept.append(dirname)
            dirnames[:] = kept
            for filename in filenames:
                path = current / filename
                if path.is_symlink():
                    continue
                if path.suffix.lower() in BYTECODE_SUFFIXES:
                    pyc_files.append(path)
        for path in pyc_files:
            try:
                real = path.resolve()
            except OSError:
                continue
            if not _is_within(real, root_real):
                continue
            path.unlink()
            pyc_count += 1
        for path in sorted(cache_dirs, key=lambda item: len(item.parts), reverse=True):
            if path.is_symlink() or not path.exists():
                continue
            try:
                real = path.resolve()
            except OSError:
                continue
            if not _is_within(real, root_real):
                continue
            shutil.rmtree(path)
            dir_count += 1
    return pyc_count, dir_count


def upgrade_tree(source: Path, dest: Path) -> UpgradeStats:
    """Overwrite program files from ``source`` onto ``dest`` and drop bytecode.

    When ``source`` and ``dest`` are the same folder (the zip was extracted
    directly onto the install), the copy is skipped and bytecode is still
    removed. Copying a folder into its own subfolder is refused.
    """
    source = source.resolve()
    dest = dest.resolve()
    if not source.is_dir():
        raise OSError(f"Source folder does not exist: {source}")
    if source != dest and _is_within(dest, source):
        raise OSError(
            f"Refusing to copy {source} into {dest}: the destination is inside the source."
        )
    copied = 0
    if source != dest:
        copied = _copy_tree(source, dest)
    else:
        dest.mkdir(parents=True, exist_ok=True)
    pyc_files, cache_dirs = _purge_bytecode(dest)
    return UpgradeStats(copied=copied, pyc_files=pyc_files, cache_dirs=cache_dirs)


def _norm_install_path(value: str) -> str:
    text = (value or "").strip().strip('"')
    if not text:
        return ""
    windows = bool(re.match(r"^[A-Za-z]:", text)) or text.startswith(("\\\\", "//"))
    if windows:
        text = text.replace("/", "\\")
        while len(text) > 3 and text.endswith("\\"):
            text = text[:-1]
        return text.casefold()
    return os.path.normcase(os.path.normpath(text))


def _path_under(child: str, parent: str) -> bool:
    child_n = _norm_install_path(child)
    parent_n = _norm_install_path(parent)
    if not child_n or not parent_n or child_n == parent_n:
        return False
    windows = "\\" in child_n or (len(child_n) > 1 and child_n[1:2] == ":")
    if windows:
        return child_n.startswith(parent_n.rstrip("\\") + "\\")
    return child_n.startswith(parent_n.rstrip("/\\") + os.sep)


def shortcut_action(
    *,
    existing_workdir: str,
    install_root: str,
    desktop: str,
    user_profile: str,
    shortcut_exists: bool = False,
) -> str:
    """Return ``create``, ``update``, or ``leave``.

    A missing shortcut is created when the desktop belongs to this profile.
    An existing shortcut with an empty working directory is left alone, as is
    one that points at a different install. An existing shortcut is updated
    only when its working directory is this install.
    """

    if shortcut_exists or (existing_workdir or "").strip():
        current = _norm_install_path(existing_workdir)
        target = _norm_install_path(install_root)
        if current and current == target:
            return "update"
        return "leave"
    if desktop and _path_under(desktop, user_profile):
        return "create"
    return "leave"


def shortcut_cli_args(
    *,
    existing_workdir: str,
    install_root: str,
    desktop: str,
    user_profile: str,
    shortcut_exists: bool,
) -> list[str]:
    """Flags for ``--shortcut-action``. Never a bare ``--existing``.

    PowerShell drops an empty string when it splats a native program, so
    ``--existing`` followed by ``""`` arrives as ``--existing`` with no value
    and argparse exits 2. Omit ``--existing`` when the working directory is
    empty. Pass ``--shortcut-exists`` only when the ``.lnk`` file is there.
    """

    args = [
        "--shortcut-action",
        "--install-root",
        install_root,
        "--desktop",
        desktop,
        "--user-profile",
        user_profile,
    ]
    existing = (existing_workdir or "").strip()
    if existing:
        args.extend(["--existing", existing])
    if shortcut_exists:
        args.append("--shortcut-exists")
    return args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shortcut-action",
        action="store_true",
        help="Print create, update, or leave for the desktop shortcut",
    )
    parser.add_argument(
        "--existing",
        nargs="?",
        const="",
        default="",
        help="WorkingDirectory of an existing shortcut. Omitted when that directory is empty.",
    )
    parser.add_argument("--install-root", default="", help="Install folder being updated")
    parser.add_argument("--desktop", default="", help="Desktop folder that would hold the shortcut")
    parser.add_argument("--user-profile", default="", help="USERPROFILE for this install")
    parser.add_argument(
        "--shortcut-exists",
        action="store_true",
        help="The .lnk file is already on that desktop",
    )
    parser.add_argument("--source", type=Path, help="Extracted release or checkout")
    parser.add_argument("--dest", type=Path, help="Install folder to update")
    args = parser.parse_args(argv)
    if args.shortcut_action:
        print(
            shortcut_action(
                existing_workdir=args.existing,
                install_root=args.install_root,
                desktop=args.desktop,
                user_profile=args.user_profile,
                shortcut_exists=args.shortcut_exists,
            )
        )
        return 0
    if args.source is None or args.dest is None:
        parser.error("--source and --dest are required")
    try:
        stats = upgrade_tree(args.source, args.dest)
    except OSError as exc:
        print(f"Could not copy the program: {exc}", file=sys.stderr)
        return 1
    print(
        f"Copied {stats.copied} file(s) to {args.dest}. "
        f"Removed {stats.pyc_files} bytecode file(s) and {stats.cache_dirs} __pycache__ "
        f"folder(s) under src, tests, and docs."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
