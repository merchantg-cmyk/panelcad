# Shared folders

Several people can open one PanelCAD project that lives in a synced folder:
Google Drive for desktop (this shop's `G:` drive), OneDrive, or Dropbox.
There is no server, no cloud account, and no API key. The synced folder is the
only shared state. YAML stays the project file.

This is a safe first version. It stops two saves from silently destroying each
other. It does not merge two unsaved drawings, and it does not show a live cursor.

## Same version on every PC

Everyone who opens a project in a shared folder must be on this build.
PanelCAD 0.5.6 opens these files, but it ignores the lock and the hash
check. A save from 0.5.6 also drops `activity` and `last_saved`. Mixing
0.5.6 with this build in one folder can overwrite a drawing with no warning.

## Lock

Opening a project in a synced folder writes a lock beside it:

```
line.pcad.yaml
line.pcad.lock.json
```

The lock records the user, the PC, the process, and the time it was taken and
last refreshed. The open window refreshes it about once a minute. Closing the
window deletes it.

Someone else who opens the project sees who holds it. They can look at the
drawing read-only, or take over the lock after a warning. Taking over does not
pull in the other person's unsaved work. A later save from the PC that lost
the lock is refused when the file on disk has changed.

A lock goes stale in two ways:

- On the same PC, the process id no longer matches a running process (crash or
  a killed window). The next open replaces that lock immediately. That is the
  crash recovery path. The crashed process cannot delete the file itself.
- On any PC, the heartbeat is older than 20 minutes. A laptop that slept, or a
  crash on a machine we cannot see, expires this way. Clocks should be within
  a few minutes of each other.

A folder is treated as synced when a path part is `Google Drive`, `My Drive`,
`GoogleDrive…`, `OneDrive…`, or `Dropbox`, when the path is on Windows `G:`,
or when `.panelcad-share` sits in the project folder or an ancestor. That
marker is how a test, or a sync client with an unusual path, opts in. A normal
local folder does not get a lock file.

The lock is advisory. A program that is not PanelCAD can still write the YAML.
The hash check below catches that the next time PanelCAD saves.

## Conflict check

Opening a project remembers a SHA-256 of the file. Before a save to that same
path, PanelCAD hashes the file again, retrying for the same span as a busy
replace. If the file still cannot be read, the save does not write and the
window says the file is busy. An unreadable file is not treated as unchanged.
If the hash differs, the save does not write. The timer that notices an
outside save tries that read once. If a sync client still holds the file, that
tick is skipped and the window does not say the file changed. The next tick
tries again.
The window offers:

- **Reload** — drop the unsaved edits and open what is on disk.
- **Save a copy** — write this session to a new file. The other save stays.
- **Review differences** — a text diff of the disk copy and this session.

A clean drawing still reloads when the file changes, which is how the AI inbox
already picked up an outside save. A drawing with unsaved edits does not reload
on its own. **File → Refresh from disk** (F5) reloads on purpose. Read-only
viewers use it to pick up someone else's save. Unsaved edits ask before they
are discarded.

Sync clients leave extra copies next to the project. PanelCAD warns when it
sees a sibling whose name is the project file plus a `conflict` note, a
` (1)` / ` (2)` suffix, or a leading `Copy of`. Examples:

```
line (1).pcad.yaml
line.pcad (1).yaml
line (conflicted copy).pcad.yaml
Copy of line.pcad.yaml
```

The warning does not delete those files and does not pick one of them to open.

## Atomic save and backups

A save writes `.<filename>.partial-<pid>-<token>` in the same folder, flushes
it, then `os.replace`s it onto the project. Replace is atomic on one drive, so
a reader never sees a half-written YAML. The temp name is not a project file.
If the sync client has the destination open, the replace is retried for a
little over eight seconds (NTFS denies the replace with WinError 5 for the
whole time another handle is open, even when that handle shares read and
write). If it still fails, the original project is unchanged and the temp file
is removed. The window says the file is busy and leaves the drawing unsaved.
A hash that still cannot be read is the same message. Anything else — a
folder PanelCAD cannot write, a path that is not a folder, a full disk, a
name that is too long, or a missing drive — says it can't save there.
Nothing is overwritten, and a failed Save As leaves the session on the
file it already had.

Before that replace, the previous bytes are copied to:

```
.line.pcad.yaml.backups/20261016-074112.pcad.yaml
```

That copy uses the same pauses. A program that has the project open without
share-read used to fail the save at once. If the copy still fails after the
retries, the backup is skipped, a warning is written to the PanelCAD log, and
the save continues. The project file is not removed until the replace works,
so a skipped backup does not discard the current YAML.

While those retries run, the window shows a wait cursor and "Saving…" in the
status bar. The lock file beside the project uses a shorter retry (a few
tenths of a second) so the heartbeat does not freeze the window.

The newest 30 copies are kept, ordered by the timestamp in the name and then
the numeric suffix (`-2` before `-10`, and a name with no suffix before `-1`).
Sorting the filenames would drop a newer same-second copy and keep an older
one. A second save in the same second is `20261016-074112-1.pcad.yaml`.
Recovery files under PanelCAD's own recovery folder are not backed up again.
A package export does not drop a backup folder into the package.

## Who saved

Each save stores `last_saved` and appends `activity` in the project YAML:
user, PC, time, and a short summary (`saved`, `saved a copy`, `took over the
lock from …`). Empty values are left out, so a project that has never been
saved with this build stays the same shape. The list keeps the newest 100
rows. Customer revision snapshots do not copy this log; a rev cloud is about
the drawing, not about who pressed Save.

The status bar shows who last saved and when. **File → Project activity…**
lists the rows. A part that still needs info does not block the save, and the
same file opens again with that part unresolved.
