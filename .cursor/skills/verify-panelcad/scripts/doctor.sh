#!/usr/bin/env bash
# Is this checkout + env worth driving? Exit 0 = yes. Changes nothing in the repo or
# user settings, but creates scratch/<run>/ dirs via env.sh: run cleanup.sh <run> after.
# Usage: scripts/doctor.sh [run-name]
source "$(dirname "$0")/env.sh" "${1:-doctor}" >/dev/null
fail=0
say(){ printf '%-10s %s\n' "$1" "$2"; }
"$PY" - <<PY || fail=1
import os, sys, pathlib
import panelcad, PySide6
from panelcad.version import __version__
src = pathlib.Path(panelcad.__file__).resolve()
want = pathlib.Path(os.environ["REPO"]).resolve() / "src"
print(f"panelcad   {__version__} from {src.parent}")
print(f"PySide6    {PySide6.__version__}")
print(f"platform   QT_QPA_PLATFORM={os.environ.get('QT_QPA_PLATFORM')}")
assert want in src.parents, f"panelcad imports from {src}, not {want}"
for bad in ("GROK_HOME", "PANELCAD_AI_API_KEY", "PANELCAD_BUG_WEBHOOK"):
    assert not os.environ.get(bad), f"{bad} set in verification env"
home = pathlib.Path(os.environ["HOME"]).resolve()
assert str(home).startswith(os.environ["VERIFY_ROOT"]), "HOME is not isolated"
from panelcad.api.session import Session
s = Session.open(pathlib.Path(os.environ["REPO"]) / "examples/sample_catalog.pcad.yaml")
print(f"sample     sample_catalog opens, sheets={[sh.id for sh in s.project.sheets]}")
PY
"$PY" -c "import pypdf" 2>/dev/null && say pypdf ok || { say pypdf MISSING; fail=1; }
command -v pdftoppm >/dev/null && say pdftoppm "ok (optional page renders)" || say pdftoppm "absent (page renders skipped)"
[ $fail = 0 ] && echo "DOCTOR OK" || echo "DOCTOR FAIL"
exit $fail
