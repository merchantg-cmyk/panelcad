from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any, NoReturn, Optional

import typer

from panelcad.api.session import Session, prepare_qt_fonts

prepare_qt_fonts()
from panelcad.version import __version__


class _Cli(typer.Typer):
    """PanelCAD commands. ``*`` is a net name, not a file glob.

    Click expands wildcards in ``sys.argv`` on Windows. ``--net *`` would
    become every file in the current folder. No command here takes a glob.
    """

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("windows_expand_args", False)
        return super().__call__(*args, **kwargs)


app = _Cli(add_completion=False, no_args_is_help=True, help="PanelCAD — industrial control panel CAD")


@app.callback()
def _root() -> None:
    from panelcad.diag.runtime import install_process_diagnostics

    install_process_diagnostics(gui=False)


@app.command()
def version() -> None:
    typer.echo(__version__)


@app.command("logs")
def logs_cmd(
    path: bool = typer.Option(False, "--path", help="Print the logs folder"),
) -> None:
    """Print the logs folder."""

    from panelcad.diag.paths import ensure_dir, logs_dir

    folder = ensure_dir(logs_dir())
    # ``panelcad logs --path`` is the documented form. Either way, print the folder.
    typer.echo(str(folder))
    if path:
        return


@app.command("bug-report")
def bug_report_cmd(
    title: str = typer.Option(..., "--title", help="Short summary"),
    description: str = typer.Option(..., "--description", help="What happened"),
    project: Optional[Path] = typer.Option(None, "--project", help="Include this project file"),
    send: bool = typer.Option(False, "--send", help="Post the report when bug reports are configured"),
    steps: str = typer.Option("", "--steps", help="Steps to reproduce"),
    contact: str = typer.Option("", "--contact", help="Optional contact name"),
) -> None:
    """Write a bug-report zip. --send posts it when a webhook or Slack bot is configured.

    The Slack post includes the description, steps, contact, version, recent
    actions, traceback, and log tail. A bot token uploads the screenshot and
    the zip. If that upload fails, the webhook posts the text.
    The webhook is PANELCAD_BUG_WEBHOOK, then settings, then the release default.
    The bot token is PANELCAD_SLACK_BOT_TOKEN, then settings, then the release bot.
    The release bot posts only to the channel id embedded when that build was made.
    PANELCAD_BUG_REPORTS=off saves the zip and does not post.
    """

    if not str(description or "").strip():
        typer.echo("description is required", err=True)
        raise typer.Exit(code=1)
    if project is not None:
        session = Session.open(project)
    else:
        session = Session.create("Bug report")
    result = session.report_bug(
        title,
        description,
        include_project=project is not None,
        include_screenshot=False,
        send=send,
        steps=steps,
        contact=contact,
    )
    typer.echo(result["path"])
    if send and not result.get("sent"):
        detail = (result.get("send") or {}).get("error") or "send failed"
        typer.echo(str(detail), err=True)
        raise typer.Exit(code=1)


settings_app = typer.Typer(
    help="Appearance, autosave, and the shared settings file",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(settings_app, name="settings")


def _settings_failed(exc: BaseException) -> None:
    typer.echo(f"could not save settings: {exc}", err=True)
    raise typer.Exit(code=1) from exc


@settings_app.callback()
def settings_main(
    ctx: typer.Context,
    autosave_seconds: Optional[int] = typer.Option(
        None,
        "--autosave-seconds",
        help="Seconds between recovery copies of unsaved work. 0 turns autosave off.",
    ),
) -> None:
    """Show or set the autosave interval. PANELCAD_AUTOSAVE_SECONDS overrides the file."""

    from panelcad.diag.settings import autosave_seconds as current_autosave
    from panelcad.diag.settings import set_autosave_seconds

    if autosave_seconds is not None:
        try:
            set_autosave_seconds(autosave_seconds)
        except OSError as exc:
            _settings_failed(exc)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
    if ctx.invoked_subcommand is not None:
        return
    typer.echo(f"autosave_seconds={current_autosave()}")


@settings_app.command("show")
def settings_show() -> None:
    """Print the saved theme and sheet canvas."""

    import json

    from panelcad.theme import public_settings

    typer.echo(json.dumps(public_settings(), indent=2))


@settings_app.command("set")
def settings_set(
    key: str = typer.Argument(..., help="theme, canvas, or phase"),
    value: str = typer.Argument(..., help="light, dark, system — paper, white, dark, auto — or on, off"),
) -> None:
    """Save appearance. Example: panelcad settings set theme dark."""

    from panelcad.theme import parse_phase_enabled, public_settings, set_canvas, set_phase_colors, set_theme

    name = key.strip().lower()
    try:
        if name == "theme":
            set_theme(value)
        elif name == "canvas":
            set_canvas(value)
        elif name in {"phase", "phase_colors"}:
            set_phase_colors(parse_phase_enabled(value))
            name = "phase_colors"
        else:
            typer.echo("key must be theme, canvas, or phase", err=True)
            raise typer.Exit(code=1)
    except OSError as exc:
        _settings_failed(exc)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{name} {public_settings()[name]}")


@app.command("new")
def new_project(
    name: str = typer.Argument(..., help="Project name"),
    out: Path = typer.Option(..., "--out", "-o", help="Path to .pcad.yaml"),
) -> None:
    s = Session.create(name)
    s.save(out)
    typer.echo(f"wrote {out}")


@app.command()
def place(
    project: Path,
    lib: str = typer.Option(..., "--lib"),
    ref: Optional[str] = typer.Option(None, "--ref"),
    x: float = typer.Option(40.0, "--x"),
    y: float = typer.Option(40.0, "--y"),
    sheet: Optional[str] = typer.Option(None, "--sheet"),
) -> None:
    from panelcad.library.panel import LayoutNotReadyError

    s = Session.open(project)
    try:
        comp = s.place(lib, ref=ref, x=x, y=y, sheet_id=sheet)
    except LayoutNotReadyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    s.save(project)
    typer.echo(f"placed {comp.id} ({comp.library_id})")


@app.command()
def wire(
    project: Path,
    frm: str = typer.Option(..., "--from"),
    to: str = typer.Option(..., "--to"),
    net: Optional[str] = typer.Option(None, "--net"),
    sheet: Optional[str] = typer.Option(None, "--sheet"),
    number: Optional[str] = typer.Option(
        None,
        "--number",
        help="Lock this wire number, the same as Set wire number",
    ),
) -> None:
    s = Session.open(project)
    w = s.wire(frm, to, net=net, sheet_id=sheet, number=number)
    s.save(project)
    extra = f"  {w.number}" if getattr(w, "number", "") else ""
    typer.echo(f"wired {w.frm} -> {w.to} ({w.id}){extra}")


@app.command("print")
def print_cmd(
    project: Path,
    svg: Optional[Path] = typer.Option(None, "--svg"),
    pdf: Optional[Path] = typer.Option(None, "--pdf"),
    dxf: Optional[Path] = typer.Option(None, "--dxf", help="One sheet, or every sheet as sibling files when --sheet is omitted"),
    bom: Optional[Path] = typer.Option(None, "--bom"),
    wires: Optional[Path] = typer.Option(None, "--wires"),
    sheet: Optional[str] = typer.Option(None, "--sheet", help="One sheet id; omit PDF to print the whole set"),
) -> None:
    s = Session.open(project)
    errors = s.validate()
    if errors:
        for e in errors:
            typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=1)
    if svg:
        s.export_svg(svg, sheet_id=sheet)
        typer.echo(f"svg {svg}")
    if pdf:
        s.export_pdf(pdf, sheet_id=sheet)
        typer.echo(f"pdf {pdf}")
    if dxf:
        if sheet:
            path = s.export_dxf(dxf, sheet_id=sheet)
            typer.echo(f"dxf {path}")
        else:
            paths = s.export_dxfs(dxf)
            for path in paths:
                typer.echo(f"dxf {path}")
    if bom:
        s.export_bom(bom)
        typer.echo(f"bom {bom}")
    if wires:
        s.export_wire_list(wires)
        typer.echo(f"wires {wires}")
    if not any([svg, pdf, dxf, bom, wires]):
        typer.echo("pass --svg, --pdf, --dxf, --bom, and/or --wires")


bom_app = typer.Typer(help="Bill of materials import/export")
app.add_typer(bom_app, name="bom")


@bom_app.command("export")
def bom_export(
    project: Path,
    out: Path = typer.Option(..., "--out", "-o"),
    grouped: bool = typer.Option(False, "--grouped", help="One row per unique part, qty + concatenated refs"),
) -> None:
    s = Session.open(project)
    s.export_bom(out, grouped=grouped)
    typer.echo(f"bom {out}")


@bom_app.command("import")
def bom_import(
    project: Path,
    bom: Path,
    no_place: bool = typer.Option(False, "--no-place", help="Only update existing refs; do not add parts"),
) -> None:
    s = Session.open(project)
    try:
        result = s.import_bom(bom, place_missing=not no_place)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    s.save(project)
    typer.echo(
        f"updated {len(result['updated'])}  created {len(result['created'])}  "
        f"skipped {len(result['skipped'])}  errors {len(result['errors'])}"
    )
    for line in result["skipped"]:
        typer.echo(f"skipped: {line}")
    for err in result["errors"]:
        typer.echo(f"error: {err}", err=True)


@app.command("export")
def export_cmd(
    project: Path,
    out: Path = typer.Option(..., "--out", "-o", help="Package directory"),
    no_pdf: bool = typer.Option(False, "--no-pdf"),
) -> None:
    """Export everything: project, JSON, BOM, wires, library, sheets, SVG, PDF, DXF."""
    s = Session.open(project)
    dest = s.export_package(out, pdf=not no_pdf)
    typer.echo(f"package {dest}")


@app.command("package")
def package_cmd(
    project: Path,
    pdf: Optional[Path] = typer.Option(
        None,
        "--pdf",
        help="Write the one-click print-package PDF here (cover, index, drawings, reports)",
    ),
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        "-o",
        help="Also write the folder package (YAML/CSV/SVG/DXF + print.pdf)",
    ),
    paper: str = typer.Option(
        "mixed",
        "--paper",
        help="mixed (Letter reports + native drawing sizes), letter, a4, a3, or tabloid",
    ),
    sections: Optional[str] = typer.Option(
        None,
        "--sections",
        help="Comma list: cover,index,revisions,drawings,bom,wires,terminals,checks",
    ),
) -> None:
    """One-click print package. Default writes a PDF next to the project."""
    s = Session.open(project)
    parts = [part.strip() for part in (sections or "").split(",") if part.strip()] or None
    if out is not None:
        dest = s.export_package(out, pdf=True, paper=paper, sections=parts)
        typer.echo(f"package {dest}")
    dest_pdf = pdf
    if dest_pdf is None and out is None:
        dest_pdf = Path(f"{project.stem}_package.pdf")
    if dest_pdf is not None:
        result = s.print_package(dest_pdf, paper=paper, sections=parts)
        typer.echo(f"pdf {result['path']}  pages={result['pages']}")


