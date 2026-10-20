# Build and install a PanelCAD release

The shop package is **one versioned zip** plus a SHA-256 sidecar. Copy the zip onto the PC. Do not split it, and do not use `Unpack-*.cmd` or Drive `part-*.bin` / `setup-*.bin` files.

The version string comes from `src/panelcad/version.py` (kept in sync with `pyproject.toml`).

## Build

From a checkout with Python 3.11+:

```bash
python scripts/build-release.py
```

That writes:

```
dist/PanelCAD-<version>-setup.zip
dist/PanelCAD-<version>-setup.zip.sha256
```

If `/opt/cursor/artifacts` is writable (Cursor cloud agents), the same two files are copied there so they can be fetched without opening `dist/`.

Windows:

```powershell
python scripts\build-release.py
```

The zip is reproducible for a given commit. Names are sorted, and `.cmd` / `.ps1` files are stored with CRLF. Each zip entry is stamped with the UTC time of the git commit that last changed that file, clamped to `SOURCE_DATE_EPOCH`. When that variable is unset, the clamp is the HEAD commit time (`git log -1 --format=%ct`), not the build machine's clock and not one fixed date for every file. A shared date made an upgrade look unchanged to `robocopy /E`: same-size edits such as `version.py` (`0.3.0` and `0.4.0` are the same length) were skipped. Unchanged files keep their older commit times, so two builds of the same commit still match byte for byte. Generated `INSTALL.txt` uses the clamp time. If git metadata is missing and `SOURCE_DATE_EPOCH` is unset, each file uses its own filesystem mtime.

The archive never includes `PanelCAD-Setup.zip`, `.git`, virtualenvs, or `dist/`. It does include the PanelCAD icon (`src/panelcad/assets/panelcad.svg`, `panelcad.ico`, and the 16–256 px PNGs). The build stops if those files are missing. `Install-PanelCAD.cmd` points the desktop and Start menu shortcuts at `panelcad.ico`.

Set `PANELCAD_RELEASE_BUG_WEBHOOK` to the Slack incoming webhook for `#panelcad-bugs` before building a release that should send bug reports. The value must be `https://hooks.slack.com/services/...`. The build refuses a bot token or any other host, writes the URL into the zip as `src/panelcad/diag/_report_endpoint.txt`, and does not leave that file in the checkout. The file is gitignored. When the variable is unset, the build prints a warning and the zip has no endpoint, so Send saves the report on the computer only. Do not commit the URL.

Set `PANELCAD_RELEASE_SLACK_BOT_TOKEN` to the PanelCAD Bugs bot token so a release can upload the screenshot and the zip. The value must be an `xoxb-…` token. The build writes it into the zip as `src/panelcad/diag/_report_bot.txt` and does not leave that file in the checkout. The file is gitignored. The token is not printed. When the variable is unset, the build prints a warning and the zip has no bot, so Send can still post text through the webhook and Slack does not get the screenshot or the zip.

The token is a dedicated Slack bot, not a user token and not a general shop app. Its only scopes should be `files:write` and `chat:write`, and it should be invited only to the shop bug channel. `files:write` uploads the screenshot and the zip. `chat:write` posts a long report as a threaded reply, because `files.completeUploadExternal` does not document a 40,000-character `initial_comment` and that method can be called only once. `PANELCAD_RELEASE_SLACK_CHANNEL` is required when the bot token is set. It is the Slack channel id, not a name. The embedded bot is pinned to that id. A channel saved in Settings, or `PANELCAD_SLACK_CHANNEL`, does not move it, so a leaked build cannot be pointed at another channel. A bot token placed in `PANELCAD_RELEASE_BUG_WEBHOOK` is still refused.

Shop builds read these values from the environment. If a variable is unset, the build fills it from `%USERPROFILE%\.panelcad\release.env` and then `release.local.env` in the repo root (see `release.local.env.example`). Both files are gitignored. Do not commit them. A public build (`python scripts/build-release.py --public`, or `PANELCAD_PUBLIC_BUILD=1`) does not read those files and does not embed a webhook, a bot, or a feed. Sending and the update checker stay off until someone configures them on that computer.

At runtime the bot token is `PANELCAD_SLACK_BOT_TOKEN`, then the token saved in Settings, then this embedded token. The webhook remains the text fallback when the upload does not post. If the screenshot was uploaded and the zip was not, that fallback text says the zip was not uploaded.

If a build leaks, rotate the token before the next shop install. Open the PanelCAD Bugs app at `api.slack.com/apps`. Keep the bot scopes at `files:write` and `chat:write` only. Revoke the current bot token and reinstall the app to the workspace so Slack issues a new `xoxb-…` token. Invite the bot only to the shop bug channel and remove it from every other channel. Rebuild with `PANELCAD_RELEASE_SLACK_BOT_TOKEN` set to the new token. Do not commit the token. Upgrade the shop PCs. A revoked token makes an older install fall back to the webhook text until that PC is upgraded.

