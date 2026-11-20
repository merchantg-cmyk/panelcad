# Public Prints Baseline Scorecard

- **DPI:** 150
- **Backend:** `raster` (`electrical` OCR)
- **Sheets:** 16 total (8 with ground truth, 8 smoke-only)
- **Outcomes:** 16 built, 0 empty (`EmptyImport`), 0 errors
- **Mean Device Recall (truth sheets):** 18.4%
- **Mean Device Precision (truth sheets):** 15.3%
- **Mean Wire Recall (truth sheets):** 0.9%

| sheet_id | standard | kind | truth | status | dev_found | dev_matched | wire_found | wire_matched | dev_prec | dev_rec | wire_prec | wire_rec |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ghanem_motor_schematic | NEMA/ANSI | schematic | full | built | 1 | 0/43 | 0 | 0/12 | 0.0% | 0.0% | - | 0.0% |
| ghanem_motor_panel | NEMA/ANSI | panel_layout | full | built | 2 | 0/4 | 0 | - | 0.0% | 0.0% | 100.0% | - |
| kuphaldt_acstarter_start_stop | NEMA/ANSI | schematic | smoke_only | built | 3 | - | 0 | - | - | - | - | - |
| kuphaldt_acstarter_reversing | NEMA/ANSI | schematic | smoke_only | built | 14 | - | 7 | - | - | - | - | - |
| kuphaldt_acstarter_autoxfmr | NEMA/ANSI | schematic | smoke_only | built | 2 | - | 0 | - | - | - | - | - |
| kuphaldt_acstarter_wye_delta | NEMA/ANSI | schematic | full | built | 21 | 4/4 | 9 | 1/15 | 19.1% | 100.0% | 11.1% | 6.7% |
| kuphaldt_ladderlogic_redline | NEMA/ANSI | schematic | smoke_only | built | 36 | - | 13 | - | - | - | - | - |
| kuphaldt_plc_micrologix | NEMA/ANSI | schematic | smoke_only | built | 4 | - | 0 | - | - | - | - | - |
| kuphaldt_plc_slc500_starter | NEMA/ANSI | schematic | full | built | 5 | 0/7 | 0 | 0/11 | 0.0% | 0.0% | - | 0.0% |
| kuphaldt_plc_io_program | NEMA/ANSI | schematic | smoke_only | built | 1 | - | 0 | - | - | - | - | - |
| kuphaldt_plc_compressor | NEMA/ANSI | schematic | smoke_only | built | 6 | - | 1 | - | - | - | - | - |
| wiki_mcc_wiring | IEC | schematic | full | built | 5 | 3/14 | 0 | 0/5 | 60.0% | 21.4% | - | 0.0% |
| wiki_pump_station_mcc | IEC | schematic | full | built | 40 | 4/24 | 11 | 0/5 | 10.0% | 16.7% | 0.0% | 0.0% |
| wiki_air_fans_mcc | IEC | schematic | full | built | 4 | 0/29 | 0 | 0/5 | 0.0% | 0.0% | - | 0.0% |
| wiki_gantry_crane | IEC | schematic | smoke_only | built | 1 | - | 0 | - | - | - | - | - |
| wiki_sandblaster | IEC | schematic | full | built | 3 | 1/11 | 1 | 0/49 | 33.3% | 9.1% | 0.0% | 0.0% |
