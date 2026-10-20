"""Pytest must exit 0 or 1 after GUI tests, without a hard process exit."""

from __future__ import annotations

import gc
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import detached_panelcad_reason, release_qt_for_exit

ROOT = Path(__file__).resolve().parents[1]

_PASSING_WINDOW = """
def test_main_window_closes():
    from PySide6.QtWidgets import QApplication
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    window.close()
    app.processEvents()
"""

_FAILING_WINDOW = (
    _PASSING_WINDOW
    + """
def test_one_failure():
    assert False
"""
)

_LEAKED_GUI = """
def test_windows_and_a_running_check_are_left_up():
    import time

    import panelcad.core.checks as checks
    from PySide6.QtWidgets import QApplication
    from panelcad.gui.main_window import MainWindow

    real = checks.run_checks

    def slow(*args, **kwargs):
        time.sleep(0.4)
        return real(*args, **kwargs)

    checks.run_checks = slow
    app = QApplication.instance() or QApplication([])
    for _ in range(3):
        window = MainWindow()
        window.show()
        window._check_runner.run_now()
    app.processEvents()
"""


def test_release_qt_stops_a_running_check_worker() -> None:
    import time

    import panelcad.core.checks as checks
    from PySide6.QtWidgets import QApplication
    from panelcad.gui.main_window import MainWindow

    real = checks.run_checks

    def slow(*args, **kwargs):
        time.sleep(0.3)
        return real(*args, **kwargs)

    checks.run_checks = slow
    try:
        app = QApplication.instance() or QApplication([])
        window = MainWindow()
        window.show()
        window._check_runner.run_now()
        worker = window._check_runner._worker
        assert worker is not None and worker.isRunning()
        release_qt_for_exit()
        assert _running_qthreads() == []
        assert app.topLevelWidgets() == []
    finally:
        checks.run_checks = real


def _running_qthreads() -> list:
    from PySide6.QtCore import QThread

    found = []
    current = QThread.currentThread()
    for obj in gc.get_objects():
        try:
            if isinstance(obj, QThread) and obj is not current and obj.isRunning():
                found.append(obj)
        except Exception:  # noqa: BLE001 — a dead wrapper is not running
            continue
    return found


def test_teardown_does_not_call_os_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []
    monkeypatch.setattr(os, "_exit", lambda code: called.append(code))
    release_qt_for_exit()
    assert called == []


def test_unstubbed_panelcad_gui_and_helper_are_refused() -> None:
    gui = [sys.executable, "-m", "panelcad", "gui"]
    helper = [sys.executable, "-m", "panelcad.update.handoff", "plan.json"]
    assert detached_panelcad_reason(gui) == "panelcad gui"
    assert detached_panelcad_reason(helper) == "panelcad.update.handoff"
    assert (
        detached_panelcad_reason([sys.executable, "-m", "panelcad.update.handoff", "--help"])
        is None
    )
    assert detached_panelcad_reason([sys.executable, "-m", "panelcad", "agent"]) is None
    assert detached_panelcad_reason([sys.executable, "-m", "panelcad", "update", "check"]) is None
    assert detached_panelcad_reason([sys.executable, "-c", "begin_session(gui=True)"]) is None
    assert detached_panelcad_reason(["panelcad.exe", "gui"]) == "panelcad gui"
    with pytest.raises(AssertionError, match="panelcad gui"):
        subprocess.Popen(gui)
    with pytest.raises(AssertionError, match="panelcad.update.handoff"):
        subprocess.Popen(helper)


def _suite(body: str, marker: Path) -> str:
    """Child module. A normal exit runs atexit and writes ``marker``."""

    return (
        "import atexit\n"
        "from pathlib import Path\n"
        "def _mark_clean_shutdown():\n"
        f"    Path({str(marker)!r}).write_text('atexit', encoding='utf-8')\n"
        "atexit.register(_mark_clean_shutdown)\n"
        + body
    )


def _child_pytest(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    """Run a one-file suite that builds a MainWindow, using this repo's conftest."""

    (tmp_path / "test_child_exit.py").write_text(source, encoding="utf-8")
    (tmp_path / "conftest.py").write_text(
        "pytest_plugins = ['tests.conftest']\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYDANTIC_AI_NO_BANNER"] = "1"
    env["PANELCAD_DATA_DIR"] = str(tmp_path / "data")
    env["PANELCAD_CONFIG_DIR"] = str(tmp_path / "config")
    env["PANELCAD_FAULT_LOG"] = str(tmp_path / "pytest-fault.log")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(ROOT / "src"), env.get("PYTHONPATH", "")]
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=line",
            "-p",
            "no:timeout",
            "-p",
            "no:cacheprovider",
            "test_child_exit.py",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def _failure_text(proc: subprocess.CompletedProcess[str], tmp_path: Path) -> str:
    fault = tmp_path / "pytest-fault.log"
    extra = ""
    if fault.is_file() and fault.stat().st_size:
        extra = "\n---- fault log ----\n" + fault.read_text(encoding="utf-8", errors="replace")
    return proc.stdout + proc.stderr + extra


def test_pytest_exit_code_is_zero_when_the_window_suite_passes(tmp_path: Path) -> None:
    marker = tmp_path / "atexit-ran"
    proc = _child_pytest(tmp_path, _suite(_PASSING_WINDOW, marker))
    assert proc.returncode == 0, _failure_text(proc, tmp_path)
    assert "1 passed" in proc.stdout
    assert marker.read_text(encoding="utf-8") == "atexit"


def test_pytest_exit_code_is_one_when_a_window_test_fails(tmp_path: Path) -> None:
    marker = tmp_path / "atexit-ran"
    proc = _child_pytest(tmp_path, _suite(_FAILING_WINDOW, marker))
    assert proc.returncode == 1, _failure_text(proc, tmp_path)
    assert "1 failed" in proc.stdout
    assert "1 passed" in proc.stdout
    assert marker.read_text(encoding="utf-8") == "atexit"


def test_gui_shutdown_exits_zero_several_times(tmp_path: Path) -> None:
    """Repeat the case that aborted when a check thread was still running."""

    for index in range(4):
        run = tmp_path / f"run-{index}"
        run.mkdir()
        marker = run / "atexit-ran"
        proc = _child_pytest(run, _suite(_LEAKED_GUI, marker))
        assert proc.returncode == 0, _failure_text(proc, run)
        assert "1 passed" in proc.stdout
        assert marker.read_text(encoding="utf-8") == "atexit"
        fault = run / "pytest-fault.log"
        if fault.is_file():
            text = fault.read_text(encoding="utf-8", errors="replace")
            assert "Fatal Python error" not in text
