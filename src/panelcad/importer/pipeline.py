"""Resumable print import. Vision work is one crop at a time."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from PIL import Image

from panelcad.api.session import Session
from panelcad.importer.build import build_session
from panelcad.importer.extract import normalize_classification, normalize_submission, slice_facts
from panelcad.importer.ingest import ingest_paths, is_print_path
from panelcad.importer.parts import match_rows
from panelcad.importer.photos import layout_targets, proposals_from_photo
from panelcad.importer.read import page_kind_from_title, read_title_block
from panelcad.importer.review import (
    apply_decision,
    build_review,
    pages_with_decisions,
    stamp_review_crop,
)
from panelcad.importer.schemas import instructions_for, schema_for
from panelcad.importer.stitch import stitch_pages
from panelcad.importer.tile import _BLANK_INK, crop_tile, image_ink_fraction, merge_extractions, partition_page_tiles
from panelcad.importer.diagnostics import ImportDiagnostics
from panelcad.importer.vision import (
    PROMPT_VERSION,
    NeedsSubmit,
    VisionFallback,
    backend_for,
    detections_to_facts,
    filter_vision_detections,
    reset_vision_trace,
    set_vision_trace,
)
from panelcad.importer.vision_config import PRESET_NONE, VisionSettings
from panelcad.importer.workspace import Workspace
from panelcad.library.parts import PartsCatalog

RASTER_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

def _preview_paint_allowed() -> bool:
    """True only when Qt is already up on this thread.

    Import bookkeeping must not construct ``QApplication``. Doing so, or
    painting from the import worker, aborts the process. On Windows that
    exit code is 3, and it happens after the project has been saved.
    """

    try:
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import QApplication
    except Exception:  # noqa: BLE001 — no Qt means no preview, not a failed import
        return False
    app = QApplication.instance()
    if app is None:
        return False
    try:
        return QThread.currentThread() is app.thread()
    except Exception:  # noqa: BLE001
        return False


def _read_part_tags(pages: list[dict[str, Any]], report: dict[str, Any]) -> list[str]:
    """Tags the read actually found, including ones the build did not place."""

    tags: list[str] = []
    seen: set[str] = set()
    for page in pages:
        merged = page.get("merged") if isinstance(page.get("merged"), dict) else {}
        for device in (merged or {}).get("devices") or []:
            if not isinstance(device, dict) or device.get("endpoint"):
                continue
            tag = str(device.get("tag") or "").strip()
            if tag and tag not in seen:
                seen.add(tag)
                tags.append(tag)
    for row in report.get("skipped") or []:
        tag = str(row.get("tag") or "").strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def _page_is_svg(page: dict[str, Any]) -> bool:
    return str(page.get("source") or "").lower().endswith(".svg")


def _empty_import_warning(
    pages: list[dict[str, Any]],
    report: dict[str, Any],
    *,
    vision_preset: str = "",
) -> str:
    """No placed device is a missed read. An SVG says why, and does not mention vision."""

    if pages and all(_page_is_svg(page) for page in pages):
        notes: list[str] = []
        for page in pages:
            note = str(page.get("svg_notice") or "").strip()
            if note and note not in notes:
                notes.append(note)
        if notes:
            return " ".join(notes)
        return "Import did not build a drawing: no devices were placed. The SVG was read as vectors."
    found = _read_part_tags(pages, report)
    if found:
        count = len(found)
        noun = "part" if count == 1 else "parts"
        return (
            f"Read {count} {noun}. They are listed in the review and stay off the BOM and the quote. "
            "Open review to see them."
        )
    scan = bool(pages) and any(not page.get("vector") and not _page_is_svg(page) for page in pages)
    if scan and str(vision_preset or "").strip().lower() in {"", "none"}:
        return SCAN_NEEDS_VISION
    if any(page.get("model_read_nothing") for page in pages):
        return "the model read nothing. " + EMPTY_IMPORT_MESSAGE
    if not report.get("wires"):
        return "read nothing. " + EMPTY_IMPORT_MESSAGE
    return "read nothing. " + EMPTY_IMPORT_MESSAGE


SCAN_NEEDS_VISION = "A scan needs a vision backend and none was chosen."

EMPTY_IMPORT_MESSAGE = (
    "Import did not build a drawing: no devices were placed. "
    "The page was read, but the result is empty. "
    "Check the review list, the page rotation, and the vision backend."
)


def page_kind_words(kind: str) -> str:
    """Words for a page kind. The raw token ``other`` is not shown."""

    token = str(kind or "").strip().lower()
    if token in {"", "other"}:
        return ""
    return token.replace("_", " ")


def describe_wire_recovery(
    *,
    devices: int,
    numbers_read: int,
    connected: int,
    unconnected: int,
) -> str:
    """Plain words for what an import placed and which wire numbers did not land."""

    text = (
        f"Placed {int(devices)} devices. "
        f"Read {int(numbers_read)} wire numbers. "
        f"Connected {int(connected)} wires. "
        f"{int(unconnected)} numbers were left unconnected."
    )
    read = int(numbers_read)
    landed = int(connected)
    if read and (landed == 0 or landed * 2 < read):
        text += " Most of those numbers did not land on a wire. See the Problems list."
    return text


class EmptyImport(RuntimeError):
    """The print saved, but it is not a drawing. Callers must not report built."""


class ImportJob:
    def __init__(self, root: Path | str) -> None:
        self.ws = Workspace(root)
        self.manifest: dict[str, Any] = self.ws.manifest()
        self.backend_name = str(self.manifest.get("backend") or "auto")
        self._recordings: dict[str, dict[str, Any]] | None = None
        self._backend: Any = None
        self._on_tick: Any = None
        self._on_cost_guard: Any = None
        self._cost_guard_declined = False
        self._cost_guard: dict[str, Any] = {}
        self._tiles_done = 0
        self._tiles_total = 0
        self._spent_usd = 0.0
        self._input_tokens = 0
        self._estimated_usd = 0.0
        self._fallbacks = 0
        self._fallback_reasons: list[str] = []
        self._page_started: dict[str, float] = {}
        self.diagnostics: ImportDiagnostics | None = None
        self._load_bookkeeping()

    @classmethod
    def start(
        cls,
        sources: list[Path | str],
        workspace: Path | str,
        *,
        photos: Path | str | None = None,
        standard: str | None = None,
        backend: str = "auto",
        recordings: dict[str, dict[str, Any]] | None = None,
        model: str | None = None,
        vision: VisionSettings | None = None,
        transport: Any = None,
        on_tick: Any = None,
        on_cost_guard: Any = None,
        dpi: float | None = None,
        truth: Path | str | None = None,
    ) -> ImportJob:
        job = cls.prepare(
            sources,
            workspace,
            photos=photos,
            standard=standard,
            backend=backend,
            recordings=recordings,
            model=model,
            vision=vision,
            transport=transport,
            on_tick=on_tick,
            on_cost_guard=on_cost_guard,
            dpi=dpi,
            truth=truth,
        )
        job.run()
        return job

    @classmethod
    def prepare(
        cls,
        sources: list[Path | str],
        workspace: Path | str,
        *,
        photos: Path | str | None = None,
        standard: str | None = None,
        backend: str = "auto",
        recordings: dict[str, dict[str, Any]] | None = None,
        model: str | None = None,
        vision: VisionSettings | None = None,
        transport: Any = None,
        on_tick: Any = None,
        on_cost_guard: Any = None,
        dpi: float | None = None,
        truth: Path | str | None = None,
    ) -> ImportJob:
        """Create the workspace and stop before the long read, so Cancel can see the job."""

        job = cls(workspace)
        settings = _vision_settings(backend, model, vision)
        if settings.preset != PRESET_NONE:
            backend = settings.preset
        elif str(backend or "auto").strip().lower() == "auto":
            # preset none must not probe a live model. The page is still read from pixels.
            backend = "raster"
        job.backend_name = backend
        job._recordings = recordings
        job._on_tick = on_tick
        job._on_cost_guard = on_cost_guard
        job._backend = backend_for(backend, recordings, model, vision=settings, transport=transport)
        job.manifest = {
            "stage": "ingest",
            "sources": [str(path) for path in sources],
            "standard": standard or "",
            "backend": backend,
            "vision": settings.public_dict(),
            "queue": [],
            "pending": "",
            "pages": [],
            "photos": [],
            "dpi": float(dpi) if dpi else "",
        }
        # A reused directory must not keep the previous job's limit or tile counts.
        job._cost_guard = {}
        job._cost_guard_declined = False
        job._save()
        if photos:
            job._add_photo_files(_image_files(photos))
        job._attach_diagnostics(truth)
        return job

    def run(self) -> ImportJob:
        self._open_vision_job()
        try:
            self.pump()
            self._drain()
        finally:
            self._close_vision_job()
            if self.diagnostics is not None:
                self.diagnostics.write()
        return self

    @classmethod
    def resume(
        cls,
        workspace: Path | str,
        *,
        backend: str | None = None,
        recordings: dict[str, dict[str, Any]] | None = None,
        model: str | None = None,
        vision: VisionSettings | None = None,
        transport: Any = None,
        on_tick: Any = None,
        on_cost_guard: Any = None,
        truth: Path | str | None = None,
    ) -> ImportJob:
        job = cls(workspace)
        if not (job.ws.root / "manifest.json").is_file():
            raise FileNotFoundError(f"no import workspace at {job.ws.root}")
        stored = VisionSettings.from_dict(job.manifest.get("vision") or {})
        settings = vision.resolved() if vision is not None else stored
        if vision is None and backend:
            settings = _vision_settings(backend, model, None) if backend else stored
        job.backend_name = backend or str(job.manifest.get("backend") or "auto")
        if settings.preset != PRESET_NONE:
            job.backend_name = settings.preset
        elif str(job.backend_name).strip().lower() == "auto":
            job.backend_name = "raster"
        job._recordings = recordings
        job._on_tick = on_tick
        job._on_cost_guard = on_cost_guard
        job._backend = backend_for(job.backend_name, recordings, model, vision=settings, transport=transport)
        job.manifest["vision"] = settings.public_dict()
        job.manifest["backend"] = job.backend_name
        job._attach_diagnostics(truth)
        job._open_vision_job()
        try:
            job.pump()
            job._drain()
        finally:
            job._close_vision_job()
            if job.diagnostics is not None:
                job.diagnostics.write()
        return job

    def pump(self) -> dict[str, Any] | None:
        """Run every step that does not need a model. Return the next crop, if any."""

        for _ in range(40):
            if self.manifest.get("pending"):
                return self.current_work()
            stage = self.manifest.get("stage") or "ingest"
            if stage == "ingest":
                self._ingest()
                self.manifest["stage"] = "classify"
            elif stage == "classify":
                work = self._classify()
                if work:
                    return work
                self.manifest["stage"] = "tile"
            elif stage == "tile":
                self._tile()
                self.manifest["stage"] = "extract"
            elif stage == "extract":
                work = self._extract()
                if work:
                    return work
                self.manifest["stage"] = "parts"
            elif stage == "parts":
                work = self._parts()
                if work:
                    return work
                self.manifest["stage"] = "photos"
            elif stage == "photos":
                work = self._photos()
                if work:
                    return work
                self.manifest["stage"] = "review"
            elif stage == "review":
                self._ensure_review(force=False)
                self.manifest["stage"] = "ready"
            else:
                self._save()
                return None
            self._save()
        self._save()
        return self.current_work() if self.manifest.get("pending") else None

    def submit(self, work_id: str, data: dict[str, Any]) -> dict[str, Any]:
        pending = str(self.manifest.get("pending") or "")
        if not pending:
            raise RuntimeError("nothing is waiting for a submission")
        if work_id != pending:
            raise ValueError(f"expected {pending}")
        if not isinstance(data, dict):
            raise ValueError("submission must be a JSON object")
        work = self.current_work()
        self._apply(work, data)
        self.manifest["pending"] = ""
        self.manifest["queue"] = [item for item in self.manifest.get("queue") or [] if item != work_id]
        self._save()
        self.pump()
        self._drain()
        return self.status()

    def status(self) -> dict[str, Any]:
        pages = []
        for page_id in self.manifest.get("pages") or []:
            page = self._page(page_id)
            info = page.get("classification") or {}
            merged = self.ws.read_json("pages", page_id, "merged.json", default={}) or {}
            pages.append(
                {
                    "id": page_id,
                    "kind": "" if str(info.get("kind") or "").strip().lower() == "other" else (info.get("kind") or ""),
                    "title": info.get("title") or "",
                    "standard": info.get("standard") or "",
                    "vector": bool(page.get("vector")),
                    "devices": len(merged.get("devices") or []),
                    "wires": len(merged.get("wires") or []),
                    "ocr_skipped": page.get("ocr_skipped") or "",
                    "seconds": page.get("seconds") or 0,
                    "rotation": page.get("rotation") or 0,
                }
            )
        work = None
        if self.manifest.get("pending"):
            work = self.current_work()
        return {
            "stage": self.manifest.get("stage"),
            "workspace": str(self.ws.root),
            "standard": self.manifest.get("standard") or "",
            "pending": bool(self.manifest.get("pending")),
            "queue": len(self.manifest.get("queue") or []),
            "vision": dict(self.manifest.get("vision") or {"preset": PRESET_NONE}),
            "pages": pages,
            "photos": len(self.manifest.get("photos") or []),
            "work": work,
            "summary": self.summary(),
        }

    def summary(self) -> dict[str, Any]:
        client = self._vision_client()
        if client is not None:
            self._spent_usd = float(getattr(client, "spent_usd", self._spent_usd) or 0.0)
            self._input_tokens = int(getattr(client, "input_tokens", self._input_tokens) or 0)
            self._estimated_usd = float(getattr(client, "estimated_usd", self._estimated_usd) or 0.0)
        return self._compact_summary()

    def current_work(self) -> dict[str, Any]:
        work_id = str(self.manifest.get("pending") or "")
        if not work_id:
            raise RuntimeError("no work item")
        data = self.ws.read_json("work", _safe_id(work_id) + ".json")
        if not isinstance(data, dict):
            raise RuntimeError(f"missing work item {work_id}")
        data = dict(data)
        data["summary"] = self._compact_summary()
        data["instructions"] = instructions_for(str(data.get("kind") or "tile"))
        data["schema"] = schema_for(str(data.get("kind") or "tile"))
        return data

    def add_photos(self, path: Path | str | list[Path | str]) -> dict[str, Any]:
        files = _image_files(path) if not isinstance(path, list) else [Path(item) for item in path]
        self._add_photo_files(files)
        stage = self.manifest.get("stage")
        if stage in {"ready", "built", "review"}:
            self.manifest["stage"] = "photos"
            self._save()
            self.pump()
            self._drain()
        return self.status()

    def decide(self, item_id: str, action: str, text: str | None = None) -> dict[str, Any]:
        items = self.review_items()
        item = apply_decision(items, item_id, action, text)
        self.ws.write_json(items, "review.json")
        return item

    def review_items(self) -> list[dict[str, Any]]:
        self._ensure_review(force=False)
        data = self.ws.read_json("review.json", default=[])
        return list(data or [])

    def rerun(self, *, tile_id: str | None = None, page_id: str | None = None) -> dict[str, Any]:
        if not tile_id and not page_id:
            raise ValueError("rerun needs tile_id or page_id")
        targets = []
        for pid in self.manifest.get("pages") or []:
            if page_id and pid != page_id:
                continue
            tiles = self.ws.read_json("pages", pid, "tiles.json", default=[]) or []
            for tile in tiles:
                if tile_id and tile["id"] != tile_id:
                    continue
                if page_id or tile["id"] == tile_id:
                    targets.append((pid, tile["id"]))
        if not targets:
            raise KeyError(tile_id or page_id or "")
        for pid, tid in targets:
            path = self.ws.root / "pages" / pid / "tiles" / f"{tid}.json"
            if path.is_file():
                path.unlink()
            page = self._page(pid)
            page["extracted"] = False
            self._save_page(page)
            merged = self.ws.root / "pages" / pid / "merged.json"
            if merged.is_file():
                merged.unlink()
        review_path = self.ws.root / "review.json"
        if review_path.is_file():
            review_path.unlink()
        self.manifest["stage"] = "extract"
        self.manifest["pending"] = ""
        self._save()
        self.pump()
        self._drain()
        return self.status()

    def build(self, *, out: Path | str | None = None, name: str = "") -> Session:
        if self.manifest.get("stage") not in {"ready", "built"}:
            self.pump()
            self._drain()
        if self.manifest.get("pending"):
            raise RuntimeError("import is waiting on a crop; submit it or pass a vision backend before build")
        pages = []
        for page_id in self.manifest.get("pages") or []:
            page = self._page(page_id)
            page["merged"] = self.ws.read_json("pages", page_id, "merged.json", default={}) or {}
            pages.append(page)
        items = self.review_items()
        decided = pages_with_decisions(pages, items) if items else pages
        stitched = stitch_pages(decided, standard_override=str(self.manifest.get("standard") or ""))
        self._apply_part_matches(stitched)
        photo_docs = []
        for photo_id in self.manifest.get("photos") or []:
            doc = self.ws.read_json("photos", f"{photo_id}.json", default=None)
            if isinstance(doc, dict) and doc.get("proposal"):
                photo_docs.append(doc["proposal"])
        targets = layout_targets(photo_docs, _tags_by_kind(stitched))
        block = stitched.get("title_block") or {}
        session, report = build_session(
            stitched,
            name=name or block.get("project") or block.get("title") or block.get("drawing_number") or "Imported print",
            photo_targets=targets,
        )
        self._publish_diagnostics(session, report)
        self.ws.write_json(stitched, "stitch.json")
        self.ws.write_json(report, "build.json")
        if out is not None:
            session.save(out)
            self.manifest["project"] = str(out)
        else:
            dest = self.ws.path("project.pcad.yaml")
            session.save(dest)
            self.manifest["project"] = str(dest)
        self._remember_result(session, report)
        if not report.get("placed"):
            # A cancel already has its own message. Do not replace it with an empty-import failure.
            if self.manifest.get("cancelled"):
                self.manifest["stage"] = "cancelled"
                self._save()
                return session
            self.manifest["stage"] = "empty"
            vision = self.manifest.get("vision") or {}
            warning = _empty_import_warning(
                decided,
                report,
                vision_preset=str(vision.get("preset") or ""),
            )
            self.manifest["warning"] = warning
            info = session.project.properties.setdefault("print_import", {})
            if isinstance(info, dict):
                info["warning"] = warning
            session.save(self.manifest["project"])
            self._save()
            raise EmptyImport(warning)
        self.manifest["stage"] = "built"
        self._save()
        self._stamp_rebuilt(session)
        return session

    def _drain(self) -> None:
        backend = self._backend
        if backend is None or getattr(backend, "name", "") == "external":
            return
        if not getattr(backend, "available", lambda: False)():
            return
        for _ in range(80):
            if not self.manifest.get("pending"):
                return
            work = self.current_work()
            try:
                data = backend.extract(work)
            except NeedsSubmit:
                return
            self._apply(work, data)
            self.manifest["pending"] = ""
            self.manifest["queue"] = [item for item in self.manifest.get("queue") or [] if item != work["id"]]
            self._save()
            self.pump()

    def _ingest(self) -> None:
        if self.manifest.get("pages"):
            return
        sources = [Path(path) for path in self.manifest.get("sources") or []]
        raw_dpi = self.manifest.get("dpi")
        dpi = float(raw_dpi) if raw_dpi else None
        pages = ingest_paths(sources, self.ws, dpi=dpi)
        self.manifest["pages"] = [page["id"] for page in pages]

    def _classify(self) -> dict[str, Any] | None:
        for page_id in self.manifest.get("pages") or []:
            page = self._page(page_id)
            if page.get("classification"):
                continue
            self._note_page_begin(page_id)
            if page.get("vector"):
                tokens = self.ws.read_json("pages", page_id, "words.json", default=[]) or []
                title = read_title_block(tokens, float(page["height_mm"]))
                kind = page_kind_from_title(title, str(page.get("text") or ""))
                page["classification"] = {**title, "kind": kind, "confidence": 0.9, "source": "vector"}
                self._save_page(page)
                continue
            if self._use_raster():
                work = self._read_raster(page)
                if work:
                    return work
                continue
            return self._enqueue("classify", page_id, page, None)
        return None

    def _tile(self) -> None:
        for page_id in self.manifest.get("pages") or []:
            existing = self.ws.read_json("pages", page_id, "tiles.json", default=None)
            if existing:
                continue
            page = self._page(page_id)
            tiles, skipped = partition_page_tiles(page)
            for tile in tiles:
                dest = self.ws.path("pages", page_id, "tiles", f"{tile['id']}.png")
                crop_tile(page, tile, dest)
                tile["crop"] = str(dest)
            for tile in skipped:
                self._write_tile_skip(page_id, tile)
            self.ws.write_json(tiles, "pages", page_id, "tiles.json")

    def _write_tile_skip(self, page_id: str, tile: dict[str, Any]) -> None:
        """A grid cell the vision pool does not read. The file names the reason."""

        reason = str(tile.get("skip_reason") or "skipped")
        body = {
            "devices": [],
            "wires": [],
            "xrefs": [],
            "texts": [],
            "skipped": True,
            "reason": reason,
            "read": {
                "tile_box": {
                    "x": tile.get("x"),
                    "y": tile.get("y"),
                    "w": tile.get("w"),
                    "h": tile.get("h"),
                },
                "raw_text": "",
                "parsed": [],
                "drops": [{"reason": reason}],
                "error": "",
            },
        }
        if tile.get("ink") is not None:
            body["ink"] = tile.get("ink")
        self.ws.write_json(body, "pages", page_id, "tiles", f"{tile['id']}.json")

    def _extract(self) -> dict[str, Any] | None:
        vision_batch: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        deferred: dict[str, Any] | None = None
        for page_id in self.manifest.get("pages") or []:
            page = self._page(page_id)
            if page.get("extracted"):
                continue
            kind = str((page.get("classification") or {}).get("kind") or "")
            if kind in {"cover", "index"}:
                self.ws.write_json({"devices": [], "wires": [], "xrefs": [], "texts": []}, "pages", page_id, "merged.json")
                self._note_page_begin(page_id)
                self._note_page_end(page)
                page["extracted"] = True
                self._save_page(page)
                continue
            tiles = self.ws.read_json("pages", page_id, "tiles.json", default=[]) or []
            if page.get("vector"):
                self._extract_vector(page, tiles)
                continue
            if (self.ws.root / "pages" / page_id / "raster.json").is_file():
                if self._vision_client() is not None and not self.manifest.get("cancelled"):
                    vision_batch.append((page, tiles))
                else:
                    self._extract_saved(page, tiles, "raster.json")
                    if self.manifest.get("cancelled"):
                        page["vision_fallback"] = "cancelled"
                        self._save_page(page)
                continue
            queued = False
            for tile in tiles:
                path = self.ws.root / "pages" / page_id / "tiles" / f"{tile['id']}.json"
                if path.is_file():
                    continue
                if kind == "bom":
                    deferred = self._enqueue("bom", page_id, page, tile)
                else:
                    deferred = self._enqueue("tile", page_id, page, tile)
                queued = True
                break
            if queued:
                break
            self._merge_saved_tiles(page, tiles)
        if vision_batch:
            self._run_vision_pool(vision_batch)
        return deferred

    def _extract_vector(self, page: dict[str, Any], tiles: list[dict[str, Any]]) -> None:
        from panelcad.importer.vectors import read_drawing
        from panelcad.library.catalog import default_catalog

        tokens = self.ws.read_json("pages", page["id"], "words.json", default=[]) or []
        strokes = self.ws.read_json("pages", page["id"], "strokes.json", default=[]) or []
        classification = page.get("classification") or {}
        standard = str(classification.get("standard") or "")
        facts = read_drawing(
            tokens,
            strokes,
            float(page["width_mm"]),
            float(page["height_mm"]),
            _device_catalog(),
            standard=standard,
            kind=str(classification.get("kind") or ""),
        )
        self._slice_facts(page, tiles, facts)

    def _extract_saved(self, page: dict[str, Any], tiles: list[dict[str, Any]], name: str) -> None:
        facts = self.ws.read_json("pages", page["id"], name, default={}) or {}
        self._slice_facts(page, tiles, facts if isinstance(facts, dict) else {})

    def _slice_facts(self, page: dict[str, Any], tiles: list[dict[str, Any]], facts: dict[str, Any]) -> None:
        parts = []
        for tile in tiles:
            piece = slice_facts(facts, tile)
            crop = str(tile.get("crop") or "")
            for group in piece.values():
                for item in group:
                    item["page_id"] = page["id"]
                    stamp_review_crop(item, tile, crop)
            self.ws.write_json(piece, "pages", page["id"], "tiles", f"{tile['id']}.json")
            parts.append(piece)
        merged = merge_extractions(parts)
        self.ws.write_json(merged, "pages", page["id"], "merged.json")
        if self.diagnostics is not None:
            self.diagnostics.ingest_pixel(
                page_id=str(page.get("id") or ""),
                devices=list(merged.get("devices") or []),
                wires=list(merged.get("wires") or []),
            )
        self._note_page_end(page)
        page["extracted"] = True
        self._save_page(page)

    def _parts(self) -> dict[str, Any] | None:
        if self.ws.read_json("parts.json", default=None) is not None and self.manifest.get("parts_done"):
            return None
        rows: list[dict[str, Any]] = []
        for page_id in self.manifest.get("pages") or []:
            page = self._page(page_id)
            if str((page.get("classification") or {}).get("kind") or "") != "bom":
                continue
            if page.get("vector"):
                continue
            merged = self.ws.read_json("pages", page_id, "merged.json", default={}) or {}
            if merged.get("rows"):
                rows.extend(merged["rows"])
                continue
            tiles = self.ws.read_json("pages", page_id, "tiles.json", default=[]) or []
            tile = tiles[0] if tiles else None
            return self._enqueue("bom", page_id, page, tile)
        parts_catalog = _parts_catalog()
        matched = match_rows(rows, parts_catalog)
        self.ws.write_json(matched, "parts.json")
        self.manifest["parts_done"] = True
        return None

    def _photos(self) -> dict[str, Any] | None:
        for photo_id in self.manifest.get("photos") or []:
            doc = self.ws.read_json("photos", f"{photo_id}.json", default={}) or {}
            if doc.get("proposal") or doc.get("skipped"):
                continue
            return self._enqueue_photo(doc)
        return None

    def _enqueue(self, kind: str, page_id: str, page: dict[str, Any], tile: dict[str, Any] | None) -> dict[str, Any]:
        if kind == "classify":
            work_id = f"{page_id}:classify"
            image = self._classify_image(page)
            page_mm = {"x": 0, "y": 0, "w": page["width_mm"], "h": page["height_mm"]}
            tile_id = ""
        elif tile is None:
            work_id = f"{page_id}:{kind}"
            image = str(page.get("image") or "")
            page_mm = {"x": 0, "y": 0, "w": page["width_mm"], "h": page["height_mm"]}
            tile_id = ""
        else:
            work_id = f"{page_id}:{tile['id']}"
            image = str(tile.get("crop") or "")
            page_mm = {"x": tile["x"], "y": tile["y"], "w": tile["w"], "h": tile["h"]}
            tile_id = tile["id"]
        work = {
            "id": work_id,
            "kind": "tile" if kind == "tile" else kind,
            "page_id": page_id,
            "tile_id": tile_id,
            "image": image,
            "page_mm": page_mm,
            "page_size_mm": {"w": page["width_mm"], "h": page["height_mm"]},
        }
        self.ws.write_json(work, "work", _safe_id(work_id) + ".json")
        if work_id not in self.manifest["queue"]:
            self.manifest["queue"].append(work_id)
        self.manifest["pending"] = work_id
        self._save()
        return self.current_work()

    def _enqueue_photo(self, doc: dict[str, Any]) -> dict[str, Any]:
        work_id = f"{doc['id']}:photo"
        work = {
            "id": work_id,
            "kind": "photo",
            "page_id": "",
            "tile_id": "",
            "photo_id": doc["id"],
            "image": doc.get("image") or "",
            "page_mm": {"x": 0, "y": 0, "w": 1, "h": 1},
        }
        self.ws.write_json(work, "work", _safe_id(work_id) + ".json")
        self.manifest["queue"].append(work_id)
        self.manifest["pending"] = work_id
        self._save()
        return self.current_work()

    def _apply(self, work: dict[str, Any], data: dict[str, Any]) -> None:
        kind = str(work.get("kind") or "")
        if kind == "classify":
            page = self._page(str(work["page_id"]))
            page["classification"] = {**normalize_classification(data), "source": "vision"}
            self._save_page(page)
            return
        if kind == "photo":
            photo_id = str(work.get("photo_id") or "")
            doc = self.ws.read_json("photos", f"{photo_id}.json", default={}) or {}
            doc["proposal"] = proposals_from_photo(doc, data if isinstance(data, dict) else {})
            self.ws.write_json(doc, "photos", f"{photo_id}.json")
            return
        if kind == "bom":
            page_id = str(work.get("page_id") or "")
            rows = data.get("rows") if isinstance(data, dict) else []
            cleaned = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                unreadable = bool(row.get("unreadable"))
                cleaned.append(
                    {
                        "qty": "" if unreadable else str(row.get("qty") or ""),
                        "catalog": "" if unreadable else str(row.get("catalog") or "").strip(),
                        "description": "" if unreadable else str(row.get("description") or ""),
                        "tag": "" if unreadable else str(row.get("tag") or "").strip(),
                        "unreadable": unreadable or not str(row.get("catalog") or "").strip(),
                        "confidence": 0.0 if unreadable else float(row.get("confidence") or 0.7),
                        "crop": str(work.get("image") or ""),
                    }
                )
            self.ws.write_json({"rows": cleaned, "devices": [], "wires": []}, "pages", page_id, "merged.json")
            page = self._page(page_id)
            page["extracted"] = True
            self._save_page(page)
            self.manifest["parts_done"] = False
            return
        page_id = str(work["page_id"])
        tile_id = str(work.get("tile_id") or "")
        tiles = self.ws.read_json("pages", page_id, "tiles.json", default=[]) or []
        tile = next((item for item in tiles if item["id"] == tile_id), None)
        piece = normalize_submission(data, tile)
        crop = str(work.get("image") or "")
        for group in piece.values():
            for item in group:
                if tile is not None:
                    item["page_id"] = page_id
                    stamp_review_crop(item, tile, crop)
                else:
                    item["tile_id"] = tile_id
                    item["page_id"] = page_id
                    item["crop"] = crop
        self.ws.write_json(piece, "pages", page_id, "tiles", f"{tile_id}.json")
        page = self._page(page_id)
        tiles = self.ws.read_json("pages", page_id, "tiles.json", default=[]) or []
        if all((self.ws.root / "pages" / page_id / "tiles" / f"{item['id']}.json").is_file() for item in tiles):
            self._merge_saved_tiles(page, tiles)

    def _merge_saved_tiles(self, page: dict[str, Any], tiles: list[dict[str, Any]]) -> None:
        parts = []
        for tile in tiles:
            piece = self.ws.read_json("pages", page["id"], "tiles", f"{tile['id']}.json", default=None)
            if isinstance(piece, dict):
                parts.append(piece)
        merged = merge_extractions(parts)
        self.ws.write_json(merged, "pages", page["id"], "merged.json")
        self._note_page_begin(page["id"])
        self._note_page_end(page)
        page["extracted"] = True
        self._save_page(page)
        review = self.ws.root / "review.json"
        if review.is_file():
            review.unlink()

    def stamp_rebuilt_previews(self, session: Session) -> None:
        """Paint review crops on this thread.

        The import worker must not paint. ``ImportWizard._import_ready`` calls
        this after the worker finishes, on the GUI thread. The CLI never
        reaches a live ``QApplication``, so it still skips the paint.
        """

        self._stamp_rebuilt(session)

    def _stamp_rebuilt(self, session: Session) -> None:
        """Optional review crops. Never required for a saved project.

        Painting starts Qt. Creating a ``QApplication`` here, or painting off
        the GUI thread, makes Qt call abort(). On Windows that is exit code 3,
        after the project file is already on disk and before the CLI can print
        a summary. Skip unless an application is already running on this thread.
        """

        items = self.review_items()
        if not items or not _preview_paint_allowed():
            return
        from panelcad.importer.preview import attach_rebuilt_previews

        try:
            attach_rebuilt_previews(session, items, self.ws.path("review"))
        except Exception:  # noqa: BLE001 — a missing preview does not undo the project
            return
        self.ws.write_json(items, "review.json")

    def _ensure_review(self, *, force: bool) -> None:
        path = self.ws.root / "review.json"
        if path.is_file() and not force:
            return
        pages = []
        for page_id in self.manifest.get("pages") or []:
            page = self._page(page_id)
            page["merged"] = self.ws.read_json("pages", page_id, "merged.json", default={}) or {}
            pages.append(page)
        parts = self.ws.read_json("parts.json", default=[]) or []
        photos = []
        for photo_id in self.manifest.get("photos") or []:
            doc = self.ws.read_json("photos", f"{photo_id}.json", default={}) or {}
            if doc.get("proposal"):
                photos.append(doc["proposal"])
        self.ws.write_json(build_review(pages, parts, photos), "review.json")

    def _apply_part_matches(self, stitched: dict[str, Any]) -> None:
        rows = self.ws.read_json("parts.json", default=[]) or []
        by_tag = {str(row.get("tag")): row for row in rows if row.get("matched") and row.get("tag")}
        unknown = [row for row in rows if row.get("flag") == "unknown" and row.get("catalog")]
        for sheet in stitched.get("sheets") or []:
            for device in sheet.get("devices") or []:
                row = by_tag.get(str(device.get("tag")))
                if not row:
                    continue
                device["library_id"] = row.get("library_id") or device.get("library_id")
                device["catalog"] = row.get("catalog") or ""
                device["manufacturer"] = row.get("manufacturer") or ""
                device["catalog_matched"] = True
                device["library_guess"] = False
                device["confidence"] = max(float(device.get("confidence") or 0), 0.9)
        if unknown:
            items = stitched.setdefault("unknown_parts", [])
            items.extend(unknown)

    def cancel(self) -> None:
        """Stop further vision calls. Tiles not yet read stay on the pixel result."""

        if not self.manifest.get("cancelled"):
            self.manifest["cancelled_done"] = int(self._tiles_done)
            self.manifest["cancelled_total"] = int(self._tiles_total)
        self.manifest["cancelled"] = True
        client = self._vision_client()
        if client is not None:
            client.cancel()
        self._save()

    def _open_vision_job(self) -> None:
        """Reset spend once. Each page starts its own clock when its first tile is sent."""

        client = self._vision_client()
        if client is not None and hasattr(client, "begin_job"):
            client.begin_job()

    def _close_vision_job(self) -> None:
        client = self._vision_client()
        if client is not None and hasattr(client, "finish_job"):
            client.finish_job()

    def _use_raster(self) -> bool:
        if self._vision_client() is not None:
            return True
        return self.backend_name in {"raster", "auto", "configured"}

    def _vision_client(self) -> Any:
        backend = self._backend
        if getattr(backend, "name", "") in {"chat", "grok-build"}:
            return backend
        return None

    def _run_vision_pool(self, pages: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> None:
        """Read every tile on the calling thread's pool. Cost and cancel cover the whole pool."""

        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

        client = self._vision_client()
        if client is None or not pages:
            return
        workers = max(1, int(getattr(client.settings, "workers", 1) or 1))
        catalog = _device_catalog()
        jobs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        pixels: dict[str, dict[str, Any]] = {}
        standards: dict[str, str] = {}
        for page, tiles in pages:
            pixel = self.ws.read_json("pages", page["id"], "raster.json", default={}) or {}
            pixels[page["id"]] = pixel if isinstance(pixel, dict) else {}
            standards[page["id"]] = str(
                (page.get("classification") or {}).get("standard") or self.manifest.get("standard") or ""
            )
            for tile in tiles:
                jobs.append((page, tile))
        total = len(jobs)
        self._tiles_total = total
        self._tiles_done = 0
        self._fallbacks = 0
        self._fallback_reasons = []
        blocked = client.unavailable_reason()
        sheet_px: dict[str, dict[str, int]] = {}
        for page, _tiles in pages:
            try:
                with Image.open(page["image"]) as handle:
                    sheet_px[page["id"]] = {"w": int(handle.size[0]), "h": int(handle.size[1])}
            except Exception:  # noqa: BLE001 — unit detection still has the crop image
                sheet_px[page["id"]] = {"w": 0, "h": 0}
        results: list[tuple[dict[str, Any], str] | None] = [None] * total

        def run_one(index: int) -> tuple[int, dict[str, Any], str]:
            page, tile = jobs[index]
            pixel = pixels[page["id"]]
            reason = blocked or client.over_budget(page["id"])
            if reason:
                piece = _fallback_slice(pixel, tile, reason)
                piece["read"] = _tile_read_body(page, tile, None, error=reason, reported=[], parsed=[], drops=[{"reason": reason}])
                return index, piece, reason
            work = {
                "id": f"{page['id']}:{tile['id']}",
                "kind": "tile",
                "page_id": page["id"],
                "tile_id": tile["id"],
                "image": str(tile.get("crop") or ""),
                "page_mm": {"x": tile["x"], "y": tile["y"], "w": tile["w"], "h": tile["h"]},
                "sheet_mm": {"w": page.get("width_mm") or 0, "h": page.get("height_mm") or 0},
                "sheet_px": sheet_px.get(page["id"]) or {"w": 0, "h": 0},
                "dpi": page.get("dpi"),
            }
            captured: dict[str, Any] = {}

            def _hook(record: dict[str, Any]) -> None:
                captured["trace"] = record
                if self.diagnostics is not None:
                    self.diagnostics.record_exchange(record, page=page, tile=tile)

            token = set_vision_trace(_hook)
            try:
                try:
                    data = client.extract(work)
                except VisionFallback as exc:
                    if self.diagnostics is not None:
                        self.diagnostics.note_fallback(page, tile, exc.reason)
                    piece = _fallback_slice(pixel, tile, exc.reason)
                    piece["read"] = _tile_read_body(
                        page,
                        tile,
                        captured.get("trace"),
                        error=exc.reason,
                        reported=[],
                        parsed=[],
                        drops=[{"reason": exc.reason}],
                    )
                    return index, piece, exc.reason
                except AssertionError:
                    raise
                except Exception:  # noqa: BLE001 — one tile must not kill the pool
                    if self.diagnostics is not None:
                        self.diagnostics.note_fallback(page, tile, "vision request failed")
                    piece = _fallback_slice(pixel, tile, "vision request failed")
                    piece["read"] = _tile_read_body(
                        page,
                        tile,
                        captured.get("trace"),
                        error="vision request failed",
                        reported=[],
                        parsed=[],
                        drops=[{"reason": "vision request failed"}],
                    )
                    return index, piece, "vision request failed"
                reported = data.get("_reported") if isinstance(data, dict) else None
                violations = data.get("_schema_violations") if isinstance(data, dict) else None
                detections = data.get("detections") if isinstance(data, dict) else None
                pixel_devices = slice_facts(pixel, tile).get("devices") or []
                detections = filter_vision_detections(
                    list(detections or []),
                    catalog,
                    tile,
                    pixel_devices,
                    standard=standards[page["id"]],
                )
                if self.diagnostics is not None:
                    self.diagnostics.ingest_tile(
                        page_id=str(page.get("id") or ""),
                        tile_id=str(tile.get("id") or ""),
                        tile=tile,
                        reported_rows=list(reported if isinstance(reported, list) else (detections or [])),
                        violations=list(violations or []),
                        filtered_rows=list(detections or []),
                    )
                piece = detections_to_facts(detections, tile, catalog, standard=standards[page["id"]])
                piece["read"] = _tile_read_body(
                    page,
                    tile,
                    captured.get("trace"),
                    error="",
                    reported=list(reported or []),
                    parsed=list(detections or []),
                    drops=_detection_drops(list(reported or []), list(violations or []), list(detections or [])),
                )
            finally:
                reset_vision_trace(token)
            crop = str(tile.get("crop") or "")
            for group in piece.values():
                if not isinstance(group, list):
                    continue
                for item in group:
                    item["page_id"] = page["id"]
                    stamp_review_crop(item, tile, crop)
            return index, piece, ""

        done = 0
        next_index = 0
        inflight: dict[Any, int] = {}
        executor = ThreadPoolExecutor(max_workers=workers)

        opened_pages: set[str] = set()

        def note_fallback(page: dict[str, Any], tile: dict[str, Any], reason: str) -> None:
            self._fallback_reasons.append(f"{tile.get('id')}: {reason}")
            self._tick(f"{page['id']} {tile.get('id')} fallback: {reason}")

        def submit_more() -> str:
            nonlocal next_index, done
            while next_index < total and len(inflight) < workers:
                page, tile = jobs[next_index]
                job_reason = blocked or (client.job_stop() if hasattr(client, "job_stop") else "")
                if job_reason == "page cost guard" and self._offer_cost_guard(client, total - next_index):
                    job_reason = ""
                if job_reason:
                    return job_reason
                if page["id"] not in opened_pages:
                    if hasattr(client, "begin_page"):
                        client.begin_page(page["id"])
                    opened_pages.add(page["id"])
                page_reason = client.over_budget(page["id"])
                if page_reason == "page timeout":
                    results[next_index] = (_fallback_slice(pixels[page["id"]], tile, page_reason), page_reason)
                    done += 1
                    self._fallbacks += 1
                    self._tiles_done = done
                    note_fallback(page, tile, page_reason)
                    self._tick(self._tile_progress(done, total))
                    next_index += 1
                    continue
                if page_reason:
                    return page_reason
                future = executor.submit(run_one, next_index)
                inflight[future] = next_index
                next_index += 1
            return ""

        try:
            submit_more()
            while inflight:
                finished, _pending = wait(set(inflight), return_when=FIRST_COMPLETED)
                for future in finished:
                    inflight.pop(future, None)
                    index, piece, reason = future.result()
                    results[index] = (piece, reason)
                    done += 1
                    page, tile = jobs[index]
                    if reason:
                        self._fallbacks += 1
                        note_fallback(page, tile, reason)
                    self._tiles_done = done
                    self._tiles_total = total
                    self._sync_vision_account(client)
                    self._tick(f"{page['id']} {tile['id']}")
                    self._tick(self._tile_progress(done, total))
                submit_more()
            stop = blocked or (client.job_stop() if hasattr(client, "job_stop") else client.over_budget())
            if not stop and next_index < total:
                stop = "page cost guard"
            if stop == "page cost guard" and not self._cost_guard:
                self._store_cost_guard(client, total - next_index, continued=False)
            if stop:
                for index in range(next_index, total):
                    page, tile = jobs[index]
                    results[index] = (_fallback_slice(pixels[page["id"]], tile, stop), stop)
                    if stop == "cancelled":
                        continue
                    done += 1
                    self._fallbacks += 1
                    note_fallback(page, tile, stop)
                    self._tiles_done = done
                    self._tiles_total = total
                    self._sync_vision_account(client)
                    self._tick(self._tile_progress(done, total))
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        grouped: dict[str, list[dict[str, Any]]] = {page["id"]: [] for page, _tiles in pages}
        failures: dict[str, list[str]] = {page["id"]: [] for page, _tiles in pages}
        failed_tile: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(results):
            page, tile = jobs[index]
            if item is None:
                piece = _fallback_slice(pixels[page["id"]], tile, "vision request failed")
                reason = "vision request failed"
            else:
                piece, reason = item
            grouped[page["id"]].append(piece)
            if isinstance(piece, dict):
                if reason:
                    body = piece.get("read") if isinstance(piece.get("read"), dict) else {}
                    if not str(body.get("error") or ""):
                        piece["read"] = _tile_read_body(
                            page,
                            tile,
                            None,
                            error=reason,
                            reported=[],
                            parsed=[],
                            drops=[{"reason": reason}],
                        )
                self.ws.write_json(piece, "pages", page["id"], "tiles", f"{tile['id']}.json")
            if reason:
                failures[page["id"]].append(f"{tile['id']} {reason}")
                failed_tile.setdefault(page["id"], tile)
        for page, _tiles in pages:
            self._finish_vision_page(
                page,
                grouped[page["id"]],
                failures[page["id"]],
                failed_tile.get(page["id"]),
                pixels[page["id"]],
                catalog,
            )
        if self.diagnostics is not None:
            self.diagnostics.write()

    def _note_rejected_wires(
        self,
        page: dict[str, Any],
        before: list[dict[str, Any]],
        after: list[dict[str, Any]],
        devices: list[dict[str, Any]],
        catalog: Any,
        width_mm: float,
        height_mm: float,
        insets: tuple[float, float, float, float] | None,
    ) -> None:
        """Append junk-filter reasons onto the tile JSON that still holds the raw reply."""

        from panelcad.importer.raster import wire_number_reject_reason

        kept = {id(wire) for wire in after}
        extra: dict[str, list[dict[str, Any]]] = {}
        for wire in before:
            if id(wire) in kept:
                continue
            if str(wire.get("source") or "") != "model" and not wire.get("model_assisted"):
                continue
            reason = wire_number_reject_reason(wire, devices, catalog, width_mm, height_mm, insets) or "junk"
            tile_id = str(wire.get("tile_id") or "") or "_page"
            extra.setdefault(tile_id, []).append(
                {
                    "reason": reason,
                    "wire_number": str(wire.get("number") or ""),
                    "from_tag": str(wire.get("from_tag") or ""),
                    "to_tag": str(wire.get("to_tag") or ""),
                }
            )
        page_id = str(page.get("id") or "")
        for tile_id, rows in extra.items():
            name = "page-filters.json" if tile_id == "_page" else f"{tile_id}.json"
            current = self.ws.read_json("pages", page_id, "tiles", name, default=None)
            if not isinstance(current, dict):
                current = {"devices": [], "wires": [], "xrefs": [], "texts": []}
            read = dict(current.get("read") or {}) if isinstance(current.get("read"), dict) else {}
            drops = list(read.get("drops") or [])
            drops.extend(rows)
            read["drops"] = drops
            current["read"] = read
            self.ws.write_json(current, "pages", page_id, "tiles", name)

    def _finish_vision_page(
        self,
        page: dict[str, Any],
        parts: list[dict[str, Any]],
        failures: list[str],
        failed_tile: dict[str, Any] | None,
        pixel: dict[str, Any],
        catalog: Any,
    ) -> None:
        from panelcad.importer.raster import (
            align_vision_devices,
            attach_vision_wires,
            collapse_wire_numbers,
            orient_ladder_devices,
            reject_junk_wire_numbers,
            snap_model_wires,
            stitch_wire_seams,
            trace_vision_wires,
        )

        merged = merge_extractions(parts)
        image = Image.open(page["image"])
        width_mm = float(page["width_mm"])
        height_mm = float(page["height_mm"])
        standard = str((page.get("classification") or {}).get("standard") or self.manifest.get("standard") or "")
        pixel_devices = list(pixel.get("devices") or [])
        merged["devices"] = align_vision_devices(
            list(merged.get("devices") or []),
            pixel_devices,
            image,
            width_mm,
            height_mm,
            catalog,
            standard=standard,
        )
        trace_devices = _devices_for_trace(merged["devices"], pixel_devices)
        if self.diagnostics is not None:
            self.diagnostics.ingest_pixel(
                page_id=str(page.get("id") or ""),
                devices=trace_devices,
                wires=list(pixel.get("wires") or []),
            )
        raw_insets = page.get("frame_insets")
        insets = None
        if isinstance(raw_insets, (list, tuple)) and len(raw_insets) == 4:
            try:
                insets = tuple(float(value) for value in raw_insets)
            except (TypeError, ValueError):
                insets = None
        orient_ladder_devices(trace_devices, catalog, spread=False)
        before_wires = list(merged.get("wires") or [])
        merged["wires"] = reject_junk_wire_numbers(
            list(merged.get("wires") or []),
            trace_devices,
            catalog,
            width_mm,
            height_mm,
            insets,
        )
        self._note_rejected_wires(
            page,
            before_wires,
            list(merged.get("wires") or []),
            trace_devices,
            catalog,
            width_mm,
            height_mm,
            insets,
        )
        merged["wires"] = attach_vision_wires(
            merged.get("wires") or [],
            image,
            width_mm,
            height_mm,
            trace_devices,
            catalog,
            list(pixel.get("wires") or []),
        )
        traced, endpoints = trace_vision_wires(
            merged.get("wires") or [],
            image,
            width_mm,
            height_mm,
            trace_devices,
            catalog,
        )
        merged["wires"] = traced
        if endpoints:
            merged.setdefault("devices", []).extend(endpoints)
        merged["wires"] = snap_model_wires(
            merged.get("wires") or [],
            trace_devices,
            image,
            width_mm,
            height_mm,
            catalog,
        )
        merged["wires"] = stitch_wire_seams(merged.get("wires") or [])
        merged["wires"] = collapse_wire_numbers(merged.get("wires") or [])
        orient_ladder_devices(trace_devices, catalog, spread=True, page_width=width_mm)
        if self.diagnostics is not None:
            self.diagnostics.settle_page(
                page_id=str(page.get("id") or ""),
                before_devices=trace_devices,
                after_devices=list(merged.get("devices") or []),
                before_wires=before_wires,
                after_wires=list(merged.get("wires") or []),
                width_mm=width_mm,
                height_mm=height_mm,
                catalog=catalog,
                insets=insets,
                spatial_drops=list(merged.get("spatial_drops") or []),
            )
        if failures:
            reason = _fallback_summary(failures)
            page["vision_fallback"] = reason
            note_text = fallback_note(list(merged.get("devices") or []), reason)
            page["model_read_nothing"] = note_text.startswith("the model read nothing")
            note: dict[str, Any] = {
                "text": note_text,
                "role": "vision_fallback",
                "vision_fallback": reason,
                "unreadable": False,
                "confidence": 1.0,
                "source": "ocr",
            }
            if failed_tile is not None:
                note["tile_id"] = str(failed_tile.get("id") or "")
                note["page_id"] = page["id"]
                note["crop"] = str(failed_tile.get("crop") or "")
            merged.setdefault("texts", []).append(note)
        else:
            page["vision_fallback"] = ""
        self._classify_from_vision(page, list(merged.get("devices") or []))
        self.ws.write_json(merged, "pages", page["id"], "merged.json")
        self._note_page_end(page)
        page["extracted"] = True
        page["vision_model"] = str((self.manifest.get("vision") or {}).get("model") or "")
        self._save_page(page)

    def diagnostics_public(self) -> dict[str, Any]:
        if self.diagnostics is None:
            return {}
        return self.diagnostics.public()

    def _attach_diagnostics(self, truth: Path | str | None = None) -> None:
        run = str(self.manifest.get("diagnostics_run") or "") or None
        self.diagnostics = ImportDiagnostics(self.ws.root, run_id=run)
        if run:
            self.diagnostics.load()
        self.manifest["diagnostics_run"] = self.diagnostics.run_id
        self.manifest["diagnostics"] = str(self.diagnostics.root)
        self.manifest["prompt_version"] = PROMPT_VERSION
        if truth:
            self.diagnostics.truth_path = str(truth)
            self.diagnostics._score_truth = True
        self._save()

    def _publish_diagnostics(self, session: Session, report: dict[str, Any]) -> None:
        if self.diagnostics is None:
            return
        self.diagnostics.mark_built(report)
        self.diagnostics.write()
        info = session.project.properties.setdefault("print_import", {})
        if isinstance(info, dict):
            info["diagnostics_run"] = self.diagnostics.run_id
            info["diagnostics"] = str(self.diagnostics.root)
            info["prompt_version"] = self.diagnostics.prompt_version

    def _offer_cost_guard(self, client: Any, remaining: int) -> bool:
        """Warn that the page cap was hit. Continue only when the caller says so.

        With no callback the remaining tiles are still skipped, and the warning
        is stored on the job so the skip is not silent.
        """

        if getattr(client, "cost_guard_continued", False):
            return True
        if self._cost_guard_declined:
            return False
        spent = float(getattr(client, "spent_usd", 0) or 0)
        settings = getattr(client, "settings", None)
        cap = float(getattr(settings, "max_usd", 0) or 0)
        info = {
            "warned": True,
            "spent_usd": round(spent, 4),
            "max_usd": cap,
            "remaining_tiles": int(max(0, remaining)),
            "reason": "page cost guard",
        }
        allowed = False
        callback = self._on_cost_guard
        if callback is not None:
            try:
                allowed = bool(callback(dict(info)))
            except Exception:  # noqa: BLE001 — a failed prompt still warns and skips
                allowed = False
        if allowed and hasattr(client, "allow_cost_guard"):
            client.allow_cost_guard()
        else:
            self._cost_guard_declined = True
        info["continued"] = bool(getattr(client, "cost_guard_continued", False))
        self._store_cost_guard_record(info)
        if info["continued"]:
            self.manifest["warning"] = (
                f"page cost guard: ${spent:.2f} of ${cap:.2f}. "
                f"Continuing with {info['remaining_tiles']} tiles still unread."
            )
        else:
            self.manifest["warning"] = (
                f"page cost guard: ${spent:.2f} of ${cap:.2f}. "
                f"{info['remaining_tiles']} tiles were not sent. Continue to read them."
            )
        self._tick(str(self.manifest["warning"]))
        self._save()
        return bool(info["continued"])

    def _tick(self, message: str) -> None:
        if self._on_tick is None:
            return
        self._on_tick(message)

    def _sync_vision_account(self, client: Any) -> None:
        self._spent_usd = float(getattr(client, "spent_usd", 0.0) or 0.0)
        self._input_tokens = int(getattr(client, "input_tokens", 0) or 0)
        self._estimated_usd = float(getattr(client, "estimated_usd", 0.0) or 0.0)

    def _tile_progress(self, done: int, total: int) -> str:
        client = self._vision_client()
        preset = str(getattr(getattr(client, "settings", None), "preset", "") or "")
        local = preset in {"ollama", "grok-build", "none"}
        if local:
            line = f"tiles {done}/{total}  fallbacks {self._fallbacks}  tokens {self._input_tokens}"
        else:
            line = (
                f"tiles {done}/{total}  ${self._spent_usd:.3f}  "
                f"fallbacks {self._fallbacks}  tokens {self._input_tokens}"
            )
            if self._estimated_usd > 0.0005:
                line += f"  est ${self._estimated_usd:.3f}"
        return line

    def _ocr_notes(self) -> list[str]:
        notes: list[str] = []
        for page_id in self.manifest.get("pages") or []:
            try:
                page = self._page(page_id)
            except KeyError:
                continue
            note = str(page.get("ocr_skipped") or "").strip()
            if note and note not in notes:
                notes.append(note)
        return notes

    def _classify_from_vision(self, page: dict[str, Any], devices: list[dict[str, Any]]) -> None:
        info = dict(page.get("classification") or {})
        kind = str(info.get("kind") or "")
        if not page.get("ocr_skipped") and kind not in {"", "other"}:
            return
        guessed = _kind_from_devices(devices)
        if not guessed:
            return
        info["kind"] = guessed
        info["source"] = "vision"
        page["classification"] = info

    def _model_ready(self) -> bool:
        backend = self._backend
        return bool(
            self.backend_name in {"auto", "configured"}
            and getattr(backend, "name", "") == "configured"
            and getattr(backend, "available", lambda: False)()
        )

    def _note_page_begin(self, page_id: str) -> None:
        self._page_started.setdefault(page_id, time.monotonic())

    def _note_page_end(self, page: dict[str, Any]) -> None:
        if "seconds" in page:
            return
        started = self._page_started.get(str(page.get("id") or ""))
        if started is None:
            return
        page["seconds"] = round(max(0.0, time.monotonic() - started), 1)
        self._tick(f"page {page['id']} {page['seconds']:.1f} s")

    def _read_raster(self, page: dict[str, Any]) -> dict[str, Any] | None:
        """Read one scan from pixels. A configured model may refine low-confidence items."""

        from panelcad.importer.raster import prepare_opencv, read_pixels, refine_with_model

        image = Image.open(page["image"]).convert("RGB")
        ink = image_ink_fraction(image)
        prepared, angles = prepare_opencv(image)
        prepared.save(page["image"])
        page["deskew_deg"] = float(angles.get("deskew_deg") or 0.0)
        width_mm = float(page["width_mm"])
        height_mm = float(page["height_mm"])
        standard = str(self.manifest.get("standard") or (page.get("classification") or {}).get("standard") or "")
        vision_on = self._vision_client() is not None
        facts = read_pixels(
            prepared,
            width_mm,
            height_mm,
            _device_catalog(),
            standard=standard,
            allow_missing_ocr=vision_on,
        )
        if facts.get("ocr_skipped"):
            from panelcad.importer.raster import TESSERACT_MESSAGE

            page["ocr_skipped"] = TESSERACT_MESSAGE
        if facts.get("frame_insets"):
            page["frame_insets"] = facts["frame_insets"]
        # A blank page can OCR into a false tag after other tests have touched
        # the engine. No ink means no device, and no model call.
        if ink < _BLANK_INK:
            facts["devices"] = []
            facts["wires"] = []
            facts["xrefs"] = []
            facts["texts"] = []
            facts["title"] = {}
            facts["kind"] = "other"
            facts["confidence"] = 0.0
        elif self._model_ready():
            facts = refine_with_model(
                prepared,
                facts,
                self._backend,
                self.ws.path("pages", page["id"], "refine"),
                width_mm=width_mm,
                height_mm=height_mm,
            )
        title = facts.get("title") if isinstance(facts.get("title"), dict) else {}
        empty = not facts.get("devices") and not any(str(title.get(key) or "").strip() for key in ("company", "project", "title"))
        if empty and ink >= _BLANK_INK and self._model_ready():
            return self._enqueue("classify", page["id"], page, None)
        page["classification"] = {
            **title,
            "kind": facts.get("kind") or "other",
            "confidence": float(facts.get("confidence") or 0.4),
            "source": "ocr",
        }
        self.ws.write_json(
            {key: facts.get(key) or [] for key in ("devices", "wires", "xrefs", "texts")},
            "pages",
            page["id"],
            "raster.json",
        )
        self._save_page(page)
        return None

    def _classify_image(self, page: dict[str, Any]) -> str:
        dest = self.ws.path("pages", page["id"], "classify.png")
        if dest.is_file():
            return str(dest)
        image = Image.open(page["image"])
        image.thumbnail((1000, 1000))
        image.save(dest)
        return str(dest)

    def _add_photo_files(self, files: list[Path]) -> None:
        for path in files:
            index = len(self.manifest.get("photos") or []) + 1
            photo_id = f"photo_{index:03d}"
            dest = self.ws.path("photos", f"{photo_id}.png")
            Image.open(path).convert("RGB").save(dest)
            doc = {"id": photo_id, "source": str(path), "image": str(dest)}
            self.ws.write_json(doc, "photos", f"{photo_id}.json")
            self.manifest.setdefault("photos", []).append(photo_id)

    def _compact_summary(self) -> dict[str, Any]:
        pages = []
        tags: list[str] = []
        for page_id in (self.manifest.get("pages") or [])[:12]:
            page = self._page(page_id)
            info = page.get("classification") or {}
            merged = self.ws.read_json("pages", page_id, "merged.json", default={}) or {}
            pages.append(
                {
                    "id": page_id,
                    "kind": "" if str(info.get("kind") or "").strip().lower() == "other" else (info.get("kind") or ""),
                    "title": info.get("title") or "",
                    "standard": info.get("standard") or "",
                    "seconds": page.get("seconds") or 0,
                    "rotation": page.get("rotation") or 0,
                }
            )
            for device in (merged.get("devices") or [])[:8]:
                tag = str(device.get("tag") or "")
                if tag and tag not in tags and len(tags) < 40:
                    tags.append(tag)
        result = self.manifest.get("result") if isinstance(self.manifest.get("result"), dict) else {}
        tiles_read, tiles_skipped = self._tile_counts()
        return {
            "stage": self.manifest.get("stage"),
            "standard": self.manifest.get("standard") or "",
            "pages": pages,
            "known_tags": tags,
            "tiles_done": int(self._tiles_done),
            "tiles_total": int(self._tiles_total),
            "spent_usd": round(float(self._spent_usd), 4),
            "input_tokens": int(self._input_tokens),
            "estimated_usd": round(float(self._estimated_usd), 4),
            "fallbacks": int(self._fallbacks),
            "fallback_reasons": list(self._fallback_reasons),
            "ocr_skipped": self._ocr_notes(),
            "page_seconds": [
                {"id": row["id"], "seconds": row.get("seconds") or 0, "rotation": row.get("rotation") or 0}
                for row in pages
            ],
            "warning": str(self.manifest.get("warning") or ""),
            "cost_guard": dict(self._cost_guard or self.manifest.get("cost_guard") or {}),
            "devices": int(result.get("devices") or 0),
            "wires": int(result.get("wires") or 0),
            "wire_numbers_read": int(result.get("wire_numbers_read") or 0),
            "wires_connected": int(result.get("wires_connected") or result.get("wires") or 0),
            "numbers_unconnected": int(result.get("numbers_unconnected") or 0),
            "recovery": str(result.get("recovery") or ""),
            "needs_info": int(result.get("needs_info") or 0),
            "unparseable": int(result.get("unparseable") or 0),
            "unparseable_tags": list(result.get("unparseable_tags") or []),
            "tiles_read": tiles_read,
            "tiles_skipped": tiles_skipped,
        }

    def _load_bookkeeping(self) -> None:
        """One cost-guard record for the job.

        Older runs copied that record onto every page. Those workspaces still
        open: the first page copy is hoisted onto the job, and a page that
        still has the field is left alone.
        """

        guard = self.manifest.get("cost_guard")
        if not isinstance(guard, dict) or not guard:
            guard = self._cost_guard_from_pages()
            if guard:
                self.manifest["cost_guard"] = dict(guard)
        if isinstance(guard, dict) and guard:
            self._cost_guard = dict(guard)

    def _cost_guard_from_pages(self) -> dict[str, Any]:
        for page_id in self.manifest.get("pages") or []:
            try:
                page = self._page(page_id)
            except (KeyError, OSError, ValueError, TypeError):
                continue
            found = page.get("cost_guard")
            if isinstance(found, dict) and found:
                return dict(found)
        return {}

    def _store_cost_guard_record(self, info: dict[str, Any]) -> None:
        self._cost_guard = dict(info)
        self.manifest["cost_guard"] = dict(info)

    def _store_cost_guard(self, client: Any, remaining: int, *, continued: bool) -> None:
        spent = float(getattr(client, "spent_usd", 0) or 0)
        settings = getattr(client, "settings", None)
        cap = float(getattr(settings, "max_usd", 0) or 0)
        info = {
            "warned": True,
            "spent_usd": round(spent, 4),
            "max_usd": cap,
            "remaining_tiles": int(max(0, remaining)),
            "reason": "page cost guard",
            "continued": bool(continued),
        }
        self._store_cost_guard_record(info)
        if not self.manifest.get("warning"):
            if continued:
                self.manifest["warning"] = (
                    f"page cost guard: ${spent:.2f} of ${cap:.2f}. "
                    f"Continuing with {info['remaining_tiles']} tiles still unread."
                )
            else:
                self.manifest["warning"] = (
                    f"page cost guard: ${spent:.2f} of ${cap:.2f}. "
                    f"{info['remaining_tiles']} tiles were not sent. Continue to read them."
                )

    def _remember_result(self, session: Session, report: dict[str, Any]) -> None:
        """Counts for the summary. Devices and wires are what the saved drawing holds.

        The build report can list the same contactor-to-overload connection more
        than once. The drawing keeps one conductor, so the summary follows the
        project, not that list.
        """

        skipped = [row for row in (report.get("skipped") or []) if isinstance(row, dict)]

        def tags(reason: str) -> list[str]:
            found: list[str] = []
            for row in skipped:
                if str(row.get("reason") or "") != reason:
                    continue
                tag = str(row.get("tag") or "").strip()
                if tag and tag not in found:
                    found.append(tag)
            return found

        devices = 0
        wires = 0
        for sheet in session.project.sheets:
            devices += len(sheet.components)
            wires += len(sheet.wires)
        unparsed = tags("tag_unparsed")
        # The same tag on two sheets is two marks. Unique skipped tags under-count.
        from panelcad.reconstruct.placeholder import is_needs_info_component

        needs_marks = 0
        for sheet in session.project.sheets:
            for comp in sheet.components:
                if is_needs_info_component(comp):
                    needs_marks += 1
        connected_numbers: list[str] = []
        for row in report.get("wires") or []:
            if not isinstance(row, dict):
                continue
            number = str(row.get("number") or "").strip()
            if number and number not in connected_numbers:
                connected_numbers.append(number)
        unconnected: list[str] = []
        for row in report.get("unresolved_wires") or []:
            if not isinstance(row, dict):
                continue
            number = str(row.get("number") or "").strip()
            if number and number not in connected_numbers and number not in unconnected:
                unconnected.append(number)
        numbers_read = len(connected_numbers) + len(unconnected)
        recovery = describe_wire_recovery(
            devices=devices,
            numbers_read=numbers_read,
            connected=wires,
            unconnected=len(unconnected),
        )
        self.manifest["result"] = {
            "devices": devices,
            "wires": wires,
            "needs_info": needs_marks,
            "unparseable": len(unparsed),
            "unparseable_tags": unparsed,
            "wire_numbers_read": numbers_read,
            "wires_connected": wires,
            "numbers_unconnected": len(unconnected),
            "recovery": recovery,
        }

    def _tile_counts(self) -> tuple[int, int]:
        """Tiles that were read, and tiles that were not sent.

        A fallback (the model was asked and failed) is neither. Blank cells,
        the title band, the page cost guard, and a cancel are skipped.
        """

        read = 0
        skipped = 0
        for page_id in self.manifest.get("pages") or []:
            folder = self.ws.root / "pages" / str(page_id) / "tiles"
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("tile_*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("skipped") or self._unsent_cost_guard_tile(data):
                    skipped += 1
                    continue
                reason = ""
                body = data.get("read") if isinstance(data.get("read"), dict) else {}
                if isinstance(body, dict):
                    reason = str(body.get("error") or "")
                if reason in {"page cost guard", "cancelled"}:
                    skipped += 1
                    continue
                if reason:
                    continue
                read += 1
        return read, skipped

    def _unsent_cost_guard_tile(self, data: dict[str, Any]) -> bool:
        """A tile from before read.error was stored, left unsent by the cost guard.

        Those files have no ``read`` body. Pixel fallbacks are stamped
        ``vision_fallback``; a real vector slice is not, so it still counts
        as read.
        """

        guard = self._cost_guard or self.manifest.get("cost_guard") or {}
        if not isinstance(guard, dict) or guard.get("continued"):
            return False
        try:
            remaining = int(guard.get("remaining_tiles") or 0)
        except (TypeError, ValueError):
            return False
        if remaining <= 0:
            return False
        if isinstance(data.get("read"), dict):
            return False
        rows = []
        for key in ("devices", "wires", "xrefs", "texts"):
            rows.extend(item for item in (data.get(key) or []) if isinstance(item, dict))
        if not rows:
            return True
        return all(bool(item.get("vision_fallback")) for item in rows)

    def _page(self, page_id: str) -> dict[str, Any]:
        data = self.ws.read_json("pages", page_id, "page.json")
        if not isinstance(data, dict):
            raise KeyError(page_id)
        return data

    def _save_page(self, page: dict[str, Any]) -> None:
        self.ws.write_json(page, "pages", page["id"], "page.json")

    def _save(self) -> None:
        self.ws.save_manifest(self.manifest)


_POWER_WORDS = ("breaker", "mpcb", "contactor", "overload", "motor")
_CONTROL_WORDS = ("plc", "hmi", "pushbutton", "estop", "pilot", "power_supply", "psu")


def _kind_from_devices(devices: list[dict[str, Any]]) -> str:
    """A page kind from the symbols the model named, when OCR did not classify it."""

    blob = " ".join(
        f"{device.get('symbol') or ''} {device.get('type') or ''} {device.get('library_id') or ''}".lower()
        for device in devices
    )
    if any(word in blob for word in _POWER_WORDS):
        return "schematic_power"
    if any(word in blob for word in _CONTROL_WORDS):
        return "schematic_control"
    return ""


def _devices_for_trace(vision_devices: list[dict[str, Any]], pixel_devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Vision devices, plus pixel devices whose tags the model did not return."""

    tags = {str(device.get("tag") or "") for device in vision_devices}
    extra = [
        device
        for device in pixel_devices
        if device.get("tag") and str(device.get("tag")) not in tags and device.get("library_id")
    ]
    return list(vision_devices) + extra


def _vision_settings(backend: str, model: str | None, vision: VisionSettings | None) -> VisionSettings:
    if vision is not None:
        settings = vision.resolved()
        if model and not settings.model:
            settings.model = model
        return settings.resolved()
    return VisionSettings(preset=backend, model=model or "").resolved()


def fallback_note(devices: list[dict[str, Any]], reason: str) -> str:
    """Say when the model itself returned no device.

    A pixel symbol kept as a fallback is not a model read. Prefix guesses are
    not devices either.
    """

    confirmed = [
        device
        for device in devices
        if device.get("tag")
        and not device.get("vision_fallback")
        and not device.get("unconfirmed")
        and not device.get("endpoint")
    ]
    if confirmed:
        return f"vision fallback: {reason}"
    return f"the model read nothing. vision fallback: {reason}"


def _image_info(path: str) -> dict[str, Any]:
    """Pixel size and file DPI for one crop. Missing files stay zero."""

    info: dict[str, Any] = {"path": path, "width_px": 0, "height_px": 0, "dpi": None}
    file = Path(path) if path else None
    if file is None or not file.is_file():
        return info
    try:
        with Image.open(file) as handle:
            info["width_px"] = int(handle.size[0])
            info["height_px"] = int(handle.size[1])
            raw = (getattr(handle, "info", {}) or {}).get("dpi")
    except Exception:  # noqa: BLE001 — a bad crop still logs the path
        return info
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        info["dpi"] = float(raw)
    elif isinstance(raw, (tuple, list)) and raw:
        try:
            info["dpi"] = float(raw[0])
        except (TypeError, ValueError):
            info["dpi"] = None
    return info


def _detection_drops(
    reported: list[Any],
    violations: list[dict[str, Any]],
    filtered: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Why a row did not become a placed device or wire. An empty read stays empty."""

    drops: list[dict[str, Any]] = []
    for violation in violations:
        if not isinstance(violation, dict):
            continue
        detail = str(violation.get("reason") or "")
        drops.append(
            {
                "reason": "schema_incomplete" if detail == "schema_incomplete" else "parse_fail",
                "rule": str(violation.get("rule") or ""),
                "detail": detail,
                "index": violation.get("index"),
            }
        )
    for row in filtered:
        if not isinstance(row, dict):
            continue
        reasons: list[str] = []
        if row.get("reject_rule"):
            reasons.append(str(row["reject_rule"]))
        if row.get("unlocalised"):
            reasons.append("unlocalised")
        if row.get("wrong_field"):
            reasons.append("wrong_field")
        if row.get("tag_unparsed"):
            reasons.append("tag_unparsed")
        if not reasons:
            continue
        drops.append(
            {
                "tag": str(row.get("tag") or ""),
                "wire_number": str(row.get("wire_number") or ""),
                "bus": str(row.get("bus") or ""),
                "reasons": reasons,
            }
        )
    if not reported and not filtered and not drops:
        drops.append({"reason": "empty"})
    return drops


def _tile_read_body(
    page: dict[str, Any],
    tile: dict[str, Any],
    trace: Any,
    *,
    error: str,
    reported: list[Any],
    parsed: list[Any],
    drops: list[dict[str, Any]],
) -> dict[str, Any]:
    """One tile's prompt box, crop size, raw reply, parsed rows, and drop reasons."""

    crop = str(tile.get("crop") or "")
    image = _image_info(crop)
    page_dpi = page.get("dpi")
    if image.get("dpi") is None and isinstance(page_dpi, (int, float)) and not isinstance(page_dpi, bool):
        image["dpi"] = float(page_dpi)
    raw = ""
    prompt = ""
    if isinstance(trace, dict):
        raw = str(trace.get("reply") or "")
        request = trace.get("request") if isinstance(trace.get("request"), dict) else {}
        prompt = str(request.get("prompt") or "")
    box = {
        "x": float(tile.get("x") or 0),
        "y": float(tile.get("y") or 0),
        "w": float(tile.get("w") or 0),
        "h": float(tile.get("h") or 0),
    }
    return {
        "tile_box": box,
        "image": image,
        "page_dpi": image.get("dpi"),
        "rotation": int(page.get("rotation") or 0),
        "prompt": prompt,
        "raw_text": raw,
        "parsed": [row for row in parsed if isinstance(row, dict)],
        "reported": [row for row in reported if isinstance(row, dict)],
        "drops": drops,
        "error": error,
        "retry": dict(trace.get("retry") or {}) if isinstance(trace, dict) and isinstance(trace.get("retry"), dict) else {},
    }


def _fallback_slice(pixel: dict[str, Any], tile: dict[str, Any], reason: str) -> dict[str, Any]:
    piece = slice_facts(pixel, tile)
    out: dict[str, Any] = {}
    for key, group in piece.items():
        copied_group = []
        for item in group:
            if key == "devices" and _prefix_guess(item):
                continue
            copied = dict(item)
            copied["vision_fallback"] = reason
            copied["page_id"] = tile.get("page_id") or copied.get("page_id") or ""
            copied["tile_x"] = float(tile.get("x") or 0)
            copied["tile_y"] = float(tile.get("y") or 0)
            copied["tile_w"] = float(tile.get("w") or 0)
            copied["tile_h"] = float(tile.get("h") or 0)
            stamp_review_crop(copied, tile, str(tile.get("crop") or ""))
            copied_group.append(copied)
        out[key] = copied_group
    return out


def _prefix_guess(item: dict[str, Any]) -> bool:
    """An OCR token whose template missed. It must not become a placed part."""

    if item.get("unconfirmed"):
        return True
    if item.get("guess") and str(item.get("source") or "") != "symbol":
        return True
    return False


def _fallback_summary(failures: list[str]) -> str:
    reasons: dict[str, list[str]] = {}
    for item in failures:
        tile, _, reason = item.partition(" ")
        reasons.setdefault(reason or "vision request failed", []).append(tile)
    parts = []
    for reason, tiles in reasons.items():
        shown = ", ".join(tiles[:8])
        extra = f" +{len(tiles) - 8}" if len(tiles) > 8 else ""
        parts.append(f"{reason} ({shown}{extra})")
    return "; ".join(parts)


def _safe_id(work_id: str) -> str:
    return work_id.replace(":", "__").replace("/", "_")


def _image_files(path: Path | str) -> list[Path]:
    folder = Path(path)
    if folder.is_file():
        return [folder]
    files = [item for item in sorted(folder.iterdir()) if item.suffix.lower() in RASTER_EXTS]
    return files


_CATALOG: Any = None


def _device_catalog() -> Any:
    global _CATALOG
    if _CATALOG is None:
        from panelcad.library.catalog import default_catalog

        _CATALOG = default_catalog()
    return _CATALOG


def _parts_catalog() -> PartsCatalog:
    from panelcad.library.parts import default_parts_catalog

    return default_parts_catalog()


def _tags_by_kind(stitched: dict[str, Any]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for sheet in stitched.get("sheets") or []:
        kind = str(sheet.get("page_kind") or sheet.get("kind") or "")
        found.setdefault(kind, set())
        for device in sheet.get("devices") or []:
            if device.get("tag"):
                found[kind].add(str(device["tag"]))
    return found


def looks_like_print(paths: list[Path]) -> bool:
    return any(is_print_path(path) for path in paths)
