import faulthandler
import gc
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
try:
    from panelcad.api.session import install_headless_qt_filter

    install_headless_qt_filter()
except Exception:
    pass

_FAULT_FILE = None
_FAULT_ANNOUNCED = False


def fault_log_path() -> Path:
    """Where this process's faulthandler writes. Override with PANELCAD_FAULT_LOG."""

    raw = os.environ.get("PANELCAD_FAULT_LOG", "").strip()
    if raw:
        return Path(raw)
    return Path(tempfile.gettempdir()) / "panelcad-pytest-fault.log"


def enable_pytest_faulthandler() -> None:
    """Arm faulthandler for the whole session, including interpreter finalization.

    Tests call ``mark_clean_exit``, which disables faulthandler. Arm again after
    each test and at session finish. The file stays open so a crash during
    finalization still lands in it.
    """

    global _FAULT_FILE, _FAULT_ANNOUNCED
    path = fault_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if _FAULT_FILE is None or _FAULT_FILE.closed:
        _FAULT_FILE = open(path, "ab", buffering=0)  # noqa: SIM115 — kept for the process
    try:
        faulthandler.enable(file=_FAULT_FILE, all_threads=True)
    except (OSError, RuntimeError, ValueError):
        return
    if not _FAULT_ANNOUNCED:
        sys.stderr.write(f"pytest faulthandler: {path}\n")
        _FAULT_ANNOUNCED = True


enable_pytest_faulthandler()

# Same rule as the app (panelcad.gui.gc_control): no automatic cyclic GC.
# A collection started inside a Qt callback, a widget destructor or a worker
# thread frees PySide wrappers at the wrong moment (0xC0000374 / 0xC0000005 on
# Windows). Collect between tests instead, at a point with no Qt frame active.
_GC_THRESHOLD = gc.get_threshold()
gc.disable()


def _collect_if_due() -> None:
    counts = gc.get_count()
    gen = -1
    for index in range(min(len(counts), len(_GC_THRESHOLD))):
        if _GC_THRESHOLD[index] and counts[index] > _GC_THRESHOLD[index]:
            gen = index
        else:
            break
    if gen >= 0:
        gc.collect(gen)


_GC_DIAG = os.environ.get("PANELCAD_GC_DIAG", "").strip()
_GC_DIAG_FILE = None


def _gc_diag(line: str) -> None:
    """Append one flushed line, so a fail-fast crash still shows the last step."""

    global _GC_DIAG_FILE
    if not _GC_DIAG:
        return
    try:
        if _GC_DIAG_FILE is None:
            _GC_DIAG_FILE = open(_GC_DIAG, "a", buffering=1, encoding="utf-8")  # noqa: SIM115
        _GC_DIAG_FILE.write(line + "\n")
        _GC_DIAG_FILE.flush()
        os.fsync(_GC_DIAG_FILE.fileno())
    except OSError:
        pass


def _flush_deferred_deletes() -> None:
    """Deliver queued deleteLater before Python frees wrappers."""

    qtcore = sys.modules.get("PySide6.QtCore")
    if qtcore is None:
        return
    try:
        if qtcore.QCoreApplication.instance() is None:
            return
        qtcore.QCoreApplication.sendPostedEvents(None, int(qtcore.QEvent.Type.DeferredDelete))
    except Exception:  # noqa: BLE001
        pass


