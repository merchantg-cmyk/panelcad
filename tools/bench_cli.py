#!/usr/bin/env python3
"""Time one-step PanelCAD CLI commands on the bundled demo projects.

The samples are ``examples/dol_starter.pcad.yaml``,
``examples/vendor_strip/strip_sample.pcad.yaml``, and a synthetic cabinet
photo from ``panelcad.reconstruct.fixtures`` (no customer data). Each command
runs in its own process. Writes stay in a temporary directory.
``PANELCAD_DATA_DIR`` and ``PANELCAD_CONFIG`` point at that directory so a
run does not read or write a user install.

    python tools/bench_cli.py
    python tools/bench_cli.py --runs 5 --json-out /tmp/panelcad-bench.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "dol_starter.pcad.yaml"
STRIP = ROOT / "examples" / "vendor_strip" / "strip_sample.pcad.yaml"
# DOL's layout sheets are not build-ready, so the print package refuses them.
# This bundled catalog sample is the one that writes the package PDF.
CATALOG = ROOT / "examples" / "sample_catalog.pcad.yaml"


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _python() -> str:
    return sys.executable


def _env(work: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PANELCAD_DATA_DIR"] = str(work / "appdata")
    env["PANELCAD_CONFIG"] = str(work / "config")
    env["PANELCAD_RECONSTRUCT_VISION"] = "stub"
    env["PYDANTIC_AI_NO_BANNER"] = "1"
    src = str(ROOT / "src")
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src if not prev else src + os.pathsep + prev
    return env


_WRAPPER = r"""
import resource
import sys

code = 0
try:
    if sys.argv[1] == "--import":
        import panelcad.cli.main  # noqa: F401
    else:
        sys.argv = ["panelcad", *sys.argv[1:]]
        from panelcad.launch import main
        main()
except SystemExit as exc:
    status = exc.code
    if status is None:
        code = 0
    elif isinstance(status, int):
        code = status
    else:
        code = 1
except Exception:
    code = 1
finally:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    sys.stderr.write(f"__BENCH__ {rss}\n")
    sys.stderr.flush()
