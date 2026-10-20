# Contributing

This file is a draft for a future public repository. It is not a promise that
the project is already open for contributions. The license is the GNU AGPL,
version 3 or later (`LICENSE`).

## What belongs here

PanelCAD is YAML-first CAD for industrial control panels. Changes should
keep the GUI, the CLI, and the JSONL agent on the same file format.

Do not add:

- API keys, webhooks, bot tokens, or update-feed URLs
- Customer names, customer drawings, panel photos, or job-specific IP plans
- Machine paths that include a person's username
- A license other than the one the copyright holder puts in `LICENSE`

Shop release values go in `release.local.env` or
`%USERPROFILE%\.panelcad\release.env`. Copy `release.local.env.example`.
Public packages are built with `python scripts/build-release.py --public`.

## Develop

Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
./scripts/check
```

On Windows, `scripts\check.ps1`. Both set `QT_QPA_PLATFORM=offscreen`.

`examples/dol_starter.pcad.yaml` is the generic project to open. Customer
examples are not part of a public tree. Tests that need them skip when the
files are absent.

## AI keys

Use your own key. `PANELCAD_AI_API_KEY`, `PANELCAD_AI_PROVIDER`, and
`PANELCAD_AI_MODEL` override Settings → AI. Tests use a scripted model and
do not call a network.

## License headers

PanelCAD is `AGPL-3.0-or-later`. A new source file may start with:

```
# SPDX-License-Identifier: AGPL-3.0-or-later
```

That line points at `LICENSE`. Do not mass-edit headers onto existing files.

## Reports

Security reports go through `SECURITY.md`. Other bugs can use the issue
tracker once one exists. Do not attach a customer drawing to a public issue.
