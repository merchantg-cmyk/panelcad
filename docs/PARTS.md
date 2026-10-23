# Manufacturer part library

Verified catalog SKUs that the user assigns onto generic library symbols. The
schematic symbol, tag, and wiring stay; manufacturer, catalog number, ratings,
and panel footprint come from the SKU.

**Never invent a catalog number, dimension, or rating.** Every field must be
on the manufacturer's official product page or official datasheet / catalog
PDF. Record that URL in `source:`. If a field is not published there, leave it
blank and mark the entry `verified: partial`.

## Where files live

Overlay order (later replaces the same manufacturer + catalog):

1. `libraries/builtin/parts/*.yaml` (shipped)
2. `PANELCAD_LIBRARY_PATH` entries and their `parts/` subfolders
3. `~/.panelcad/library` and `~/.panelcad/library/parts`
4. `<project>/library` and `<project>/library/parts`

Terminal articles stay in `libraries/builtin/terminal_vendor_articles.yaml`
and are loaded from the `# https://…` comments next to each article. Do not
copy those SKUs into a manufacturer file.

## YAML format

```yaml
kind: manufacturer_parts
manufacturer: Allen-Bradley
version: "0.1"

suffix_rules:
  - id: 100-c-coil
    placeholder: coil
    source: https://literature.rockwellautomation.com/idc/groups/literature/documents/td/100-td013_-en-p.pdf
    codes:
      - { code: D, coil_voltage: "110VAC 50Hz / 120VAC 60Hz" }
      - { code: KJ, coil_voltage: "24VAC 50/60Hz" }
      - { code: EJ, coil_voltage: "24VDC" }

parts:
  - catalog: "1489-M1C160"
    library_id: iec.breakers.mcb_1p_16a
    category: protection.breaker
    description: "Bulletin 1489-M thermal-magnetic MCB, 1 pole, 16 A, C-curve"
    verified: full
    source: https://literature.rockwellautomation.com/idc/groups/literature/documents/sg/1492-sg122_-en-p.pdf
    source_date: "2026-10-07"
    series: 1489-M
    amps: 16
    poles: 1
    ka: 10
    heat_w: 2.5
    mounting: din
    width_mm: 17.5
    height_mm: 111
    depth_mm: 69
    # Optional shop price. Leave these off shipped parts. Do not invent a cost.
    # unit_cost: 0
    # vendor: ""
    # lead_time: ""
    # price_date: ""   # YYYY-MM-DD, the day the shop recorded the cost

  - catalog_template: "100-C09{coil}10"
    suffix_rule: 100-c-coil
    expand: [D, KJ, EJ]
    library_id: iec.contactors.ac3_9a
    category: switching.contactor
    verified: full
    source: https://literature.rockwellautomation.com/idc/groups/literature/documents/td/100-td013_-en-p.pdf
    amps: 9
    poles: 3
    contact_config: 1NO
    heat_w: 3.4
    mounting: din
    width_mm: 45
    height_mm: 81
    depth_mm: 80.5

  - catalog: "3RV2011-1DA25"
    library_id: iec.breakers.mpcb_3p
    category: protection.mpcb
    verified: full
    source: https://mall.industry.siemens.com/teddatasheet/?caller=Mall&format=PDF&language=en&mlfbs=3RV2011-1DA25
    amps: 3.2
    amps_min: 2.2
    amps_max: 3.2
    contact_config: 1NO1NC
    ka: 100
```

Required on every entry: `manufacturer`, `catalog` (or `catalog_template` +
`suffix_rule`), `library_id`, `category`, `verified` (`full` or `partial`),
`source` (http or https URL).

Optional ratings: `amps`, `amps_min` / `amps_max` (adjustable thermal range;
keep `amps` as the max), `poles`, `coil_voltage`, `contact_config`, `ka`,
`sccr`, `sccr_ka` (numeric short-circuit current rating in kA), `heat_w` (watts of published dissipation — enclosure sizing reads this
name), `watt_loss` with `watt_loss_source` (an http URL of the maker's own
dissipation figure; the thermal report treats this as published data and does
not use it to choose the box), optional `max_ambient_c`, `coil_va_sealed`, `coil_va_inrush`, `power_w`, `current_a` (control-circuit
consumption; `heat_w` is not coil watts), `va`, `inrush_method`, `power_source`,
`mounting` (`din` / `panel` / `door` / `plug_on` / `none`),
`width_mm` / `height_mm` / `depth_mm`, `notes`, `series`, `source_date`.

