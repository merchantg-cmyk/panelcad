#!/usr/bin/env bash
# Source this: isolated run dir + env for one PanelCAD verification run.
# Usage: source env.sh [run-name]
set -u
REPO="${PANELCAD_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}"
# Runs live next to the repo by default, never inside it. Override with VERIFY_ROOT.
VERIFY_ROOT="${VERIFY_ROOT:-$(cd "$REPO/.." && pwd)/verify-panelcad-runs}"
RUN="${1:-${RUN:-run-$(date +%Y%m%d-%H%M%S)}}"
export RUN REPO VERIFY_ROOT
export PY="${PANELCAD_PY:-python3}"
export SCRATCH="$VERIFY_ROOT/scratch/$RUN"          # removed by cleanup.sh
export EVIDENCE="$VERIFY_ROOT/evidence/$RUN"        # survives cleanup
mkdir -p "$SCRATCH/home" "$SCRATCH/data" "$SCRATCH/config" "$SCRATCH/xdg" "$SCRATCH/work" "$EVIDENCE"
export HOME="$SCRATCH/home" XDG_CONFIG_HOME="$SCRATCH/xdg" XDG_DATA_HOME="$SCRATCH/xdg/data"
export PANELCAD_DATA_DIR="$SCRATCH/data" PANELCAD_CONFIG="$SCRATCH/config"
export PANELCAD_BUG_REPORTS=off PANELCAD_UPDATE_FEED= PANELCAD_AI_LOOKUP=off PANELCAD_WEB_ACCESS=off
export PANELCAD_NO_SPLASH=1 PANELCAD_NO_WELCOME=1 PANELCAD_KEEP_OFFSCREEN=1 PANELCAD_AUTOSAVE_SECONDS=0
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
unset PANELCAD_AI_API_KEY PANELCAD_SLACK_BOT_TOKEN PANELCAD_BUG_WEBHOOK GROK_HOME 2>/dev/null || true
export PYTHONPATH="$REPO/src"
echo "RUN=$RUN SCRATCH=$SCRATCH EVIDENCE=$EVIDENCE"
