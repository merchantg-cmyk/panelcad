# Problems list (design checks)

## Sub-features
- The Problems dock, `problemsDock` (View → Problems, Ctrl+5), holds a `QTableWidget` (`ProblemsDock.table`) and the buttons Jump, Fix, Ignore, Restore and Export….
- Signals: jump_requested, fix_requested, suppress_requested, unsuppress_requested, export_requested, and print_requested (which opens Print Package).
- Reports → Design checks… exports the checks.

## How to get to it (user POV)
Open a project and press Ctrl+5. Select a row, then use Jump to go to it on the sheet, Fix to apply the suggested fix, or Ignore to suppress it.

## Driving it with drive.py
Not implemented yet. The plan:
1. Toggle the dock with `menu_action(win, "View", "Problems")`.
2. Read the table rows and select one with `QTest.mouseClick` on the `table.viewport()` cell rect, then click "Jump" and assert the canvas sheet changed.
3. Click "Ignore" and assert the row moves to suppressed.
4. Prove it by saving and checking that the suppression is persisted in the YAML. Screenshot the dock before and after.

## Gotchas
- The row order follows check severity, so match rows by their check/message text, not by index.
