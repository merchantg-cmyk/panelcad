"""Start logging and crash capture for a GUI or CLI process."""

from __future__ import annotations

import atexit
import os

from panelcad.diag.crash import UncleanExit, begin_session, crash_was_fatal, mark_clean_exit
from panelcad.diag.log import configure_logging

_atexit_registered = False
_installed = False


def _under_pytest() -> bool:
    if os.environ.get("PANELCAD_DIAGNOSTICS", "").strip().lower() in {"1", "true", "yes"}:
        return False
    return "PYTEST_CURRENT_TEST" in os.environ


def _on_exit() -> None:
    if crash_was_fatal():
        return
    mark_clean_exit(force=True)


def reset_process_diagnostics() -> None:
    """Allow a later ``install_process_diagnostics`` to arm again. Tests use this."""

    global _installed
    _installed = False


def install_process_diagnostics(*, gui: bool) -> UncleanExit | None:
    """Log startup, arm crash hooks, and return an unclean previous exit if there was one.

    Arming twice in one process (the CLI callback and ``panelcad agent``) does not
    write a second startup line or take anyone else's lock.

    The full pytest suite leaves this off unless ``PANELCAD_DIAGNOSTICS`` is set,
    so tests do not replace ``sys.excepthook`` or write outside their temp dir.
    """

    global _atexit_registered, _installed
    if _under_pytest():
        return None
    if _installed:
        return None
    configure_logging()
    unclean = begin_session(gui=gui)
    if gui is False:
        from panelcad.diag.crash import install_cli_exception_hook

        install_cli_exception_hook()
    if not _atexit_registered:
        atexit.register(_on_exit)
        _atexit_registered = True
    _installed = True
    return unclean
