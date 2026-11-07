"""Demo-print scan ladder: degrade, import, and score.

Sources are the repo's own examples (no customer drawings). Each sheet is
rendered, then saved as a clean scan and as shop-floor degradations. Scoring
runs ``ImportJob`` with the raster backend. A value the reader could not see
is ``?`` and is not counted as a recovered tag or wire number.

    python scripts/scan_ladder.py generate
    python scripts/scan_ladder.py score --label before
    python scripts/scan_ladder.py report
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "scan_ladder"
RUN_ROOT = Path(tempfile.gettempdir()) / "scan-ladder-runs"

SOURCES: tuple[dict[str, str], ...] = (
    {
        "id": "schematic",
        "project": "examples/scan_ladder_demo.pcad.yaml",
        "sheet": "elementary",
        "kind": "schematic",
    },
    {
        "id": "panel",
        "project": "examples/scan_ladder_demo.pcad.yaml",
        "sheet": "backplate",
        "kind": "panel_layout",
    },
    {
        "id": "terminal",
        "project": "examples/scan_ladder_demo.pcad.yaml",
        "sheet": "strip",
        "kind": "terminal",
    },
)

# dpi is the render used as the source of that variant. phone keeps a 72 DPI tag.
VARIANTS: tuple[dict[str, Any], ...] = (
    {"id": "clean_300", "dpi": 300, "kind": "clean"},
    {"id": "dpi_200", "dpi": 200, "kind": "clean"},
    {"id": "dpi_150", "dpi": 150, "kind": "clean"},
    {"id": "dpi_100", "dpi": 100, "kind": "clean"},
    {"id": "jpeg", "dpi": 200, "kind": "jpeg", "quality": 30},
    {"id": "skew", "dpi": 200, "kind": "skew"},
    {"id": "phone", "dpi": 200, "kind": "phone"},
    {"id": "faded", "dpi": 200, "kind": "faded"},
    {"id": "speckle", "dpi": 150, "kind": "speckle"},
    {"id": "fax", "dpi": 200, "kind": "fax"},
)

SKEW_DEG = {"schematic": 1.6, "panel": -2.2, "terminal": 2.8}


def _qt() -> None:
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])


def render_sheet(project: Path, sheet_id: str, dest_dir: Path, dpi: int) -> Path:
    """One PDF page of a demo sheet, rasterized at ``dpi``."""

    from panelcad.api.session import Session
    from panelcad.importer.ingest import rasterize_pdf

    _qt()
    dest_dir.mkdir(parents=True, exist_ok=True)
    pdf = dest_dir / f"{sheet_id}.pdf"
    session = Session.open(project)
    session.export_pdf(pdf, sheet_id=sheet_id)
    pages = rasterize_pdf(pdf, dest_dir / f"r{dpi}", dpi=dpi)
    if len(pages) != 1:
        raise RuntimeError(f"{project.name} {sheet_id} rendered {len(pages)} pages")
    return pages[0]


def degrade(image: Any, source_id: str, variant: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Return the degraded image and the save options (format, dpi, quality)."""

    import numpy as np
    from PIL import Image, ImageFilter

    image = image.convert("RGB")
    kind = variant["kind"]
    dpi = int(variant["dpi"])
    save: dict[str, Any] = {"format": "PNG", "dpi": (dpi, dpi), "optimize": True, "compress_level": 9}
    if kind == "clean":
        return image, save
    if kind == "jpeg":
        buf = BytesIO()
        image.save(buf, format="JPEG", quality=int(variant.get("quality") or 30))
        buf.seek(0)
        out = Image.open(buf).convert("RGB")
        save = {"format": "JPEG", "dpi": (dpi, dpi), "quality": int(variant.get("quality") or 30), "optimize": True}
        return out, save
    if kind == "skew":
        angle = float(SKEW_DEG[source_id])
        out = image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(232, 232, 228))
        return out, save
    if kind == "faded":
        arr = np.asarray(image).astype(np.float32)
        faded = np.clip(arr * 0.42 + 148.0, 0, 255).astype(np.uint8)
        return Image.fromarray(faded), save
    if kind == "speckle":
        return _speckle(image), save
    if kind == "fax":
        gray = np.asarray(image.convert("L"))
        bw = np.where(gray < 160, 0, 255).astype(np.uint8)
        return Image.fromarray(bw), save
    if kind == "phone":
        return _phone(image), {"format": "JPEG", "dpi": (72, 72), "quality": 72, "optimize": True}
    raise ValueError(kind)