def _gc_garbage_summary() -> str:
    """Find what the next collect will free (DEBUG_SAVEALL), describe it, let it go.

    Opt-in with ``PANELCAD_GC_DIAG``. The objects are only listed: they go back
    to being unreachable cycles, and the normal ``gc.collect()`` right after
    this frees them, so a crash in that free comes after a flushed line naming
    what was in the garbage. Listed one by one: Shiboken wrappers whose C++
    object is still alive (this collect destroys it), Qt types, and functions
    whose closure holds a Qt object. Wrappers whose C++ object is already gone
    are counted by type. Nothing here calls a method on a Qt object.
    """

    from collections import Counter

    try:
        import shiboken6
    except Exception:  # noqa: BLE001 — no Qt in this run
        shiboken6 = None
    old_debug = gc.get_debug()
    gc.set_debug(old_debug | gc.DEBUG_SAVEALL)
    try:
        gc.collect()
        garbage = list(gc.garbage)
    finally:
        gc.garbage.clear()
        gc.set_debug(old_debug)
    try:
        counts = Counter(type(obj).__qualname__ for obj in garbage)
        live: list[str] = []
        dead: Counter = Counter()
        other: list[str] = []
        if shiboken6 is not None:
            base = shiboken6.Object
            for obj in garbage:
                kind = type(obj)
                name = f"{kind.__module__}.{kind.__qualname__}"
                if isinstance(obj, base):
                    if shiboken6.isValid(obj):
                        live.append(
                            f"{name}@{id(obj):x}"
                            f"(py_owned={shiboken6.ownedByPython(obj)},"
                            f"py_created={shiboken6.createdByPython(obj)})"
                        )
                    else:
                        dead[name] += 1
                elif isinstance(obj, type) and issubclass(obj, base):
                    other.append(f"type:{obj.__module__}.{obj.__qualname__}")
                elif callable(obj) and getattr(obj, "__closure__", None):
                    held = []
                    for cell in obj.__closure__:
                        try:
                            value = cell.cell_contents
                        except ValueError:
                            continue
                        if isinstance(value, base):
                            state = "live" if shiboken6.isValid(value) else "dead"
                            held.append(f"{type(value).__qualname__}:{state}")
                    if held:
                        other.append(
                            f"fn:{getattr(obj, '__qualname__', '?')}->{','.join(held)}"
                        )
        top = ", ".join(f"{name}={n}" for name, n in counts.most_common(10))
        line = f"garbage n={len(garbage)} types[{top}]"
        line += f" qt_live={len(live)}[" + "; ".join(live[:40])
        line += (f" +{len(live) - 40} more" if len(live) > 40 else "") + "]"
        line += f" qt_dead={sum(dead.values())}["
        line += ", ".join(f"{name}={n}" for name, n in dead.most_common(25)) + "]"
        if other:
            line += " other[" + "; ".join(other[:20])
            line += (f" +{len(other) - 20} more" if len(other) > 20 else "") + "]"
        return line
    except Exception as exc:  # noqa: BLE001 — diagnostics must not raise
        return f"garbage n={len(garbage)} summary-error={type(exc).__name__}"
    finally:
        del garbage


@pytest.fixture(autouse=True)
def _gc_between_tests(request):
    gc.disable()
    _gc_diag(f"start {request.node.nodeid}")
    yield
    gc.disable()
    _gc_diag(f"end {request.node.nodeid} counts={gc.get_count()}")
    _flush_deferred_deletes()
    _gc_diag("deferred-deletes flushed")
    if _GC_DIAG:
        _gc_diag(_gc_garbage_summary())
    # Full collection every test: garbage never piles up into one large
    # collection of stale Qt wrappers long after their owners went away.
    found = gc.collect()
    _gc_diag(f"collected {found}")


def _qt_instances(cls: type) -> list:
    """Live wrappers of ``cls``. Dead Shiboken objects raise and are skipped."""

    found = []
    for obj in gc.get_objects():
        try:
            if isinstance(obj, cls):
                found.append(obj)
        except Exception:  # noqa: BLE001 — a half-destroyed wrapper is not live
            continue
    return found


