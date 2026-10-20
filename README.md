# PanelCAD

CAD for **industrial control panels**: power distribution, PLCs, HMIs, relays, contactors, terminals, VFDs, and enclosure layout.

It is meant to feel like KiCad, but for control-panel work rather than PCBs.

- **Windows and Linux**
- **GUI** for people (Qt / PySide6)
- **YAML + CLI + Python API + JSONL agent** for AI and scripts
- **Multi-page** print sets with title blocks
- **Import/export package** (project, library, BOM, wires, SVG, PDF, DXF)
- **Print package** — one vector PDF: cover, drawing index, revisions, every sheet at its real size, then BOM / wire list / terminals / design checks (`File → Print Package…` or `panelcad package --pdf`)
- **Electrical reports** — wire numbers, live coil/contact cross-reference (NEMA line numbers, IEC contact mirror), terminal strips, PLC I/O, schematic/panel audit, live design checks. Vendor strip files: Weidmüller WMC CSV (documented import), Phoenix / WAGO CSV fallback, plus interchange XML. See `docs/VENDOR_STRIP_EXPORT.md` and `docs/AUTODESK_ELECTRICAL.md`.
- **Troubleshoot** — Tools → Troubleshoot walks a fault from the tags, wire numbers, and cross-references already on the drawing. The same walk is `panelcad electrical troubleshoot guide` and JSONL `troubleshoot_guide`. Explain uses the account in Settings > AI. See `docs/TROUBLESHOOT.md`.
- **Manufacturer catalog parts** — assign a verified SKU (Allen-Bradley, Siemens, Eaton, Schneider) onto a generic symbol. Format: `docs/PARTS.md`.
- **Reconstruct from photos** — phone shots of a **physical** panel (perspective, glare, clutter) become a starting layout and a floor checklist to rebuild missing or wrong prints. Old drawings / scans are a secondary cross-check in the same project (print-import pipeline when present). Tools → Reconstruct from photos…, `panelcad reconstruct`, JSONL `reconstruct_*`.
- **Bug reports** — Help → Report a bug, Edit → Settings → Bug reports, `panelcad bug-report`, and `Session.report_bug`. Logs live in the per-user PanelCAD folder (`panelcad logs --path`). Sending stays off until you save a webhook or bot token. A public build does not include one. A shop build can embed a bot that is pinned to the channel chosen at build time. Autosave interval (seconds, `0` off) is in that Settings page, `panelcad settings`, and JSONL `settings`.

The YAML file **is** the project. The GUI, the CLI, and an AI agent all read and write the same format.

