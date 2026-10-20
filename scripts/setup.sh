#!/usr/bin/env bash
# Linux setup for PanelCAD
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
echo "OK. Activate with:  source .venv/bin/activate"
echo "Then:  ./scripts/check"
echo "Then:  panelcad gui"
