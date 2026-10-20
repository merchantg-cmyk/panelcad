# Outside agents

This page is for an AI that can see a print and return JSON. PanelCAD does not call you, and it does not contain an API key. A person runs the commands below. You read files and write one JSON file.

The drawing file is YAML (`.pcad.yaml`). Do not invent a catalog number, a manufacturer, or a millimetre. If you are not sure, set `needs_info` to true.

## Workflow

1. The person runs:

```
panelcad import prep PRINT.pdf --out agent-folder --dpi 200
```

`PRINT.pdf` can also be a PNG, JPEG, TIFF, BMP, WEBP, DXF, or SVG. `--dpi` is the page PNG resolution for a PDF or other vector page, from 72 to 600. The default is 200. Tiles are crops of that PNG, so they use the same resolution. 200–300 keeps a symbol readable in a crop. A value outside that band, such as 150, still writes the page PNG and the tiles. A scan or photo keeps the DPI stored in the file for millimetres, and the page PNG keeps that file's pixels.

2. Read `agent-folder/README.md`, `agent-folder/manifest.json`, and `agent-folder/schema/agent_response.v1.json`.
3. Look at each `pages/page_NNN.png` and its tiles. Tiles overlap so a symbol on a boundary is in two images. Coordinates in the JSON are millimetres, origin at the top-left of the full page, not pixels and not tile-local.
4. Read `pages/page_NNN.json`. `text` is the extracted vector text or OCR, with `x`, `y`, `w`, `h` in millimetres. `lines` are the extracted strokes. On a vector page, copy tags, wire numbers, and title-block text from that file. On a scan, the page image is the source and the OCR text is only a hint. Read the tag you see. If OCR missed a character, still return the tag; a person confirms it. Do not invent a character you cannot see. If you are not sure, set `needs_info` true.
5. Write one JSON object that matches schema `panelcad.agent-print/v1`. Set `prep_dir` to the folder path.
6. The person runs:

```
panelcad import apply response.json --out job.pcad.yaml
```

`--prep FOLDER` overrides `prep_dir` when you omitted it. The command prints a JSON report: `accepted`, `rejected`, `conflicts`, `needs_info`, and `problems`. Exit code 1 means the file was not applied (`ok` is false). Rejections do not stop a valid file from building; they are listed.