Source: [github.com/merchantg-cmyk/panelcad](https://github.com/merchantg-cmyk/panelcad). Releases: [github.com/merchantg-cmyk/panelcad/releases](https://github.com/merchantg-cmyk/panelcad/releases).

## Install and build from source

Python 3.11 or newer. On Windows, the GUI needs a desktop session. On Linux, install Qt’s platform dependencies the way your distro packages PySide6.

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
./scripts/check             # Windows: .\scripts\check.ps1
panelcad gui examples/dol_starter.pcad.yaml
```

`examples/dol_starter.pcad.yaml` is a generic starter. Your own jobs are YAML files you keep wherever you like.

Bring your own model key. PanelCAD does not ship one. Settings → AI, or the environment:

| Variable | Purpose |
| --- | --- |
| `PANELCAD_AI_API_KEY` | Key for the provider you choose |
| `PANELCAD_AI_PROVIDER` | Provider name accepted by Pydantic AI |
| `PANELCAD_AI_MODEL` | Model string |
| `PANELCAD_AI_ENDPOINT` | Optional base URL |

Local Ollama needs no key. `panelcad ai` defaults to `ollama:gemma3:4b` when Ollama is running on `127.0.0.1:11434`.

A public release zip is `python scripts/build-release.py --public`. That zip does not embed a bug-report webhook, a Slack bot, or a shop update feed. Shop builds that should embed those read them from the environment or from `release.local.env` (see `release.local.env.example` and `docs/RELEASE.md`). Do not commit that file.

Help → Updates on a public build offers download from the [public GitHub Releases](https://github.com/merchantg-cmyk/panelcad/releases). `PANELCAD_GITHUB_REPOSITORY=owner/name` overrides [merchantg-cmyk/panelcad](https://github.com/merchantg-cmyk/panelcad). A shop build keeps the feed built into that release. Nothing installs until you click Install now, and the zip is checked against its sha256. Bug reports stay off until you configure them.

## Shop PCs

An installed shop copy lives at `%USERPROFILE%\PanelCAD`. The desktop shortcut is **PanelCAD**. How to build that zip is `docs/RELEASE.md`. A public checkout does not include shop handoff notes.

## Quick start (developers)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
.\scripts\check.ps1
panelcad gui examples\dol_starter.pcad.yaml
```

Linux: `scripts/setup.sh` then `source .venv/bin/activate` and `./scripts/check`.

## Install or upgrade on a Windows PC

Copy **one** file, `PanelCAD-<version>-setup.zip`, onto the PC. Do not split it and do not run `Unpack-*.cmd`.

1. Right-click → Extract All.
2. Open the `PanelCAD-<version>` folder.
3. Double-click `Install-PanelCAD.cmd`.

Unattended: `Install-PanelCAD.cmd /quiet`, or set `PANELCAD_INSTALL_QUIET=1`. That skips `pause` and does not open the window.

That installs or upgrades `%USERPROFILE%\PanelCAD`, keeps the venv at `%USERPROFILE%\.venvs\panelcad`, and leaves your drawings and extra files alone. The desktop and Start menu shortcuts use the PanelCAD icon. Opening the window shows a splash screen while the parts library loads. How to build the zip, check its SHA-256, and publish an update feed: `docs/RELEASE.md`. On a shop PC, **Help → Updates** points at that feed (an https URL or a file in a synced folder). PanelCAD does not install an update until you click **Install now**.

## Pydantic AI

`panelcad ai` is a typed drawing agent. Its tools call the same operator as the JSONL agent, so a generic 24V box is rejected and wires stay orthogonal. The default model is local Ollama `gemma3:4b`.

```bash
panelcad ai -p examples/dol_starter.pcad.yaml "List the sheets and do not change them."
panelcad ai -p sheet.pcad.yaml --model ollama:gemma3:4b "Place coil CR1 and pilot LT1 and wire CR1.A2 to LT1.X1."
```

Any [Pydantic AI model string](https://ai.pydantic.dev) works with `--model`. Tests use a scripted model and do not need Ollama.

## Scan vision with Ollama

A scanned sheet can be read by a local vision model. This is separate from `panelcad ai`. The Ollama preset calls native `POST /api/chat` (not the OpenAI `/v1` path), sends the detection JSON schema as `format`, and sets `think` to false so a thinking model does not spend the context window on reasoning.

```bash
ollama pull gemma3:4b
panelcad import scan.png --vision-backend ollama --out sheet.pcad.yaml
```

Ollama listens on `http://127.0.0.1:11434`. The GUI is File → Import print…, or Edit → Scan vision…. The model box lists only vision-capable tags (`/api/tags`, then `/api/show` capabilities).

| Model | Disk | VRAM on a 16 GB GPU | Notes |
| --- | --- | --- | --- |
| `gemma3:4b` | about 3 GB | about 8 GB, fits | Default. Text and image. |
| `qwen3-vl:8b-instruct` | about 6 GB | about 16 GB | Non-thinking. Fits when the GPU is otherwise idle. |
| `qwen3.8:27b` | about 18 GB | spills to CPU (about 70% GPU) | Best reader with thinking off. Slow because part of the model sits in RAM. |

Defaults for the Ollama preset: `num_ctx` 16384, `num_predict` 4096, thinking off, one worker, 300 s per request, 3600 s per page. The page clock starts when that page's tiles start. It does not cover the rest of the job. A large local model uses one worker so requests are not queued behind each other. Change them in the GUI, or:

```bash
panelcad import scan.png --vision-backend ollama --vision-model qwen3-vl:8b-instruct --timeout 300 --page-timeout 3600 --num-ctx 16384 --think off
```

`--think on` is for a thinking model when you want the reasoning. Leave it off for `qwen3.8:27b` or the reply is cut off at the context limit. A custom `/v1` server gets `max_tokens` and, when set, `reasoning_effort` (`none`, `low`, `medium`, `high`). Local backends do not show a dollar cost.

## If you were interrupted

1. `CONTEXT.md` — story so far
2. `HANDOFF.md` — how to run and what to do next
3. `docs/AI_INTERFACE.md` — AI inspect/edit/print without the GUI
4. `docs/RELEASE.md` — one-file shop zip and how CI actually runs

## License

PanelCAD is free software under the GNU Affero General Public License, version 3, or any later version published by the Free Software Foundation. The copyright and that grant are in `NOTICE`. The full license text is `LICENSE`. Other people's code, fonts, and icons are listed in `THIRD_PARTY_NOTICES.md`.

If you run a modified PanelCAD as a network service, the AGPL requires you to offer the corresponding source to the people who use that service.

## What to open

| Role | Start here |
|------|------------|
| Human | `panelcad gui examples\dol_starter.pcad.yaml` |
| AI / script | `panelcad inspect --json` or `from panelcad import Session` |
| Shop PC install | `docs/RELEASE.md` |
