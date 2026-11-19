# PanelCAD project format (v0.1)

Projects are YAML. Extension: `.pcad.yaml`.

Coordinates are **millimetres**. Origin is **top-left**, **Y increases downward**. Rotation is 0/90/180/270 degrees clockwise.

## Minimal project

```yaml
format: panelcad
version: "0.1"
name: Pump MCC
sheets:
  - id: power
    title: Power Distribution
    kind: schematic
    page: { size: A3, orientation: landscape }
    components:
      - id: Q1
        library_id: iec.breakers.mcb_3p_32a
        x: 40
        y: 30
        rotation: 0
        properties: { In: 32A }
    wires:
      - id: w1
        net: L1
        from: Q1.2
        to: K1.L1
        points: []          # empty → auto Manhattan
```

## Fields

### cables

One field cable from a panel end to one field device. `propose_cables` builds these from wires whose one end is a terminal, or a VFD output pin (`U`/`V`/`W`, or `T1`/`T2`/`T3` on a drive), and whose other end is a motor, valve, limit switch, instrument, or a device marked `location: field` (a remote pushbutton station). Door operators are not field cables. When a terminal strip sits between a drive and the motor, the strip is the panel end.

NEMA tags are `W101`, `W102`, …. IEC tags are `-W1`, `-W2`, …. `ref_prefixes.cable` overrides the prefix. `show_tag` (default true, omitted) writes the tag into `wire.cable` so the schematic draws it on the field wire.

`cable_type` is a standard description (`3C+G #12 VFD shielded`, `3C+G #14 TC-ER`, `2PR #18 shielded instrument`). The shield is extra; it is not inside the conductor count. `3C+G` is three phase conductors plus the ground. `part_number` is empty unless someone typed it or the part library has that description with an `http` source URL. Conductor colors follow that same rule: a color already on the wire, typed in the editor, or named by one library cable part (`colors: 1=BLK, 2=RED, G=GRN` in the notes, with an `http` source) is kept. Otherwise the color stays empty and the termination list prints `per cable`. The ground conductor's role prints as `GND`; it is not given a jacket color. `gland` stays empty unless someone enters a cord grip. A library cable part that states an OD in its description or notes, and has an `http` source, fills `od` / `od_source`. The schedule then prints that OD. It does not guess an NPT or metric trade size; with no OD and no entered gland the schedule says `size per cable OD`.

`kind` is `field` (omitted) or `interpanel`. An inter-panel cable sets `from_panel` and `to_panel` to panel ids, plus `to_strip` and `to_terminals` at the far end. Field cables omit those keys.

`shield_bond` is `both`, `panel`, `field`, or `none`. A VFD output cable defaults to `both`: the drain lands on the drive PE / EMC shield clamp (or the strip PE when the cable leaves a strip) and on the motor frame. An instrument cable defaults to `panel`: the drain lands on a ground terminal of that strip when one exists, otherwise `panel chassis ground`, and the field end is `cut back and insulated`. Those two defaults cite Rockwell 520-UM001 and 1756-IN063 on `shield_source`. A shielded cable with no cited default leaves the drain ends blank so the design check can flag it. Spare conductors read `spare, coiled and taped` at both ends until someone assigns a terminal. Length is user-entered (`ft` on NEMA, `m` on IEC) with an optional `slack_pct`. Pull length is length × (1 + slack/100).

```yaml
cables:
  - tag: W101
    from_strip: TB1
    from_terminals: [TB1-1, TB1-2, TB1-3, TB1-G]
    to_device: MTR1
    to_kind: motor
    cable_type: "3C+G #14 TC-ER"
    conductors: 4
    gauge: "#14"
    length: 120
    slack_pct: 10
    route: TRAY-A
    assignments:
      - { id: "1", panel_terminal: TB1-1, field_terminal: MTR1.T1, wire_number: "101", role: phase }
      - { id: G, panel_terminal: TB1-G, field_terminal: MTR1.PE, role: ground }
```

### Project

