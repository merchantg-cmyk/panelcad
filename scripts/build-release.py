#!/usr/bin/env python3
"""Build one versioned PanelCAD setup zip and its SHA-256 sidecar.

Usage (from the repo root):

    python scripts/build-release.py
    python scripts/build-release.py --output-dir dist --artifacts-dir /opt/cursor/artifacts

The version is read from src/panelcad/version.py (must match pyproject.toml).
The zip is meant to be copied as a single file onto a Windows PC: extract it,
then double-click Install-PanelCAD.cmd. That installs or upgrades
%USERPROFILE%\\PanelCAD without wiping user drawings or the venv.

Shop builds read unset PANELCAD_RELEASE_* values from
%USERPROFILE%\\.panelcad\\release.env and then release.local.env in the
repo root. Those files are gitignored. A variable already set in the
environment wins. Nothing in this repository holds the webhook, the bot
token, the channel id, or the feed URL.

PANELCAD_RELEASE_BUG_WEBHOOK, when set, must be an
https://hooks.slack.com/services/ URL. The build embeds that incoming
webhook in the zip and refuses a bot token or any other host. When the
variable is unset, the build warns and ships no endpoint file. The URL is
never written into the checkout.

PANELCAD_RELEASE_SLACK_BOT_TOKEN, when set, must be an xoxb-… bot token.
PANELCAD_RELEASE_SLACK_CHANNEL must be the Slack channel id that token is
allowed to post to. The build embeds both in the zip as
src/panelcad/diag/_report_bot.txt and does not write that file into the
checkout, print the token, or log the channel. A token without a channel
id fails the build. When the token is unset, the build warns and ships no
bot file.

PANELCAD_RELEASE_UPDATE_FEED, when set, must be an https URL (a Google
Drive uc?export=download&id= link is fine). The build embeds it as the
default update feed and does not write it into the checkout. When the
variable is unset, the build warns and the checker stays off until a feed
is set.

Pass --public, or set PANELCAD_PUBLIC_BUILD=1, to build a public zip. That
build does not read the release env files and does not embed a webhook, a
bot, or a shop feed, even when those variables are set. Sending stays off.
Update checks offer the public GitHub Releases for
merchantg-cmyk/panelcad. PANELCAD_GITHUB_REPOSITORY=owner/name overrides
that default.

Zip timestamps are per file, not one fixed date. Each entry uses the UTC
time of the git commit that last changed that path, clamped to
SOURCE_DATE_EPOCH when it is set and otherwise to the HEAD commit time.
A single shared date (the old 2026-01-01 default) made same-size files
look unchanged to robocopy. Unchanged paths keep their older commit times,
so two builds of the same commit still match. See docs/RELEASE.md.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

INCLUDE_DIRS = (
    "src",
    "libraries",
    "examples",
    "scripts",
    "docs",
    "tests",
    "agents",
)
INCLUDE_FILES = (
    "pyproject.toml",
    "README.md",
    "CHANGELOG.md",
    "AGENTS.md",
    "DESIGN.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "Install-PanelCAD.cmd",
    ".gitignore",
)
# Window, installer shortcut, and taskbar icon. The zip must contain these
# even if a later filter would skip a binary under src/.
BRAND_ASSETS = (
    "src/panelcad/assets/panelcad.svg",
    "src/panelcad/assets/panelcad.ico",
    "src/panelcad/assets/panelcad-16.png",
    "src/panelcad/assets/panelcad-24.png",
    "src/panelcad/assets/panelcad-32.png",
    "src/panelcad/assets/panelcad-48.png",
    "src/panelcad/assets/panelcad-64.png",
    "src/panelcad/assets/panelcad-128.png",
    "src/panelcad/assets/panelcad-256.png",
)
EXCLUDE_DIR_NAMES = {
    ".git",
    ".venv",
    ".venvs",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".sharpen",
    ".grok",
    "dist",
    "panelcad.egg-info",
    ".mypy_cache",
    "reference",
}
EXCLUDE_FILE_NAMES = {
    "PanelCAD-Setup.zip",
    "PanelCAD-Setup.tar.gz",
}
CRLF_SUFFIXES = {".cmd", ".ps1", ".bat"}
EXEC_NAMES = {"check", "setup.sh", "build-release.py"}
ENDPOINT_REL = "src/panelcad/diag/_report_endpoint.txt"
BOT_REL = "src/panelcad/diag/_report_bot.txt"
FEED_REL = "src/panelcad/update/_release_feed.txt"


def read_version(root: Path) -> str:
    text = (root / "src" / "panelcad" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, flags=re.M)
    if not match:
        raise SystemExit("Could not read __version__ from src/panelcad/version.py")
    return match.group(1)


def read_pyproject_version(root: Path) -> str | None:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.M)
    return match.group(1) if match else None


# ZIP DOS dates cannot represent times before 1980-01-01 00:00:00 UTC.
ZIP_EPOCH = 315532800
_COMMIT_MARK = "@COMMIT@"


@dataclass(frozen=True)
class ZipClock:
    """Per-file commit times plus a reproducibility cap.

    ``stamps`` maps a repo-relative posix path to the unix time of the
    newest commit that lists that path. ``cap`` is ``SOURCE_DATE_EPOCH``
    when set, otherwise the HEAD commit time. Entry times are
    ``min(stamp, cap)`` so a rebuild does not follow the wall clock and
    a dirty tree cannot stamp files newer than the commit being released.
    """

    cap: int | None
    stamps: dict[str, int]


def source_date_epoch() -> int | None:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise SystemExit(
            f"SOURCE_DATE_EPOCH must be an integer unix timestamp, got {raw!r}"
        ) from exc


def parse_git_name_log(text: str) -> dict[str, int]:
    """Map path -> commit time from ``git log --pretty=format:@COMMIT@%ct --name-only``.

    The first time a path appears is the newest commit that touched it.
    A commit with no names (an empty or merge commit) is only a marker line.
    """
    stamps: dict[str, int] = {}
    current: int | None = None
    for line in text.splitlines():
        if line.startswith(_COMMIT_MARK):
            raw = line[len(_COMMIT_MARK) :].strip()
            if raw.isdigit():
                current = int(raw)
            continue
        rel = line.strip().replace("\\", "/")
        if not rel or current is None or rel in stamps:
            continue
        stamps[rel] = current
    return stamps


def _git_toplevel(root: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip()).resolve()


def git_file_stamps(root: Path) -> tuple[int | None, dict[str, int]]:
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "core.quotepath=false",
                "log",
                "--name-only",
                f"--pretty=format:{_COMMIT_MARK}%ct",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None, {}
    if proc.returncode != 0:
        return None, {}
    head: int | None = None
    for line in proc.stdout.splitlines():
        if line.startswith(_COMMIT_MARK):
            raw = line[len(_COMMIT_MARK) :].strip()
            if raw.isdigit():
                head = int(raw)
                break
    return head, parse_git_name_log(proc.stdout)


def load_zip_clock(root: Path) -> ZipClock:
    """Choose reproducible per-file zip times for ``root``.

    A checkout uses git history. A folder that is not its own git toplevel
    (a temp dir inside this repo, or an export without ``.git``) does not
    borrow the parent repository's history. With no git metadata and no
    ``SOURCE_DATE_EPOCH``, callers fall back to each file's mtime.
    """
    root = root.resolve()
    cap = source_date_epoch()
    stamps: dict[str, int] = {}
    head: int | None = None
    toplevel = _git_toplevel(root)
    if toplevel is not None and toplevel == root:
        head, stamps = git_file_stamps(root)
    if cap is None:
        cap = head
    return ZipClock(cap=cap, stamps=stamps)


def to_zip_date_time(timestamp: int) -> tuple[int, int, int, int, int, int]:
    moment = time.gmtime(int(timestamp))
    second = moment.tm_sec - (moment.tm_sec % 2)
    return (
        moment.tm_year,
        moment.tm_mon,
        moment.tm_mday,
        moment.tm_hour,
        moment.tm_min,
        second,
    )


def entry_unix_time(clock: ZipClock, rel: str | None, fallback_mtime: int | None) -> int:
    ts: int | None = None
    if rel:
        ts = clock.stamps.get(rel.replace("\\", "/"))
    if ts is None:
        if clock.cap is not None:
            ts = clock.cap
        elif fallback_mtime is not None:
            ts = int(fallback_mtime)
        else:
            ts = ZIP_EPOCH
    if clock.cap is not None:
        ts = min(ts, clock.cap)
    if ts < ZIP_EPOCH:
        ts = ZIP_EPOCH
    return ts


def entry_date_time(
    clock: ZipClock, rel: str | None, fallback_mtime: int | None
) -> tuple[int, int, int, int, int, int]:
    return to_zip_date_time(entry_unix_time(clock, rel, fallback_mtime))


def _endpoint_tools(root: Path):
    """Load the endpoint helper without importing the PanelCAD package."""

    import importlib.util

    path = root / "src" / "panelcad" / "diag" / "endpoint.py"
    spec = importlib.util.spec_from_file_location("_panelcad_endpoint_build", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Missing {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def public_build_requested() -> bool:
    """True when this process asked for a public zip with reporting and updates off."""

    flag = os.environ.get("PANELCAD_PUBLIC_BUILD", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


PUBLIC_BUILD_NOTICE = (
    "Public build: bug-report webhook and Slack bot are off.\n"
    "The shop update feed is not embedded.\n"
    "Update checks offer download from the public GitHub Releases.\n"
    "The public repository is merchantg-cmyk/panelcad.\n"
)
PUBLIC_MARKER_REL = "src/panelcad/update/_release_public.txt"
PUBLIC_MARKER_PAYLOAD = b"public\n"


def load_release_env(root: Path) -> None:
    """Fill unset PANELCAD_RELEASE_* keys from gitignored files.

    Existing environment variables win. Values are not printed.
    """

    home = Path.home() / ".panelcad" / "release.env"
    for path in (home, root / "release.local.env"):
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for line in raw.splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export ") :].strip()
            if not key.startswith("PANELCAD_RELEASE_"):
                continue
            value = value.strip().strip('"').strip("'")
            if os.environ.get(key, "").strip() or not value:
                continue
            os.environ[key] = value


def release_endpoint_payload(root: Path, *, public: bool = False) -> bytes | None:
    """Bytes to embed, or None when the release has no built-in endpoint.

    A value that is not an incoming webhook fails the build. The URL is not
    printed. A file already sitting in the tree is not what gets shipped.
    A public build embeds nothing.
    """

    if public:
        return None
    tools = _endpoint_tools(root)
    raw = os.environ.get(tools.RELEASE_WEBHOOK_ENV, "").strip()
    if not raw:
        print(tools.MISSING_ENDPOINT_WARNING, file=sys.stderr, end="")
        return None
    reason = tools.classify_release_webhook(raw)
    if reason:
        raise SystemExit(
            "PANELCAD_RELEASE_BUG_WEBHOOK was refused "
            f"({reason}). Use an https://hooks.slack.com/services/ URL. "
            "A bot token is never embedded."
        )
    return (raw + "\n").encode("utf-8")


def release_bot_payload(root: Path, *, public: bool = False) -> bytes | None:
    """Bytes to embed, or None when the release has no built-in bot.

    Only an ``xoxb-…`` token is accepted. The token and the channel id are
    not printed. A file already sitting in the tree is not what gets shipped.
    The channel id comes from ``PANELCAD_RELEASE_SLACK_CHANNEL``. A public
    build embeds nothing.
    """

    if public:
        return None
    tools = _endpoint_tools(root)
    raw = os.environ.get(tools.RELEASE_BOT_TOKEN_ENV, "").strip()
    if not raw:
        print(tools.MISSING_BOT_WARNING, file=sys.stderr, end="")
        return None
    reason = tools.classify_release_bot_token(raw)
    if reason:
        raise SystemExit(
            "PANELCAD_RELEASE_SLACK_BOT_TOKEN was refused "
            f"({reason}). Use a Slack bot token. "
            "The value was not embedded."
        )
    channel = os.environ.get(tools.RELEASE_BOT_CHANNEL_ENV, "").strip()
    channel_reason = tools.classify_release_channel(channel)
    if channel_reason:
        raise SystemExit(
            "PANELCAD_RELEASE_SLACK_CHANNEL was refused "
            f"({channel_reason}). {tools.MISSING_CHANNEL_ERROR}"
        )
    return (raw + "\n" + channel + "\n").encode("utf-8")


def _feed_tools(root: Path):
    """Load the built-in feed helper without importing the PanelCAD package."""

    import importlib.util

    path = root / "src" / "panelcad" / "update" / "builtin.py"
    spec = importlib.util.spec_from_file_location("_panelcad_feed_build", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Missing {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def release_feed_payload(root: Path, *, public: bool = False) -> bytes | None:
    """Bytes to embed, or None when the release has no built-in feed.

    A value that is not https fails the build. The URL is not printed. A
    file already sitting in the tree is not what gets shipped. A public
    build embeds nothing.
    """

    if public:
        return None
    tools = _feed_tools(root)
    raw = os.environ.get(tools.RELEASE_FEED_ENV, "").strip()
    if not raw:
        print(tools.MISSING_FEED_WARNING, file=sys.stderr, end="")
        return None
    reason = tools.classify_release_feed(raw)
    if reason:
        raise SystemExit(
            "PANELCAD_RELEASE_UPDATE_FEED was refused "
            f"({reason}). Use an https URL. The value was not embedded."
        )
    return (raw + "\n").encode("utf-8")


def should_skip_file(path: Path) -> bool:
    name = path.name
    if name in {"_report_endpoint.txt", "_report_bot.txt", "_release_feed.txt", "_release_public.txt"}:
        return True
    if name in EXCLUDE_FILE_NAMES:
        return True
    if name.startswith("PanelCAD-") and name.endswith(("-setup.zip", "-setup.zip.sha256")):
        return True
    if name.endswith((".pyc", ".pyo")):
        return True
    return False


def _snapshot_module():
    """The public-snapshot helper. Loaded by path so a release build stays standalone."""

    import importlib.util

    path = Path(__file__).resolve().parent / "public-snapshot.py"
    spec = importlib.util.spec_from_file_location("_panelcad_public_snapshot", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Missing {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def iter_release_files(root: Path, *, public: bool = False) -> list[tuple[str, Path]]:
    snap = _snapshot_module() if public else None
    items: list[tuple[str, Path]] = []
    for name in INCLUDE_FILES:
        path = root / name
        if not path.is_file():
            continue
        rel = name.replace("\\", "/")
        if snap is not None and snap.excluded(rel):
            continue
        items.append((rel, path))
    for dirname in INCLUDE_DIRS:
        base = root / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if any(part in EXCLUDE_DIR_NAMES for part in path.relative_to(root).parts):
                continue
            if should_skip_file(path):
                continue
            rel = path.relative_to(root).as_posix()
            if snap is not None and snap.excluded(rel):
                continue
            items.append((rel, path))
    present = {rel for rel, _path in items}
    for rel in BRAND_ASSETS:
        if rel in present:
            continue
        path = root / rel
        if path.is_file():
            items.append((rel, path))
    items.sort(key=lambda item: item[0].lower())
    return items


def missing_brand_assets(root: Path) -> list[str]:
    """Icon files the Windows shortcuts and the window need inside the zip."""

    return [rel for rel in BRAND_ASSETS if not (root / rel).is_file()]


def file_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix.lower() in CRLF_SUFFIXES:
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")
    return data


def unix_mode(rel: str, path: Path) -> int:
    if path.suffix == ".sh" or path.name in EXEC_NAMES:
        return 0o755
    return 0o644


def install_text(version: str) -> str:
    return (
        f"PanelCAD {version}\n"
        "\n"
        "Install or upgrade on Windows 10/11:\n"
        "  1. Copy this zip onto the PC (USB, share, or browser download).\n"
        "  2. Right-click -> Extract All.\n"
        f"  3. Open the PanelCAD-{version} folder.\n"
        "  4. Double-click Install-PanelCAD.cmd.\n"
        "\n"
        "Close PanelCAD first if it is already open. Run the installer from this\n"
        "extracted folder, not from an older copy already in %USERPROFILE%\\PanelCAD.\n"
        "\n"
        "The installer copies the program to %USERPROFILE%\\PanelCAD and overwrites\n"
        "program files even when the size and timestamp match. It deletes\n"
        "__pycache__ folders and stale .pyc files under that install's src, tests,\n"
        "and docs folders only. The virtual environment stays at\n"
        "%USERPROFILE%\\.venvs\\panelcad. Drawings you saved (*.pcad.yaml) and any\n"
        "extra files outside those three trees are left alone.\n"
        "An existing example AI journal (examples\\*.pcad.ai.jsonl, and the\n"
        "inbox and prompt journals beside those examples) is not overwritten.\n"
        "\n"
        "After pip install, the installer runs:\n"
        '  python -c "import panelcad; print(panelcad.__version__)"\n'
        f"and stops if the printed version is not {version}.\n"
        "\n"
        "Unattended install (no pause, and the drawing window is not opened):\n"
        "  Install-PanelCAD.cmd /quiet\n"
        "  or set PANELCAD_INSTALL_QUIET=1\n"
        "The desktop shortcut is still created. Next time, double-click PanelCAD.\n"
        "\n"
        "Do not use Drive split archives or Unpack-*.cmd. This zip is the whole release.\n"
        "\n"
        "To publish it for Settings -> Updates, run:\n"
        "  panelcad release publish-feed --zip <this zip> --feed <folder or json> --notes \"...\"\n"
        "Pass --download-url when the zip is fetched from an https link, including a\n"
        "Google Drive direct download (uc?export=download&id=...).\n"
        "The feed can be an https URL or a file in a synced folder. See docs/RELEASE.md.\n"
        "\n"
        f"Check the hash before you install:  {sha_name(version)}\n"
    )


def sha_name(version: str) -> str:
    return f"PanelCAD-{version}-setup.zip.sha256"


def zip_name(version: str) -> str:
    return f"PanelCAD-{version}-setup.zip"


def add_zip_bytes(
    zf: zipfile.ZipFile,
    arcname: str,
    data: bytes,
    mode: int,
    date_time: tuple[int, int, int, int, int, int],
) -> None:
    info = zipfile.ZipInfo(arcname, date_time=date_time)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (mode & 0xFFFF) << 16
    zf.writestr(info, data)


def build_release(root: Path, output_dir: Path, *, public: bool = False) -> tuple[Path, Path]:
    missing = missing_brand_assets(root)
    if missing:
        raise SystemExit(
            "Release is missing PanelCAD icon assets:\n  " + "\n  ".join(missing)
        )
    version = read_version(root)
    py_version = read_pyproject_version(root)
    if py_version and py_version != version:
        raise SystemExit(
            f"Version mismatch: src/panelcad/version.py={version!r} "
            f"pyproject.toml={py_version!r}"
        )

    public = public or public_build_requested()
    if public:
        print(PUBLIC_BUILD_NOTICE, file=sys.stderr, end="")
    endpoint_payload = release_endpoint_payload(root, public=public)
    bot_payload = release_bot_payload(root, public=public)
    feed_payload = release_feed_payload(root, public=public)

    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / zip_name(version)
    checksum = output_dir / sha_name(version)
    prefix = f"PanelCAD-{version}"
    files = iter_release_files(root, public=public)
    clock = load_zip_clock(root)

    if archive.exists():
        archive.unlink()
    if checksum.exists():
        checksum.unlink()

    with zipfile.ZipFile(archive, "w") as zf:
        add_zip_bytes(
            zf,
            f"{prefix}/INSTALL.txt",
            install_text(version).encode("utf-8"),
            0o644,
            entry_date_time(clock, "INSTALL.txt", None),
        )
        for rel, path in files:
            add_zip_bytes(
                zf,
                f"{prefix}/{rel}",
                file_bytes(path),
                unix_mode(rel, path),
                entry_date_time(clock, rel, int(path.stat().st_mtime)),
            )
        if endpoint_payload is not None:
            add_zip_bytes(
                zf,
                f"{prefix}/{ENDPOINT_REL}",
                endpoint_payload,
                0o644,
                entry_date_time(clock, ENDPOINT_REL, None),
            )
        if bot_payload is not None:
            add_zip_bytes(
                zf,
                f"{prefix}/{BOT_REL}",
                bot_payload,
                0o644,
                entry_date_time(clock, BOT_REL, None),
            )
        if feed_payload is not None:
            add_zip_bytes(
                zf,
                f"{prefix}/{FEED_REL}",
                feed_payload,
                0o644,
                entry_date_time(clock, FEED_REL, None),
            )
        if public:
            add_zip_bytes(
                zf,
                f"{prefix}/{PUBLIC_MARKER_REL}",
                PUBLIC_MARKER_PAYLOAD,
                0o644,
                entry_date_time(clock, PUBLIC_MARKER_REL, None),
            )

    if public:
        problems = public_zip_problems(archive, root)
        if problems:
            archive.unlink(missing_ok=True)
            raise SystemExit(
                "Public zip failed the snapshot self-check:\n  " + "\n  ".join(problems[:40])
            )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    return archive, checksum


def public_zip_problems(archive: Path, root: Path) -> list[str]:
    """Self-check of a public zip. Paths are the files inside the version folder."""

    snap = _snapshot_module()
    banned = snap.load_banned_words(root / "scripts" / "public-snapshot.banned.txt")
    if not banned:
        return ["banned list missing: scripts/public-snapshot.banned.txt"]
    tmp = Path(tempfile.mkdtemp(prefix="panelcad-public-zip-"))
    try:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp)
        dirs = [path for path in tmp.iterdir() if path.is_dir()]
        target = dirs[0] if len(dirs) == 1 else tmp
        return snap.self_check(target, banned)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def copy_into(path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / path.name
    target.write_bytes(path.read_bytes())
    return target


def default_artifacts_dir() -> Path | None:
    raw = os.environ.get("PANELCAD_ARTIFACTS_DIR")
    if raw:
        return Path(raw)
    candidate = Path("/opt/cursor/artifacts")
    if candidate.is_dir() and os.access(candidate, os.W_OK):
        return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None, help="Repository root")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Where to write the zip and sha256 (default: <root>/dist)",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=None,
        help="Optional extra folder to copy the finished files into",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="Do not embed a bug webhook, Slack bot, or update feed",
    )
    args = parser.parse_args(argv)

    root = (args.root or Path(__file__).resolve().parent.parent).resolve()
    output_dir = (args.output_dir or (root / "dist")).resolve()
    public = args.public or public_build_requested()
    if not public:
        load_release_env(root)
    archive, checksum = build_release(root, output_dir, public=public)

    extras = []
    artifacts_dir = args.artifacts_dir or default_artifacts_dir()
    if artifacts_dir:
        extras.append(copy_into(archive, artifacts_dir))
        extras.append(copy_into(checksum, artifacts_dir))

    print(f"version  {read_version(root)}")
    print(f"zip      {archive}  ({archive.stat().st_size} bytes)")
    print(f"sha256   {checksum.read_text(encoding='ascii').strip()}")
    for extra in extras:
        print(f"copied   {extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
