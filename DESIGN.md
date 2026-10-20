# PanelCAD design

## Problem

Electricians and controls engineers still draw industrial panels in generic CAD, or in vendor tools that AIs cannot drive. We need:

- Schematic + panel/door layout for breakers, contactors, relays, PLCs, HMIs, terminals, drives
- A GUI a human can draft in
- A text/code path an AI can draft in
- Prints (schematic, BOM, from-to wire list)
- A component library that can grow without rewriting the engine

Cross-platform: **Windows and Linux**.

## Key decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Language | Python 3.11+ | Same codebase on Win/Linux; AIs already emit it well |
| GUI | PySide6 (Qt) | Native-enough desktop CAD; offscreen PDF; used in similar tools |
| Project file | YAML (`*.pcad.yaml`) | Diffable, AI-writable, human-readable. No binary. |
| Domain models | Pydantic v2 | Validation + JSON schema for AI |
| Façade | `panelcad.Session` | One API for GUI, CLI, agent, Python |
| Drawing | Data-driven primitives | Library YAML describes symbols; engine only knows rect/line/circle/text/polyline |
| Headless print | `Painter` ABC → SVG always; Qt PDF when available | AI can export without a window |
| Refs | IEC 81346 on IEC projects (`Q`, `K`, `A`, `X`, …); JIC / NFPA 79 prefixes on NEMA/ANSI (`CB`, `M`, `PB`, `MTR`, …) | Shop prints match the drawing standard. Map is `libraries/builtin/ref_prefixes.yaml`; a project may override keys in `ref_prefixes`. Loading a file never retags. |
| Units | mm everywhere, Y down | Matches screen + enclosure drawings; documented in FORMAT.md |

## Alternatives considered

- **KiCad fork** — PCB semantics (nets/footprints/copper) fight DIN-rail / wire-duct / door layout. Wrong domain.
- **C++ Qt** — closer to KiCad performance; slower for us and for AI-authored extensions. Revisit if the canvas becomes huge.
- **Electron** — easier web UI, worse offline CAD feel, heavier runtime.
- **JSON-only projects** — slightly easier to parse; worse for humans editing library files. YAML stays.

## System shape

```
┌────────────┐  ┌────────────┐  ┌──────────────┐
│  Qt GUI    │  │ CLI/typer  │  │ JSONL agent  │
└─────┬──────┘  └─────┬──────┘  └──────┬───────┘
      │               │                │
      └───────────────┼────────────────┘
                      ▼
                 Session (API)
                      │
          ┌───────────┼────────────┐
          ▼           ▼            ▼
       Project     Catalog      Exporters
       (YAML)      (library)    SVG/PDF/DXF/BOM/wires/print package
```

## Views (KiCad analogy)

| KiCad | PanelCAD |
|-------|----------|
| Schematic | `sheet.kind = schematic` |
| PCB layout | `sheet.kind = panel_layout` (DIN rail, mm, enclosure) |
| Footprint lib | `panel:` block on a device |
| Symbol lib | `schematic:` block on a device |
| Netlist | wires + nets in YAML; exportable wire list |
| Plot/print | SVG/PDF/DXF |

Door layout and terminal strips are extra sheet kinds, not a third editor.

## Library

Devices are YAML. Identity is `{library}.{device_id}`, e.g. `iec.breakers.mcb_3p_32a`.

A device has:

- schematic graphics + pins
- panel footprint (width/height/depth mm, DIN-rail flag)
- default properties (voltage, current, poles)
- `ref_prefix` for auto reference names (IEC / OEM catalog default). NEMA/ANSI jobs remap via `libraries/builtin/ref_prefixes.yaml` (and optional `project.ref_prefixes`) so a breaker is `CB`, a motor is `MTR`, and so on.

Project-local `library/` directories override builtins.

## AI interface

Three layers, same Session:

1. **Edit the YAML** — documented in `docs/FORMAT.md`
2. **Python** — `from panelcad import Session`
3. **JSON lines on stdin/stdout** — `panelcad agent` (for tools that should not import Python)

Never give the AI a second, GUI-only object model.

## Prints

- Schematic sheet → SVG (always), PDF (Qt), DXF (R2010, mm, one file per sheet)
- Panel / door layout → the same three; DXF is true millimetres with INSUNITS set
- BOM → CSV
- Wire list (from-to) → CSV (device, terminal, number, color, gauge). New wires get a number as they are drawn (default NEMA sheet/line `101`).

Cable schedule, PLC I/O list, wire numbers, and coil/contact cross-reference are in `panelcad electrical` (see `docs/AUTODESK_ELECTRICAL.md`). Cross-references are derived from the sheet zone grid (NEMA `101` / IEC `/sheet.column`) and drawn live; opening a file never writes them. Multi-sheet PDF is `export_pdf` on the whole project. DXF stays one file per sheet so a vendor can take the door cutout or backplate alone.

## Non-goals (v0)

- Full IEC 60617 symbol certification
- Simulation / PLC logic execution
- Auto-routing of panel wiring ducts
- Multi-user real-time editing
- Cloud license / vendor lock-in

## PR / milestone plan

Treat these as sequential slices we can review independently:

1. **Core + YAML + library loader** — this slice
2. **CLI + Python API + agent protocol**
3. **Headless SVG/PDF/BOM**
4. **GUI schematic editor**
5. **Panel layout editor**
6. **Library expansion + title blocks + I/O list**
7. **Linux CI / packaging**

## Open questions (product, not blockers)

- Default drawing standard: **NEMA/ANSI** (George's shop prints). IEC stays a first-class setting; Circuit Builder and the title block honor whichever is set. Symbols stay swappable by library.
- Whether manufacturer catalogs are shipped or downloaded
- Plugin language: Python only, or also WASM later