| Field | Type | Notes |
|-------|------|-------|
| format | `panelcad` | Required |
| version | string | Format version |
| name | string | |
| properties | map | Free-form extras. `suppressed_checks` is a list of live-check finding ids (`check:sheet:ref:target`) the Problems dock has ignored. `quote` holds a shop estimate (quote number, markups, imported prices, manual prices, labor overrides). A manual row may store only the fields that were typed (vendor, lead time, or date) without freezing the list cost. It is omitted until a quote is edited. Prices are never filled in by the program. |
| title_block | map | Boxes along the bottom of every sheet |
| ref_prefixes | map | Optional. Shop overrides for NEMA/ANSI device tags (`protection.breaker: MCB`). Keys are a device `category` or `category.<tag>`. Empty / omitted uses `libraries/builtin/ref_prefixes.yaml`. IEC projects ignore this map and keep each device's library `ref_prefix`. Loading a file never retags existing ids. |
| wire_numbering | map | Optional. `{scheme: sheet_line\|sequential\|iec, enabled: true, start: 1}`. Default NEMA sheet/line (`101`, `102`). Omitted when it is the default so existing files stay unchanged. Opening a file never assigns numbers. |
| net_wire_specs | map | Optional. Per-net defaults `{size, color, type}` that a wire inherits when its own field is blank. Key `*` is the global fallback (`panelcad electrical net-spec --net all` stores that same key). Keys `control` and `power` are the class defaults used by the from-to report. Empty fields and an empty map are omitted. |
| cross_reference | map | Optional. `{enabled: true, nc_mark: underline\|bar\|parentheses, rung_pitch_mm: 12.7, rung_numbers: used\|all}`. NEMA line = `sheet*100 + rung` at `rung_pitch_mm` (default 12.7 mm / 0.5 in). IEC uses `/sheet.column`. Opening a file never writes `properties.xref`. Omitted when it is the default. `update_cross_references` / `panelcad electrical xref --write` persist the last computed text. |
| revision_control | map | Optional. Offline customer revisions stored in the project file (no git). `{scheme: letter\|number, clear_clouds_on_issue: true, issued: [...], clouds: [...]}`. Each issued row is `{rev, date, description, drawn, checked, approved, snapshot}`. `snapshot` is the project at issue time and does not nest further snapshots. Clouds are `{id, sheet, x, y, w, h, rev, manual, note}` in sheet millimetres. `note` is optional shop text drawn in an empty cloud, such as a removal (`S2 deleted`) or a moved-from mark (`K9 moved`). Empty notes are omitted. Omitted when unused. The current rev is `title_block.revisions[0]` and is drawn on every sheet. |
| cables | list | Optional. Field cables that leave a panel, and inter-panel cables. Omitted when empty. See below. |
| panels | list | Optional. One physical panel (`CP1`, `RIO2`). Omitted when empty. A file with no `panels` list is a single unnamed panel and loads unchanged. Saving that file does not add the list. There is no rewrite migration. |
| active_panel | string | Optional. Panel id the GUI and `place` assign new devices to. Omitted when empty. |
| location_system | string | Optional. Default IEC `=system` aspect. A panel's own `system` wins. Omitted when empty. |
| reconstruct | map | Optional. As-built reconstruction from **phone photos of a physical panel**. Omitted when unused. Photos live in `photos/` next to the project. `{photos, items, checklist, prints, mismatches}`. A part is layout-ready only with a catalog match that has W×H×D or a tech-confirmed envelope (W, H, D, mount). Unidentified items become reconstruct-only `needs_info` / INFO NEEDED markers — never generic bodies. A `.reconstruct.yaml` sidecar keeps this block if a main build opens the file. An old print under `prints` is a secondary cross-check only. |
| sheets | list | One entry per print page. Panel jobs are multi-sheet. Each sheet may name a `panel`. |
| last_saved | map | Optional. `{user, host, time, summary}` of the last save. Omitted until a save records it. Not copied into customer revision snapshots. |
| activity | list | Optional. Newest 100 saves: `{user, host, time, summary}`. Omitted when empty. Same exclusion from revision snapshots. See `docs/SHARED_FOLDERS.md`. |

### panels

Several enclosures in one project: a main panel, a remote I/O panel, an MCC section, an operator station. Each panel has a tag, an optional name, a catalog enclosure, a backpanel sheet, a door sheet, and a terminal-strip sheet.

```yaml
panels:
  - { id: cp1, tag: CP1, name: Main, enclosure_id: wall-20x12x8-n12, system: A1 }
  - { id: rio2, tag: RIO2, name: Remote I/O, enclosure_id: wall-20x16x8-n12 }
active_panel: rio2
location_system: A1
```

