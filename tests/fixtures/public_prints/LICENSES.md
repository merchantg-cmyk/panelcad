# Public Print Fixtures — Attribution & Licenses

All electrical drawings in `tests/fixtures/public_prints/` are derived from publicly available, openly licensed sources for automated importer verification and regression testing. Derived raster PNGs (at 300 DPI and 150 DPI) retain the license obligations of their respective upstream works.

---

## Source Inventory & Attribution

### 1. Tony Kuphaldt — *AC Induction Motor Starters* (ModEL)
- **Source ID:** `kuphaldt_acstarter`
- **Author:** Tony R. Kuphaldt
- **Source URL:** https://www.ibiblio.org/kuphaldt/socratic/model/mod_acstarter.pdf
- **License:** Creative Commons Attribution 4.0 International (CC BY 4.0)
- **Pages Used (from `kuphaldt_acstarter_p21-39.pdf`):**
  - `kuphaldt_acstarter_start_stop`: excerpt page 1 (book p. 18; ModEL p. 21) — three-phase motor starter with control transformer and start/stop seal-in
  - `kuphaldt_acstarter_reversing`: excerpt page 2 (book p. 19; ModEL p. 22) — reversing motor starter with forward/reverse interlocks
  - `kuphaldt_acstarter_autoxfmr`: excerpt page 8 (book p. 33) — autotransformer starter with time-delay relay `TD1`
  - `kuphaldt_acstarter_wye_delta`: excerpt page 10 (book p. 35) — wye-delta starter (`M1`, `M2`, `OL1`, `TD1`)
- **Modifications:** Selected vector pages rasterized to 300 DPI and 150 DPI PNGs.

### 2. Tony Kuphaldt — *Relay Ladder Logic* (ModEL)
- **Source ID:** `kuphaldt_ladderlogic`
- **Author:** Tony R. Kuphaldt
- **Source URL:** https://www.ibiblio.org/kuphaldt/socratic/model/mod_ladderlogic.pdf
- **License:** Creative Commons Attribution 4.0 International (CC BY 4.0)
- **Pages Used (from `kuphaldt_ladderlogic_p32-33.pdf`):**
  - `kuphaldt_ladderlogic_redline`: excerpt page 1 (book p. 29; ModEL p. 32) — photographed relay motor-control ladder with red-line PLR edits
- **Modifications:** Selected page rasterized to 300 DPI and 150 DPI PNGs.

### 3. Tony Kuphaldt — *PLC Discrete I/O* (ModEL)
- **Source ID:** `kuphaldt_plc_discrete`
- **Author:** Tony R. Kuphaldt
- **Source URL:** https://www.ibiblio.org/kuphaldt/socratic/model/mod_plc_discrete.pdf
- **License:** Creative Commons Attribution 4.0 International (CC BY 4.0)
- **Pages Used (from `kuphaldt_plc_discrete_p147-156.pdf`):**
  - `kuphaldt_plc_micrologix`: excerpt page 1 (book p. 143; ModEL p. 147) — MicroLogix 1000 start/stop/OL wiring and ladder logic
  - `kuphaldt_plc_slc500_starter`: excerpt page 2 (book p. 144; ModEL p. 148) — SLC 500 I/O and 480 VAC motor starter wiring (`F1`–`F7`)
  - `kuphaldt_plc_io_program`: excerpt page 7 (book p. 149; ModEL p. 153) — discrete I/O card wiring and PLC ladder program
  - `kuphaldt_plc_compressor`: excerpt page 10 (book p. 152; ModEL p. 156) — SLC 500 air compressor wiring (`PSH`, `PSL`)
- **Modifications:** Selected vector pages rasterized to 300 DPI and 150 DPI PNGs.

### 4. Khaled Ghanem — Industrial Motor Control Power Schematic
- **Source ID:** `github_industrial_motor_schematic`
- **Author:** Khaled Ghanem
- **Source URL:** https://raw.githubusercontent.com/KhaledGhanem0/industrial-motor-control/main/assets/schematic.pdf
- **Repository:** https://github.com/KhaledGhanem0/industrial-motor-control
- **License:** MIT License (Copyright (c) 2026 Khaled Ghanem; full license text below)
- **Pages Used:**
  - `ghanem_motor_schematic`: page 1 of 1 — 14-motor power distribution schematic (`MS7`–`MS20`, `MC7`–`MC20`, `MOT7`–`MOT20`)
