---
name: verify-panelcad
description: Drive the real PanelCAD Qt desktop GUI (PySide6) offscreen through its menus and dialogs, capture screenshots and output files, and prove a PanelCAD change works. Use before calling any PanelCAD GUI, export, wiring, conversion, or design-check work done.
---

# verify-panelcad

PanelCAD is a PySide6 desktop app (`panelcad gui [project.pcad.yaml]` → `panelcad.gui.app.launch_gui`). There is no server or debug port: Qt widgets are only reachable in-process, so the driver (`scripts/drive.py`) calls the same `launch_gui()` the CLI uses, then drives the window through `QMenuBar` actions (by visible text), dialog widgets and `QTest` clicks/keys. A proof never calls `Session` methods or `MainWindow` slots directly.

Paths below are relative to this skill folder. Repo = four levels up from `scripts/` (override `PANELCAD_REPO`). Python defaults to `python3` on PATH (override `PANELCAD_PY`, e.g. a venv with `pip install -e .` plus `pypdf`). Runs go to `<repo>/../verify-panelcad-runs` (override `VERIFY_ROOT`), never inside the repo. Keep machine-specific values in your shell env, not in the repo.

Never touch a main install, its venv, `.panelcad`, `.grok`, `GROK_HOME` or `auth.json`. `env.sh` points HOME, XDG, `PANELCAD_DATA_DIR`, `PANELCAD_CONFIG` and QSettings at the run's scratch dir and turns off bug reports, the update feed, AI lookup and web access. Never print token or webhook values. Do not run the full test suite for a proof.

## Launch

```bash
scripts/launch.sh print-package my-run      # feature, run name
```

What it does: sources `scripts/env.sh my-run` (isolated dirs and env, `QT_QPA_PLATFORM=offscreen`), runs the doctor (saved to evidence), copies the feature's sample from `examples/` into scratch (the repo sample is never edited), launches the GUI on it, and drives the feature. Starting from an empty data dir needs no migrations or seed data. The welcome dialog is suppressed with `PANELCAD_NO_WELCOME=1`.

Ready means the driver logged `PASS  ready: main window up with project <path>`: `MainWindow` is visible and `win.session.path` is the scratch copy. The 2 s startup wait drains the startup-prompt and update timers. Any modal dialog that is not expected (recovery, update, crash) is screenshotted as `NN-unexpected-<Class>.png` and fails the run.

Teardown: the driver closes the window and the process exits. There is no long-lived instance. `timeout 180` bounds a hung run.

## Doctor

```bash
scripts/doctor.sh [run-name]      # prints DOCTOR OK / DOCTOR FAIL; creates scratch/<run>/
```

This checks that `panelcad` imports from this repo's `src/` (not from an editable install somewhere else), prints the version and PySide6 version, confirms the offscreen platform, confirms HOME is isolated, confirms no secret env vars are set, confirms `examples/sample_catalog.pcad.yaml` opens, confirms `pypdf` imports (required), and reports whether `pdftoppm` is present (optional; page PNG renders are skipped without failing when it is missing). Run it first whenever anything looks off. It changes nothing in the repo or user settings, but it is not read-only: it creates `scratch/<run>/` dirs, so run `scripts/cleanup.sh <run>` afterwards.

## Drive

`scripts/drive.py <feature>` (always called through `launch.sh`). Implemented: `print-package`. Every feature in `features/` lists its menu path and widget handles. Add a new journey as a `feature_<name>(app, win)` function and register it in `FEATURES` with its sample.

Handles to use, in order of preference:
- **Menu actions:** use `menu_action(win, "File", "Print Package…")`, which matches the visible text with `&` stripped. Menus are `&File`, `&Edit`, `&View`, `&Insert`, `&Tools`, `&Reports` and Help.
- **objectNames:** docks are `problemsDock`, `libraryDock`, `inspectorDock`, `bomDock` and `aiDock`; the canvas is `sheetCanvas`; the toolbar is `mainToolbar`. In the converter: `convert-review`, `chunk:<id>`, `accept:<id>`, `rerun:<id>`, `revert:<id>`, `keep-existing-numbers` and `convert-compare`.
- **Dialog attributes:** for example `PrintPackageDialog.sheet_list`, `.checks[<section>]`, `.paper` and `.path`. Read them and click them with `QTest`; never call `.values()` or accept with forced state.
- **Modal dialogs:** `exec()` blocks, so `ModalPump` polls `QApplication.activeModalWidget()` every 150 ms and hands each modal to the next expected handler in order.

## Evidence

Everything goes to `$VERIFY_ROOT/evidence/<run>/`, which survives cleanup:
- `doctor.txt` and `drive.log` (the PASS/FAIL line for every check, plus `exit=`);
- `result.json`, with `ok` and each check;
- `NN-*.png` widget grabs of the main window before and after and of each dialog, before and after the user input;
- output files (for example `verify_package.pdf`, `pdfinfo.txt`, `pdf-outline.txt`, `verify_package.txt`, and `pdf-page-*.png` renders only when `pdftoppm` exists), and `result-dialog.txt` with the app's own result message.

