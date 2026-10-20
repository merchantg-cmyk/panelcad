"""Rotating log file. About 1 MB, five backups. No file bodies and no secrets."""

from __future__ import annotations

import logging
import platform
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from panelcad.diag.paths import ensure_dir, log_file_path, logs_dir
from panelcad.diag.redact import redact_text
from panelcad.version import __version__

MAX_LOG_BYTES = 1_000_000
LOG_BACKUP_COUNT = 5
LOG_NAME = "panelcad"

_configured = False


class RedactFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def _logger() -> logging.Logger:
    logger = logging.getLogger(LOG_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def reset_logging() -> None:
    """Drop file handlers so tests do not keep writing the previous temp log."""

    global _configured
    logger = _logger()
    for handler in list(logger.handlers):
        try:
            handler.close()
        except OSError:
            pass
        logger.removeHandler(handler)
    _configured = False


def logging_configured() -> bool:
    return _configured


def configure_logging(
    *,
    max_bytes: int = MAX_LOG_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
) -> Path:
    """Install the rotating file handler and write one startup line."""

    global _configured
    folder = ensure_dir(logs_dir())
    path = log_file_path()
    logger = _logger()
    for handler in list(logger.handlers):
        try:
            handler.close()
        except OSError:
            pass
        logger.removeHandler(handler)
    handler = RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(RedactFormatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    _configured = True
    log_startup()
    return path if path.is_file() else folder / "panelcad.log"


def pyside_version() -> str:
    try:
        import PySide6
    except Exception:  # noqa: BLE001
        return ""
    return str(getattr(PySide6, "__version__", "") or "")


def install_path() -> Path:
    return Path(__file__).resolve().parents[1]


def log_startup() -> None:
    side = pyside_version() or "not-loaded"
    log_event(
        "startup",
        f"version={__version__} os={platform.platform()} python={platform.python_version()} "
        f"pyside6={side} install={install_path()} executable={sys.executable}",
    )


def log_event(kind: str, message: str = "", *, level: int | None = None) -> None:
    if not _configured:
        return
    text = redact_text(message or "")
    if len(text) > 500:
        text = text[:500]
    if level is None:
        if kind == "error":
            level = logging.ERROR
        elif kind == "warning":
            level = logging.WARNING
        else:
            level = logging.INFO
    try:
        _logger().log(level, "%s %s", kind, text)
    except Exception:  # noqa: BLE001
        return


def log_io(action: str, path: object = None) -> None:
    shown = "" if path is None else redact_text(str(path))
    log_event(action, shown)


def log_agent(op: str, ok: bool) -> None:
    name = "".join(ch for ch in str(op or "") if ch.isalnum() or ch in {"_", "-"})
    log_event("agent", f"op={name or 'unknown'} ok={str(bool(ok)).lower()}")


def tail_log(*, max_chars: int = 12_000) -> str:
    path = log_file_path()
    if not path.is_file():
        return ""
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(data) > max_chars:
        data = data[-max_chars:]
    return redact_text(data)


def read_log_text() -> str:
    path = log_file_path()
    if not path.is_file():
        return ""
    try:
        return redact_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
