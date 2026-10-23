"""Shared-folder locks, conflict checks, and sync-safe saves."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from panelcad.api.session import Session
from panelcad.core.models import Component, ShareEvent
from panelcad.core.revisions import snapshot_body
from panelcad.diag.crash import process_start_token
from panelcad.io.share import (
    REPLACE_PAUSES,
    WATCH_PAUSES,
    FileBusy,
    LockInfo,
    ProjectChanged,
    ReadOnlyProject,
    atomic_write_text,
    backup_dir_for,
    current_host,
    current_user,
    file_stamp,
    find_conflict_copies,
    folder_is_shared,
    now_iso,
    project_lock_path,
    prune_backups,
    read_project_lock,
    store_project_lock,
)
from panelcad.io.yaml_format import dump_project, load_project
from panelcad.needs_info import save_status_note
from panelcad.reconstruct.placeholder import is_needs_info_component, project_has_needs_info


def _app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def _shared(tmp_path: Path, kind: str = "Google Drive") -> Path:
    root = tmp_path / kind / "jobs"
    root.mkdir(parents=True)
    return root / "line.pcad.yaml"


def _foreign(
    path: Path,
    *,
    user: str = "bob",
    host: str = "OTHER-PC",
    minutes_ago: int = 0,
    pid: int = 4242,
    started: str = "remote-start",
    token: str = "bob-token",
) -> LockInfo:
    when = (datetime.now() - timedelta(minutes=minutes_ago)).replace(microsecond=0)
    stamp = when.isoformat(timespec="seconds")
    info = LockInfo(
        user=user,
        host=host,
        pid=pid,
        started=started,
        token=token,
        acquired=stamp,
        heartbeat=stamp,
    )
    store_project_lock(path, info)
    return info


def _rename(path: Path, name: str) -> None:
    project = load_project(path)
    project.name = name
    path.write_text(dump_project(project), encoding="utf-8")


def test_synced_folder_detection(tmp_path: Path) -> None:
    plain = tmp_path / "jobs" / "line.pcad.yaml"
    assert folder_is_shared(plain) is False
    assert folder_is_shared(_shared(tmp_path, "Google Drive")) is True
    assert folder_is_shared(_shared(tmp_path, "My Drive")) is True
    assert folder_is_shared(_shared(tmp_path, "OneDrive - Shop")) is True
    assert folder_is_shared(_shared(tmp_path, "Dropbox")) is True
    assert folder_is_shared(_shared(tmp_path, "GoogleDrive-alex@shop.com")) is True
    assert folder_is_shared(r"G:\Jobs\line.pcad.yaml") is True
    assert folder_is_shared("G:/Jobs/line.pcad.yaml") is True
    marked = tmp_path / "custom" / "line.pcad.yaml"
    marked.parent.mkdir()
    (marked.parent / ".panelcad-share").write_text("shared\n", encoding="utf-8")
    assert folder_is_shared(marked) is True
    nested = marked.parent / "year" / "line.pcad.yaml"
    assert folder_is_shared(nested) is True


def test_fresh_project_yaml_omits_the_share_log() -> None:
    text = dump_project(Session.create("Empty").project)
    assert "activity:" not in text
    assert "last_saved:" not in text


def test_revision_snapshot_omits_the_share_log() -> None:
    session = Session.create("Rev")
    session.project.last_saved = ShareEvent(user="ada", host="SHOP", time=now_iso(), summary="saved")
    session.project.activity.append(session.project.last_saved)
    body = snapshot_body(session.project)
    assert "activity" not in body
    assert "last_saved" not in body


def test_plain_folder_has_no_lock_but_refuses_a_silent_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "plain.pcad.yaml"
    session = Session.create("Plain")
    session.save(path)
    assert not project_lock_path(path).exists()
    assert session.project.last_saved is not None
    assert session.project.last_saved.user == current_user()
    assert session.project.activity[-1].summary == "saved"
    before = path.read_bytes()
    opened = Session.open(path)
    assert path.read_bytes() == before
    assert opened.share_mode == "local"
    assert not project_lock_path(path).exists()
    _rename(path, "Other")
    opened.project.name = "Mine"
    with pytest.raises(ProjectChanged):
        opened.save()
    assert load_project(path).name == "Other"
    copy = tmp_path / "plain-copy.pcad.yaml"
    opened.save(copy)
    assert load_project(copy).name == "Mine"
    assert load_project(path).name == "Other"
    assert opened.project.activity[-1].summary == "saved a copy"


def test_open_in_a_synced_folder_writes_a_lock(tmp_path: Path) -> None:
    path = _shared(tmp_path)
    made = Session.create("Line")
    made.save(path)
    made.release_share_lock()
    before = path.read_bytes()
    opened = Session.open(path)
    try:
        assert path.read_bytes() == before
        lock = read_project_lock(path)
        assert lock is not None
        assert lock.user == current_user()
        assert lock.host == current_host()
        assert lock.acquired
        assert lock.heartbeat
        assert lock.pid == os.getpid()
        assert opened.read_only is False
        assert opened.share_mode in {"acquired", "ours"}
    finally:
        opened.release_share_lock()


def test_a_live_lock_from_another_pc_opens_read_only(tmp_path: Path) -> None:
    path = _shared(tmp_path, "Dropbox")
    made = Session.create("Line")
    made.save(path)
    made.release_share_lock()
    _foreign(path, user="bob", host="SHOP-PC")
    opened = Session.open(path)
    assert opened.read_only is True
    assert opened.lock_holder is not None
    assert opened.lock_holder.user == "bob"
    assert opened.lock_holder.host == "SHOP-PC"
    assert "bob" in opened.share_notice
    with pytest.raises(ReadOnlyProject):
        opened.save()
    assert load_project(path).name == "Line"
    assert read_project_lock(path) is not None
    assert read_project_lock(path).user == "bob"


def test_take_over_then_a_changed_file_is_not_overwritten(tmp_path: Path) -> None:
    path = _shared(tmp_path, "OneDrive - Shop")
    made = Session.create("Line")
    made.save(path)
    made.release_share_lock()
    _foreign(path, user="bob", host="SHOP-PC")
    opened = Session.open(path)
    assert opened.read_only is True
    opened.take_over_lock()
    assert opened.read_only is False
    assert read_project_lock(path).user == current_user()
    assert "Took over" in opened.share_notice
    _rename(path, "Bob")
    opened.project.name = "Local"
    with pytest.raises(ProjectChanged):
        opened.save()
    assert load_project(path).name == "Bob"
    copy = path.with_name("line-copy.pcad.yaml")
    opened.save(copy)
    assert load_project(copy).name == "Local"
    assert load_project(path).name == "Bob"
    assert opened.project.activity[-1].summary.startswith("took over")
    opened.release_share_lock()


def test_concurrent_editors_do_not_overwrite(tmp_path: Path) -> None:
    path = _shared(tmp_path)
    alice = Session.create("Line")
    alice.save(path)
    alice.project.name = "Alice"
    _rename(path, "Bob")
    _foreign(path, user="bob", host="OTHER-PC")
    with pytest.raises(ProjectChanged) as caught:
        alice.save()
    assert caught.value.path == path
    assert load_project(path).name == "Bob"
    diff = alice.preview_diff()
    assert "Alice" in diff
    assert "Bob" in diff
    copy = path.with_name("line-alice.pcad.yaml")
    alice.save(copy)
    assert load_project(copy).name == "Alice"
    assert load_project(path).name == "Bob"
    assert alice.project.activity[-1].summary == "saved a copy"
    alice.release_share_lock()


def test_stale_lock_expires_and_a_dead_local_process_is_replaced(tmp_path: Path) -> None:
    path = _shared(tmp_path)
    made = Session.create("Line")
    made.save(path)
    made.release_share_lock()
    _foreign(path, user="old", host="OTHER-PC", minutes_ago=21)
    opened = Session.open(path)
    try:
        assert opened.share_mode == "stale"
        assert opened.read_only is False
        assert "stale" in opened.share_notice.lower()
        assert read_project_lock(path).user == current_user()
    finally:
        opened.release_share_lock()

    _foreign(
        path,
        user="crash",
        host=current_host(),
        pid=999_999,
        started="not-a-live-start",
        minutes_ago=0,
    )
    recovered = Session.open(path)
    try:
        assert recovered.share_mode == "stale"
        assert recovered.read_only is False
        assert read_project_lock(path).user == current_user()
    finally:
        recovered.release_share_lock()


def test_a_live_process_on_this_pc_keeps_the_lock(tmp_path: Path) -> None:
    path = _shared(tmp_path, "Dropbox")
    made = Session.create("Line")
    made.save(path)
    made.release_share_lock()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        started = process_start_token(proc.pid)
        assert started
        _foreign(
            path,
            user="bob",
            host=current_host(),
            pid=proc.pid,
            started=started,
            minutes_ago=0,
        )
        opened = Session.open(path)
        assert opened.read_only is True
        assert opened.lock_holder is not None
        assert opened.lock_holder.user == "bob"
    finally:
        proc.kill()
        proc.wait(timeout=5)
    taken = Session.open(path)
    try:
        assert taken.share_mode == "stale"
        assert taken.read_only is False
    finally:
        taken.release_share_lock()


def test_unreadable_lock_stays_read_only_until_take_over(tmp_path: Path) -> None:
    path = _shared(tmp_path)
    made = Session.create("Line")
    made.save(path)
    made.release_share_lock()
    project_lock_path(path).write_text("{", encoding="utf-8")
    opened = Session.open(path)
    assert opened.read_only is True
    assert opened.share_mode == "unreadable"
    opened.take_over_lock()
    assert opened.read_only is False
    assert read_project_lock(path).user == current_user()
    opened.release_share_lock()
    assert not project_lock_path(path).exists()


def test_heartbeat_refreshes_and_notices_a_takeover(tmp_path: Path) -> None:
    path = _shared(tmp_path)
    session = Session.create("Line")
    session.save(path)
    info = read_project_lock(path)
    assert info is not None
    info.heartbeat = "2000-01-01T00:00:00"
    store_project_lock(path, info)
    session.heartbeat_share_lock()
    assert read_project_lock(path).heartbeat != "2000-01-01T00:00:00"
    _foreign(path, user="bob", host="OTHER-PC")
    session.heartbeat_share_lock()
    assert session.read_only is True
    assert session.lock_holder is not None
    assert session.lock_holder.user == "bob"
    assert read_project_lock(path).user == "bob"
    session.release_share_lock()
    assert read_project_lock(path).user == "bob"


def test_conflict_copy_names(tmp_path: Path) -> None:
    path = _shared(tmp_path)
    path.write_text("name: Line\n", encoding="utf-8")
    folder = path.parent
    names = [
        "line (1).pcad.yaml",
        "line.pcad (1).yaml",
        "line (conflicted copy).pcad.yaml",
        "line (Alex's conflicted copy 2026-10-16).pcad.yaml",
        "Copy of line.pcad.yaml",
        "notes (1).yaml",
    ]
    for name in names:
        (folder / name).write_text("x\n", encoding="utf-8")
    found = {item.name for item in find_conflict_copies(path)}
    assert "notes (1).yaml" not in found
    assert found == {
        "line (1).pcad.yaml",
        "line.pcad (1).yaml",
        "line (conflicted copy).pcad.yaml",
        "line (Alex's conflicted copy 2026-10-16).pcad.yaml",
        "Copy of line.pcad.yaml",
    }


def test_atomic_save_keeps_a_timestamped_backup_and_no_partial(tmp_path: Path) -> None:
    path = _shared(tmp_path)
    session = Session.create("Line")
    session.save(path)
    assert list(backup_dir_for(path).glob("*.pcad.yaml")) == []
    session.project.name = "Second"
    session.save()
    backups = list(backup_dir_for(path).glob("*.pcad.yaml"))
    assert len(backups) == 1
    assert load_project(backups[0]).name == "Line"
    assert load_project(path).name == "Second"
    assert list(path.parent.glob(f".{path.name}.partial-*")) == []
    assert file_stamp(path)
    session.release_share_lock()


def test_failed_replace_leaves_the_original_and_rolls_back_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "plain.pcad.yaml"
    session = Session.create("Plain")
    session.save(path)
    original = path.read_bytes()
    summaries = [row.summary for row in session.project.activity]
    monkeypatch.setattr("panelcad.io.share.REPLACE_PAUSES", ())

    def boom(src, dest):
        raise PermissionError("busy")

    monkeypatch.setattr(os, "replace", boom)
    direct = tmp_path / "direct.pcad.yaml"
    direct.write_text("old\n", encoding="utf-8")
    with pytest.raises(FileBusy) as caught:
        atomic_write_text(direct, "new\n", backup=False)
    assert isinstance(caught.value.__cause__, PermissionError)
    assert direct.read_text(encoding="utf-8") == "old\n"
    assert list(tmp_path.glob(".direct.pcad.yaml.partial-*")) == []

    session.project.name = "Next"
    with pytest.raises(FileBusy) as caught:
        session.save()
    assert isinstance(caught.value.__cause__, PermissionError)
    assert path.read_bytes() == original
    assert [row.summary for row in session.project.activity] == summaries
    assert load_project(path).name == "Plain"


def test_backup_prune_keeps_the_newest(tmp_path: Path) -> None:
    folder = tmp_path / ".line.pcad.yaml.backups"
    folder.mkdir()
    for index in range(5):
        (folder / f"2020010{index}-000000.pcad.yaml").write_text(str(index), encoding="utf-8")
    prune_backups(folder, keep=2)
    left = sorted(item.name for item in folder.glob("*.pcad.yaml"))
    assert left == ["20200103-000000.pcad.yaml", "20200104-000000.pcad.yaml"]


def test_backup_prune_keeps_newer_same_second_suffixes(tmp_path: Path) -> None:
    folder = tmp_path / ".line.pcad.yaml.backups"
    folder.mkdir()
    stamp = "20261016-120000"
    for index in range(36):
        name = f"{stamp}.pcad.yaml" if index == 0 else f"{stamp}-{index}.pcad.yaml"
        (folder / name).write_text(name, encoding="utf-8")
    prune_backups(folder, keep=30)
    left = {item.name for item in folder.glob("*.pcad.yaml")}
    assert len(left) == 30
    assert f"{stamp}.pcad.yaml" not in left
    assert f"{stamp}-2.pcad.yaml" not in left
    assert f"{stamp}-5.pcad.yaml" not in left
    assert f"{stamp}-6.pcad.yaml" in left
    assert f"{stamp}-10.pcad.yaml" in left
    assert f"{stamp}-35.pcad.yaml" in left


def test_many_saves_in_one_second_keep_the_newest_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Frozen(datetime):
        @classmethod
        def now(cls, *args, **kwargs):
            return cls(2026, 10, 16, 12, 0, 0)

    monkeypatch.setattr("panelcad.io.share.datetime", Frozen)
    path = _shared(tmp_path)
    session = Session.create("Line")
    session.save(path)
    for index in range(31):
        session.project.name = f"Save{index}"
        session.save()
    stamp = "20261016-120000"
    folder = backup_dir_for(path)
    names = {item.name for item in folder.glob("*.pcad.yaml")}
    assert len(names) == 30
    assert f"{stamp}.pcad.yaml" not in names
    assert f"{stamp}-1.pcad.yaml" in names
    assert f"{stamp}-10.pcad.yaml" in names
    assert f"{stamp}-30.pcad.yaml" in names
    kept = {item.name: item for item in folder.glob("*.pcad.yaml")}
    assert load_project(kept[f"{stamp}-1.pcad.yaml"]).name == "Save0"
    assert load_project(kept[f"{stamp}-30.pcad.yaml"]).name == "Save29"
    assert load_project(path).name == "Save30"
    session.release_share_lock()


def test_replace_waits_long_enough_for_a_shared_handle() -> None:
    assert sum(REPLACE_PAUSES) >= 8


def test_replace_retries_every_pause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "line.pcad.yaml"
    path.write_text("old\n", encoding="utf-8")
    sleeps: list[float] = []
    calls = {"n": 0}
    monkeypatch.setattr("panelcad.io.share.time.sleep", lambda delay: sleeps.append(delay))

    def boom(src, dest):
        calls["n"] += 1
        raise PermissionError("busy")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(FileBusy) as caught:
        atomic_write_text(path, "new\n", backup=False)
    assert isinstance(caught.value.__cause__, PermissionError)
    assert calls["n"] == len(REPLACE_PAUSES) + 1
    assert sleeps == list(REPLACE_PAUSES)
    assert path.read_text(encoding="utf-8") == "old\n"


def test_backup_copy_retries_a_busy_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "line.pcad.yaml"
    path.write_text("old\n", encoding="utf-8")
    sleeps: list[float] = []
    calls = {"n": 0}
    real_copy = shutil.copy2
    monkeypatch.setattr("panelcad.io.share.time.sleep", lambda delay: sleeps.append(delay))

    def flaky(src, dest, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("busy")
        real_copy(src, dest)

    monkeypatch.setattr("panelcad.io.share.shutil.copy2", flaky)
    atomic_write_text(path, "new\n")
    assert path.read_text(encoding="utf-8") == "new\n"
    backups = list(backup_dir_for(path).glob("*.pcad.yaml"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "old\n"
    assert calls["n"] == 3
    assert sleeps == list(REPLACE_PAUSES[:2])


def test_backup_copy_is_skipped_when_the_file_stays_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "line.pcad.yaml"
    path.write_text("old\n", encoding="utf-8")
    sleeps: list[float] = []
    calls = {"n": 0}
    monkeypatch.setattr("panelcad.io.share.time.sleep", lambda delay: sleeps.append(delay))

    def fail_copy(src, dest, *args, **kwargs):
        calls["n"] += 1
        dest.write_text("partial\n", encoding="utf-8")
        raise PermissionError("busy")

    monkeypatch.setattr("panelcad.io.share.shutil.copy2", fail_copy)
    with caplog.at_level(logging.WARNING, logger="panelcad.io.share"):
        atomic_write_text(path, "new\n")
    assert path.read_text(encoding="utf-8") == "new\n"
    assert list(backup_dir_for(path).glob("*.pcad.yaml")) == []
    assert calls["n"] == len(REPLACE_PAUSES) + 1
    assert sleeps == list(REPLACE_PAUSES)
    assert "skipped backup" in caplog.text


def _flaky_project_open(path: Path, real_open, fail_times: int):
    calls = {"n": 0}

    def flaky(self, mode="r", *args, **kwargs):
        text = mode if isinstance(mode, str) else "r"
        reading = "r" in text and "w" not in text and "a" not in text and "+" not in text
        if Path(self) == path and reading:
            calls["n"] += 1
            if calls["n"] <= fail_times:
                raise PermissionError("busy")
        return real_open(self, mode, *args, **kwargs)

    return calls, flaky


def test_hash_read_retries_then_the_save_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "plain.pcad.yaml"
    session = Session.create("Plain")
    session.save(path)
    session.project.name = "Next"
    sleeps: list[float] = []
    monkeypatch.setattr("panelcad.io.share.time.sleep", lambda delay: sleeps.append(delay))
    real_open = Path.open
    calls, flaky = _flaky_project_open(path, real_open, 2)
    monkeypatch.setattr(Path, "open", flaky)
    session.save()
    assert calls["n"] >= 3
    assert sleeps[:2] == list(REPLACE_PAUSES[:2])
    monkeypatch.undo()
    assert load_project(path).name == "Next"


def test_unreadable_project_is_not_treated_as_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "plain.pcad.yaml"
    session = Session.create("Plain")
    session.save(path)
    original = path.read_bytes()
    session.project.name = "Next"
    monkeypatch.setattr("panelcad.io.share.REPLACE_PAUSES", ())
    real_open = Path.open
    _calls, blocked = _flaky_project_open(path, real_open, 10_000)
    monkeypatch.setattr(Path, "open", blocked)
    with pytest.raises(PermissionError):
        session.disk_matches()
    with pytest.raises(FileBusy) as caught:
        session.save()
    assert isinstance(caught.value.__cause__, PermissionError)
    monkeypatch.undo()
    assert path.read_bytes() == original
    assert load_project(path).name == "Plain"


def test_yaml_watch_skips_a_busy_file_and_sees_it_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panelcad.gui.main_window import MainWindow

    assert sum(WATCH_PAUSES) <= 0.05
    app = _app()
    path = tmp_path / "plain.pcad.yaml"
    session = Session.create("Plain")
    session.save(path)
    window = MainWindow(session)
    window.show()
    app.processEvents()
    window._note_yaml_mtime()
    remembered = window._yaml_mtime
    project = load_project(path)
    project.name = "From disk"
    path.write_text(dump_project(project), encoding="utf-8")
    os.utime(path, (remembered + 2, remembered + 2))
    real_open = Path.open
    _calls, blocked = _flaky_project_open(path, real_open, 10_000)
    monkeypatch.setattr(Path, "open", blocked)
    try:
        for _ in range(2):
            started = time.perf_counter()
            window._poll_yaml()
            assert time.perf_counter() - started < 0.1
        assert window._yaml_mtime == remembered
        assert window.session.project.name == "Plain"
        assert "changed on disk" not in window.statusBar().currentMessage()
    finally:
        monkeypatch.undo()
    window._poll_yaml()
    assert window.session.project.name == "From disk"
    window.close()
    app.processEvents()


def test_window_save_shows_a_wait_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = _app()
    path = tmp_path / "plain.pcad.yaml"
    session = Session.create("Plain")
    session.save(path)
    window = MainWindow(session)
    window.show()
    app.processEvents()
    window.session.project.name = "Next"
    seen: list[object] = []
    notes: list[str] = []
    bar = window.statusBar()
    real_show = bar.showMessage

    def override(cursor):
        seen.append(cursor)

    def restore():
        seen.append("restore")

    def show(text, timeout=0):
        notes.append(str(text))
        real_show(text, timeout)

    monkeypatch.setattr(QApplication, "setOverrideCursor", staticmethod(override))
    monkeypatch.setattr(QApplication, "restoreOverrideCursor", staticmethod(restore))
    monkeypatch.setattr(bar, "showMessage", show)
    assert window.save_project() is True
    assert seen[0] == Qt.CursorShape.WaitCursor
    assert "restore" in seen
    assert "Saving…" in notes
    assert load_project(path).name == "Next"
    window.close()
    app.processEvents()


def test_window_save_reports_a_busy_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.gui.main_window import MainWindow

    app = _app()
    path = tmp_path / "plain.pcad.yaml"
    session = Session.create("Plain")
    session.save(path)
    window = MainWindow(session)
    window.show()
    app.processEvents()
    window.session.project.name = "Dirty"
    original = path.read_bytes()
    shown: list[str] = []

    def boom(src, dest):
        raise PermissionError("busy")

    def warning(*args, **kwargs):
        shown.append(args[2] if len(args) > 2 else "")
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr("panelcad.io.share.REPLACE_PAUSES", ())
    monkeypatch.setattr(os, "replace", boom)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(warning))
    assert window.save_project() is False
    assert path.read_bytes() == original
    assert load_project(path).name == "Plain"
    assert window._project_dirty() is True
    assert shown == [
        (
            "The file is busy (sync or another program). Nothing was overwritten. "
            "Try Save again or Save a copy."
        )
    ]
    window.close()
    app.processEvents()


def _dialog_warning(shown: list[tuple[str, str]]):
    def warning(*args, **kwargs):
        title = args[1] if len(args) > 1 else ""
        text = args[2] if len(args) > 2 else ""
        shown.append((str(title), str(text)))
        return QMessageBox.StandardButton.Ok

    return staticmethod(warning)


def test_save_as_readonly_folder_is_not_file_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0:
        pytest.skip("chmod 555 does not block Windows or root")
    from panelcad.gui.main_window import MainWindow

    app = _app()
    path = tmp_path / "good.pcad.yaml"
    session = Session.create("Plain")
    session.save(path)
    window = MainWindow(session)
    window.show()
    app.processEvents()
    folder = tmp_path / "ro"
    folder.mkdir()
    folder.chmod(0o555)
    blocked = folder / "new.pcad.yaml"
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_a, **_k: (str(blocked), ""))
    monkeypatch.setattr(QMessageBox, "warning", _dialog_warning(shown))
    try:
        assert window.save_project_as() is False
    finally:
        folder.chmod(0o755)
    assert not blocked.exists()
    assert window.session.path == path
    assert path.read_bytes()
    assert shown
    title, text = shown[0]
    assert title == "Can't save there"
    assert text.startswith("Can't save there")
    assert "PanelCAD can't write to this folder." in text
    assert "Nothing was overwritten." in text
    assert "Use File → Save As to pick another folder." in text
    assert "File busy" not in title
    assert "File busy" not in text
    assert "Try Save again" not in text
    window.close()
    app.processEvents()


def test_replace_permission_error_on_every_try_is_file_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panelcad.gui.main_window import MainWindow

    app = _app()
    path = tmp_path / "plain.pcad.yaml"
    session = Session.create("Plain")
    session.save(path)
    window = MainWindow(session)
    window.show()
    app.processEvents()
    window.session.project.name = "Dirty"
    original = path.read_bytes()
    calls = {"n": 0}
    shown: list[tuple[str, str]] = []

    def boom(src, dest):
        calls["n"] += 1
        raise PermissionError("busy")

    monkeypatch.setattr("panelcad.io.share.time.sleep", lambda _delay: None)
    monkeypatch.setattr(os, "replace", boom)
    monkeypatch.setattr(QMessageBox, "warning", _dialog_warning(shown))
    assert window.save_project() is False
    assert calls["n"] == len(REPLACE_PAUSES) + 1
    assert path.read_bytes() == original
    assert window.session.path == path
    assert shown == [
        (
            "File busy",
            "The file is busy (sync or another program). Nothing was overwritten. "
            "Try Save again or Save a copy.",
        )
    ]
    window.close()
    app.processEvents()


def test_unreadable_hash_on_save_is_file_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panelcad.gui.main_window import MainWindow

    app = _app()
    path = tmp_path / "plain.pcad.yaml"
    session = Session.create("Plain")
    session.save(path)
    window = MainWindow(session)
    window.show()
    app.processEvents()
    window.session.project.name = "Dirty"
    original = path.read_bytes()
    shown: list[tuple[str, str]] = []
    real_open = Path.open
    _calls, blocked = _flaky_project_open(path, real_open, 10_000)
    monkeypatch.setattr("panelcad.io.share.time.sleep", lambda _delay: None)
    monkeypatch.setattr(Path, "open", blocked)
    monkeypatch.setattr(QMessageBox, "warning", _dialog_warning(shown))
    assert window.save_project() is False
    assert window.session.path == path
    assert shown == [
        (
            "File busy",
            "The file is busy (sync or another program). Nothing was overwritten. "
            "Try Save again or Save a copy.",
        )
    ]
    monkeypatch.undo()
    assert path.read_bytes() == original
    assert load_project(path).name == "Plain"
    window.close()
    app.processEvents()


def test_needs_info_still_saves_and_reopens(tmp_path: Path) -> None:
    path = _shared(tmp_path, "Dropbox")
    session = Session.create("Needs")
    session.project.sheets[0].components.append(
        Component(id="U1", library_id="reconstruct.needs_info", x=10, y=20)
    )
    session.save(path)
    assert save_status_note(session.project) == "1 part still needs info"
    assert "reconstruct.needs_info" in path.read_text(encoding="utf-8")
    assert session.project.last_saved is not None
    assert session.project.last_saved.time
    again = Session.open(path)
    assert project_has_needs_info(again.project)
    assert is_needs_info_component(again.project.sheets[0].component("U1"))
    again.project.name = "Still"
    again.save()
    third = Session.open(path)
    assert third.project.name == "Still"
    assert project_has_needs_info(third.project)
    assert len(third.project.activity) >= 2
    assert third.project.activity[-1].user == current_user()
    third.release_share_lock()
    again.release_share_lock()
    session.release_share_lock()


def test_window_releases_the_lock_on_close(tmp_path: Path) -> None:
    from panelcad.gui.main_window import MainWindow

    _app()
    path = _shared(tmp_path)
    session = Session.create("Line")
    session.save(path)
    window = MainWindow(session)
    window.show()
    _app().processEvents()
    texts = [action.text() for action in window.findChildren(QAction)]
    assert any("Refresh from" in text and "disk" in text for text in texts)
    assert any("activity" in text.lower() for text in texts)
    window.close()
    _app().processEvents()
    assert not project_lock_path(path).exists()


def test_window_open_offers_read_only_take_over_and_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panelcad.gui.main_window import MainWindow

    app = _app()
    path = _shared(tmp_path, "Dropbox")
    made = Session.create("Line")
    made.save(path)
    made.release_share_lock()
    _foreign(path, user="bob", host="SHOP-PC")

    choices = iter(["cancel", "readonly", "takeover"])

    def choose(*_args, **_kwargs):
        return next(choices)

    monkeypatch.setattr("panelcad.gui.share_dialog.ask_locked", choose)
    window = MainWindow(Session.create("Untitled"))
    window.show()
    app.processEvents()
    window.open_path(str(path))
    app.processEvents()
    assert window.session.project.name == "Untitled"
    assert read_project_lock(path).user == "bob"

    window.open_path(str(path))
    app.processEvents()
    assert window.session.read_only is True
    assert window.session.project.name == "Line"
    assert "read-only" in window.windowTitle()
    assert "bob" in window._saved_label.text()
    assert "bob" in window.statusBar().currentMessage()

    window.open_path(str(path))
    app.processEvents()
    assert window.session.read_only is False
    assert read_project_lock(path).user == current_user()
    assert "Took over" in window.statusBar().currentMessage()
    window.close()
    app.processEvents()


def test_window_save_can_reload_or_copy_when_the_file_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panelcad.gui.main_window import MainWindow
    from panelcad.gui.share_dialog import DiffDialog, ProjectActivityDialog

    app = _app()
    path = _shared(tmp_path)
    session = Session.create("Line")
    session.save(path)
    window = MainWindow(session)
    window.show()
    app.processEvents()
    window._remember_saved_project()
    assert "Last saved" in window._saved_label.text()
    dialog = ProjectActivityDialog(window.session, window)
    assert dialog.table.rowCount() >= 1
    dialog.close()

    _rename(path, "From disk")
    assert window.refresh_from_disk() is True
    app.processEvents()
    assert window.session.project.name == "From disk"

    window.session.project.name = "Local edit"
    _rename(path, "Someone else")
    monkeypatch.setattr("panelcad.gui.share_dialog.ask_conflict", lambda *_a, **_k: "reload")
    assert window.save_project() is True
    assert window.session.project.name == "Someone else"
    assert load_project(path).name == "Someone else"

    window.session.project.name = "Local edit"
    _rename(path, "On disk")
    copy = path.with_name("line-copy.pcad.yaml")
    monkeypatch.setattr("panelcad.gui.share_dialog.ask_conflict", lambda *_a, **_k: "copy")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_a, **_k: (str(copy), ""))
    assert window.save_project() is True
    assert load_project(copy).name == "Local edit"
    assert load_project(path).name == "On disk"

    diff_box = DiffDialog(window, "name changed")
    assert "name changed" in diff_box.text.toPlainText()
    diff_box._pick("reload")
    assert diff_box.choice == "reload"
    diff_box.close()
    window.close()
    app.processEvents()


def test_dirty_refresh_can_be_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.gui.main_window import MainWindow

    app = _app()
    path = tmp_path / "plain.pcad.yaml"
    session = Session.create("Plain")
    session.save(path)
    window = MainWindow(session)
    window.show()
    app.processEvents()
    window.session.project.name = "Dirty"
    monkeypatch.setattr("panelcad.gui.share_dialog.confirm_discard", lambda *_a, **_k: False)
    assert window.refresh_from_disk() is False
    assert window.session.project.name == "Dirty"
    window.close()
    app.processEvents()