Adjustable categories are `protection.mpcb` and `protection.overload`. A
`verified: full` row in those categories must publish `amps_min` and
`amps_max`. Assignment writes `range` as `min-maxA` so the overload-vs-FLA
check can use it. Motor protectors (3RV2, 140M) map to the generic MPCB
symbols `iec.breakers.mpcb_3p` / `nema.breakers.mpcb_3p` (thermal + magnetic),
not the MCB.

You may nest `ratings:` or `dimensions_mm: {w, h, d}` — the loader flattens
them. Unknown keys are rejected.

`verified: full` means every filled field is on the cited page. It does **not**
mean every possible rating exists. `verified: partial` means at least one
wanted field was not published there (usually W×H×D), or a filled field comes
from a second document. When that happens the `source` URL is the page that
holds the ratings, and `notes` cites the outline URL and page.

A suffix rule is allowed only when the manufacturer documents the construction
(example: 100-TD013 coil letter after the current rating). Do not invent
suffixes.

## Using a part

Shipped manufacturers include Allen-Bradley, Siemens, Eaton, Schneider
Electric (Square D), AutomationDirect, LS Electric, and Yaskawa, plus the
terminal articles. AutomationDirect, LS Electric, and Yaskawa are checked in
`docs/PARTS-AD-LS-YASKAWA.md`. A part with no `width_mm` / `height_mm` is
`verified: partial`. Assigning it raises design check `catalog_missing_dimensions`
and `recommend_enclosure` lists the tag in `catalog_dims_missing`, because
auto-sizing is still using the generic symbol.

- GUI: Inspector **Catalog part…** or Library **Catalog parts…**. Search by
  manufacturer, catalog number, or rating; filter by manufacturer, category, and
  `amps_min` / `amps_max`. The row shows verified status and the source URL.
  Assign is one undo step. Circuit Builder has a preferred-manufacturer list.
- In-app drawing agent tools: `parts_search` (same filters, including
  `amps_min` / `amps_max`) then `assign_part` (`ref`, `catalog`, `manufacturer?`).
  `insert_circuit` takes `manufacturer` and assigns that vendor's SKUs.
  `secondary_manufacturer` fills categories that brand does not make. Omit it
  and the enclosure maker is used when that name is a parts brand; otherwise
  AutomationDirect is used. `none` leaves those symbols generic. Fallback rows
  are marked `catalog_fallback=yes` with `catalog_primary` set to the preferred
  brand. The Circuit Builder dialog, CLI `--secondary-manufacturer`, and the
  drawing-agent tool take the same argument.
- CLI: `panelcad library parts -q 100-C -m Allen-Bradley`,
  `panelcad library parts -m AutomationDirect`, and
  `panelcad library assign job.pcad.yaml K1 100-C09D10 -m Allen-Bradley`.
  `panelcad electrical circuit job.pcad.yaml --manufacturer "LS Electric"` inserts
  a circuit and assigns that vendor.
- JSONL: `parts_search` then `assign_part` (`ref`, `catalog`, `manufacturer?`).
  `insert_circuit` accepts `manufacturer`.
- Session: `search_parts(...)` / `assign_part(ref, catalog, manufacturer=)`

Assignment writes instance properties (`manufacturer`, `part_number`,
`catalog`, `description`, ratings, `heat_w`, `watt_loss` (and its source URL), footprint with
`dim_source: catalog`). It never changes `library_id`.

The BOM instance and grouped CSV include `manufacturer`, `catalog`, and
`source` (the catalog URL). The panel layout uses `effective_footprint`
(catalog millimetres when present).

Published panel cutouts live in `libraries/builtin/cutouts.yaml` (`kind: cutouts`),
joined by `library_id` or by manufacturer + catalog. Each record cites the
drawing URL. A symbol with no verified drawing is `unknown: true` and is not
given a hole. `verified: false` keeps the transcribed numbers and marks them
unverified in the inspector and on the sheet. The IEC 60947-5-1 size 022
record is unverified: the official IEC text was not available, and the
30 mm / 50 mm centres are not taken from 800-TD008. `assign_part` copies a
catalog cutout onto `properties.cutout` when one is on file.
`part_set_cutout` / `panelcad cutouts set` edits that override. Reassigning
a SKU replaces it. A shop edit from the inspector is stored without the
manufacturer citation.

`unverified_part` is an **info** design check on devices assigned from a
`verified: partial` entry.

## Shop overlays

Drop a YAML file in `~/.panelcad/library/parts/` or `<project>/library/parts/`
with the same `kind: manufacturer_parts`. A later file that repeats
`manufacturer` + `catalog` replaces the builtin row — use that to correct a
number, not to invent one. Keep the official `source:` on the replacement.