7. Read the project back and fix it with the commands in [Fix the project](#fix-the-project). Then run `check` again.

The same two steps exist on the JSONL agent (`panelcad agent`), which does not open the GUI:

```
{"id": 1, "op": "import_agent_prep", "path": "PRINT.pdf", "out": "agent-folder", "dpi": 200}
{"id": 2, "op": "import_agent_apply", "path": "response.json", "out": "job.pcad.yaml"}
```

## What you return

Schema file: `src/panelcad/importer/agent_response.v1.json` (copied into the prep folder). Version string: `panelcad.agent-print/v1`. Extra fields are rejected. Do not send `width_mm`, `height_mm`, or `depth_mm`.

| Field | Meaning |
| --- | --- |
| `devices[].tag` | Printed device tag, copied exactly. |
| `devices[].symbol` | Symbol type that is drawn: `breaker`, `contactor`, `relay`, `contact`, `coil`, `terminal`, `pushbutton`, `pilot`, `plc`, `drive`, `fuse`, `disconnect`, or another short name. This is not a library id and not a catalog number. |
| `devices[].contact` | `NO`, `NC`, or `""` when the symbol is not a contact. |
| `devices[].x`, `devices[].y` | Position in millimetres on that page. |
| `devices[].sheet` | Page id from `manifest.json`, such as `page_001`. |
| `devices[].rung` | Ladder rung number. `0` when the page has no rung number. |
| `devices[].terminals` | Terminal names printed on this device. |
| `bbox` | On every device, terminal, and wire. Millimetres, origin top-left: `{"x", "y", "w", "h"}`. The wizard crops this box for one-click confirm. |
| `devices[].manufacturer`, `devices[].catalog` | Only when those words are printed. Otherwise `""`. |
| `devices[].confidence` | `0` to `1`. Below `0.6`, the part stays needs-info even if a catalog number is filled in. |
| `devices[].needs_info` | `true` when the part is unknown, unreadable, or you will not name a catalog number. |
| `terminals[]` | `device`, `terminal`, `sheet`, confidence, needs-info. Optional `x` and `y`. |
| `wires[].number` | Printed wire number. Empty only when `needs_info` is true. |
| `wires[].from`, `wires[].to` | `device.terminal`. The first dot splits them: `Q1.2`, `CR1.A1`. A net with three or more terminals is several rows that share one `number`. Each row names two ends. Number `101` on `Q1.2`, `CR1.A1`, and `TB1.1` is one row `Q1.2` to `CR1.A1` and another row `CR1.A1` to `TB1.1`, both numbered `101`. |
| `title_block` | Company, project, title, drawing number, revision, sheet number, sheet count, customer, location, drawn by, date, scale, standard (`""`, `IEC`, or `NEMA/ANSI`). |

Each tile JSON lists the text tokens inside that tile, including the overlap. The tile image is a crop of the page PNG at the `--dpi` above.

Apply counts a wire as connected only when its number is extracted text on a traced conductor and both endpoint symbols touch that conductor. A contact tag must match a coil with the same tag, so a clipped tag is flagged and not renamed. An NO/NC reading that disagrees with another reading, or with a diagonal on the symbol, is needs-info in Problems. The report `counts` are `connected`, `needs_review`, and `rejected`.

On a scan, OCR is weak tag by tag. A tag or wire number the OCR text does not contain is placed and asked as a confirm question. It is not rejected. Vector text stays authoritative: a tag that is not in the vector text is rejected, and that rejection is still a question. A wire that was rejected only because its endpoint was missing is placed when that tag is confirmed.

## Ask a person when something is missing

`import apply` and `panelcad check --json` include `questions`. Each one has `id`, `question`, `bbox` and a `crop` image when the page PNG is still available, `candidates` when a reading is already known, and `changes` (what the answer will do). Unanswered items stay needs-info: no millimetres, off the BOM and the quote. PanelCAD does not pick an answer.

Ask the person, then:

```
panelcad import answer job.pcad.yaml QUESTION-ID VALUE
panelcad import answer job.pcad.yaml QUESTION-ID -- -X2
panelcad import answer job.pcad.yaml --answers answers.json
```

`answers.json` is `[{"id": "...", "value": "..."}]` or `{"id": "value"}`. The reply is the questions still open. Loop until `open` is 0, then run `check` again. A value that starts with `-`, such as an IEC tag `-X2`, is accepted as the answer. `--` before the value also works.

A catalog number is written only when that exact number is in the library, has published dimensions, and the symbol is exactly one known kind that uses that library part. A longer phrase is not a kind. `NO` or `NC` on anything except a pushbutton, selector, or estop is a contact and gets no part. A wire answer is `device.terminal to device.terminal`. A tag already on the project is reused, including a copy from another sheet. A tag that is only in the extracted text becomes a needs-info device and is asked; answer `no` to drop it. A wire number, title-block token, cross-reference list, single letter, or bare number is refused. A tag answer renames the device, or places a needs-info mark when the reading was never placed. If that tag is already on the sheet, the reading is merged into the existing device and the question closes. None of these invent a part.

Example:

```
agent:  panelcad check job.pcad.yaml --json
        questions[0].id = q-tag-page-001-an1
        questions[0].question = What tag is printed here? AN1 is incomplete.
        questions[0].candidates = ["FAN1"]
        questions[0].crop = .../agent-questions/q-tag-page-001-an1.png

agent:  The crop shows a coil whose first letter is under the circle. I read AN1. The text also has FAN1. Which tag is it?

person: FAN1

agent:  panelcad import answer job.pcad.yaml q-tag-page-001-an1 FAN1

agent:  Is PB1 normally open or normally closed? The symbol has a diagonal.

person: NC

agent:  panelcad import answer job.pcad.yaml q-contact-page-001-pb1 NC

agent:  CR1 has no catalog number in the library. What number is printed? Until then it stays off the BOM and the quote.

person: I don't know.

agent:  Leaving CR1 as needs-info. panelcad check job.pcad.yaml --json
        open questions remain until someone names a published catalog number.
```

The import wizard shows the same questions one at a time, with the crop, the candidates, Apply answer, and Skip. Skip does not answer.

A catalog number is placed only when that exact number is already in the PanelCAD parts library, the library row has published width and height, the part's kind matches the symbol, and the number is printed on that device. Otherwise the device is a needs-info mark: library id `reconstruct.needs_info`, no millimetres, `bom: no`. It is left off the BOM and off the quote price. The print stays NOT FOR BUILD until a person assigns a real part.

PanelCAD does not pick a breaker, relay, or contactor from the symbol type or the tag prefix.

## Example

```json
{
  "schema_version": "panelcad.agent-print/v1",
  "prep_dir": "agent-folder",
  "project_name": "Example",
  "title_block": {
    "company": "ACME CONTROLS",
    "project": "Example",
    "title": "Power",
    "drawing_number": "E-100",
    "rev": "A",
    "sheet_number": "1",
    "sheet_count": "1",
    "customer": "",
    "location": "",
    "drawn_by": "",
    "date": "",
    "scale": "",
    "standard": "",
    "confidence": 0.9,
    "needs_info": false,
    "unreadable_fields": []
  },
  "devices": [
    {
      "tag": "Q1",
      "symbol": "breaker",
      "contact": "",
      "x": 40,
      "y": 30,
      "sheet": "page_001",
      "rung": 1,
      "confidence": 0.95,
      "needs_info": false,
      "manufacturer": "Allen-Bradley",
      "catalog": "1489-M1C010",
          "terminals": ["1", "2"],
          "bbox": {"x": 36, "y": 22, "w": 16, "h": 20}
        },
    {
      "tag": "CR1",
      "symbol": "relay",
      "contact": "NO",
      "x": 80,
      "y": 30,
      "sheet": "page_001",
      "rung": 1,
      "confidence": 0.4,
      "needs_info": true,
      "manufacturer": "",
      "catalog": "",
          "terminals": ["A1", "A2"],
          "bbox": {"x": 74, "y": 22, "w": 16, "h": 20}
        }
      ],
  "terminals": [
    {
      "device": "CR1",
      "terminal": "A1",
      "sheet": "page_001",
      "confidence": 0.5,
      "needs_info": true,
      "bbox": {"x": 74, "y": 20, "w": 8, "h": 8}
    }
  ],
  "wires": [
    {
      "number": "101",
      "from": "Q1.2",
      "to": "CR1.A1",
      "sheet": "page_001",
      "confidence": 0.8,
      "needs_info": false,
      "bbox": {"x": 48, "y": 26, "w": 20, "h": 8}
    }
  ]
}
```

`1489-M1C010` is a published Allen-Bradley breaker in the parts library (17.5 × 111 × 69 mm). `CR1` has no catalog number, so it stays needs-info. Use a number only when you can see it on the print and it is the number that was printed.

## Apply report

`accepted` rows were written. `as` is `catalog`, `needs-info`, `pin`, `wire`, or `title`.

`rejected` rows were not written. Typical reasons on a vector page: the tag or wire number is not in the extracted text, the sheet id is not in the manifest, the position is outside the page, or an endpoint names a device that was rejected. Each rejected row is also a question. On a scan, a tag or wire number that OCR did not read is placed for a person to confirm instead of being rejected.

`conflicts` rows disagreed with another reading (two symbol types for one tag, two wire numbers on the same ends, a title field that is not in the text). The kept reading is in `accepted`. The other is not written.

`problems` is the Problems list (the same rows as `panelcad check --json`).

## Read the project

```
panelcad describe job.pcad.yaml --json
panelcad check job.pcad.yaml --json
```

`describe --json` is the project (sheets, components, pins, wires, title block), plus `problems`, `wire_list`, and `from_to`.

`check --json` is `errors` (structural) and `problems` (design findings, including needs-info and evidence misses). Exit code 1 when a structural error or an error-severity Problem is present. Warnings stay in `problems` and the command still exits 0.

## Fix the project

| Command | What it changes |
| --- | --- |
| `panelcad place job.pcad.yaml --lib LIBRARY_ID --ref TAG --x 40 --y 30 --sheet page-001` | Place a library symbol. A layout sheet refuses a part that has no published dimensions. |
| `panelcad wire job.pcad.yaml --from Q1.2 --to CR1.A1 --net 101 --sheet page-001` | Add a wire. Ends are `device.terminal`. |
| `panelcad electrical set-wire job.pcad.yaml --from Q1.2 --to CR1.A1 --number 102 --json` | Set the number on an existing wire. Also `--id w1`, `--color`, `--gauge`, `--type`. |
| `panelcad electrical number-wires job.pcad.yaml` | Renumber. Locked numbers stay. |
| `panelcad library assign job.pcad.yaml TAG CATALOG --manufacturer "Allen-Bradley"` | Assign a published catalog SKU. This is the only way a part gets catalog dimensions. |
| `panelcad rename job.pcad.yaml OLD NEW --sheet page-001` | Rename a tag. |
| `panelcad delete job.pcad.yaml TAG --sheet page-001` | Delete a device and the wires on it. |
| `panelcad electrical wire-list job.pcad.yaml --out wires.csv` | Write the wire list. |

JSONL equivalents, if you are already on `panelcad agent`: `place`, `wire`, `set_wire_spec`, `number_wires`, `assign_part`, `rename_component`, `delete_component`, `design_checks`, `inspect`, `wire_list`.

Sheet ids in the saved project are the page id with underscores turned into hyphens (`page_001` becomes `page-001`). `describe --json` shows the ids to pass as `--sheet`.

## GUI

Import print → check **Use an outside AI agent** → Prepare for an agent. The dialog shows the folder and these instructions. Apply agent JSON picks the file you wrote and opens the project. The vision backend is not used for that path.
