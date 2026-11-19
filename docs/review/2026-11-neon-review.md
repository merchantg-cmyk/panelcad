# PanelCAD Whole-Repo Code Review & Prioritized Recommendations

- **Reviewed baseline:** `main` at `0.5.28` (`1728757`)
- **Codebase size:** 542 Python files (~358,649 lines across `src/`, `tests/`, `scripts/`, `tools/`)
- **Primary product priorities:**
  1. Scan-to-issue pipeline accuracy and reliability (offline bundled ONNX OCR -> importer -> ladder layout -> leftover questions -> checkers -> issue; unreadable stays `?`, zero invented tags/wires/connections, checkers never loosened, transformer primary off-page with X1 -> secondary fuse -> L1 as `102` and X2 -> L2 as `104`)
  2. Everyday GUI drawing reliability for real redraw jobs
  3. Clean, professional printed output (PDF, print package, File -> Print, DXF)
  4. Reliable Windows quiet-install and Drive-hosted updates / rollback
  5. Code maintainability as all engineering goes through AI agents

This document lists the **bigger refactors and fixes NOT included in the behavior-preserving cleanup PR**, ranked by impact on the priorities above. Size is scoped technically (`S` = 1–3 files / localized; `M` = 4–10 files / one subsystem; `L` = cross-subsystem / structural).

---

## Executive Summary

PanelCAD has strong domain invariants (YAML as source of truth, hard issue gates, deterministic ladder redraw, no invented facts, offline bundled OCR). The main structural risks come from rapid feature growth across AI-driven sessions without periodic boundary consolidation:

- **Monolithic hot-spot files drive regressions and merge conflicts.** Three classes (`Session` 8,281 lines / 377 methods, `MainWindow` 5,147 lines / 298 methods, `SchematicView` 2,548 lines / 130 methods) and four procedural importer modules (`cleanup.py` 5,512L, `vectors.py` 5,373L, `raster.py` 5,358L, `ladder_synth.py` 3,294L) concentrate nearly every high-churn workflow into a few multi-thousand-line files.
- **29 package-level circular dependency cycles**, led by `core/` importing `render/` ~150 times because pure geometry helpers (`pin_world`, `parse_pin_ref`, `working_rect`) live in `render/` instead of `core/`, and `ocr/` importing `importer/`.
- **Silent exception swallowing in critical paths:** `ocr/ppocr.py` catches `Exception` and returns `None` without logging (silently degrading shop scans to the glyph matcher), and `render/pdf.py` rewrites `tmp` in place while `pypdf.PdfReader` holds it open inside `except Exception: pass` (risking silent 0-byte PDF truncation on Windows).
- **33 real test failures were hiding behind 12 environment-only failures/errors + 2 flaky GUI tests** on cloud VMs (45 failures + 2 errors total on baseline `1728757`). The largest real cluster is 15 importer / vector-roundtrip / symbol-picking regressions (including 3-pole overload `F1` being mapped to 2-pin `ol_contact`, breaking 6 of 9 DOL power wires, and `ingest.py` losing `dpi_tag_ignored` because `prepare_scan` overwrote DPI first).

---

## High Priority

### H1. Unify and isolate the scan-to-issue pipeline stages with typed stage contracts (and quarantine deferred preserve mode)

- **Priority:** High — Priority #1 (scan-to-issue accuracy) & #5 (AI maintainability)
- **Files involved:**
  - `src/panelcad/importer/pipeline.py` (2,541L)
  - `src/panelcad/importer/raster.py` (5,358L)
  - `src/panelcad/importer/strip_scan.py` (323L)
  - `src/panelcad/importer/build.py` (3,216L)
  - `src/panelcad/importer/ladder_synth.py` (3,294L)
  - `src/panelcad/importer/cleanup.py` (5,512L)
  - `src/panelcad/importer/leftover_questions.py` (2,782L; `_apply_action` alone is 706L)
  - `src/panelcad/ocr/ppocr.py` (280L), `src/panelcad/ocr/dispatch.py` (236L), `src/panelcad/ocr/backend.py` (110L)
- **Why it matters:**
  - Today, OCR extraction, page classification (schematic vs panel vs terminal strip), symbol matching, wire tracing, ladder synthesis, vision cleanup, and leftover-question mutation pass loosely typed dicts (`merged.json`, `page.json`, `facts`) across >23,000 lines of procedural code.
  - Because schematic, panel-layout, and terminal-strip readers share ad-hoc heuristics inside `raster.py` and `ocr/ppocr.py`, schematic fixes repeatedly regress panel and terminal-strip reads.
  - `ocr/ppocr.py` silently swallows ONNX/PaddleOCR failures (`except Exception: return None` at line 85) with no log entry, silently falling back to the glyph matcher on a broken install. Its `_fine_crops()` helper (lines 237–280) also bails out completely when a noisy scan has `> 6000` connected components, searches only above `height * 0.72`, and returns at most **one** `720x500` crop — hurting multi-zone panel layouts and terminal strips.
  - Electrical domain rules (e.g., transformer primary off-page, secondary `X1 -> fuse -> L1 (102)` and `X2 -> L2 (104)`, never `X1` straight to `L1`) are encoded imperatively across `cleanup.py`, `ladder_synth.py`, `leftover_questions.py`, and `core/elementary_layout.py`, making it easy for one stage to undo another stage's invariant.