- **Modifications:** Rasterized to 300 DPI and 150 DPI PNGs.

### 5. Khaled Ghanem — Industrial Motor Control Panel Layout
- **Source ID:** `github_industrial_motor_panel`
- **Author:** Khaled Ghanem
- **Source URL:** https://raw.githubusercontent.com/KhaledGhanem0/industrial-motor-control/main/assets/panel_layout.pdf
- **Repository:** https://github.com/KhaledGhanem0/industrial-motor-control
- **License:** MIT License (Copyright (c) 2026 Khaled Ghanem; full license text below)
- **Pages Used:**
  - `ghanem_motor_panel`: page 1 of 1 — enclosure backplate layout (`PS01`, `PLC`, `SLOT 1`–`SLOT 3`, `PS02`, `CR300`, rails, wire ducts)
- **Modifications:** Rasterized to 300 DPI and 150 DPI PNGs.

### 6. Wikimedia Commons — Wiring Diagram of Motor Control Centre
- **Source ID:** `wiki_mcc_wiring`
- **Author:** Dmitry G
- **Source URL:** https://commons.wikimedia.org/wiki/File:Wiring_diagram_of_motor_control_centre.JPG
- **License:** Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0)
- **Pages Used:**
  - `wiki_mcc_wiring`: single image — IEC power and start/stop control wiring (`QS1`, `KM`, `K1`–`K4`, `SB1`, `SB2`, `HL1`, `HL2`)
- **Modifications:** Converted from JPEG to lossless PNG and normalized to 150 DPI (native `1600x1540`) and 300 DPI (`3200x3080`); derivative PNGs remain under CC BY-SA 3.0.

### 7. Wikimedia Commons — Pump-Station Motor Control Centre Wiring
- **Source ID:** `wiki_pump_station_mcc`
- **Author:** Dmitry G
- **Source URL:** https://commons.wikimedia.org/wiki/File:Wiring_diagram_of_motor_control_centre_on_pump_station.JPG
- **License:** Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0)
- **Pages Used:**
  - `wiki_pump_station_mcc`: single image — IEC duplex pump-station starters, E-stops, and pressure-switch interlocks
- **Modifications:** Converted from JPEG to lossless PNG and normalized to 150 DPI (native `1860x1420`) and 300 DPI (`3720x2840`); derivative PNGs remain under CC BY-SA 3.0.

### 8. Wikimedia Commons — Air-Fans Control Centre Wiring
- **Source ID:** `wiki_air_fans_mcc`
- **Author:** Dmitry G
- **Source URL:** https://commons.wikimedia.org/wiki/File:Wiring_diagram_of_air_fans_control_centre.JPG
- **License:** Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0)
- **Pages Used:**
  - `wiki_air_fans_mcc`: single image — IEC power and 24 V control ladder for three fan motors and smoke exhaust
- **Modifications:** Converted from JPEG to lossless PNG and normalized to 150 DPI (native `2275x2500`) and 300 DPI (`4550x5000`); derivative PNGs remain under CC BY-SA 3.0.

### 9. Wikimedia Commons — Gantry Crane Wiring Diagram
- **Source ID:** `wiki_gantry_crane`
- **Author:** Dmitry G
- **Source URL:** https://commons.wikimedia.org/wiki/File:Wiring_diagram_of_the_gantry_crane.JPG
- **License:** Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0)
- **Pages Used:**
  - `wiki_gantry_crane`: single image — wide industrial IEC gantry crane power and control schematic
- **Modifications:** Converted from JPEG to lossless PNG and normalized to 300 DPI (native `5500x1940`) and 150 DPI (`2750x970`); derivative PNGs remain under CC BY-SA 3.0.

### 10. Wikimedia Commons — Sandblaster Wiring Diagram
- **Source ID:** `wiki_sandblaster`
- **Author:** Dmitry G
- **Source URL:** https://commons.wikimedia.org/wiki/File:Sandblaster_wiring_diagram.JPG
- **License:** Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0)
- **Pages Used:**
  - `wiki_sandblaster`: single image — IEC motor starter with door interlock, thermal relay, RCD, and wire labels
- **Modifications:** Converted from JPEG to lossless PNG and normalized to 150 DPI (native `1300x1660`) and 300 DPI (`2600x3320`); derivative PNGs remain under CC BY-SA 3.0.

---

## MIT License Text (`github.com/KhaledGhanem0/industrial-motor-control`)

```text
MIT License

Copyright (c) 2026 Khaled Ghanem

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
