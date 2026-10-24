# IEC-to-NEMA conversion with the From-To check

## Sub-features
- Tools → "Convert drawing standard..." opens `ConvertDialog`. It has IEC/NEMA radios, Whole project or Selected sheets, and `keep-existing-numbers`.
- A chunked review (`convert-review`) with per-chunk `accept:<id>`, `rerun:<id>` and `revert:<id>`, and a before/after `convert-compare` dialog.
- The From-To check (`convert/from_to_check.py`, scorer from-to-check/1). The save happens only after every chunk is done and the check passes. A blocked single-chunk revert raises RevertRefused and keeps the conversion. A final-step block rolls back to the original project.

## How to get to it (user POV)
Open an IEC project (`examples/xref_iec_mirror.pcad.yaml`) and choose Tools → Convert drawing standard.... Pick NEMA/ANSI, run it, accept each chunk, and finish.

## Driving it with drive.py
Not implemented yet. The plan:
1. Trigger the menu action; the ModalPump handler gets the ConvertDialog.
2. Click `self.nema` and start the conversion.
3. For each `chunk:<id>` frame, click `accept:<id>`, found with `findChild(QPushButton, "accept:<id>")`.
4. Prove it: the saved YAML shows NEMA library ids or the standard, and the From-To result is pass. Also confirm that every wire's endpoints are unchanged, comparing the wire lists before and after (File → Export wire list…).

## Gotchas
- The conversion may call an AI provider. With `PANELCAD_AI_LOOKUP=off`, check whether it still runs and record what it skips. Never point it at a real provider key during a proof.