- **Proposed change:**
  1. Introduce explicit Pydantic/dataclass stage contracts between `OCR -> PageFacts -> ElectricalGraph -> LadderLayout -> LeftoverQuestions -> Checkers` so each stage has a small, unit-testable boundary.
  2. Split page-kind readers cleanly into dedicated modules (`schematic_reader.py`, `panel_reader.py`, `strip_scan.py`) behind a thin dispatcher so schematic tuning cannot touch panel/terminal extraction.
  3. Log and surface ONNX/PP-OCR load/runtime failures in `ocr/ppocr.py` instead of returning `None` silently; vectorize `_fine_crops()` with a 2D integral image (`cv2.integral` / `np.histogram2d`) and allow multiple fine crops with adaptive speckle filtering so noisy panel/terminal scans do not hit the `count > 6000` cliff.
  4. Consolidate shop electrical invariants (transformer primary off-page, secondary fuse -> `L1`/`102`, `X2 -> L2`/`104`) into one shared rule module consumed by synthesis, cleanup, and checkers.
  5. Quarantine deferred preserve-mode branches behind a single adapter instead of interleaving `preserve` checks throughout `build.py`, `cleanup.py`, and `leftover_questions.py`.
- **Estimated size:** `L` (cross-module refactor inside `ocr/` and `importer/`, best landed in 4–5 incremental PRs after PR 161 and the panel/terminal scan fix land)
- **Risk:** **High** if done in one sweep; **Medium** if staged one boundary at a time behind the scan-ladder gate suite (`test_scan_ladder`, `test_scan_ladder_demo`, `test_terminal_strip_physical`, `test_elementary_layout`, `test_ansi_drafting`).

---

### H2. Fix the 15 real importer / symbol-picking / vector-roundtrip regressions on `main`

- **Priority:** High — Priority #1 (scan-to-issue and print-import accuracy)
- **Files involved:**
  - `src/panelcad/importer/vectors.py` (5,373L)
  - `src/panelcad/importer/read.py` (3,452L)
  - `src/panelcad/importer/symbols.py` (600+L)
  - `src/panelcad/importer/ingest.py` (671L)
  - `src/panelcad/importer/build.py` (3,216L)
  - Tests: `tests/test_print_wire_roundtrip.py`, `tests/test_import_b_fixes.py`, `tests/test_nema_print_import.py`, `tests/test_importer.py`
- **Why it matters:**
  - **12 tests in `test_print_wire_roundtrip.py`** and **`test_import_b_fixes.py::test_revised_panel_layout_does_not_retitle_dol_power`** are genuinely failing on `main` (pure vector DXF/PDF inputs, zero OCR dependency):
    - 3-phase thermal overload `F1` is being classified as 2-pin `iec.protection.ol_contact` instead of a 3-pole overload relay, causing 6 of 9 DOL power wires (`K1 -> F1 -> M1`) to drop (`connectivity_recall` drops from `1.0` to `0.333`).
    - 1-pole breakers (`CB1..`) are misclassified as `nema.breakers.mcb_3p_32a`, `aux_nc` with a drawn slash is misclassified as `aux_no`, and `contact_tag_parts("KA1-14")` now returns `('KA1', '14')` instead of `None`.
  - **`test_nema_print_import.py::test_photo_72_dpi_uses_a_real_sheet`** is a real regression in `importer/ingest.py:406-419`: `prepare_scan()` normalizes a bogus 72-DPI phone photo and stamps `effective_dpi` onto `corrected.info["dpi"]`; `ingest.py` then overwrites `source_dpi = prepared_dpi` before calling `resolve_sheet_size_mm()`, so `dpi_note` becomes empty and `record["dpi_tag_ignored"]` is never recorded.
  - **`test_import_b_fixes.py::test_pdf_title_band_uses_whole_lines`**: `_has_drawing_text()` in `importer/read.py:2942` requires `>= 6` working-area tokens, causing sparse vector PDFs to be reclassified as raster scans (`vector=False`) and discarding their embedded PDF text layer.
- **Proposed change:**
  1. Preserve `source_dpi` vs `effective_dpi` cleanly in `ingest.py` so `dpi_tag_ignored` / `dpi_note` is recorded whenever `prepare_scan` overrides a bogus 72-DPI tag.
  2. Audit the recent symbol-selection changes in `importer/vectors.py`, `importer/symbols.py`, and `importer/build.py` that broke 3-pole overload (`F1`), 1-pole breaker (`CB`), NC slash detection, and `contact_tag_parts`.
  3. Keep embedded PDF text tokens for title-block reading when a PDF has real text and no full-page scan image (`not pdf_page_is_scan(page)`), instead of forcing sparse text PDFs through raster OCR.
- **Estimated size:** `M` (4–6 files in `src/panelcad/importer/`)
- **Risk:** **Medium** — touches symbol classification shared with scan imports; must re-verify full scan-ladder gate suite after each fix.

---

### H3. Fix Windows PDF export data-corruption & bookmark-drop bugs and make all PDF exports atomic

- **Priority:** High — Priority #3 (clean, professional printed output on Windows)
- **Files involved:**
  - `src/panelcad/render/pdf.py` (79L)
  - `src/panelcad/render/pdf_outline.py` (66L)
  - `src/panelcad/render/print_package.py` (1,643L)
  - `src/panelcad/render/revision_report.py`, `thermal_page.py`, `troubleshoot_log.py`, `cable_schedule.py`, `cutout_draw.py`, `power_report.py`
- **Why it matters:**
  - **In-place truncation risk (`render/pdf.py:37-54`):** After `QPdfWriter` writes `tmp`, `_reader = PdfReader(str(tmp))` opens `tmp` and then `with open(tmp, "wb") as _fh: _writer.write(_fh)` overwrites `tmp` **in place** while `_reader` still holds the file open, wrapped in `except Exception: pass`. On Windows, opening a file already open by `PdfReader` or hitting a mid-write error can truncate `tmp` to 0 bytes and then `os.replace(tmp, dest)` silently overwrites the user's PDF with an empty file.
  - **Windows file-lock bookmark drop (`render/print_package.py:1071-1091` + `render/pdf_outline.py:33-53`):** `print_package.py` does not release `QPdfWriter` before calling `apply_outlines_and_links(dest, ...)`, and `pdf_outline.py` keeps `reader = PdfReader(str(path))` alive while calling `tmp.replace(dest)`. On Windows this triggers `PermissionError (WinError 32)`, which is caught and silently strips all PDF bookmarks and clickable index links.
  - **Non-atomic writes:** 7 PDF exporters (`print_package.py`, `revision_report.py`, `thermal_page.py`, `troubleshoot_log.py`, `cable_schedule.py`, `cutout_draw.py`, `power_report.py`) instantiate `QPdfWriter(str(dest))` directly on the target path instead of a temp file + `os.replace`, leaving partial/corrupt PDFs if an error occurs or if the PDF is open in a viewer.
  - **200+ pypdf deprecation warnings:** `pdf_outline.py:64,66` uses positional arguments to `PdfWriter.add_outline_item()`, scheduled for removal in `pypdf 8.0.0`.
