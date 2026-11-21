# Public Print Fixtures — Attribution & Licenses

All electrical drawings in `tests/fixtures/public_prints/` are derived from publicly available, openly licensed sources for automated importer verification and regression testing. Derived raster PNGs (at 300 DPI and 150 DPI) retain the license obligations of their respective upstream works. These third-party test images are aggregated separately under their respective Creative Commons licenses and are excluded from PanelCAD's AGPL-3.0-or-later code license.

## License Texts / Deeds

- **CC BY 4.0** — Creative Commons Attribution 4.0 International: https://creativecommons.org/licenses/by/4.0/
- **CC BY-SA 3.0** — Creative Commons Attribution-ShareAlike 3.0 Unported: https://creativecommons.org/licenses/by-sa/3.0/

---

## Source Inventory & Attribution

### 1. Tony Kuphaldt — *AC Induction Motor Starters* (ModEL)
- **Source ID:** `kuphaldt_acstarter`
- **Author:** Tony R. Kuphaldt
- **Source URL:** https://www.ibiblio.org/kuphaldt/socratic/model/mod_acstarter.pdf
- **License:** Creative Commons Attribution 4.0 International (CC BY 4.0) — https://creativecommons.org/licenses/by/4.0/
- **Pages Used (upstream `mod_acstarter.pdf`):**
  - `kuphaldt_acstarter_start_stop`: ModEL PDF p. 21 (book p. 18) — three-phase motor starter with control transformer and start/stop seal-in
  - `kuphaldt_acstarter_reversing`: ModEL PDF p. 22 (book p. 19) — reversing motor starter with forward/reverse interlocks
  - `kuphaldt_acstarter_autoxfmr`: ModEL PDF p. 36 (book p. 33) — autotransformer starter with time-delay relay `TD1`
  - `kuphaldt_acstarter_wye_delta`: ModEL PDF p. 38 (book p. 35) — wye-delta starter (`M1`, `M2`, `OL1`, `TD1`)
- **Modifications:** Selected vector pages rasterized to 300 DPI and 150 DPI PNGs.

### 2. Tony Kuphaldt — *Relay Ladder Logic* (ModEL)
- **Source ID:** `kuphaldt_ladderlogic`
- **Author:** Tony R. Kuphaldt
- **Source URL:** https://www.ibiblio.org/kuphaldt/socratic/model/mod_ladderlogic.pdf
- **License:** Creative Commons Attribution 4.0 International (CC BY 4.0) — https://creativecommons.org/licenses/by/4.0/
- **Pages Used (upstream `mod_ladderlogic.pdf`):**
  - `kuphaldt_ladderlogic_redline`: ModEL PDF p. 32 (book p. 29) — photographed relay motor-control ladder with red-line PLR edits
- **Modifications:** Selected page rasterized to 300 DPI and 150 DPI PNGs.

### 3. Tony Kuphaldt — *PLC Discrete I/O* (ModEL)
- **Source ID:** `kuphaldt_plc_discrete`
- **Author:** Tony R. Kuphaldt
- **Source URL:** https://www.ibiblio.org/kuphaldt/socratic/model/mod_plc_discrete.pdf
- **License:** Creative Commons Attribution 4.0 International (CC BY 4.0) — https://creativecommons.org/licenses/by/4.0/
- **Pages Used (upstream `mod_plc_discrete.pdf`):**
  - `kuphaldt_plc_micrologix`: ModEL PDF p. 147 (book p. 143) — MicroLogix 1000 start/stop/OL wiring and ladder logic
  - `kuphaldt_plc_slc500_starter`: ModEL PDF p. 148 (book p. 144) — SLC 500 I/O and 480 VAC motor starter wiring (`F1`–`F7`)
  - `kuphaldt_plc_io_program`: ModEL PDF p. 153 (book p. 149) — discrete I/O card wiring and PLC ladder program
  - `kuphaldt_plc_compressor`: ModEL PDF p. 156 (book p. 152) — SLC 500 air compressor wiring (`PSH`, `PSL`)
