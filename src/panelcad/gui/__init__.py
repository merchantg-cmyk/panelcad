"""Qt GUI. ``run_gui`` is lazy so the splash module can load on its own."""

from __future__ import annotations


def __getattr__(name: str):
    if name == "run_gui":
        from panelcad.gui.app import run_gui

        globals()["run_gui"] = run_gui
        return run_gui
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | {"run_gui"})


__all__ = ["run_gui"]