def _speckle(image: Any) -> Any:
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(11)
    arr = np.asarray(image).copy()
    height, width = arr.shape[:2]
    count = max(80, int(width * height * 0.0015))
    ys = rng.integers(0, height, size=count)
    xs = rng.integers(0, width, size=count)
    salt = rng.random(count) > 0.45
    arr[ys[salt], xs[salt]] = 255
    arr[ys[~salt], xs[~salt]] = 0
    # A few 2×2 toner clumps, the way a dusty platen prints.
    for y, x in zip(ys[: count // 8], xs[: count // 8]):
        arr[y : y + 2, x : x + 2] = 20
    return Image.fromarray(arr)


def _phone(image: Any) -> Any:
    """Mild keystone on a desk, with a lighting gradient. Tagged 72 DPI by the caller."""

    import cv2
    import numpy as np
    from PIL import Image

    rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    ys = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    xs = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :]
    light = (1.0 - 0.22 * xs - 0.16 * ys)[:, :, None]
    lit = np.clip(rgb.astype(np.float32) * light, 0, 255).astype(np.uint8)
    canvas_w, canvas_h = int(width * 1.18), int(height * 1.22)
    quad = np.array(
        [
            [canvas_w * 0.08, canvas_h * 0.10],
            [canvas_w * 0.93, canvas_h * 0.06],
            [canvas_w * 0.96, canvas_h * 0.90],
            [canvas_w * 0.05, canvas_h * 0.94],
        ],
        dtype=np.float32,
    )
    src = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(src, quad)
    desk = np.full((canvas_h, canvas_w, 3), 58, dtype=np.uint8)
    warped = cv2.warpPerspective(lit, matrix, (canvas_w, canvas_h), borderValue=(58, 58, 58))
    mask = cv2.warpPerspective(np.full((height, width), 255, np.uint8), matrix, (canvas_w, canvas_h))
    canvas = np.where(mask[:, :, None] > 127, warped, desk)
    return Image.fromarray(canvas)


def _save(image: Any, path: Path, options: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, **options)


def sheet_truth(session: Any, source: dict[str, str]) -> dict[str, Any]:
    """Devices, wire numbers, and connection pairs printed on one demo sheet."""

    project = session.project
    sheet = project.sheet(source["sheet"])
    devices: list[str] = []
    offpage: list[str] = []
    if source["kind"] == "terminal":
        from panelcad.core.terminal_strip import layout_terminal_strip

        laid = layout_terminal_strip(project, session.catalog, str(sheet.strip or "X1"))
        devices = [str(item["ref"]) for item in laid.get("items") or [] if item.get("kind") == "terminal"]
        numbers = []
        for item in laid.get("items") or []:
            for key in ("internal", "external"):
                text = str(item.get(key) or "").strip()
                if text and text not in numbers:
                    numbers.append(text)
        edges: list[str] = []
    else:
        numbers = []
        edges = []
        for comp in sheet.components:
            lib = str(comp.library_id or "")
            if "offpage" in lib:
                offpage.append(comp.id)
            else:
                devices.append(comp.id)
        for wire in sheet.wires:
            number = str(wire.number or "").strip()
            if number and number not in numbers:
                numbers.append(number)
            pair = _pair(str(wire.frm), str(wire.to))
            if pair and pair not in edges:
                edges.append(pair)
    tb = project.title_block
    texts = [tb.company, tb.customer, tb.location, tb.drawing_number, tb.drawn_by, project.name, sheet.title]
    texts = [str(item).strip() for item in texts if str(item or "").strip()]
    width, height = sheet.page.dimensions()
    return {
        "id": source["id"],
        "project": source["project"],
        "sheet": source["sheet"],
        "kind": source["kind"],
        "standard": str(tb.standard or ""),
        "drawing_style": str(tb.drawing_style or ""),
        "page_mm": [round(width, 3), round(height, 3)],
        "page_size": sheet.page.size,
        "devices": devices,
        "offpage": offpage,
        "wire_numbers": numbers,
        "edges": edges,
        "texts": texts,
    }


def _pair(left: str, right: str) -> str:
    a = left.strip().upper()
    b = right.strip().upper()
    if "." not in a or "." not in b:
        return ""
    return " ".join(sorted((a, b)))


def generate() -> None:
    masters: dict[tuple[str, int], Any] = {}
    from PIL import Image

    from panelcad.api.session import Session

    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="scan-ladder-render-") as tmp:
        render_dir = Path(tmp)
        for source in SOURCES:
            session = Session.open(ROOT / source["project"])
            truth = sheet_truth(session, source)
            path = FIXTURE_ROOT / "truth" / f"{source['id']}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
            for dpi in sorted({int(item["dpi"]) for item in VARIANTS}):
                raster = render_sheet(ROOT / source["project"], source["sheet"], render_dir / source["id"], dpi)
                masters[(source["id"], dpi)] = Image.open(raster).convert("RGB")
            for variant in VARIANTS:
                image, options = degrade(masters[(source["id"], int(variant["dpi"]))], source["id"], variant)
                suffix = ".jpg" if options["format"] == "JPEG" else ".png"
                dest = FIXTURE_ROOT / source["id"] / f"{variant['id']}{suffix}"
                _save(image, dest, options)
                print(f"wrote {dest.relative_to(ROOT)} {dest.stat().st_size}")
    manifest = {"sources": [item["id"] for item in SOURCES], "variants": [item["id"] for item in VARIANTS]}
    (FIXTURE_ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _recall(wanted: set[str], found: set[str]) -> float | None:
    if not wanted:
        return None
    return len(wanted & found) / len(wanted)


def _near(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 1:
        return False
    mismatches = 0
    i = j = 0
    a, b = left.upper(), right.upper()
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        mismatches += 1
        if mismatches > 1:
            return False
        if len(a) - i > len(b) - j:
            i += 1
        elif len(b) - j > len(a) - i:
            j += 1
        else:
            i += 1
            j += 1
    return mismatches + (len(a) - i) + (len(b) - j) <= 1


def probe_vision() -> tuple[Any, str]:
    """The vision backend this machine can actually call, or a reason it cannot.

    No scores are invented when nothing here can see the page.
    """

    import shutil
    import urllib.error
    import urllib.request

    from panelcad.importer.vision_config import VisionSettings

    if os.environ.get("XAI_API_KEY", "").strip():
        return VisionSettings(preset="grok"), ""
    if shutil.which("grok"):
        return VisionSettings(preset="grok-build"), ""
    if shutil.which("ollama"):
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as response:
                if response.status == 200:
                    return VisionSettings(preset="ollama"), ""
        except (OSError, urllib.error.URLError, TimeoutError):
            pass
        return None, "Ollama is installed but it is not running, so vision was not scored."
    return (
        None,
        "No vision backend can run on this machine. XAI_API_KEY is unset, "
        "Ollama is not installed, and the Grok CLI is not installed.",
    )


def score_variant(source_id: str, variant_id: str, *, workspace: Path, mode: str = "ocr") -> dict[str, Any]:
    from PIL import Image

    from panelcad.core.ansi_drafting import ansi_drafting_issues
    from panelcad.importer.pipeline import EmptyImport, ImportJob

    truth = json.loads((FIXTURE_ROOT / "truth" / f"{source_id}.json").read_text(encoding="utf-8"))
    image_path = _fixture_path(source_id, variant_id)
    row: dict[str, Any] = {
        "source": source_id,
        "variant": variant_id,
        "image": str(image_path.relative_to(ROOT)),
        "bytes": image_path.stat().st_size,
        "mode": mode,
    }
    vision = None
    if mode == "vision":
        vision, reason = probe_vision()
        if vision is None:
            row.update(_zero(reason))
            row["vision_available"] = False
            # Not a measured zero. The model was not called.
            row["devices"] = None
            row["wire_numbers"] = None
            row["text"] = None
            row["connectivity_recall"] = None
            row["connectivity_precision"] = None
            return row
    try:
        if vision is None:
            job = ImportJob.start([image_path], workspace, backend="raster")
        else:
            job = ImportJob.start([image_path], workspace, vision=vision)
        # A schematic scan is issued by the ladder engine. Panel and terminal
        # sheets keep the scan's own placement.
        layout = "ladder" if truth.get("kind") == "schematic" else "preserve"
        built = job.build(layout_mode=layout, target_standard=str(truth.get("standard") or "auto"))
    except EmptyImport as exc:
        row.update(_zero(str(exc)))
        return row
    except Exception as exc:  # noqa: BLE001 — the ladder records a failed import, it does not hide it
        row.update(_zero(f"{type(exc).__name__}: {exc}"))
        return row
    page = job._page("page_001")
    raster = job.ws.read_json("pages", "page_001", "raster.json", default={}) or {}
    built_tags = {
        comp.id
        for sheet in built.project.sheets
        for comp in sheet.components
        if "offpage" not in str(comp.library_id or "")
    }
    built_numbers = {
        str(wire.number).strip()
        for sheet in built.project.sheets
        for wire in sheet.wires
        if str(wire.number or "").strip()
    }
    for sheet in built.project.sheets:
        for comp in sheet.components:
            props = comp.properties or {}
            for key in ("wire_top", "wire_bottom"):
                value = str(props.get(key) or "").strip()
                if value.isdigit():
                    built_numbers.add(value)
    built_edges = set()
    for sheet in built.project.sheets:
        if sheet.kind in {"panel_layout", "door_layout", "terminal"}:
            continue
        for wire in sheet.wires:
            pair = _pair(str(wire.frm), str(wire.to))
            if pair:
                built_edges.add(pair)
    truth_tags = set(truth["devices"])
    offpage = set(truth.get("offpage") or [])
    truth_numbers = set(truth["wire_numbers"])
    truth_edges = set(truth["edges"])
    unreadable = _unreadable(raster, built)
    concrete_tags = {tag for tag in built_tags if tag != "?" and not str(tag).startswith("?")}
    concrete_numbers = {num for num in built_numbers if num != "?" and not str(num).startswith("?")}
    guesses = sorted(
        tag
        for tag in concrete_tags - truth_tags - offpage
        if any(_near(tag, wanted) for wanted in truth_tags)
    )
    extras = sorted(concrete_tags - truth_tags - set(guesses))
    title_bits = _title_bits(built)
    token_text = {str(item.get("text") or "").strip() for item in (raster.get("texts") or []) if isinstance(item, dict)}
    token_text |= {str(item.get("tag") or "").strip() for item in (raster.get("devices") or []) if isinstance(item, dict)}
    token_text |= {str(item.get("number") or "").strip() for item in (raster.get("wires") or []) if isinstance(item, dict)}
    found_text = {item.upper() for item in concrete_tags | concrete_numbers | title_bits | token_text if item}
    text_hits = [item for item in truth["texts"] if item.upper() in found_text or any(item.upper() in bit.upper() for bit in title_bits)]
    prepare = page.get("prepare") if isinstance(page.get("prepare"), dict) else {}
    ansi_issues: list[str] = []
    if truth.get("standard") == "NEMA/ANSI":
        ansi_issues = ansi_drafting_issues(built.project, built.catalog)
    row.update(
        {
            "error": "",
            "devices": _recall(truth_tags, concrete_tags),
            "wire_numbers": _recall(truth_numbers, concrete_numbers),
            "text": _recall(set(truth["texts"]), set(text_hits)) if truth["texts"] else None,
            "connectivity_recall": _recall(truth_edges, built_edges),
            "connectivity_precision": (len(truth_edges & built_edges) / len(built_edges)) if built_edges else (None if truth_edges else None),
            "truth_devices": len(truth_tags),
            "found_devices": len(truth_tags & concrete_tags),
            "truth_wires": len(truth_numbers),
            "found_wires": len(truth_numbers & concrete_numbers),
            "truth_edges": len(truth_edges),
            "found_edges": len(truth_edges & built_edges),
            "guessed": guesses,
            "extra_tags": extras,
            "unreadable": unreadable,
            "missing_tags": sorted(truth_tags - concrete_tags),
            "missing_wires": sorted(truth_numbers - concrete_numbers),
            "standard": str(built.project.title_block.standard or ""),
            "drawing_style": str(built.project.title_block.drawing_style or ""),
            "deskew_deg": prepare.get("deskew_deg", page.get("deskew_deg")),
            "rectified": bool(prepare.get("rectified", page.get("rectified"))),
            "illumination": bool(prepare.get("illumination_normalized", page.get("illumination_normalized"))),
            "ansi_issues": ansi_issues,
            "warning": str((job.manifest or {}).get("warning") or ""),
        }
    )
    compare = _compare(image_path, built, workspace / "compare.png")
    row["compare"] = str(compare) if compare else ""
    return row


def _fixture_path(source_id: str, variant_id: str) -> Path:
    folder = FIXTURE_ROOT / source_id
    for suffix in (".png", ".jpg"):
        path = folder / f"{variant_id}{suffix}"
        if path.is_file():
            return path
    raise FileNotFoundError(f"{source_id}/{variant_id}")


def _zero(error: str) -> dict[str, Any]:
    return {
        "error": error,
        "devices": 0.0,
        "wire_numbers": 0.0,
        "text": 0.0,
        "connectivity_recall": 0.0,
        "connectivity_precision": None,
        "guessed": [],
        "extra_tags": [],
        "unreadable": 0,
        "missing_tags": [],
        "missing_wires": [],
        "ansi_issues": [],
        "warning": error,
        "compare": "",
    }


def _unreadable(raster: dict[str, Any], built: Any) -> int:
    count = 0
    for group in ("devices", "wires", "texts"):
        for item in raster.get(group) or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("tag") or item.get("number") or item.get("text") or "")
            if item.get("unreadable") or text == "?" or text.startswith("?"):
                count += 1
    for sheet in built.project.sheets:
        for wire in sheet.wires:
            if wire.inferred or str(wire.drawn_mark(ladder=True)).startswith("?"):
                count += 1
    return count


def _title_bits(built: Any) -> set[str]:
    tb = built.project.title_block
    bits = {
        tb.company,
        tb.customer,
        tb.location,
        tb.drawing_number,
        tb.drawn_by,
        tb.standard,
        built.project.name,
    }
    for sheet in built.project.sheets:
        bits.add(sheet.title)
    return {str(item).strip() for item in bits if str(item or "").strip()}


def _compare(scan: Path, built: Any, dest: Path) -> Path | None:
    try:
        from PIL import Image, ImageDraw

        from panelcad.importer.preview import sheet_frame_index
    except Exception:
        return None
    _qt()
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter

    from panelcad.render.page import paint_sheet

    sheet = built.project.sheets[0]
    width_mm, height_mm = sheet.page.dimensions()
    scale = 90 / 25.4
    image = QImage(max(1, int(width_mm * scale)), max(1, int(height_mm * scale)), QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    painter.scale(scale, scale)
    index, count = sheet_frame_index(built.project, sheet)
    paint_sheet(painter, built.project, sheet, built.catalog, sheet_index=index, sheet_count=count)
    painter.end()
    raw = dest.with_name("rebuilt.png")
    raw.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(raw))
    left = Image.open(scan).convert("RGB")
    right = Image.open(raw).convert("RGB")
    height = 900

    def fit(picture: Any) -> Any:
        width = max(1, int(picture.width * height / picture.height))
        return picture.resize((width, height), Image.Resampling.LANCZOS)

    left, right = fit(left), fit(right)
    overlay = _overlay(left, right)
    gap = 12
    label_h = 36
    canvas = Image.new("RGB", (left.width + right.width + overlay.width + gap * 2, height + label_h), (248, 246, 240))
    canvas.paste(left, (0, label_h))
    canvas.paste(right, (left.width + gap, label_h))
    canvas.paste(overlay, (left.width + right.width + gap * 2, label_h))
    draw = ImageDraw.Draw(canvas)
    draw.text((8, 8), "input", fill=(20, 20, 20))
    draw.text((left.width + gap + 8, 8), "recreated", fill=(20, 20, 20))
    draw.text((left.width + right.width + gap * 2 + 8, 8), "diff", fill=(20, 20, 20))
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest, optimize=True)
    return dest


