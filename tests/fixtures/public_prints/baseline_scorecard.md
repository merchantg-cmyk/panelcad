# Public Prints Baseline Scorecard

- **DPI:** 150
- **Backend:** `raster` (`electrical` OCR)
- **Sheets:** 14 total (6 with ground truth, 8 smoke-only)
- **Outcomes:** 14 built, 0 empty (`EmptyImport`), 0 errors
- **Mean Device Recall (truth sheets):** 25.7%
- **Mean Device Precision (truth sheets):** 23.7%
- **Mean Wire Recall (truth sheets with wire numbers):** 0.0%

| sheet_id | standard | kind | truth | status | dev_found | dev_matched | dev_extra | wire_found | wire_matched | dev_prec | dev_rec | wire_prec | wire_rec |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| kuphaldt_acstarter_start_stop | NEMA/ANSI | schematic | smoke_only | built | 3 | - | - | 0 | - | - | - | - | - |
| kuphaldt_acstarter_reversing | NEMA/ANSI | schematic | smoke_only | built | 14 | - | - | 7 | - | - | - | - | - |
| kuphaldt_acstarter_autoxfmr | NEMA/ANSI | schematic | smoke_only | built | 2 | - | - | 0 | - | - | - | - | - |
| kuphaldt_acstarter_wye_delta | NEMA/ANSI | schematic | full | built | 21 | 4/4 | 17 | 9 | - | 19.1% | 100.0% | 0.0% | - |
| kuphaldt_ladderlogic_redline | NEMA/ANSI | schematic | smoke_only | built | 36 | - | - | 13 | - | - | - | - | - |
| kuphaldt_plc_micrologix | NEMA/ANSI | schematic | smoke_only | built | 4 | - | - | 0 | - | - | - | - | - |
| kuphaldt_plc_slc500_starter | NEMA/ANSI | schematic | full | built | 5 | 0/7 | 5 | 0 | - | 0.0% | 0.0% | - | - |
| kuphaldt_plc_io_program | NEMA/ANSI | schematic | smoke_only | built | 1 | - | - | 0 | - | - | - | - | - |
| kuphaldt_plc_compressor | NEMA/ANSI | schematic | smoke_only | built | 6 | - | - | 1 | - | - | - | - | - |
| wiki_mcc_wiring | IEC | schematic | full | built | 5 | 4/14 | 1 | 0 | 0/5 | 80.0% | 28.6% | - | 0.0% |
| wiki_pump_station_mcc | IEC | schematic | full | built | 40 | 4/24 | 36 | 11 | 0/5 | 10.0% | 16.7% | 0.0% | 0.0% |
| wiki_air_fans_mcc | IEC | schematic | full | built | 4 | 0/29 | 4 | 0 | 0/5 | 0.0% | 0.0% | - | 0.0% |
| wiki_gantry_crane | IEC | schematic | smoke_only | built | 1 | - | - | 0 | - | - | - | - | - |
| wiki_sandblaster | IEC | schematic | full | built | 3 | 1/11 | 2 | 1 | 0/49 | 33.3% | 9.1% | 0.0% | 0.0% |
