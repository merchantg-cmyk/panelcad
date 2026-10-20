# AutomationDirect, LS Electric, and Yaskawa verification

Read on 2026-10-08. `verified: full` means every filled field is in the `source` PDF. `verified: partial` means a wanted field was not on that URL, or an outline comes from a second document. The URL column for each field is the document that field was read from. Blank millimetres were left blank on purpose.

## Counts

| Manufacturer | Parts | verified full | full W x H x D | width and height |
| --- | ---: | ---: | ---: | ---: |
| AutomationDirect | 57 | 8 | 28 | 30 |
| LS Electric | 65 | 22 | 44 | 44 |
| Yaskawa | 68 | 13 | 22 | 22 |

| Manufacturer | Category | Count |
| --- | --- | ---: |
| AutomationDirect | connection.duct | 2 |
| AutomationDirect | connection.terminal | 1 |
| AutomationDirect | control.plc | 5 |
| AutomationDirect | drive.vfd | 10 |
| AutomationDirect | operator.light | 1 |
| AutomationDirect | operator.pushbutton | 3 |
| AutomationDirect | operator.selector | 1 |
| AutomationDirect | power.supply | 2 |
| AutomationDirect | protection.breaker | 4 |
| AutomationDirect | protection.fuse | 1 |
| AutomationDirect | protection.overload | 8 |
| AutomationDirect | protection.supplementary | 2 |
| AutomationDirect | switching.contactor | 17 |
| LS Electric | control.plc | 2 |
| LS Electric | protection.breaker | 1 |
| LS Electric | drive.vfd | 23 |
| LS Electric | protection.mpcb | 17 |
| LS Electric | protection.overload | 18 |
| LS Electric | switching.contactor | 4 |
| Yaskawa | drive.option | 10 |
| Yaskawa | drive.vfd | 58 |

## Coil and order strings

- GH15 order codes are the full catalog. On ghseriescontactors.pdf tMRC-100 the trailing letter is the coil column: A is 110-120 VAC 50-60 Hz, B is 220-240 VAC 50-60 Hz. GH15GT-3-00A is 120 VAC 60 Hz only. Depth is the tMRC-107 row that names that frame: BN/CN/DN 85.0 mm, ET/FT 91.0 mm, GT 109.0 mm. GT SCCR 5 kA is the UL MCCB Type 2 figure on tMRC-102.
- LS MC-9b, MC-12b, MC-18b, and MC-22b keep those catalog strings. `coil_voltage` is `120V 60Hz`, one voltage on catalog page 24 in the 60 Hz list for the 22AF column. Page 20 prints the numbering blocks (example MC-6a / R / 220V 60Hz SCREW 1a1b). That combined string is not one catalog cell, so it is not stored as the catalog. Dimension page 142, heading MC-9b~22b AC 1NO1NC, is 45 x 73.5 x 87.4 mm.
- The 2023 Metasol catalog prints MMS-32H. It does not print MMS-32S. No MMS outline was on the dimension pages, so those millimetres stay blank.
- LS DOL branch device is BKN 3P C6A from the LSIS MCB catalog on ls-electric.com (C curve, 6 A, 3-pole, order No. 06130054R0, heading 6 kA at 400 VAC, 35 mm DIN). The page prints 17.8 mm per pole and does not print a 3-pole outline, so width, height, and depth stay blank. The rest of that C-curve table was not copied.

## Secondary vendor

Circuit Builder `secondary_manufacturer` fills a category when the preferred brand has no match. Omit it and the enclosure maker is used when that name is a parts brand; otherwise AutomationDirect is used. `none` or `off` leaves the symbol generic. A fallback assignment sets `catalog_fallback=yes` and `catalog_primary` to the preferred brand. The same argument is on the dialog, `panelcad electrical circuit --secondary-manufacturer`, JSONL `insert_circuit`, and the drawing-agent tool.

## Enclosures

Published back-panel sizes, not an H-3 by W-3 estimate.

| Catalog | HxWxD in | Subpanel HxW in | Page |
| --- | --- | --- | --- |
| N412121206C | 12.00 x 12.00 x 6.92 | NP1212C 10.20 x 10.20 | n412sdwm.pdf and encsubpanelsnpc.pdf |
| N412161206C | 16.00 x 12.00 x 6.92 | NP1612C 14.20 x 10.20 | n412sdwm.pdf and encsubpanelsnpc.pdf |
| N412161208C | 16.00 x 12.00 x 8.92 | NP1612C 14.20 x 10.20 | n412sdwm.pdf and encsubpanelsnpc.pdf |
| N412201608C | 20.00 x 16.00 x 8.92 | NP2016C 18.20 x 14.20 | n412sdwm.pdf and encsubpanelsnpc.pdf |
| N412242008C | 24.00 x 20.00 x 8.92 | NP2420C 22.20 x 18.20 | n412sdwm.pdf and encsubpanelsnpc.pdf |
| N412161206SSC | 16.00 x 12.00 x 6.92 | NP1612C 14.20 x 10.20 | n412sssdwm PDF (Wiegmann) and encsubpanelsnpc.pdf |
| BB121275W | 4.80 x 4.72 x 2.95 | BPB1212P 4.41 x 4.33 | bantam_poly PDF tENA-2 and tENA-17 |
| BB161255W | 6.30 x 4.72 x 2.17 | BPB1612P 5.94 x 4.41 | bantam_poly PDF tENA-2 and tENA-17 |
| BB201275W | 7.87 x 4.72 x 2.95 | BPB2012P 7.44 x 4.29 | bantam_poly PDF tENA-2 and tENA-17 |

## GA500 local-control schematic

A Yaskawa row whose description contains GA500 is drawn as the 3-wire sequence in the programming manual, not as one DI held closed by a start button. The sheet note carries the same citations. Generic drives stay on DI1 and COM.