def release_qt_for_exit() -> None:
    """Close Qt objects left by tests so the interpreter can finish normally.

    Measured on Linux, offscreen, faulthandler on, no ``os._exit``. Leaving
    three MainWindows up and exiting normally segfaulted in 6 of 8 runs
    (SIGSEGV, no Python frame, PySide6.QtWidgets still loaded). Stopping the
    timers and leaving the windows up still segfaulted (3 of 4). Closing every
    top-level widget exited 0 in 4 of 4. A ``_CheckWorker`` QThread still
    running at exit aborted in 5 of 6 runs (SIGABRT) and segfaulted in the
    other. Waiting for that worker, then ``close`` and ``deleteLater`` on every
    top-level widget, exited 0 in 6 of 6.

    What is still alive right after ``MainWindow()`` and one event-loop turn:
    the 400 ms AI timer, the 450 ms check timer, a parentless 400 ms single-shot
    (startup check), and top-level QMenu, QFrame, and MainWindow objects. No
    QThread until ``LiveCheckRunner.run_now`` starts ``_CheckWorker``.

    The sweep is by type, so windows and timers added on main are included.
    QuoteDock is a child of MainWindow and closes with it. CutoutDialog,
    ConvertDialog, CompareDialog, IoWizard, and BugReportDialog are QDialogs,
    so they are top-level widgets and get ``close`` plus ``deleteLater``.
    Parentless ``QTimer.singleShot`` calls in the cutout dialog, screen fit,
    docks, and the bug reporter are stopped with every other QTimer. I/O sheet
    generation runs on the calling thread. The only QThread is still
    ``_CheckWorker``. Daemon threads named ``panelcad*`` (the update worker and
    ``panelcad-autosave``) are joined.

    The Windows full suite at 13e12e9 died after the summary with 0xC0000409
    on 1 of 2 runs. This function does not call ``os._exit``. After a
    MainWindow, ``nt._exit`` crashes 0xC0000005 on Windows.
    """

    try:
        from PySide6.QtCore import QCoreApplication, QEvent, QThread, QThreadPool, QTimer
        from PySide6.QtWidgets import QApplication
    except Exception:  # noqa: BLE001 — Qt is optional for a non-GUI run
        return
    app = QApplication.instance()
    if app is None:
        return
    for timer in _qt_instances(QTimer):
        try:
            timer.stop()
        except Exception:  # noqa: BLE001
            continue
    current = QThread.currentThread()
    for thread in _qt_instances(QThread):
        try:
            if thread is current or not thread.isRunning():
                continue
            thread.requestInterruption()
            thread.quit()
            thread.wait(2000)
        except Exception:  # noqa: BLE001
            continue
    try:
        pool = QThreadPool.globalInstance()
        if pool.activeThreadCount():
            pool.waitForDone(2000)
    except Exception:  # noqa: BLE001
        pass
    for thread in threading.enumerate():
        if thread is threading.current_thread():
            continue
        if thread.daemon and not thread.name.startswith("panelcad"):
            continue
        try:
            thread.join(timeout=1.0)
        except RuntimeError:
            continue
    for widget in list(app.topLevelWidgets()):
        try:
            # A reconstruct wizard left open asks "Discard changes?" and that
            # modal never returns offscreen. Teardown has nobody to click it.
            if hasattr(widget, "_accepting"):
                widget._accepting = True
            widget.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            widget.deleteLater()
        except Exception:  # noqa: BLE001
            pass
    for _ in range(8):
        try:
            app.processEvents()
        except Exception:  # noqa: BLE001
            break
    # processEvents leaves DeferredDelete queued. Deliver it or the widgets
    # are still alive when the interpreter finishes.
    try:
        QCoreApplication.sendPostedEvents(None, int(QEvent.Type.DeferredDelete))
        app.processEvents()
    except Exception:  # noqa: BLE001
        pass
    gc.collect()
    try:
        app.processEvents()
    except Exception:  # noqa: BLE001
        pass


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    """Print the summary, release Qt, and let Python exit with pytest's code.

    The crash is native finalization of widgets and a check thread that tests
    leave alive. See ``release_qt_for_exit``. Do not ``os._exit``.
    """

    result = yield
    sys.stdout.flush()
    sys.stderr.flush()
    enable_pytest_faulthandler()
    try:
        release_qt_for_exit()
    except Exception:  # noqa: BLE001 — a Python error here must not hide the suite
        traceback.print_exc()
    return result

