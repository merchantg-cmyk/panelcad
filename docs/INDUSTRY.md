# What panel-print software is for (this industry)

Shop prints for industrial controls are not generic CAD. Electricians and panel builders need:

1. **I/O ladders that match the PLC** — field left, card, loads right. Terminals labeled as the OEM prints them (Keyence X0/Y0, SMC OUT0, GC Si/So), not invented numbers.
2. **Off-page arrows** — a wire that leaves the sheet must land on another sheet with a TO / FROM callout (same net).
3. **Wire list + BOM** from the same source as the drawing. New wires get a number as they are drawn. A traced print that left number and gauge blank stays blank until you draw, run `number_wires`, or `set_wire_spec`.
4. **Panel / door layout in millimetres** — DIN rail pack, OEM W×H×D, a photo slot on the footprint. BOM `incabinet` marks what occupies the box; `recommend_cabinet` sizes W×H×D from that list.
5. **Generic schematic symbols** — PB, circle coil, light. OEM photos belong on the layout, not the rung.
6. **Title block + revision** on every page. The drafter draws, the engineer signs.
7. **Cleanup** so symbols do not stack on the card or sit in the title block.

PanelCAD ops that match that: `layout_io`, `cleanup`, `wire`+`to_sheet`, `set_pin_labels`, `set_panel`, `set_picture`, `set_display_size`, `coil_style`.
