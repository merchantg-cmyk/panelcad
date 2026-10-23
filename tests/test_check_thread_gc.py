"""The live Problems check thread never runs Python's cyclic GC.

A collection started on the check thread could free PySide wrappers that own
Qt GUI objects while the canvas paints them (python312.dll 0xc0000005 when
switching sheets right after a save). Automatic GC is paused while a check
thread runs and resumed from the GUI thread.
"""

from __future__ import annotations

import gc
import threading

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from panelcad.api.session import Session  # noqa: E402
from panelcad.gui import problems  # noqa: E402


@pytest.fixture(autouse=True)
def _automatic_gc_on():
    """These tests check the pause itself, so start from automatic GC on."""

    gc.enable()
    yield
    gc.disable()


@pytest.fixture()
def app():
    return QApplication.instance() or QApplication([])


def _wait(app, cond, timeout=30.0):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_gc_pause_nests_and_restores():
    assert gc.isenabled()
    a, b = problems._GuiThreadGcPause(), problems._GuiThreadGcPause()
    a.acquire()
    a.acquire()
    b.acquire()
    assert not gc.isenabled()
    a.release()
    assert not gc.isenabled()
    b.release()
    b.release()
    assert gc.isenabled()


def test_gc_pause_keeps_gc_off_if_it_was_off():
    gc.disable()
    try:
        p = problems._GuiThreadGcPause()
        p.acquire()
        p.release()
        assert not gc.isenabled()
    finally:
        gc.enable()


def test_check_thread_never_collects_and_gc_comes_back(app, monkeypatch):
    seen: list[tuple[str, bool]] = []
    collected_on: list[str] = []
    real = problems._CheckWorker.run

    def run(self):
        seen.append((threading.current_thread().name, gc.isenabled()))
        real(self)

    def on_gc(phase, info):
        if phase == "start":
            collected_on.append(threading.current_thread().name)

    monkeypatch.setattr(problems._CheckWorker, "run", run)
    session = Session.create("gc probe")
    runner = problems.LiveCheckRunner(session)
    got: list = []
    runner.rows_ready.connect(got.append)
    gc.callbacks.append(on_gc)
    try:
        for _ in range(3):
            got.clear()
            runner.run_now()
            assert _wait(app, lambda: bool(got))
            assert _wait(app, lambda: runner._gc_pause is None)
            assert gc.isenabled()
    finally:
        gc.callbacks.remove(on_gc)
        runner.stop()
    assert seen and all(enabled is False for _, enabled in seen)
    main = threading.main_thread().name
    assert all(name == main for name in collected_on)


def test_stop_resumes_gc_and_the_worker_snapshot_is_a_copy(app):
    session = Session.create("gc stop")
    runner = problems.LiveCheckRunner(session)
    runner.run_now()
    worker = runner._worker
    assert worker is not None
    assert worker._payload is not session.project
    runner.stop()
    _wait(app, lambda: runner._gc_pause is None, timeout=5)
    assert gc.isenabled()
    assert not runner._busy
