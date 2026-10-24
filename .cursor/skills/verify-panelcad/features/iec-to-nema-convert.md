# IEC-to-NEMA conversion with the From-To check

## Sub-features
- Tools → "Convert drawing standard..." opens `ConvertDialog`. It has IEC/NEMA radios, Whole project or Selected sheets, and `keep-existing-numbers`.
- A chunked review (`convert-review`) with per-chunk `accept:<id>`, `rerun:<id>` and `revert:<id>`, and a before/after `convert-compare` dialog.
- The From-To check (`convert/from_to_check.py`, scorer `from-to-check/1`). The save happens only after every chunk is done and the check passes. A blocked single-chunk revert raises `RevertRefused` and keeps the conversion. A final-step block rolls back to the original project.

## How to get to it (user POV)
Open an IEC project (`examples/xref_iec_mirror.pcad.yaml`) and choose Tools → Convert drawing standard.... Pick NEMA/ANSI, click Convert, inspect the Before/After preview, click Accept on each chunk, and click Close.

## Driving it with drive.py (`feature_iec_to_nema_convert`)
Implemented in `scripts/drive.py` (`launch.sh iec-to-nema-convert <run>` / `run-windows.ps1 -Feature iec-to-nema-convert -Run <run>`):
1. Trigger `Tools → Convert drawing standard...`; `ModalPump` captures `ConvertDialog`.
2. Verify `dlg.nema.isChecked()` and `dlg.whole.isChecked()`, type the output path into `dlg.path`, and click the `Convert` button via `QTest.mouseClick`.
3. Verify `convert-review-header` is visible, click `thumb:Before:main` to open and screenshot `CompareDialog` (`convert-compare`), then click every `accept:<id>` button via `QTest.mouseClick` and click `Close`.
4. Verify the saved YAML has `title_block.standard == "NEMA/ANSI"`, `drawing_style == "ladder"`, NEMA `library_id`s (`nema.*`), role-mapped tags (`PB1`, `M1`, `PL1`, `PL2`), and the `.convert.json` workspace reports `connection_check.scorer == "from-to-check/1"`, `ok == True`, `missing == []`, `extra == []`, and all chunks `accepted`.

## Gotchas
- With `PANELCAD_AI_LOOKUP=off`, deterministic table conversion runs offline without any external API calls. Never point it at a real provider key during a proof.
