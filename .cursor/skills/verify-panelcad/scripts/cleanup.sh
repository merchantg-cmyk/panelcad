#!/usr/bin/env bash
# Remove scratch state for a run. Never touches evidence/. Kills only the PID
# recorded in scratch/<run>/app.pid, and only if /proc/<pid>/cmdline contains
# drive.py (guards against PID reuse). Never kills by process name.
# Usage: scripts/cleanup.sh <run-name>
set -u
REPO="${PANELCAD_REPO:-$(cd "$(dirname "$0")/../../../.." && pwd)}"
VERIFY_ROOT="${VERIFY_ROOT:-$(cd "$REPO/.." && pwd)/verify-panelcad-runs}"
RUN="${1:?run-name}"
SCRATCH="$VERIFY_ROOT/scratch/$RUN"
if [ -f "$SCRATCH/app.pid" ]; then
  pid=$(cat "$SCRATCH/app.pid")
  if [ -r "/proc/$pid/cmdline" ] && tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q 'drive\.py'; then
    kill "$pid" 2>/dev/null && echo "killed $pid (drive.py)"
  else
    echo "app.pid $pid is not a live drive.py; not killing"
  fi
fi
case "$SCRATCH" in "$VERIFY_ROOT"/scratch/?*) rm -rf -- "$SCRATCH" ;; esac
ls -d "$VERIFY_ROOT/evidence/$RUN" >/dev/null 2>&1 && echo "evidence kept: $VERIFY_ROOT/evidence/$RUN ($(ls "$VERIFY_ROOT/evidence/$RUN" | wc -l) files)" || echo "no evidence for $RUN"
