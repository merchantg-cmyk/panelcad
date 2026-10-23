"""Run Python's cyclic GC only on the GUI thread, from the event loop.

CPython starts a cyclic collection on whichever thread crosses the allocation
threshold, at whatever line that thread is on. In a Qt app that can be a
worker thread, or the GUI thread inside a paint, a virtual override or a
widget destructor. A cycle holding PySide wrappers then destroys Qt objects
at the wrong time or on the wrong thread: an access violation or a heap
corruption (0xC0000374) on Windows.

``install`` turns automatic collection off for the whole process and starts
a timer on the GUI thread. Each tick collects the generations whose counts
are over the normal thresholds, the way automatic GC would, but only at a
top-level event-loop point. Worker threads never collect.
Set ``PANELCAD_AUTO_GC=1`` to keep CPython's automatic GC instead.
"""

from __future__ import annotations

import gc
import os

from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer

INTERVAL_MS = 500
_THRESHOLD = gc.get_threshold()
_controller: "GuiGcController | None" = None


def collect_if_due(threshold: tuple[int, ...] = _THRESHOLD) -> int:
    """Collect the oldest generation over its threshold. Returns it, or -1."""

    counts = gc.get_count()
    gen = -1
    for index in range(min(len(counts), len(threshold))):
        limit = threshold[index]
        if limit and counts[index] > limit:
            gen = index
        else:
            break
    if gen < 0:
        return -1
    gc.collect(gen)
    return gen


class GuiGcController(QObject):
    def __init__(self, app: QCoreApplication, interval_ms: int = INTERVAL_MS) -> None:
        super().__init__(app)
        self._app = app
        gc.disable()
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.tick)
        self._timer.start()

    def tick(self) -> int:
        if QThread.currentThread() is not self._app.thread():
            return -1
        return collect_if_due()


def install(app: QCoreApplication | None = None) -> GuiGcController | None:
    """Idempotent. Call on the GUI thread once the QApplication exists."""

    global _controller
    if os.environ.get("PANELCAD_AUTO_GC", "").strip() == "1":
        return None
    app = app or QCoreApplication.instance()
    if app is None:
        return None
    if _controller is not None:
        try:
            if _controller.parent() is app:
                gc.disable()
                return _controller
        except RuntimeError:  # the wrapper's C++ object is gone
            pass
    _controller = GuiGcController(app)
    return _controller


def installed() -> GuiGcController | None:
    return _controller
