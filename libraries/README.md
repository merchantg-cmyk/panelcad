# Component library

Each YAML file under `builtin/` is a catalog of **devices**. Add a new object by copying a nearby device and changing `id`, pins, `draw`, and `panel` size.

`panel` must say whether the part is DIN-rail (`din`) and whether it belongs on the enclosure (`in_panel`). Leave W/H/D blank to get a generic size; set `dims: catalog` when the numbers came from an OEM drawing. Motors, node blocks, and disconnects are `in_panel: false`.

Identity of a device is `{library}.{id}` from the file, e.g. `iec.breakers.mcb_1p_16a`. Generic symbols are **IEC** (`library: iec`) or **NEMA/ANSI** (`library: nema`, one North American group). OEM files stay `adc` / `oem`.

Project-local overrides: put YAML files in `<project>/library/`.

Manufacturer catalog SKUs live in `builtin/parts/` (one YAML per manufacturer).
They map onto the generic symbols above and never invent catalog numbers —
see `docs/PARTS.md`. Terminal articles stay in `terminal_vendor_articles.yaml`;
do not copy them into a parts file. Overlay order for parts is builtin →
`PANELCAD_LIBRARY_PATH` → `~/.panelcad/library` → `<project>/library` (later
wins).

Device tags: each device has a catalog `ref_prefix` (IEC 81346 on `iec.*`). When the project or sheet standard is NEMA/ANSI, new tags use the JIC / NFPA 79 map in `builtin/ref_prefixes.yaml` (breaker `CB`, motor `MTR`, …). You can change a prefix without a code change: edit that file, drop `library/ref_prefixes.yaml` next to the project, or set `ref_prefixes:` on the project YAML. Existing ids are never rewritten on load.

Environment override: `PANELCAD_LIBRARY_PATH` (path-separator list).

Standard enclosures are a separate table, `libraries/enclosures.yaml` (override with `library/enclosures.yaml` next to the project or `PANELCAD_ENCLOSURE_PATH`). These are wall- and floor-mount boxes, not schematic devices. Leave `catalog` blank unless the number is on the manufacturer's official page.

See `docs/FORMAT.md` for the draw shorthand.

Terminal jumpers are a compact `jumpers:` table in `builtin/jumpers.yaml` (family, pitch, poles, manufacturer, part number). The engine expands each row into a `connection.jumper` device. Copy a row to add Phoenix / WAGO / Weidmüller / Dinkle parts your shop stocks. Matching uses the terminal's `jumper_family` and `pitch_mm` (see `oem_terminals.yaml` and `iec.terminals.x_2p5`).

Do not encode symbols in Python. If a symbol is wrong, fix the YAML.
