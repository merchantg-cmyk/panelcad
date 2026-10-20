"""Weak-model operator harness for PanelCAD.

A small local model issues JSONL ops. This package is the harness:
tiny cards, a forgiving parser, clipped replies, and traces. Grok
refines those pieces when the weak model fails. The model never
writes code and never clicks the GUI.
"""

from panelcad.sharpen.loop import run_sharpen
from panelcad.sharpen.tasks import TASKS, all_tasks, get_task

__all__ = ["TASKS", "all_tasks", "get_task", "run_sharpen"]