@app.command(
    "import",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
def import_cmd(
    ctx: typer.Context,
    sources: Optional[list[Path]] = typer.Argument(
        None, help="Package, project, or a print (PDF, images, DXF, SVG)"
    ),
    into: Optional[Path] = typer.Option(None, "--into", help="Existing project to merge into"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Save imported project here"),
    photos: Optional[Path] = typer.Option(None, "--photos", help="Folder of panel photos (layout proposals only)"),
    standard: Optional[str] = typer.Option(None, "--standard", help="nema or iec. Default: read the title block"),
    resume: Optional[Path] = typer.Option(None, "--resume", help="Workspace to resume"),
    workspace: Optional[Path] = typer.Option(None, "--workspace", help="Where page JSON and crops are stored"),
    external: bool = typer.Option(False, "--external", help="Stop at the next crop for an outside agent"),
    vision_backend: Optional[str] = typer.Option(
        None,
        "--vision-backend",
        help="Scan backend: none, grok, grok-build, ollama, or custom. Vector PDFs are unchanged.",
    ),
    vision_model: Optional[str] = typer.Option(None, "--vision-model", help="Model id for the scan vision backend"),
    vision_base_url: Optional[str] = typer.Option(None, "--vision-base-url", help="OpenAI-compatible base URL"),
    vision_api_key_env: Optional[str] = typer.Option(
        None,
        "--vision-api-key-env",
        help="Environment variable that holds the API key. The key is not stored.",
    ),
    workers: Optional[int] = typer.Option(
        None,
        "--workers",
        help="Parallel vision workers. Ollama defaults to 1. Other backends default to 3. Maximum 8.",
    ),
    keep_grok_sessions: bool = typer.Option(
        False,
        "--keep-grok-sessions",
        help="Leave each tile's folder in ~/.grok/sessions. Default deletes only that tile's folder.",
    ),
    grok_tools_none: bool = typer.Option(
        False,
        "--grok-tools-none",
        help="Pass --tools none to grok. Off by default until it is tested on this PC.",
    ),
    timeout: Optional[float] = typer.Option(
        None,
        "--timeout",
        help="Seconds for one vision request. Ollama's preset default is 300.",
    ),
    page_timeout: Optional[float] = typer.Option(
        None,
        "--page-timeout",
        help="Seconds for one page of tiles, not the whole job. Ollama's preset default is 3600.",
    ),
    num_ctx: Optional[int] = typer.Option(
        None,
        "--num-ctx",
        help="Ollama context length. The preset default is 16384.",
    ),
    num_predict: Optional[int] = typer.Option(
        None,
        "--num-predict",
        help="Completion cap. Ollama receives this as num_predict. A /v1 server receives max_tokens. Default 4096.",
    ),
    think: Optional[str] = typer.Option(
        None,
        "--think",
        help="Ollama thinking: on or off. The preset default is off.",
    ),
    reasoning_effort: Optional[str] = typer.Option(
        None,
        "--reasoning-effort",
        help="Reasoning effort for a /v1 server: none, low, medium, or high.",
    ),
    dpi: Optional[float] = typer.Option(
        None,
        "--dpi",
        help="PDF and vector page PNG resolution (72–600). A scan keeps the DPI stored in the file. `import prep` defaults to 200. Tiles are crops of the page PNG.",
    ),
    truth: Optional[Path] = typer.Option(
        None,
        "--truth",
        help="Ground truth JSON or YAML. Each item is labeled with the stage where it was lost.",
    ),
    agent_prep: Optional[Path] = typer.Option(
        None,
        "--prep",
        help="With `import apply`, the prep folder when the JSON omits prep_dir.",
    ),
    answers: Optional[Path] = typer.Option(
        None,
        "--answers",
        help="With `import answer`, a JSON file of {id, value} rows, or a map of id to value.",
    ),
) -> None:
    """Import a package, or turn a print (PDF, scan, DXF, SVG) into a PanelCAD project.

    `panelcad import prep PRINT --out DIR` writes page images, tiles, extracted
    text, and the JSON schema for an outside agent. PanelCAD does not call a model.
    `panelcad import apply RESPONSE.json --out PROJECT` checks that JSON and builds
    the project. `panelcad import answer PROJECT QUESTION VALUE` applies a human's
    answer and prints the questions still open.
    """
    paths = list(sources or [])
    if paths and paths[0].name in {"prep", "apply", "answer"} and not paths[0].exists():
        _import_agent_command(
            paths,
            out=out,
            dpi=dpi,
            prep_dir=agent_prep,
            answers=answers,
            extra=list(ctx.args),
        )
        return
    if paths and paths[0].name == "diag" and not paths[0].exists():
        _import_diag(paths[1] if len(paths) > 1 else None, truth)
        return
    if (
        resume is not None
        or photos is not None
        or standard
        or external
        or vision_backend
        or vision_model
        or vision_base_url
        or vision_api_key_env
        or workers is not None
        or keep_grok_sessions
        or grok_tools_none
        or any(_is_print_source(path) for path in paths)
    ):
        _import_print(
            paths,
            into=into,
            out=out,
            photos=photos,
            standard=standard,
            resume=resume,
            workspace=workspace,
            external=external,
            vision_backend=vision_backend,
            vision_model=vision_model,
            vision_base_url=vision_base_url,
            vision_api_key_env=vision_api_key_env,
            workers=workers,
            keep_grok_sessions=keep_grok_sessions,
            grok_tools_none=grok_tools_none,
            timeout=timeout,
            page_timeout=page_timeout,
            num_ctx=num_ctx,
            num_predict=num_predict,
            think=think,
            reasoning_effort=reasoning_effort,
            dpi=dpi,
            truth=truth,
        )
        return
    if len(paths) != 1:
        raise typer.BadParameter("pass one package or project, or a print file")
    source = paths[0]
    try:
        if into is not None:
            s = Session.open(into)
            result = s.import_any(source, replace=False)
            s.save(into)
            typer.echo(json_result(result))
            return
        s = Session.from_any(source)
        dest = out or (into if into else None)
        if dest is None and source.is_dir():
            dest = source / "project.pcad.yaml"
        if dest is not None:
            s.save(dest)
            typer.echo(f"imported {s.project.name} -> {dest}  sheets={len(s.project.sheets)}")
            return
        typer.echo(f"loaded {s.project.name}  sheets={len(s.project.sheets)}  (pass --out to save)")
    except (ValueError, UnicodeDecodeError, OSError) as exc:
        typer.echo(str(exc).splitlines()[0], err=True)
        raise typer.Exit(code=1) from None


def _import_agent_command(
    paths: list[Path],
    *,
    out: Optional[Path],
    dpi: Optional[float],
    prep_dir: Optional[Path],
    answers: Optional[Path] = None,
    extra: Optional[list[str]] = None,
) -> None:
    """`import prep`, `import apply`, and `import answer` share the existing import command."""

    from panelcad.importer.agent_exchange import DEFAULT_DPI, apply_response, prepare_print

    verb = paths[0].name
    rest = paths[1:]
    if verb == "answer":
        _import_answer(rest, answers, extra or [])
        return
    if verb == "prep":
        if out is None:
            raise typer.BadParameter("pass --out DIR for import prep")
        if not rest:
            raise typer.BadParameter("pass a print after import prep")
        try:
            payload = prepare_print(rest, out, dpi=DEFAULT_DPI if dpi is None else dpi)
        except (OSError, ValueError) as exc:
            typer.echo(json.dumps({"ok": False, "error": str(exc).splitlines()[0]}, indent=2), err=True)
            raise typer.Exit(code=1) from None
        typer.echo(json.dumps(payload, indent=2))
        return
    if len(rest) != 1:
        raise typer.BadParameter("pass the agent JSON: panelcad import apply RESPONSE.json")
    payload = apply_response(rest[0], out=out, prep=prep_dir)
    typer.echo(json.dumps(payload, indent=2, default=str))
    if not payload.get("ok"):
        raise typer.Exit(code=1)


def _import_answer(rest: list[Path], answers: Optional[Path], extra: list[str]) -> None:
    """Apply a human's answer and print the questions that are still open.

    A value that starts with ``-`` is an answer (an IEC tag such as ``-X2``), not an option.
    """

    from panelcad.importer.agent_questions import apply_answer, apply_answers, load_answers

    if not rest:
        raise typer.BadParameter("panelcad import answer PROJECT QUESTION VALUE")
    session = Session.open(rest[0])
    if answers is not None:
        payload = apply_answers(session, load_answers(answers))
    elif len(rest) >= 3 or extra:
        parts = [str(part) for part in rest[2:]] + [str(part) for part in extra]
        if len(rest) < 2 or not parts:
            raise typer.BadParameter("panelcad import answer PROJECT QUESTION VALUE, or --answers FILE")
        payload = apply_answer(session, rest[1].name, " ".join(parts))
    else:
        raise typer.BadParameter("panelcad import answer PROJECT QUESTION VALUE, or --answers FILE")
    typer.echo(json.dumps(payload, indent=2, default=str))
    if not payload.get("applied"):
        raise typer.Exit(code=1)


def _is_print_source(path: Path) -> bool:
    from panelcad.importer.ingest import is_print_path

    return is_print_path(path)


def _import_print(
    sources: list[Path],
    *,
    into: Optional[Path],
    out: Optional[Path],
    photos: Optional[Path],
    standard: Optional[str],
    resume: Optional[Path],
    workspace: Optional[Path],
    external: bool,
    vision_backend: Optional[str] = None,
    vision_model: Optional[str] = None,
    vision_base_url: Optional[str] = None,
    vision_api_key_env: Optional[str] = None,
    workers: Optional[int] = None,
    keep_grok_sessions: bool = False,
    grok_tools_none: bool = False,
    timeout: Optional[float] = None,
    page_timeout: Optional[float] = None,
    num_ctx: Optional[int] = None,
    num_predict: Optional[int] = None,
    think: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    dpi: Optional[float] = None,
    truth: Optional[Path] = None,
) -> None:
    from panelcad.importer.raster import TesseractMissing
    from panelcad.importer.vision_config import vision_from_options

    vision = None
    backend = "external" if external else "auto"
    if (
        vision_backend
        or workers is not None
        or keep_grok_sessions
        or grok_tools_none
        or timeout is not None
        or page_timeout is not None
        or num_ctx is not None
        or num_predict is not None
        or think is not None
        or reasoning_effort is not None
    ):
        vision = vision_from_options(
            preset=vision_backend or "none",
            base_url=vision_base_url or "",
            model=vision_model or "",
            api_key_env=vision_api_key_env or "",
            workers=workers,
            timeout_s=timeout,
            page_timeout_s=page_timeout,
            num_ctx=num_ctx,
            num_predict=num_predict,
            think=think,
            reasoning_effort=reasoning_effort or "",
            clean_grok_sessions=not keep_grok_sessions,
            grok_tools_none=grok_tools_none,
        )
        backend = "raster" if vision.preset == "none" else vision.preset
        if external:
            backend = "external"
            vision = VisionSettings(preset="none", clean_grok_sessions=not keep_grok_sessions)
    try:
        _run_print_import(
            sources,
            into=into,
            out=out,
            photos=photos,
            standard=standard,
            resume=resume,
            workspace=workspace,
            backend=backend,
            vision_model=vision_model,
            vision=vision,
            dpi=dpi,
            truth=truth,
        )
    except TesseractMissing as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None


def _confirm_cost_guard(info: dict) -> bool:
    """Warn on the page cap. A terminal can continue; a pipe skips the rest."""

    remaining = int(info.get("remaining_tiles") or 0)
    spent = float(info.get("spent_usd") or 0)
    cap = float(info.get("max_usd") or 0)
    typer.echo(
        f"page cost guard: ${spent:.2f} of ${cap:.2f}. {remaining} tiles have not been sent.",
        err=True,
    )
    if not sys.stdin.isatty():
        typer.echo("page cost guard: remaining tiles skipped. Re-run in a terminal to continue.", err=True)
        return False
    return bool(typer.confirm("Continue reading the remaining tiles?", default=False))


def _echo_tile(message: str) -> None:
    if str(message).startswith("tiles "):
        typer.echo(message, err=True)


def _one_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    if text and text[0].strip():
        return text[0].strip()
    return type(exc).__name__


def _failure_reason(exc: BaseException) -> str:
    """Name the problem. A missing path alone is not a reason."""

    if isinstance(exc, FileNotFoundError):
        path = getattr(exc, "filename", None) or _one_line(exc)
        return f"file not found: {path}"
    if isinstance(exc, OSError):
        import errno

        if getattr(exc, "errno", None) in {errno.ENOENT, errno.ENOTDIR}:
            path = getattr(exc, "filename", None) or _one_line(exc)
            return f"file not found: {path}"
    return _one_line(exc)


def _raise_if_cli_signal(exc: BaseException) -> None:
    """Usage errors and aborts keep Typer's own exit code."""

    if isinstance(exc, (typer.Exit, typer.Abort, typer.BadParameter)):
        raise exc


def _echo_summary(job, *, include_warning: bool = True) -> None:
    summary = job.summary()
    est = ""
    if float(summary.get("estimated_usd") or 0) > 0:
        est = f"  est ${float(summary['estimated_usd']):.3f}"
    done = int(summary.get("tiles_done") or 0)
    total = int(summary.get("tiles_total") or 0)
    spent = float(summary.get("spent_usd") or 0)
    typer.echo(
        "summary "
        f"tiles {done}/{total}  "
        f"${spent:.3f}  fallbacks {int(summary.get('fallbacks') or 0)}  "
        f"tokens {int(summary.get('input_tokens') or 0)}{est}  "
        f"stage {summary.get('stage')}",
        err=True,
    )
    for row in summary.get("page_seconds") or []:
        typer.echo(f"page {row['id']} {float(row.get('seconds') or 0):.1f} s", err=True)
    for note in summary.get("ocr_skipped") or []:
        typer.echo(str(note), err=True)
    if include_warning and summary.get("warning"):
        typer.echo(str(summary["warning"]), err=True)
    for reason in summary.get("fallback_reasons") or []:
        typer.echo(f"fallback {reason}", err=True)


def _echo_counts(job) -> None:
    """Devices, wires, needs-info, unparseable tags, tiles, and cost. One line."""

    summary = job.summary()
    tags = [str(tag) for tag in (summary.get("unparseable_tags") or []) if str(tag).strip()]
    unparsed = str(int(summary.get("unparseable") or 0))
    if tags:
        shown = ", ".join(tags[:8])
        extra = len(tags) - 8
        if extra > 0:
            shown = f"{shown} +{extra}"
        unparsed = f"{unparsed} ({shown})"
    cost = float(summary.get("spent_usd") or 0)
    typer.echo(
        "summary "
        f"devices {int(summary.get('devices') or 0)}  "
        f"wires {int(summary.get('wires') or 0)}  "
        f"needs-info {int(summary.get('needs_info') or 0)}  "
        f"unparseable {unparsed}  "
        f"tiles read {int(summary.get('tiles_read') or 0)}  "
        f"skipped {int(summary.get('tiles_skipped') or 0)}  "
        f"cost ${cost:.3f}"
    )
    recovery = str(summary.get("recovery") or "").strip()
    if recovery:
        typer.echo(recovery)


def _saved_import(job) -> bool:
    """True when the drawing file is on disk and the job marked it built."""

    path = str(job.manifest.get("project") or "")
    if not path or not Path(path).is_file():
        return False
    return str(job.manifest.get("stage") or "") == "built"


def _fail_import(job, exc: BaseException) -> NoReturn:
    _raise_if_cli_signal(exc)
    try:
        _echo_summary(job)
        _echo_counts(job)
    except Exception:  # noqa: BLE001 — the failure line still has to be printed
        pass
    typer.echo(f"import failed: {_failure_reason(exc)}", err=True)
    raise typer.Exit(code=1) from None


def _run_print_import(
    sources: list[Path],
    *,
    into: Optional[Path],
    out: Optional[Path],
    photos: Optional[Path],
    standard: Optional[str],
    resume: Optional[Path],
    workspace: Optional[Path],
    backend: str,
    vision_model: Optional[str],
    vision,
    dpi: Optional[float] = None,
    truth: Optional[Path] = None,
) -> None:
    from panelcad.importer.pipeline import EmptyImport, ImportJob

    root = resume or workspace
    if root is None:
        root = (out.parent / f"{out.stem}-import") if out is not None else Path("import-workspace")
    try:
        if resume is not None:
            job = ImportJob.resume(
                resume,
                backend=backend,
                model=vision_model,
                vision=vision,
                on_tick=_echo_tile,
                on_cost_guard=_confirm_cost_guard,
                truth=truth,
            )
        else:
            if not sources:
                raise typer.BadParameter("pass a PDF or images, or --resume a workspace")
            job = ImportJob.start(
                sources,
                root,
                photos=photos,
                standard=standard,
                backend=backend,
                model=vision_model,
                vision=vision,
                on_tick=_echo_tile,
                on_cost_guard=_confirm_cost_guard,
                dpi=dpi,
                truth=truth,
            )
    except EmptyImport as exc:
        typer.echo(f"import failed: {_failure_reason(exc)}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        _raise_if_cli_signal(exc)
        typer.echo(f"import failed: {_failure_reason(exc)}", err=True)
        raise typer.Exit(code=1) from None
    status = job.status()
    if status.get("pending"):
        _echo_summary(job)
        work = status.get("work") or {}
        typer.echo(json_result({"paused": True, "workspace": str(root), "work": work}))
        raise typer.Exit(code=2)
    try:
        session = job.build(out=out)
    except EmptyImport as exc:
        _echo_summary(job, include_warning=False)
        _echo_counts(job)
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        _raise_if_cli_signal(exc)
        # The drawing is already on disk. A later bookkeeping error must not
        # hide that, and must not become a silent non-zero exit.
        if _saved_import(job):
            from panelcad.api.session import Session

            dest = job.manifest.get("project")
            try:
                saved = Session.open(dest)
            except Exception as open_exc:  # noqa: BLE001
                _fail_import(job, open_exc)
            _echo_summary(job)
            typer.echo(
                f"imported print {saved.project.name} -> {dest}  "
                f"sheets={len(saved.project.sheets)}  stage={job.manifest.get('stage')}"
            )
            _echo_counts(job)
            typer.echo(f"import warning: {_one_line(exc)}", err=True)
            return
        _fail_import(job, exc)
    if into is not None and out is None:
        try:
            session.save(into)
        except Exception as exc:
            _raise_if_cli_signal(exc)
            _echo_summary(job)
            _echo_counts(job)
            typer.echo(f"import failed: {_failure_reason(exc)}", err=True)
            raise typer.Exit(code=1) from None
    dest = job.manifest.get("project")
    try:
        _echo_summary(job)
    except Exception as exc:  # noqa: BLE001 — the imported line is the success signal
        typer.echo(f"import warning: {_one_line(exc)}", err=True)
    typer.echo(
        f"imported print {session.project.name} -> {dest}  "
        f"sheets={len(session.project.sheets)}  stage={job.manifest.get('stage')}"
    )
    try:
        _echo_counts(job)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"import warning: {_one_line(exc)}", err=True)


def _import_diag(run: Optional[Path], truth: Optional[Path] = None) -> None:
    """Print the funnel for one import run. ``run`` is a directory, a project, or a run id."""

    from panelcad.importer.diagnostics import load_run

    if run is None:
        typer.echo("panelcad import diag <run>", err=True)
        raise typer.Exit(code=1)
    try:
        report = load_run(run, truth=truth)
    except (FileNotFoundError, ValueError, OSError) as exc:
        typer.echo(str(exc).splitlines()[0], err=True)
        raise typer.Exit(code=1) from None
    typer.echo(str(report.get("markdown") or ""))


def json_result(result: dict) -> str:
    import json as _json

    return _json.dumps(result, default=str)


lib_app = typer.Typer(help="Component library")
app.add_typer(lib_app, name="library")


@lib_app.command("list")
def library_list(query: str = typer.Option("", "--query", "-q")) -> None:
    s = Session.create("tmp")
    for d in s.catalog.search(query):
        typer.echo(f"{d.library_id:40}  {d.name}  [{d.category}]")


@lib_app.command("parts")
def library_parts(
    query: str = typer.Option("", "--query", "-q"),
    manufacturer: str = typer.Option("", "--manufacturer", "-m"),
    category: str = typer.Option("", "--category", "-c"),
    amps_min: float | None = typer.Option(None, "--amps-min"),
    amps_max: float | None = typer.Option(None, "--amps-max"),
) -> None:
    """Search verified manufacturer catalog parts."""

    s = Session.create("tmp")
    hits = s.search_parts(
        query,
        manufacturer=manufacturer or None,
        category=category or None,
        amps_min=amps_min,
        amps_max=amps_max,
    )
    for part in hits:
        ratings = part.get("ratings") or ""
        extra = f"  {ratings}" if ratings else ""
        typer.echo(
            f"{part['manufacturer']:22} {part['catalog']:24} {part['verified']:8}  {part['description']}{extra}"
        )
    typer.echo(f"{len(hits)} part(s)")


@lib_app.command("assign")
def library_assign(
    project: Path,
    ref: str,
    catalog: str,
    manufacturer: str = typer.Option("", "--manufacturer", "-m"),
) -> None:
    """Assign a verified catalog SKU to a placed device (keeps the symbol)."""

    from panelcad.library.panel import LayoutNotReadyError

    s = Session.open(project)
    try:
        result = s.assign_part(ref, catalog, manufacturer=manufacturer or None)
    except LayoutNotReadyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    s.save(project)
    typer.echo(
        f"assigned {result['manufacturer']} {result['catalog']} -> {ref} ({result['verified']})"
    )


@lib_app.command("import")
def library_import(
    project: Path,
    source: Path,
) -> None:
    s = Session.open(project)
    ids = s.import_library(source)
    s.save(project)
    typer.echo(f"imported {len(ids)} devices")


@app.command()
def example(
    kind: str = typer.Argument("dol"),
    out: Path = typer.Option(Path("examples/dol_starter.pcad.yaml"), "--out", "-o"),
) -> None:
    if kind != "dol":
        raise typer.BadParameter("only 'dol' is shipped in v0.1")
    from panelcad.examples.dol import write_dol

    path = write_dol(out)
    typer.echo(f"wrote {path}")


@app.command("layout")
def layout_cmd(
    project: Path,
    sheet: Optional[str] = typer.Option(None, "--sheet", help="panel_layout sheet id"),
) -> None:
    """Pack unique schematic devices onto DIN rails on a panel_layout sheet."""
    s = Session.open(project)
    result = s.layout_from_schematic(sheet)
    s.save(project)
    typer.echo(
        f"packed {result['din']} DIN + {result['field']} field onto {result['rails']} rails ({result['sheet']})"
    )
    for row in result.get("unplaced") or []:
        reason = str(row.get("reason") or row.get("ref") or "").strip()
        if reason:
            typer.echo(reason)


enc_app = typer.Typer(help="Recommend or apply a standard enclosure from the parts list")
app.add_typer(enc_app, name="enclosure")


def _echo_enclosure(rec: dict) -> None:
    box = rec.get("enclosure") or {}
    heat = rec.get("heat") or {}
    needed = rec.get("needed") or {}
    units = rec.get("units") or "mm"
    typer.echo(box.get("name") or "no enclosure")
    typer.echo(
        f"  needed {needed.get('height_' + ('in' if units == 'in' else 'mm'))} "
        f"× {needed.get('width_' + ('in' if units == 'in' else 'mm'))} "
        f"× {needed.get('depth_' + ('in' if units == 'in' else 'mm'))} "
        f"+ {float(needed.get('spare_pct') or 0):.0%} spare  fit={rec.get('fit')}"
    )
    if heat:
        rise = heat.get("rise_k")
        typer.echo(
            f"  heat {heat.get('watts_known')} W  ΔT {rise} K  "
            f"ambient {heat.get('ambient_c')} °C  limit {heat.get('max_internal_c')} °C  "
            f"ok={heat.get('ok')}  cooling={heat.get('cooling') or 'none'}"
        )
        if heat.get("unknown_refs"):
            typer.echo("  heat unknown: " + ", ".join(heat["unknown_refs"][:12]))
    for line in rec.get("reasons") or []:
        typer.echo(f"  - {line}")
    for line in rec.get("warnings") or []:
        typer.echo(f"  ! {line}")


@enc_app.command("recommend")
def enclosure_recommend(
    project: Path,
    sheet: Optional[str] = typer.Option(None, "--sheet"),
    spare: Optional[float] = typer.Option(None, "--spare", help="Spare space fraction, default 0.25"),
    nema: Optional[str] = typer.Option(None, "--nema", help="1, 12, 4, or 4X"),
    material: Optional[str] = typer.Option(None, "--material", help="steel or stainless"),
    ambient: Optional[float] = typer.Option(None, "--ambient", help="Ambient °C"),
    tmax: Optional[float] = typer.Option(None, "--tmax", help="Max internal °C"),
    separate_control_rail: Optional[bool] = typer.Option(
        None,
        "--separate-control-rail/--no-separate-control-rail",
        help="Control devices on their own rail. Default off",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Recommend a standard box. Does not change the project."""
    s = Session.open(project)
    rec = s.recommend_enclosure(
        sheet_id=sheet,
        spare_pct=spare,
        nema=nema,
        material=material,
        ambient_c=ambient,
        max_internal_c=tmax,
        separate_control_rail=separate_control_rail,
    )
    if json_out:
        typer.echo(json_result(rec))
        return
    _echo_enclosure(rec)


@enc_app.command("apply")
def enclosure_apply(
    project: Path,
    sheet: Optional[str] = typer.Option(None, "--sheet"),
    place: bool = typer.Option(False, "--place", help="Place parts in the proposed arrangement"),
    spare: Optional[float] = typer.Option(None, "--spare"),
    nema: Optional[str] = typer.Option(None, "--nema"),
    material: Optional[str] = typer.Option(None, "--material"),
    ambient: Optional[float] = typer.Option(None, "--ambient"),
    tmax: Optional[float] = typer.Option(None, "--tmax"),
    separate_control_rail: Optional[bool] = typer.Option(
        None,
        "--separate-control-rail/--no-separate-control-rail",
        help="Control devices on their own rail. Default off",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Write the recommended enclosure as one undo step."""
    s = Session.open(project)
    rec = s.apply_enclosure(
        sheet_id=sheet,
        place_parts=place,
        spare_pct=spare,
        nema=nema,
        material=material,
        ambient_c=ambient,
        max_internal_c=tmax,
        separate_control_rail=separate_control_rail,
    )
    _save(s, project)
    if json_out:
        typer.echo(json_result(rec))
        return
    _echo_enclosure(rec)
    typer.echo(f"applied on {rec.get('sheet')}")


@app.command()
def agent() -> None:
    """JSONL stdin/stdout protocol for AI tools."""
    from panelcad.cli.agent import run_agent

    run_agent()


def _ai_model_spec(model: str | None) -> str:
    """The model the user named, or the account in Settings > AI.

    With nothing configured, this does not call a local model and does not
    use a built-in key.
    """

    import os

    from panelcad.ai.config import configuration_reason, load_ai_lookup

    chosen = str(model or "").strip()
    if chosen:
        return chosen
    reason = configuration_reason()
    if reason:
        if "turned off" in reason.casefold():
            typer.echo("AI is off: turn on AI in Settings > AI (no key is built in)", err=True)
        else:
            typer.echo("connect your own AI in Settings > AI (no key is built in)", err=True)
        raise typer.Exit(code=1)
    cfg = load_ai_lookup()
    name = cfg.resolved_model()
    if cfg.provider == "ollama":
        endpoint = cfg.resolved_endpoint()
        if endpoint and not os.environ.get("OLLAMA_BASE_URL", "").strip():
            os.environ["OLLAMA_BASE_URL"] = endpoint
        return f"ollama:{name}" if name else "ollama:gemma3:4b"
    if cfg.provider and name:
        return f"{cfg.provider}:{name}"
    typer.echo("connect your own AI in Settings > AI (no key is built in)", err=True)
    raise typer.Exit(code=1)


@app.command("ai")
def ai_cmd(
    prompt: str = typer.Argument(..., help="What to draw or change on the sheet"),
    project: Optional[Path] = typer.Option(None, "--project", "-p", help="Existing or new .pcad.yaml"),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="Pydantic AI model string. Omit this to use Settings > AI.",
    ),
    name: str = typer.Option("Panel", "--name", help="Project name when creating a file"),
) -> None:
    """Draw with a Pydantic AI agent. Tools place and wire; the router keeps paths orthogonal."""
    spec = _ai_model_spec(model)
    from panelcad.ai.drawing import drawing_agent, model_for, open_drawing

    drawing = open_drawing(project, name=name)
    with drawing_agent.override(model=model_for(spec)):
        result = drawing_agent.run_sync(prompt, deps=drawing)
    if drawing.path is not None and drawing.operator.session is not None:
        drawing.operator.session.save(drawing.path)
        typer.echo(f"saved {drawing.path}")
    typer.echo(result.output)


@app.command("sharpen")
def sharpen_cmd(
    task: str = typer.Argument(
        "list-sheets",
        help="Canned task id (list-sheets, place-terminal, title-block). Pass 'tasks' to list.",
    ),
    project: Optional[Path] = typer.Option(
        None,
        "--project",
        "-p",
        help="Job YAML. Default: G:\\My Drive\\Grok\\PanelCAD\\examples\\dol_starter.pcad.yaml",
    ),
    pdf: Optional[Path] = typer.Option(
        None,
        "--pdf",
        help="Controls print PDF for the from-print task",
    ),
    page: int = typer.Option(1, "--page", help="1-indexed PDF page for from-print"),
    pages: Optional[str] = typer.Option(
        None,
        "--pages",
        help="from-print pages: 1, 1-3, 1,2,3, or all (overrides --page)",
    ),
    standard: str = typer.Option(
        "IEC",
        "--standard",
        help="IEC or NEMA/ANSI catalog for from-print (NEMA/ANSI is one North American group)",
    ),
    style: str = typer.Option(
        "schematic",
        "--style",
        help="schematic or ladder — stored on the title block so the AI can read it",
    ),
    model: str = typer.Option("gemma3:4b", "--model", "-m", help="Ollama model tag"),
    host: str = typer.Option("http://127.0.0.1:11434", "--host"),
    via_cli: bool = typer.Option(
        True,
        "--cli/--in-process",
        help="Drive `panelcad agent` over JSONL (default) vs in-process Session",
    ),
    json_out: bool = typer.Option(False, "--json"),
    repeat: int = typer.Option(
        1,
        "--repeat",
        help="Trials for a skill- task. 10/10 means --repeat 10 with no misses.",
    ),
) -> None:
    """Run a weak local model against PanelCAD's JSONL CLI. Traces stay in PanelCAD/.sharpen."""
    import json as _json

    from panelcad.sharpen.loop import run_sharpen
    from panelcad.sharpen.tasks import all_tasks

    if task in {"tasks", "list", "ls"}:
        for item in all_tasks().values():
            typer.echo(f"{item.id:18}  turns≤{item.max_turns:<3}  {item.instruction.splitlines()[0][:70]}")
        return
    if task in {"suite", "all-actions", "all"}:
        from panelcad.sharpen.suite import run_suite

        summary = run_suite(model=model, host=host, via_cli=via_cli)
        if json_out:
            typer.echo(_json.dumps(summary, indent=2))
        else:
            mark = "ok" if summary["ok"] else "FAIL"
            typer.echo(f"{mark}  suite  {summary['elapsed_s']}s  job {summary['job']}")
            for row in summary["tasks"]:
                flag = "ok" if row["ok"] else "FAIL"
                typer.echo(f"  {flag:4}  {row['id']:16}  {row['reason']}")
            if summary["ops_missing"]:
                typer.echo("  missing ops: " + ", ".join(summary["ops_missing"]))
        raise typer.Exit(code=0 if summary["ok"] else 2)
    if task.startswith("skill-") and repeat > 1:
        from panelcad.sharpen.skills import run_skill_streak

        op = task[len("skill-") :]
        try:
            streak = run_skill_streak(
                op,
                n=repeat,
                model=model,
                host=host,
                via_cli=via_cli,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        payload = {
            "ok": streak.ok,
            "task": f"skill-{op}",
            "op": streak.op,
            "n": streak.n,
            "streak": streak.streak,
            "miss": streak.miss,
            "reason": "10/10" if streak.ok and streak.n == 10 else f"{streak.streak}/{streak.n} trailing, miss={streak.miss}",
            "runs": [
                {
                    "ok": row.ok,
                    "reason": row.reason,
                    "job": str(row.job),
                    "trace": str(row.trace),
                    "ops": [t.get("op") for t in row.turns if t.get("op")],
                }
                for row in streak.runs
            ],
        }
        if json_out:
            typer.echo(_json.dumps(payload, indent=2))
        else:
            mark = "ok" if streak.ok else "FAIL"
            typer.echo(f"{mark}  skill-{op}  {payload['reason']}")
            for i, row in enumerate(streak.runs, start=1):
                flag = "ok" if row.ok else "FAIL"
                typer.echo(f"  {flag:4}  {i:02d}/{streak.n}  {row.reason}")
        raise typer.Exit(code=0 if streak.ok else 2)
    if task in {"from-print", "print", "recreate"}:
        from panelcad.sharpen.from_print import run_from_print

        try:
            result = run_from_print(
                pdf=pdf,
                page=page,
                pages=pages,
                model=model,
                host=host,
                via_cli=via_cli,
                standard=standard,
                style=style,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        payload = {
            "ok": result.ok,
            "task": result.task,
            "reason": result.reason,
            "model": result.model,
            "turns": len(result.turns),
            "elapsed_s": result.elapsed_s,
            "job": str(result.job),
            "trace": str(result.trace),
            "ops": [t.get("op") for t in result.turns if t.get("op")],
        }
        if json_out:
            typer.echo(_json.dumps(payload, indent=2))
        else:
            mark = "ok" if result.ok else "FAIL"
            typer.echo(f"{mark}  {result.task}  {result.reason}")
            typer.echo(f"  model {result.model}  turns {payload['turns']}  {result.elapsed_s}s")
            typer.echo(f"  job   {result.job}")
            typer.echo(f"  trace {result.trace}")
        raise typer.Exit(code=0 if result.ok else 2)
    try:
        result = run_sharpen(
            task,
            project=project,
            model=model,
            host=host,
            via_cli=via_cli,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    payload = {
        "ok": result.ok,
        "task": result.task,
        "reason": result.reason,
        "model": result.model,
        "turns": len(result.turns),
        "elapsed_s": result.elapsed_s,
        "job": str(result.job),
        "trace": str(result.trace),
        "ops": [t.get("op") for t in result.turns if t.get("op")],
    }
    if json_out:
        typer.echo(_json.dumps(payload, indent=2))
    else:
        mark = "ok" if result.ok else "FAIL"
        typer.echo(f"{mark}  {result.task}  {result.reason}")
        typer.echo(f"  model {result.model}  turns {payload['turns']}  {result.elapsed_s}s")
        typer.echo(f"  job   {result.job}")
        typer.echo(f"  trace {result.trace}")
    raise typer.Exit(code=0 if result.ok else 2)


@app.command("send")
def send_cmd(
    project: Path,
    payload: Optional[str] = typer.Argument(None, help="One JSON object, or omit to read JSONL stdin"),
    model: str = typer.Option("Grok 4.6", "--model", help="Used with --hello"),
    hello: bool = typer.Option(False, "--hello", help="Announce the AI to the open GUI"),
    idle: bool = typer.Option(False, "--idle"),
    say: Optional[str] = typer.Option(None, "--say", help="Status line in the AI window"),
) -> None:
    """Queue Session ops for an open GUI (AI never clicks the window)."""
    import json as _json
    import sys

    from panelcad.api.activity import send

    messages: list[dict] = []
    if hello:
        messages.append({"op": "hello", "model": model, "name": "Grok"})
    if say:
        messages.append({"op": "say", "text": say})
    if idle:
        messages.append({"op": "idle"})
    if payload:
        messages.append(_json.loads(payload))
    elif not messages and sys.stdin is not None and not sys.stdin.isatty():
        for line in sys.stdin:
            line = line.strip()
            if line:
                messages.append(_json.loads(line))
    if not messages:
        raise typer.BadParameter("pass JSON, --hello, --say, --idle, or JSONL on stdin")
    dest = None
    for msg in messages:
        dest = send(project, msg)
    typer.echo(f"queued {len(messages)} op(s) → {dest}")


@app.command()
def gui(project: Optional[Path] = typer.Argument(None)) -> None:
    from panelcad.gui.app import run_gui

    run_gui(project)


@app.command()
def validate(
    project: Path,
    design: bool = typer.Option(False, "--design", help="Also print live design checks (warnings/info)"),
) -> None:
    s = Session.open(project)
    errors = s.validate()
    if errors:
        for e in errors:
            typer.echo(e)
        raise typer.Exit(code=1)
    if design:
        from panelcad.core.checks import STRUCTURAL_CHECKS

        leftover = [
            row
            for row in s.design_checks()
            if not (row["severity"] == "error" and row["check"] in STRUCTURAL_CHECKS)
        ]
        if leftover:
            for row in leftover:
                typer.echo(f"{row['severity']:7} {row['check']:28} {row['message']}")
            if any(row["severity"] == "error" for row in leftover):
                raise typer.Exit(code=1)
            typer.echo(f"ok  ({len(leftover)} design note(s))")
            return
    typer.echo("ok")


@app.command()
def inspect(
    project: Path,
    sheet: Optional[str] = typer.Option(None, "--sheet"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable snapshot"),
) -> None:
    """Dump the project so an AI or script can read it without the GUI."""
    import json

    s = Session.open(project)
    data = s.describe(sheet)
    if json_out:
        typer.echo(json.dumps(data, indent=2, default=str))
        return
    typer.echo(f"{data['name']}  sheets={data['sheet_count']}  active={data['active_sheet']}")
    tb = data["title_block"]
    typer.echo(f"  dwg {tb.get('drawing_number') or '-'}  rev {(tb.get('revisions') or [{}])[0].get('rev', '-')}")
    for sh in data["sheets"]:
        typer.echo(f"  [{sh['index']}/{sh['count']}] {sh['id']}  {sh['title']}  ({sh['kind']})")
        for c in sh["components"]:
            typer.echo(f"      {c['id']:6}  {c['library_id']}  pins={','.join(c['pins'][:8])}")
        for w in sh["wires"]:
            typer.echo(f"      wire {w['from']} -> {w['to']}  {w['net'] or ''}")
    if data["errors"]:
        raise typer.Exit(code=1)


def _agent_snapshot(project: Path, sheet: str | None = None) -> dict[str, Any]:
    """Project state, Problems, and the wire list for an outside agent."""

    session = Session.open(project)
    data = session.describe(sheet)
    data["problems"] = session.design_checks()
    data["wire_list"] = session.wire_list_rows()
    data["from_to"] = session.from_to_rows()
    return data


@app.command("describe")
def describe_cmd(
    project: Path,
    sheet: Optional[str] = typer.Option(None, "--sheet"),
    json_out: bool = typer.Option(False, "--json", help="Project, Problems, and wire list as JSON"),
) -> None:
    """Read the project back: sheets, devices, Problems, and wires."""

    data = _agent_snapshot(project, sheet)
    if json_out:
        typer.echo(json.dumps(data, indent=2, default=str))
    else:
        typer.echo(f"{data['name']}  sheets={data['sheet_count']}  active={data['active_sheet']}")
        typer.echo(f"problems {len(data['problems'])}  wires {len(data['wire_list'])}")
        for row in data["problems"]:
            typer.echo(f"{row['severity']:7} {row['check']:28} {row['message']}")
        for row in data["wire_list"]:
            typer.echo(
                f"  {row.get('number') or '-':8} {row.get('from_device') or row.get('from') or ''} "
                f"-> {row.get('to_device') or row.get('to') or ''}"
            )
    if data["errors"]:
        raise typer.Exit(code=1)


@app.command("check")
def check_cmd(
    project: Path,
    json_out: bool = typer.Option(False, "--json", help="Problems and structural errors as JSON"),
    include_suppressed: bool = typer.Option(False, "--include-suppressed"),
) -> None:
    """Design Problems plus structural errors. Same findings as the Problems dock."""

    from panelcad.importer.agent_questions import questions_for

    session = Session.open(project)
    errors = session.validate()
    problems = session.design_checks(include_suppressed=include_suppressed)
    questions = questions_for(session)
    failed = bool(errors) or any(row.get("severity") == "error" for row in problems)
    if json_out:
        typer.echo(
            json.dumps(
                {"ok": not failed, "errors": errors, "problems": problems, "questions": questions},
                indent=2,
                default=str,
            )
        )
    else:
        for error in errors:
            typer.echo(f"error   {error}")
        for row in problems:
            typer.echo(f"{row['severity']:7} {row['check']:28} {row['message']}")
        if questions:
            typer.echo(f"questions {len(questions)}")
        if not errors and not problems:
            typer.echo("ok")
    if failed:
        raise typer.Exit(code=1)


@app.command("sheets")
def sheets_cmd(project: Path) -> None:
    s = Session.open(project)
    for row in s.list_sheets():
        mark = "*" if row["active"] else " "
        typer.echo(f"{mark} {row['index']}/{row['count']}  {row['id']:16}  {row['title']}  ({row['kind']})  {row['components']} devices")


def _sheet_for_ref(session: Session, ref: str, sheet: str | None) -> str | None:
    """Active sheet when it holds ``ref``. Otherwise the only other sheet that does."""

    if sheet:
        return sheet
    hits = [
        page.id
        for page in session.project.sheets
        if any(comp.id == ref for comp in page.components)
    ]
    if not hits:
        return None
    active = session.sheet().id
    if active in hits or len(hits) > 1:
        return active if active in hits else None
    return hits[0]


@app.command("rename")
def rename_cmd(
    project: Path,
    old: str,
    new: str,
    sheet: Optional[str] = typer.Option(None, "--sheet"),
) -> None:
    """Rename a component. The new tag is used on wires, the I/O list, and the sheet."""
    s = Session.open(project)
    try:
        found = _sheet_for_ref(s, old, sheet)
        if sheet is None and found is None and any(
            comp.id == old for page in s.project.sheets for comp in page.components
        ):
            raise ValueError(f"{old} is on more than one sheet; pass --sheet")
        result = s.rename_component(old, new, sheet_id=found)
    except (KeyError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    _save(s, project)
    typer.echo(f"{result.get('old', old)} -> {result.get('new', new)}")


@app.command("delete")
def delete_cmd(
    project: Path,
    ref: str,
    sheet: Optional[str] = typer.Option(None, "--sheet"),
) -> None:
    """Delete a component and the wires that land on it."""
    s = Session.open(project)
    try:
        found = _sheet_for_ref(s, ref, sheet)
        if sheet is None and found is None and any(
            comp.id == ref for page in s.project.sheets for comp in page.components
        ):
            raise ValueError(f"{ref} is on more than one sheet; pass --sheet")
        s.delete_component(ref, sheet_id=found)
    except (KeyError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    _save(s, project)
    typer.echo(f"deleted {ref}")


@app.command()
def device(library_id: str) -> None:
    import json

    s = Session.create("tmp")
    typer.echo(json.dumps(s.inspect_device(library_id), indent=2))


@app.command("convert")
def convert_cmd(
    project: Path,
    to: Optional[str] = typer.Option(None, "--to", help="iec or nema"),
    sheets: Optional[str] = typer.Option(None, "--sheets", help="Comma-separated sheet ids; omit for the whole project"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Save-as path. Never the source file."),
    resume: Optional[Path] = typer.Option(None, "--resume", help="Workspace JSON from a stopped conversion"),
    keep_numbers: bool = typer.Option(
        False,
        "--keep-numbers",
        help="Keep each device's number. Omit to number each letter family 1, 2, 3.",
    ),
) -> None:
    """Convert a project between IEC and NEMA, one sheet and one circuit at a time."""

    import json

    if resume:
        ws = json.loads(Path(resume).read_text(encoding="utf-8"))
        source = Path(str(ws.get("source_path") or project))
        session = Session.open(source) if source.is_file() else Session.open(project)
        session.convert_standard_plan("", resume=resume)
        result = session.convert_standard_apply(resume, out=out)
    else:
        if not to:
            raise typer.BadParameter("pass --to iec|nema, or --resume a workspace")
        chosen = [part.strip() for part in sheets.split(",") if part.strip()] if sheets else None
        session = Session.open(project)
        result = session.convert_standard(to, sheets=chosen, out=out, keep_numbers=keep_numbers)
    typer.echo(
        json.dumps(
            {
                "out": result.get("out_path"),
                "target": result.get("target"),
                "done": result.get("done"),
                "pending": result.get("pending"),
                "tag_map": result.get("tag_map"),
                "unmapped": result.get("unmapped"),
                "chunks": [
                    {"id": row["id"], "status": row["status"], "issues": row.get("issues") or []}
                    for row in result.get("chunks") or []
                ],
            },
            indent=2,
        )
    )


elec = typer.Typer(
    help="Wire numbers, tags, cross-reference, terminals, I/O, schematic/panel audit, motor circuits, and SCCR"
)
app.add_typer(elec, name="electrical")

io_app = typer.Typer(help="PLC I/O modules and schematic sheets")
app.add_typer(io_app, name="io")


@io_app.command("modules")
def io_modules_cmd(
    platform: Optional[str] = typer.Option(None, "--platform", help="brx, click, compactlogix, micro800, s7-1200, …"),
) -> None:
    """List I/O modules. Unverified rows print the reason they will not auto-draw."""
    from panelcad.library.io_modules import list_modules, unverified_message

    catalog = Session.create("io-modules").catalog
    for module in list_modules(catalog, platform):
        state = "verified" if module.verified else "unverified"
        name = module.catalog or module.id
        line = f"{state:10} {module.platform:16} {module.id:28} {name}"
        if not module.verified:
            line += f"  — {unverified_message(module)}"
        typer.echo(line)


@io_app.command("generate")
def io_generate_cmd(
    project: Path,
    module: list[str] = typer.Option([], "--module", help="Verified module id. Repeat for more than one."),
    redraw: bool = typer.Option(False, "--all", help="Redraw every I/O module already in the project."),
    ref: Optional[str] = typer.Option(None, "--ref", help="Tag when only one module is added."),
    slot: int = typer.Option(1, "--slot"),
    position: int = typer.Option(1, "--position", help="CLICK expansion position. 1 is the first module after the CPU."),
    polarity: str = typer.Option("", "--polarity", help="sink or source"),
    address_base: Optional[int] = typer.Option(None, "--address-base"),
    fuses: Optional[bool] = typer.Option(None, "--fuses/--no-fuses"),
    points: Optional[Path] = typer.Option(
        None, "--points", help="CSV: pin, tag, description, field. Optional module column when several modules are added."
    ),
) -> None:
    """Add verified I/O modules and draw their schematic sheets.

    ``--all`` redraws modules already in the project and does not add one.
    """
    from panelcad.core.io_sheets import assign_point

    if not module and not redraw:
        typer.echo("pass --module to add a card, or --all to redraw", err=True)
        raise typer.Exit(1)
    s = Session.open(project) if project.exists() else Session.create(project.stem or "I/O")
    try:
        if redraw and not module:
            result = s.io_generate_sheets(fuses=fuses)
            _save(s, project)
            typer.echo(f"sheets {', '.join(result['sheets'])}")
            for warning in result.get("warnings") or []:
                typer.echo(f"warning: {warning}")
            return
        added = []
        for index, module_id in enumerate(module):
            added.append(
                s.io_add_module(
                    module_id,
                    ref=ref if len(module) == 1 else None,
                    slot=slot + index,
                    position=position + index,
                    polarity=polarity,
                    address_base=address_base,
                )
            )
        if points is not None:
            raw = points.read_text(encoding="utf-8-sig")
            reader = csv.DictReader(raw.splitlines())
            by_ref = {row["ref"]: row for row in added}
            only = added[0]["ref"] if len(added) == 1 else ""
            for row in reader:
                if not row:
                    continue
                lowered = {(key or "").strip().lower(): (value or "").strip() for key, value in row.items()}
                pin = lowered.get("pin") or lowered.get("address") or ""
                if not pin:
                    continue
                target = lowered.get("module") or only
                if target not in by_ref and target not in {item["module"] for item in added}:
                    match = next((item for item in added if item["module"] == target or item["catalog"] == target), None)
                    target = match["ref"] if match else target
                elif target in {item["module"] for item in added}:
                    target = next(item["ref"] for item in added if item["module"] == target)
                assign_point(
                    s,
                    target,
                    pin,
                    tag=lowered.get("tag") or None,
                    description=lowered.get("description") or None,
                    field=lowered.get("field") or None,
                )
        result = s.io_generate_sheets(fuses=fuses)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    _save(s, project)
    typer.echo(f"sheets {', '.join(result['sheets'])}")
    for warning in result.get("warnings") or []:
        typer.echo(f"warning: {warning}")


@io_app.command("regenerate")
def io_regenerate_cmd(
    project: Path,
    fuses: Optional[bool] = typer.Option(None, "--fuses/--no-fuses"),
) -> None:
    """Redraw every I/O sheet already in the project. Does not add a module."""
    s = Session.open(project)
    try:
        result = s.io_generate_sheets(fuses=fuses)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    _save(s, project)
    typer.echo(f"sheets {', '.join(result['sheets'])}")
    for warning in result.get("warnings") or []:
        typer.echo(f"warning: {warning}")


def _save(session: Session, project: Path) -> None:
    session.save(project)


@elec.command("number-wires")
def electrical_number_wires(
    project: Path,
    start: int = typer.Option(1, "--start"),
    overwrite: bool = typer.Option(False, "--overwrite"),
    scheme: Optional[str] = typer.Option(None, "--scheme", help="sheet_line, sequential, or iec"),
) -> None:
    """Full renumber. Locked and conventional names stay. One undo in the GUI/Session."""
    s = Session.open(project)
    result = s.number_wires(start=start, overwrite=overwrite, scheme=scheme)
    _save(s, project)
    typer.echo(
        f"numbered {result['assigned']} wires across {result['nets']} nets  "
        f"({result.get('scheme') or 'sheet_line'})"
    )


@app.command("wire-numbering")
def wire_numbering_cmd(
    project: Path,
    scheme: Optional[str] = typer.Option(None, "--scheme", help="sheet_line (default), sequential, iec"),
    start: Optional[int] = typer.Option(None, "--start"),
    auto: Optional[bool] = typer.Option(None, "--auto/--no-auto", help="Assign numbers as wires are drawn"),
    split_feedthrough: Optional[bool] = typer.Option(
        None,
        "--split-feedthrough/--no-split-feedthrough",
        help="Give each side of a feed-through terminal its own number",
    ),
) -> None:
    """Show or set automatic wire numbers. Changing the feed-through split renumbers unlocked wires."""
    s = Session.open(project)
    if scheme is not None or start is not None or auto is not None or split_feedthrough is not None:
        s.set_wire_numbering(scheme=scheme, enabled=auto, start=start, split_feedthrough=split_feedthrough)
        _save(s, project)
    settings = s.wire_numbering()
    state = "on" if settings["enabled"] else "off"
    split = "on" if settings.get("split_feedthrough") else "off"
    typer.echo(f"scheme {settings['scheme']}  start {settings['start']}  auto {state}  split-feedthrough {split}")


@elec.command("numbering")
def electrical_numbering(
    project: Path,
    scheme: Optional[str] = typer.Option(None, "--scheme", help="sheet_line (default), sequential, iec"),
    start: Optional[int] = typer.Option(None, "--start"),
    auto: Optional[bool] = typer.Option(None, "--auto/--no-auto", help="Assign numbers as wires are drawn"),
) -> None:
    """Show or set automatic wire-number settings. Opening a file never renumbers."""
    s = Session.open(project)
    if scheme is not None or start is not None or auto is not None:
        s.set_wire_numbering(scheme=scheme, enabled=auto, start=start)
        _save(s, project)
    settings = s.wire_numbering()
    state = "on" if settings["enabled"] else "off"
    typer.echo(f"scheme {settings['scheme']}  start {settings['start']}  auto {state}")


@elec.command("set-wire")
def electrical_set_wire(
    project: Path,
    number: Optional[str] = typer.Option(None, "--number", help="Wire number. Empty string clears it. Setting a number locks it."),
    wire_id: Optional[str] = typer.Option(None, "--id", help="Wire id, such as w1"),
    frm: Optional[str] = typer.Option(None, "--from", help="device.terminal"),
    to: Optional[str] = typer.Option(None, "--to", help="device.terminal"),
    sheet: Optional[str] = typer.Option(None, "--sheet"),
    color: Optional[str] = typer.Option(None, "--color"),
    gauge: Optional[str] = typer.Option(None, "--gauge"),
    wire_type: Optional[str] = typer.Option(None, "--type", help="Conductor type (MTW, THHN, …)"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Set one wire's number, color, gauge, or type. Same fields as JSONL set_wire_spec."""

    if all(value is None for value in (number, color, gauge, wire_type)):
        raise typer.BadParameter("pass --number, --color, --gauge, or --type")
    session = Session.open(project)
    try:
        wire = session.set_wire_spec(
            wire_id,
            frm=frm,
            to=to,
            sheet_id=sheet,
            number=number,
            color=color,
            gauge=gauge,
            wire_type=wire_type,
        )
    except (KeyError, ValueError) as exc:
        typer.echo(str(exc).splitlines()[0], err=True)
        raise typer.Exit(code=1) from None
    _save(session, project)
    payload = {
        "id": wire.id,
        "from": wire.frm,
        "to": wire.to,
        "number": wire.number,
        "number_locked": wire.number_locked,
        "color": wire.color,
        "gauge": wire.gauge,
        "type": wire.wire_type,
    }
    if json_out:
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"{payload['id']}  {payload['from']} -> {payload['to']}  {payload['number'] or '-'}")


@elec.command("wire-list")
def electrical_wire_list(
    project: Path,
    out: Path = typer.Option(..., "--out", "-o"),
) -> None:
    """Legacy from/to wire list: device, terminal, number, color, gauge."""
    s = Session.open(project)
    s.export_wire_list(out)
    typer.echo(f"wires {out}  {len(s.wire_list_rows())} rows")


@elec.command("from-to")
def electrical_from_to(
    project: Path,
    out: Path = typer.Option(..., "--out", "-o"),
    sort: str = typer.Option("number", "--sort", help="number or device"),
) -> None:
    """From-to report: one conductor per row, from the netlist. Does not renumber."""
    s = Session.open(project)
    s.export_from_to(out, sort=sort)
    typer.echo(f"from-to {out}  {len(s.from_to_rows(sort=sort))} rows  sort {sort}")


@elec.command("net-spec")
def electrical_net_spec(
    project: Path,
    net: str = typer.Option(
        ...,
        "--net",
        help="Net name. * or all is the fallback for every net without its own spec.",
    ),
    size: Optional[str] = typer.Option(None, "--size", help="Gauge / cross-section. Empty string clears."),
    color: Optional[str] = typer.Option(None, "--color"),
    wire_type: Optional[str] = typer.Option(None, "--type", help="Conductor type (MTW, THHN, …)"),
) -> None:
    """Set the size, color, and type a net's wires inherit when their own fields are blank."""
    s = Session.open(project)
    result = s.set_net_wire_spec(net, size=size, color=color, wire_type=wire_type)
    _save(s, project)
    typer.echo(
        f"net {result['net']}  size {result['size'] or '-'}  "
        f"color {result['color'] or '-'}  type {result['type'] or '-'}"
    )


@elec.command("wire-markers")
def electrical_wire_markers(
    project: Path,
    out: Path = typer.Option(..., "--out", "-o"),
    target: str = typer.Option(
        "generic",
        "--target",
        help="generic, phoenix, weidmueller, or brady. Weidmüller and Brady are the generic CSV.",
    ),
    style: str = typer.Option("heat-shrink", "--style", help="heat-shrink or clip-on"),
    sort: str = typer.Option("number", "--sort", help="number or device"),
) -> None:
    """One marker per wire end. Phoenix is a single column; other vendors get the generic CSV."""
    s = Session.open(project)
    result = s.export_wire_markers(out, target=target, style=style, sort=sort)
    typer.echo(
        f"markers {result['path']}  {result['rows']}  {result['target']}  "
        f"{result['style']}  {result['confidence']}"
    )
    typer.echo(result["note"])


@elec.command("retag")
def electrical_retag(
    project: Path,
    mode: str = typer.Option("duplicates", "--mode", help="duplicates, family, or standard"),
    apply_prefixes: bool = typer.Option(
        False,
        "--apply-prefixes",
        help="Remap tags to the project's drawing-standard prefixes (JIC/NFPA on NEMA/ANSI)",
    ),
) -> None:
    s = Session.open(project)
    renamed = s.retag(mode, apply_prefixes=apply_prefixes)
    _save(s, project)
    for row in renamed:
        typer.echo(f"{row['sheet']}: {row['from']} -> {row['to']}")
    typer.echo(f"renamed {len(renamed)}")


@elec.command("xref")
def electrical_xref(
    project: Path,
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    write: bool = typer.Option(
        False,
        "--write",
        help="Persist properties.xref and off-page callouts, then save. Display is derived without this.",
    ),
    nc_mark: Optional[str] = typer.Option(
        None,
        "--nc-mark",
        help="NC contact mark: underline (default), bar, or parentheses",
    ),
    device: Optional[str] = typer.Option(
        None,
        "--device",
        help="List every element of this device tag (page, zone, terminals, wires).",
    ),
    zone: Optional[str] = typer.Option(
        None,
        "--zone",
        help="What is at this page-zone (5-A2, A2, /1.3, 105, sheet:A2).",
    ),
    wire: Optional[str] = typer.Option(
        None,
        "--wire",
        help="Trace this wire number across sheets.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the index payload as JSON."),
) -> None:
    """Device cross-reference and troubleshooting index. Derived unless --write."""
    s = Session.open(project)
    if nc_mark is not None:
        s.set_cross_reference(nc_mark=nc_mark)
        if write:
            _save(s, project)
    if device is not None or zone is not None or wire is not None:
        try:
            if device is not None:
                payload = s.device_index(device)
            elif zone is not None:
                payload = s.lookup_zone(zone)
            else:
                payload = s.trace_wire(str(wire))
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if write:
        rows = s.update_cross_references()
        _save(s, project)
    else:
        rows = s.cross_reference_rows()
    if out:
        s.export_cross_reference(out)
        typer.echo(f"xref {out}")
    if as_json:
        typer.echo(json.dumps({"rows": rows, "index": s.device_index()}, indent=2, sort_keys=True))
        return
    typer.echo(f"{len(rows)} symbols")


def _troubleshoot_payload(payload: dict) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _troubleshoot_readings(raw: Optional[str]) -> list[dict] | None:
    from panelcad.core.xref_index import QueryError

    if raw in (None, ""):
        return None
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise QueryError("readings must be a JSON list") from exc
    if not isinstance(parsed, list):
        raise QueryError("readings must be a JSON list")
    return parsed


def _troubleshoot_call(project: Path, fn) -> None:
    from panelcad.core.xref_index import QueryError

    session = Session.open(project)
    try:
        payload = fn(session)
    except QueryError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _troubleshoot_payload(payload)


ts_app = typer.Typer(help="Read-only troubleshooting from the drawing. Does not edit the project file.")
elec.add_typer(ts_app, name="troubleshoot")


@ts_app.command("circuit")
def electrical_troubleshoot_circuit(
    project: Path,
    ref: str = typer.Option(..., "--ref", help="Device tag on the drawing."),
) -> None:
    """Control and power path for one device. Missing voltages are needs-info."""

    _troubleshoot_call(project, lambda session: session.circuit_for(ref))


@ts_app.command("checkpoints")
def electrical_troubleshoot_checkpoints(
    project: Path,
    ref: str = typer.Option(..., "--ref", help="Device tag on the drawing."),
    symptom: str = typer.Option(..., "--symptom", help="Symptom id, for example motor_wont_start."),
) -> None:
    """Ordered LIVE and DE-ENERGIZED checks. Each flow starts with lockout text."""

    _troubleshoot_call(project, lambda session: session.checkpoints(ref, symptom))


@ts_app.command("upstream")
def electrical_troubleshoot_upstream(
    project: Path,
    endpoint: str = typer.Option(..., "--endpoint", help="Terminal as ref.pin, for example K1.A1."),
) -> None:
    """Walk from a terminal toward the supply."""

    _troubleshoot_call(project, lambda session: session.upstream(endpoint))


@ts_app.command("downstream")
def electrical_troubleshoot_downstream(
    project: Path,
    endpoint: str = typer.Option(..., "--endpoint", help="Terminal as ref.pin, for example K1.A1."),
) -> None:
    """Walk from a terminal toward the load or return."""

    _troubleshoot_call(project, lambda session: session.downstream(endpoint))


@ts_app.command("plc-point")
def electrical_troubleshoot_plc_point(
    project: Path,
    address: str = typer.Option(..., "--address", help="PLC address, point label, or tag on the drawing."),
) -> None:
    """One PLC I/O point from the drawing."""

    _troubleshoot_call(project, lambda session: session.plc_point(address))


@ts_app.command("causes")
def electrical_troubleshoot_causes(
    project: Path,
    ref: str = typer.Option(..., "--ref", help="Device tag on the drawing."),
    symptom: str = typer.Option(..., "--symptom", help="Symptom id, for example motor_wont_start."),
    readings: Optional[str] = typer.Option(
        None,
        "--readings",
        help='JSON list of {"id"|"device"+"terminal"|"wire", "result": "ok"|"wrong"|"skipped"}.',
    ),
) -> None:
    """Ranked causes. A first wrong reading narrows the fault to that segment."""

    _troubleshoot_call(
        project,
        lambda session: session.likely_causes(ref, symptom, _troubleshoot_readings(readings)),
    )


@ts_app.command("guide")
def electrical_troubleshoot_guide(
    project: Path,
    ref: str = typer.Option(..., "--ref", help="Device tag on the drawing."),
    symptom: str = typer.Option(..., "--symptom", help="Symptom id, for example motor_wont_start."),
    readings: Optional[str] = typer.Option(None, "--readings", help="JSON list of readings."),
) -> None:
    """Guided next check, fault window, and causes. Same walk as the Troubleshoot dock."""

    _troubleshoot_call(
        project,
        lambda session: session.troubleshoot_guide(ref, symptom, _troubleshoot_readings(readings)),
    )


@ts_app.command("session-csv")
def electrical_troubleshoot_session_csv(
    project: Path,
    out: Path = typer.Option(..., "--out", "-o", help="CSV path."),
    ref: str = typer.Option(..., "--ref", help="Device tag on the drawing."),
    symptom: str = typer.Option(..., "--symptom", help="Symptom id."),
    readings: Optional[str] = typer.Option(None, "--readings", help="JSON list of readings."),
    who: str = typer.Option("", "--who", help="Who took the readings."),
    note: str = typer.Option("", "--note", help="Note stored with the export."),
) -> None:
    """Write the guided session as CSV. The project file is not changed."""

    from panelcad.core.xref_index import QueryError

    session = Session.open(project)
    try:
        dest = session.export_troubleshoot_csv(
            out,
            ref=ref,
            symptom=symptom,
            readings=_troubleshoot_readings(readings),
            who=who,
            note=note,
        )
    except QueryError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"troubleshoot {dest}")


@ts_app.command("session-pdf")
def electrical_troubleshoot_session_pdf(
    project: Path,
    out: Path = typer.Option(..., "--out", "-o", help="PDF path."),
    ref: str = typer.Option(..., "--ref", help="Device tag on the drawing."),
    symptom: str = typer.Option(..., "--symptom", help="Symptom id."),
    readings: Optional[str] = typer.Option(None, "--readings", help="JSON list of readings."),
    who: str = typer.Option("", "--who", help="Who took the readings."),
    note: str = typer.Option("", "--note", help="Note stored with the export."),
) -> None:
    """Write the guided session as PDF. The page opens with the lockout text."""

    from panelcad.core.xref_index import QueryError

    session = Session.open(project)
    try:
        dest = session.export_troubleshoot_pdf(
            out,
            ref=ref,
            symptom=symptom,
            readings=_troubleshoot_readings(readings),
            who=who,
            note=note,
        )
    except QueryError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"troubleshoot {dest}")


@ts_app.command("export")
def electrical_troubleshoot_export(
    project: Path,
    out: Path = typer.Option(..., "--out", "-o", help="troubleshoot.json path."),
) -> None:
    """Write the open troubleshoot.json document. The project file is not changed."""

    session = Session.open(project)
    dest = session.export_troubleshoot(out)
    typer.echo(f"troubleshoot {dest}")


@ts_app.command("log")
def electrical_troubleshoot_log(
    project: Path,
    ref: str = typer.Option(..., "--ref", help="Device tag on the drawing."),
    symptom: str = typer.Option(..., "--symptom", help="Symptom id."),
    readings: Optional[str] = typer.Option(None, "--readings", help="JSON list of readings."),
    who: str = typer.Option("", "--who", help="Who took the readings."),
    note: str = typer.Option("", "--note", help="Note stored with the entry."),
) -> None:
    """Append readings to <project>.troubleshoot-log.json beside the project."""

    _troubleshoot_call(
        project,
        lambda session: session.append_troubleshoot_log(
            ref=ref,
            symptom=symptom,
            readings=_troubleshoot_readings(readings),
            who=who,
            note=note,
            project_path=project,
        ),
    )


@elec.command("xref-settings")
def electrical_xref_settings(
    project: Path,
    nc_mark: Optional[str] = typer.Option(None, "--nc-mark", help="underline, bar, or parentheses"),
    enabled: Optional[bool] = typer.Option(None, "--on/--off", help="Draw live cross-references"),
    rung_pitch_mm: Optional[float] = typer.Option(None, "--rung-pitch", help="NEMA rung pitch in mm (default 12.7 = 0.5 in)"),
    rung_numbers: Optional[str] = typer.Option(None, "--rung-numbers", help="used (rungs with content) or all"),
) -> None:
    """Show or set cross-reference display settings. Opening a file never writes refs."""
    s = Session.open(project)
    if any(v is not None for v in (nc_mark, enabled, rung_pitch_mm, rung_numbers)):
        s.set_cross_reference(
            nc_mark=nc_mark,
            enabled=enabled,
            rung_pitch_mm=rung_pitch_mm,
            rung_numbers=rung_numbers,
        )
        _save(s, project)
    settings = s.cross_reference_settings()
    state = "on" if settings["enabled"] else "off"
    typer.echo(
        f"nc_mark {settings['nc_mark']}  draw {state}  "
        f"rung_pitch {settings['rung_pitch_mm']:g}  rung_numbers {settings['rung_numbers']}"
    )


@elec.command("terminals")
def electrical_terminals(
    project: Path,
    out: Path = typer.Option(..., "--out", "-o"),
    strip: Optional[str] = typer.Option(None, "--strip", help="Resequence this strip before export"),
    rename: bool = typer.Option(False, "--rename", help="Rename levels to STRIP-n"),
) -> None:
    s = Session.open(project)
    if strip:
        s.sequence_terminal_strip(strip, rename=rename)
        _save(s, project)
    s.export_terminal_strips(out)
    typer.echo(f"terminals {out}")


def _refs(text: str) -> list[str]:
    return [part.strip() for part in str(text or "").split(",") if part.strip()]


@elec.command("strip-list")
def electrical_strip_list(project: Path) -> None:
    """List terminal strips, how many levels each has, and the drawing sheet."""
    s = Session.open(project)
    rows = s.list_terminal_strips()
    if not rows:
        typer.echo("no terminal strips")
        return
    for row in rows:
        sheet = row["sheet"] or "—"
        typer.echo(f"{row['strip']:8} {row['terminals']:3} terminals  {sheet}")


@elec.command("strip-open")
def electrical_strip_open(
    project: Path,
    strip: str = typer.Option(..., "--strip", help="Strip name, such as X1"),
) -> None:
    """Open the DIN-rail drawing and add end stops and end plates if they are missing."""
    s = Session.open(project)
    try:
        result = s.open_terminal_strip(strip)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _save(s, project)
    n = sum(1 for item in result["items"] if item["kind"] == "terminal")
    typer.echo(f"strip {result['strip']} on {result['sheet']}  {n} terminals")


@elec.command("strip-add")
def electrical_strip_add(
    project: Path,
    strip: str = typer.Option(..., "--strip"),
    library_id: str = typer.Option("iec.terminals.x_2p5", "--library", help="Terminal block library id"),
    after: Optional[str] = typer.Option(None, "--after", help="Insert after this terminal ref"),
) -> None:
    """Add a terminal on the schematic that already holds the strip."""
    s = Session.open(project)
    try:
        result = s.add_terminal(strip, library_id=library_id, after=after)
    except (ValueError, KeyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _save(s, project)
    typer.echo(f"{result['ref']}  level {result['terminal']}  on {result['sheet']}")


@elec.command("strip-reorder")
def electrical_strip_reorder(
    project: Path,
    strip: str = typer.Option(..., "--strip"),
    refs: str = typer.Option(..., "--refs", help="Every terminal on the strip, comma-separated, in the new order"),
) -> None:
    """Set the level order. Schematic positions stay put."""
    s = Session.open(project)
    try:
        result = s.reorder_terminals(strip, _refs(refs))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _save(s, project)
    order = [item["ref"] for item in result["items"] if item["kind"] == "terminal"]
    typer.echo(" ".join(order))


@elec.command("strip-move")
def electrical_strip_move(
    project: Path,
    strip: str = typer.Option(..., "--strip"),
    ref: str = typer.Option(..., "--ref"),
    direction: str = typer.Option("later", "--direction", help="earlier or later"),
) -> None:
    s = Session.open(project)
    try:
        result = s.move_terminal(strip, ref, direction)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _save(s, project)
    order = [item["ref"] for item in result["items"] if item["kind"] == "terminal"]
    typer.echo(" ".join(order))


@elec.command("strip-retag")
def electrical_strip_retag(
    project: Path,
    strip: str = typer.Option(..., "--strip"),
) -> None:
    """Rename tags to STRIP-n in level order. Wires follow."""
    s = Session.open(project)
    try:
        result = s.retag_terminal_strip(strip)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _save(s, project)
    for row in result["renamed"]:
        typer.echo(f"{row['from']} -> {row['to']}")
    typer.echo(f"renamed {len(result['renamed'])}")


@elec.command("strip-accessory")
def electrical_strip_accessory(
    project: Path,
    strip: str = typer.Option(..., "--strip"),
    kind: str = typer.Option(..., "--kind", help="end_plate, end_stop, or partition"),
    after: Optional[str] = typer.Option(None, "--after", help="Partition sits after this terminal"),
) -> None:
    s = Session.open(project)
    try:
        result = s.add_strip_accessory(strip, kind, after=after)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _save(s, project)
    typer.echo(f"{result['ref']}  {result['kind']}  on {result['sheet']}")


@elec.command("strip-export")
def electrical_strip_export(
    project: Path,
    strip: str = typer.Option(..., "--strip"),
    vendor: str = typer.Option(..., "--vendor", help="phoenix, clipx, wago, weidmueller (clipx/wagoxml write XML)"),
    out: Path = typer.Option(..., "--out", "-o", help="CSV or XML path"),
    file_format: Optional[str] = typer.Option(None, "--format", help="csv or xml (default from vendor and file suffix)"),
) -> None:
    """Export one strip for Phoenix, WAGO, or Weidmüller. CSV is the confirmed fallback."""
    s = Session.open(project)
    try:
        result = s.export_terminal_strip(out, strip, vendor, file_format=file_format)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    extra = f"  {result['unmapped']} unmapped" if result.get("unmapped") else ""
    typer.echo(f"{result['product']}  {result['format']}  {result['rows']} rows{extra}  {result['path']}")
    for warning in result.get("warnings") or []:
        typer.echo(f"warning: {warning['message']}", err=True)


@elec.command("strip-delete")
def electrical_strip_delete(
    project: Path,
    ref: str = typer.Option(..., "--ref", help="Terminal or accessory tag to remove from the strip and the schematic"),
) -> None:
    s = Session.open(project)
    try:
        result = s.delete_strip_component(ref)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _save(s, project)
    typer.echo(f"deleted {result['ref']}")


@elec.command("strip-jumper")
def electrical_strip_jumper(
    project: Path,
    refs: str = typer.Option(..., "--refs", help="Two or more terminal refs, comma-separated"),
    group: Optional[str] = typer.Option(None, "--group", help="Jumper id. Default is the next J-number"),
    clear: bool = typer.Option(False, "--clear", help="Remove the jumper from these terminals"),
) -> None:
    s = Session.open(project)
    try:
        if clear:
            result = s.clear_terminal_jumpers(refs=_refs(refs), group=group)
        else:
            result = s.jumper_terminals(_refs(refs), group=group)
    except (ValueError, KeyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _save(s, project)
    if clear:
        typer.echo(f"cleared  {' '.join(result.get('cleared') or [])}")
        return
    parts = result.get("parts") or []
    extra = ""
    if parts:
        extra = f"  {parts[0].get('part_number') or parts[0].get('catalog') or ''}"
    typer.echo(f"{result['jumper']}  {' '.join(result['refs'])}{extra}".rstrip())


@elec.command("audit")
def electrical_audit(
    project: Path,
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
) -> None:
    """Schematic devices missing a panel or door footprint, and the reverse."""
    s = Session.open(project)
    rows = s.audit_schematic_panel()
    if out:
        s.export_audit(out)
        typer.echo(f"audit {out}")
    for row in rows:
        typer.echo(f"{row['issue']:20} {row['ref']:8} {row['library_id']}  ({row['sheets']})")
    if rows:
        raise typer.Exit(code=1)
    typer.echo("ok")


@elec.command("power-budget")
def electrical_power_budget(
    project: Path,
    margin: Optional[float] = typer.Option(None, "--margin", help="Steady-state limit as a fraction of nameplate (0.8 = 80%)"),
    supply: Optional[str] = typer.Option(None, "--supply", help="One supply tag"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="CSV of loads and totals"),
    pdf: Optional[Path] = typer.Option(None, "--pdf", help="One letter page per supply"),
) -> None:
    """Sum control loads on each supply and compare them with the rating."""
    s = Session.open(project)
    report = s.power_budget(supply=supply, margin=margin)
    if out:
        s.export_power_budget(out, supply=supply, margin=margin)
        typer.echo(f"power budget {out}")
    if pdf:
        s.export_power_budget_pdf(pdf, supply=supply, margin=margin)
        typer.echo(f"power budget {pdf}")
    for row in report["supplies"]:
        typer.echo(
            f"{row['ref']:8} {row['kind']:2}  {row.get('utilization') or row['steady_w']}  "
            f"loads {len(row['loads'])}"
        )
        for load in row["loads"]:
            mark = "estimate" if load["estimate"] else (load.get("catalog") or "catalog")
            inrush = f"{load['inrush_va']} VA" if load.get("inrush_published") else (load.get("inrush_note") or "not published")
            typer.echo(
                f"    {load['ref']:8} {load['kind']:8} {load['steady_w']} W  {inrush}  {mark}"
            )
        if row.get("protection_note"):
            typer.echo(f"    {row['protection_note']}")
        for item in row.get("protection") or []:
            if item.get("message"):
                typer.echo(f"    {item['message']}")
        if row.get("boost_note"):
            typer.echo(f"    {row['boost_note']}")
        for warning in row["warnings"]:
            typer.echo(f"    ! {warning}")
    if report["failed"]:
        raise typer.Exit(code=1)
    if not report["supplies"]:
        typer.echo("no control supply")
    else:
        typer.echo("ok")


@elec.command("checks")
def electrical_checks(
    project: Path,
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    include_suppressed: bool = typer.Option(False, "--include-suppressed"),
) -> None:
    """Live design checks: the same findings as the GUI problems panel."""
    s = Session.open(project)
    rows = s.design_checks(include_suppressed=include_suppressed)
    if out:
        s.export_checks(out, include_suppressed=include_suppressed)
        typer.echo(f"checks {out}")
    for row in rows:
        typer.echo(f"{row['severity']:7} {row['check']:28} {row['message']}")
    errors = [row for row in rows if row["severity"] == "error" and row.get("suppressed") != "yes"]
    if errors:
        raise typer.Exit(code=1)
    if not rows:
        typer.echo("ok")


@elec.command("thermal")
def electrical_thermal(
    project: Path,
    pdf: Optional[Path] = typer.Option(None, "--pdf", help="Write the thermal report PDF"),
    json_out: bool = typer.Option(False, "--json"),
    ambient: Optional[float] = typer.Option(None, "--ambient", help="Ambient °C"),
    tmax: Optional[float] = typer.Option(None, "--tmax", help="Max internal °C"),
    tmin: Optional[float] = typer.Option(None, "--tmin", help="Min internal °C (enables the heater)"),
    save: bool = typer.Option(False, "--save", help="Store ambient, max, and min on the project"),
) -> None:
    """Watt loss, temperature rise, and cooling for the enclosure already chosen."""
    s = Session.open(project)
    overrides: dict = {}
    if ambient is not None:
        overrides["ambient_c"] = ambient
    if tmax is not None:
        overrides["max_internal_c"] = tmax
    if tmin is not None:
        overrides["min_internal_c"] = tmin
    if save and overrides:
        s.set_ambient(**overrides)
        _save(s, project)
        overrides = {}
    if pdf:
        report = s.compute_heat_load(path=pdf, **overrides)
    else:
        report = s.compute_heat_load(**overrides)
    if json_out:
        typer.echo(json_result(report))
        return
    enc = report.get("enclosure") or {}
    rec = report.get("recommendation") or {}
    if enc:
        typer.echo(
            f"{enc.get('width_mm'):g}×{enc.get('height_mm'):g}×{enc.get('depth_mm'):g} mm  "
            f"{enc.get('material_label')}  mount {enc.get('mount')}  NEMA {enc.get('nema') or '—'}  "
            f"A={report.get('surface_m2')} m²"
        )
    else:
        typer.echo("no enclosure")
    typer.echo(
        f"published {report.get('watts_published')} W  estimate {report.get('watts_estimate')} W  "
        f"total {report.get('watts_total')} W  rise {report.get('rise_k')} K  "
        f"internal {report.get('internal_c')} °C  design {report.get('design_c')} °C"
    )
    typer.echo(rec.get("summary") or "")
    for row in report.get("devices") or []:
        margin = row.get("margin_k")
        margin_s = "" if margin is None else f"  {float(margin):+.1f} K"
        typer.echo(
            f"  {row['ref']:8} {row['watts']:8.2f} W  {row['source_label']:10}{margin_s}  "
            f"{row.get('catalog') or row.get('description')}"
        )
    if report.get("estimate_refs"):
        typer.echo("estimates: " + ", ".join(report["estimate_refs"]))
    if pdf:
        typer.echo(f"pdf {pdf}")


@elec.command("io")
def electrical_io(
    project: Path,
    out: Optional[Path] = typer.Option(None, "--out", "-o"),
    src: Optional[Path] = typer.Option(None, "--import", help="CSV: module, address, description, type, wire"),
) -> None:
    s = Session.open(project)
    if src:
        result = s.import_io_list(src)
        _save(s, project)
        typer.echo(f"updated {len(result['updated'])}  errors {len(result['errors'])}")
        for err in result["errors"]:
            typer.echo(f"error: {err}", err=True)
    if out:
        s.export_io_list(out)
        typer.echo(f"io {out}")
    if not src and not out:
        for row in s.io_rows():
            typer.echo(f"{row['module']:8} {row['address']:8} {row['type']:3} {row['wire']:6} {row['description']}")


@elec.command("plc-io")
def electrical_plc_io(
    project: Path,
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="CSV path for the PLC I/O list"),
) -> None:
    """PLC I/O list: address, type, tag, description, connected device, and spares."""
    s = Session.open(project)
    if out:
        s.export_io_list(out)
        typer.echo(f"plc-io {out}")
        return
    for row in s.io_rows():
        typer.echo(
            f"{row['module']:8} {row.get('point') or row['address']:8} {row['type']:3} "
            f"{row.get('tag', ''):16} {row.get('spare', ''):4} "
            f"{row.get('wire', ''):6} {row['description']}"
        )


@elec.command("plc-tags")
def electrical_plc_tags(
    project: Path,
    out: Path = typer.Option(..., "--out", "-o"),
    target: str = typer.Option(
        ...,
        "--target",
        help="studio5000, tia (.xlsx), tia_csv, domore, keyence, sysmac, or cxprogrammer",
    ),
    prefix: str = typer.Option("", "--prefix"),
    name_from: str = typer.Option("device", "--name-from", help="device or description"),
    logix_style: str = typer.Option("alias", "--logix-style", help="alias or base"),
    logix_slot: int = typer.Option(1, "--logix-slot"),
    analog_start: Optional[str] = typer.Option(
        None,
        "--analog-start",
        help="TIA analog byte address, or auto. Omit to use the project analog_start property.",
    ),
) -> None:
    """Export PLC tags. tia is the Excel tag table. keyence and sysmac are labeled generic CSVs."""
    s = Session.open(project)
    kwargs: dict[str, Any] = {
        "target": target,
        "prefix": prefix,
        "name_from": name_from,
        "logix_style": logix_style,
        "logix_slot": logix_slot,
    }
    if analog_start is not None:
        kwargs["analog_start"] = analog_start
    try:
        result = s.export_plc_tags(out, **kwargs)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{result['target']} {result['format']} {result['path']}  {result['count']} points")
    if result.get("note"):
        typer.echo(result["note"])


@elec.command("cables")
def electrical_cables(
    project: Path,
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Cable schedule CSV, or PDF if the path ends in .pdf"),
    pdf: Optional[Path] = typer.Option(None, "--pdf", help="Cable schedule PDF (schedule page and terminations)"),
    pull: Optional[Path] = typer.Option(None, "--pull", help="Pull list CSV: total length per cable type"),
    terminations: Optional[Path] = typer.Option(None, "--terminations", help="Conductor-to-terminal list CSV"),
    propose: bool = typer.Option(False, "--propose", help="Group field wires into cables before export"),
    spares: Optional[int] = typer.Option(None, "--spares", help="Extra conductors when proposing. Default depends on the cable"),
    replace: bool = typer.Option(False, "--replace", help="Replace cables already in the project"),
) -> None:
    """Field cable schedule for everything leaving the panel."""

    s = Session.open(project)
    if propose:
        result = s.propose_cables(spares=spares, replace=replace)
        _save(s, project)
        typer.echo(f"proposed {result['count']}")
    if out:
        s.export_cable_schedule(out)
        typer.echo(f"cables {out}")
    if pdf:
        dest = s.export_cable_schedule_pdf(pdf)
        typer.echo(f"pdf {dest}")
    if pull:
        s.export_cable_pull_list(pull)
        typer.echo(f"pull {pull}")
    if terminations:
        s.export_cable_terminations(terminations)
        typer.echo(f"terminations {terminations}")
    if not any([out, pdf, pull, terminations]):
        for row in s.cable_rows():
            tag = row.get("tag") or row.get("cable") or ""
            kind = row.get("cable_type") or ""
            dest_dev = row.get("to_device") or row.get("to") or ""
            typer.echo(f"{tag:8} {dest_dev:12} {kind}")
        if not s.cable_rows() and not s.project.cables:
            typer.echo("no cables")


@elec.command("phase-colors")
def electrical_phase_colors(
    project: Path,
    voltage: Optional[float] = typer.Option(
        None,
        "--voltage",
        help="System voltage. 208/240 V US shop convention is black/red/blue; 480 V is brown/orange/yellow.",
    ),
    high_from: Optional[float] = typer.Option(
        None, "--high-from", help="Use the 480 V NEMA palette at or above this voltage. Default 300."
    ),
    enabled: Optional[bool] = typer.Option(None, "--enabled/--disabled"),
    nema240: Optional[str] = typer.Option(None, "--nema240", help="L1,L2,L3 for NEMA 208/240 V"),
    nema480: Optional[str] = typer.Option(None, "--nema480", help="L1,L2,L3 for NEMA 480 V"),
    iec_colors: Optional[str] = typer.Option(
        None,
        "--iec",
        help="L1,L2,L3. Brown, black, and grey are a permitted IEC default, not a required order.",
    ),
    neutral: Optional[str] = typer.Option(None, "--neutral", help="NEMA neutral. Default white/grey."),
    iec_neutral: Optional[str] = typer.Option(
        None,
        "--iec-neutral",
        help=(
            "IEC neutral. Blue (default light blue #5eb0e8). "
            "IEC 60445 §6.2.2 is unverified from an official source."
        ),
    ),
    ground: Optional[str] = typer.Option(None, "--ground"),
) -> None:
    """Set project phase colors for 3-phase power sheets."""

    s = Session.open(project)
    fields: dict[str, object] = {}
    if voltage is not None:
        fields["system_voltage"] = voltage
    if high_from is not None:
        fields["nema_high_voltage"] = high_from
    if enabled is not None:
        fields["enabled"] = enabled
    if nema240 is not None:
        fields["nema_low"] = nema240
    if nema480 is not None:
        fields["nema_high"] = nema480
    if iec_colors is not None:
        fields["iec"] = iec_colors
    if neutral is not None:
        fields["neutral"] = neutral
    if iec_neutral is not None:
        fields["iec_neutral"] = iec_neutral
    if ground is not None:
        fields["ground"] = ground
    if not fields:
        payload = s.phase_colors()
    else:
        payload = s.set_phase_colors(**fields)
        s.save()
    typer.echo(
        "phase-colors "
        f"enabled={payload['enabled']} voltage={payload['system_voltage']} "
        f"high_from={payload['nema_high_voltage']}"
    )
    for warning in payload.get("warnings") or []:
        typer.echo(f"warning: {warning}")


@elec.command("circuit")
def electrical_circuit(
    project: Path,
    kind: str = typer.Option(
        "dol", "--kind", help="dol, vfd, reversing, star_delta, soft_starter, or plc_motor"
    ),
    drive: Optional[str] = typer.Option(
        None, "--drive", help="generic_1k5, gs20_0p5, or es1_0p75"
    ),
    branch: str = typer.Option("breaker", "--branch", help="breaker or disconnect"),
    line_contactor: bool = typer.Option(False, "--line-contactor", help="VFD only: contactor ahead of the drive"),
    control: str = typer.Option("none", "--control", help="none or local"),
    kw: Optional[float] = typer.Option(None, "--kw", help="Motor kW. Default is 1.5 kW for DOL, or the drive rating"),
    hp: Optional[float] = typer.Option(None, "--hp", help="Motor horsepower"),
    voltage: Optional[float] = typer.Option(None, "--voltage", help="Line voltage. Default 480 NEMA / 400 IEC"),
    control_voltage: Optional[float] = typer.Option(None, "--control-voltage", help="Control voltage. 24 uses the PSU; 120 uses the CPT"),
    fla: Optional[float] = typer.Option(None, "--fla", help="Nameplate FLA. Default is NEC Table 430.250"),
    pilot_lights: bool = typer.Option(False, "--pilot-lights", help="Add run / direction / fault pilots"),
    bypass: bool = typer.Option(False, "--bypass", help="Soft starter: bypass contactor"),
    control_wiring: str = typer.Option("3wire", "--control-wiring", help="soft starter: 2wire or 3wire"),
    starter: str = typer.Option("dol", "--starter", help="plc_motor: dol or vfd"),
    timer_seconds: Optional[float] = typer.Option(
        None, "--timer-seconds", help="star_delta: on-delay seconds (default 6)"
    ),
    overload_placement: str = typer.Option(
        "windings", "--overload-placement", help="star_delta: windings (0.58×FLA) or line (1.0×FLA)"
    ),
    hoa: bool = typer.Option(True, "--hoa/--no-hoa", help="plc_motor: Hand-Off-Auto selector"),
    plc_model: Optional[str] = typer.Option(
        None, "--plc-model", help="plc_motor: nema.plc.compact_16dio or iec.plc.compact_16dio"
    ),
    io_run: Optional[str] = typer.Option(None, "--io-run", help="plc_motor: run-feedback input (default I0_0)"),
    io_ol: Optional[str] = typer.Option(None, "--io-ol", help="plc_motor: OL-trip input (default I0_1)"),
    io_auto: Optional[str] = typer.Option(None, "--io-auto", help="plc_motor: HOA Auto input (default I0_2)"),
    io_out: Optional[str] = typer.Option(None, "--io-out", help="plc_motor: CR output (default Q0_0)"),
    sheet: Optional[str] = typer.Option(None, "--sheet", help="Sheet to insert on"),
    new_sheet: bool = typer.Option(False, "--new-sheet", help="Create a Motor power schematic"),
    x: float = typer.Option(40.0, "--x"),
    y: float = typer.Option(22.0, "--y"),
    manufacturer: Optional[str] = typer.Option(
        None, "--manufacturer", "-m", help="Preferred manufacturer. Assigns that vendor's catalog SKUs."
    ),
    secondary_manufacturer: Optional[str] = typer.Option(
        None,
        "--secondary-manufacturer",
        help="Fill categories the preferred brand does not make. Omit for the project vendor, else AutomationDirect. none disables it.",
    ),
    phases: Optional[float] = typer.Option(
        None,
        "--phases",
        help="3-phase only. 1 is refused: single-phase circuits are not drawn yet. 3 and 3.0 are accepted. A non-integer such as 3.6 is refused.",
    ),
    place_layout: bool = typer.Option(
        True,
        "--place-layout/--no-place-layout",
        help="Place in-cabinet devices on the panel and operators on the door",
    ),
    shared_supply: bool = typer.Option(
        True,
        "--shared-supply/--no-shared-supply",
        help="Reuse the panel control supply. --no-shared-supply adds one for this circuit",
    ),
    shared_estop: bool = typer.Option(
        True,
        "--shared-estop/--no-shared-estop",
        help="One E-stop for the panel. --no-shared-estop adds one for this motor",
    ),
    separate_control_rail: bool = typer.Option(
        False,
        "--separate-control-rail/--no-separate-control-rail",
        help="Put timer, relays, PSU, and PLC on their own rail. Default is the power rail, control on the right",
    ),
    standard: Optional[str] = typer.Option(
        None, "--standard", help="IEC or NEMA/ANSI. Omit to follow the project."
    ),
) -> None:
    """Insert a motor circuit. New panel devices go in free space; operators go on the door."""

    s = Session.open(project)
    try:
        result = s.insert_circuit(
            kind=kind,
            drive=drive,
            branch=branch,
            line_contactor=line_contactor,
            control=control,
            kw=kw,
            hp=hp,
            sheet_id=sheet,
            new_sheet=new_sheet,
            x=x,
            y=y,
            voltage=voltage,
            control_voltage=control_voltage,
            fla=fla,
            pilot_lights=pilot_lights,
            bypass=bypass,
            control_wiring=control_wiring,
            starter=starter,
            timer_seconds=timer_seconds,
            overload_placement=overload_placement,
            hoa=hoa,
            plc_model=plc_model,
            io_run=io_run,
            io_ol=io_ol,
            io_auto=io_auto,
            io_out=io_out,
            manufacturer=manufacturer,
            secondary_manufacturer=secondary_manufacturer,
            phases=phases,
            place_layout=place_layout,
            shared_supply=shared_supply,
            shared_estop=shared_estop,
            separate_control_rail=separate_control_rail,
            standard=standard,
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _save(s, project)
    layout = result.get("layout") or {}
    panel = layout.get("sheet")
    extra = f", layout {panel}" if panel else ""
    door = layout.get("door")
    if door:
        extra += f", door {door}"
    warns = layout.get("warnings") or []
    if warns:
        extra += f"  warning: {'; '.join(str(w) for w in warns)}"
    preferred = result.get("preferred") or []
    skus = ", ".join(
        f"{row.get('role')} {row.get('catalog')}" for row in preferred if row.get("catalog")
    )
    if skus:
        extra += f"  catalog: {skus}"
    typer.echo(
        f"{result['kind']} {result.get('standard') or 'NEMA/ANSI'} on {result['sheet']}{extra}  "
        f"{len(result['components'])} devices, {len(result['wires'])} wires"
    )


@elec.command("sccr")
def electrical_sccr(
    project: Path,
    pdf: Optional[Path] = typer.Option(None, "--pdf", help="Write the SCCR report page"),
    png: Optional[Path] = typer.Option(None, "--png", help="Write the SCCR report page as PNG"),
    available_ka: Optional[float] = typer.Option(None, "--available-ka", help="Available fault current, kA"),
    voltage: Optional[float] = typer.Option(None, "--voltage", help="Panel SCCR voltage"),
    ref: Optional[str] = typer.Option(None, "--ref", help="Device tag whose SCCR to set"),
    sccr_ka: Optional[float] = typer.Option(None, "--sccr-ka", help="SCCR in kA for --ref"),
) -> None:
    """UL 508A Supplement SB short-circuit current rating."""

    s = Session.open(project)
    changed = False
    if ref and sccr_ka is not None:
        s.set_part_sccr(ref, sccr_ka=sccr_ka)
        changed = True
    if available_ka is not None or voltage is not None:
        s.set_available_fault(
            available_ka,
            sccr_voltage=voltage,
        )
        changed = True
    result = s.compute_sccr()
    if pdf is not None:
        s.export_sccr_pdf(pdf)
        typer.echo(f"pdf {pdf}")
    if png is not None:
        s.export_sccr_png(png)
        typer.echo(f"png {png}")
    if changed:
        _save(s, project)
    limit = ", ".join(result.get("limiting_refs") or []) or "none"
    typer.echo(f"SCCR {result.get('panel_text')}  limited by {limit}")
    if result.get("below_available"):
        typer.echo(
            f"warning: below available fault {result.get('available_fault_ka')} kA",
            err=True,
        )


@elec.command("size-motor")
def electrical_size_motor(
    hp: Optional[float] = typer.Option(None, "--hp", help="Horsepower. Must be a published NEC row."),
    kw: Optional[float] = typer.Option(None, "--kw", help="kW. NEC projects convert to hp."),
    volts: Optional[float] = typer.Option(None, "--volts", help="System voltage. 480 V uses the 460 V column."),
    phases: int = typer.Option(3, "--phases", help="1 or 3"),
    fla: Optional[float] = typer.Option(None, "--fla", help="Nameplate current for the overload setting."),
    standard: str = typer.Option("NEMA/ANSI", "--standard", help="NEMA/ANSI or IEC"),
    device: str = typer.Option(
        "inverse_time_breaker",
        "--device",
        help="inverse_time_breaker, dual_element_fuse, nontime_delay_fuse, or instantaneous_trip",
    ),
    wire_table: str = typer.Option(
        "auto",
        "--wire-table",
        help="auto (UL 508A 29.6, not verified against the standard), marked-75, or marked-60. Both ampacity tables are always checked.",
    ),
    temp_c: Optional[int] = typer.Option(
        None,
        "--temp-c",
        help="60 or 75 when terminations are marked. Omit to use UL 508A 29.6 (not verified against the standard).",
    ),
) -> None:
    """Size one motor branch from NEC tables, or record an IEC nameplate current."""

    from panelcad.core.motor_sizing import size_motor_circuit

    result = size_motor_circuit(
        hp=hp,
        kw=kw,
        volts=volts,
        phases=phases,
        fla=fla,
        standard=standard,
        device=device,
        wire_table=wire_table,
        temp_c=temp_c,
    )
    typer.echo(str(result.get("report") or result.get("error") or ""))
    if not result.get("ok"):
        raise typer.Exit(code=1)


@app.command("nameplate")
def nameplate_cmd(
    project: Path,
    pdf: Optional[Path] = typer.Option(None, "--pdf", help="Real-size nameplate PDF"),
    dxf: Optional[Path] = typer.Option(None, "--dxf", help="Engraving DXF (TEXT, CUT, HOLE)"),
    png: Optional[Path] = typer.Option(None, "--png", help="Preview PNG of the plate"),
    set_field: Optional[list[str]] = typer.Option(None, "--set", help="Override key=value. Repeat for more."),
    size: Optional[str] = typer.Option(None, "--size", help="3x5, or a custom width x height in inches"),
) -> None:
    """Build the industrial control panel nameplate from the drawing."""

    from panelcad.electrical.nameplate import parse_size_token

    s = Session.open(project)
    updates: dict[str, object] = {}
    if size:
        preset, width_in, height_in = parse_size_token(size)
        updates["preset"] = preset
        if width_in is not None:
            updates["width_in"] = width_in
            updates["height_in"] = height_in
    for item in set_field or []:
        if "=" not in item:
            raise typer.BadParameter(f"--set expects key=value, got {item}")
        key, value = item.split("=", 1)
        updates[key.strip()] = value
    if updates:
        s.nameplate_set(**updates)
        _save(s, project)
    plate = s.nameplate_build()
    if pdf or dxf or png:
        exported = s.nameplate_export(pdf=pdf, dxf=dxf, png=png)
        for key in ("pdf", "dxf", "caution_dxf", "png"):
            if exported.get(key):
                typer.echo(f"{key} {exported[key]}")
    for field in plate["fields"]:
        if field.get("hidden"):
            continue
        mark = "  overridden" if field.get("overridden") else ""
        typer.echo(f"{field['label']}: {field.get('value') or '—'}{mark}")
    for line in plate.get("working") or []:
        if "Overridden:" in str(line):
            typer.echo(line)
    for issue in plate.get("issues") or []:
        typer.echo(f"warning: {issue['message']}", err=True)


@elec.command("items")
def electrical_items(project: Path) -> None:
    """Store BOM item numbers on each device for panel balloons."""
    s = Session.open(project)
    count = s.assign_item_numbers()
    _save(s, project)
    typer.echo(f"items {count}")


rev_app = typer.Typer(help="Issue, list, and compare customer drawing revisions")
app.add_typer(rev_app, name="rev")


@rev_app.command("issue")
def rev_issue(
    project: Path,
    description: str = typer.Option("", "--description", "-d"),
    rev: Optional[str] = typer.Option(
        None, "--rev", help="Use this rev instead of the next letter or number"
    ),
    scheme: Optional[str] = typer.Option(
        None, "--scheme", help="letter (A, B, C) or number (0, 1, 2)"
    ),
    date: Optional[str] = typer.Option(None, "--date", help="YYYY-MM-DD. Default is today"),
    drawn: str = typer.Option("", "--drawn"),
    checked: str = typer.Option("", "--checked"),
    approved: str = typer.Option("", "--approved"),
    clear_clouds: Optional[bool] = typer.Option(
        None,
        "--clear-clouds/--keep-clouds",
        help="Clear clouds from earlier revisions when this one is issued",
    ),
    clouds: bool = typer.Option(True, "--clouds/--no-clouds", help="Draw clouds around changes"),
) -> None:
    """Snapshot the project, bump the rev, and fill the title-block revision table."""

    s = Session.open(project)
    try:
        result = s.issue_revision(
            description=description,
            rev=rev,
            scheme=scheme,
            date=date,
            drawn=drawn,
            checked=checked,
            approved=approved,
            clear_clouds=clear_clouds,
            auto_clouds=clouds,
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    s.save(project)
    typer.echo(
        f"issued {result['rev']}  {result['date']}  {result['description'] or '-'}  "
        f"clouds={result['clouds']}"
    )


@rev_app.command("list")
def rev_list(
    project: Path,
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List issued revisions. The current rev is on every sheet title block."""

    import json as _json

    s = Session.open(project)
    info = s.list_revisions()
    if json_out:
        typer.echo(_json.dumps(info, indent=2))
        return
    typer.echo(f"scheme {info['scheme']}  current {info['current'] or '-'}")
    if not info["revisions"]:
        typer.echo("no revisions issued")
        return
    for row in info["revisions"]:
        line = (
            f"{row['rev']:6}  {row['date'] or '-':12}  "
            f"{row['drawn'] or '-':4}  {row['description']}"
        )
        typer.echo(line)
    if info["clouds"]:
        typer.echo(f"clouds {len(info['clouds'])}")


@rev_app.command("compare")
def rev_compare(
    project: Path,
    left: str = typer.Argument(..., help="Revision, or 'working'"),
    right: str = typer.Argument(..., help="Revision, or 'working'"),
    csv: Optional[Path] = typer.Option(None, "--csv", help="Write the diff as CSV"),
    pdf: Optional[Path] = typer.Option(None, "--pdf", help="Write the compare report PDF"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Diff devices, wires, terminals, BOM, and layout moves between two revisions."""

    import json as _json

    s = Session.open(project)
    try:
        report = s.compare_revisions(left, right, csv_path=csv, pdf_path=pdf)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    counts = report["counts"]
    devices = (
        f"+{counts['devices_added']} -{counts['devices_removed']} ~{counts['devices_changed']}"
    )
    wires = (
        f"+{counts['wires_added']} -{counts['wires_removed']} ~{counts['wires_renumbered']}"
    )
    terminals = (
        f"+{counts['terminals_added']} -{counts['terminals_removed']} "
        f"~{counts['terminals_changed']}"
    )
    typer.echo(
        f"compare {report['left']} -> {report['right']}  "
        f"devices {devices}  wires {wires}  terminals {terminals}  "
        f"bom {counts['bom']}  layout {counts['layout']}"
    )
    if csv:
        typer.echo(f"csv {report.get('csv')}")
    if pdf:
        typer.echo(f"pdf {report.get('pdf')}")
    if json_out:
        public = {k: v for k, v in report.items() if k != "areas"}
        typer.echo(_json.dumps(public, indent=2, default=str))


update_app = typer.Typer(help="Check for a PanelCAD release and install it when you confirm")
app.add_typer(update_app, name="update")


def _configure_stdio() -> None:
    """A cp1252 pipe must not crash on a status line. Replace what it cannot encode."""

    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def _update_config():
    import os
    from pathlib import Path

    raw = os.environ.get("PANELCAD_CONFIG_DIR", "").strip()
    return Path(raw) if raw else None


@update_app.command("check")
def update_check(
    feed: Optional[str] = typer.Option(None, "--feed", help="Feed URL or path for this call"),
    json_out: bool = typer.Option(False, "--json", help="Print the check result as JSON"),
) -> None:
    """Read the release feed. Does not download or install."""

    from panelcad.update.manifest import UpdateError
    from panelcad.update.service import check_for_update

    _configure_stdio()

    try:
        report = check_for_update(feed=feed, config_dir=_update_config())
    except UpdateError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if json_out:
        typer.echo(json_result(report))
        return
    if not report.get("enabled"):
        typer.echo("Updates are off. Set a feed in Help > Updates or PANELCAD_UPDATE_FEED.")
        return
    typer.echo(f"PanelCAD {report.get('current')}")
    typer.echo(f"Feed: {report.get('feed')}")
    typer.echo(str(report.get("reason") or ""))
    notes = str(report.get("notes") or "").strip()
    if report.get("update_available") and notes:
        typer.echo(notes)


@update_app.command("install")
def update_install_cmd(
    confirm: bool = typer.Option(False, "--confirm", help="Install the update. Required."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Verify sha256 and print the handoff. Do not run it."
    ),
    feed: Optional[str] = typer.Option(None, "--feed", help="Feed URL or path for this call"),
    expect_version: Optional[str] = typer.Option(
        None, "--expect-version", help="Install only when the feed still has this version"
    ),
    expect_sha256: Optional[str] = typer.Option(
        None, "--expect-sha256", help="Install only when the feed still has this sha256"
    ),
) -> None:
    """Download the installer, verify sha256, and start the Windows upgrade."""

    from panelcad.update.manifest import UpdateError
    from panelcad.update.service import install_update

    _configure_stdio()
    try:
        result = install_update(
            confirm=confirm,
            dry_run=dry_run,
            feed=feed,
            config_dir=_update_config(),
            expected_version=expect_version,
            expected_sha256=expect_sha256,
        )
    except UpdateError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json_result(result))
    if result.get("close_app"):
        raise typer.Exit(code=0)


@update_app.command("rollback")
def update_rollback_cmd(
    confirm: bool = typer.Option(
        False, "--confirm", help="Restore the previous program folder. Required."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the rollback plan. Do not run it."
    ),
) -> None:
    """Restore src, scripts, and packaging files. Drawings in the program folder stay."""

    from panelcad.update.manifest import UpdateError
    from panelcad.update.service import rollback_update

    _configure_stdio()
    try:
        result = rollback_update(confirm=confirm, dry_run=dry_run, config_dir=_update_config())
    except UpdateError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json_result(result))
    if result.get("close_app"):
        raise typer.Exit(code=0)


release_app = typer.Typer(help="Publish a PanelCAD release feed")
app.add_typer(release_app, name="release")


@release_app.command("publish-feed")
def release_publish_feed(
    zip_path: Path = typer.Option(..., "--zip", help="Installer zip from scripts/build-release.py"),
    feed: str = typer.Option(
        ..., "--feed", help="Manifest file, or a folder that will hold panelcad-feed.json"
    ),
    notes: str = typer.Option("", "--notes", help="Release notes stored in the manifest"),
    notes_file: Optional[Path] = typer.Option(
        None, "--notes-file", help="Read release notes from this file"
    ),
    release_date: Optional[str] = typer.Option(
        None, "--date", help="Release date YYYY-MM-DD (default: today, UTC)"
    ),
    min_version: Optional[str] = typer.Option(
        None,
        "--min-version",
        help="Oldest installed version that can apply this update",
    ),
    download: Optional[str] = typer.Option(
        None,
        "--download",
        help=(
            "Download location written into the manifest "
            "(default: the zip file name next to the feed)"
        ),
    ),
    download_url: Optional[str] = typer.Option(
        None,
        "--download-url",
        help=(
            "https URL written as the manifest download. "
            "sha256 and size still come from --zip. "
            "A Google Drive direct link is "
            "https://drive.google.com/uc?export=download&id=<id>."
        ),
    ),
    version: Optional[str] = typer.Option(
        None, "--version", help="Override the version read from the zip name"
    ),
    no_copy: bool = typer.Option(False, "--no-copy", help="Do not copy the zip into a folder feed"),
) -> None:
    """Compute sha256 and write or update the release feed manifest."""

    from panelcad.update.feed import require_remote_download_url
    from panelcad.update.manifest import UpdateError
    from panelcad.update.publish import publish_feed

    _configure_stdio()
    body = notes
    if notes_file is not None:
        body = notes_file.read_text(encoding="utf-8")
    named = (download or "").strip()
    explicit = (download_url or "").strip()
    if explicit and named and explicit != named:
        typer.echo("--download and --download-url disagree. Pass one of them.", err=True)
        raise typer.Exit(code=1)
    chosen = named or None
    if explicit:
        try:
            chosen = require_remote_download_url(explicit)
        except UpdateError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
    try:
        path = publish_feed(
            zip_path=zip_path,
            feed=feed,
            notes=body,
            release_date=release_date,
            min_version=min_version,
            download=chosen,
            version=version,
            copy_zip=not no_copy,
        )
    except UpdateError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"feed {path}")


quote_app = typer.Typer(
    no_args_is_help=True,
    help="Quote from the drawing. Writes files only; nothing is sent.",
)
app.add_typer(quote_app, name="quote")


def _quote_summary(quote: dict) -> str:
    totals = quote["totals"]
    lines = [
        f"quote {quote.get('number') or '(no number)'}",
        f"customer {quote.get('customer') or ''}",
        f"project {quote.get('project') or ''}",
        "  ".join(
            bit
            for bit in (
                f"parts sell {totals['parts_sell']:.2f}",
                f"unpriced {quote['unpriced']}",
                str(quote.get("needs_info_label") or ""),
                f"stale {quote['stale']}",
            )
            if bit
        ),
        f"labor {totals['labor_hours']:.3f} h  sell {totals['labor_sell']:.2f}",
        f"{quote.get('total_label') or 'Total'} {totals['total']:.2f}",
        "files only — nothing sent",
        quote.get("label") or "",
    ]
    return "\n".join(line for line in lines if line)


def _quote_warn(result: dict, *, customer_defaults: bool = False) -> None:
    if result.get("incomplete"):
        label = result.get("total_label") or (
            f"INCOMPLETE – {result.get('unpriced')} of {result.get('lines')} lines unpriced, not included"
        )
        typer.echo(f"warning: {label}")
    if customer_defaults and result.get("example_defaults"):
        typer.echo("warning: example defaults, edit these")


@quote_app.command("build")
def quote_build_cmd(
    project: Path,
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write the quote JSON here"),
    today: Optional[str] = typer.Option(None, "--today", help="Price-age date YYYY-MM-DD"),
) -> None:
    """Add up parts and labor for this drawing. Does not send the quote."""

    session = Session.open(project)
    quote = session.quote_build(today=today)
    if out is not None:
        result = session.quote_export(out, fmt="json", today=today)
        typer.echo(f"quote {result['path']}")
    typer.echo(_quote_summary(quote))
    _quote_warn(quote)


@quote_app.command("export")
def quote_export_cmd(
    project: Path,
    out: Path = typer.Option(..., "--out", "-o", help="xlsx, pdf, csv, or json path"),
    view: str = typer.Option("internal", "--view", help="internal or customer"),
    format: Optional[str] = typer.Option(None, "--format", help="xlsx, pdf, csv, or json"),
    today: Optional[str] = typer.Option(None, "--today", help="Price-age date YYYY-MM-DD"),
) -> None:
    """Write a quote file. Customer view omits cost and markup columns."""

    session = Session.open(project)
    result = session.quote_export(out, fmt=format, view=view, today=today)
    typer.echo(
        f"{result['format']} {result['view']} {result['path']}  sent={result['sent']}  "
        f"incomplete={result['incomplete']}  unpriced={result['unpriced']}"
    )
    _quote_warn(result, customer_defaults=result.get("view") == "customer")


@quote_app.command("import-prices")
def quote_import_prices_cmd(
    project: Path,
    prices: Path = typer.Argument(..., help="CSV or XLSX price list already on this computer"),
    price_mode: Optional[str] = typer.Option(None, "--price-mode", help="cheapest or preferred"),
    preferred_vendor: Optional[str] = typer.Option(None, "--preferred-vendor"),
) -> None:
    """Replace this project's price list. Nothing is downloaded."""

    session = Session.open(project)
    try:
        result = session.quote_import_prices(
            prices,
            price_mode=price_mode,
            preferred_vendor=preferred_vendor,
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _save(session, project)
    skipped = result.get("skipped") or []
    typer.echo(
        f"imported {result['rows']}  encoding {result.get('encoding') or ''}  "
        f"vendors {', '.join(result['vendors'])}  "
        f"skipped {len(skipped)}  sent={result['sent']}"
    )
    for line in skipped[:12]:
        typer.echo(f"skipped: {line}")


@quote_app.command("set")
def quote_set_cmd(
    project: Path,
    number: Optional[str] = typer.Option(None, "--number"),
    customer: Optional[str] = typer.Option(None, "--customer"),
    project_name: Optional[str] = typer.Option(None, "--project-name", help="Quote project name"),
    currency: Optional[str] = typer.Option(None, "--currency"),
    price_mode: Optional[str] = typer.Option(None, "--price-mode", help="cheapest or preferred"),
    preferred_vendor: Optional[str] = typer.Option(None, "--preferred-vendor"),
    parts_markup: Optional[str] = typer.Option(None, "--parts-markup"),
    labor_markup: Optional[str] = typer.Option(None, "--labor-markup"),
    tax: Optional[str] = typer.Option(None, "--tax"),
    freight: Optional[str] = typer.Option(None, "--freight"),
    contingency: Optional[str] = typer.Option(None, "--contingency"),
    category: Optional[list[str]] = typer.Option(
        None,
        "--category",
        help="Category markup as category=percent. Repeat for more than one.",
    ),
) -> None:
    """Save quote header fields on the project. Nothing is sent."""

    fields: dict = {}
    if number is not None:
        fields["number"] = number
    if customer is not None:
        fields["customer"] = customer
    if project_name is not None:
        fields["project"] = project_name
    if currency is not None:
        fields["currency"] = currency
    if price_mode is not None:
        fields["price_mode"] = price_mode
    if preferred_vendor is not None:
        fields["preferred_vendor"] = preferred_vendor
    if parts_markup is not None:
        fields["parts_markup_percent"] = parts_markup
    if labor_markup is not None:
        fields["labor_markup_percent"] = labor_markup
    if tax is not None:
        fields["tax_percent"] = tax
    if freight is not None:
        fields["freight"] = freight
    if contingency is not None:
        fields["contingency_percent"] = contingency
    if category:
        mapping: dict[str, str] = {}
        for item in category:
            if "=" not in item:
                typer.echo("category must be name=percent", err=True)
                raise typer.Exit(code=1)
            name, percent = item.split("=", 1)
            mapping[name.strip()] = percent.strip()
        fields["category_markups"] = mapping
    session = Session.open(project)
    try:
        session.quote_set_rates(**fields)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _save(session, project)
    typer.echo("saved  sent=False")
cutouts_app = typer.Typer(help="To-scale cutout and drill drawings")
app.add_typer(cutouts_app, name="cutouts")


def _cutout_cli_options(
    datum: Optional[str],
    style: Optional[str],
    view: Optional[str],
    units: Optional[str],
    edge: Optional[float],
    flange: Optional[float],
    thickness: Optional[float],
    surfaces: Optional[str],
) -> dict:
    options: dict = {}
    if datum:
        options["datum"] = datum
    if style:
        options["style"] = style
    if view:
        options["view"] = view
    if units:
        options["units"] = units
    if edge is not None:
        options["edge_clearance_mm"] = edge
    if flange is not None:
        options["flange_mm"] = flange
    if thickness is not None:
        options["panel_thickness_mm"] = thickness
    if surfaces:
        options["surfaces"] = [part.strip() for part in surfaces.split(",") if part.strip()]
    return options


@cutouts_app.command("generate")
def cutouts_generate_cmd(
    project: Path,
    pdf: Optional[Path] = typer.Option(None, "--pdf", help="Also write the cutout PDF"),
    datum: Optional[str] = typer.Option(None, "--datum", help="top_left, top_right, bottom_left, bottom_right"),
    style: Optional[str] = typer.Option(None, "--style", help="ordinate or baseline"),
    view: Optional[str] = typer.Option(None, "--view", help="front or back"),
    units: Optional[str] = typer.Option(None, "--units", help="mm or inch"),
    persist: bool = typer.Option(False, "--persist", help="Store these settings on the project"),
    surfaces: Optional[str] = typer.Option(None, "--surfaces", help="Comma list: door,backplate,left,right,top,bottom"),
) -> None:
    """List cutout sheets for the door, backplate, and walls."""

    options = _cutout_cli_options(datum, style, view, units, None, None, None, surfaces)
    session = Session.open(project)
    result = session.cutouts_generate(options, persist=persist)
    if pdf is not None:
        session.cutouts_export_pdf(pdf, options if not persist else {"surfaces": options.get("surfaces")} if options.get("surfaces") else None)
        typer.echo(f"pdf {pdf}")
    if persist:
        session.save(project)
    typer.echo(f"cutouts {result['count']}  {', '.join(result['surfaces'])}")


@cutouts_app.command("dxf")
def cutouts_dxf_cmd(
    project: Path,
    out: Path = typer.Option(..., "--out", "-o", help="DXF file or folder"),
    datum: Optional[str] = typer.Option(None, "--datum"),
    style: Optional[str] = typer.Option(None, "--style"),
    view: Optional[str] = typer.Option(None, "--view"),
    units: Optional[str] = typer.Option(None, "--units", help="mm or inch"),
    surfaces: Optional[str] = typer.Option(None, "--surfaces"),
) -> None:
    """Export 1:1 shop DXF (layers CUT, DRILL, DIM, TEXT, OUTLINE)."""

    options = _cutout_cli_options(datum, style, view, units, None, None, None, surfaces)
    session = Session.open(project)
    result = session.cutouts_export_dxf(out, options or None)
    for path in result["paths"]:
        typer.echo(f"dxf {path}")
    skipped = result.get("skipped") or []
    if skipped:
        typer.echo("skipped " + ", ".join(skipped))


@cutouts_app.command("set")
def cutouts_set_cmd(
    project: Path,
    ref: str,
    cutout: Optional[str] = typer.Option(None, "--cutout", help="JSON object: shape, size, holes, depth"),
    unknown: Optional[bool] = typer.Option(
        None,
        "--unknown/--no-unknown",
        help="Mark the cutout unknown, or restore the opening from before it was marked unknown",
    ),
    clear: bool = typer.Option(False, "--clear", help="Remove the override and use the library record"),
    surface: Optional[str] = typer.Option(None, "--surface"),
    include_key: Optional[bool] = typer.Option(None, "--include-key/--no-include-key"),
    min_center_x: Optional[float] = typer.Option(None, "--min-center-x", help="Minimum horizontal centre distance, mm"),
    min_center_y: Optional[float] = typer.Option(None, "--min-center-y", help="Minimum vertical centre distance, mm"),
    pitch_swap: Optional[bool] = typer.Option(
        None,
        "--pitch-swap/--no-pitch-swap",
        help="Row and column pitches may be swapped",
    ),
    centers_interchangeable: Optional[bool] = typer.Option(
        None,
        "--centers-interchangeable/--no-centers-interchangeable",
        hidden=True,
        help="Same as --pitch-swap. Kept so an 80-column help line is not clipped.",
    ),
) -> None:
    """Write a cutout onto one device. Does not invent a manufacturer hole."""

    import json

    from panelcad.core.cutout import resolve_centers_interchangeable

    payload = json.loads(cutout) if cutout else None
    if payload is not None and not isinstance(payload, dict):
        raise typer.BadParameter("--cutout must be a JSON object")
    try:
        interchangeable = resolve_centers_interchangeable(pitch_swap, centers_interchangeable)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    session = Session.open(project)
    result = session.part_set_cutout(
        ref,
        payload,
        unknown=unknown,
        clear=clear,
        surface=surface,
        include_key=include_key,
        min_center_x_mm=min_center_x,
        min_center_y_mm=min_center_y,
        centers_interchangeable=interchangeable,
    )
    session.save(project)
    typer.echo(f"cutout {result['id']}  sheets {', '.join(result['sheets'])}")


recon_app = typer.Typer(help="Reconstruct a panel from real photos (prints are a secondary cross-check)")
app.add_typer(recon_app, name="reconstruct")


def _recon_session(project: Path) -> Session:
    return Session.open(project)


@recon_app.command("add")
def reconstruct_add_cmd(
    project: Path,
    photos: list[Path] = typer.Argument(..., help="Phone photos of the physical panel"),
    role: str = typer.Option("other", "--role", help="backpanel, door_inside, door_outside, nameplate, part_label, terminal_strip, wire_numbers, other"),
    note: str = typer.Option("", "--note"),
) -> None:
    session = _recon_session(project)
    result = session.reconstruct_add_photos(photos, role=role, note=note)
    session.save(project)
    typer.echo(f"photos {result['count']}")


@recon_app.command("tag")
def reconstruct_tag_cmd(
    project: Path,
    photo_id: str,
    role: str = typer.Option(..., "--role"),
    note: str = typer.Option("", "--note"),
) -> None:
    session = _recon_session(project)
    result = session.reconstruct_tag_photo(photo_id, role, note=note or None)
    session.save(project)
    typer.echo(f"{result['id']} {result['role']}")


@recon_app.command("corners")
def reconstruct_corners_cmd(
    project: Path,
    photo_id: str,
    points: str = typer.Option(..., "--points", help="x,y x,y x,y x,y clockwise from NW"),
) -> None:
    session = _recon_session(project)
    corners = []
    for token in points.replace(";", " ").split():
        parts = token.split(",")
        if len(parts) != 2:
            raise typer.BadParameter("each corner is x,y")
        corners.append((float(parts[0]), float(parts[1])))
    result = session.reconstruct_set_corners(photo_id, corners)
    session.save(project)
    typer.echo(f"corners {result['id']} {len(result.get('corners') or [])}")


@recon_app.command("photos")
def reconstruct_photos_cmd(project: Path) -> None:
    import json

    session = _recon_session(project)
    typer.echo(json.dumps(session.reconstruct_list_photos(), indent=2))


@recon_app.command("scale")
def reconstruct_scale_cmd(
    project: Path,
    photo_id: str,
    kind: str = typer.Option(..., "--kind", help="din_rail, catalog_part, wire_duct, measured"),
    p1: str = typer.Option(..., "--p1", help="x,y photo pixels"),
    p2: str = typer.Option(..., "--p2", help="x,y photo pixels"),
    known_mm: Optional[float] = typer.Option(None, "--known-mm"),
    known_height_mm: Optional[float] = typer.Option(None, "--known-height-mm"),
    catalog: str = typer.Option("", "--catalog"),
    manufacturer: str = typer.Option("", "--manufacturer"),
    slot: Optional[str] = typer.Option(None, "--slot", help="1/scale or 2/scale_b"),
    replace: Optional[str] = typer.Option(None, "--replace", help="scale or scale_b"),
) -> None:
    def _xy(text: str) -> tuple[float, float]:
        parts = text.replace(" ", "").split(",")
        if len(parts) != 2:
            raise typer.BadParameter("use x,y")
        return (float(parts[0]), float(parts[1]))

    session = _recon_session(project)
    result = session.reconstruct_set_scale(
        photo_id,
        kind,
        _xy(p1),
        _xy(p2),
        known_mm=known_mm,
        known_height_mm=known_height_mm,
        catalog=catalog,
        manufacturer=manufacturer,
        slot=slot,
        replace=replace,
    )
    session.save(project)
    typer.echo(f"scale {result['photo']} {result['kind']} mm_per_px={result.get('mm_per_px')}")


@recon_app.command("clear-scale")
def reconstruct_clear_scale_cmd(project: Path, photo_id: str) -> None:
    session = _recon_session(project)
    result = session.reconstruct_clear_scale(photo_id)
    session.save(project)
    typer.echo(f"cleared {result.get('photo') or photo_id}")


@recon_app.command("identify")
def reconstruct_identify_cmd(project: Path, photo_id: Optional[str] = typer.Option(None, "--photo")) -> None:
    import json

    session = _recon_session(project)
    result = session.reconstruct_identify(photo_id)
    session.save(project)
    typer.echo(json.dumps(result, indent=2))


@recon_app.command("confirm")
def reconstruct_confirm_cmd(
    project: Path,
    item_id: str,
    catalog: str = typer.Option("", "--catalog"),
    manufacturer: str = typer.Option("", "--manufacturer"),
    width: Optional[float] = typer.Option(None, "--width-mm"),
    height: Optional[float] = typer.Option(None, "--height-mm"),
    depth: Optional[float] = typer.Option(None, "--depth-mm"),
    mount: str = typer.Option("", "--mount"),
    tag: str = typer.Option("", "--tag"),
    library_id: str = typer.Option("", "--library-id"),
) -> None:
    session = _recon_session(project)
    result = session.reconstruct_confirm_item(
        item_id,
        catalog=catalog,
        manufacturer=manufacturer,
        envelope_w_mm=width,
        envelope_h_mm=height,
        envelope_d_mm=depth,
        mount=mount,
        tag=tag,
        library_id=library_id,
    )
    session.save(project)
    typer.echo(f"{result['id']} {result['status']}")


@recon_app.command("place-needs-info")
def reconstruct_place_needs_info_cmd(project: Path) -> None:
    import json

    session = _recon_session(project)
    result = session.reconstruct_place_needs_info()
    session.save(project)
    typer.echo(json.dumps(result, indent=2))


@recon_app.command("place")
def reconstruct_place_cmd(project: Path, sheet: Optional[str] = typer.Option(None, "--sheet")) -> None:
    import json

    session = _recon_session(project)
    result = session.reconstruct_place_confirmed(sheet)
    session.save(project)
    typer.echo(json.dumps(result, indent=2))


@recon_app.command("checklist")
def reconstruct_checklist_cmd(project: Path) -> None:
    import json

    typer.echo(json.dumps(_recon_session(project).reconstruct_checklist(), indent=2))


@recon_app.command("next")
def reconstruct_next_cmd(project: Path) -> None:
    import json

    typer.echo(json.dumps(_recon_session(project).reconstruct_next_question(), indent=2))


@recon_app.command("answer")
def reconstruct_answer_cmd(
    project: Path,
    step_id: str,
    text: str = typer.Option("", "--text"),
    photo: Optional[Path] = typer.Option(None, "--photo"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    import json

    session = _recon_session(project)
    result = session.reconstruct_answer(step_id, text, photo=photo, confirm=confirm)
    session.save(project)
    typer.echo(json.dumps(result, indent=2))


@recon_app.command("compare")
def reconstruct_compare_cmd(
    project: Path,
    print_path: Optional[Path] = typer.Option(None, "--print", help="Secondary old print (.pcad.yaml, print-index YAML, PDF)"),
) -> None:
    import json

    session = _recon_session(project)
    result = session.reconstruct_compare_print(print_path)
    session.save(project)
    typer.echo(json.dumps(result, indent=2))


@recon_app.command("unconfirmed")
def reconstruct_unconfirmed_cmd(project: Path) -> None:
    import json

    typer.echo(json.dumps(_recon_session(project).reconstruct_unconfirmed(), indent=2))


def main() -> None:
    _configure_stdio()
    app()


if __name__ == "__main__":
    main()
