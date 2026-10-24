# PanelCAD feature map

One file per user-facing feature. Each lists its sub-features, how a user reaches it, how to drive it with `scripts/drive.py` (QTest plus menu actions), and its gotchas. **Proven** means a journey exists in `drive.py` and has passed a recorded run.

| Feature | File | Driver journey | Status |
| --- | --- | --- | --- |
| Print package export with sheet selection | [print-package.md](print-package.md) | `print-package` | Proven (Linux offscreen, run proof-2) |
| New project and sheet editing | [new-project-sheets.md](new-project-sheets.md) | none yet | Mapped |
| Ladder wiring, wires landing on pins | [ladder-wiring.md](ladder-wiring.md) | none yet | Mapped |
| IEC-to-NEMA conversion with the From-To check | [iec-to-nema-convert.md](iec-to-nema-convert.md) | `iec-to-nema-convert` | Proven |
| Problems list (design checks) | [problems-list.md](problems-list.md) | none yet | Mapped |
