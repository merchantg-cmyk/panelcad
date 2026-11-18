# File → Print dialog (fit-to-page + page range)

File → Print… opens a print preview of every sheet, fitted to the printer page.
The preview's Print button opens a `QPrintDialog` with page range and current page
enabled so the shop can print one sheet of a multi-sheet project.

## Sub-features
- Fit-to-page into the printer paint rect (paper size, orientation, margins, DPI).
- `QPrintDialog` options: `PrintPageRange`, `PrintCurrentPage`, `PrintToFile`, `PrintShowPageSize`.
- `setMinMax(1, N)` / `setFromTo(1, N)` so the range controls are active.
- Paint honors `PageRange` / `CurrentPage` (see `panelcad.render.page.resolve_print_sheets`).

## How to get to it (user POV)
Open a project, choose File → Print…, then click Print on the preview toolbar.
Pick a page range (or Current page) and confirm.

## Driving it with drive.py
`scripts/launch.sh print-dialog <run>` uses `examples/sample_catalog.pcad.yaml` (3 sheets).
1. `menu_action(win, "File", "Print…").trigger()`.
2. `ModalPump` handler 1 on `QPrintPreviewDialog`: screenshot, trigger the Print action.
3. Handler 2 on `QPrintDialog`: assert page-range and current-page options, min/max, screenshot, reject.
4. Reject the preview if it is still open.

## Gotchas
- Offscreen has no real printer; the journey cancels the print dialog after the screenshot.
- The Print action on the preview is rebound by `MainWindow.print_sheet` so the dialog gets PanelCAD's options (stock Qt preview would omit Current page).
