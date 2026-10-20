# From-to wire list and wire markers

The from-to report is one row per conductor. The rows are the wires already in the netlist (`wire_networks`). A contact is not a junction, so each side of a contact is its own row and its own wire number. This report does not assign numbers. The legacy wire list (`export_wire_list`, device and terminal columns) is unchanged.

Columns: `wire_number`, `from`, `to`, `from_sheet`, `from_line`, `to_sheet`, `to_line`, `from_column`, `to_column`, `size`, `color`, `type`, `crossing`.

`from` / `to` are `tag:pin` (`M1:A1`) or `strip:terminal` (`X1:4`). A terminal end uses the strip and terminal number, not the symbol pin A/B. A rail name (`24V`, `0V`, `L1`) is not an end: the device and terminal pins on that net are daisy-chained in sheet order, then down the rung (`PS1:minus` → `M1:A2` → `M2:A2`), through a strip terminal when one is on the net. A drawing junction or off-page arrow is not an end either; the row connects the pins on either side. `crossing` is the `strip:terminal` a conductor lands on (`X1:4`). The cell is filled whether or not the wire number changes. A row that does not land on a strip terminal leaves it blank. `from_line` / `to_line` are the NEMA line (`sheet*100 + rung`). `from_column` / `to_column` are the IEC zone column.

Sort `number` (numeric, then IEC `d-d`, then text; blanks last) or `device` (from tag and pin, then to, then number).

Wire numbers come from the drawing. This report does not assign them. A terminal is not a junction, so each side is its own network unless the wires share a net name. A 24V feed-through (`net` `24V` on both sides) keeps `24V` on the field wire. A motor lead on a different net takes the next sheet/line number.

Size, color, and type, per field: the wire, then `net_wire_specs` for that net, then the `control` or `power` entry, then `*`. An unknown gauge is `TBD`. The report does not substitute a default size. Color and type still fall back when those fields are blank: control `BU` / `MTW`, power `BK` / `MTW` (a line or motor lead: wire number or net `L1`/`L2`/`L3`/`T1`/`T2`/`T3`/`U1`/`V1`/`W1`, or a device pin `L1`–`L3`, `T1`–`T6`, `U`/`V`/`W` and the numbered forms). The power color is black until phase colors are added; `default_power_color` is that hook. A PSU `plus`/`minus` pin stays control. The CSV and the print-package page both show the filled value. `type` is MTW, THHN, and the like. It is not drawn on the sheet. Empty fields are omitted from `.pcad.yaml` — `TBD` and the color/type defaults are not written back.

GUI: **Reports → Wire List (From-To)…**. Right-click a wire for **Set size, color, type…**. **Reports → Net wire defaults…** edits one net (or `*`). `all` is the same fallback key. The print package section is **Wire List (From-To)**, after the legacy wire list.

JSONL: `wire_list` with `from_to=true` (or `report=from_to`) returns and, with `path`, writes this report. Without that flag, `rows` stays the legacy list and `from_to` is still returned. `set_net_wire_spec` args: `net`, `size?`, `color?`, `type?`. `help` includes `schemas.wire_list`.

The drawing-agent tool `wire_list` always requests the from-to report (`path`, `sort`).

CLI: `panelcad electrical from-to JOB.pcad.yaml --out from_to.csv --sort number` and `panelcad electrical net-spec JOB.pcad.yaml --net RUN --size 18 --color BU --type MTW`. `--net *` or `--net all` sets the fallback (stored as `*`).

## Wire markers

One printed marker per conductor end. The marker count is twice the from-to rows. Two conductors that land on the same pin (`OL1:T1` from `M1:T1` and from `M2:T1`) print two markers there. A junction dot, an off-page arrow, or a rail name is not an end. Ends with no number are skipped (`blank_ends`). Every marker has a device and a pin.

`style` is `heat-shrink` or `clip-on`.

GUI: **File → Export → Wire Markers…** (target, style, sort). JSONL `export_wire_markers` (`path` required). Drawing-agent tool of the same name. CLI: `panelcad electrical wire-markers JOB.pcad.yaml --out markers.csv --target generic --style heat-shrink`.

`help` includes `schemas.export_wire_markers` with `doc_urls`. The result's `confidence` is `documented`, `unverified`, or `panelcad`.

### Generic CSV (`confidence: panelcad`)

Header, comma-separated:

`text,end,device,pin,sheet,location,size,color,type,style`

`text` is the wire number. One row per end.

### Phoenix Contact — documented (`phoenix-single-column`)

PROJECT complete (the marking software that followed CLIP PROJECT) and the MARKING system data exchange. The 2020 guide and the 2024 US update both say: "It is important to input the data into a single column of Excel." No header name is published. A later brochure allows more than one column and still does not name them. Heat-shrink versus clip-on is the material chosen after **Material mapping → Enforce default material**, not a column in the file.

This target is one column, no header, one wire number per row.

* https://assets.phoenixcontact.com/file/3706e4b1-1fb7-47fd-aedc-d04ae95cf8cb/media/original?Project_Complete_Marking_guide_Final_2020.pdf
* https://assets.phoenixcontact.com/file/3a3ca93e-cae1-45d4-8444-7e49ecedddd0/media/original?US_Project-Complete-Marking-Guide-updates_20241017_U007204A__1_.pdf
* https://assets.phoenixcontact.com/file/f45d95ab-b61d-43c1-a2ee-bce42edd215e/media/original
* https://www.phoenixcontact.com/en-gb/products/software-marking-system-software-1669297

### Weidmüller M-Print PRO — unverified header

The user manual, Importing Files, steps 4/5 and 7: pick a delimiter, then field mappings send source columns onto element fields (`MARKER`, `TEXTBOX1`, …) that belong to the marker type already chosen. No fixed wire-marker CSV header is published. The WMC file `article;designation;marker` is a terminal-rail import, not this marker file.

`target=weidmueller` writes the generic CSV. Map `text` in the wizard and pick heat-shrink or clip-on there.

* https://assets.dam.weidmueller.com/assets/api/e0a21ebb-a3e5-4a2c-8114-c954d403b63f/Original/User_Manual_EN.pdf
* https://www.weidmueller.com/int/service/engineering_data.jsp

### Brady Workstation / LabelMark — unverified header

Brady's import notes: the first row is user-named field names, each later row is one label, and a `.csv` file uses commas (Advanced Import / Data Import in LabelMark 6). Brady does not publish a wire-marker column list.

`target=brady` writes the generic CSV. Map `text` onto the label and choose the material in the template.

* https://support.bradyid.com/articles/en_US/Knowledge/How-to-format-Excel-files-for-importing-into-Brady-software
* https://support.bradyid.com/articles/en_US/Knowledge/How-Do-I-Import-Data-From-a-CSV-File-into-Brady-Workstation
* https://support.bradyid.com/s/article/Data-Import-in-LabelMark-6
