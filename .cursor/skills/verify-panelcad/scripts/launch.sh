#!/usr/bin/env bash
# Launch the real PanelCAD GUI (same launch_gui() path as `panelcad gui`) in an
# isolated env and drive one feature. App and driver share one process: Qt
# widgets are only reachable in-process, so there is no server to keep alive.
# Usage: scripts/launch.sh <feature> [run-name]     e.g. launch.sh print-package
set -euo pipefail
FEATURE="${1:?feature, e.g. print-package}"
source "$(dirname "$0")/env.sh" "${2:-}"
"$(dirname "$0")/doctor.sh" "$RUN" > "$EVIDENCE/doctor.txt" 2>&1 || { cat "$EVIDENCE/doctor.txt"; exit 2; }
set +e
timeout 180 "$PY" "$(dirname "$0")/drive.py" "$FEATURE" 2>&1 | tee "$EVIDENCE/drive.log"
rc=${PIPESTATUS[0]}
echo "exit=$rc" >> "$EVIDENCE/drive.log"
echo "evidence: $EVIDENCE"
exit $rc
