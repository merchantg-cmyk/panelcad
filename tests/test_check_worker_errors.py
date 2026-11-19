"""A snapshot the checks cannot read must not raise inside QThread.run."""

from __future__ import annotations

import time

from PySide6.QtWidgets import QApplication

from panelcad.api.session import Session


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(app: QApplication, predicate, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for LiveCheckRunner"
        app.processEvents()
        time.sleep(0.01)


def test_check_worker_reports_an_invalid_snapshot_instead_of_raising():
    from panelcad.gui.problems import _CheckWorker

    _app()
    session = Session.create("worker-errors")
    payload = session.project.model_dump(mode="python")
    payload["sheets"][0]["drawing_style"] = "not-a-style"
    worker = _CheckWorker(payload, session.catalog, False)
    got: dict[str, object] = {}
    worker.check_failed.connect(lambda msg: got.setdefault("failed", msg))
    worker.finished_rows.connect(lambda rows: got.setdefault("rows", rows))
    worker.run()  # same body the thread runs, called directly
    assert "rows" not in got
    assert "ValidationError" in str(got.get("failed"))
    worker.deleteLater()


def test_live_runner_is_not_left_busy_after_a_failed_check():
    from panelcad.gui.problems import LiveCheckRunner

    app = _app()
    session = Session.create("runner-errors")
    session.sheet().drawing_style = "not-a-style"  # type: ignore[assignment]
    runner = LiveCheckRunner(session)
    got_rows: list[object] = []
    runner.rows_ready.connect(lambda rows: got_rows.append(rows))
    try:
        runner.run_now()
        _wait_until(app, lambda: not runner._busy and runner._gc_pause is None)
        session.sheet().drawing_style = "ladder"
        runner.run_now()
        _wait_until(app, lambda: bool(got_rows) and runner._gc_pause is None)
    finally:
        runner.stop()
