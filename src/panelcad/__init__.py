"""PanelCAD package.

``Session`` stays lazy so ``python -m panelcad gui`` can paint the splash
before the parts library and the drawing window are imported.
"""

from __future__ import annotations

from panelcad.version import __version__

__all__ = ["Session", "__version__", "create_project", "open_project"]

_LAZY_EXPORTS = {
    "Session": "Session",
    "create_project": "create_project",
    "open_project": "open_project",
}


def __getattr__(name: str):
    attr = _LAZY_EXPORTS.get(name)
    if attr is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from panelcad.api import session as session_mod

    globals()["Session"] = session_mod.Session
    globals()["create_project"] = session_mod.create_project
    globals()["open_project"] = session_mod.open_project
    return globals()[attr]


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