- **Proposed change:**
  - Add one small `atomic_pdf_writer(dest)` helper in `render/pdf.py` used by all 8 PDF exporters: paint to `tmp1`, explicitly `del painter, writer`, if post-processing with `pypdf` (`PdfReader(tmp1)` -> write `tmp2` -> close `reader` before `os.replace(tmp2, dest)`), clean up temp files on error, and pass keyword arguments (`title=`, `page_number=`) to `add_outline_item`.
- **Estimated size:** `S`–`M` (1 new helper + mechanical call-site updates across 8 render modules)
- **Risk:** **Low** — purely file-lifecycle safety; easy to test on both Linux and Windows.

---

### H4. Make Windows install/update atomic, offline-resilient, and rollback-complete

- **Priority:** High — Priority #4 (reliable Windows install/update)
- **Files involved:**
  - `scripts/Install-PanelCAD.ps1` (472L)
  - `scripts/build-release.py` (940L)
  - `scripts/install_tree.py` (374L)
  - `src/panelcad/update/handoff.py` (892L)
  - `pyproject.toml` (64L)
- **Why it matters:**
  - **Live PyPI dependency on shop upgrades (`Install-PanelCAD.ps1:341-352`):** Although ONNX Runtime wheels are carefully bundled offline, the installer still runs `pip install --upgrade pip`, `pip install -e $InstallRoot`, and `pip install --force-reinstall --no-deps "opencv-python-headless>=4.9,<5"` against live PyPI with unpinned lower-bound-only dependencies in `pyproject.toml`. A breaking PyPI release or a shop PC offline/behind a proxy breaks installation mid-upgrade.
  - **Copy-before-install hazard (`Install-PanelCAD.ps1:330`):** `Copy-Source` overwrites `%USERPROFILE%\PanelCAD` *before* `pip install` succeeds. If `pip install` fails, the program folder is half-upgraded and the installer aborts without restoring the backup.
  - **Incomplete rollback (`update/handoff.py:34`):** `PROGRAM_DIRS = ("src", "scripts")` omits `libraries/`, `packaging/`, and `resources/` (including bundled OCR weights/locks) and does not restore venv wheel versions. Rolling back across a release that changed `libraries/` or `pyproject.toml` dependencies leaves a mismatched installation.
  - **Unverified Python bootstrap download (`Install-PanelCAD.ps1:155`):** Downloads `python-3.12.10-amd64.exe` from `python.org` without SHA-256 hash verification.
  - **Release zip bloat (`scripts/build-release.py:83-95`):** `INCLUDE_DIRS` ships `tests/` (including hundreds of fixture PNGs/JPGs) and `agents/` inside every shop setup zip.
- **Proposed change:**
  1. Pin a `constraints.txt` / lockfile generated at release time (and optionally bundle wheels for truly offline shop installs) instead of letting shop PCs resolve unpinned ranges against live PyPI.
  2. Add SHA-256 verification for the Python bootstrap installer in `Install-PanelCAD.ps1`.
  3. Include `libraries/` and `packaging/` in `handoff.py`'s `PROGRAM_DIRS` rollback list, and auto-trigger rollback if `Install-PanelCAD.ps1` fails after `Copy-Source`.
  4. Decide explicitly whether `tests/` belongs in the shop zip or only in a dev/source archive (keep `packaging/` lockfiles needed by `Install-PanelCAD.ps1`).
- **Estimated size:** `M` (4–5 files in `scripts/` and `src/panelcad/update/`)
- **Risk:** **Medium** — installer/updater changes must be validated end-to-end on a real Windows machine before cutting a release.

---

### H5. Fix `core/` <-> `render/` and `ocr/` <-> `importer/` layering inversions

- **Priority:** High — Priority #5 (maintainability for AI agents) & #1/#2 (reliability)
- **Files involved:**
  - `src/panelcad/render/sheet.py` (73L — zero rendering code, purely geometry!)
  - `src/panelcad/render/titleblock.py` (`working_rect`, `title_block_rect`)
  - `src/panelcad/render/wiring.py` (`route_sheet_wires`, `wire_ends`, `sheet_obstacles`)
  - `src/panelcad/core/elementary_layout.py` (~35 repeated lazy `from panelcad.render...` imports)
  - `src/panelcad/core/checks.py`, `core/circuit_builder.py`, `core/cleanup.py`, `core/io_sheets.py`
  - `src/panelcad/ocr/backend.py`, `ocr/dispatch.py`, `ocr/tesseract_engine.py`, `ocr/bench.py`
  - `src/panelcad/render/qt_painter.py` (imports `panelcad.api.session._ensure_qt_app`)
  - `src/panelcad/api/session.py:113` (imports `panelcad.gui.bug_report_dialog`)
