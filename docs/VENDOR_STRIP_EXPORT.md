# Vendor terminal-strip export

A shop may want a file the vendor configurator will import so a rail can be ordered pre-assembled. This note is what public documentation actually allows, and what PanelCAD writes.

Articles come from library data (`libraries/builtin/terminal_vendor_articles.yaml`, device `props.vendor_articles`, or component `manufacturer` + `part_number`). They are not hardcoded in the writer. Unmapped blocks keep a fallback value and appear in `result.warnings`. Each builtin article has a product-page URL in the YAML; those numbers were checked on phoenixcontact.com, wago.com, and weidmueller.com/eshop (2026-10-07).

Samples: `examples/vendor_strip/`.

## Weidmüller WMC — high confidence

Weidmüller publishes a generic CSV import for every ECAD system that can emit part numbers:

- [CSV interface short manual](https://assets.dam.weidmueller.com/assets/api/ef741515-689c-4d5e-8335-e0320ee015e7/Original/Weidmueller_Configurator_CSV_Interface_shortmanual.pdf)
- [WMC user manual](https://assets.dam.weidmueller.com/assets/api/612b700b-b67f-4937-b667-5daa013ef788/Original/Weidmueller_Configurator_WMC_user_manual.pdf)

Confirmed layout: semicolon, no header, row 1 is the first block.

| Column | Content |
| --- | --- |
| 1 | article number |
| 2 | product designation (PanelCAD uses the tag) |
| 3 | marker labelling (terminal number, then internal / external / jumper wire text) |

This **is** the native WMC drop-in. WMC will ask for article mapping when the number is not a Weidmüller code.

AutoCAD Electrical 2027 also mentions a **CAEDEX** file. That schema is not published. PanelCAD does not invent it. `vendor=weidmueller` + `format=xml` is rejected.

`export_terminal_strip(..., vendor="weidmueller")`  
Reports → Export Weidmüller strip (WMC)…  
`panelcad electrical strip-export job.pcad.yaml --strip X1 --vendor weidmueller -o X1.csv`

## Phoenix Contact clipx ENGINEER / PROJECT complete — unconfirmed XML, medium CSV

clipx ENGINEER (current) and PROJECT complete (predecessor) talk to CAE tools through vendor add-ons. Public notes:

- clipx ENGINEER ↔ EPLAN Pro Panel exchanges an **AML** file in the background. There is no published generic XML drop. ([EPLAN help](https://eplan.help/en-us/Infoportal/Content/Plattform/2025/Content/htm/cabinetgui_h_klemmenzubehoermitclipxhinzufuegen.htm))
- clipx ENGINEER 2.1 added **Import ECAD** for Zuken / WSCAD plugins, not a documented file schema.
- PROJECT complete documents **File → Data Exchange** and “CSV and XML interface” without a column or element list.
- AutoCAD Electrical `AEPHOENIXEXPORT` writes “an XML file compatible with Phoenix Contact Clip Project Planning” / clipx ENGINEER (2027). Autodesk lists the **fields** (catalog, terminal number, installation/location, tag, quantity, spare, accessories) and does **not** publish the tags.

PanelCAD therefore writes two files:

1. **PROJECT complete CSV** (`vendor=phoenix`, default) — rail-order comma CSV: strip, position, tag, article, internal, external, jumper, accessory. Confidence **medium**: the product documents CSV exchange; the column names are PanelCAD’s.
2. **clipx interchange XML** (`vendor=clipx` or a `.xml` path) — those Autodesk fields in a PanelCAD XSD (`examples/vendor_strip/phoenix_clipx.xsd`). Confidence **unconfirmed**. Do not treat a successful structural test as a clipx ENGINEER import.

CSV stays the fallback.

## WAGO smartDESIGNER — unconfirmed XML, low CSV

WAGO documents rail import from **EPLAN, WSCAD, and Zuken E³**. smartSCRIPT accepts Excel/CSV for **marking**, not rail assembly.

AutoCAD Electrical 2027 claims an XML for smartDESIGNER. The schema is not public. Users report the older ProServe XML (`AEWAGOEXPORT`) does **not** import into current smartDESIGNER.

PanelCAD writes:

1. **Comma CSV** (`vendor=wago` / `smartdesigner`) — same rail-order columns as Phoenix. Confidence **low** for rail import; usable as a spreadsheet / smartSCRIPT marking source.
2. **smartDESIGNER interchange XML** (`vendor=wagoxml` or a `.xml` path) — same fields as the Phoenix XML, WAGO namespace. Confidence **unconfirmed**.

CSV stays the fallback.

## How to export

| Surface | CSV | XML |
| --- | --- | --- |
| Reports menu | Phoenix PROJECT complete, WAGO smartDESIGNER, Weidmüller WMC | Phoenix CLIPX XML, WAGO smartDESIGNER XML |
| Session | `s.export_terminal_strip(path, "X1", "phoenix")` | `s.export_terminal_strip(path, "X1", "clipx")` or `file_format="xml"` |
| CLI | `--vendor phoenix\|wago\|weidmueller` | `--vendor clipx` or `--format xml` |
| JSONL | `{"op":"export_terminal_strip","path":"X1.csv","strip":"X1","vendor":"phoenix"}` | `"vendor":"clipx"` or `"format":"xml"` |

Result keys include `confidence`, `note`, `mapped`, `unmapped`, and `warnings`.