raise SystemExit(code)
"""


def _run(args: list[str], *, env: dict[str, str], cwd: Path, timeout: float) -> tuple[float, int, int]:
    """Return wall seconds, peak RSS kilobytes, and the process exit code."""

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [_python(), "-c", _WRAPPER, *(args or ["--import"])],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return time.perf_counter() - started, 0, 124
    return time.perf_counter() - started, _parse_rss(completed.stderr), completed.returncode


def _parse_rss(stderr: str) -> int:
    for line in reversed(stderr.splitlines()):
        if not line.startswith("__BENCH__ "):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1])
    return 0


def _capture_startup(env: dict[str, str], cwd: Path, workspace: Path, timeout: float) -> tuple[float, int, int]:
    """Time ``capture-server`` until it prints that it is listening, then stop it."""

    proc = subprocess.Popen(
        [
            _python(),
            "-m",
            "panelcad",
            "capture-server",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--workspace",
            str(workspace),
        ],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    started = time.perf_counter()
    heard = False
    assert proc.stdout is not None
    deadline = started + timeout
    while time.perf_counter() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        if "listening" in line.lower():
            heard = True
            break
    wall = time.perf_counter() - started
    rss = _peak_rss(proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    code = 0 if heard else (proc.returncode if proc.returncode is not None else 1)
    return wall, rss, code


def _peak_rss(pid: int) -> int:
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        if line.startswith("VmHWM:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return 0


def _prepare(work: Path, env: dict[str, str]) -> dict[str, Path]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])
    from panelcad.reconstruct.fixtures import write_backpanel_photo

    paths: dict[str, Path] = {}
    fresh = work / "fresh"
    fresh.mkdir(parents=True, exist_ok=True)
    base = fresh / "base.pcad.yaml"
    subprocess.run(
        [_python(), "-m", "panelcad", "new", "Bench", "--out", str(base)],
        cwd=work,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    photo = fresh / "backpanel.png"
    write_backpanel_photo(photo, seed=7)
    added = fresh / "with-photo"
    if added.exists():
        shutil.rmtree(added)
    added.mkdir()
    project = added / "job.pcad.yaml"
    shutil.copy2(base, project)
    subprocess.run(
        [_python(), "-m", "panelcad", "reconstruct", "add", str(project), str(photo), "--role", "backpanel"],
        cwd=work,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    paths["base"] = base
    paths["photo"] = photo
    paths["with_photo"] = added
    return paths


def _cases(work: Path, prepared: dict[str, Path]) -> list[tuple[str, str, list[str] | None]]:
    """Name, kind, argv. kind is ``cli``, ``import``, or ``capture``."""

    out = work / "out"
    out.mkdir(parents=True, exist_ok=True)
    demo = str(DEMO)
    strip = str(STRIP)
    return [
        ("import cli.main", "import", None),
        ("version", "cli", ["version"]),
        ("help", "cli", ["--help"]),
        ("new", "cli", ["new", "Demo", "--out", str(out / "new.pcad.yaml")]),
        ("validate", "cli", ["validate", demo]),
        ("check", "cli", ["check", demo]),
        ("describe --json", "cli", ["describe", demo, "--json"]),
        ("inspect --json", "cli", ["inspect", demo, "--json"]),
        ("sheets", "cli", ["sheets", demo]),
        ("print svg (one sheet)", "cli", ["print", demo, "--svg", str(out / "power.svg"), "--sheet", "power"]),
        ("print pdf (one sheet)", "cli", ["print", demo, "--pdf", str(out / "power.pdf"), "--sheet", "power"]),
        ("print pdf (all sheets)", "cli", ["print", demo, "--pdf", str(out / "all.pdf")]),
        ("print dxf (one sheet)", "cli", ["print", demo, "--dxf", str(out / "power.dxf"), "--sheet", "power"]),
        ("print bom+wires", "cli", ["print", demo, "--bom", str(out / "bom.csv"), "--wires", str(out / "wires.csv")]),
        ("package pdf (one sheet)", "cli", ["package", str(CATALOG), "--pdf", str(out / "pkg-power.pdf"), "--sheets", "power"]),
        ("package pdf (full)", "cli", ["package", str(CATALOG), "--pdf", str(out / "pkg.pdf")]),
        ("bom export", "cli", ["bom", "export", demo, "--out", str(out / "bom-export.csv")]),
        ("electrical wire-list", "cli", ["electrical", "wire-list", demo, "--out", str(out / "wire-list.csv")]),
        ("electrical terminals", "cli", ["electrical", "terminals", demo, "--out", str(out / "terminals.csv")]),
        ("electrical checks", "cli", ["electrical", "checks", demo]),
        ("electrical strip-list", "cli", ["electrical", "strip-list", strip]),
        (
            "terminal strip sheet pdf",
            "cli",
            ["print", strip, "--pdf", str(out / "strip.pdf"), "--sheet", "strip-x1"],
        ),
        ("library list", "cli", ["library", "list"]),
        ("reconstruct add", "add", None),
        ("reconstruct photos-to-drawings", "photos", None),
        ("capture-server startup", "capture", None),
    ]


def _one(kind: str, args: list[str] | None, work: Path, env: dict[str, str], prepared: dict[str, Path]) -> tuple[float, int, int]:
    if kind == "import":
        return _run(["--import"], env=env, cwd=work, timeout=60)
    if kind == "capture":
        slot = work / "capture"
        if slot.exists():
            shutil.rmtree(slot)
        slot.mkdir()
        return _capture_startup(env, work, slot, timeout=30)
    if kind == "add":
        dest = work / "add-run"
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir()
        project = dest / "job.pcad.yaml"
        shutil.copy2(prepared["base"], project)
        return _run(
            ["reconstruct", "add", str(project), str(prepared["photo"]), "--role", "backpanel"],
            env=env,
            cwd=work,
            timeout=60,
        )
    if kind == "photos":
        dest = work / "photos-run"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(prepared["with_photo"], dest)
        project = dest / "job.pcad.yaml"
        return _run(
            ["reconstruct", "photos-to-drawings", str(project)],
            env=env,
            cwd=work,
            timeout=120,
        )
    assert args is not None
    return _run(args, env=env, cwd=work, timeout=180)


def _format_table(rows: list[dict]) -> str:
    headers = ("command", "median_s", "peak_rss_mb", "exit")
    body = []
    for row in rows:
        body.append(
            (
                row["command"],
                f"{row['median_s']:.3f}",
                f"{row['peak_rss_kb'] / 1024:.1f}",
                str(row["exit"]),
            )
        )
    widths = [len(item) for item in headers]
    for line in body:
        for index, cell in enumerate(line):
            widths[index] = max(widths[index], len(cell))
    def _line(cells: tuple[str, ...]) -> str:
        return " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(cells))
    rule = "-+-".join("-" * width for width in widths)
    lines = [_line(headers), rule]
    lines.extend(_line(line) for line in body)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark one-step PanelCAD CLI commands on bundled demo data.")
    parser.add_argument("--runs", type=int, default=5, help="Repetitions per command (median is reported).")
    parser.add_argument("--json-out", type=Path, default=None, help="Also write the rows as JSON.")
    args = parser.parse_args(argv)
    if args.runs < 1:
        print("runs must be >= 1", file=sys.stderr)
        return 2
    if not DEMO.is_file() or not STRIP.is_file() or not CATALOG.is_file():
        print("bundled demo projects are missing", file=sys.stderr)
        return 1

    work = Path(os.environ.get("PANELCAD_BENCH_DIR", "")).expanduser() if os.environ.get("PANELCAD_BENCH_DIR") else None
    cleanup = work is None
    if work is None:
        import tempfile

        work = Path(tempfile.mkdtemp(prefix="panelcad-bench-"))
    else:
        work.mkdir(parents=True, exist_ok=True)
    env = _env(work)
    (work / "appdata").mkdir(exist_ok=True)
    (work / "config").mkdir(exist_ok=True)
    try:
        prepared = _prepare(work, env)
        cases = _cases(work, prepared)
        rows: list[dict] = []
        for name, kind, cmd in cases:
            samples: list[tuple[float, int, int]] = []
            for _ in range(args.runs):
                samples.append(_one(kind, cmd, work, env, prepared))
            exits = {item[2] for item in samples}
            row = {
                "command": name,
                "runs": args.runs,
                "median_s": round(_median([item[0] for item in samples]), 4),
                "min_s": round(min(item[0] for item in samples), 4),
                "max_s": round(max(item[0] for item in samples), 4),
                "peak_rss_kb": int(_median([float(item[1]) for item in samples])),
                "exit": samples[-1][2] if len(exits) == 1 else sorted(exits)[-1],
                "seconds": [round(item[0], 4) for item in samples],
            }
            rows.append(row)
            print(f"{name}: {row['median_s']:.3f}s  rss {row['peak_rss_kb']}k  exit {row['exit']}", flush=True)
        print()
        print(_format_table(rows))
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        # check / electrical checks exit 1 when the demo has a design finding.
        # capture-server is stopped once it is listening.
        soft = {"check", "electrical checks", "capture-server startup"}
        bad = [row for row in rows if row["exit"] != 0 and row["command"] not in soft]
        return 1 if bad else 0
    finally:
        if cleanup:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
