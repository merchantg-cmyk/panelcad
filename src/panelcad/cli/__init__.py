"""Command-line package.

The Typer app stays lazy. Importing the JSONL agent used to load ``cli.main``
through this module, and the GUI imports that agent with the main window.
"""

from __future__ import annotations

__all__ = ["app", "main"]


def __getattr__(name: str):
    if name in {"app", "main"}:
        from panelcad.cli.main import app, main

        globals()["app"] = app
        globals()["main"] = main
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
