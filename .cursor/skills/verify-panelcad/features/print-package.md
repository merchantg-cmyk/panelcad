# Print package export

File → Print Package… builds one vector PDF: the cover, the index, the revisions, the nameplate, the selected drawing sheets, the BOM, the wire lists, the device index, the PLC I/O list, the design checks and the thermal page. The choices you make are saved per project in QSettings.

## Sub-features
- Section checkboxes (`PrintPackageDialog.checks[<key>]`, with keys from `render.print_package.SECTIONS`).
- Per-sheet selection (`sheet_list`, checkable items, `UserRole` = sheet id). Unticking every sheet drops the drawings section.
- Paper combo (`paper`). The default is "Mixed".
- PDF path (`path` QLineEdit, plus Browse…).
- The result box "Print package written", whose text comes from `describe_result`, and a status-bar summary.
- The NOT FOR BUILD gate: parts that need info block a layout print package ("Print package failed").
- The selection is remembered per project (QSettings `print_package` group).
- File → Export package… (the folder variant). It is not driven yet.

## How to get to it (user POV)
Open a project, then choose File → Print Package…. Tick or untick sheets and sections, type the PDF path, and click OK. A box reports the path, the page count, and which sections were printed or skipped.

## Driving it with drive.py
`scripts/launch.sh print-package <run>` uses `examples/sample_catalog.pcad.yaml`, which has 3 sheets: power, control and enclosure.
1. Call `menu_action(win, "File", "Print Package…").trigger()`.
2. `ModalPump` handler 1, on the PrintPackageDialog:
   - assert the list shows the sheets power, control and enclosure;
   - untick `enclosure` with a QTest click on the item's check area (the Space key is the fallback);
   - select all in `path` and type the scratch PDF path;
   - click the OK button in the `QDialogButtonBox`.
3. Handler 2: the QMessageBox titled "Print package written". Save its text, take a screenshot, and accept it.
4. Side effects:
   - the PDF exists and is larger than 1 KB;
   - pypdf reports exactly 13 pages, asserted by the `PDF has 13 pages` check (it is 14 with every sheet);
   - the box text contains "Drawing sheets: 2";
   - the pypdf outline level-1 entries under Drawings are exactly `["Power", "Control"]`.

## Gotchas
- `examples/dol_starter.pcad.yaml` fails with "NOT FOR BUILD: 6 part(s) need a catalog part…", even with the enclosure sheet unticked. That is the app's real gate, not a driver bug, so use `sample_catalog` (run proof-1 recorded this failure).
- QSettings remembers the last selection per project. The isolated `XDG_CONFIG_HOME` keeps runs independent. On Windows, the registry is shared (see SKILL.md).
- `dialog.exec()` blocks the driver, so all dialog work must happen in `ModalPump` handlers.
- "Drawings" is level 0 in the PDF outline and the sheets are level 1. "Enclosure Thermal" is a report, not the enclosure sheet.
