# DOL sample: Accept a low-confidence read

File → Photos → drawings… on `examples/dol_starter.pcad.yaml`.

The photo is the synthetic backpanel from `write_backpanel_photo` (no customer
shot). The read is the existing partial-label fixture: part number MC-9, brand
LS Electric, read confidence 0.91. The catalog matcher scores LS MC-9b at about
0.59, so the review row is low-confidence. A blank second photo is missing
info. Accept read is enabled only for the low-confidence row.

The offline stub does not OCR nameplates. This run installs that fixture as
the photo reader (`FakeVision`) so the real menu, Run, and Accept button hit
`reconstruct_photos_to_drawings`. Thresholds are unchanged.

## Driving it
`VERIFY_ROOT=/tmp/verify-panelcad-runs PANELCAD_PY=<venv python> scripts/launch.sh dol-photos-pdf <run>`

1. File → Photos → drawings…, add the nameplate through the file dialog, Run, click Accept read.
2. The low-confidence row clears and the status says drawings were generated.
3. File → Photos → drawings… again, add the blank photo, Run. Accept stays disabled on the missing-info row.
4. File → Export PDF… writes the sheet PDF.