Set `PANELCAD_RELEASE_UPDATE_FEED` to an https URL before building a release that should check for updates on its own. A Google Drive direct link (`https://drive.google.com/uc?export=download&id=<id>`) is a valid feed. The build writes that URL into the zip as `src/panelcad/update/_release_feed.txt` and does not leave the file in the checkout. The file is gitignored. When the variable is unset, the build prints a warning and the zip has no built-in feed. Do not commit the URL. At runtime the feed is `PANELCAD_UPDATE_FEED`, then the saved Help > Updates value, then this built-in URL.

## Install or upgrade on Windows 10/11

Existing drawings and the Python environment stay put. The program lives at `%USERPROFILE%\PanelCAD`. The venv stays at `%USERPROFILE%\.venvs\panelcad` (off Google Drive). Extra files already in the install folder that this package does not ship are left alone. Keep your jobs as `*.pcad.yaml` in Documents or another folder you own; do not rely on overwriting `examples\`.

1. Copy `PanelCAD-<version>-setup.zip` onto the PC (USB, share, or browser). One file is enough.
2. Optional but recommended: check the hash in PowerShell

   ```powershell
   Get-FileHash -Algorithm SHA256 .\PanelCAD-0.5.0-setup.zip
   Get-Content .\PanelCAD-0.5.0-setup.zip.sha256
   ```

   The hex strings must match.
3. Right-click the zip → **Extract All**.
4. Open the `PanelCAD-<version>` folder that appears.
5. Double-click `Install-PanelCAD.cmd`.

First install downloads Python 3.12 from python.org only if this PC has no Python 3.11+. Then it `pip install -e` into the venv and creates a desktop shortcut named **PanelCAD**.

Upgrade is the same double-click. Close PanelCAD first so Windows is not holding the program files open, and run `Install-PanelCAD.cmd` from the newly extracted folder (not from the older copy already in `%USERPROFILE%\PanelCAD`). The installer overwrites program files even when the size and timestamp match, deletes `__pycache__` and stale `.pyc` files under that install's `src`, `tests`, and `docs` only, and re-runs `pip install -e`. It does not delete the venv, drawings, extra files outside those three trees, or `%USERPROFILE%\.panelcad`. An existing example AI journal (`examples\*.pcad.ai.jsonl`, plus the inbox and prompt journals beside those examples) is left as the shop left it. A first install still receives one if the package ships it.

After pip install, the installer runs `python -c "import panelcad; print(panelcad.__version__)"` and stops if that is not this release.

The window should open on `examples\dol_starter.pcad.yaml`. Use **File → Open** for your job.

Unattended install (no `pause`, and the drawing window is not opened):

```bat
Install-PanelCAD.cmd /quiet
```

or set `PANELCAD_INSTALL_QUIET=1` before running the installer. A missing desktop shortcut is created when that desktop belongs to this profile. An existing shortcut is updated only when its working directory is this install folder. A shortcut with an empty working directory, or one that points somewhere else, is left alone, and a side-folder install does not rewrite the interactive user's Desktop shortcut. If that decision fails, the installer leaves the shortcut unchanged and still finishes. When the program folder already exists, the installer copies it once with `scripts/install_tree.py` to `%USERPROFILE%\.panelcad\backups\PanelCAD-<old-version>-<timestamp>` and writes `%USERPROFILE%\.panelcad\rollback.json` before replacing program files. Older backups are removed so only the newest few remain (the Updates setting `keep_backups`, default 3). That record is what **Help > Roll back to previous version** restores: `src`, `scripts`, and top-level packaging files such as `pyproject.toml` are copied back in place, then the venv is reinstalled. The install folder is not renamed, so drawings saved inside it stay. PanelCAD's updater makes the same copy itself and sets `PANELCAD_BACKUP_DONE=1`, so the installer does not take a second one. Drawings, `%USERPROFILE%\.panelcad` (settings, logs, and the user part library), and `%USERPROFILE%\.venvs\panelcad` are not deleted.

## Updates

A shop PC checks a **release feed** you configure, including a file in a synced folder. With no feed, the shop checker stays off. A public build (`python scripts/build-release.py --public`) does not embed that feed. Its update option is the public GitHub Releases page: Check for updates reads the latest release, and Install now verifies the sha256 before anything runs. The repository is `merchantg-cmyk/panelcad` (`PUBLIC_GITHUB_REPOSITORY` in `src/panelcad/update/github_releases.py`). Releases are [github.com/merchantg-cmyk/panelcad/releases](https://github.com/merchantg-cmyk/panelcad/releases). `PANELCAD_GITHUB_REPOSITORY=owner/name` overrides that default.

The feed is one JSON file, `panelcad-feed.json` (or any path you set). It can be an `https://` URL or a file, UNC, or synced-folder path. A shop PC can point at a file in a synced folder, or at any other path you choose. The feed is not hardcoded.