if "PANELCAD_CONFIG" not in os.environ:
    os.environ["PANELCAD_CONFIG"] = tempfile.mkdtemp(prefix="panelcad-cfg-")

@pytest.fixture(autouse=True)
def _pytest_faulthandler_stays_armed():
    """``mark_clean_exit`` disables faulthandler. Turn it back on for the next test."""

    enable_pytest_faulthandler()
    yield
    enable_pytest_faulthandler()


def detached_panelcad_reason(command: object) -> str | None:
    """Why this argv must not be started from a test, or None when it is safe.

    ``python -m panelcad gui`` is the desktop app. ``python -m panelcad.update.handoff``
    is the detached installer helper. Both keep running after pytest exits on
    Windows. ``panelcad agent`` and ``panelcad update check`` are not those.
    """

    if isinstance(command, (str, bytes)):
        text = command.decode("utf-8", "replace") if isinstance(command, bytes) else command
        parts = tuple(text.split())
    elif isinstance(command, (list, tuple)):
        parts = tuple(os.fsdecode(os.fspath(part)) for part in command)
    else:
        return None
    for index, part in enumerate(parts):
        if part != "-m" or index + 1 >= len(parts):
            continue
        module = parts[index + 1]
        if module == "panelcad.update.handoff":
            # ``--help`` is the runpy warning check. It exits. A plan path does not.
            if "--help" in parts or "-h" in parts:
                return None
            return "panelcad.update.handoff"
        if module == "panelcad" and index + 2 < len(parts) and parts[index + 2] == "gui":
            return "panelcad gui"
    for index, part in enumerate(parts[:-1]):
        stem = Path(part).name.lower()
        if stem in {"panelcad", "panelcad.exe"} and parts[index + 1] == "gui":
            return "panelcad gui"
    return None


@pytest.fixture(scope="session", autouse=True)
def _block_unstubbed_panelcad_processes():
    """Fail a test that starts the GUI or the update helper with the real Popen.

    ``panelcad.update.handoff`` looks up ``subprocess.Popen`` when it launches.
    Tests that need a launch replace that name, or ``_relaunch``, first.
    ``python -c`` lock processes and ``panelcad agent`` stay allowed.
    """

    real = subprocess.Popen

    class _GuardedPopen(real):  # type: ignore[misc, valid-type]
        """Same launch, but ``Popen[bytes]`` still type-checks after the swap.

        A plain function breaks libraries that subscript ``subprocess.Popen``
        while a test is running (the drawing agent imports one).
        """

        def __init__(self, command, *args, **kwargs):
            reason = detached_panelcad_reason(command)
            if reason is not None:
                raise AssertionError(
                    f"test started a real {reason} process via subprocess.Popen: {command!r}. "
                    "Stub the launch and assert on the command."
                )
            super().__init__(command, *args, **kwargs)

    subprocess.Popen = _GuardedPopen  # type: ignore[misc, assignment]
    try:
        yield
    finally:
        subprocess.Popen = real


_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_EXAMPLES_READ_ATTEMPTS = 8
_EXAMPLES_READ_WAIT_S = 0.025
_EXAMPLES_SKIP_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
_EXAMPLES_SKIP_SUFFIXES = (
    ".pcad.ai.jsonl",
    ".pcad.ai-in.jsonl",
    ".pcad.ai-prompt.jsonl",
    ".pcad.ai-in.cursor",
    ".pcad.ai-prompt.cursor",
)
_EXAMPLES_TEXT_SUFFIXES = {
    ".yaml",
    ".yml",
    ".csv",
    ".md",
    ".svg",
    ".txt",
    ".json",
    ".xml",
    ".xsd",
    ".dxf",
}


def _examples_skip(path: Path) -> bool:
    """Journals and OS junk change without a drawing change. Ignore them."""

    name = path.name.lower()
    if name in _EXAMPLES_SKIP_NAMES:
        return True
    return any(name.endswith(suffix) for suffix in _EXAMPLES_SKIP_SUFFIXES)