- TOEPYAIGA5002A Table 2.45 p.287. With A1-03 = 3330 the parenthesis value is the default: H1-05 = 0 (3-Wire Sequence) on terminal S5.
- TOEPYAIGA5002A p.291, setting 0 and Figure 2.56. Setting 3-wire on a terminal other than S1 or S2 makes S1 Run (turn ON to run) and S2 Stop (turn OFF to stop). The terminal set to 0 is forward when OFF and reverse when ON. Figure 2.57 on the same page is the time chart. b1-17 = 0 (disregard an existing RUN command at power-up) is the default called out in that warning.
- TOEPC7106173TA note *9 p.44 and Table 8.4 p.66. Sinking mode, internal supply: jumper SC to SP. Do not also close SC-SN. Do not close SP-SN. SN is the MFDI 0 V common. SP is +24 Vdc. SC is the MFDI selection common.
- TOEPC7106173TA Figure 8.1 pp.42-43 draws a safety switch into Safe Disable H1 and H2, separate from the run terminals. The manual does not call that switch an E-stop. Note *16 p.45: use only sourcing mode for Safe Disable. Note *19 p.45: disconnect the H1-HC and H2-HC jumpers. Figure 18.2 p.233: H1 and H2 are the two channels that stop the output transistors. HC is the Safe Disable common for the internal supply.
- Catalog 4005ABA on SL.GA500.01: ND 3 HP 5.4 A, HD 3 HP 4.8 A. The schematic prints both. The stored ampere field is the Heavy Duty current.
- TOEPC7106173TA Table 5.4 p.19, model 4005: HD 1.5 kW (3 HP) 4.8 A, ND 2.2 kW (3 HP) 5.4 A. The sample motor is printed as 3 hp (2.2 kW) at 460/480 V with FLA 4.8 A. NEC Table 430.250 lists 4.8 A for 3 hp at 460 V, which is also the HD output current. The 2.2 kW figure is the ND applicable-motor value on that row, whose ND current is 5.4 A. The sheet note says so.
- TOEPC7106173TA Table 15.4 p.218, model 4005: semiconductor fuse Eaton/Bussmann FWH-70B (ventilated enclosure not required). Class J, CC, and T fuse maximum 9 A. MCCB maximum 15 A (ventilated enclosure required). Class RK1 or RK5 maximum 9 A (enclosure required). The part number is the cell; it is not read as a 70 A rating. FAZ-C6-3-NA-L is 6 A and 10 kA at 240 VAC, so it is not the branch device. BKN 3P C6A is 6 kA at 400 VAC. No AutomationDirect or LS row in this library was verified for this 480 V application. The sample uses three FWH-70B fuses. Eaton Bussmann data sheet 1200 (BH-0 series; https://www.eaton.com/content/dam/eaton/products/electrical-circuit-protection/fuses/data-sheets/bus-ele-ds-1200-bh-0-series.pdf) lists FWH 70A-100A on block BH-0122, 5/16 stud, 500 V row. Overall length B is 164.08 mm. The plan view shows each base 53.98 mm across the fuse. The front view is 68.20 mm from the mounting face to the top of the stud. The bases have screw slots; the sheet does not show a DIN clip. Note 1 does not give pole-to-pole spacing, so the three outlines are drawn touching, on the backplate, above the Figure 7.5 keep-out. The NEMA tag is the fuse prefix from `ref_prefixes.yaml` (`protection.fuse` is FU).
- TOEPC7106173TA Table 7.9 p.31, model 4005A (IP20/UL Open Type, no built-in EMC filter): 108 x 128 x 154 mm. That matches 4005ABA on SL.GA500.01. Those catalog millimetres are unchanged. Table 7.10 model 4005E (with EMC filter) is a different depth and is not used.
- TOEPC7106173TA Figure 7.5 p.36, installation clearances for one drive: A = 50 mm minimum top and bottom on the front view, B = 30 mm minimum on each side, C = 100 mm minimum above and below on the side view. The layout keep-out is B and C. Figure 7.4 on the same page is orientation only.
- SEL1 is AutomationDirect GCX3300. Cutsheet: 22 mm, 2-position, maintained, (1) N.O., plastic base and bezel, black knob, IP65, UL Listed E189258, CSA 66746. The drawing PDF was not parsed, so width, height, and depth stay blank. https://cdn.automationdirect.com/static/specs/cutsheet/GCX3300_cutsheet.pdf
- ES1 stays generic. The schematic needs 2 NC for Safe Disable H1 and H2. GCX1131 and GCX1136 on https://cdn.automationdirect.com/static/specs/cents22mmmush.pdf are twist-to-release 22 mm operators with (1) N.C. only, so they are not substituted. The BOM says "part not selected". The sheet still says the manual does not call H1/H2 an E-stop.

## Not entered

- Productivity1000 P1-540: the CPU insert PDF under the P1000 user-manual path returned HTTP 404. A different CPU was not substituted.
- G100: no page read for this pass aligned a G100 model, amps, and frame together.
- GA800: the technical-manual dimension tables were not in the documents fetched. SL.GA800.01 is the rating table. Inches on a mechanical page were not converted.
- GA500 2010ABA, 2012ABA, and 2021ABA: SIEPC71061752 Table 10.27 does not separate the depth rows cleanly, so those millimetres stay blank.
- S100 LSLV0075S100-2: section 7.3 leaves that row's dimension cells empty.
- Fuji SC-E height and depth: the drawing PDF was not readable, so only the published width is stored.
- MMS-32H outlines: not on Metasol dimension pages 142-177.
- The carbon Ultimate enclosure page does not print the word Wiegmann. Those rows use the N412 catalog numbers from that page. The stainless page names Wiegmann. The BantamBox page does not print Attabox; those rows use the BantamBox name and the sizes on that PDF.

## Fields

| Part | Field | Value | URL |
| --- | --- | --- | --- |
| AutomationDirect BX-DM1E-18ED13 | description | BRX Do-more PLC, 120-240 VAC, Ethernet, 10 discrete in, 8 sinking out | https://cdn.automationdirect.com/static/specs/cutsheet/BX-DM1E-18ED13_cutsheet.pdf |
| AutomationDirect BX-DM1E-18ED13 | mounting | din | https://cdn.automationdirect.com/static/specs/cutsheet/BX-DM1E-18ED13_cutsheet.pdf |
| AutomationDirect BX-DM1E-18ED13 | width_mm | 119.1 | https://cdn.automationdirect.com/static/drawings/BX-DM1E-18ED13.pdf |
| AutomationDirect BX-DM1E-18ED13 | height_mm | 107.9 | https://cdn.automationdirect.com/static/drawings/BX-DM1E-18ED13.pdf |
| AutomationDirect BX-DM1E-18ED13 | depth_mm | 112.1 | https://cdn.automationdirect.com/static/drawings/BX-DM1E-18ED13.pdf |
| AutomationDirect BX-DM1E-18ED13 | verified | partial | https://cdn.automationdirect.com/static/specs/cutsheet/BX-DM1E-18ED13_cutsheet.pdf |
| AutomationDirect C0-00DD1-D | description | CLICK Basic PLC, 24 VDC, 8 DC inputs, 6 sinking DC outputs, 2 RS-232 | https://cdn.automationdirect.com/static/specs/cutsheet/C0-00DD1-D_cutsheet.pdf |
| AutomationDirect C0-00DD1-D | mounting | din | https://cdn.automationdirect.com/static/specs/cutsheet/C0-00DD1-D_cutsheet.pdf |
| AutomationDirect C0-00DD1-D | width_mm | 53.5 | https://cdn.automationdirect.com/static/drawings/C0-00DD1-D.pdf |
| AutomationDirect C0-00DD1-D | height_mm | 102.1 | https://cdn.automationdirect.com/static/drawings/C0-00DD1-D.pdf |
| AutomationDirect C0-00DD1-D | depth_mm | 75 | https://cdn.automationdirect.com/static/drawings/C0-00DD1-D.pdf |
| AutomationDirect C0-00DD1-D | verified | partial | https://cdn.automationdirect.com/static/specs/cutsheet/C0-00DD1-D_cutsheet.pdf |
| AutomationDirect C0-00DD2-D | description | CLICK Basic PLC, 24 VDC, 8 DC inputs, 6 sourcing DC outputs | https://cdn.automationdirect.com/static/specs/cutsheet/C0-00DD2-D_cutsheet.pdf |
| AutomationDirect C0-00DD2-D | mounting | din | https://cdn.automationdirect.com/static/specs/cutsheet/C0-00DD2-D_cutsheet.pdf |
| AutomationDirect C0-00DD2-D | width_mm | 53.5 | https://cdn.automationdirect.com/static/drawings/C0-00DD2-D.pdf |
| AutomationDirect C0-00DD2-D | height_mm | 102.1 | https://cdn.automationdirect.com/static/drawings/C0-00DD2-D.pdf |
| AutomationDirect C0-00DD2-D | depth_mm | 75 | https://cdn.automationdirect.com/static/drawings/C0-00DD2-D.pdf |
| AutomationDirect C0-00DD2-D | verified | partial | https://cdn.automationdirect.com/static/specs/cutsheet/C0-00DD2-D_cutsheet.pdf |
| AutomationDirect C0-08ND3 | description | CLICK 8-point 12-24 VDC sink/source input module | https://cdn.automationdirect.com/static/specs/cutsheet/C0-08ND3_cutsheet.pdf |
| AutomationDirect C0-08ND3 | mounting | din | https://cdn.automationdirect.com/static/specs/cutsheet/C0-08ND3_cutsheet.pdf |
| AutomationDirect C0-08ND3 | width_mm | 27 | https://cdn.automationdirect.com/static/drawings/C0-08ND3.pdf |
| AutomationDirect C0-08ND3 | height_mm | 100.3 | https://cdn.automationdirect.com/static/drawings/C0-08ND3.pdf |
| AutomationDirect C0-08ND3 | depth_mm | 75 | https://cdn.automationdirect.com/static/drawings/C0-08ND3.pdf |
| AutomationDirect C0-08ND3 | verified | partial | https://cdn.automationdirect.com/static/specs/cutsheet/C0-08ND3_cutsheet.pdf |
| AutomationDirect C0-16TD1 | description | CLICK 16-point 5-27 VDC sinking output module, 0.1 A per point | https://cdn.automationdirect.com/static/specs/cutsheet/C0-16TD1_cutsheet.pdf |
| AutomationDirect C0-16TD1 | amps | 0.1 | https://cdn.automationdirect.com/static/specs/cutsheet/C0-16TD1_cutsheet.pdf |
| AutomationDirect C0-16TD1 | mounting | din | https://cdn.automationdirect.com/static/specs/cutsheet/C0-16TD1_cutsheet.pdf |
| AutomationDirect C0-16TD1 | width_mm | 27 | https://cdn.automationdirect.com/static/drawings/C0-16TD1.pdf |
| AutomationDirect C0-16TD1 | height_mm | 100.3 | https://cdn.automationdirect.com/static/drawings/C0-16TD1.pdf |
| AutomationDirect C0-16TD1 | depth_mm | 75 | https://cdn.automationdirect.com/static/drawings/C0-16TD1.pdf |
| AutomationDirect C0-16TD1 | verified | partial | https://cdn.automationdirect.com/static/specs/cutsheet/C0-16TD1_cutsheet.pdf |
| AutomationDirect DN-T12-A | description | DINnector DN-T12-A screw feed-through terminal, 24-12 AWG, 20 A, 600 V, gray | https://cdn.automationdirect.com/static/specs/cutsheet/DN-T12-A_cutsheet.pdf |
| AutomationDirect DN-T12-A | amps | 20 | https://cdn.automationdirect.com/static/specs/cutsheet/DN-T12-A_cutsheet.pdf |
| AutomationDirect DN-T12-A | sccr | 100kA | https://cdn.automationdirect.com/static/specs/cutsheet/DN-T12-A_cutsheet.pdf |
| AutomationDirect DN-T12-A | mounting | din | https://cdn.automationdirect.com/static/specs/cutsheet/DN-T12-A_cutsheet.pdf |
| AutomationDirect DN-T12-A | verified | partial | https://cdn.automationdirect.com/static/specs/cutsheet/DN-T12-A_cutsheet.pdf |
| AutomationDirect EHCC3DIU | description | Edison modular Class CC fuse holder, indicator, 30 A, 600 V, 3-pole | https://cdn.automationdirect.com/static/specs/cutsheet/EHCC3DIU_cutsheet.pdf |
| AutomationDirect EHCC3DIU | amps | 30 | https://cdn.automationdirect.com/static/specs/cutsheet/EHCC3DIU_cutsheet.pdf |
| AutomationDirect EHCC3DIU | poles | 3 | https://cdn.automationdirect.com/static/specs/cutsheet/EHCC3DIU_cutsheet.pdf |
| AutomationDirect EHCC3DIU | sccr | 200kA | https://cdn.automationdirect.com/static/specs/cutsheet/EHCC3DIU_cutsheet.pdf |
| AutomationDirect EHCC3DIU | mounting | din | https://cdn.automationdirect.com/static/specs/cutsheet/EHCC3DIU_cutsheet.pdf |
| AutomationDirect EHCC3DIU | width_mm | 52.5 | https://cdn.automationdirect.com/static/drawings/EHCC3DIU.pdf |
| AutomationDirect EHCC3DIU | height_mm | 73.9 | https://cdn.automationdirect.com/static/drawings/EHCC3DIU.pdf |
| AutomationDirect EHCC3DIU | depth_mm | 52.8 | https://cdn.automationdirect.com/static/drawings/EHCC3DIU.pdf |
| AutomationDirect EHCC3DIU | verified | partial | https://cdn.automationdirect.com/static/specs/cutsheet/EHCC3DIU_cutsheet.pdf |
| AutomationDirect FAZ-C10-1-SP | description | Eaton FAZ supplementary protector, 10 A, 1-pole, C curve, UL 1077 | https://cdn.automationdirect.com/static/specs/eatonfaz.pdf |
| AutomationDirect FAZ-C10-1-SP | amps | 10 | https://cdn.automationdirect.com/static/specs/eatonfaz.pdf |
| AutomationDirect FAZ-C10-1-SP | poles | 1 | https://cdn.automationdirect.com/static/specs/eatonfaz.pdf |
| AutomationDirect FAZ-C10-1-SP | mounting | din | https://cdn.automationdirect.com/static/specs/eatonfaz.pdf |
| AutomationDirect FAZ-C10-1-SP | verified | partial | https://cdn.automationdirect.com/static/specs/eatonfaz.pdf |
| AutomationDirect FAZ-C10-3-NA-L | description | Eaton FAZ-NA-L miniature breaker, 10 A, 3-pole, C curve, UL 489 | https://cdn.automationdirect.com/static/specs/eatonfazna.pdf |
| AutomationDirect FAZ-C10-3-NA-L | amps | 10 | https://cdn.automationdirect.com/static/specs/eatonfazna.pdf |
| AutomationDirect FAZ-C10-3-NA-L | poles | 3 | https://cdn.automationdirect.com/static/specs/eatonfazna.pdf |
| AutomationDirect FAZ-C10-3-NA-L | mounting | din | https://cdn.automationdirect.com/static/specs/eatonfazna.pdf |
| AutomationDirect FAZ-C10-3-NA-L | verified | partial | https://cdn.automationdirect.com/static/specs/eatonfazna.pdf |
| AutomationDirect FAZ-C15-3-NA-L | description | Eaton FAZ-NA-L miniature breaker, 15 A, 3-pole, C curve, UL 489 | https://cdn.automationdirect.com/static/specs/eatonfazna.pdf |
| AutomationDirect FAZ-C15-3-NA-L | amps | 15 | https://cdn.automationdirect.com/static/specs/eatonfazna.pdf |
| AutomationDirect FAZ-C15-3-NA-L | poles | 3 | https://cdn.automationdirect.com/static/specs/eatonfazna.pdf |
| AutomationDirect FAZ-C15-3-NA-L | mounting | din | https://cdn.automationdirect.com/static/specs/eatonfazna.pdf |
| AutomationDirect FAZ-C15-3-NA-L | verified | partial | https://cdn.automationdirect.com/static/specs/eatonfazna.pdf |
| AutomationDirect FAZ-C20-3-NA-L | description | Eaton FAZ-NA-L miniature breaker, 20 A, 3-pole, C curve, UL 489 | https://cdn.automationdirect.com/static/specs/eatonfazna.pdf |
| AutomationDirect FAZ-C20-3-NA-L | amps | 20 | https://cdn.automationdirect.com/static/specs/eatonfazna.pdf |
| AutomationDirect FAZ-C20-3-NA-L | poles | 3 | https://cdn.automationdirect.com/static/specs/eatonfazna.pdf |
| AutomationDirect FAZ-C20-3-NA-L | mounting | din | https://cdn.automationdirect.com/static/specs/eatonfazna.pdf |
| AutomationDirect FAZ-C20-3-NA-L | verified | partial | https://cdn.automationdirect.com/static/specs/eatonfazna.pdf |
| AutomationDirect FAZ-C6-1-SP | description | Eaton FAZ supplementary protector, 6 A, 1-pole, C curve, UL 1077 | https://cdn.automationdirect.com/static/specs/eatonfaz.pdf |
| AutomationDirect FAZ-C6-1-SP | amps | 6 | https://cdn.automationdirect.com/static/specs/eatonfaz.pdf |
| AutomationDirect FAZ-C6-1-SP | poles | 1 | https://cdn.automationdirect.com/static/specs/eatonfaz.pdf |
| AutomationDirect FAZ-C6-1-SP | mounting | din | https://cdn.automationdirect.com/static/specs/eatonfaz.pdf |
| AutomationDirect FAZ-C6-1-SP | verified | partial | https://cdn.automationdirect.com/static/specs/eatonfaz.pdf |
| AutomationDirect FAZ-C6-3-NA-L | description | Eaton FAZ-NA-L miniature breaker, 6 A, 3-pole, C curve, UL 489, 10 kA at 240 VAC | https://cdn.automationdirect.com/static/specs/cutsheet/FAZ-C6-3-NA-L_cutsheet.pdf |
| AutomationDirect FAZ-C6-3-NA-L | amps | 6 | https://cdn.automationdirect.com/static/specs/cutsheet/FAZ-C6-3-NA-L_cutsheet.pdf |
| AutomationDirect FAZ-C6-3-NA-L | poles | 3 | https://cdn.automationdirect.com/static/specs/cutsheet/FAZ-C6-3-NA-L_cutsheet.pdf |
| AutomationDirect FAZ-C6-3-NA-L | ka | 10 | https://cdn.automationdirect.com/static/specs/cutsheet/FAZ-C6-3-NA-L_cutsheet.pdf |
| AutomationDirect FAZ-C6-3-NA-L | mounting | din | https://cdn.automationdirect.com/static/specs/cutsheet/FAZ-C6-3-NA-L_cutsheet.pdf |
| AutomationDirect FAZ-C6-3-NA-L | width_mm | 53.1 | https://cdn.automationdirect.com/static/drawings/FAZ-C6-3-NA-L.pdf |
| AutomationDirect FAZ-C6-3-NA-L | height_mm | 105 | https://cdn.automationdirect.com/static/drawings/FAZ-C6-3-NA-L.pdf |
| AutomationDirect FAZ-C6-3-NA-L | depth_mm | 60 | https://cdn.automationdirect.com/static/drawings/FAZ-C6-3-NA-L.pdf |
| AutomationDirect FAZ-C6-3-NA-L | verified | partial | https://cdn.automationdirect.com/static/specs/cutsheet/FAZ-C6-3-NA-L_cutsheet.pdf |
| AutomationDirect GCX1100 | description | GCX 22 mm metal momentary pushbutton, black, 30 mm actuator, 1 NO, IP65 | https://cdn.automationdirect.com/static/specs/cents22mmpb.pdf |
| AutomationDirect GCX1100 | contact_config | 1NO | https://cdn.automationdirect.com/static/specs/cents22mmpb.pdf |
| AutomationDirect GCX1100 | mounting | door | https://cdn.automationdirect.com/static/specs/cents22mmpb.pdf |
| AutomationDirect GCX1100 | verified | partial | https://cdn.automationdirect.com/static/specs/cents22mmpb.pdf |
| AutomationDirect GCX1101 | description | GCX 22 mm metal momentary pushbutton, red, 30 mm actuator, 1 NC, IP65 | https://cdn.automationdirect.com/static/specs/cents22mmpb.pdf |
| AutomationDirect GCX1101 | contact_config | 1NC | https://cdn.automationdirect.com/static/specs/cents22mmpb.pdf |
| AutomationDirect GCX1101 | mounting | door | https://cdn.automationdirect.com/static/specs/cents22mmpb.pdf |
| AutomationDirect GCX1101 | verified | partial | https://cdn.automationdirect.com/static/specs/cents22mmpb.pdf |
| AutomationDirect GCX1102 | description | GCX 22 mm metal momentary pushbutton, green, 30 mm actuator, 1 NO, IP65 | https://cdn.automationdirect.com/static/specs/cents22mmpb.pdf |
| AutomationDirect GCX1102 | contact_config | 1NO | https://cdn.automationdirect.com/static/specs/cents22mmpb.pdf |
| AutomationDirect GCX1102 | mounting | door | https://cdn.automationdirect.com/static/specs/cents22mmpb.pdf |
| AutomationDirect GCX1102 | verified | partial | https://cdn.automationdirect.com/static/specs/cents22mmpb.pdf |
| AutomationDirect GCX3300 | description | GCX 22 mm selector, 2-position, maintained, 1 NO, plastic base, black knob, IP65 | https://cdn.automationdirect.com/static/specs/cutsheet/GCX3300_cutsheet.pdf |
| AutomationDirect GCX3300 | contact_config | 1NO | https://cdn.automationdirect.com/static/specs/cutsheet/GCX3300_cutsheet.pdf |
| AutomationDirect GCX3300 | mounting | door | https://cdn.automationdirect.com/static/specs/cutsheet/GCX3300_cutsheet.pdf |
| AutomationDirect GCX3300 | verified | partial | https://cdn.automationdirect.com/static/specs/cutsheet/GCX3300_cutsheet.pdf |
| AutomationDirect GCX1232-24L | description | GCX 22 mm metal LED indicating light, green, 24 VAC/VDC, full voltage, IP65 | https://cdn.automationdirect.com/static/specs/cutsheet/GCX1232-24L_cutsheet.pdf |
| AutomationDirect GCX1232-24L | mounting | door | https://cdn.automationdirect.com/static/specs/cutsheet/GCX1232-24L_cutsheet.pdf |
| AutomationDirect GCX1232-24L | width_mm | 40 | https://cdn.automationdirect.com/static/drawings/GCX1232-24L.pdf |
| AutomationDirect GCX1232-24L | height_mm | 29.8 | https://cdn.automationdirect.com/static/drawings/GCX1232-24L.pdf |
| AutomationDirect GCX1232-24L | depth_mm | 38.8 | https://cdn.automationdirect.com/static/drawings/GCX1232-24L.pdf |
| AutomationDirect GCX1232-24L | verified | partial | https://cdn.automationdirect.com/static/specs/cutsheet/GCX1232-24L_cutsheet.pdf |
| AutomationDirect GH15BN-3-01A | description | GH15 IEC contactor, AC-3 9 A at 440 V, 3 poles, 1 NC aux, 110-120 VAC coil, 45 mm frame | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-01A | amps | 9 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-01A | poles | 3 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-01A | coil_voltage | 110-120VAC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-01A | contact_config | 1NC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-01A | ka | 5 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-01A | sccr | 5kA | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-01A | mounting | din | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-01A | width_mm | 45 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-01A | height_mm | 80 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-01A | depth_mm | 85 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-01A | verified | full | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10A | description | GH15 IEC contactor, AC-3 9 A at 440 V, 3 poles, 1 NO aux, 110-120 VAC coil, 45 mm frame | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10A | amps | 9 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10A | poles | 3 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10A | coil_voltage | 110-120VAC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10A | contact_config | 1NO | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10A | ka | 5 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10A | sccr | 5kA | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10A | mounting | din | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10A | width_mm | 45 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10A | height_mm | 80 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10A | depth_mm | 85 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10A | verified | full | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10B | description | GH15 IEC contactor, AC-3 9 A at 440 V, 3 poles, 1 NO aux, 220-240 VAC coil, 45 mm frame | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10B | amps | 9 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10B | poles | 3 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10B | coil_voltage | 220-240VAC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10B | contact_config | 1NO | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10B | ka | 5 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10B | sccr | 5kA | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10B | mounting | din | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10B | width_mm | 45 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10B | height_mm | 80 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10B | depth_mm | 85 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15BN-3-10B | verified | full | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15CN-3-10A | description | GH15 IEC contactor, AC-3 12 A at 440 V, 3 poles, 1 NO aux, 110-120 VAC coil, 45 mm frame | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15CN-3-10A | amps | 12 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15CN-3-10A | poles | 3 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15CN-3-10A | coil_voltage | 110-120VAC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15CN-3-10A | contact_config | 1NO | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15CN-3-10A | ka | 5 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15CN-3-10A | sccr | 5kA | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15CN-3-10A | mounting | din | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15CN-3-10A | width_mm | 45 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15CN-3-10A | height_mm | 80 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15CN-3-10A | depth_mm | 85 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15CN-3-10A | verified | full | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15DN-3-10A | description | GH15 IEC contactor, AC-3 16 A at 440 V, 3 poles, 1 NO aux, 110-120 VAC coil, 45 mm frame | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15DN-3-10A | amps | 16 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15DN-3-10A | poles | 3 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15DN-3-10A | coil_voltage | 110-120VAC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15DN-3-10A | contact_config | 1NO | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15DN-3-10A | ka | 5 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15DN-3-10A | sccr | 5kA | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15DN-3-10A | mounting | din | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15DN-3-10A | width_mm | 45 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15DN-3-10A | height_mm | 80 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15DN-3-10A | depth_mm | 85 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15DN-3-10A | verified | full | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15ET-3-00A | description | GH15 IEC contactor, AC-3 25 A at 440 V, 3 poles, 110-120 VAC coil, 45 mm frame | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15ET-3-00A | amps | 25 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15ET-3-00A | poles | 3 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15ET-3-00A | coil_voltage | 110-120VAC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15ET-3-00A | ka | 5 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15ET-3-00A | sccr | 5kA | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15ET-3-00A | mounting | din | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15ET-3-00A | width_mm | 45 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15ET-3-00A | height_mm | 80 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15ET-3-00A | depth_mm | 91 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15ET-3-00A | verified | full | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15FT-3-00A | description | GH15 IEC contactor, AC-3 32 A at 440 V, 3 poles, 110-120 VAC coil, 45 mm frame | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15FT-3-00A | amps | 32 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15FT-3-00A | poles | 3 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15FT-3-00A | coil_voltage | 110-120VAC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15FT-3-00A | ka | 5 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15FT-3-00A | sccr | 5kA | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15FT-3-00A | mounting | din | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15FT-3-00A | width_mm | 45 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15FT-3-00A | height_mm | 80 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15FT-3-00A | depth_mm | 91 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15FT-3-00A | verified | full | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15GT-3-00A | description | GH15 IEC contactor, AC-3 40 A at 440 V, 3 poles, 120 VAC 60 Hz coil, 60 mm frame | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15GT-3-00A | amps | 40 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15GT-3-00A | poles | 3 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15GT-3-00A | coil_voltage | 120VAC 60Hz | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15GT-3-00A | ka | 5 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15GT-3-00A | sccr | 5kA | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15GT-3-00A | mounting | din | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15GT-3-00A | width_mm | 60 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15GT-3-00A | height_mm | 114 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15GT-3-00A | depth_mm | 109 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GH15GT-3-00A | verified | full | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect GS21-20P5 | description | DURApulse GS20 200-240 V 1-phase, 0.5 hp, 0.4 kW, CT 2.8 A, VT 3.2 A, frame A, IP20 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS21-20P5 | amps | 2.8 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS21-20P5 | width_mm | 68 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS21-20P5 | height_mm | 128 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS21-20P5 | depth_mm | 125 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS21-20P5 | verified | partial | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-4010 | description | DURApulse GS20 380-480 V 3-phase, 10 hp, 7.5 kW, CT 17 A, VT 20.5 A, frame D, IP20 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-4010 | amps | 17 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-4010 | width_mm | 109 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-4010 | height_mm | 207 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-4010 | depth_mm | 154 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-4010 | verified | partial | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-4015 | description | DURApulse GS20 380-480 V 3-phase, 15 hp, 11 kW, CT 25 A, VT 28 A, frame E, IP20 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-4015 | amps | 25 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-4015 | width_mm | 130 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-4015 | height_mm | 250 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-4015 | depth_mm | 185 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-4015 | verified | partial | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-4020 | description | DURApulse GS20 380-480 V 3-phase, 20 hp, 15 kW, CT 32 A, VT 36 A, frame E, IP20 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-4020 | amps | 32 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-4020 | width_mm | 130 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-4020 | height_mm | 250 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-4020 | depth_mm | 185 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-4020 | verified | partial | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-4030 | description | DURApulse GS20 380-480 V 3-phase, 30 hp, 22 kW, CT 45 A, VT 49 A, frame F, IP20 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-4030 | amps | 45 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-4030 | width_mm | 175 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-4030 | height_mm | 300 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-4030 | depth_mm | 192 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-4030 | verified | partial | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-41P0 | description | DURApulse GS20 380-480 V 3-phase, 1 hp, 0.75 kW, CT 2.7 A, VT 3 A, frame A, IP20 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-41P0 | amps | 2.7 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-41P0 | width_mm | 68 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-41P0 | height_mm | 128 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-41P0 | depth_mm | 143 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-41P0 | verified | partial | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-42P0 | description | DURApulse GS20 380-480 V 3-phase, 2 hp, 1.5 kW, CT 4.2 A, VT 4.6 A, frame B, IP20 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-42P0 | amps | 4.2 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-42P0 | width_mm | 72 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-42P0 | height_mm | 142 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-42P0 | depth_mm | 143 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-42P0 | verified | partial | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-43P0 | description | DURApulse GS20 380-480 V 3-phase, 3 hp, 2.2 kW, CT 5.5 A, VT 6.5 A, frame C, IP20 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-43P0 | amps | 5.5 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-43P0 | width_mm | 87 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-43P0 | height_mm | 157 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-43P0 | depth_mm | 152 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-43P0 | verified | partial | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-45P0 | description | DURApulse GS20 380-480 V 3-phase, 5 hp, 3.7 kW, CT 9 A, VT 10.5 A, frame C, IP20 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-45P0 | amps | 9 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-45P0 | width_mm | 87 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-45P0 | height_mm | 157 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-45P0 | depth_mm | 152 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-45P0 | verified | partial | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-47P5 | description | DURApulse GS20 380-480 V 3-phase, 7.5 hp, 5.5 kW, CT 12 A, VT 15.7 A, frame D, IP20 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-47P5 | amps | 12 | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect GS23-47P5 | width_mm | 109 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-47P5 | height_mm | 207 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-47P5 | depth_mm | 154 | https://cdn.automationdirect.com/static/manuals/gs20m/ch2.pdf |
| AutomationDirect GS23-47P5 | verified | partial | https://cdn.automationdirect.com/static/specs/gs20drives.pdf |
| AutomationDirect PSP24-060S | description | Rhino PSP 24 VDC 2.5 A / 60 W power supply, 85-264 VAC input, IP20, DIN or chassis | https://cdn.automationdirect.com/static/specs/cutsheet/PSP24-060S_cutsheet.pdf |
| AutomationDirect PSP24-060S | amps | 2.5 | https://cdn.automationdirect.com/static/specs/cutsheet/PSP24-060S_cutsheet.pdf |
| AutomationDirect PSP24-060S | mounting | din | https://cdn.automationdirect.com/static/specs/cutsheet/PSP24-060S_cutsheet.pdf |
| AutomationDirect PSP24-060S | width_mm | 45 | https://cdn.automationdirect.com/static/drawings/PSP24-060S.pdf |
| AutomationDirect PSP24-060S | height_mm | 75 | https://cdn.automationdirect.com/static/drawings/PSP24-060S.pdf |
| AutomationDirect PSP24-060S | depth_mm | 100 | https://cdn.automationdirect.com/static/drawings/PSP24-060S.pdf |
| AutomationDirect PSP24-060S | verified | partial | https://cdn.automationdirect.com/static/specs/cutsheet/PSP24-060S_cutsheet.pdf |
| AutomationDirect PSP24-120S | description | Rhino PSP 24 VDC 5 A / 120 W power supply, 85-264 VAC input, IP20, DIN or chassis | https://cdn.automationdirect.com/static/specs/cutsheet/PSP24-120S_cutsheet.pdf |
| AutomationDirect PSP24-120S | amps | 5 | https://cdn.automationdirect.com/static/specs/cutsheet/PSP24-120S_cutsheet.pdf |
| AutomationDirect PSP24-120S | mounting | din | https://cdn.automationdirect.com/static/specs/cutsheet/PSP24-120S_cutsheet.pdf |
| AutomationDirect PSP24-120S | width_mm | 85 | https://cdn.automationdirect.com/static/drawings/PSP24-120S.pdf |
| AutomationDirect PSP24-120S | height_mm | 75 | https://cdn.automationdirect.com/static/drawings/PSP24-120S.pdf |
| AutomationDirect PSP24-120S | depth_mm | 100 | https://cdn.automationdirect.com/static/drawings/PSP24-120S.pdf |
| AutomationDirect PSP24-120S | verified | partial | https://cdn.automationdirect.com/static/specs/cutsheet/PSP24-120S_cutsheet.pdf |
| AutomationDirect RTD32-1400 | description | GH15 RTD32 thermal overload, 10-14 A, class 10A, for GH15CN to GH15FT series contactors | https://www.automationdirect.com/pn/RTD32-1400 |
| AutomationDirect RTD32-1400 | amps | 14 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-1400 | amps_min | 10 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-1400 | amps_max | 14 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-1400 | contact_config | 1NO1NC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-1400 | mounting | plug_on | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-1400 | verified | partial | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-1800 | description | GH15 RTD32 thermal overload, 13-18 A, class 10A, for GH15DN, GH15ET, or GH15FT series contactors | https://www.automationdirect.com/pn/RTD32-1800 |
| AutomationDirect RTD32-1800 | amps | 18 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-1800 | amps_min | 13 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-1800 | amps_max | 18 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-1800 | contact_config | 1NO1NC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-1800 | mounting | plug_on | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-1800 | verified | partial | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-2400 | description | GH15 RTD32 thermal overload, 17-24 A, class 10A, for GH15ET or GH15FT series contactors | https://www.automationdirect.com/pn/RTD32-2400 |
| AutomationDirect RTD32-2400 | amps | 24 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-2400 | amps_min | 17 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-2400 | amps_max | 24 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-2400 | contact_config | 1NO1NC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-2400 | mounting | plug_on | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-2400 | verified | partial | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-3200 | description | GH15 RTD32 thermal overload, class 10A, for GH15ET or GH15FT 45 mm contactors | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-3200 | notes | Product page https://www.automationdirect.com/pn/RTD32-3200 fetched 2026-10-08 lists 23.0-32.0 A adjustable and GH15ET or GH15FT. GH-series PDF prints 22.0 to 32.0 A. Stored amps_min stays 22. | https://www.automationdirect.com/pn/RTD32-3200 |
| AutomationDirect RTD32-3200 | amps | 32 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-3200 | amps_min | 22 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-3200 | amps_max | 32 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-3200 | contact_config | 1NO1NC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-3200 | mounting | plug_on | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-3200 | verified | partial | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-400 | description | GH15 RTD32 thermal overload, 2.7-4.0 A, class 10A, for GH15BN to GH15FT series contactors | https://www.automationdirect.com/pn/RTD32-400 |
| AutomationDirect RTD32-400 | amps | 4 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-400 | amps_min | 2.7 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-400 | amps_max | 4 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-400 | contact_config | 1NO1NC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-400 | mounting | plug_on | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-400 | verified | partial | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-600 | description | GH15 RTD32 thermal overload, 4.0-6.0 A, class 10A, for GH15BN to GH15FT series contactors | https://www.automationdirect.com/pn/RTD32-600 |
| AutomationDirect RTD32-600 | amps | 6 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-600 | amps_min | 4 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-600 | amps_max | 6 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-600 | contact_config | 1NO1NC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-600 | mounting | plug_on | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-600 | verified | partial | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-900 | description | GH15 RTD32 thermal overload, 6.0-9.0 A, class 10A, for GH15BN to GH15FT series contactors | https://www.automationdirect.com/pn/RTD32-900 |
| AutomationDirect RTD32-900 | amps | 9 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-900 | amps_min | 6 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-900 | amps_max | 9 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-900 | contact_config | 1NO1NC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-900 | mounting | plug_on | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD32-900 | verified | partial | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD65-4200 | description | GH15 RTD65 thermal overload, 28-42 A, class 10A, 60 mm | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD65-4200 | amps | 42 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD65-4200 | amps_min | 28 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD65-4200 | amps_max | 42 | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD65-4200 | contact_config | 1NO1NC | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD65-4200 | mounting | plug_on | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect RTD65-4200 | verified | partial | https://cdn.automationdirect.com/static/specs/ghseriescontactors.pdf |
| AutomationDirect SC-E02P-110VAC | description | Fuji Duo SC-E02 contactor, AC-3 9 A / 4 kW at 380-440 V, 3 poles, 110 VAC coil, 43 mm wide | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02P-110VAC | amps | 9 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02P-110VAC | poles | 3 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02P-110VAC | coil_voltage | 110VAC | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02P-110VAC | mounting | din | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02P-110VAC | width_mm | 43 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02P-110VAC | verified | partial | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02P-24VAC | description | Fuji Duo SC-E02 contactor, AC-3 9 A / 4 kW at 380-440 V, 24 VAC coil, 43 mm wide | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02P-24VAC | amps | 9 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02P-24VAC | poles | 3 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02P-24VAC | coil_voltage | 24VAC | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02P-24VAC | mounting | din | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02P-24VAC | width_mm | 43 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02P-24VAC | verified | partial | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02PG-24VDC | description | Fuji Duo SC-E02G contactor, AC-3 9 A / 4 kW at 380-440 V, 24 VDC coil, 43 mm wide | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02PG-24VDC | amps | 9 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02PG-24VDC | poles | 3 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02PG-24VDC | coil_voltage | 24VDC | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02PG-24VDC | mounting | din | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02PG-24VDC | width_mm | 43 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E02PG-24VDC | verified | partial | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E03P-110VAC | description | Fuji Duo SC-E03 contactor, AC-3 12 A / 5.5 kW at 380-440 V, 110 VAC coil, 43 mm wide | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E03P-110VAC | amps | 12 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E03P-110VAC | poles | 3 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E03P-110VAC | coil_voltage | 110VAC | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E03P-110VAC | mounting | din | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E03P-110VAC | width_mm | 43 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E03P-110VAC | verified | partial | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E04P-110VAC | description | Fuji Duo SC-E04 contactor, AC-3 18 A / 7.5 kW at 380-440 V, 110 VAC coil, 43 mm wide | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E04P-110VAC | amps | 18 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E04P-110VAC | poles | 3 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E04P-110VAC | coil_voltage | 110VAC | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E04P-110VAC | mounting | din | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E04P-110VAC | width_mm | 43 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E04P-110VAC | verified | partial | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E05P-110VAC | description | Fuji Duo SC-E05 contactor, AC-3 25 A / 11 kW at 380-440 V, 110 VAC coil, 43 mm wide | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E05P-110VAC | amps | 25 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E05P-110VAC | poles | 3 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E05P-110VAC | coil_voltage | 110VAC | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E05P-110VAC | mounting | din | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E05P-110VAC | width_mm | 43 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E05P-110VAC | verified | partial | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E1-110VAC | description | Fuji Duo SC-E1 contactor, AC-3 32 A / 15 kW at 380-440 V, 110 VAC coil, 54 mm wide | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E1-110VAC | amps | 32 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E1-110VAC | poles | 3 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E1-110VAC | coil_voltage | 110VAC | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E1-110VAC | mounting | din | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E1-110VAC | width_mm | 54 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E1-110VAC | verified | partial | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E2-110VAC | description | Fuji Duo SC-E2 contactor, AC-3 40 A / 18.5 kW at 380-440 V, 110 VAC coil, 54 mm wide | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E2-110VAC | amps | 40 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E2-110VAC | poles | 3 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E2-110VAC | coil_voltage | 110VAC | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E2-110VAC | mounting | din | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E2-110VAC | width_mm | 54 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E2-110VAC | verified | partial | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E3-110VAC | description | Fuji Duo SC-E3 contactor, AC-3 65 A / 30 kW at 380-440 V, 110 VAC coil, 67 mm wide | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E3-110VAC | amps | 65 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E3-110VAC | poles | 3 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E3-110VAC | coil_voltage | 110VAC | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E3-110VAC | mounting | din | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E3-110VAC | width_mm | 67 | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect SC-E3-110VAC | verified | partial | https://cdn.automationdirect.com/static/specs/fujimc9150.pdf |
| AutomationDirect T1-1522G-1 | description | T1 slotted wire duct, gray, 40 x 60 mm, 2 m, cover included | https://cdn.automationdirect.com/static/specs/oldspec/wireductt1_2026_04_24.pdf |
| AutomationDirect T1-1522G-1 | mounting | panel | https://cdn.automationdirect.com/static/specs/oldspec/wireductt1_2026_04_24.pdf |
| AutomationDirect T1-1522G-1 | width_mm | 40 | https://cdn.automationdirect.com/static/specs/oldspec/wireductt1_2026_04_24.pdf |
| AutomationDirect T1-1522G-1 | height_mm | 60 | https://cdn.automationdirect.com/static/specs/oldspec/wireductt1_2026_04_24.pdf |
| AutomationDirect T1-1522G-1 | verified | partial | https://cdn.automationdirect.com/static/specs/oldspec/wireductt1_2026_04_24.pdf |
| AutomationDirect T1-2222G-1 | description | T1 slotted wire duct, gray, 60 x 60 mm, 2 m, cover included | https://cdn.automationdirect.com/static/specs/oldspec/wireductt1_2026_04_24.pdf |
| AutomationDirect T1-2222G-1 | mounting | panel | https://cdn.automationdirect.com/static/specs/oldspec/wireductt1_2026_04_24.pdf |
| AutomationDirect T1-2222G-1 | width_mm | 60 | https://cdn.automationdirect.com/static/specs/oldspec/wireductt1_2026_04_24.pdf |
| AutomationDirect T1-2222G-1 | height_mm | 60 | https://cdn.automationdirect.com/static/specs/oldspec/wireductt1_2026_04_24.pdf |
| AutomationDirect T1-2222G-1 | verified | partial | https://cdn.automationdirect.com/static/specs/oldspec/wireductt1_2026_04_24.pdf |
| LS Electric LSLV0004S100-2 | description | S100 240 V, 0.4 kW, rated current 2.5 A, frame A-1 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0004S100-2 | amps | 2.5 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0004S100-2 | width_mm | 68 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0004S100-2 | height_mm | 128 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0004S100-2 | depth_mm | 123 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0004S100-2 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0004S100-4 | description | S100 480 V, 0.4 kW, rated current 1.25 A, frame A-1 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0004S100-4 | amps | 1.25 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0004S100-4 | width_mm | 68 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0004S100-4 | height_mm | 128 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0004S100-4 | depth_mm | 123 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0004S100-4 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0008S100-2 | description | S100 240 V, 0.8 kW, rated current 5 A, frame A-2 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0008S100-2 | amps | 5 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0008S100-2 | width_mm | 68 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0008S100-2 | height_mm | 128 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0008S100-2 | depth_mm | 128 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0008S100-2 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0008S100-4 | description | S100 480 V, 0.8 kW, rated current 2.5 A, frame A-2 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0008S100-4 | amps | 2.5 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0008S100-4 | width_mm | 68 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0008S100-4 | height_mm | 128 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0008S100-4 | depth_mm | 128 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0008S100-4 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0015S100-2 | description | S100 240 V, 1.5 kW, rated current 8 A, frame B-1 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0015S100-2 | amps | 8 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0015S100-2 | width_mm | 100 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0015S100-2 | height_mm | 128 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0015S100-2 | depth_mm | 130 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0015S100-2 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0015S100-4 | description | S100 480 V, 1.5 kW, rated current 4 A, frame B-1 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0015S100-4 | amps | 4 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0015S100-4 | width_mm | 100 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0015S100-4 | height_mm | 128 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0015S100-4 | depth_mm | 130 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0015S100-4 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0022S100-2 | description | S100 240 V, 2.2 kW, rated current 11 A, frame B-2 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0022S100-2 | amps | 11 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0022S100-2 | width_mm | 100 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0022S100-2 | height_mm | 128 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0022S100-2 | depth_mm | 145 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0022S100-2 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0022S100-4 | description | S100 480 V, 2.2 kW, rated current 5.5 A, frame B-2 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0022S100-4 | amps | 5.5 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0022S100-4 | width_mm | 100 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0022S100-4 | height_mm | 128 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0022S100-4 | depth_mm | 145 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0022S100-4 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0037S100-2 | description | S100 240 V, 3.7 kW, rated current 16 A, frame C | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0037S100-2 | amps | 16 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0037S100-2 | width_mm | 140 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0037S100-2 | height_mm | 128 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0037S100-2 | depth_mm | 145 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0037S100-2 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0037S100-4 | description | S100 480 V, 3.7 kW, rated current 8 A, frame C | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0037S100-4 | amps | 8 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0037S100-4 | width_mm | 140 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0037S100-4 | height_mm | 128 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0037S100-4 | depth_mm | 145 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0037S100-4 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0040S100-2 | description | S100 240 V, 4 kW, rated current 17 A, frame C | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0040S100-2 | amps | 17 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0040S100-2 | width_mm | 140 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0040S100-2 | height_mm | 128 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0040S100-2 | depth_mm | 145 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0040S100-2 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0040S100-4 | description | S100 480 V, 4 kW, rated current 9 A, frame C | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0040S100-4 | amps | 9 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0040S100-4 | width_mm | 140 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0040S100-4 | height_mm | 128 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0040S100-4 | depth_mm | 145 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0040S100-4 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0055S100-2 | description | S100 240 V, 5.5 kW, rated current 24 A, frame D | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0055S100-2 | amps | 24 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0055S100-2 | width_mm | 160 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0055S100-2 | height_mm | 232 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0055S100-2 | depth_mm | 140 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0055S100-2 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0055S100-4 | description | S100 480 V, 5.5 kW, rated current 12 A, frame D | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0055S100-4 | amps | 12 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0055S100-4 | width_mm | 160 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0055S100-4 | height_mm | 232 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0055S100-4 | depth_mm | 140 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0055S100-4 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0075S100-2 | description | S100 240 V, 7.5 kW, rated current 32 A, frame D | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0075S100-2 | amps | 32 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0075S100-2 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0075S100-4 | description | S100 480 V, 7.5 kW, rated current 16 A, frame D | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0075S100-4 | amps | 16 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0075S100-4 | width_mm | 160 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0075S100-4 | height_mm | 232 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0075S100-4 | depth_mm | 140 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0075S100-4 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0110S100-2 | description | S100 240 V, 11 kW, rated current 46 A, frame E | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0110S100-2 | amps | 46 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0110S100-2 | width_mm | 180 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0110S100-2 | height_mm | 290 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0110S100-2 | depth_mm | 163 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0110S100-2 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0110S100-4 | description | S100 480 V, 11 kW, rated current 24 A, frame E | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0110S100-4 | amps | 24 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0110S100-4 | width_mm | 180 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0110S100-4 | height_mm | 290 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0110S100-4 | depth_mm | 163 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0110S100-4 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0150S100-2 | description | S100 240 V, 15 kW, rated current 60 A, frame F | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0150S100-2 | amps | 60 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0150S100-2 | width_mm | 220 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0150S100-2 | height_mm | 350 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0150S100-2 | depth_mm | 187 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0150S100-2 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0150S100-4 | description | S100 480 V, 15 kW, rated current 30 A, frame E | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0150S100-4 | amps | 30 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0150S100-4 | width_mm | 180 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0150S100-4 | height_mm | 290 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0150S100-4 | depth_mm | 163 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0150S100-4 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0185S100-4 | description | S100 480 V, 18.5 kW, rated current 39 A, frame F | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0185S100-4 | amps | 39 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0185S100-4 | width_mm | 220 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0185S100-4 | height_mm | 350 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0185S100-4 | depth_mm | 187 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0185S100-4 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0220S100-4 | description | S100 480 V, 22 kW, rated current 45 A, frame F | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0220S100-4 | amps | 45 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0220S100-4 | width_mm | 220 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0220S100-4 | height_mm | 350 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0220S100-4 | depth_mm | 187 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0220S100-4 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0300S100-4 | description | S100 480 V, 30 kW, rated current 61 A, frame G | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0300S100-4 | amps | 61 | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric LSLV0300S100-4 | width_mm | 275 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0300S100-4 | height_mm | 450 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0300S100-4 | depth_mm | 284 | https://ssq.ls-electric.com/uploads/document/17250045619710/S100_Simple_Manual_English_V3.7_240830.pdf |
| LS Electric LSLV0300S100-4 | verified | partial | https://www.lselectricamerica.com/wp-content/uploads/2021/08/LSISVFDWattLossandEfficiencyBulletinRevF.pdf |
| LS Electric MC-12b | description | Metasol MC-12b contactor, AC-3 12 A / 5.5 kW at 380/440 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-12b | amps | 12 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-12b | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-12b | coil_voltage | 120V 60Hz | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-12b | contact_config | 1NO1NC | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-12b | mounting | din | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-12b | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-12b | height_mm | 73.5 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-12b | depth_mm | 87.4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-12b | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-18b | description | Metasol MC-18b contactor, AC-3 18 A / 7.5 kW at 380/440 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-18b | amps | 18 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-18b | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-18b | coil_voltage | 120V 60Hz | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-18b | contact_config | 1NO1NC | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-18b | mounting | din | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-18b | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-18b | height_mm | 73.5 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-18b | depth_mm | 87.4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-18b | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-22b | description | Metasol MC-22b contactor, AC-3 22 A / 11 kW at 380/440 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-22b | amps | 22 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-22b | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-22b | coil_voltage | 120V 60Hz | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-22b | contact_config | 1NO1NC | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-22b | mounting | din | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-22b | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-22b | height_mm | 73.5 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-22b | depth_mm | 87.4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-22b | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric BKN 3P C6A | description | BKN miniature circuit breaker, 3-pole, C curve, 6 A, 6 kA at 400 VAC | https://www.ls-electric.com/upload/customer/download/1483/MCB_E_180131.pdf |
| LS Electric BKN 3P C6A | amps | 6 | https://www.ls-electric.com/upload/customer/download/1483/MCB_E_180131.pdf |
| LS Electric BKN 3P C6A | poles | 3 | https://www.ls-electric.com/upload/customer/download/1483/MCB_E_180131.pdf |
| LS Electric BKN 3P C6A | ka | 6 | https://www.ls-electric.com/upload/customer/download/1483/MCB_E_180131.pdf |
| LS Electric BKN 3P C6A | mounting | din | https://www.ls-electric.com/upload/customer/download/1483/MCB_E_180131.pdf |
| LS Electric BKN 3P C6A | verified | partial | https://www.ls-electric.com/upload/customer/download/1483/MCB_E_180131.pdf |
| LS Electric MC-9b | description | Metasol MC-9b contactor, AC-3 9 A / 4 kW at 380/440 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-9b | amps | 9 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-9b | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-9b | coil_voltage | 120V 60Hz | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-9b | contact_config | 1NO1NC | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-9b | mounting | din | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-9b | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-9b | height_mm | 73.5 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-9b | depth_mm | 87.4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MC-9b | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.16 | description | Metasol MMS-32H manual motor starter, 0.1-0.16 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.16 | amps | 0.16 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.16 | amps_min | 0.1 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.16 | amps_max | 0.16 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.16 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.16 | ka | 65 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.16 | sccr | 100 kA at 240 V, 65 kA at 480Y/277 V, 25 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.16 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.25 | description | Metasol MMS-32H manual motor starter, 0.16-0.25 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.25 | amps | 0.25 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.25 | amps_min | 0.16 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.25 | amps_max | 0.25 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.25 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.25 | ka | 65 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.25 | sccr | 100 kA at 240 V, 65 kA at 480Y/277 V, 25 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.25 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.4 | description | Metasol MMS-32H manual motor starter, 0.25-0.4 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.4 | amps | 0.4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.4 | amps_min | 0.25 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.4 | amps_max | 0.4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.4 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.4 | ka | 65 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.4 | sccr | 100 kA at 240 V, 65 kA at 480Y/277 V, 25 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.4 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.63 | description | Metasol MMS-32H manual motor starter, 0.4-0.63 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.63 | amps | 0.63 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.63 | amps_min | 0.4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.63 | amps_max | 0.63 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.63 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.63 | ka | 65 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.63 | sccr | 100 kA at 240 V, 65 kA at 480Y/277 V, 25 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-0.63 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1 | description | Metasol MMS-32H manual motor starter, 0.63-1 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1 | amps | 1 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1 | amps_min | 0.63 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1 | amps_max | 1 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1 | ka | 65 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1 | sccr | 100 kA at 240 V, 65 kA at 480Y/277 V, 25 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1.6 | description | Metasol MMS-32H manual motor starter, 1-1.6 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1.6 | amps | 1.6 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1.6 | amps_min | 1 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1.6 | amps_max | 1.6 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1.6 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1.6 | ka | 65 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1.6 | sccr | 100 kA at 240 V, 65 kA at 480Y/277 V, 25 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-1.6 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-10 | description | Metasol MMS-32H manual motor starter, 6-10 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-10 | amps | 10 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-10 | amps_min | 6 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-10 | amps_max | 10 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-10 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-10 | ka | 65 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-10 | sccr | 100 kA at 240 V, 65 kA at 480Y/277 V, 10 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-10 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-13 | description | Metasol MMS-32H manual motor starter, 9-13 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-13 | amps | 13 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-13 | amps_min | 9 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-13 | amps_max | 13 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-13 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-13 | ka | 65 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-13 | sccr | 100 kA at 240 V, 65 kA at 480Y/277 V, 10 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-13 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-17 | description | Metasol MMS-32H manual motor starter, 11-17 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-17 | amps | 17 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-17 | amps_min | 11 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-17 | amps_max | 17 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-17 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-17 | ka | 30 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-17 | sccr | 100 kA at 240 V, 30 kA at 480Y/277 V, 10 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-17 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-2.5 | description | Metasol MMS-32H manual motor starter, 1.6-2.5 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-2.5 | amps | 2.5 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-2.5 | amps_min | 1.6 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-2.5 | amps_max | 2.5 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-2.5 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-2.5 | ka | 65 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-2.5 | sccr | 100 kA at 240 V, 65 kA at 480Y/277 V, 25 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-2.5 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-22 | description | Metasol MMS-32H manual motor starter, 14-22 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-22 | amps | 22 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-22 | amps_min | 14 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-22 | amps_max | 22 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-22 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-22 | ka | 30 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-22 | sccr | 100 kA at 240 V, 30 kA at 480Y/277 V, 10 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-22 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-26 | description | Metasol MMS-32H manual motor starter, 18-26 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-26 | amps | 26 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-26 | amps_min | 18 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-26 | amps_max | 26 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-26 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-26 | ka | 30 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-26 | sccr | 100 kA at 240 V, 30 kA at 480Y/277 V, 10 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-26 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-32 | description | Metasol MMS-32H manual motor starter, 22-32 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-32 | amps | 32 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-32 | amps_min | 22 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-32 | amps_max | 32 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-32 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-32 | ka | 30 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-32 | sccr | 100 kA at 240 V, 30 kA at 480Y/277 V, 10 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-32 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-4 | description | Metasol MMS-32H manual motor starter, 2.5-4 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-4 | amps | 4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-4 | amps_min | 2.5 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-4 | amps_max | 4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-4 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-4 | ka | 65 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-4 | sccr | 100 kA at 240 V, 65 kA at 480Y/277 V, 25 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-4 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-40 | description | Metasol MMS-32H manual motor starter, 28-40 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-40 | amps | 40 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-40 | amps_min | 28 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-40 | amps_max | 40 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-40 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-40 | ka | 30 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-40 | sccr | 100 kA at 240 V, 30 kA at 480Y/277 V, 10 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-40 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-6 | description | Metasol MMS-32H manual motor starter, 4-6 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-6 | amps | 6 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-6 | amps_min | 4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-6 | amps_max | 6 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-6 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-6 | ka | 65 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-6 | sccr | 100 kA at 240 V, 65 kA at 480Y/277 V, 25 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-6 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-8 | description | Metasol MMS-32H manual motor starter, 5-8 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-8 | amps | 8 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-8 | amps_min | 5 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-8 | amps_max | 8 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-8 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-8 | ka | 65 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-8 | sccr | 100 kA at 240 V, 65 kA at 480Y/277 V, 10 kA at 600Y/347 V | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MMS-32H-8 | verified | partial | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.14 | description | Metasol MT-32 differential overload, screw, IP20, 0.1-0.16 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.14 | amps | 0.16 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.14 | amps_min | 0.1 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.14 | amps_max | 0.16 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.14 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.14 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.14 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.14 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.14 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.14 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.21 | description | Metasol MT-32 differential overload, screw, IP20, 0.16-0.25 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.21 | amps | 0.25 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.21 | amps_min | 0.16 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.21 | amps_max | 0.25 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.21 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.21 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.21 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.21 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.21 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.21 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.33 | description | Metasol MT-32 differential overload, screw, IP20, 0.25-0.4 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.33 | amps | 0.4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.33 | amps_min | 0.25 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.33 | amps_max | 0.4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.33 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.33 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.33 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.33 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.33 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.33 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.52 | description | Metasol MT-32 differential overload, screw, IP20, 0.4-0.63 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.52 | amps | 0.63 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.52 | amps_min | 0.4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.52 | amps_max | 0.63 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.52 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.52 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.52 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.52 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.52 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.52 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.82 | description | Metasol MT-32 differential overload, screw, IP20, 0.63-1 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.82 | amps | 1 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.82 | amps_min | 0.63 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.82 | amps_max | 1 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.82 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.82 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.82 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.82 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.82 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K0.82 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K1.3 | description | Metasol MT-32 differential overload, screw, IP20, 1-1.6 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K1.3 | amps | 1.6 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K1.3 | amps_min | 1 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K1.3 | amps_max | 1.6 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K1.3 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K1.3 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K1.3 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K1.3 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K1.3 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K1.3 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K11 | description | Metasol MT-32 differential overload, screw, IP20, 9-13 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K11 | amps | 13 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K11 | amps_min | 9 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K11 | amps_max | 13 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K11 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K11 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K11 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K11 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K11 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K11 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K15 | description | Metasol MT-32 differential overload, screw, IP20, 12-18 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K15 | amps | 18 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K15 | amps_min | 12 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K15 | amps_max | 18 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K15 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K15 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K15 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K15 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K15 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K15 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K19 | description | Metasol MT-32 differential overload, screw, IP20, 16-22 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K19 | amps | 22 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K19 | amps_min | 16 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K19 | amps_max | 22 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K19 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K19 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K19 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K19 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K19 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K19 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K2.1 | description | Metasol MT-32 differential overload, screw, IP20, 1.6-2.5 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K2.1 | amps | 2.5 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K2.1 | amps_min | 1.6 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K2.1 | amps_max | 2.5 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K2.1 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K2.1 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K2.1 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K2.1 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K2.1 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K2.1 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K21.5 | description | Metasol MT-32 differential overload, screw, IP20, 18-25 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K21.5 | amps | 25 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K21.5 | amps_min | 18 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K21.5 | amps_max | 25 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K21.5 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K21.5 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K21.5 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K21.5 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K21.5 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K21.5 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K27 | description | Metasol MT-32 differential overload, screw, IP20, 22-32 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K27 | amps | 32 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K27 | amps_min | 22 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K27 | amps_max | 32 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K27 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K27 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K27 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K27 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K27 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K27 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K3.3 | description | Metasol MT-32 differential overload, screw, IP20, 2.5-4 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K3.3 | amps | 4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K3.3 | amps_min | 2.5 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K3.3 | amps_max | 4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K3.3 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K3.3 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K3.3 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K3.3 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K3.3 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K3.3 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K34 | description | Metasol MT-32 differential overload, screw, IP20, 28-40 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K34 | amps | 40 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K34 | amps_min | 28 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K34 | amps_max | 40 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K34 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K34 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K34 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K34 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K34 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K34 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K5 | description | Metasol MT-32 differential overload, screw, IP20, 4-6 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K5 | amps | 6 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K5 | amps_min | 4 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K5 | amps_max | 6 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K5 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K5 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K5 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K5 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K5 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K5 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K6.5 | description | Metasol MT-32 differential overload, screw, IP20, 5-8 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K6.5 | amps | 8 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K6.5 | amps_min | 5 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K6.5 | amps_max | 8 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K6.5 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K6.5 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K6.5 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K6.5 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K6.5 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K6.5 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K7.5 | description | Metasol MT-32 differential overload, screw, IP20, 6-9 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K7.5 | amps | 9 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K7.5 | amps_min | 6 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K7.5 | amps_max | 9 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K7.5 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K7.5 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K7.5 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K7.5 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K7.5 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K7.5 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K8.5 | description | Metasol MT-32 differential overload, screw, IP20, 7-10 A | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K8.5 | amps | 10 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K8.5 | amps_min | 7 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K8.5 | amps_max | 10 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K8.5 | poles | 3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K8.5 | mounting | plug_on | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K8.5 | width_mm | 45 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K8.5 | height_mm | 74.55 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K8.5 | depth_mm | 86.3 | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric MT-32/3K8.5 | verified | full | https://tbs.ls-electric.com/tbsdata/FileRepository/CTLG/%5BMetasol_MS%5D_EN_C04002-32-202304.pdf |
| LS Electric XBC-DN32H | description | XGB XBC-DN32H, AC 100-240 V supply, DC 24 V input 16 point, transistor output 16 point | https://www.ls-electric.com/upload/customer/download/9f2ada3f-cd60-4e03-8a42-b0abe15fcc09/10310001563_Users%20Manual_XBC-DN32H_V1.0_EN_161212.pdf |
| LS Electric XBC-DN32H | verified | partial | https://www.ls-electric.com/upload/customer/download/9f2ada3f-cd60-4e03-8a42-b0abe15fcc09/10310001563_Users%20Manual_XBC-DN32H_V1.0_EN_161212.pdf |
| LS Electric XBC-DR32H | description | XGB XBC-DR32H, AC 100-240 V supply, DC 24 V input 16 point, relay output 16 point | https://www.ls-electric.com/upload/customer/download/9f2ada3f-cd60-4e03-8a42-b0abe15fcc09/10310001563_Users%20Manual_XBC-DN32H_V1.0_EN_161212.pdf |
| LS Electric XBC-DR32H | verified | partial | https://www.ls-electric.com/upload/customer/download/9f2ada3f-cd60-4e03-8a42-b0abe15fcc09/10310001563_Users%20Manual_XBC-DN32H_V1.0_EN_161212.pdf |
| Yaskawa 2001ABA | description | GA500 240 V three-phase IP20, catalog code 2001ABA under the GA50U column, ND 1/6 HP 1.2 A, HD 1/6 HP 0.8 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2001ABA | amps | 0.8 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2001ABA | width_mm | 68 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2001ABA | height_mm | 128 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2001ABA | depth_mm | 76 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2001ABA | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2002ABA | description | GA500 240 V three-phase IP20, catalog code 2002ABA under the GA50U column, ND 1/4 HP 1.9 A, HD 1/4 HP 1.6 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2002ABA | amps | 1.6 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2002ABA | width_mm | 68 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2002ABA | height_mm | 128 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2002ABA | depth_mm | 76 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2002ABA | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2004ABA | description | GA500 240 V three-phase IP20, catalog code 2004ABA under the GA50U column, ND 3/4 HP 3.5 A, HD 1/2 HP 3 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2004ABA | amps | 3 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2004ABA | width_mm | 68 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2004ABA | height_mm | 128 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2004ABA | depth_mm | 108 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2004ABA | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2006ABA | description | GA500 240 V three-phase IP20, catalog code 2006ABA under the GA50U column, ND 1.5 HP 6 A, HD 1 HP 5 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2006ABA | amps | 5 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2006ABA | width_mm | 68 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2006ABA | height_mm | 128 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2006ABA | depth_mm | 128 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2006ABA | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2010ABA | description | GA500 240 V three-phase IP20, catalog code 2010ABA under the GA50U column, ND 3 HP 9.6 A, HD 2 HP 8 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2010ABA | amps | 8 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2010ABA | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2012ABA | description | GA500 240 V three-phase IP20, catalog code 2012ABA under the GA50U column, ND 4 HP 12.2 A, HD 3 HP 11 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2012ABA | amps | 11 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2012ABA | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2021ABA | description | GA500 240 V three-phase IP20, catalog code 2021ABA under the GA50U column, ND 7.5 HP 21 A, HD 5 HP 17.6 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2021ABA | amps | 17.6 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2021ABA | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2030ABA | description | GA500 240 V three-phase IP20, catalog code 2030ABA under the GA50U column, ND 10 HP 30 A, HD 7.5 HP 25 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2030ABA | amps | 25 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2030ABA | width_mm | 140 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2030ABA | height_mm | 260 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2030ABA | depth_mm | 140 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2030ABA | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2042ABA | description | GA500 240 V three-phase IP20, catalog code 2042ABA under the GA50U column, ND 15 HP 42 A, HD 10 HP 33 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2042ABA | amps | 33 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2042ABA | width_mm | 140 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2042ABA | height_mm | 260 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2042ABA | depth_mm | 140 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2042ABA | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2056ABA | description | GA500 240 V three-phase IP20, catalog code 2056ABA under the GA50U column, ND 20 HP 56 A, HD 15 HP 47 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2056ABA | amps | 47 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2056ABA | width_mm | 180 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2056ABA | height_mm | 300 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2056ABA | depth_mm | 143 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2056ABA | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2070ABA | description | GA500 240 V three-phase IP20, catalog code 2070ABA under the GA50U column, ND 25 HP 70 A, HD 20 HP 60 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2070ABA | amps | 60 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2070ABA | width_mm | 220 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2070ABA | height_mm | 350 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2070ABA | depth_mm | 187 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2070ABA | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2082ABA | description | GA500 240 V three-phase IP20, catalog code 2082ABA under the GA50U column, ND 30 HP 82 A, HD 25 HP 75 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2082ABA | amps | 75 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 2082ABA | width_mm | 220 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2082ABA | height_mm | 350 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2082ABA | depth_mm | 187 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SIEPC71061752&documentName=SIEPC71061752.pdf |
| Yaskawa 2082ABA | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4001ABA | description | GA500 480 V three-phase IP20, catalog code 4001ABA under the GA50U column, ND 1/2 HP 1.2 A, HD 1/2 HP 1.2 A, frame 2.1 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4001ABA | amps | 1.2 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4001ABA | width_mm | 108 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4001ABA | height_mm | 128 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4001ABA | depth_mm | 81 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4001ABA | verified | full | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4002ABA | description | GA500 480 V three-phase IP20, catalog code 4002ABA under the GA50U column, ND 1 HP 2.1 A, HD 3/4 HP 1.8 A, frame 2.2 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4002ABA | amps | 1.8 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4002ABA | width_mm | 108 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4002ABA | height_mm | 128 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4002ABA | depth_mm | 99 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4002ABA | verified | full | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4002ABM | description | GA800 480 V protected chassis, catalog code 4002ABM under the GA80U column, ND 1 HP 2.1 A, HD 0.75 HP 1.8 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4002ABM | amps | 1.8 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4002ABM | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4004ABA | description | GA500 480 V three-phase IP20, catalog code 4004ABA under the GA50U column, ND 2 HP 4.1 A, HD 2 HP 3.4 A, frame 2.4 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4004ABA | amps | 3.4 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4004ABA | width_mm | 108 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4004ABA | height_mm | 128 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4004ABA | depth_mm | 138 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4004ABA | verified | full | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4004ABM | description | GA800 480 V protected chassis, catalog code 4004ABM under the GA80U column, ND 2 HP 4.1 A, HD 1.5 HP 3.4 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4004ABM | amps | 3.4 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4004ABM | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4005ABA | description | GA500 480 V three-phase IP20, catalog code 4005ABA under the GA50U column, ND 3 HP 5.4 A, HD 3 HP 4.8 A, frame 2.5 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4005ABA | amps | 4.8 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4005ABA | width_mm | 108 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4005ABA | height_mm | 128 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4005ABA | depth_mm | 154 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4005ABA | verified | full | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4005ABM | description | GA800 480 V protected chassis, catalog code 4005ABM under the GA80U column, ND 3 HP 5.4 A, HD 2 HP 4.8 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4005ABM | amps | 4.8 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4005ABM | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4007ABA | description | GA500 480 V three-phase IP20, catalog code 4007ABA under the GA50U column, ND 4 HP 7.1 A, HD 3 HP 5.6 A, frame 2.5 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4007ABA | amps | 5.6 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4007ABA | width_mm | 108 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4007ABA | height_mm | 128 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4007ABA | depth_mm | 154 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4007ABA | verified | full | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4007ABM | description | GA800 480 V protected chassis, catalog code 4007ABM under the GA80U column, ND 4 HP 7.1 A, HD 3 HP 5.5 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4007ABM | amps | 5.5 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4007ABM | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4009ABA | description | GA500 480 V three-phase IP20, catalog code 4009ABA under the GA50U column, ND 5 HP 8.9 A, HD 4 HP 7.3 A, frame 2.5 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4009ABA | amps | 7.3 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4009ABA | width_mm | 108 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4009ABA | height_mm | 128 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4009ABA | depth_mm | 154 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4009ABA | verified | full | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4009ABM | description | GA800 480 V protected chassis, catalog code 4009ABM under the GA80U column, ND 5 HP 8.9 A, HD 4 HP 7.2 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4009ABM | amps | 7.2 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4009ABM | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4012ABA | description | GA500 480 V three-phase IP20, catalog code 4012ABA under the GA50U column, ND 7.5 HP 11.9 A, HD 5 HP 9.2 A, frame 3.1 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4012ABA | amps | 9.2 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4012ABA | width_mm | 140 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4012ABA | height_mm | 128 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4012ABA | depth_mm | 143 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4012ABA | verified | full | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4012ABM | description | GA800 480 V protected chassis, catalog code 4012ABM under the GA80U column, ND 7.5 HP 11.9 A, HD 5 HP 9.2 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4012ABM | amps | 9.2 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4012ABM | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4018ABA | description | GA500 480 V three-phase IP20, catalog code 4018ABA under the GA50U column, ND 10 HP 17.5 A, HD 10 HP 14.8 A, frame 5 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4018ABA | amps | 14.8 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4018ABA | width_mm | 140 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4018ABA | height_mm | 260 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4018ABA | depth_mm | 140 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4018ABA | verified | full | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4018ABM | description | GA800 480 V protected chassis, catalog code 4018ABM under the GA80U column, ND 10 HP 17.5 A, HD 7.5 HP 14.8 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4018ABM | amps | 14.8 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4018ABM | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4023ABA | description | GA500 480 V three-phase IP20, catalog code 4023ABA under the GA50U column, ND 15 HP 23.4 A, HD 10 HP 18 A, frame 5 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4023ABA | amps | 18 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4023ABA | width_mm | 140 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4023ABA | height_mm | 260 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4023ABA | depth_mm | 140 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4023ABA | verified | full | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4023ABM | description | GA800 480 V protected chassis, catalog code 4023ABM under the GA80U column, ND 15 HP 23.4 A, HD 10 HP 18 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4023ABM | amps | 18 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4023ABM | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4031ABA | description | GA500 480 V three-phase IP20, catalog code 4031ABA under the GA50U column, ND 20 HP 31 A, HD 15 HP 24 A, frame 6 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4031ABA | amps | 24 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4031ABA | width_mm | 180 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4031ABA | height_mm | 300 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4031ABA | depth_mm | 143 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4031ABA | verified | full | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4031ABM | description | GA800 480 V protected chassis, catalog code 4031ABM under the GA80U column, ND 20 HP 31 A, HD 15 HP 24 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4031ABM | amps | 24 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4031ABM | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4038ABA | description | GA500 480 V three-phase IP20, catalog code 4038ABA under the GA50U column, ND 25 HP 38 A, HD 20 HP 31 A, frame 6 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4038ABA | amps | 31 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4038ABA | width_mm | 180 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4038ABA | height_mm | 300 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4038ABA | depth_mm | 143 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4038ABA | verified | full | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4038ABM | description | GA800 480 V protected chassis, catalog code 4038ABM under the GA80U column, ND 25 HP 38 A, HD 20 HP 31 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4038ABM | amps | 31 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4038ABM | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4044ABA | description | GA500 480 V three-phase IP20, catalog code 4044ABA under the GA50U column, ND 30 HP 44 A, HD 25 HP 39 A, frame 8 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4044ABA | amps | 39 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4044ABA | width_mm | 190 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4044ABA | height_mm | 350 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4044ABA | depth_mm | 204 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4044ABA | verified | full | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4044ABM | description | GA800 480 V protected chassis, catalog code 4044ABM under the GA80U column, ND 30 HP 44 A, HD 25 HP 39 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4044ABM | amps | 39 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4044ABM | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4060ABA | description | GA500 480 V three-phase IP20, catalog code 4060ABA under the GA50U column, ND 40 HP 60 A, HD 30 HP 45 A, frame 8 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4060ABA | amps | 45 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4060ABA | width_mm | 190 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4060ABA | height_mm | 350 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4060ABA | depth_mm | 204 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4060ABA | verified | full | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa 4060ABM | description | GA800 480 V protected chassis, catalog code 4060ABM under the GA80U column, ND 40 HP 59.6 A, HD 30 HP 45 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4060ABM | amps | 45 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4060ABM | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4075ABM | description | GA800 480 V protected chassis, catalog code 4075ABM under the GA80U column, ND 50 HP 74.9 A, HD 40 HP 60 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4075ABM | amps | 60 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4075ABM | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4089ABM | description | GA800 480 V protected chassis, catalog code 4089ABM under the GA80U column, ND 60 HP 89.2 A, HD 50 HP 75 A | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4089ABM | amps | 75 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa 4089ABM | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA800.01&documentName=SL.GA800.01.pdf |
| Yaskawa CIMR-VU2A0004FAA | description | V1000 200-240 V 3-phase, ND 3.5 A 1/2 & 3/4 HP, HD 3 A 1/2 HP, CIMR-VU2A0004FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0004FAA | amps | 3 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0004FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0006FAA | description | V1000 200-240 V 3-phase, ND 6 A 1 HP, HD 5 A 3/4 & 1 HP, CIMR-VU2A0006FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0006FAA | amps | 5 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0006FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0012FAA | description | V1000 200-240 V 3-phase, ND 12 A 3 HP, HD 11 A 3 HP, CIMR-VU2A0012FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0012FAA | amps | 11 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0012FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0020FAA | description | V1000 200-240 V 3-phase, ND 19.6 A 5 HP, HD 17.5 A 5 HP, CIMR-VU2A0020FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0020FAA | amps | 17.5 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0020FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0030FAA | description | V1000 200-240 V 3-phase, ND 30 A 7.5 & 10 HP, HD 25 A 7.5 HP, CIMR-VU2A0030FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0030FAA | amps | 25 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0030FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0040FAA | description | V1000 200-240 V 3-phase, ND 40 A 10 HP, HD 33 A 10 HP, CIMR-VU2A0040FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0040FAA | amps | 33 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0040FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0056FAA | description | V1000 200-240 V 3-phase, ND 56 A 20 HP, HD 47 A 15 HP, CIMR-VU2A0056FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0056FAA | amps | 47 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0056FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0069FAA | description | V1000 200-240 V 3-phase, ND 69 A 25 HP, HD 60 A 20 HP, CIMR-VU2A0069FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0069FAA | amps | 60 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU2A0069FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0001FAA | description | V1000 380-480 V 3-phase, ND 1.2 A 1/2 HP, HD 1.2 A 1/2 HP, CIMR-VU4A0001FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0001FAA | amps | 1.2 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0001FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0002FAA | description | V1000 380-480 V 3-phase, ND 2.1 A 3/4 & 1 HP, HD 1.8 A 3/4 HP, CIMR-VU4A0002FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0002FAA | amps | 1.8 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0002FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0004FAA | description | V1000 380-480 V 3-phase, ND 4.1 A 2 HP, HD 3.4 A 1 & 2 HP, CIMR-VU4A0004FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0004FAA | amps | 3.4 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0004FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0005FAA | description | V1000 380-480 V 3-phase, ND 5.4 A 3 HP, HD 4.8 A 3 HP, CIMR-VU4A0005FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0005FAA | amps | 4.8 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0005FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0007FAA | description | V1000 380-480 V 3-phase, ND 6.9 A 4 HP, HD 5.5 A 3 HP, CIMR-VU4A0007FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0007FAA | amps | 5.5 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0007FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0009FAA | description | V1000 380-480 V 3-phase, ND 8.8 A 5 HP, HD 7.2 A 4 HP, CIMR-VU4A0009FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0009FAA | amps | 7.2 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0009FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0011FAA | description | V1000 380-480 V 3-phase, ND 11.1 A 7.5 HP, HD 9.2 A 5 HP, CIMR-VU4A0011FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0011FAA | amps | 9.2 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0011FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0018FAA | description | V1000 380-480 V 3-phase, ND 17.5 A 10 HP, HD 14.8 A 7.5 & 10 HP, CIMR-VU4A0018FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0018FAA | amps | 14.8 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0018FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0023FAA | description | V1000 380-480 V 3-phase, ND 23 A 15 HP, HD 18 A 10 HP, CIMR-VU4A0023FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0023FAA | amps | 18 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0023FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0031FAA | description | V1000 380-480 V 3-phase, ND 31 A 20 HP, HD 24 A 15 HP, CIMR-VU4A0031FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0031FAA | amps | 24 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0031FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0038FAA | description | V1000 380-480 V 3-phase, ND 38 A 25 HP, HD 31 A 20 HP, CIMR-VU4A0038FAA | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0038FAA | amps | 31 | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa CIMR-VU4A0038FAA | verified | partial | https://www.yaskawa.com/products/drives/industrial-ac-drives/microdrives/v1000-drive/-/content/_6b474b12-3855-47b3-b64a-6bc14f88ac1d_Models38Ratings |
| Yaskawa JVOP-KPLCA04MEB | description | GA500 standard LCD keypad, local/remote, non-Bluetooth, JVOP-KPLCA04MEB | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa JVOP-KPLCA04MEB | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa JVOP-KPLCC04MBB | description | GA500 LCD keypad with Bluetooth, JVOP-KPLCC04MBB | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa JVOP-KPLCC04MBB | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa URS000154 | description | GA500 10% duty braking resistor URS000154, quantity 1 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa URS000154 | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa USR000032 | description | GA500 10% duty braking resistor USR000032, quantity 1 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa USR000032 | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa USR000034 | description | GA500 10% duty braking resistor USR000034, quantity 1 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa USR000034 | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa USR000035 | description | GA500 10% duty braking resistor USR000035, quantity 1 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa USR000035 | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa USR000036 | description | GA500 10% duty braking resistor USR000036, quantity 1 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa USR000036 | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa USR000038 | description | GA500 10% duty braking resistor USR000038, quantity 1 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa USR000038 | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa USR000039 | description | GA500 10% duty braking resistor USR000039, quantity 1 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa USR000039 | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa USR000066 | description | GA500 10% duty braking resistor USR000066, quantity 1 | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
| Yaskawa USR000066 | verified | partial | https://www.yaskawa.com/delegate/getAttachment?cmd=documents&documentId=SL.GA500.01&documentName=SL.GA500.01.pdf |
