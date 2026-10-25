# Panel wire routing, zip-tie bundles, and technician walkthrough

## Sub-features
- Duct-based backpanel wire routing (`route_panel_wires`) and per-wire physical cut length calculation (`cut_length_mm`, `cut_length_in`).
- Interactive Zip-Tie Slice Tool (`✂ Zip-Tie Slice Tool`) to bundle intersected wires into shared `ZipTieWaypoint` trunks.
- Technician Wiring Walkthrough (`walkthrough` mode) stepping one wire route at a time on the backpanel layout with `FROM:` / `TO:` callout badges, directional arrows, and HUD banner.
- Printable `Wire-by-Wire Cut Sheet` (`wire_by_wire` sheet + CSV export).

## Reach via GUI
- `Reports → Number wires`
- `SheetBar` tab `3 - Panel Layout` (`enclosure`)
- `Reports → Wiring Walkthrough & Cut Sheet…` (`wiring-walkthrough-dialog`)
- `wiring-mode-combo`, `wiring-slice-btn`, `wiring-next-btn`, `wiring-prev-btn`, `wiring-installed-check`, `wire-by-wire-table`, `wiring-add-sheet-btn`
