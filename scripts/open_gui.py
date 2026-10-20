"""Launch the PanelCAD Qt GUI. python.exe, not pythonw."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)

from panelcad.gui.app import run_gui

path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
run_gui(path)