- **Modifications:** Selected vector pages rasterized to 300 DPI and 150 DPI PNGs.

### 4. Wikimedia Commons — Wiring Diagram of Motor Control Centre
- **Source ID:** `wiki_mcc_wiring`
- **Author:** Dmitry G
- **Source URL:** https://commons.wikimedia.org/wiki/File:Wiring_diagram_of_motor_control_centre.JPG
- **License:** Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0) — https://creativecommons.org/licenses/by-sa/3.0/
- **Pages Used:**
  - `wiki_mcc_wiring`: single image — IEC power and start/stop control wiring (`QS1`, `KM`, `K1`–`K4`, `SB1`, `SB2`, `HL1`, `HL2`)
- **Modifications:** Converted from JPEG to lossless PNG and normalized to 150 DPI (native `1600x1540`) and 300 DPI (`3200x3080`); derivative PNGs remain under CC BY-SA 3.0.

### 5. Wikimedia Commons — Pump-Station Motor Control Centre Wiring
- **Source ID:** `wiki_pump_station_mcc`
- **Author:** Dmitry G
- **Source URL:** https://commons.wikimedia.org/wiki/File:Wiring_diagram_of_motor_control_centre_on_pump_station.JPG
- **License:** Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0) — https://creativecommons.org/licenses/by-sa/3.0/
- **Pages Used:**
  - `wiki_pump_station_mcc`: single image — IEC duplex pump-station starters, E-stops, and pressure-switch interlocks
- **Modifications:** Converted from JPEG to lossless PNG and normalized to 150 DPI (native `1860x1420`) and 300 DPI (`3720x2840`); derivative PNGs remain under CC BY-SA 3.0.

### 6. Wikimedia Commons — Air-Fans Control Centre Wiring
- **Source ID:** `wiki_air_fans_mcc`
- **Author:** Dmitry G
- **Source URL:** https://commons.wikimedia.org/wiki/File:Wiring_diagram_of_air_fans_control_centre.JPG
- **License:** Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0) — https://creativecommons.org/licenses/by-sa/3.0/
- **Pages Used:**
  - `wiki_air_fans_mcc`: single image — IEC power and 24 V control ladder for three fan motors and smoke exhaust
- **Modifications:** Converted from JPEG to lossless PNG and normalized to 150 DPI (native `2275x2500`) and 300 DPI (`4550x5000`); derivative PNGs remain under CC BY-SA 3.0.

### 7. Wikimedia Commons — Gantry Crane Wiring Diagram
- **Source ID:** `wiki_gantry_crane`
- **Author:** Dmitry G
- **Source URL:** https://commons.wikimedia.org/wiki/File:Wiring_diagram_of_the_gantry_crane.JPG
- **License:** Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0) — https://creativecommons.org/licenses/by-sa/3.0/
- **Pages Used:**
  - `wiki_gantry_crane`: single image — wide industrial IEC gantry crane power and control schematic
- **Modifications:** Converted from JPEG to lossless PNG and normalized to 300 DPI (native `5500x1940`) and 150 DPI (`2750x970`); derivative PNGs remain under CC BY-SA 3.0.

### 8. Wikimedia Commons — Sandblaster Wiring Diagram
- **Source ID:** `wiki_sandblaster`
- **Author:** Dmitry G
- **Source URL:** https://commons.wikimedia.org/wiki/File:Sandblaster_wiring_diagram.JPG
- **License:** Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0) — https://creativecommons.org/licenses/by-sa/3.0/
- **Pages Used:**
  - `wiki_sandblaster`: single image — IEC motor starter with door interlock, thermal relay, RCD, and wire labels
- **Modifications:** Converted from JPEG to lossless PNG and normalized to 150 DPI (native `1300x1660`) and 300 DPI (`2600x3320`); derivative PNGs remain under CC BY-SA 3.0.