`enclosure_id` is a row in `libraries/enclosures.yaml` (id or confirmed catalog number). The backpanel sheet stores that row's millimetres. A typed width, height, or depth is not a panel enclosure. A table row with no confirmed catalog number still supplies the standard size and leaves `catalog` blank.

Devices carry `panel: cp1` (the panel id). The device tag stays `K1` or `CR1`. On an IEC drawing the schematic shows `=A1+CP1-K1`. On a NEMA drawing the tag stays `CR1` and the location line is `CP1`. A single-panel file leaves `panel` off the component, so the drawing does not change.

Sheets use the same `panel` id. The print package, when there are two or more panels, prints unassigned sheets first and then each panel's sheets together. The bill is one section per panel plus a project total.

An inter-panel cable is a `cables` row with `kind: interpanel`, `from_panel`, `to_panel`, and the strip at each end. `kind: field` is omitted. Off-page arrows between those panels include the other panel's tag (`TO RIO2 4 / K1`).

### title_block

Drawn as labeled boxes on the bottom of the sheet, plus zone coordinates (numbers across, letters down the side). On a NEMA/ANSI ladder the left rail shows sheet/line numbers (`101`, `102`, …) by rung pitch (default 0.5 in); IEC keeps the letter rows and uses `/sheet.column` for refs.

