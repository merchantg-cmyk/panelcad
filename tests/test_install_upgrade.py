"""Installer upgrade: same-size files, stale bytecode, and zip timestamps."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import py_compile
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from panelcad.version import __version__

ROOT = Path(__file__).resolve().parents[1]


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    """Skip when the OS refuses the link (Windows WinError 1314 without privilege)."""

    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"panelcad_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclass looks up the class module in sys.modules while the class body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _upgrade(source: Path, dest: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "install_tree.py"),
            "--source",
            str(source),
            "--dest",
            str(dest),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _imported_version(dest: Path) -> tuple[str, str]:
    """Import the install tree, not the checkout sitting on sys.path."""
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "src = Path(sys.argv[1]).resolve()\n"
        "drop = Path(sys.argv[2]).resolve()\n"
        "kept = []\n"
        "for entry in sys.path:\n"
        "    if not entry:\n"
        "        kept.append(entry)\n"
        "        continue\n"
        "    try:\n"
        "        resolved = Path(entry).resolve()\n"
        "    except OSError:\n"
        "        kept.append(entry)\n"
        "        continue\n"
        "    if resolved != drop:\n"
        "        kept.append(entry)\n"
        "sys.path = kept\n"
        "sys.path.insert(0, str(src))\n"
        "import panelcad\n"
        "print(panelcad.__file__)\n"
        "print(panelcad.__version__)\n"
    )
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYDANTIC_AI_NO_BANNER"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", code, str(dest / "src"), str(ROOT / "src")],
        cwd=dest,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) >= 2, proc.stdout
    return lines[-2], lines[-1]


def test_parse_git_name_log_keeps_the_newest_commit():
    build = _load_script("build-release.py")
    text = "\n".join(
        [
            "@COMMIT@100",
            "@COMMIT@90",
            "",
            "src/panelcad/version.py",
            "README.md",
            "@COMMIT@80",
            "",
            "README.md",
            "AGENTS.md",
        ]
    )
    stamps = build.parse_git_name_log(text)
    assert stamps["src/panelcad/version.py"] == 90
    assert stamps["README.md"] == 90
    assert stamps["AGENTS.md"] == 80


@pytest.mark.skipif(not (ROOT / ".git").is_dir(), reason=".git is required for commit timestamps")
def test_zip_timestamps_are_per_file_and_reproducible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    build = _load_script("build-release.py")
    first, _ = build.build_release(ROOT, tmp_path / "a")
    second, _ = build.build_release(ROOT, tmp_path / "b")
    first_hash = hashlib.sha256(first.read_bytes()).digest()
    second_hash = hashlib.sha256(second.read_bytes()).digest()
    assert first_hash == second_hash

    clock = build.load_zip_clock(ROOT)
    prefix = f"PanelCAD-{__version__}/"
    with zipfile.ZipFile(first) as zf:
        infos = {info.filename: info.date_time for info in zf.infolist()}

    distinct = set()
    for name, date_time in infos.items():
        assert name.startswith(prefix)
        rel = name[len(prefix) :]
        expected = build.entry_date_time(clock, rel, None)
        assert date_time == expected, rel
        distinct.add(date_time)

    version_name = f"{prefix}src/panelcad/version.py"
    # The old builder stamped every entry 2026-01-01 00:00:00.
    assert infos[version_name] != (2026, 1, 1, 0, 0, 0)
    assert len(distinct) >= 2


def test_source_date_epoch_caps_zip_timestamps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    epoch = 1_600_000_000
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(epoch))
    build = _load_script("build-release.py")
    archive, _ = build.build_release(ROOT, tmp_path)
    capped = build.to_zip_date_time(epoch)
    prefix = f"PanelCAD-{__version__}/"
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
    assert infos
    for info in infos:
        assert info.date_time <= capped
    version = next(info for info in infos if info.filename == f"{prefix}src/panelcad/version.py")
    assert version.date_time == capped


@pytest.mark.skipif(not (ROOT / ".git").is_dir(), reason=".git is required for commit timestamps")
def test_future_source_date_epoch_keeps_older_file_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "4102444800")
    build = _load_script("build-release.py")
    archive, _ = build.build_release(ROOT, tmp_path)
    clock = build.load_zip_clock(ROOT)
    name = f"PanelCAD-{__version__}/src/panelcad/version.py"
    with zipfile.ZipFile(archive) as zf:
        info = zf.getinfo(name)
    assert info.date_time == build.entry_date_time(clock, "src/panelcad/version.py", None)
    assert info.date_time != build.to_zip_date_time(4102444800)
    assert info.date_time < build.to_zip_date_time(4102444800)


def test_zip_clock_without_git_uses_mtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    build = _load_script("build-release.py")
    clock = build.load_zip_clock(tmp_path)
    assert clock.stamps == {}
    assert clock.cap is None
    mtime = 1_700_000_000
    assert build.entry_unix_time(clock, "src/panelcad/version.py", mtime) == mtime


def test_source_date_epoch_must_be_an_integer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "yesterday")
    build = _load_script("build-release.py")
    with pytest.raises(SystemExit):
        build.source_date_epoch()


def test_upgrade_overwrites_same_size_same_date_and_drops_stale_pyc(tmp_path: Path):
    """Build the release zip and upgrade a same-size, same-date install.

    This is the 0.3.0 -> 0.4.0 shop failure: ``version.py`` kept the same
    length, the zip date matched, and ``version.cpython-*.pyc`` still
    reported the old version after the ``.py`` bytes changed.
    """
    build = _load_script("build-release.py")
    archive, _ = build.build_release(ROOT, tmp_path / "dist")
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(extracted)
    source = extracted / f"PanelCAD-{__version__}"
    dest = tmp_path / "PanelCAD"
    seeded = _upgrade(source, dest)
    assert seeded.returncode == 0, seeded.stderr

    src_version = source / "src" / "panelcad" / "version.py"
    dst_version = dest / "src" / "panelcad" / "version.py"
    new_bytes = src_version.read_bytes()
    # Same length as this release, so the file size does not change.
    # 0.3.0 and 0.4.0 were the shop case; 0.5.10 is one character longer.
    old_version = "0.3.0" + "0" * (len(__version__) - len("0.3.0"))
    assert len(old_version) == len(__version__)
    token = f'__version__ = "{__version__}"'.encode("utf-8")
    old_token = f'__version__ = "{old_version}"'.encode("utf-8")
    assert token in new_bytes
    old_bytes = new_bytes.replace(token, old_token, 1)
    assert len(old_bytes) == len(new_bytes)

    dst_version.write_bytes(old_bytes)
    mtime = int(src_version.stat().st_mtime)
    os.utime(src_version, (mtime, mtime))
    os.utime(dst_version, (mtime, mtime))
    py_compile.compile(
        str(dst_version),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )

    # New bytes, same size, same mtime: Python still executes the old pyc.
    dst_version.write_bytes(src_version.read_bytes())
    os.utime(dst_version, (mtime, mtime))
    assert dst_version.stat().st_size == src_version.stat().st_size
    assert int(dst_version.stat().st_mtime) == mtime
    module_file, reported = _imported_version(dest)
    assert Path(module_file).resolve().is_relative_to((dest / "src").resolve())
    assert reported == old_version

    # Older install on disk: different bytes, same size, same date, stale pyc.
    dst_version.write_bytes(old_bytes)
    os.utime(dst_version, (mtime, mtime))
    assert dst_version.read_bytes() != src_version.read_bytes()
    assert dst_version.stat().st_size == src_version.stat().st_size
    assert int(dst_version.stat().st_mtime) == mtime

    job = dest / "Jobs" / "pump.pcad.yaml"
    job.parent.mkdir(parents=True)
    job.write_text("user job\n", encoding="utf-8")
    outside_pyc = dest / "Jobs" / "__pycache__" / "notes.pyc"
    outside_pyc.parent.mkdir(parents=True)
    outside_pyc.write_bytes(b"keep")
    extra = dest / "examples" / "shop_extra.pcad.yaml"
    extra.write_text("keep extra\n", encoding="utf-8")
    local_note = dest / "src" / "panelcad" / "LOCAL.txt"
    local_note.write_text("keep local\n", encoding="utf-8")
    for rel in ("docs", "tests"):
        stale = dest / rel / "__pycache__" / "stale.pyc"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"stale")

    upgraded = _upgrade(source, dest)
    assert upgraded.returncode == 0, upgraded.stderr
    assert dst_version.read_bytes() == src_version.read_bytes()
    # Same mtime as the stale pyc header. The import wins only because the
    # bytecode was removed, not because the timestamp moved.
    assert int(dst_version.stat().st_mtime) == mtime
    assert not (dst_version.parent / "__pycache__").exists()
    assert not (dest / "docs" / "__pycache__").exists()
    assert not (dest / "tests" / "__pycache__").exists()
    assert list((dest / "src").rglob("*.pyc")) == []
    module_file, reported = _imported_version(dest)
    assert Path(module_file).resolve().is_relative_to((dest / "src").resolve())
    assert reported == __version__
    assert job.read_text(encoding="utf-8") == "user job\n"
    assert outside_pyc.read_bytes() == b"keep"
    assert extra.read_text(encoding="utf-8") == "keep extra\n"
    assert local_note.read_text(encoding="utf-8") == "keep local\n"


def test_upgrade_keeps_an_existing_example_ai_journal(tmp_path: Path):
    source = tmp_path / "release"
    shipped = '{"op":"from-setup"}\n'
    user = '{"op":"user-notes"}\n'
    assert len(shipped.encode()) == len(user.encode())
    journal = source / "examples" / "dol_starter.pcad.ai.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(shipped, encoding="utf-8")
    inbox = source / "examples" / "dol_starter.pcad.ai-in.jsonl"
    inbox.write_text('{"op":"inbox"}\n', encoding="utf-8")
    (source / "examples" / "dol_starter.pcad.yaml").write_text("new example\n", encoding="utf-8")
    (source / "src").mkdir()
    (source / "src" / "version.py").write_text("new\n", encoding="utf-8")

    dest = tmp_path / "PanelCAD"
    dest_journal = dest / "examples" / "dol_starter.pcad.ai.jsonl"
    dest_journal.parent.mkdir(parents=True)
    dest_journal.write_text(user, encoding="utf-8")
    dest_inbox = dest / "examples" / "dol_starter.pcad.ai-in.jsonl"
    dest_inbox.write_text('{"op":"kept-in"}\n', encoding="utf-8")
    (dest / "examples" / "dol_starter.pcad.yaml").write_text("old example\n", encoding="utf-8")
    mtime = 1_700_000_000
    os.utime(journal, (mtime, mtime))
    os.utime(dest_journal, (mtime, mtime))

    proc = _upgrade(source, dest)
    assert proc.returncode == 0, proc.stderr
    assert dest_journal.read_text(encoding="utf-8") == user
    assert dest_inbox.read_text(encoding="utf-8") == '{"op":"kept-in"}\n'
    example = dest / "examples" / "dol_starter.pcad.yaml"
    assert example.read_text(encoding="utf-8") == "new example\n"
    assert (dest / "src" / "version.py").read_text(encoding="utf-8") == "new\n"


def test_upgrade_copies_example_journal_when_the_install_has_none(tmp_path: Path):
    source = tmp_path / "release"
    journal = source / "examples" / "dol_starter.pcad.ai.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"op":"from-setup"}\n', encoding="utf-8")
    dest = tmp_path / "PanelCAD"

    proc = _upgrade(source, dest)
    assert proc.returncode == 0, proc.stderr
    copied = dest / "examples" / "dol_starter.pcad.ai.jsonl"
    assert copied.read_text(encoding="utf-8") == '{"op":"from-setup"}\n'


def test_upgrade_refuses_to_write_through_a_symlink(tmp_path: Path):
    source = tmp_path / "release"
    (source / "src" / "panelcad").mkdir(parents=True)
    (source / "src" / "panelcad" / "version.py").write_text("new\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("keep", encoding="utf-8")
    (outside / "evil.pyc").write_bytes(b"keep")
    dest = tmp_path / "PanelCAD"
    dest.mkdir()
    _symlink_or_skip(dest / "src", outside, target_is_directory=True)

    proc = _upgrade(source, dest)
    assert proc.returncode != 0
    assert "symlink" in proc.stderr.lower()
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "keep"
    assert (outside / "evil.pyc").read_bytes() == b"keep"


def test_upgrade_replaces_a_file_symlink_without_editing_its_target(tmp_path: Path):
    source = tmp_path / "release"
    (source / "src" / "panelcad").mkdir(parents=True)
    (source / "src" / "panelcad" / "version.py").write_text("new\n", encoding="utf-8")
    outside = tmp_path / "outside-version.py"
    outside.write_text("outside\n", encoding="utf-8")
    dest = tmp_path / "PanelCAD"
    link = dest / "src" / "panelcad" / "version.py"
    link.parent.mkdir(parents=True)
    _symlink_or_skip(link, outside)

    proc = _upgrade(source, dest)
    assert proc.returncode == 0, proc.stderr
    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert not link.is_symlink()
    assert link.read_text(encoding="utf-8") == "new\n"


def test_purge_skips_a_symlinked_pycache(tmp_path: Path):
    source = tmp_path / "release"
    (source / "src" / "panelcad").mkdir(parents=True)
    (source / "src" / "panelcad" / "version.py").write_text("new\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("keep", encoding="utf-8")
    (outside / "evil.pyc").write_bytes(b"keep")
    dest = tmp_path / "PanelCAD"
    (dest / "src").mkdir(parents=True)
    _symlink_or_skip(dest / "src" / "__pycache__", outside, target_is_directory=True)

    proc = _upgrade(source, dest)
    assert proc.returncode == 0, proc.stderr
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "keep"
    assert (outside / "evil.pyc").read_bytes() == b"keep"
    assert (dest / "src" / "__pycache__").is_symlink()
    assert (dest / "src" / "panelcad" / "version.py").read_text(encoding="utf-8") == "new\n"


def test_upgrade_same_folder_only_purges_bytecode(tmp_path: Path):
    root = tmp_path / "PanelCAD"
    package = root / "src" / "panelcad"
    package.mkdir(parents=True)
    (package / "version.py").write_text('__version__ = "0.4.0"\n', encoding="utf-8")
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "version.cpython-312.pyc").write_bytes(b"stale")
    job = root / "Jobs" / "pump.pcad.yaml"
    job.parent.mkdir(parents=True)
    job.write_text("keep\n", encoding="utf-8")

    proc = _upgrade(root, root)
    assert proc.returncode == 0, proc.stderr
    assert not cache.exists()
    assert job.read_text(encoding="utf-8") == "keep\n"
    assert (package / "version.py").read_text(encoding="utf-8").startswith("__version__")


def test_refuses_to_copy_into_its_own_subfolder(tmp_path: Path):
    source = tmp_path / "release"
    (source / "src").mkdir(parents=True)
    (source / "src" / "a.py").write_text("a\n", encoding="utf-8")
    proc = _upgrade(source, source / "nested")
    assert proc.returncode != 0
    assert "inside the source" in proc.stderr
