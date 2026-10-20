# AI interface

The GUI is for humans. An AI must **never click the Qt window**. Use one of these instead — they all share `Session`. The **AI** dock shows the model (e.g. Grok 4.6), the page being edited, and each op as it happens.

1. **Python** `from panelcad import Session` — best when you can import the package
2. **JSONL** `panelcad agent` — stdin/stdout, one JSON object per line
3. **Inbox** `panelcad send job.pcad.yaml --hello` then JSONL ops — the open GUI applies them live
4. **CLI** `panelcad inspect --json`, `panelcad describe --json`, `panelcad check --json`, `place`, `wire`, `print`, `bom`
5. **YAML** — the `.pcad.yaml` file is the source of truth
6. **Outside print agent** — `panelcad import prep` / `panelcad import apply`. No built-in model and no API key. See `docs/AGENTS.md`.

## Discover

```
{"id": 0, "op": "help"}
```

```python
s = Session.open("job.pcad.yaml")
print(s.describe())          # every sheet, components, pins, wires, title block, errors
print(s.list_sheets())       # pages in the print set
print(s.inspect_device("adc.click.c0_16nd3"))  # pins before you wire
```

```
panelcad inspect job.pcad.yaml --json
panelcad sheets job.pcad.yaml
panelcad device adc.durapulse.gs21_20p5
```

## Walk a multi-page job

```json
{"id": 1, "op": "open", "path": "examples/dol_starter.pcad.yaml"}
{"id": 2, "op": "list_sheets"}
{"id": 3, "op": "inspect"}
{"id": 4, "op": "set_active_sheet", "sheet_id": "io"}
{"id": 5, "op": "list_components", "sheet": "io"}
{"id": 6, "op": "list_wires", "sheet": "io"}
{"id": 7, "op": "inspect_device", "library_id": "adc.durapulse.gs21_20p5"}
{"id": 8, "op": "place", "library_id": "iec.terminals.x_2p5", "ref": "X20", "x": 50, "y": 180, "sheet": "io"}
{"id": 9, "op": "move", "ref": "X20", "x": 55, "y": 185, "sheet": "io"}
{"id": 10, "op": "set_properties", "ref": "X20", "manufacturer": "Wago", "part_number": "2002-1201"}
{"id": 11, "op": "wire", "from": "U1.DI1", "to": "X20.A", "net": "RUN", "sheet": "io"}
{"id": 12, "op": "set_title_block", "drawing_number": "E-AI-1", "rev": "C", "company": "Acme"}
{"id": 13, "op": "validate"}
{"id": 14, "op": "export_pdf", "path": "job.pdf"}
{"id": 14c, "op": "print_package", "path": "job_package.pdf"}
{"id": 14b, "op": "export_dxf", "path": "job.dxf", "sheet": "power"}
{"id": 15, "op": "save"}
```

`export_pdf` without `sheet` prints **every drawing page**. Title block shows SHEET n / N. `print_package` is the one-click job PDF (cover + index + drawings + reports, including the device index when families exist).

```json
{"id": "x1", "op": "device_index", "ref": "M1"}
{"id": "x2", "op": "lookup_zone", "zone": "1-A3"}
{"id": "x3", "op": "trace_wire", "number": "102"}
```

```python
s.device_index("M1")
s.lookup_zone("1-A3")
s.trace_wire("102")
```

```
panelcad electrical xref job.pcad.yaml --device M1
panelcad electrical xref job.pcad.yaml --zone 1-A3
panelcad electrical xref job.pcad.yaml --wire 102
```

## Ops

