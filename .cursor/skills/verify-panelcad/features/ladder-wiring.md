# Ladder wiring: wires land on pins

## Sub-features
- Edit → Wire (W) wire mode. Hover a terminal and click or drag to the next terminal (the status bar text says so).
- Pin leads (`core/pin_ink.py`). Wires and leads count as label obstacles.
- Design checks `wire_end_off_pin` and `label_covered_by_wire`.
- Ladder relayout places plain coil/seal rungs in wired order. Any wired part left out of the rungs is flagged "unplaced".
- Edit → Drawing prefs: wire numbering (sheet/line, sequential, IEC) and the NEMA line pitch.

## How to get to it (user POV)
Open a ladder (NEMA) project such as `examples/xref_nema_seal_in.pcad.yaml`. Press W, then click from one device terminal to another.

## Driving it with drive.py
Not implemented yet. The plan:
1. Find `sheetCanvas` and map two terminal scene positions to viewport points with `canvas.mapFromScene`. Use `QTest.mousePress/mouseMove/mouseRelease` with wire mode on, entered through the Edit → Wire action.
2. Prove it with File → Save, then check that the YAML holds the new wire between those two pins.
3. Open the Problems dock and confirm there are no `wire_end_off_pin` rows for the new wire. Screenshot the canvas at the end.

## Gotchas
- Terminal positions come from library symbols. Read them from the scene items, never hard-code pixel coordinates.
- Never use customer prints as fixtures. Use the repo examples only.