def _examples_bytes(path: Path) -> bytes:
    """Read ``path``. Retry when Windows still has the file open."""

    last: PermissionError | None = None
    for attempt in range(_EXAMPLES_READ_ATTEMPTS):
        try:
            data = path.read_bytes()
        except PermissionError as exc:
            last = exc
            if attempt + 1 < _EXAMPLES_READ_ATTEMPTS:
                time.sleep(_EXAMPLES_READ_WAIT_S)
            continue
        if path.suffix.lower() in _EXAMPLES_TEXT_SUFFIXES:
            data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        return data
    assert last is not None
    raise last


def _examples_snapshot(root: Path) -> dict[str, str]:
    """Hash shipped files under examples/. A new or edited drawing changes the map.

    AI journals are append-only and carry timestamps. Windows may also lock a
    file for a moment, or rewrite a text file with CRLF. Those are not a
    different drawing.
    """

    snap: dict[str, str] = {}
    if not root.is_dir():
        return snap
    paths: list[Path] = []
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file() or _examples_skip(path):
                continue
        except OSError:
            continue
        paths.append(path)
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix().lower()):
        rel = path.relative_to(root).as_posix()
        try:
            data = _examples_bytes(path)
        except FileNotFoundError:
            continue
        snap[rel] = hashlib.sha256(data).hexdigest()
    return snap


@pytest.fixture(scope="session", autouse=True)
def examples_tree_unchanged():
    """Fail the suite if any test writes into the repo or install examples/."""
    before = _examples_snapshot(_EXAMPLES)
    yield
    after = _examples_snapshot(_EXAMPLES)
    if before == after:
        return
    changed = [rel for rel in sorted(set(before) | set(after)) if before.get(rel) != after.get(rel)]
    raise AssertionError(
        "pytest changed files under examples/: " + ", ".join(changed)
    )


@pytest.fixture(autouse=True)
def _panelcad_diag_sandbox(tmp_path, monkeypatch):
    """Keep logs, crash files, and settings inside this test's temp dir."""

    monkeypatch.setenv("PANELCAD_DATA_DIR", str(tmp_path / "panelcad-data"))
    monkeypatch.delenv("PANELCAD_BUG_WEBHOOK", raising=False)
    monkeypatch.delenv("PANELCAD_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("PANELCAD_SLACK_CHANNEL", raising=False)
    monkeypatch.delenv("PANELCAD_BUG_RELAY", raising=False)
    monkeypatch.delenv("PANELCAD_BUG_REPORTS", raising=False)
    monkeypatch.delenv("PANELCAD_RELEASE_BUG_WEBHOOK", raising=False)
    monkeypatch.delenv("PANELCAD_RELEASE_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("PANELCAD_RELEASE_SLACK_CHANNEL", raising=False)
    monkeypatch.delenv("PANELCAD_RELEASE_UPDATE_FEED", raising=False)
    monkeypatch.delenv("PANELCAD_PUBLIC_BUILD", raising=False)
    monkeypatch.delenv("PANELCAD_UPDATE_FEED", raising=False)
    monkeypatch.delenv("PANELCAD_GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("PANELCAD_GIT_SHA", raising=False)
    monkeypatch.delenv("PANELCAD_DIAGNOSTICS", raising=False)
    monkeypatch.delenv("PANELCAD_AUTOSAVE_SECONDS", raising=False)
    from panelcad.diag.crash import reset_state
    from panelcad.diag.log import reset_logging
    from panelcad.diag.reporters import clear_extra_reporters

    reset_logging()
    reset_state()
    clear_extra_reporters()
    yield
    reset_logging()
    reset_state()
    clear_extra_reporters()


@pytest.fixture
def confirm_generic_layout(monkeypatch):
    """Stamp a confirmed envelope so packing/cutout tests can place a library symbol.

    Opt in with ``@pytest.mark.confirm_generic_layout``. Reconstruct and
    layout-ready tests must not use this — they need the real refuse path.
    """

    from tests.support import patch_layout_place_confirms

    patch_layout_place_confirms(monkeypatch)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "confirm_generic_layout: confirm library envelopes so packing/cutout tests can place symbols",
    )


@pytest.fixture(autouse=True)
def _confirm_generic_layout_places(request, monkeypatch):
    """Activate only when the test (or its class/module) opted into the marker."""

    marker = request.node.get_closest_marker("confirm_generic_layout")
    if marker is None:
        return
    from tests.support import patch_layout_place_confirms

    patch_layout_place_confirms(monkeypatch)


@pytest.fixture(autouse=True)
def isolated_panelcad_config(tmp_path, monkeypatch):
    """Each test gets its own settings file so a dark-mode test cannot leak."""

    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path / "panelcad-config"))
    from panelcad.settings import invalidate

    invalidate()
    yield
    invalidate()