```json
{
  "version": "0.5.0",
  "release_date": "2026-10-08",
  "notes": "What changed.",
  "download": "PanelCAD-0.5.0-setup.zip",
  "size": 12345678,
  "sha256": "64 hex characters",
  "min_version": "0.4.0"
}
```

`download` is an https URL, a drive or UNC path, or a path relative to the manifest (the usual case when the zip sits next to the JSON in a synced folder). `min_version` is the oldest installed version that can apply this package. Omit it, or leave it empty, when any older install may upgrade.

Set the feed in **Help > Updates**, or set `PANELCAD_UPDATE_FEED` to the same URL or path. The environment variable wins when it is non-empty. A saved setting wins over the URL embedded at build time with `PANELCAD_RELEASE_UPDATE_FEED`. On a shop build, with none of those, the checker stays off. On a public build, with none of those, Check for updates uses the public GitHub Releases option. `version` and `min_version` must be PEP 440 (for example `0.5.0`). A bad value is an error, not a traceback. `release_date` is `YYYY-MM-DD` or empty.

A feed URL or a `download` URL may be a Google Drive direct download (`https://drive.google.com/uc?export=download&id=<id>`). PanelCAD follows Drive's redirects and one HTML confirmation page. If the response is still an HTML page, the update stops and the file is not kept. The zip is still checked against the sha256 in the feed.

PanelCAD can check at startup (at most once a day; the checkbox turns that off) and from **Help > Check for updates**. The check and the install run off the drawing thread, with a progress dialog and Cancel. The dialog shows the version, the release date as plain text, the download size, and the notes. **Install now** downloads or copies the zip for the version and sha256 that dialog showed, checks the size and sha256, and refuses the file if they do not match or if the feed changed after the dialog opened. It asks you to save unsaved drawing changes first and stops if you cancel. It then closes PanelCAD and runs `Install-PanelCAD.ps1 -Quiet` (the same unattended switch as `Install-PanelCAD.cmd /quiet`). The helper's working folder is the data directory, not the program folder, and its log is `%USERPROFILE%\.panelcad\logs`. That log keeps a `working folder` line ahead of the installer transcript. Check, install, and rollback also write those paths to the rotating PanelCAD log, with the home directory redacted the same way a save is. The program files are copied with `scripts/install_tree.py`. The next launch shows **Updated to X**. If the install or roll back fails, the next launch shows **rollback/update failed: see log**. **Later** does nothing. **Skip this version** remembers that version and stops prompting for it. Nothing installs unless someone clicks **Install now** or, outside the GUI, passes `--confirm`. An AI inbox line with `confirm: true` opens the same dialog; it does not install by itself. Save As starts in `Documents\PanelCAD`.

Publish a feed after `scripts/build-release.py` has written the zip. `--feed` may be the manifest file or the folder that should hold `panelcad-feed.json`. A folder feed also receives a copy of the zip, and `download` is the file name so a synced copy of that folder still resolves.

```bash
panelcad release publish-feed \
  --zip dist/PanelCAD-0.5.0-setup.zip \
  --feed /path/to/synced/PanelCAD \
  --notes "What changed." \
  --min-version 0.4.0
```

`--download-url` writes an https URL into `download` instead of the file name. The sha256 and size still come from `--zip`. Use it when the zip is fetched from Google Drive or another https host:

```bash
panelcad release publish-feed \
  --zip dist/PanelCAD-0.5.0-setup.zip \
  --feed /path/to/synced/PanelCAD \
  --download-url "https://drive.google.com/uc?export=download&id=<id>" \
  --notes "What changed."
```

Windows:

```powershell
panelcad release publish-feed --zip dist\PanelCAD-0.5.0-setup.zip --feed "$env:USERPROFILE\PanelCAD-feed" --notes "What changed."
```

The same actions are on the agent: JSONL `update_check`, `update_install` (`confirm` must be JSON `true`; `dry_run` verifies and returns the handoff without running it), and `update_rollback`. CLI: `panelcad update check`, `panelcad update install --confirm`, `panelcad update rollback --confirm`.

## Tests (local)

```bash
python -m pip install -e ".[dev]"
./scripts/check
```

Windows: `scripts\check.ps1`. Both set `QT_QPA_PLATFORM=offscreen` and run the full pytest suite.

## CI on this Origin repo

Cursor Origin can **display** check runs that a connected CI app posts (Depot CI, or anything that uses the Origin Checks API). It does **not** execute `.github/workflows` by itself.

This repository has no Depot (or other) CI app connected. The previous Origin PR reported **no checks**. Adding workflow YAML does not make a green badge appear.

Workflow files are still in the tree so they are ready when a runner is connected:

- `.github/workflows/ci.yml` — GitHub Actions syntax (checkout, Python 3.12, `scripts/check`)
- `.depot/workflows/ci.yml` — same job, which [Depot CI](https://depot.dev/docs/ci/integrations/origin) reads after the first merge to the default branch **and** after the Depot app is installed on the Origin namespace

Until then, the command that actually runs the suite is `scripts/check` (this environment, a shop PC, or a cloud agent).