Proof standards:
- Use the real user path: menus, dialog widgets, QTest input. No internal setters or test-only hooks.
- Capture the action and the resulting state (dialog before and after input, the result box, the status bar), not only the final screen.
- Check side effects independently of the UI: the file exists, and its content (page count, PDF outline, text) matches what was selected.
- No mocks. Network features are switched off by env. A feature that needs a network or AI provider is out of scope until a real provider boundary is in place.
- If a sample lacks a precondition, the journey fails. Record why in that feature's Gotchas; never weaken the check.

## Cleanup

```bash
scripts/cleanup.sh my-run
```

`drive.py` writes its own PID to `scratch/<run>/app.pid` at start and deletes it when it exits. `cleanup.sh` kills that PID only if `/proc/<pid>/cmdline` contains `drive.py`; otherwise it prints that it is not killing. It never kills by process name. It removes `scratch/<run>/` (HOME, settings, QSettings, the project copy and outputs). It never touches `evidence/<run>/` and prints how many files are kept. Run it after every run, including failed ones.

## Helpers

| Script | Invocation | Purpose |
| --- | --- | --- |
| `scripts/env.sh` | `source scripts/env.sh <run>` | isolated env + dirs; exports `RUN SCRATCH EVIDENCE PY REPO` |
| `scripts/doctor.sh` | `scripts/doctor.sh [run]` | readiness check (creates scratch dirs; clean up after) |
| `scripts/launch.sh` | `scripts/launch.sh <feature> [run]` | doctor + launch GUI + drive one feature, tee log |
| `scripts/drive.py` | (via launch.sh) `drive.py <feature>` | in-process QTest driver, exit 0 = all checks pass |
| `scripts/cleanup.sh` | `scripts/cleanup.sh <run>` | remove scratch, keep evidence |
| `scripts/setup-windows.ps1` | `setup-windows.ps1 -Zip <src-rev.zip> [-Rev <rev>]` | Windows: extract to `panelcad-test\src-<rev>` (from -Rev or the zip name), make/reuse `panelcad-test\venv`, `pip install -e . pypdf` |
| `scripts/run-windows.ps1` | `run-windows.ps1 -Feature print-package -Run <name>` | Windows: isolated run dir + env (HOME/USERPROFILE/APPDATA/LOCALAPPDATA), bench-tree import check, verified registry backup (abort if export fails) then restore, native Qt platform, evidence, cleanup |

A full proof:

```bash
cd .cursor/skills/verify-panelcad
scripts/doctor.sh && scripts/cleanup.sh doctor
scripts/launch.sh print-package proof-$(date +%H%M); echo rc=$?
scripts/cleanup.sh proof-HHMM && ls "$VERIFY_ROOT"/evidence/proof-HHMM
```

## Windows test PCs

Use only `%USERPROFILE%\panelcad-test`; one targeted proof per change. Never the main install, its venv, or its settings.

**Proven on Windows 10/11 with the native Qt platform** (Python 3.12, PySide6 6.12): `setup-windows.ps1` and `run-windows.ps1 -Feature print-package` pass 12/12, the script exits with drive.py's exit code (`exit=0` logged), the registry key is removed after the run when it did not exist before, the child runs with HOME/USERPROFILE/APPDATA/LOCALAPPDATA pointed at the run's home folder, panelcad is asserted to load from the bench tree, and scratch is removed. **Not yet proven on Windows:** the registry backup-and-restore path for a key that already exists (including the `restore FAILED` exit 3), the abort when `reg export` fails, the other features, and running several instances at once.

```powershell
$b="$env:USERPROFILE\panelcad-test"
# setup (once per tree): extract the zip, make/reuse venv, pip install -e . pypdf
powershell -NoProfile -ExecutionPolicy Bypass -File <extracted>\.cursor\skills\verify-panelcad\scripts\setup-windows.ps1 -Zip "$b\src-<rev>.zip"
# run
powershell -NoProfile -ExecutionPolicy Bypass -File "$b\src-<rev>\.cursor\skills\verify-panelcad\scripts\run-windows.ps1" -Feature print-package -Run <name>
```

Notes:
- QSettings lives in `HKCU\Software\PanelCAD`, which env vars do not isolate. `run-windows.ps1` exports it to `evidence\<run>\registry-before.reg` when present, checks the `reg export` exit code and that the file exists, and aborts before the run if either fails. It deletes and re-imports the key only after the backup is confirmed; a key that did not exist before is removed after the run.
- It uses the real Windows platform: a window appears on screen for ~2 minutes. Don't touch the mouse during the run.
- PDF checks use `pypdf`; poppler is not installed and not needed (renders skipped).
- Evidence: `panelcad-test\evidence\<run>`; scratch `panelcad-test\run\<run>` is deleted. The `.sh` helpers are Linux-only.
- PowerShell 5.1 turns native stderr into errors under `Stop`; the scripts wrap `reg` in `cmd /c` and use `Continue` around python/pip.
