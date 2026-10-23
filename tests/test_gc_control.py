"""Cyclic GC runs only on the GUI thread, from a timer (panelcad.gui.gc_control)."""

from __future__ import annotations

import gc
import threading

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from panelcad.gui import gc_control  # noqa: E402


def _app():
    return QApplication.instance() or QApplication([])


def test_install_turns_automatic_gc_off_and_is_idempotent():
    app = _app()
    gc.enable()
    first = gc_control.install(app)
    assert first is not None
    assert not gc.isenabled()
    assert gc_control.install(app) is first
    assert first.parent() is app


def test_env_opt_out_leaves_gc_alone(monkeypatch):
    monkeypatch.setenv("PANELCAD_AUTO_GC", "1")
    gc.enable()
    try:
        assert gc_control.install(_app()) is None
        assert gc.isenabled()
    finally:
        gc.disable()


def test_tick_collects_due_generation_on_gui_thread_only():
    app = _app()
    ctl = gc_control.install(app)
    gc.collect()
    junk = []
    for _ in range(gc_control._THRESHOLD[0] + 50):
        a: list = []
        a.append(a)
        junk.append(a)
    del junk, a
    starts: list[str] = []

    def cb(phase, info):
        if phase == "start":
            starts.append(threading.current_thread().name)

    gc.callbacks.append(cb)
    try:
        assert ctl.tick() >= 0
        result: list[int] = []
        t = threading.Thread(target=lambda: result.append(ctl.tick()))
        t.start()
        t.join()
        assert result == [-1]
    finally:
        gc.callbacks.remove(cb)
    assert starts and all(n == threading.main_thread().name for n in starts)


def test_collect_if_due_skips_when_under_threshold():
    gc.collect()
    assert gc_control.collect_if_due((10**9, 10, 10)) == -1