def _overlay(left: Any, right: Any) -> Any:
    import numpy as np
    from PIL import Image

    if right.size != left.size:
        right = right.resize(left.size, Image.Resampling.LANCZOS)
    a = np.asarray(left.convert("L"))
    b = np.asarray(right.convert("L"))
    ink_a = a < 180
    ink_b = b < 180
    out = np.full((*a.shape, 3), 255, np.uint8)
    both = ink_a & ink_b
    out[both] = (20, 20, 20)
    out[ink_a & ~ink_b] = (200, 40, 40)
    out[~ink_a & ink_b] = (30, 70, 180)
    return Image.fromarray(out)


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value * 100:.1f}%"
    return str(value)


def score(label: str, *, only: str = "", variant: str = "", mode: str = "ocr") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    out_dir = RUN_ROOT / label
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    for source in SOURCES:
        if only and source["id"] != only:
            continue
        for item in VARIANTS:
            if variant and item["id"] != variant:
                continue
            print(f"score {label} {mode} {source['id']} {item['id']}", flush=True)
            row = score_variant(
                source["id"],
                item["id"],
                workspace=out_dir / source["id"] / item["id"],
                mode=mode,
            )
            rows.append(row)
            brief = {key: row.get(key) for key in ("devices", "wire_numbers", "text", "connectivity_recall", "error", "ansi_issues")}
            print(" ", brief, flush=True)
    dest = FIXTURE_ROOT / f"scores-{label}.json"
    dest.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return rows