- **Why it matters:**
  - Static analysis found **29 package-level 2-cycles** across `src/panelcad/`.
  - `core/` has **~150 lazy imports** pointing upward into `render/`, `api/`, `importer/`, `convert/`, and `reconstruct/`. Almost all `core -> render` imports are for just 6 pure-geometry functions currently sitting in `render/sheet.py` (only 73 lines, zero Qt/Painter imports!) and `working_rect()` in `render/titleblock.py`.
  - `ocr/` imports `importer/tesseract_runtime.py` and `importer/raster.py`, even though `ocr` is the lower-level subsystem.
  - `render/qt_painter.py` imports `api/session.py` solely for `_ensure_qt_app()`, and `api/session.py` imports `gui/bug_report_dialog.py`.
  - Because of these cycles, modules rely on scattered function-local imports to survive import order, making startup profiling, refactoring, and AI edits brittle.
- **Proposed change:**
  1. Move `parse_pin_ref`, `named_pin_ids`, `resolve_pin`, `pin_world`, `pin_side_world` from `render/sheet.py` to `core/pin_geometry.py` (leaving thin re-exports in `render/sheet.py` for backwards compatibility).
  2. Move `working_rect` / `title_block_rect` geometry constants to `core/sheet_frame.py`.
  3. Move `tesseract_runtime.py` from `importer/` into `ocr/` (with a compatibility re-export in `importer/tesseract_runtime.py`).
  4. Move `_ensure_qt_app()` into a tiny leaf helper `panelcad/qt_app.py` (removes `render -> api` and deduplicates 3 `_ensure_app`/`_ensure_qt` copies).
  5. Add a lightweight import-layer unit test (e.g., `core` must not import `gui`/`api`; `ocr` must not import `importer`) to prevent regressions.
- **Estimated size:** `M` (can be done incrementally one boundary per PR with zero behavior change)
- **Risk:** **Low** if compatibility re-exports are kept during migration.

---

### H6. Decompose the three GUI/API god classes (`Session`, `MainWindow`, `SchematicView`)

- **Priority:** High — Priority #2 (everyday GUI drawing reliability) & #5 (AI maintainability)
- **Files involved:**
  - `src/panelcad/api/session.py` (**8,835L**, `Session`: 8,281L / **377 methods**)
  - `src/panelcad/gui/main_window.py` (**5,471L**, `MainWindow`: 5,147L / **298 methods**)
  - `src/panelcad/gui/canvas.py` (**4,607L**, `SchematicView`: 2,548L / **130 methods**, `ComponentItem`: 712L)
  - `src/panelcad/api/history.py` (140L)
