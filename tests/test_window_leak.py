"""Opening and closing MainWindow repeatedly frees every window.

Close must delete the window (``WA_DeleteOnClose``), and nothing global (theme
listeners, guidance fixes, timers, threads) may keep a closed window alive.
"""

from __future__ import annotations

import gc
import sys

import pytest
import shiboken6

CYCLES = 50
WARMUP = 5
MAX_WARMUP = 30


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _settle(app) -> None:
    from PySide6.QtCore import QCoreApplication, QEvent

    for _ in range(3):
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, int(QEvent.Type.DeferredDelete))
    gc.collect()


def _live_windows(cls) -> int:
    # ``type()``, not ``isinstance``: isinstance reads ``__class__``, which makes
    # lazy proxies in the heap (openai's numpy/pandas proxies) import modules.
    return sum(
        1 for obj in gc.get_objects() if issubclass(type(obj), cls) and shiboken6.isValid(obj)
    )


def _cycle(app, Session, MainWindow, n: int) -> None:
    window = MainWindow(Session.create(f"Leak {n}"))
    window.resize(1200, 800)
    window.show()
    app.processEvents()
    window.close()
    del window
    _settle(app)


# pytest-qt keeps every Qt warning as a record; 55 windows' worth would
# count as growth here, so this test does not capture them.
@pytest.mark.no_qt_log
def test_open_close_cycles_free_every_window(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path))
    app = _app()
    from panelcad import theme
    from panelcad.api.session import Session
    from panelcad.gui import guidance
    from panelcad.gui.main_window import MainWindow

    before_windows = _live_windows(MainWindow)
    # Warm up until lazy imports (the AI SDK and its models load on first use)
    # are done; a one-time import is not a per-window leak.
    stable = 0
    n = 0
    while n < WARMUP or (stable < 3 and n < MAX_WARMUP):
        loaded = len(sys.modules)
        _cycle(app, Session, MainWindow, n)
        stable = stable + 1 if len(sys.modules) == loaded else 0
        n += 1
    _settle(app)
    modules_before = set(sys.modules)
    baseline_objects = len(gc.get_objects())
    baseline_listeners = len(theme._listeners)
    baseline_fixes = len(guidance._FIXES)

    for n in range(CYCLES):
        _cycle(app, Session, MainWindow, 1000 + n)
    _settle(app)

    assert _live_windows(MainWindow) == before_windows == 0
    assert len(theme._listeners) <= baseline_listeners
    assert len(guidance._FIXES) <= baseline_fixes
    growth = (len(gc.get_objects()) - baseline_objects) / baseline_objects
    late_imports = sorted(set(sys.modules) - modules_before)
    assert growth < 0.05, (
        f"object count grew {growth:.1%} over {CYCLES} cycles; "
        f"modules imported meanwhile: {late_imports[:10]}"
    )


def test_import_thread_holds_itself_until_finished() -> None:
    """The import thread outlives a freed wizard and then frees itself."""

    app = _app()
    from PySide6.QtCore import QCoreApplication, QEvent

    from panelcad.gui import import_wizard

    class _Job:
        def run(self) -> None:
            pass

        def status(self) -> dict:
            return {"pending": True}

    thread = import_wizard._ImportThread(_Job())
    thread.start()
    assert thread in import_wizard._RUNNING_IMPORTS
    assert thread.wait(5000)
    for _ in range(3):
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, int(QEvent.Type.DeferredDelete))
    assert thread not in import_wizard._RUNNING_IMPORTS
    assert not shiboken6.isValid(thread)