| Field | Notes |
|-------|--------|
| company, address | Left company box |
| logo | Image path (`png`/`jpg`/`svg`). Relative to the project file, usually `assets/company_logo.png` |
| customer, location | Bottom-center boxes |
| drawing_number | DWG No. |
| drawn_by, checked_by, approved_by | Sign-off boxes |
| scale | Default `NTS` |
| standard | Default `NEMA/ANSI` (DESIGN.md / models.py — George's prints). Circuit Builder uses this when the sheet has no `standard`. An explicit `IEC` project keeps `iec.*` and IEC 81346 tags (`Q`, `K`, …). `NEMA`, `ANSI`, `JIC`, and `NFPA` all mean the North American `nema.*` catalog and JIC / NFPA 79 tags (`CB`, `M`, `PB`, …). |
| revisions | List of `{rev, date, description, drawn, checked, approved}` — up to 3 rows shown |

### Sheet

| Field | Type | Notes |
|-------|------|-------|
| id | string | Stable; used by CLI/agent |
| title | string | Sheet title in the title block |
| description | string | Sheet description box |
| kind | `schematic` \| `panel_layout` \| `door_layout` \| `terminal` \| `network` \| `io` | `network` = comms page. `io` = PLC I/O ladder. `terminal` draws a DIN-rail strip, not schematic symbols |
| strip | string | On a `terminal` sheet, the strip this page draws (`X1`). Empty on every other sheet, and omitted from the YAML when empty |
| strip_part | int | Continuation of a long strip (`2`, `3`, …). Omitted when it is `1` |
| standard | string | Optional. Empty inherits `title_block.standard`. Circuit Builder reads this first. |
| page.size | `A4`/`A3`/`A2`/`custom` | |
| page.orientation | `landscape`/`portrait` | New `panel_layout` sheets default to A3 portrait |
| page.width_mm / height_mm | number | Required if size=custom |
| components | list | On `panel_layout`, `(x,y)` is the footprint top-left in mm. Optional `properties.display_width_mm` / `display_height_mm` size the drawn box (door photo) without changing catalog BOM millimetres. BOM `incabinet` (yes/no) is whether the part occupies cabinet volume (`in_panel` is the same flag). |
| wires | list | Schematic / network. `{id, from, to, net, label?, number?, number_locked?, color?, gauge?, type?, cable?, inferred?, points}`. Drawn text is the wire `number` when set, else `label`, else `net`. `color`, `gauge`, and `cable` are the spec under that text. `type` (MTW, THHN, …) is report-only. `inferred` (omitted when false) marks an as-built guess: the canvas draws it dashed with a `?` caption until a tech confirms it. On the from-to report a blank size, color, or type inherits that net, then `control` or `power`, then `*`. A gauge that is still blank is `TBD` (no default size). Color and type still fall back (control BU MTW, power BK MTW). The stored fields stay blank. See `docs/WIRE_MARKERS.md`. `number_locked` keeps a user-set number. Empty fields are omitted. A wire that continues on another page is two local stubs plus matching `iec.connection.offpage` / `offpage_from` arrows (`Session.wire(..., to_sheet=)`). |
| rails | list | `panel_layout`: `{id, x, y, length_mm, height_mm?, type}` — TS35 clip centerline. `height_mm` is the drawn hat (scaled with the sheet; 35 mm true). |
| enclosure | map | `{x, y, width_mm, height_mm, depth_mm, name}` backplate outline |
| network | map | `{name, medium, protocol}` on `kind: network` sheets |
| closed | bool | Optional. `true` hides the tab; the page stays in the print set. Omit or `false` when open. |
| panel | string | Optional. Panel id (`cp1`). Omitted when empty. Layout, door, and terminal sheets created while a panel is active are assigned to it. |

A `terminal` sheet does not copy the terminal blocks. It projects every terminal whose strip name matches `strip`, in schematic reading order (sheet, then top to bottom, then left to right), at catalog block width. A strip that does not fit continues on the next row and then on `strip_part` 2, 3, …. Pin A is the internal side (rotated text above the block) and pin B is the field side (rotated text below). End stops, end plates, and partitions are on the panel DIN rail with the terminal footprints, touching at catalog width:

| property | values |
| --- | --- |
| strip | Strip name, same as the sheet |
| accessory | `end_plate`, `end_stop`, or `partition` |
| end | `left` or `right` on end plates and end stops |
| after | Terminal ref a partition sits after |

Library ids are `iec.accessories.end_plate`, `iec.accessories.end_stop`, and `iec.accessories.partition` (`connection.accessory`, so they are not extra strip levels). A jumper is still `properties.jumper` on the terminal (old files load). The span (first to last marked terminal in strip order) picks a catalog part from `libraries/builtin/jumpers.yaml` by `jumper_family` and `pitch_mm`. That part is a BOM line, a strip-export accessory row, and the label on the drawn bar. George adds jumpers by appending a `jumpers:` table row (family, pitch, poles, manufacturer, part number).

Vendor strip export maps each block to a catalog article in this order: component `properties.vendor_articles.<vendor>`, component `manufacturer` + `part_number` when the manufacturer is that vendor, the same fields on the library device, then `libraries/builtin/terminal_vendor_articles.yaml` (override by copying that file into the project `library/` folder). Anything that still has no article is exported with the fallback part number or library id and listed in `warnings`. See `docs/VENDOR_STRIP_EXPORT.md`.

Schematic position is the order. Reorder on the strip moves the symbols into that order, writes levels 1..n, and retags them `STRIP-n` (wires and the panel footprint follow). `sequence_terminal_strip` writes those same levels from the schematic; `rename=True` retags the same way. After a reorder, sequence changes nothing. Deleting a block in the strip view deletes that tag on the schematic and on the panel. The grouped BOM counts a tag once when the same ref is on the schematic and the panel.

Sidecar files next to the project (not inside the YAML): `*.pcad.ai.jsonl` is the AI activity journal; `*.pcad.ai-in.jsonl` is the inbox an open GUI applies; `*.pcad.ai-prompt.jsonl` is the human prompt line from the AI dock. A synced folder also gets `*.pcad.lock.json` while someone has the project open, and `.<filename>.backups/` keeps the previous YAML. See `docs/SHARED_FOLDERS.md`.

### rails

DIN rail. `(x, y)` is the **left end of the clip centerline**. Default type `ts35` (35 mm hat).

Devices with `panel.din: true` snap so the rail runs through the upper part of the footprint (`clip_offset`, typically 15 mm from the top).

```yaml
panel: { w: 27.0, h: 90.3, d: 66.8, din: true, in_panel: true, mount: din_rail, dims: catalog }
```

| Field | Notes |
|-------|--------|
| w / h / d | mm. If omitted or 0, a **generic** size for the category is filled in |
| din | DIN-rail mount (`true`/`false`) |
| in_panel | `false` for motors, node blocks, disconnects — schematic/BOM only |
| mount | `din_rail` \| `backplate` \| `door` \| `field` \| `none` |
| dims | `generic` \| `catalog` \| `measured` |
| draw | Footprint block geometry from Symbol Builder (lines, rects, circles) |
| attributes | Footprint attributes such as `P_TAG1`, `MFG`, `CAT`, `ITEM` |

A Symbol Builder file also has `block: parent|child|terminal|footprint` and schematic `attributes` (`TAG1`, `TAG2`, `DESC1`, `MFG`, `CAT`, …). Those fields fill in when the symbol is inserted. The geometry stays in `schematic.draw`.

```yaml
  - id: enclosure
    title: Enclosure
    kind: panel_layout
    page: { size: A3, orientation: portrait }
    enclosure: { x: 18, y: 18, width_mm: 261, height_mm: 330, depth_mm: 200, name: "400×500×200" }
    rails:
      - { id: r1, x: 38, y: 68, length_mm: 221, type: ts35 }
    components:
      - id: Q1
        library_id: iec.breakers.mcb_3p_32a
        x: 38
        y: 53
```

### enclosure

The box / backplate drawn behind rails. Optional. `Session.add_sheet(..., kind="panel_layout")` creates a default enclosure plus three rails that fit the page. Optional fields `nema_type`, `material`, `catalog`, `manufacturer`, `mount` are written by **Size Enclosure** when you apply a standard box.

Standard box sizes live in `libraries/enclosures.yaml` (not the device catalog). George can edit that table. `catalog` is blank unless the number was confirmed on the maker's official page.

### Component

| Field | Type | Notes |
|-------|------|-------|
| id | string | Reference designator, unique on the sheet (`Q1`) |
| library_id | string | `iec.breakers.mcb_3p_32a` |
| x, y | mm | Symbol origin |
| rotation | 0/90/180/270 | |
| scale | number | Visual size. `1.0` is the library symbol / footprint. Drag the orange handle in the GUI. |
| mirror | bool | |
| properties | map | Overrides device defaults |
| pins_locked | bool | Default `true`. Unlock to slide connection points on this instance. |
| pin_overrides | map | `{ pin_id: {x, y, side?} }` in symbol-local mm. Omit pins that still use the library. |
| picture | string | Photo for **panel_layout / door_layout** only. Empty shows a PHOTO slot on the footprint. |
| panel | string | Optional. Panel id this device belongs to. Omitted when empty. Same tag on another sheet is the same device and the same panel. |

Pin references are `{component_id}.{pin_id}` → `Q1.2`, `K1.A1`. Comms port refs on network sheets use the same form: `A2.ETH`, `A2.COM2`.

```yaml
pins_locked: false
pin_overrides:
  A1: { x: 0, y: 30, side: left }
```

### ports (library device or instance)

```yaml
ports:
  - { id: ETH, kind: ethernet, medium: "10/100", protocol: "Modbus TCP", connector: RJ45, side: right }
  - { id: COM1, kind: serial, medium: RS-232, protocol: Modbus RTU, connector: RJ12, side: left }
```

`kind` is `ethernet` | `serial` | `usb` | `can` | `fieldbus` | `other`. Instance ports (object editor) override the library list for that ref.

### Wire

| Field | Type | Notes |
|-------|------|-------|
| id | string | |
| from / to | pin ref | |
| net | string | Optional name (`L1`, `24V`, `N$3`) |
| number | string | Project wire number. Same electrical network shares one number. |
| number_locked | bool | User-set. Omit when false. Auto-number never overwrites. |
| color / gauge / cable | string | Spec drawn under the number |
| points | list of `{x,y}` | Extra waypoints; endpoints come from pins |

## Library files

See `libraries/builtin/*.yaml`. Device id in file is combined with the file's `library:` key:

```yaml
library: iec
version: "0.1"
devices:
  - id: breakers.mcb_1p_16a
    name: MCB 1P 16A
    category: protection.breaker
    ref_prefix: Q
```

Full id: `iec.breakers.mcb_1p_16a`. `ref_prefix` is the IEC / OEM catalog default. On a NEMA/ANSI sheet, `place` and Circuit Builder look up the category in `libraries/builtin/ref_prefixes.yaml` (overridable per project) so the breaker becomes `CB1` instead of `Q1`.

A shop file `library/ref_prefixes.yaml` or `~/.panelcad/library/ref_prefixes.yaml` merges on top of the builtin map. Same shape:

```yaml
kind: ref_prefixes
standards:
  NEMA/ANSI:
    protection.breaker: MCB
    operator.selector: SEL
```

Schematic `draw` shorthand:

```yaml
draw:
  - rect: [x, y, w, h]
  - line: [x1, y1, x2, y2]
  - circle: [cx, cy, r]
  - polyline: [[x,y], [x,y], ...]
  - text: [x, y, "string", size?]
```

Pins:

```yaml
pins:
  - { id: "1", x: 10, y: 0, side: top }
```

`side` is `left|right|top|bottom` and drives auto-wire stubs.

Quote YAML 1.1 boolean words if you mean text: `NO`, `YES`, `on`, `off`, `true`, `false`. Write `contact: "NO"` not `contact: NO`.

## BOM CSV

Excel-friendly (`utf-8-sig`). Two layouts:

**Instance** (default, round-trip) — one row per device:

`item,qty,sheet,ref,library_id,description,manufacturer,part_number,catalog,source,catalog_fallback,catalog_primary,category,width_mm,height_mm,depth_mm,din_rail,incabinet,in_panel,mount,dim_source,notes,x,y`

plus any extra property columns (`In`, `poles`, …).

`din_rail` / `in_panel` are `yes`/`no`. `mount` is `din_rail`, `backplate`, `door`, `field`, or `none`. `dim_source` is `generic` (typical size), `catalog` (OEM drawing), or `measured`.

Motors, schematic nodes, and disconnects have `in_panel: no` and are omitted from panel-layout packing. An agent can overwrite sizes with `set_panel` after pulling an OEM cut sheet.

## Cutout drawings

`libraries/builtin/cutouts.yaml` holds published openings (round, rectangular, or D, plus drills, depth, and panel thickness). A device override is `properties.cutout` (same fields, or `unknown: true`). `properties.surface` is `door`, `backplate`, `left`, `right`, `top`, or `bottom`. `properties.include_key` cuts an optional key recess. `verified: false` means the figures were not checked on an official publication; the sheet prints `UNVERIFIED` in front of that source. The IEC 60947-5-1 Table 2 / Table 3 record is unverified (secondary BIS copy only). A shop edit clears `source` and sets the note to a shop opening.

Project properties used by the sheets and the design checks:

- `cutout_datum` — `top_left` (default), `top_right`, `bottom_left`, `bottom_right`
- `cutout_style` — `ordinate` or `baseline`
- `cutout_view` — `front` or `back` (back mirrors X)
- `cutout_units` — `mm` or `inch` (dimension numbers; DXF geometry uses the same unit)
- `cutout_edge_clearance_mm`, `cutout_flange_mm`, `cutout_panel_thickness_mm` — blank means that check is off. A cut that crosses the outline still warns.

Shop DXF layers are `CUT`, `DRILL`, `DIM`, `TEXT`, and `OUTLINE`, 1:1. The print package section `cutouts` is optional.

## Troubleshooting export

`troubleshoot.json` is an open document written beside a project or into an export package. It is not a field in `.pcad.yaml`. Readings live in `<stem>.troubleshoot-log.json` next to the project file. Schema: `docs/TROUBLESHOOT.md`.

**Grouped** (`--grouped`) — one row per unique part; `qty` is the count and `ref` is `Q1, Q2`.

Import:

- Matching `ref` → update manufacturer / part number / catalog / source URL / notes (does not delete anything). `source` (aliases `url`, `catalog_source`) is stored as `catalog_source`.
- Unknown `ref` or no `ref`, plus `library_id` → place that many parts.
- No `ref` and no `library_id`, but a part number, catalog number, or description → needs-info part. No millimetres and no generic footprint. Kept off the BOM, quote total, and cutouts. The print is marked NOT FOR BUILD until a library part is chosen.
- Header aliases accepted: `Tag`, `Part Number`, `part #`, `part`, `item number`, `model`, `model number`, `catalog number`, `cat no`, `manufacturer part number`, `brand`, `make`, `Mfr`, `count`, `quantity required`, `qty req`, `Qty`, `Type`, …

```
panelcad bom export job.pcad.yaml -o bom.csv
panelcad bom import job.pcad.yaml bom.csv
```