| op | view / edit | notes |
|----|-------------|--------|
| hello / say / idle | view | Announce the AI (`model=Grok 4.6`) to the GUI; status line; mark idle |
| update_check | view | Read the release feed (Help > Updates, `PANELCAD_UPDATE_FEED`, or the built-in feed). No download. Args: `feed?`. Off when no feed is set. |
| update_install | edit | Download or copy the setup zip, verify sha256 (refuse on mismatch), and start the Windows quiet upgrade. Requires `confirm=true` on the CLI and JSONL. The open GUI still waits for Install now. Optional `expected_version` and `expected_sha256` refuse the install if the feed changed. `dry_run=true` returns the handoff and does not run the installer. |
| update_rollback | edit | Restore `src`, `scripts`, and top-level packaging files (`pyproject.toml`, `setup.py`, `setup.cfg`, `MANIFEST.in`) from the saved backup and reinstall the venv. Drawings in the program folder stay. Requires `confirm=true` on the CLI and JSONL. The open GUI still waits for Roll back. Optional `dry_run=true`. |
| ui_set_theme / ui_set_canvas / ui_set_phase_colors | view | Window theme `light` / `dark` / `system`, on-screen sheet `paper` / `white` / `dark` / `auto`, and phase colours `on` / `off`. Saved in the shared settings file with autosave and bug reports. Print, PDF, DXF, and SVG stay on white paper with the drawing colors. CLI: `panelcad settings set theme dark`. |
| list_prompts / read_prompts | view | Human text from the AI dock prompt line. `read_prompts` consumes unread lines; then act with Session ops. |
| report_bug | view | Write a bug-report zip. Args: `title`, `description`, `steps?`, `contact?`, `include_project?=false`, `include_screenshot?=false`, `send?=false`. Returns `path`, `sent`, and `send`. `send` posts the description, steps, contact, version, recent actions, traceback, and log tail when Settings, `PANELCAD_BUG_WEBHOOK`, a Slack bot token and channel, or the release's built-in webhook or bot is set. The bot uploads the screenshot and the zip (`files:write`); a release can embed that token, pinned to `#panelcad-bugs`. If that upload fails, the webhook posts the text. `PANELCAD_BUG_REPORTS=off` or `bug_reports.disabled` saves the zip only. A failed op also returns `crash.id` and `crash.path`. |
| help | view | full op list |
| inspect | view | whole project or `sheet` |
| list_sheets | view | |
| list_components | view | includes pin ids |
| list_wires | view | |
| inspect_device | view | library pins before wiring |
| get_title_block | view | |
| library_search | view | |
| parts_search | view | Verified manufacturer catalog parts. Args: `query?`, `manufacturer?`, `category?`, `library_id?`, `amps_min?`, `amps_max?`. Returns catalog number, ratings, `verified`, `source`, footprint, `amps_min`/`amps_max`. |
| assign_part | edit | Assign a verified catalog SKU to a placed device. Keeps `library_id`, tag, and wiring. One undo. Args: `ref`, `catalog`, `manufacturer?`, `sheet?`. Writes manufacturer, catalog, ratings including `amps_min`/`amps_max`, `heat_w`, and footprint (`dim_source=catalog`) onto every sheet that has that tag. When that SKU has a published cutout, the same step copies it onto `properties.cutout`. |
| save_library_device | edit | Save a Symbol Builder block. Writes `library/user.yaml` next to the job (or `~/.panelcad/library`). Never overwrites builtin IEC/NEMA YAML. Args include `block` (`parent`, `child`, `terminal`, `footprint`), `draw`, `attributes` (`tag`, `x`, `y`), `footprint_draw`, `footprint_attributes`, `pins`. Duplicate a builtin with `library_id` + new `id`. |
| validate | view | empty errors = ok. Structural only (duplicate refs, bad pins, overlaps, wire-number clash). |
| design_checks / export_checks | view | Live design findings (severity, sheet, ref, location). Same engine as the GUI Problems dock. Args: `include_suppressed?`, `path?`. Includes `layout_not_ready` (schematic occupant not catalog-ready and not on a layout) and `reconstruct_photo_missing` (photo file not next to the project). |
| check_control_power | view | Loads on each 24 V DC supply or control transformer, totals, margin, inrush, and output protection. Does not invent ratings. Args: `supply?`, `margin?`, `path?` (CSV), `pdf?`. |
| suppress_check / unsuppress_check | edit | Ignore one finding. Stored on `properties.suppressed_checks`. Args: `finding_id` (or `check`+`sheet`+`ref`+`target`). |
| create_project / open / save | edit | |
| add_sheet / set_active_sheet / set_sheet_title / close_sheet / open_sheet / delete_sheet | edit | `kind=panel_layout` creates rails + enclosure. `kind=network` is a communications page. `close_sheet` hides a tab; `open_sheet` brings it back; `delete_sheet` removes the page. |
| place / move / set_properties / set_scale / set_panel / confirm_envelope / delete_component / duplicate_component / rotate_component | edit | On a panel sheet, DIN parts snap to the nearest rail. `set_scale` resizes. `set_panel` writes OEM / measured W/H/D + DIN / in_panel. **Without `confirm`, `set_panel` does not write `dim_source=catalog`** (unconfirmed `measured`). `dim_source=catalog` on the instance does not make a part layout-ready unless `assign_part` wrote `catalog_key`/`catalog_verified` and a published footprint, or the library device itself published catalog W×H. `set_properties` cannot launder `dim_source=catalog`, caller `catalog_key`/`catalog_verified`, a bare `catalog`/`part_number`, or `envelope_confirmed` — catalog readiness goes through `assign_part` only (published W/H/D used). `place` onto a layout ignores caller trust markers. `update_oem` / `apply_oem` require W/H/D and do not mark catalog-ready without a verified SKU. `confirm_envelope` (or `set_panel` `confirm=true`) stores a person-confirmed envelope with `dim_source=measured`. **`confirm=true` requires explicit finite `width_mm`, `height_mm`, `depth_mm` of at least 1 mm, and `mount`.** Do not confirm the current footprint (it may be a generic table fill). NaN, zero, negative, under 1 mm, and sizes larger than the largest catalog enclosure are rejected. **Never invent millimetres.** A backpanel or door place is refused unless the part is layout-ready: a catalog SKU with published body/footprint, or that confirmed envelope. Reconstruct INFO NEEDED placeholders (`reconstruct.needs_info` / `reconstruct_needs_info`) fail the same `is_layout_ready` gate. Generic library symbols stay on the schematic; they are left unplaced on the layout and listed as `layout_not_ready` errors. `rotate_component` is 90° steps. A place onto a door or backplate whose enclosure is drawn below full size stores that sheet's draw scale (the same as a circuit insert) unless `scale` is passed as something other than 1. CLI `place` and JSONL `place` both do this. Expected layout refusals raise `LayoutNotReadyError` and do not write a crash report. |
| place_from_bom | edit | Pull a library part from the BOM onto the active sheet. One catalog object. Same ref across schematic / ladder / I/O / network / panel. Args: `library_id`, `ref?`, `x?`, `y?`, `sheet?`. A part that occupies a backpanel or door and is not layout-ready is not placed. Reply is `placed: false`, `reason: not_layout_ready`, and `message` naming the shop fix. A `layout_not_ready` Problems row is derived at check time (not persisted). |
| insert_circuit | edit | Insert a motor circuit onto a schematic sheet (`dol`, `vfd`, `reversing`, `star_delta`, `soft_starter`, `plc_motor`). Adds only the new in-cabinet devices into free space on an existing `panel_layout` (creates Enclosure if needed) without moving placed parts. **Generic symbols are not packed:** assign `manufacturer` (published SKUs) or `confirm_envelope` first; unready parts stay off the layout and appear in `layout.warnings`. Local operators go on a `door_layout` sheet. New kinds put control on a Motor control sheet. Same tags on schematic, layout, BOM, and electrical reports. One undo removes the whole circuit. Does not replace the project. Symbols follow the sheet `standard` when set, otherwise `title_block.standard` (default **NEMA/ANSI**, same as DESIGN.md / models.py). An explicit `IEC` project keeps `iec.*`. IEC reversing, star-delta, soft starter, and PLC motor control sheets are column diagrams (L+ to 0V, or X1 to X2) with IEC 60617 symbols and contact mirrors. Args: `kind`, `standard` (`IEC` or `NEMA/ANSI`; omit to follow the sheet, then the title block), `manufacturer` (preferred vendor; assigns published catalog SKUs onto the new symbols), `secondary_manufacturer` (fills categories that brand does not make; omit for the enclosure maker when that name is a parts brand, else AutomationDirect; `none` leaves those symbols generic; fallback rows set `catalog_fallback=yes` and `catalog_primary` to the preferred brand), `drive` (`generic_1k5`, `gs20_0p5`, `es1_0p75`), `branch` (`breaker` or `disconnect`), `line_contactor` (VFD), `control` (`none` or `local`), `control_wiring` (`3wire`/`2wire`, soft starter), `bypass` (soft starter), `starter` (`dol`/`vfd`, plc_motor), `timer_seconds` (star-delta on-delay, default 6), `overload_placement` (`windings` 0.58×FLA or `line` 1.0×FLA), `hoa` (PLC HOA selector, default true), `plc_model` (`nema.plc.compact_16dio` / `iec.plc.compact_16dio`), `io_run` / `io_ol` / `io_auto` / `io_out` (default I0_0 / I0_1 / I0_2 / Q0_0), `pilot_lights`, `kw`, `hp`, `voltage`, `control_voltage`, `fla`, `sheet`, `new_sheet`, `x` (40), `y` (22), `shared_supply` (default true: reuse the panel control supply; false adds a supply for this circuit), `shared_estop` (default true: one E-stop for the panel; false adds an E-stop for this motor), `separate_control_rail` (default false: timer, relays, the PSU, and the PLC sit on the right of the power rail when they fit; true puts them on their own DIN rail). `help` returns a structured `schemas.insert_circuit` with kinds and argument descriptions. Result includes `standard`, `layout`, and `layout.warnings`. DOL always includes the contactor and the overload. A VFD does not add an overload between the drive and the motor. Star-delta overload defaults to the windings at 0.58×FLA (NEC 430.250 when FLA is omitted). PLC motor uses an interposing CR and optional HOA. 1-phase drives are wired to L/N (GS20) or AC1/AC2 (ES1), never L1/L2/L3. `kw` of 4 or less selects the 9 A contactor; above 15 kW selects the 100 A MCCB. |
| undo / redo | edit | Undo or redo the last drawing change (place, wire, move, cleanup, …). |
| set_pins_locked / set_pin / reset_pins | edit | Connection points. Locked by default. `set_pin` args: `ref`, `pin_id`, `x`, `y`, `side?`. |
| set_picture / clear_picture | edit | Device photo on panel/door footprints only. Args: `ref`, `path`. |
| apply_cut_sheet_pictures | edit | Match OEM cut-sheet images in a folder to panel/door footprints. Skips print scans. Args: `folder?`. |
| set_display_size | edit | Drawn W×H on panel/door (photo box). Catalog millimetres stay on the BOM. Args: `ref`, `width_mm`, `height_mm`. |
| recommend_cabinet | edit | Cabinet W×H×D from BOM `incabinet=yes`. Args: `sheet?`, `apply?=false`. |
| size_enclosure / recommend_enclosure / apply_enclosure | edit | Shop enclosure auto-size from **catalog or confirmed-envelope** footprints, wire duct, spare space, door devices, and heat. Generic table sizes are ignored (`missing_size` / `layout_not_ready`). `recommend_enclosure` / `size_enclosure` never write. `apply_enclosure` (or `size_enclosure` `apply=true`) updates the box as one undo; `place_parts` lays out only layout-ready parts. Args: `sheet?`, `apply?`, `place_parts?`, `spare_pct?`, `nema?`, `material?`, `ambient_c?`, `max_internal_c?`. |
| compute_heat_load | view | Watt loss, IEC 60890 / Rittal temperature rise, and cooling for the enclosure already on the panel. Does not resize the box. Published `watt_loss` or `heat_w` is labeled Published; everything else is ESTIMATE. Args: `path?` (PDF), `ambient_c?`, `max_internal_c?`, `min_internal_c?`, `min_ambient_c?`, `component_max_c?`, `altitude_m?`, `outdoor?`, `mount?`, `material?`, `nema?`. |
| set_ambient | edit | Store ambient and limits for `compute_heat_load`. Does not change enclosure size. One undo. Args: `ambient_c?`, `max_internal_c?`, `min_internal_c?`, `min_ambient_c?`, `component_max_c?`, `altitude_m?`, `outdoor?`, `mount?`, `material?`, `nema?`, `estimates?` `{ref: watts}`. A per-device user figure is `set_properties` `watt_estimate` (always labeled ESTIMATE). |
| request_oem_update / update_oem | edit | BOM “Update OEM”. Request queues a prompt that requires `width_mm`, `height_mm`, and `depth_mm`. `update_oem` writes unconfirmed measured sizes; a person must `confirm_envelope` or `assign_part` a verified SKU before the part is layout-ready. |
| delete_wire | edit | Args: `wire_id` |
| set_wire_label | edit | Text drawn on the wire. Args: `wire_id` or `from`+`to`, `label`, `net`? |
| add_rail / delete_rail / list_rails / set_enclosure | edit | Panel furniture, millimetres |
| layout_from_schematic | edit | Pack unique **layout-ready** schematic devices onto the active panel sheet. Generic/placeholder parts are left unplaced (`result.unplaced`) and listed in Problems as `layout_not_ready`. Circuit Builder and `layout_from_schematic` skip parts that are not layout-ready (generic symbol sizes). Those refs stay on the schematic. |
| cleanup | edit | **Run after place / layout_io.** Unstacks overlaps, scales I/O field symbols to the rung pitch, keeps devices in the working area. Args: `sheet?` (omit = every sheet). |
| wire | edit | `from` / `to` are `Ref.Pin`. `to_sheet=` another page id lays matching TO/FROM off-page arrows and a local stub on each sheet. |
| set_title_block | edit | company, drawing_number, rev, date, … |
| issue_revision | edit | Snapshot the project into the file, bump the rev, and fill the title-block revision table. Args: `description?`, `rev?`, `scheme?=letter\|number`, `date?`, `drawn?`, `checked?`, `approved?`, `clear_clouds?`, `clouds?=true` |
| list_revisions | view | Issued revisions (oldest first) and clouds. `current` is the rev on every sheet. |
| compare_revisions | view | Diff two revisions or `working`. Devices, wires, terminals, BOM, layout moves. Args: `left`, `right` (aliases `from_rev`, `to_rev`), `path?` (CSV), `pdf?` |
| add_revision_cloud | edit | Manual cloud. Args: `x`, `y`, `w` (or `width`), `h` (or `height`), `sheet?`, `rev?` |
| export_pdf / export_svg / export_dxf / export_bom / import_bom / export_wire_list | print | `export_pdf` = drawing sheets. DXF = one sheet (active unless `sheet=`), R2010 mm. Revision clouds and the title-block rev table are included. `import_bom` reads a parts-list CSV; columns are listed under BOM CSV below. |
| print_package / export_package | print | One customer PDF (cover, index with PAGE links, revisions, drawings, then BOM / wires / from-to / device index / PLC I/O list / terminals / checks / thermal). **Blocked with `NOT FOR BUILD`** while any backpanel/door occupant lacks catalog dimensions or a confirmed envelope (stricter than stamping the sheet). Empty report sections are omitted. `cutouts` is an optional section (off unless named) that adds one to-scale cutout sheet per enclosure surface. Print/PDF is white paper. `export_package` writes the folder plus that PDF as `print.pdf`. Args: `path`, `paper?=mixed`, `sections?` (`from_to` is the conductor report, `xref` / `device_index` is the cross-reference list, `plc_io` is the I/O list, `thermal` is the enclosure thermal report, `power` and `cutouts` are opt-in). |
| cutouts_generate | view/edit | Cutout and drill sheets for the door, backplate, and walls. Args: `datum?` (`top_left` default), `style?=ordinate\|baseline`, `view?=front\|back`, `units?=mm\|inch`, `edge_clearance_mm?`, `flange_mm?`, `panel_thickness_mm?`, `surfaces?`, `persist?=false`. `persist` stores those settings on project properties (one undo). Does not invent a hole. |
| cutouts_export_dxf | print | 1:1 shop DXF per surface that has openings. Layers `CUT`, `DRILL`, `DIM`, `TEXT`, `OUTLINE`. Args: `path`, `units?=mm\|inch`, `view?`, `datum?`, `style?`, `surface?` or `surfaces?`. Origin is the bottom-left of the viewed panel, Y up. Hole-table X/Y stay in the datum (Y down). Skipped faces are listed. Unknown openings are text only. |
| cutouts_export_pdf | print | 1:1 cutout PDF for the same sheets. Args: `path`, `units?`, `view?`, `datum?`, `style?`, `surface?` or `surfaces?`. This is the sheet PDF, not the print-package page. |
| part_set_cutout | edit | Set or clear one device cutout. Args: `ref`, `cutout?` (`shape` round, rect, or d, plus holes, `corner_radius_mm`, `clearance_mm`, `depth_behind_mm`, source), `unknown?=true` (`false` restores the opening, key, and centre minimums; JSON strings `true` and `false` are booleans), `clear?=true`, `surface?` (`door`, `backplate`, `left`, `right`, `top`, `bottom`), `include_key?`, `min_center_x_mm?`, `min_center_y_mm?`, `centers_interchangeable?`, `sheet?`. One undo. `clear` removes the override so the library record applies. The inspector edits the same fields, including drills, corner radius, clearance, and depth. CLI: `panelcad cutouts set --min-center-x --min-center-y --pitch-swap` (`--centers-interchangeable` is the same flag; passing both with different values is an error). JSONL accepts `pitch_swap` the same way. |
| number_wires / set_wire_spec / set_net_wire_spec / set_wire_numbering / wire_numbering / wire_list | edit | Wires get a number as they are drawn. Default scheme is NEMA sheet/line (`101`). `number_wires` is a full renumber with undo; locked and conventional names stay. `set_wire_spec` with `number` locks it. `size` / `color` / `type` on the wire, or `set_net_wire_spec` (`net`, including `control`, `power`, and `*`; `all` is stored as `*`) when the wire field is blank. The from-to CSV and print page show `TBD` when the gauge is still blank after the wire, that net, `control` or `power`, and `*`. They do not fill a default size. Color and type still fall back (control BU MTW, power BK MTW). Rails are daisy-chained between `tag:pin` / `strip:terminal` ends. One wire marker per conductor end. `wire_list` without `from_to` keeps the legacy device/terminal rows and still returns `from_to`. `from_to=true` (or `report=from_to`) makes `rows` the conductor report and `path` writes that CSV. Sort `number` or `device`. Columns: `wire_number`, `from` (`tag:pin` or `strip:terminal`), `to`, sheet/line/column at each end, `size`, `color`, `type`, `crossing`. |
| export_wire_markers | print | One marker per wire end. Args: `path` (required), `target?=generic\|phoenix\|weidmueller\|brady`, `style?=heat-shrink\|clip-on`, `sort?=number\|device`. Phoenix is one column, no header. Weidmüller and Brady are the generic CSV (`confidence=unverified`). `help` returns `schemas.wire_list` and `schemas.export_wire_markers`. See `docs/WIRE_MARKERS.md`. |
| quote_build / quote_set_rates / quote_import_prices / quote_export | view/edit | Quote from the drawing. Prices only from an imported CSV/XLSX, a library `unit_cost`, or `manual` on `quote_set_rates`. Nothing is downloaded or sent. `quote_export` writes xlsx, pdf, csv, or json and is **blocked (`NOT FOR BUILD`)** while any layout occupant is still generic. `view=customer` omits cost, markup, vendor, and shop rate in every format. Result includes `incomplete` and `unpriced`. Labor placeholders are labelled example defaults only while they are still the shipped numbers. |
| phase_colors / set_phase_colors | view/edit | 3-phase power colors. Black/red/blue (208/240 V) and brown/orange/yellow (480 V) are a US shop convention. The 480 V palette is chosen when `system_voltage` (or the drawing) is at or above `nema_high_voltage` (default 300). Brown, black, and grey are a permitted IEC default, not a required L1/L2/L3 order. `nema_low`, `nema_high`, and `iec` are `L1,L2,L3`. `neutral` is the NEMA white/grey. `iec_neutral` is blue (default light blue #5eb0e8). IEC 60445 §6.2.2 is unverified from an official source. Ground is green (IEC green/yellow). A blue IEC line color warns when the drawing has a neutral. Control wiring is unchanged. Omit `system_voltage` or pass `auto` to follow the drawing. |
| retag | edit | `mode=duplicates` (default), `family`, or `standard`. `standard` (or `apply_prefixes=true`) remaps tags to the project's drawing-standard prefixes. A contact keeps its coil's tag. |
| cross_reference / export_cross_reference / set_cross_reference | view/edit | Derived coil/contact/device report (sheet, line, zone). `write?=false` persist `properties.xref`. `nc_mark?=underline\|bar\|parentheses`. CSV lists each coil and its contacts. Opening a file never writes refs. |
| circuit_for / checkpoints / upstream / downstream / plc_point / likely_causes | view | Read-only troubleshooting. Same payload from Session, JSONL, `panelcad electrical troubleshoot`, and the drawing agent. `circuit_for` `ref` is the control and power path. `checkpoints` `ref` + `symptom` (`motor_wont_start`, `motor_trips`, `wont_stop`, `no_control_power`, `plc_input_not_seen`, `plc_output_on_load_off`, `vfd_faulted`) returns ordered checks, each `LIVE` or `DE-ENERGIZED`, and opens with lockout and NFPA 70E text. Expected voltage comes only from a net name, a rail name, or a supply output on that net; otherwise the check is `needs-info`. `upstream` and `downstream` take `endpoint` as `ref.pin`. `plc_point` takes `address` (address, point label, or tag on the drawing). `likely_causes` takes `ref`, `symptom`, and optional `readings` (`ok` / `wrong` / `skipped`); the first wrong reading narrows the fault. Answers cite only wire numbers and terminals on the drawing. Nothing in the payload tells the reader to jumper, bypass, force, defeat, or override a safety device, overload, or PLC point. `export_troubleshoot` `path` writes the open `troubleshoot.json` document (`docs/TROUBLESHOOT.md`). `append_troubleshoot_log` appends readings to `<stem>.troubleshoot-log.json` next to the saved project and does not write the project file. Unknown devices, symptoms, and PLC points are a query error (exit 1 on the CLI, no crash record). |
| device_index / lookup_zone / trace_wire | view | Floor-troubleshooting index. `device_index` `ref?` lists every element of a device (page, zone, element type, terminals, wire numbers). Exact tag match (M1 does not return M10). A lamp tag returns the lamp, not an off-page whose dest starts with that tag. `lookup_zone` `zone` (`5-A2`, `A2`, `/1.3`, `105`, `sheet:A2`) plus optional `sheet` returns what sits there. Bad or off-grid zones raise the same sentence on Session, JSONL, CLI (exit 1), and the drawing tool — not a crash record. `trace_wire` `number` follows a conductor across sheets (from/to, pages, pin zones); empty is `wire number is required`, unknown numbers error the same way. Same payload from Session, `panelcad electrical xref --device/--zone/--wire`, and JSONL. The print package includes a Device Index appendix when the project has families. |
| terminal_strips / sequence_terminals / jumper_terminals / clear_terminal_jumpers | edit | Strip table in schematic order. `sequence_terminals` args: `strip`, `rename?`. Levels follow schematic position. `rename` retags STRIP-n. `jumper_terminals` args: `refs[]`, `group?` — marks a consecutive span and derives a catalog jumper (family + pitch + poles) for the BOM. `clear_terminal_jumpers` args: `refs[]?` or `group?`. |
| open_terminal_strip / list_terminal_strips | edit | Open the to-scale DIN-rail drawing (end hardware on the panel rail if missing). Long strips continue on the next row or sheet. List returns `strip`, `terminals`, `sheet`. |
| add_terminal / reorder_terminals / move_terminal / retag_terminal_strip / add_strip_accessory | edit | Same component as the schematic. Reorder moves x/y into the new order and retags STRIP-n. Retag uses that same schematic order. Accessory `kind` is `end_plate`, `end_stop`, or `partition` (placed on the panel rail). |
| delete_strip_component / export_terminal_strip | edit | Delete removes the tag from the schematic and the panel. One undo. Export args: `path`, `strip`, `vendor?=phoenix\|clipx\|wago\|weidmueller`, `format?=csv\|xml`. Phoenix PROJECT complete and WAGO default to comma CSV. `clipx` or a `.xml` path writes interchange XML (vendor schema unpublished — import unconfirmed). Weidmüller WMC is the documented semicolon CSV `article;designation;marker`. Result includes `warnings` for unmapped articles. |
| audit_panel / export_audit | view | Schematic device with no panel or door footprint, and the reverse. Full live checks are `design_checks`. |
| io_list / plc_io_list / import_io / export_io | edit | PLC I/O list from the drawing (address, type, tag, description, device, spare). Import CSV columns: `module`, `address`, `description`, `type`, `wire`. On a generated I/O module, `tag` and `field` are kept and the platform address in `pin_labels` is left alone. |
| io_add_module / io_assign_point / io_generate_sheets / rename_component | edit | Place a verified PLC I/O module, assign a point (tag, description, field device), and draw ladder I/O sheets. Unverified modules are refused. Module tags are IO1, IO2, … across the project. `fuses` defaults to the project setting. I/O CSV `field` round-trips the symbol. `rename_component` renames a device and copies a field tag back onto the I/O list. `help` returns the I/O schemas. CLI: `panelcad io modules` and `panelcad io generate` (`--fuses` / `--no-fuses` omitted keeps the project setting). |
| assign_item_numbers | edit | BOM item on each device, drawn on the panel footprint. |
| cable_schedule / propose_cables / add_cable / update_cable / assign_cable_conductors / export_cable_schedule | edit | Field cables from a panel terminal strip, or from a VFD's output terminals, to a field device. `propose_cables` groups them (spares?, replace?, show_tags?, route?). `add_cable` / `update_cable` take `from_device` and `shield_bond` (`both`, `panel`, `field`, `none`). `export_cable_schedule` writes CSV, or PDF when `path` ends in `.pdf`; optional `pull` and `terminations` CSVs. The gland cell is `size per cable OD` unless a gland was entered or the library part carries an OD and an http source. Conductor colors print `per cable` unless entered or named by that library part (`colors: 1=BLK, G=GRN`). The ground role prints `GND`. Part numbers stay blank unless entered or taken from the library with a source URL. `panel_wire_annotation` is wire number and gauge at each footprint. |
| export_plc_tags | print | PLC tag file. Args: `path`, `target=studio5000\|tia\|tia_csv\|domore\|keyence\|sysmac\|cxprogrammer`, `prefix?`, `name_from?=device\|description`, `logix_style?=alias\|base`, `logix_slot?=1`, `analog_start?=byte or auto`. `device` uses the I/O tag (parent plus contact function, such as `M1_RUN_FB`). `tia` is the Excel tag table. `tia_csv`, `keyence`, and `sysmac` are labeled generic CSVs. `cxprogrammer` is the CX-Programmer symbol-table paste file. `analog_start` is the TIA `%IW`/`%QW` byte when the module has no `start_address`; omit it to use project property `analog_start`. `help` schemas describe each argument. |
| reconstruct_add_photos / reconstruct_tag_photo / reconstruct_list_photos | edit/view | Phone photos of a **physical panel** (backpanel, door, nameplate, part label, terminal strip, wire numbers). Not drawings. Files are copied into `photos/` next to the project. Args: `paths[]` or `path`, `role?`, `note?`. |
| reconstruct_set_corners / reconstruct_set_scale / reconstruct_clear_scale | edit | Perspective corners (four photo-pixel points) plus two independent known lengths, or the enclosure face width **and** height. `known_height_mm` applies only to `kind=face` (it does not invent a face from two lengths). A single length does not publish millimetres. A later length with the same kind and direction replaces that slot; pass `slot` (`1`/`2`) or `replace` (`scale`/`scale_b`) to overwrite a mark. A length mark never silently drops a face scale — the face stays and the reply `message` says so; call `reconstruct_clear_scale` first to replace the face. Returns `ok` with `scaled: false` and `error_mm` when the residual is above 8 mm (does not raise). Args: `photo_id`, `corners?`, `kind`, `p1`, `p2`, `known_mm?`, `known_height_mm?`, `catalog?`, `slot?`, `replace?`. |
| reconstruct_identify | edit | Read nameplates and wire numbers via the local vision backend (offline stub, test fake, or Ollama). PR #28 print-import vision plugs in later. Args: `photo_id?`. |
| reconstruct_confirm_item / reconstruct_place_confirmed / reconstruct_place_needs_info | edit | A part is layout-ready only with a catalog match that has W×H×D **or** a tech-confirmed envelope (`envelope_w_mm`, `envelope_h_mm`, `envelope_d_mm`, `mount`, `library_id`). Unidentified photo parts become reconstruct-only `needs_info` / INFO NEEDED markers — never generic bodies. Confirm converts the placeholder in place. Print package is marked NOT FOR BUILD while any remain. Args: `item_id`, `catalog?`, `envelope_w_mm?`, `envelope_h_mm?`, `envelope_d_mm?`, `mount?`, `library_id?`, `tag?`. |
| reconstruct_checklist / reconstruct_next_question / reconstruct_answer / reconstruct_confirm_connection | view/edit | Floor checklist: “trace wire 104 from K1:A1 — where does it land?”. Typed landing (`K1:A1`) or a new photo. Inferred wires are dashed with a `?` caption and a Problems row until confirmed. |
| reconstruct_attach_print / reconstruct_compare_print | edit | Secondary old print (`.pcad.yaml`, print-index YAML, PDF/scan). Photos stay authoritative. Reuses the print-import pipeline when that branch is merged. Flags missing tags, catalog mismatches, and unread wire numbers. |
| reconstruct_photo_region / reconstruct_unconfirmed / reconstruct_status | view | Photo crop for an item or placed ref; list items that must not go on the layout; full reconstruct snapshot. `describe()` includes `reconstruct` when present. CLI: `panelcad reconstruct`. |

## BOM CSV

`import_bom` (File > Import, or BOM dock > Import CSV…) accepts these columns. Header matching ignores case, `#`, `.`, and trailing punctuation.

| Column | Aliases |
| --- | --- |
| Tag/Ref | Tag, Tags, Reference, Designation |
| Part Number | part #, part, item number, model, model number, manufacturer part number, mpn, pn, part no |
| Manufacturer | mfr, mfg, vendor, brand, make |
| Description | name, desc |
| Qty | quantity, count, quantity required, qty req |
| library_id | library, type, symbol, component (optional) |
| catalog | catalogue, catalog number, cat no, sku, order number |

A row that already has a tag updates that part. A row with `library_id` places that symbol. A row with a part number, catalog number, or description and no tag or library id is stored as a needs-info part (`reconstruct.needs_info`): no millimetres, not a generic footprint, kept off the BOM, the quote total, and cutouts. The print is marked NOT FOR BUILD while any remain. The import does not invent a library id.

A CSV with none of those columns is refused. The message names the headers that were found and the headers that were expected.

## PLC tag file formats

Studio 5000 CSV tag import follows the Logix Designer CSV file-format page (v37):

https://www.rockwellautomation.com/en-us/docs/studio-5000-logix-designer/37-00/contents-ditamap/studio-5000-logix-designer/import-and-export/csv-file-format.html

> The first line in the file contains a version number, followed by a set of component blocks.
>
> Any line whose first column is "Type" is interpreted as a schema line.
>
> Any line whose first column is Remark is ignored by the import process.

Line 1 is `0.3`. The next line is `Remark,"CSV-Import-Export"` (the spelling on that page). The schema line is `TYPE,SCOPE,NAME,DESCRIPTION,DATATYPE,SPECIFIER,ATTRIBUTES`. 1756-RM014 says comments start "with REMARK and a comma"; the CSV file-format page is the one that fixes the order and the `Remark` spelling. Alias specifiers are `Local:slot:I.Ptxx.Data` / `Local:slot:O.Ptxx.Data` (5069-UM004H pp. 121, 126, 129), and analog channels are `Local:slot:I.Chxx.Data` / `Local:slot:O.Chxx.Data` (5069-UM005F p. 139).

TIA Portal `.xlsx` follows "Format of the export file (*.xlsx)":

https://docs.tia.siemens.cloud/r/en-us/v21/declaring-plc-tags/editing-plc-tag-tables/exporting-and-importing-plc-tags/format-of-the-export-file-.xlsx

> The export file contains two sheets:
>
> The name of the first sheet is "PLCTags".
>
> The name of the second sheet is "TagTableProperties".
>
> The column titles are predefined and are always expected to be in English.

PLCTags columns, in the order that page lists them: Name, Path, Data Type, Logical Address, Comment, Hmi Visible, Hmi Accessible, Hmi Writeable, Typeobject ID, Version ID. TagTableProperties columns: Path, BelongsToUnit, Accessibility. The importing page says to set custom property `TIA_Version` to `"1.0"` when that property is missing. `tia_csv` is a separate labeled generic CSV and is not that workbook.

`%IW0` is IB0+IB1, so analog words do not share a byte with `%I0.0`–`%I0.7` (the same split on the outputs). The number in `%IWn` is the starting byte and stays even. A part's `start_address` property is used when the library has one. Otherwise `analog_start` on the project, or the export dialog, sets that byte. Otherwise the next free even word begins after the highest digital byte. Do-more `X`/`Y` and `WX`/`WY` are separate blocks, and Studio 5000 addresses are per slot (`I.Ptxx.Data` or `I.Chxx.Data`).

A contact's I/O row names the parent device (`M1`, `OL1`, `SEL1`). The default tag is that parent plus the contact role or HOA position (`M1_RUN_FB`, `OL1_OK` for an NC overload, `SEL1_AUTO`). `name_from=description` still uses the I/O description. Names stay unique and match each target: Logix 40 characters, a letter or underscore, then letters, digits, and underscores; Do-more nicknames are 1–16 and must not equal an element id.

Do-more element documentation (no header; Element, Nickname, Extra Info, Description):

https://hosteng.com/dmdhelp/content/Import_Export/Import_Element_Documentation.htm

CX-Programmer symbol paste is tabbed text with no header. W446, Importing and Exporting Symbols:

https://files.omron.eu/downloads/latest/manual/en/w446_cx-programmer_operation_manual_en.pdf

> The information is in a tabbed format of text.
>
> For supplying symbols complete with data-type, use format of 4 columns, which consists of <Symbol Name> tab <Symbol Data-type> tab <Symbol Address> tab <Comment>.

The example on that page is `Name BOOL 1.1 this is a comment` (without the quotes). "Each item should be on a new line." "Each symbol name must be unique within its table." Bit addresses use the standard form: entering `10` is read as `0.10`, and bit zero of channel 50 is `50.00`. "Contact inputs will be incremented from CIO 0.00 (word CIO 0) and contact outputs will be incremented from CIO 100.00 (word CIO 100)." W446 does not name a file encoding; the paste file is UTF-8. A module with both an analog input and an analog output is one CJ1W-MAD42 Special I/O unit. W345 section 9-5-6, page 398, Allocations for Normal Mode: `n = CIO 2000 + (unit number × 10)`, `n+1` is "Output 1 set value", and `n+5` is "Input 1 conversion value" (16-bit binary, `UINT`). Page 401 stores input conversion values in `n+5` to `n+8`. Page 410 writes output set values to `n+1` and `n+2`; the unit-0 example uses CIO 2001 for output 1 and the input example uses CIO 2005 for input 1. A channel outside that table is the address `unverified`. An input-only module still uses the AD map (`n+1` is input 1) and an output-only module uses the DA map (`n+1` is output 1). DM words in that manual are the unit's settings, not the conversion value.

https://edata.omron.com.au/eData/PLCs/CJ1/W345-E1-12.pdf

`keyence` is a labeled generic CSV. KV STUDIO Ver.11 User's Manual says to read "a CSV format file (MBCS) (*.csv) or TXT format file (Unicode) (*.txt)" and does not list the columns. The manuals index does not publish that PDF without an account: https://www.keyence.com/support/user/controls/plc/manual/ . Device numbers in the generic file use the Unit Editor default from the same manual: "relays are assigned in 10-channel units from the leading number of 30000 and DM entries are assigned in 100-entry units from the leading number of 10000." `R10000` is channel 100. KV-8000 specs name relay `R` (32000 points), `MR` (64000 points), and `DM` (65535 words) and do not list a buffer-memory map: https://www.keyence.com/products/controls/plc-building-block-type/kv-8000/specs/ . The KV Nano unit-number formula in that section is not applied, because the manual does not state the first unit number.

`sysmac` is a labeled generic CSV. W504 does not list a global-variable import file. Device-variable AT is filled by the I/O Map and cannot be edited; CJ-series memory AT on that page is `%0` to `%6143` and `%D0` to `%D32767` (words, not bits). Names follow W501 6-3-12, page 6-84, under "Characters that cannot be used together": no leading underscore, no trailing underscore, no consecutive underscores (`__`), and no `P_` prefix. Separate underscores stay, so `M1_RUN_FB` is unchanged. Names are not case sensitive, at most 127 bytes, and unique among global variables in the CPU Unit.

https://edata.omron.com.au/eData/NJ/W504-E1-35.pdf

## Quote

`quote_build` prices the grouped BOM and estimates labor from the drawing (devices by category, wires and both ends, one labor line for each field cable schedule row plus each network or serial link, terminal blocks, duct and DIN rail length, door devices, PLC I/O points from the I/O list, and fixed engineering / programming / testing / documentation lines). Schedule conductors and those network or serial links are not also counted as wires or as two terminations. Cutouts are not counted yet; the internal line says to enter minutes per cutout. `set_cutout_counter` supplies that count when a drawing feature is connected. A line with no price is flagged and left out of the parts total. The total label is `INCOMPLETE – N of M lines unpriced, not included` while any line has no price. Each labor line is priced from its minutes times the shop rate divided by 60, rounded half up to the cent. Hours shown are minutes divided by 60 to 3 decimal places and are not used to price the line. Extended sell is extended cost times (1 + markup), rounded half up to the cent. The age of each price is reported. `quote_set_rates` takes `project` as a header field. `quote_import_prices` reports `encoding` (`utf-8`, `cp1252`, or `xlsx`).

```json
{"id": 1, "op": "quote_import_prices", "path": "prices.csv", "price_mode": "cheapest"}
{"id": 2, "op": "quote_set_rates", "number": "Q-100", "project": "West pump", "parts_markup_percent": 20, "labor_markup_percent": 0}
{"id": 3, "op": "quote_build"}
{"id": 4, "op": "quote_export", "path": "quote.pdf", "view": "customer"}
```

```
panelcad quote import-prices job.pcad.yaml prices.csv --price-mode cheapest
panelcad quote set job.pcad.yaml --number Q-100 --project-name "West pump" --parts-markup 20
panelcad quote build job.pcad.yaml
panelcad quote export job.pcad.yaml --out quote.xlsx
panelcad quote export job.pcad.yaml --out quote-customer.pdf --view customer
```

The GUI is Reports → Quote (internal and customer xlsx, pdf, and csv). The same files are written from the panel. PanelCAD does not email or upload them. A customer export warns when example labor defaults remain, and asks before writing a file that still has unpriced lines.

## Motor circuit

`insert_circuit` adds a motor starter to the open schematic (or a new sheet titled Motor power). Kinds: DOL, VFD, reversing, star-delta, soft starter, PLC-controlled motor. New in-cabinet devices go into free space on the existing panel; local operators go on the door. New kinds put the control section on a Motor control sheet. It does not replace the project or wipe hand-placed layout parts. One undo removes every symbol, wire, and layout change it added. A project with no standard set uses **NEMA/ANSI**. `shared_supply` and `shared_estop` default to true (the next circuit reuses the panel supply and the panel E-stop). `separate_control_rail` defaults to false (control devices share the power rail). Pass false or true on the op to switch them.

```json
{"id": 2, "op": "insert_circuit", "kind": "dol", "branch": "breaker", "control": "local", "kw": 1.5}
{"id": 3, "op": "insert_circuit", "kind": "vfd", "drive": "es1_0p75", "branch": "disconnect", "control": "local", "new_sheet": true}
```

```python
s.insert_circuit(kind="dol", control="local")
s.insert_circuit(kind="vfd", drive="gs20_0p5", branch="breaker", line_contactor=True, new_sheet=True)
```

DOL power is branch → contactor → overload → motor. The default motor is 1.5 kW, so an IEC project uses `iec.contactors.ac3_9a` and `iec.breakers.mcb_3p_32a`. A NEMA/ANSI project uses the matching `nema.*` parts (`nema.breakers.mcb_3p_32a`, `nema.contactors.ac3_9a`, `nema.protection.overload`, `nema.loads.motor_3ph`). `kw=7.5` selects the 32 A contactor. `kw` above 15 selects the 100 A MCCB. A VFD has no overload unless you add one yourself. `generic_1k5` is 3-phase in on L1/L2/L3 (`nema.drives.vfd_1k5` when the standard is NEMA/ANSI). OEM drives (`gs20_0p5`, `es1_0p75`) stay on their catalog ids. Local DOL control is a 24 V series rung (e-stop, stop, start) into coil A1/A2. Local VFD control is a dry contact from the drive common (COM, DCM, or P7-7) through those operators to the run pin.

Circuit Builder inserts 3-phase circuits only. `insert_circuit(..., phases=1)` raises ValueError (`single-phase circuits are not drawn yet`) on Session, the JSONL agent, and `panelcad electrical circuit --phases 1`. A non-integer such as `phases=3.6` or `"3.6"` raises ValueError (`phases must be an integer`) on Session, the JSONL agent, and `panelcad electrical circuit --phases 3.6`. `3`, `3.0`, `"3"`, and `"3.0"` still draw. Single-phase sizing (1 hp at 115 V is 16 A) is the Motor sizing panel, `size_motor_circuit`, and `panelcad electrical size-motor --phases 1`. That path does not draw a 1-phase schematic.

## Terminal strip

`open_terminal_strip` draws the strip (DIN rail, blocks, end hardware). The terminals themselves stay on the schematic. One undo removes the edit.

```json
{"id": 4, "op": "open_terminal_strip", "strip": "X1"}
{"id": 5, "op": "add_terminal", "strip": "X1", "after": "X1-1"}
{"id": 6, "op": "reorder_terminals", "strip": "X1", "refs": ["X1-2", "X1-1", "X1-3"]}
{"id": 7, "op": "jumper_terminals", "refs": ["X1-1", "X1-2"]}
{"id": 8, "op": "retag_terminal_strip", "strip": "X1"}
{"id": 9, "op": "clear_terminal_jumpers", "refs": ["X1-1", "X1-2"]}
```

```python
s.open_terminal_strip("X1")
s.add_terminal("X1", after="X1-1")
s.reorder_terminals("X1", ["X1-2", "X1-1", "X1-3"])
s.jumper_terminals(["X1-1", "X1-2"])
s.retag_terminal_strip("X1")
s.clear_terminal_jumpers(refs=["X1-1", "X1-2"])
```

Schematic reading order is the terminal order. Reorder moves the symbols to match the strip and retags them. Sequence reads that position and writes the same levels and, with `rename`, the same tags. Vendor export: `export_terminal_strip` with `vendor` `phoenix` (PROJECT complete CSV), `clipx` (clipx ENGINEER interchange XML, import unconfirmed), `wago` (smartDESIGNER CSV), `wagoxml` (smartDESIGNER interchange XML, import unconfirmed), or `weidmueller` (WMC semicolon CSV — the documented WMC import). Articles map from library data; unmapped refs are listed in `warnings`.

## Python (same façade)

```python
from panelcad import Session

s = Session.open("job.pcad.yaml")
s.describe()
s.list_sheets()
s.set_active_sheet("control")
s.place("iec.relays.k_4co_24vdc", ref="K2", x=100, y=80)

s.add_sheet("enclosure", title="Enclosure", kind="panel_layout")
s.layout_from_schematic()
s.list_rails()
s.place("iec.terminals.x_2p5", ref="X1", x=40, y=80, rail_id="r1")

s.add_port("A2", "COM3", kind="serial", medium="RS-485")
s.add_sheet("lan", title="Ethernet", kind="network")
s.layout_network(medium="ethernet")
s.wire("A2.ETH", "P1.ETH", net="LAN")
s.wire("G1.plus", "K2.A1", net="24V")
s.insert_circuit(kind="vfd", drive="generic_1k5", branch="breaker")
s.wire("A4.Q1", "H1.X1", net="LT_Red", sheet_id="rack-out", to_sheet="lights")
s.cleanup()
s.set_pins_locked("K2", False)
s.set_pin("K2", "A1", x=0, y=30, side="left")
s.set_pins_locked("K2", True)
s.set_title_block(rev="B")
s.issue_revision(description="Issued for construction", drawn="GH", checked="AK", approved="JS")
s.compare_revisions("A", "working", csv_path="rev.csv", pdf_path="rev.pdf")
s.export_pdf("job.pdf")
s.print_package("job_package.pdf")
s.export_dxf("power.dxf", sheet_id="power")
s.save()
```

## Import / export everything

Round-trip without the GUI. A **package folder** is the seamless unit:

```
panelcad export job.pcad.yaml -o job_pack
panelcad import job_pack -o job2.pcad.yaml
```

Package contains: `project.pcad.yaml`, `project.json`, `title_block.yaml`, `bom.csv`, `bom_grouped.csv`, `wires.csv`, `sheets/*.yaml`, `library/*.yaml`, `svg/*.svg`, `dxf/*.dxf`, `print.pdf` (the one-click print package).

```
panelcad package job.pcad.yaml --pdf job_package.pdf
panelcad rev issue job.pcad.yaml -d "Issued for construction" --drawn GH
panelcad rev list job.pcad.yaml
panelcad rev compare job.pcad.yaml A working --csv rev.csv --pdf rev.pdf
```

Auto-detect a single file:

```
panelcad import extra.yaml --into job.pcad.yaml   # library, sheet, title block, or project
panelcad import vendor.csv --into job.pcad.yaml   # BOM or wire list
```

```python
s.export_package("job_pack")
s.import_any("vendor_bom.csv")
Session.from_any("job_pack")
s.export_json("job.json")
```

Agent: `print_package`, `export_package`, `import_any`, `export_json`, `import_library`, `import_sheet`, `import_wires`, `wire_list`, `export_wire_markers`, `set_net_wire_spec`. The Pydantic drawing agent also has `print_package` (`path`, `paper`, `sections`), `wire_list` (always the from-to report; `path`, `sort`), and `export_wire_markers` (`path`, `target`, `style`, `sort`).

## Bug reports

Humans use Help → Report a bug and Edit → Settings → Bug reports. The same zip is `Session.report_bug`, JSONL `report_bug`, the drawing-agent tool `report_bug`, and `panelcad bug-report`. `panelcad logs --path` prints the logs folder.

```json
{"id": "r1", "op": "report_bug", "title": "Wire would not land", "description": "Pin A2 rejected the wire", "send": false}
```

```python
s.report_bug("Wire would not land", "Pin A2 rejected the wire")
```

```
panelcad bug-report --title "Wire would not land" --description "Pin A2 rejected the wire"
panelcad logs --path
```

`help` returns `schemas.report_bug`. A failed op includes `crash.id` and `crash.path`; put that id in the description when you file the report. Sending uses the environment webhook, then the webhook saved in settings, then the incoming webhook embedded in a release build. The bot token is the environment, then settings, then the bot token embedded in a shop release; that token posts only to the channel id stored with it. A public build embeds neither. `PANELCAD_BUG_REPORTS=off` or `bug_reports.disabled` turns sending off. The channel message is the report text (bold title, version, OS, git sha, machine-name hash, zip file name, contact, description, steps, recent actions, traceback, and log tail). A webhook posts text only; the zip stays on the computer. The bot uploads the screenshot and the zip. The computer name is included only when system info is included. The zip never contains webhook URLs or tokens. Home directories in the log and in paths are written as `~`.

A crash copies the primary drawing only. The crash dialog does not replace it. Each process has its own lock; `panelcad logs` and the agent do not clear a GUI lock that still belongs to a running process. The next start offers the copy recorded on a dead lock and shows when that session was last active. Unsaved work is copied into the recovery folder on the autosave interval (default 300 seconds) and is never written over the open file. `0` turns autosave off. `PANELCAD_AUTOSAVE_SECONDS` overrides the saved interval.

```json
{"id": "s1", "op": "settings", "autosave_seconds": 300}
```

```python
s.set_autosave_seconds(300)
s.app_settings()
```

```
panelcad settings
panelcad settings --autosave-seconds 0
```

`help` returns `schemas.settings`. The same interval is Edit → Settings. Reopening a recovery copy from the next-start prompt loads it as unsaved work and Save writes the original project, or Save As when there was no file.

## Rules

1. Unique `id` per component **on that sheet**.
2. `inspect_device` first — only wire pins that exist.
3. Leave wire `points` empty unless you need a custom route.
4. Tag prefixes follow the project (or sheet) standard. IEC keeps IEC 81346: Q breaker, K contactor/relay, A PLC, P HMI, X terminal, S operator, H lamp, F overload/fuse, G PSU, T transformer, U VFD, M motor. NEMA/ANSI uses JIC / NFPA 79: CB, FU, DISC, M, OL, MTR, VFD, SS (soft starter), PB, SEL (selector — not SS), PL, CR, TR, T, PS, TB, PLC, LS, PRS (`operator.pressure`), ES (e-stop). `inspect_device` returns the prefix `place` will use. `retag` with `mode=standard` (or `apply_prefixes=true`) remaps an existing file; load never retags. Shop overrides live in `project.ref_prefixes` or `library/ref_prefixes.yaml` — see `libraries/builtin/ref_prefixes.yaml`.
5. Always `validate` before `export_pdf`.
6. Do not drive the GUI.
7. After `place` / `layout_io` / a batch of wires, run **`cleanup`**. Do not leave symbols piled on top of each other or hanging in the title block.
8. Schematic and I/O pages use **generic** symbols (PB, solenoid/coil, light, relay). OEM photos and footprints belong on panel/door layout only.
9. A wire that continues on another page uses `to_sheet=` so both pages get a TO / FROM callout. Do not drag a polyline off the sheet edge.
10. `iec.loads.solenoid` is the generic coil for valves / solenoids on I/O rungs. `iec.connection.offpage` / `offpage_from` are the callout arrows.
11. I/O and network terminals show **OEM silkscreen** (`set_pin_labels` or library `label:`). Keyence X0/Y0, SMC OUT0, GC Si/So. Wire refs stay `I0`/`Q0`.
12. Coil style is `title_block.coil_style`: `circle` (default) or `square`. GUI: Shift+C / Shift+Q.
13. After a batch of edits, `cleanup`. After I/O pack, confirm cards sit in ANSI columns (DI right, DO left).
