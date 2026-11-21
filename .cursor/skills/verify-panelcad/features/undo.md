# Edit → Undo (GUI history)

Ctrl+Z / Edit → Undo must reverse every drawing edit. A project reopened from
disk starts with an empty stack; new place / move / wire / label / rotate /
delete steps then increment the stack and undo correctly.

## How to get to it (user POV)
Open a project, edit the drawing, then Edit → Undo (or Ctrl+Z).

## Driving it with drive.py
`scripts/launch.sh undo <run>` uses `examples/sample_catalog.pcad.yaml`.
1. Confirm the reopened session has zero undo steps.
2. Place a device from the library dock onto the control sheet, wire, label,
   rotate, and delete through the canvas / menus.
3. Assert the undo count rises after each edit and Edit → Undo restores it.
4. Screenshot before and after undo.

## Gotchas
- Offscreen uses `QT_QPA_PLATFORM=offscreen`.
- Do not call Session mutators for the edit path; read `win.session` only for
  assertions on undo depth and model state.
