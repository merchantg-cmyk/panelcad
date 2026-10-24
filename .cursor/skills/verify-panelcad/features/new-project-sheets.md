# New project and sheet editing

## Sub-features
- File → New opens the print-style chooser (`style_dialog.choose_print_style`; "ladder" also sets square coils), then shows the editor on "Untitled Panel".
- Insert → Sheet… adds a sheet. View → Next sheet (PgDown) and Previous sheet (PgUp) move between them, and the sheet tab bar is `sheet_bar.py`.
- File → Save / Save As… writes YAML, which is the source of truth.
- Edit → Drawing prefs → Standard: IEC / NEMA/ANSI.

## How to get to it (user POV)
From the start page or the File menu, choose New and pick a print style (ladder for NEMA). Then add sheets from Insert → Sheet…, place parts from the Library dock, and save.

## Driving it with drive.py
Not implemented yet. The plan:
1. Trigger `menu_action(win, "File", "New")` and use a ModalPump handler to pick ladder in the style dialog.
2. Trigger `Insert → Sheet…` and fill in the sheet dialog.
3. Trigger `File → Save As…`. The QFileDialog is modal; type the path into its line edit.
4. Prove it by parsing the saved YAML for the new sheet id and `drawing_style: ladder`, with screenshots of the `sheetCanvas` before and after.

## Gotchas
- Launching with no project shows the start page. Launch with a sample instead, then use File → New.
- QFileDialog can be native on Windows. Offscreen on Linux it is the Qt dialog.