def _mean(row: dict[str, Any]) -> float:
    vals = [row.get(key) for key in ("devices", "wire_numbers", "text", "connectivity_recall")]
    nums = [float(item) for item in vals if isinstance(item, (int, float))]
    return sum(nums) / len(nums) if nums else 0.0


def report() -> None:
    before_path = FIXTURE_ROOT / "scores-ocr-prev.json"
    if not before_path.is_file():
        before_path = FIXTURE_ROOT / "scores-before.json"
    after_path = FIXTURE_ROOT / "scores-after.json"
    vision_path = FIXTURE_ROOT / "scores-vision.json"
    before = json.loads(before_path.read_text(encoding="utf-8")) if before_path.is_file() else []
    after = json.loads(after_path.read_text(encoding="utf-8")) if after_path.is_file() else []
    vision_rows = json.loads(vision_path.read_text(encoding="utf-8")) if vision_path.is_file() else []
    if isinstance(vision_rows, dict):
        vision_rows = list(vision_rows.get("rows") or [])
    keys = {(row["source"], row["variant"]): row for row in before}
    vision_keys = {(row["source"], row["variant"]): row for row in vision_rows}
    vision_note = ""
    for row in vision_rows:
        if row.get("vision_available") is False and row.get("error"):
            vision_note = str(row["error"])
            break
    lines = [
        "# Scan ladder",
        "",
        "Demo prints only. Scores are recall of the printed devices, wire numbers, title text, and pin-to-pin connections. A `?` is an unreadable mark, not a match and not a guess.",
        "",
        "OCR before is the previous artwork (seal-in, DOL enclosure, vendor strip). OCR after is this pass on the drafter-style sheets. The two columns are different drawings. Vision is the existing vision-model import when a backend can run.",
        "",
    ]
    if vision_note:
        lines.extend([f"Vision: {vision_note}", ""])
    lines.extend(
        [
            "| source | variant | devices OCR before | devices OCR after | wires OCR before | wires OCR after | conn OCR before | conn OCR after | devices vision | wires vision | conn vision | ANSI after |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    csv_rows = []
    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in after or before:
        other = keys.get((row["source"], row["variant"]), {})
        left = other or row
        right = row if after else {}
        seen = vision_keys.get((row["source"], row["variant"]), {})
        vision_devices = "unavailable" if seen.get("vision_available") is False else _fmt(seen.get("devices"))
        vision_wires = "unavailable" if seen.get("vision_available") is False else _fmt(seen.get("wire_numbers"))
        vision_conn = "unavailable" if seen.get("vision_available") is False else _fmt(seen.get("connectivity_recall"))
        lines.append(
            "| {source} | {variant} | {db} | {da} | {wb} | {wa} | {cb} | {ca} | {vd} | {vw} | {vc} | {ansi} |".format(
                source=row["source"],
                variant=row["variant"],
                db=_fmt(left.get("devices")),
                da=_fmt(right.get("devices")) if right else "",
                wb=_fmt(left.get("wire_numbers")),
                wa=_fmt(right.get("wire_numbers")) if right else "",
                cb=_fmt(left.get("connectivity_recall")),
                ca=_fmt(right.get("connectivity_recall")) if right else "",
                vd=vision_devices,
                vw=vision_wires,
                vc=vision_conn,
                ansi=len((right or left).get("ansi_issues") or []),
            )
        )
        csv_rows.append(
            {
                "source": row["source"],
                "variant": row["variant"],
                "devices_ocr_before": left.get("devices"),
                "devices_ocr_after": right.get("devices") if right else "",
                "wires_ocr_before": left.get("wire_numbers"),
                "wires_ocr_after": right.get("wire_numbers") if right else "",
                "connectivity_ocr_before": left.get("connectivity_recall"),
                "connectivity_ocr_after": right.get("connectivity_recall") if right else "",
                "devices_vision": vision_devices,
                "wires_vision": vision_wires,
                "connectivity_vision": vision_conn,
                "ansi_issues_after": len((right or left).get("ansi_issues") or []),
                "error": (right or left).get("error") or seen.get("error") or "",
            }
        )
        ranked.append((_mean(right or left), right or left))
    lines.append("")
    fails = []
    for row in after or before:
        if row.get("error"):
            fails.append(f"- {row['source']} {row['variant']}: {row['error']}")
        for issue in (row.get("ansi_issues") or [])[:8]:
            fails.append(f"- {row['source']} {row['variant']}: {issue}")
    if fails:
        lines.extend(["## Still failing", ""] + fails + [""])
    (FIXTURE_ROOT / "scorecard.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (FIXTURE_ROOT / "scorecard.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0].keys()) if csv_rows else ["source"])
        writer.writeheader()
        writer.writerows(csv_rows)
    artifacts = Path("/opt/cursor/artifacts")
    if artifacts.is_dir() and ranked:
        ranked.sort(key=lambda item: item[0])
        worst, best = ranked[0][1], ranked[-1][1]
        for label, row in (("worst", worst), ("best", best)):
            src = Path(str(row.get("compare") or ""))
            if src.is_file():
                shutil.copy(src, artifacts / f"scan-ladder-{label}-{row['source']}-{row['variant']}.png")
        shutil.copy(FIXTURE_ROOT / "scorecard.md", artifacts / "scan-ladder-scorecard.md")
        shutil.copy(FIXTURE_ROOT / "scorecard.csv", artifacts / "scan-ladder-scorecard.csv")
    print(f"wrote {FIXTURE_ROOT / 'scorecard.md'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Demo scan ladder")
    parser.add_argument("command", choices=("generate", "score", "report"))
    parser.add_argument("--label", default="before")
    parser.add_argument("--only", default="")
    parser.add_argument("--variant", default="")
    parser.add_argument("--mode", choices=("ocr", "vision"), default="ocr")
    args = parser.parse_args()
    if args.command == "generate":
        generate()
    elif args.command == "score":
        score(args.label, only=args.only, variant=args.variant, mode=args.mode)
    else:
        report()


if __name__ == "__main__":
    main()