- **Why it matters:**
  - Every GUI or editing change (like PR 162's drag/undo fix, or future 90° text label rotation) collides inside the same 3 giant files. AI agents cannot hold an 8,800-line class in focused context, which directly causes gesture-state / undo-lock mismatches and subtle regressions.
  - **Undo/history structural risk (`api/session.py` + `api/history.py`):**
    - `Session._history_lock` relies on balanced imperative `begin_command`/`end_command` calls across scattered canvas event handlers rather than a single state-machine/transaction object owning active drag gestures.
    - `api/history.py:45` shares unchanged nested dicts/lists across snapshots (`apply_value` copies only modified containers). Any caller that mutates a dict inside `project` or snapshot cache in place can silently corrupt earlier undo entries.
  - **Qt reliability risks:**
    - `canvas.py:4180` broke `test_theme.py::test_gui_sources_have_no_hex_outside_the_theme_module` with a hardcoded `QColor("#f59e0b")` (left untouched here because PR 162 edits `canvas.py`).
    - `test_wire_hit.py` (`test_hover_shows_the_wire_that_will_be_picked`, `test_device_click_and_rubber_band_still_select`) passes 15/15 standalone but fails inside the full suite — indicating Qt focus/hover/scene state leaking across tests or fragile viewport coordinate assumptions in `SchematicView`.
    - `canvas.py` still calls the deprecated 5-arg `QMouseEvent(...)` constructor at lines 3379 and 3645.
- **Proposed change:**
  1. Wait for PR 162 to merge, then extract cohesive controllers/mixins out of `Session` (e.g., `SessionEditing`, `SessionLayout`, `SessionImport`, `SessionExport`, `SessionReconstruct`, `SessionElectrical`) while keeping `Session`'s public API 100% unchanged.
  2. Extract canvas interaction modes (select/drag, wire drawing, zip-tie preview, rail landing) out of `SchematicView` into dedicated gesture controller classes with explicit `start`/`update`/`commit`/`cancel` lifecycle hooks bound to `Session.command(...)`.
  3. Extract menu/action builders and dock wiring out of `MainWindow` (start with `MainWindow.__init__` which is 391 lines).
  4. Replace `#f59e0b` in `canvas.py:4180` with a theme token once PR 162 lands, and investigate the `test_wire_hit.py` cross-test focus flake.
- **Estimated size:** `L` (3–5 incremental PRs after PR 162 merges)
- **Risk:** **Medium** — mechanical delegation/extraction is safe when covered by `test_gui_undo_paths`, `test_layout`, `test_wire_hit`, `test_window_leak`, and `verify-panelcad`.

---

## Medium Priority

### M1. Separate layout mutation from issue checking in `core/elementary_layout.py` and fix the 9 layout/shop-drawing geometry regressions

- **Priority:** Medium — Priority #1 (scan-to-issue) & #3 (print quality)
- **Files involved:**
  - `src/panelcad/core/elementary_layout.py` (2,662L)
  - `src/panelcad/core/cleanup.py` (1,600+L)
  - `src/panelcad/core/checks.py` (2,517L)
  - `src/panelcad/core/ansi_drafting.py` (310L)
  - `src/panelcad/render/wire_labels.py` (2,871L; `_score()` is 246L)
  - `src/panelcad/render/label_avoid.py` (1,240L)
  - Tests: `tests/test_shop_drawing.py`, `tests/test_schematic_quality.py`, `tests/test_cleanup.py`, `tests/test_convert_standard.py`, `tests/test_wire_labels.py`
- **Why it matters:**
  - `core/elementary_layout.py` currently combines conductor repair, symbol placement, wire dressing, AND the `elementary_layout_issues()` gate checker in one 2,662-line file. Keeping checkers strictly read-only in a dedicated `core/elementary_checks.py` module reduces the risk of a layout tweak accidentally loosening a checker.
  - 9 real geometry/layout tests currently fail on `main`:
    - `test_shop_drawing.py` (3 failures): `iec-star-delta-control` wire `K1-23.14->K4-18.13` overlaps label `K4.A1`; `iec.protection.overload_9a` aux contact clearance `0.5 mm < 1.5 mm`; `nema.contactors.ac3_9a` missing coil box.
    - `test_schematic_quality.py` (2 failures): 9 and 18 collinear wire pairs on rotated/sideways starters.
    - `test_cleanup.py` (2 failures): aux lamp rung missing `L1` landing; `CR14` and `PL1` separated by `74.2 mm` instead of `< 16 mm`.
    - `test_convert_standard.py` (1 failure): wire `205` intersects pin `KT1` on converted star-delta control sheet.
    - `test_wire_labels.py` (1 failure): `CB1` shifted from `x=40.0` to `x=179.0`.
- **Proposed change:**
  1. Split `elementary_layout_issues()` and its pure check helpers out of `core/elementary_layout.py` into `core/elementary_checks.py` (re-exporting from `elementary_layout.py`).
  2. Triage and fix the 9 geometry/layout failures without weakening any assertion or checker threshold.
- **Estimated size:** `M`
- **Risk:** **Medium** — layout tweaks must be gated against both `test_elementary_layout` / `test_ansi_drafting` and the shop-drawing / schematic-quality suites.

---

### M2. Fix guidance / Problems-panel drift and export/golden mismatches (5 real failures)

- **Priority:** Medium — Priority #2 (GUI clarity) & #3 (printed output)
- **Files involved:**
  - `src/panelcad/gui/problems.py` (`_CHECK_LABELS`)
  - `src/panelcad/core/checks.py` (`page_over_dense`, `wire_end_off_pin`, `label_covered_by_wire`)
  - `src/panelcad/guidance.py` / `src/panelcad/gui/main_window.py` (menu tooltips)
  - `src/panelcad/render/pdf.py` / `src/panelcad/render/titleblock.py` (`test_revisions.py`)
  - `examples/dol_starter.pcad.yaml`, `tests/test_nameplate.py`, `tests/golden/wire_list/dol-from-to.csv`
- **Why it matters:**
  - `test_guidance.py::test_every_finding_id_has_a_label`: 3 real design checks (`page_over_dense`, `wire_end_off_pin`, `label_covered_by_wire`) have no human-readable label in `gui/problems.py:_CHECK_LABELS`, so operators see raw snake_case IDs in the Problems dock.
  - `test_guidance.py::test_help_and_menus_use_floor_wording`: 4 menu tooltips simply echo their menu label (`Panel…`, `Cut Sheet…`, etc.).
  - `test_revisions.py::test_clouds_and_rev_table_export_pdf_svg_dxf_and_print_package`: Revision table text `"Issued for construction"` is missing from page 2 (panel layout sheet) of the exported PDF.
  - `test_nameplate.py::test_dol_starter_does_not_invent_supplies_and_accepts_fla` and `test_from_to.py::test_golden_exports_for_dol_reversing_and_plc`: Stale expected values after `examples/dol_starter.pcad.yaml` changed company (`Acme` -> `Example Controls`) and zone column (`3` -> `4`).
- **Proposed change:**
  - Add floor-wording labels for the 3 missing findings in `gui/problems.py` and distinct tooltips for the 4 echoing menu actions.
  - Debug why page 2 of `export_pdf` omits `"Issued for construction"` in the revision table.
  - Confirm whether the `dol_starter` company/zone changes are intentional and update the test/golden file accordingly.
- **Estimated size:** `S` (5–7 files, localized)
- **Risk:** **Low**

---

### M3. Address scan-pipeline, rendering, and test-suite performance hot spots

- **Priority:** Medium — Priority #1 (fast scan-to-print), #2 (responsive GUI), #5 (fast AI iteration)
- **Files involved:**
  - `src/panelcad/importer/raster.py` (`prepare_opencv` bilateral filter, `_search_template`, `_trace_wires`)
  - `src/panelcad/ocr/ppocr.py` (`_fine_crops` nested Python window loop; multi-scale `limits = [2000, 2560]` running full detection twice)
  - `src/panelcad/render/wire_labels.py` (2,871L; O(N²) pairwise wire-group checks & repeated obstacle box building)
  - `src/panelcad/render/qt_painter.py` (`QFont` + `QFontMetricsF` constructed from scratch on every `draw_world_text` / `text_advance_mm` call)
  - `tests/conftest.py` (`_gc_between_tests` runs `gc.get_objects()` twice + full `gc.collect()` after **every one** of 3,073 tests)
- **Why it matters:**
  - **Full test suite speed:** Sequential `./scripts/check` takes **~50+ minutes** on a 4-core cloud VM (~14 minutes even with `pytest -n 4 --dist loadfile`). A large share of overhead is `conftest.py:_gc_between_tests` scanning the entire Python heap (`gc.get_objects()`) twice and running a full generation-2 `gc.collect()` on all 3,073 tests — even pure non-Qt unit tests that never imported `PySide6`.
  - **Canvas/PDF text rendering:** `render/qt_painter.py` creates a brand-new `QFont` and `QFontMetricsF` on every single call to `draw_world_text()` and `text_advance_mm()`. Caching `(family, bold) -> (QFont, QFontMetricsF)` eliminates thousands of Qt allocations per redraw/export.
  - **Scan reading latency:** `ocr/ppocr.py:_fine_crops()` runs nested Python `sum(1 for x, y in points if ...)` over sliding windows; `raster.py:prepare_opencv()` runs `cv2.bilateralFilter` on full 300-DPI RGB sheets before deskewing.
- **Proposed change:**
  1. In `tests/conftest.py`, skip `_live_main_windows()` heap scans and full `gc.collect()` when `panelcad.gui.main_window` is not even loaded in `sys.modules` (or run gen-0/gen-1 between non-GUI tests and gen-2 only after Qt tests / every N tests), and consider adding `pytest-xdist` to `[project.optional-dependencies] dev`.
  2. Cache `(QFont, QFontMetricsF)` in `render/qt_painter.py` (reset on font reload).
  3. Vectorize `ocr/ppocr.py:_fine_crops()` window counting with `np.histogram2d` / integral image.
- **Estimated size:** `S`–`M`
- **Risk:** **Low** — benchmark before/after; keep the Windows PySide6 GC-safety invariant (`gc.disable()` during Qt callbacks) intact.

---

### M4. Consolidate overlapping conversion / reconstruction / import pipelines and error handling

- **Priority:** Medium — Priority #5 (maintainability) & #1 (accuracy)
- **Files involved:**
  - `src/panelcad/importer/` (50,202L), `src/panelcad/convert/` (16,137L), `src/panelcad/reconstruct/` (14,441L), `src/panelcad/sharpen/` (5,841L)
  - `src/panelcad/cli/main.py` (4,686L) and `src/panelcad/cli/agent.py` (3,144L)
  - 82 `try/except/pass` (`S110`) + 21 `try/except/continue` (`S112`) + 48 blind `except Exception` (`BLE001`) sites across `src/`
- **Why it matters:**
  - Four packages (`importer/`, `convert/`, `reconstruct/`, `sharpen/`) total **86.6k lines** and re-implement overlapping concepts: endpoint splitting (`_split_endpoint` in 3+ modules), numeric coercion (`_num` / `_as_float` / `_prop_float` in 11+ modules), catalog device lookup (`_device` in 17 modules), JSON extraction from model replies (`_json_object` in 3 modules), and settings path resolution (`config_dir` / `settings_path` in 4 modules).
  - Over 100 silent `try/except: pass|continue` blocks hide real data/IO bugs during import, conversion, and GUI updates (especially in `importer/build.py`, `importer/cleanup.py`, `convert/flags.py`, `convert/import_convert.py`, and `gui/reconstruct_wizard.py`).
- **Proposed change:**
  1. Extract shared micro-utilities (`parse_float`, `split_endpoint`, `extract_json_object`, `config_dir`) into `core/` or `settings.py` and retire copies incrementally.
  2. Split `cli/main.py` (4,686L) into Typer sub-apps by command group (`cli/import_cmd.py`, `cli/export_cmd.py`, `cli/electrical_cmd.py`, `cli/update_cmd.py`).
  3. Replace silent `except Exception: pass` blocks in `importer/`, `ocr/`, and `render/` with narrowed exception types and `logging.debug/warning`.
- **Estimated size:** `M`–`L` (best executed module-by-module)
- **Risk:** **Low–Medium**

---

### M5. Close test coverage gaps on high-value untested modules & wire up Depot/GitHub CI

- **Priority:** Medium — Priority #1, #2, #3, #5
- **Files involved:**
  - 29 source modules with zero direct test references, notably:
    - `src/panelcad/core/io_sheets.py` (2,598L)
    - `src/panelcad/core/circuit_extra.py` (1,763L)
    - `src/panelcad/render/panel_wiring_render.py` (798L)
    - `src/panelcad/core/motor_apply.py` (626L)
    - `src/panelcad/core/circuit_iec.py` (587L)
    - `src/panelcad/reconstruct/guided.py` (565L)
    - `src/panelcad/render/cutout_dxf.py` (390L), `render/power_report.py` (304L), `render/cable_schedule.py` (294L), `render/pdf_outline.py` (66L)
    - `src/panelcad/gui/power_dock.py` (355L), `gui/wiring_walkthrough.py` (295L), `gui/missing_info_editor.py` (288L)
  - `.depot/workflows/ci.yml` & `.github/workflows/ci.yml`
  - `pyproject.toml` (`[tool.ruff.lint]`)
- **Why it matters:**
  - Some of those 29 modules are exercised indirectly through `Session`, but direct unit tests for edge cases (especially `pdf_outline.py`, `cutout_dxf.py`, `io_sheets.py`, `missing_info_editor.py`) are absent.
  - `.depot/workflows/ci.yml` and `.github/workflows/ci.yml` do not run `ruff check` and do not install `tesseract-ocr` or cache the pinned PP-OCR ONNX weights, so regressions (like the 33 real failures and the public-snapshot banned-word leaks!) landed on `main` unnoticed.
- **Proposed change:**
  1. Add `ruff check src tests --select F401,F601,F811,F821,PIE794,B033,B018` and `python scripts/public-snapshot.py` self-check to `./scripts/check` (or a fast pre-PR check script) so undefined names, unused imports, and private-name leaks fail immediately in <2 seconds.
  2. Connect Depot CI (or an Origin Checks runner) to run the gate suite + `ruff` + public-snapshot check on every PR, and cache the pinned ONNX weights in CI so OCR-dependent tests run on real weights instead of skipping.
  3. Add focused unit tests for `render/pdf_outline.py` (including Windows file-handle closure / atomic replace) and `gui/missing_info_editor.py`.
- **Estimated size:** `M`
- **Risk:** **Low**

---

## Low Priority

### L1. Expand `ruff` rules gradually in `pyproject.toml`

- **Priority:** Low — Priority #5 (maintainability)
- **Files involved:** `pyproject.toml` (lines 52–64), repo-wide
- **Why it matters:** `pyproject.toml` currently enforces only `F841` on a single file (`src/panelcad/settings.py`), ignoring `F401`, `F821`, `F601`, `F811`, `PIE794`, `B033`, `B018`, `I001`, and `F841` everywhere else. As a result, real bugs like the `NameError` on `VisionSettings` in `cli/main.py:1009` and shadowed dictionary keys in `sharpen/cards.py:43` slipped through.
- **Proposed change:** Enable high-signal bug rules (`F401,F601,F811,F821,PIE794,B033,B018`) repo-wide in `pyproject.toml` once PR 162 fixes the 2 remaining items in `api/session.py:4792` and `gui/main_window.py:2697`. Keep style-only churn (`UP045`, `I001`, `RUF007`) optional until wanted.
- **Estimated size:** `S`
- **Risk:** **Low**

---

### L2. Clean up legacy one-off live scripts under `scripts/`

- **Priority:** Low — Priority #5 (repo hygiene)
- **Files involved:** `scripts/place_bx08nd3_live.py`, `place_bx12nd3_live.py`, `place_es1_3a_n4_live.py`, `place_ppc5_1102_live.py`, `place_sio_mb12cdr_live.py`, `place_symbol_sheets_live.py`, `show-type-a.cmd`, plus the legacy private live-stream helper excluded in `scripts/public-snapshot.py`
- **Why it matters:** These 8 one-off local development scripts contain hardcoded Windows Drive paths and are excluded from public snapshots (`scripts/public-snapshot.py`), yet sit beside production release/install scripts (`build-release.py`, `Install-PanelCAD.ps1`, `install_tree.py`, `fetch_electrical_ocr.py`). None are referenced by `src/` or `tests/`.
- **Proposed change:** Confirm with the owner which ones are still used; archive or move remaining local-only dev helpers into `scripts/dev/` (updating `scripts/public-snapshot.py` `EXCLUDE` accordingly) so `scripts/` contains only supported release, build, install, and benchmark tools.
- **Estimated size:** `S`
- **Risk:** **Low**

---

### L3. Plan 90° rotated text labels cleanly when resumed (currently on hold)

- **Priority:** Low (on hold per owner) — Priority #2 (GUI drawing) & #3 (printed output)
- **Files involved:** `src/panelcad/core/models.py`, `src/panelcad/render/painter.py`, `src/panelcad/render/qt_painter.py`, `src/panelcad/render/svg.py`, `src/panelcad/render/dxf.py`, `src/panelcad/render/label_avoid.py`, `src/panelcad/gui/canvas.py`
- **Why it matters:** `draw_world_text` in `render/qt_painter.py:23` already accepts `rotate: float = 0`, and `Component.rotation` supports `0/90/180/270`, so when 90° text label rotation is unpaused it should be wired consistently across the YAML model, `label_avoid.py` bounding boxes (swapping width/height on 90°/270°), hit-testing in `canvas.py`, and all three painters (`QtPainter`, `SvgPainter`, `DxfPainter`) in one coordinated slice rather than canvas-only.
- **Estimated size:** `M`
- **Risk:** **Low–Medium**

---

### L4. Remove deprecated 5-arg `QMouseEvent` calls in `canvas.py` and tests

- **Priority:** Low — Priority #2 (Qt6 forward compatibility)
- **Files involved:** `src/panelcad/gui/canvas.py:3379,3645`, `tests/test_layout.py`, `tests/test_offgrid_click.py`, `tests/test_rail_wire_gui.py`, `tests/test_draw_memory.py`, `tests/test_wire_hit.py`, `tests/test_undo_steps.py`, `tests/test_ai_walkthrough.py`
- **Why it matters:** Emits ~40 `DeprecationWarning`s on every test run and may break on future PySide6 minor/major upgrades.
- **Proposed change:** Switch to the 6-arg `QMouseEvent(type, localPos, globalPos, button, buttons, modifiers)` signature after PR 162 merges.
- **Estimated size:** `S`
- **Risk:** **Low**

---

## Appendix: Baseline Test Suite Triage (`main` `1728757`)

Baseline full suite (`./scripts/check -n 4 --dist loadfile`): **45 failed, 2,992 passed, 34 skipped, 2 errors** (3,073 total).
Baseline gate suite (`test_elementary_layout`, `test_ansi_drafting`, `test_scan_ladder_demo`, `test_terminal_strip_physical`, `test_layout`, `test_scan_ladder`): **86 passed, 0 failed**.

### 1. Environment-Only on Cloud VMs (12 total: 2 errors + 10 failures) — Fixed or Honestly Skipped in Cleanup PR

| Test | Baseline | Cause | Action in Cleanup PR |
| --- | --- | --- | --- |
| `test_check_worker_errors.py::test_check_worker_reports_an_invalid_snapshot_instead_of_raising` | ERROR | Used `pytest-qt` `qapp` fixture (`pytest-qt` not in `pyproject.toml`) | **Fixed** using `QApplication` directly -> now **PASS** |
| `test_check_worker_errors.py::test_live_runner_is_not_left_busy_after_a_failed_check` | ERROR | Used `pytest-qt` `qtbot` fixture | **Fixed** using `QApplication` + event-loop wait -> now **PASS** |
| `test_electrical_ocr.py::test_cli_accepts_the_electrical_backend` | FAIL | Rich ANSI color escapes around `--ocr-backend` when `NO_COLOR` unset | **Fixed** (strip ANSI in assertion) -> now **PASS** |
| `test_cli_sharpen.py::test_help_names_exit_codes_and_examples` | FAIL | Rich ANSI color escapes around `--json` when `NO_COLOR` unset | **Fixed** (strip ANSI in `_text`) -> now **PASS** |
| `test_importer.py::test_missing_tesseract_is_a_clear_message` | FAIL | Did not pin `PANELCAD_OCR_BACKEND=tesseract`; default electrical backend skipped `pytesseract` | **Fixed** (set env var) -> now **PASS** |
| `test_importer.py::test_tesseract_error_names_the_language_data` | FAIL | Did not pin `PANELCAD_OCR_BACKEND=tesseract` + stubbed binary path | **Fixed** (set env vars) -> now **PASS** |
| `test_importer.py::test_phone_photo_stays_landscape` | FAIL | Missing `weights_ready()` guard (5 sibling orientation tests already had it) | **Skipped honestly** when ONNX weights absent |
| `test_scan_reader.py::test_letters_inside_a_coil_are_the_tag_and_a_blank_coil_is_not_named` | FAIL | Tesseract-contract test whose skip guard failed to check `find_tesseract()` | **Skipped honestly** when Tesseract binary absent |
| `test_scan_reader.py::test_a_turned_strip_number_is_read_and_a_blank_side_stays_unread` | FAIL | Tesseract-contract test missing `find_tesseract()` check and `ocr_scope("tesseract")` | **Skipped honestly** when Tesseract binary absent |
| `test_terminal_photo.py::test_detection_reads_tags` | FAIL | `_need_tesseract()` only checked `importorskip("pytesseract")`, not ONNX weights or binary | **Skipped honestly** when neither OCR runtime installed |
| `test_terminal_photo.py::test_photo_populates_strip_model_without_invented_sizes` | FAIL | Same `_need_tesseract()` bug | **Skipped honestly** when neither OCR runtime installed |
| `test_bugs_d.py::test_context_fractions_are_not_sheet_numbers_in_three_fonts` | FAIL | Rendered-font OCR test ran glyph fallback when neither ONNX weights nor Tesseract existed | **Skipped honestly** when neither OCR runtime installed |

### 2. Real Public-Release Hygiene Failures Fixed in Cleanup PR (2 failures)

| Test | Root Cause | Action in Cleanup PR |
| --- | --- | --- |
| `test_bug_report.py::test_release_bot_token_is_embedded_only_in_the_zip` | Staff/machine name in `screen_fit.py:106` & `test_print_fit_range.py:205`; Windows user path in `tests/fixtures/orientation/orientation-scores-cecec57.json` | **Fixed** (removed private names & user path) -> now **PASS** |
| `test_release.py::test_public_zip_uses_the_snapshot_exclude_list` | Same 3 files failed `public-snapshot.py` self-check | **Fixed** -> now **PASS** |

### 3. Flaky / Order-Dependent Offscreen Qt Failures (2 failures — pass 15/15 standalone)

- `tests/test_wire_hit.py::test_hover_shows_the_wire_that_will_be_picked`
- `tests/test_wire_hit.py::test_device_click_and_rubber_band_still_select`

### 4. Real Product / Test Failures Remaining on `main` (31 failures — left for targeted fix PRs)

- **Importer / vector-roundtrip / symbol-picking (15 + 1 hybrid = 16):**
  - `tests/test_print_wire_roundtrip.py` (12): `test_contact_suffix_maps_to_the_device_and_terminal`, `test_bylayer_dxf_keeps_the_power_wires`, `test_shipped_dol_power_wires_come_back`, `test_missing_slash_is_no_and_a_drawn_slash_is_nc`, `test_three_phase_stack_keeps_the_three_pole_symbols`, `test_single_pole_breakers_keep_their_own_wires`, `test_control_breaker_beside_a_stack_stays_one_pole`, `test_stacked_rungs_keep_the_overload_contact`, `test_four_rungs_keep_overload_contacts_under_the_rung_above`, `test_real_n1_booster_uses_the_three_pole_symbols`, `test_side_by_side_button_outputs_stay_separate_nets`, `test_blank_import_wires_can_be_numbered`
  - `tests/test_import_b_fixes.py` (2): `test_revised_panel_layout_does_not_retitle_dol_power` (real `F1` -> `ol_contact` bug), `test_pdf_title_band_uses_whole_lines` (hybrid: sparse vector PDF fell below 6-word threshold into raster OCR)
  - `tests/test_importer.py` (1): `test_mild_vision_wire_numbers_become_built_wires`
  - `tests/test_nema_print_import.py` (1): `test_photo_72_dpi_uses_a_real_sheet` (`prepare_scan` overwrote DPI before `ingest.py` recorded `dpi_tag_ignored`)
- **Layout / cleanup / conversion / shop-drawing geometry (9):**
  - `tests/test_shop_drawing.py` (3): `test_sheet_is_shop_ready[iec-star-delta-control]`, `test_aux_contact_art_clears_the_body`, `test_nema_coil_box_clears_the_first_pole`
  - `tests/test_schematic_quality.py` (2): `test_rotated_starter_bus_keeps_each_phase_on_its_own_lane`, `test_sideways_starters_stay_put_after_the_first_cleanup`
  - `tests/test_cleanup.py` (2): `test_ladder_completes_aux_lamp_rung_to_rails`, `test_ladder_lamp_is_not_in_series_with_the_coil`
  - `tests/test_convert_standard.py` (1): `test_converted_sheets_pass_geometry`
  - `tests/test_wire_labels.py` (1): `test_device_tags_clear_the_wire_label_row`
- **GUI theme & guidance (3):**
  - `tests/test_theme.py::test_gui_sources_have_no_hex_outside_the_theme_module` (raw `#f59e0b` in `canvas.py:4180`; untouched here due to in-flight PR 162)
  - `tests/test_guidance.py` (2): `test_help_and_menus_use_floor_wording`, `test_every_finding_id_has_a_label`
- **Export / nameplate / golden drift (3):**
  - `tests/test_revisions.py::test_clouds_and_rev_table_export_pdf_svg_dxf_and_print_package`
  - `tests/test_nameplate.py::test_dol_starter_does_not_invent_supplies_and_accepts_fla`
  - `tests/test_from_to.py::test_golden_exports_for_dol_reversing_and_plc`
