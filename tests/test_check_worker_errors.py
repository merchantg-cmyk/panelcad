"""A snapshot the checks cannot read must not raise inside QThread.run."""

from __future__ import annotations

from panelcad.api.session import Session


def test_check_worker_reports_an_invalid_snapshot_instead_of_raising(qapp):
    from panelcad.gui.problems import _CheckWorker

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


def test_live_runner_is_not_left_busy_after_a_failed_check(qtbot):
    from panelcad.gui.problems import LiveCheckRunner

    session = Session.create("runner-errors")
    session.sheet().drawing_style = "not-a-style"  # type: ignore[assignment]
    runner = LiveCheckRunner(session)
    try:
        runner.run_now()
        qtbot.waitUntil(lambda: not runner._busy and runner._gc_pause is None, timeout=10000)
        session.sheet().drawing_style = "ladder"
        with qtbot.waitSignal(runner.rows_ready, timeout=10000):
            runner.run_now()
        qtbot.waitUntil(lambda: runner._gc_pause is None, timeout=10000)
    finally:
        runner.stop()