@pytest.fixture(autouse=True)
def _tests_do_not_block_on_dialogs(monkeypatch):
    """A modal with nobody to click it must not stall the suite.

    Offscreen Qt on Windows leaves ``QDialog.exec`` and a native file dialog
    waiting at 0% CPU. A click the test already scheduled still runs first.
    A dialog that is still open after a few seconds is rejected. File dialogs
    return a cancelled choice unless the test stubs its own path.
    """

    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import (
            QApplication,
            QDialog,
            QFileDialog,
            QInputDialog,
            QMessageBox,
        )
    except Exception:  # noqa: BLE001 — a non-GUI run has nothing to dismiss
        yield
        return

    live: set[int] = set()
    serial = {"n": 0}

    def arm(kinds: type | tuple[type, ...]) -> int:
        serial["n"] += 1
        token = serial["n"]
        live.add(token)

        def dismiss(token: int = token, kinds: type | tuple[type, ...] = kinds) -> None:
            if token not in live:
                return
            app = QApplication.instance()
            if app is None:
                return
            for widget in list(app.topLevelWidgets()):
                try:
                    if isinstance(widget, kinds) and widget.isVisible():
                        widget.reject()
                except Exception:  # noqa: BLE001 — a dying wrapper is already gone
                    continue

        if QApplication.instance() is not None:
            try:
                QTimer.singleShot(8000, dismiss)
            except Exception:  # noqa: BLE001 — no event loop yet, so nothing is modal
                pass
        return token

    real_exec = QDialog.exec

    def guarded_exec(self, *args, **kwargs):  # noqa: ANN001
        token = arm(QDialog)
        try:
            return real_exec(self, *args, **kwargs)
        finally:
            live.discard(token)

    try:
        monkeypatch.setattr(QDialog, "exec", guarded_exec)
    except (AttributeError, TypeError):
        pass

    def wrap_static(cls: type, name: str, kinds: type) -> None:
        real = getattr(cls, name)

        def wrapped(*args, **kwargs):  # noqa: ANN002
            token = arm(kinds)
            try:
                return real(*args, **kwargs)
            finally:
                live.discard(token)

        try:
            monkeypatch.setattr(cls, name, wrapped)
        except (AttributeError, TypeError):
            return

    for name in ("information", "warning", "critical", "question"):
        wrap_static(QMessageBox, name, QMessageBox)
    for name in ("getText", "getMultiLineText", "getInt", "getDouble", "getItem"):
        if hasattr(QInputDialog, name):
            wrap_static(QInputDialog, name, QInputDialog)

    def _cancelled_file(*_args, **_kwargs):
        return "", ""

    def _cancelled_files(*_args, **_kwargs):
        return [], ""

    def _cancelled_dir(*_args, **_kwargs):
        return ""

    for name, stub in (
        ("getOpenFileName", _cancelled_file),
        ("getSaveFileName", _cancelled_file),
        ("getOpenFileNames", _cancelled_files),
        ("getExistingDirectory", _cancelled_dir),
    ):
        try:
            monkeypatch.setattr(QFileDialog, name, stub)
        except (AttributeError, TypeError):
            continue
    yield
