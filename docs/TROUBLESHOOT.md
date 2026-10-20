# Troubleshooting export (`troubleshoot.json`)

Open, read-only document any program can read. PanelCAD writes it. It is not stored inside `.pcad.yaml`.

A package export (`panelcad` export package, `Session.export_package`) includes `troubleshoot.json` next to `project.pcad.yaml`.

Readings taken on the floor are a separate file beside the project. `pump.pcad.yaml` writes `pump.troubleshoot-log.json`. Appending a reading does not modify the project file.

## Document

| Field | Meaning |
| --- | --- |
| `format` | `panelcad-troubleshoot` |
| `schema` | `1.0` |
| `generator` | PanelCAD version that wrote the file |
| `project` | Project name |
| `drawing_number` | Title-block drawing number |
| `read_only` | Always true |
| `safety` | Lockout, NFPA 70E / arc-flash, drawing disclaimer, and the rules for safety devices, overloads, and PLC points |
| `rules` | How expected values, citations, safety, and the log file work |
| `symptoms` | Symptom ids the query API accepts |
| `ops` | Read-only query names |
| `devices` | One row per schematic symbol: `tag`, `type`, `name`, `catalog` (part number when the drawing has one), `library_id`, `page`, `zone`, `page_zone`, `sheet`, `pins`, `safety` |
| `nets` | One row per conductor net: `id`, `name`, `wire_no`, `wire_numbers`, `voltage` (object or null), `rail`, `endpoints` (`tag.pin` or a rail name), `terminals` (`tag:pin`) |
| `cross_refs` | Coil and contact families already on the drawing |
| `plc_io` | Address, point label, tag, direction, module, device, wire or net, sheet. From the PLC I/O list |
| `safety_devices` | Tags flagged as safety devices |
| `plc_code_refs` | Always `[]` in this version. A PLC program is not part of the drawing |
| `needs_info` | Nets with no voltage on the drawing, plus the missing PLC program |

`voltage` is present only when the drawing states it: a net name, a wire label, a wire number that is a voltage name, a rail name, or a supply output property on that net. Conflicting numbers, or mixed AC and DC marks, are omitted and listed under `needs_info`. Nothing in this file is a guessed voltage.

## Query API

Same payload from `Session`, JSONL `panelcad agent`, the drawing agent, and `panelcad electrical troubleshoot`. Every reply includes `safety` and `read_only: true`.

| Op | CLI | Arguments |
| --- | --- | --- |
| `circuit_for` | `electrical troubleshoot circuit --ref` | Device tag |
| `checkpoints` | `electrical troubleshoot checkpoints --ref --symptom` | Device tag and symptom |
| `upstream` | `electrical troubleshoot upstream --endpoint` | `ref.pin` |
| `downstream` | `electrical troubleshoot downstream --endpoint` | `ref.pin` |
| `plc_point` | `electrical troubleshoot plc-point --address` | Address, point label, or tag on the drawing |
| `likely_causes` | `electrical troubleshoot causes --ref --symptom --readings` | Optional readings JSON |
| `troubleshoot_guide` | `electrical troubleshoot guide --ref --symptom --readings` | Same walk the Troubleshoot dock shows |

## Dock

Tools → Troubleshoot (and View → Troubleshoot) is the guided walk for a person. Pick a symptom and a device tag from the cross-reference index. The checks follow the wires on the drawing. OK, Wrong, and Skipped move the next check. A Wrong reading limits the fault to the path between the last OK and that check, and the sheet highlights that path.

Save log appends the sidecar. Export CSV and Export PDF write the job-file copy (`electrical troubleshoot session-csv` and `session-pdf`). Those files are not stored in the project YAML.

Explain this step calls the account in Settings > AI. PanelCAD does not include a key. A reply that leaves the drawing, or that suggests jumpering, bypassing, forcing, defeating, or overriding a device, is discarded.

PLC program import is not part of this dock. `plc_code_refs` stays empty.

Symptoms: `motor_wont_start`, `motor_trips`, `wont_stop`, `no_control_power`, `plc_input_not_seen`, `plc_output_on_load_off`, `vfd_faulted`.

`checkpoints` lists checks in trace order. Each check has `energized` of `LIVE` or `DE-ENERGIZED`, a terminal, a wire number when the drawing has one, meter leads, and `expected`. `expected.needs_info` is true when the drawing does not state the value.

`likely_causes` ranks plain-language causes. A reading whose `result` is `wrong` narrows the list to the segment between the last `ok` and the first `wrong`. Results are `ok`, `wrong`, or `skipped`.

`export_troubleshoot` (JSONL) and `electrical troubleshoot export --out` write this document.

`append_troubleshoot_log` and `electrical troubleshoot log` append one entry to the sidecar. The sidecar `format` is `panelcad-troubleshoot-log`, `schema` `1.0`.

## Safety

Every flow starts with qualified-person, lockout/tagout, and NFPA 70E arc-flash text, and says the drawing may be out of date.

Checks are `DE-ENERGIZED` (continuity, after lockout and a zero-energy check) or `LIVE` (voltage).

Answers do not tell anyone to jumper, bypass, force, defeat, or override an E-stop, safety relay, guard, interlock, overload, or PLC point. A safety device that is open is reported as: find why the safety circuit is open and reset it per the machine's procedure. An overload uses the same form. A PLC point says to confirm the program and leave the point in its normal state.

Cite only wire numbers and terminal ids that appear in the document or on the drawing. An unknown device, symptom, terminal, or PLC point is an error, not a guess.

## Log file

```json
{
  "format": "panelcad-troubleshoot-log",
  "schema": "1.0",
  "project": "job.pcad.yaml",
  "entries": [
    {
      "id": "…",
      "when": "2026-10-14T00:00:00+00:00",
      "who": "",
      "ref": "M1",
      "symptom": "motor_wont_start",
      "readings": [{"id": "…", "result": "wrong"}],
      "note": ""
    }
  ]
}
```
