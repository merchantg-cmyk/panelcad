"""Typed drawing agent. The JSONL operator in ``panelcad.cli.agent`` still runs Gemma.

The names below load on first use. ``panelcad.ai.drawing`` pulls in
pydantic-ai, MCP and the OpenAI client (several seconds on a cold Windows
start), and light submodules such as ``panelcad.ai.troubleshoot_explain`` or
``panelcad.ai.config`` must not pay for that when the main window is built.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from panelcad.ai.drawing import Drawing, drawing_agent, open_drawing

__all__ = ["Drawing", "drawing_agent", "open_drawing"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from panelcad.ai import drawing

        value = getattr(drawing, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
