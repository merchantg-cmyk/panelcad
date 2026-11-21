"""Panel and terminal scans keep Tesseract + preserve; schematics keep electrical + ladder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panelcad.importer.pipeline import (
    EmptyImport,
    ImportJob,
    _choose_raster_ocr_backend,
    _page_wants_tesseract_reader,
    _raw_has_terminal_strip,
    _scan_default_layout,
)

ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "scan_ladder"

# Floors from the committed OCR scorecard (scores-after.json) for the variants
# that regressed when electrical OCR became the default scan reader.
PANEL_FLOORS = {
    "clean_300": 0.555,
    "faded": 0.555,
}
TERMINAL_FLOORS = {
    "clean_300": 1.0,
    "dpi_200": 1.0,
    "dpi_100": 1.0,
    "fax": 1.0,
}


def _fixture(source: str, variant: str) -> Path:
    for suffix in (".png", ".jpg"):
        path = ROOT / source / f"{variant}{suffix}"
        if path.is_file():
            return path
    raise FileNotFoundError(f"{source}/{variant}")


def _device_recall(built_tags: set[str], truth_tags: set[str]) -> float:
    if not truth_tags:
        return 1.0
    concrete = {tag for tag in built_tags if tag != "?" and not str(tag).startswith("?")}
    return len(truth_tags & concrete) / len(truth_tags)


def test_scan_default_layout_is_preserve_for_panel_and_terminal_only():
    assert _scan_default_layout({"sheets": [{"kind": "panel_layout", "page_kind": "panel_layout"}]}) == "preserve"
    assert _scan_default_layout({"sheets": [{"kind": "terminal", "page_kind": "terminal_plan"}]}) == "preserve"
    assert _scan_default_layout({"sheets": [{"kind": "schematic", "page_kind": "schematic_control"}]}) == "ladder"
    assert (
        _scan_default_layout(
            {
                "sheets": [
                    {"kind": "schematic", "page_kind": "schematic_control"},
                    {"kind": "panel_layout", "page_kind": "panel_layout"},
                ]
            }
        )
        == "ladder"
    )


def test_page_wants_tesseract_for_layout_and_strip_kinds():
    assert _page_wants_tesseract_reader({"kind": "panel_layout", "devices": []})
    assert _page_wants_tesseract_reader({"kind": "terminal_plan", "devices": []})
    assert not _page_wants_tesseract_reader({"kind": "schematic_control", "devices": []})
    assert _page_wants_tesseract_reader(
        {
            "kind": "other",
            "devices": [
                {"tag": "X1-1", "symbol": "terminal"},
                {"tag": "X1-2", "type": "terminal"},
            ],
        }
    )
    assert not _page_wants_tesseract_reader(
        {
            "kind": "other",
            "devices": [{"tag": "M1", "symbol": "contactor"}],
        }
    )
    assert not _page_wants_tesseract_reader(
        {"kind": "schematic_control", "devices": []},
        image=None,
    )
    # A schematic whose title band read as "other" but carries control/power devices
    # alongside X1 strip terminals must keep electrical OCR (not re-read via Tesseract).
    assert not _page_wants_tesseract_reader(
        {
            "kind": "other",
            "devices": [
                {"tag": "M1", "symbol": "contactor"},
                {"tag": "ES1", "symbol": "estop"},
                {"tag": "PB1", "symbol": "pushbutton"},
                {"tag": "X1-1", "symbol": "terminal"},
            ],
        },
        image=None,
    )


def test_raster_ocr_backend_pins_terminal_not_schematic():
    from PIL import Image

    from panelcad.importer.imageutil import auto_rotate
    from panelcad.ocr.backend import active_backend

    assert active_backend() == "electrical"
    term, _ = auto_rotate(Image.open(_fixture("terminal", "dpi_200")).convert("RGB"))
    dpi = Image.open(_fixture("terminal", "dpi_200")).info.get("dpi", (200, 200))
    dx = float(dpi[0] if isinstance(dpi, tuple) else dpi)
    w = term.size[0] * 25.4 / dx
    h = term.size[1] * 25.4 / dx
    assert _raw_has_terminal_strip(term, w, h)
    assert _choose_raster_ocr_backend(term, w, h) == "tesseract"

    schem, _ = auto_rotate(Image.open(_fixture("schematic", "clean_300")).convert("RGB"))
    sdpi = Image.open(_fixture("schematic", "clean_300")).info.get("dpi", (300, 300))
    sx = float(sdpi[0] if isinstance(sdpi, tuple) else sdpi)
    sw = schem.size[0] * 25.4 / sx
    sh = schem.size[1] * 25.4 / sx
    # Rung ink can look strip-like; a schematic title peek must keep electrical.
    assert _choose_raster_ocr_backend(schem, sw, sh) is None

    # 10 mm phase-pitch strokes on schematic/dpi_150 (h/w ~ 1.66) must not trigger
    # strip detection even if a degraded title band peek returns "other".
    schem150, _ = auto_rotate(Image.open(_fixture("schematic", "dpi_150")).convert("RGB"))
    s150_dpi = Image.open(_fixture("schematic", "dpi_150")).info.get("dpi", (150, 150))
    s150_x = float(s150_dpi[0] if isinstance(s150_dpi, tuple) else s150_dpi)
    s150_w = schem150.size[0] * 25.4 / s150_x
    s150_h = schem150.size[1] * 25.4 / s150_x
    assert not _raw_has_terminal_strip(schem150, s150_w, s150_h)
    assert _choose_raster_ocr_backend(schem150, s150_w, s150_h) is None


@pytest.mark.parametrize("variant,floor", sorted(PANEL_FLOORS.items()))
def test_panel_scan_device_recall_floor(tmp_path: Path, variant: str, floor: float):
    truth = json.loads((ROOT / "truth" / "panel.json").read_text(encoding="utf-8"))
    job = ImportJob.start([_fixture("panel", variant)], tmp_path / variant, backend="raster")
    built = job.build(layout_mode="preserve", target_standard=str(truth.get("standard") or "auto"))
    assert job.manifest.get("layout_mode") == "preserve"
    page = job._page("page_001")
    assert (page.get("classification") or {}).get("kind") == "panel_layout"
    tags = {
        comp.id
        for sheet in built.project.sheets
        for comp in sheet.components
        if "offpage" not in str(comp.library_id or "")
    }
    recall = _device_recall(tags, set(truth["devices"]))
    assert recall + 1e-9 >= floor, f"panel/{variant} recall {recall:.3f} < {floor}"


@pytest.mark.parametrize("variant,floor", sorted(TERMINAL_FLOORS.items()))
def test_terminal_scan_device_recall_floor(tmp_path: Path, variant: str, floor: float):
    truth = json.loads((ROOT / "truth" / "terminal.json").read_text(encoding="utf-8"))
    job = ImportJob.start([_fixture("terminal", variant)], tmp_path / variant, backend="raster")
    try:
        built = job.build(layout_mode="preserve", target_standard=str(truth.get("standard") or "auto"))
    except EmptyImport as exc:
        pytest.fail(f"terminal/{variant} raised EmptyImport without vision: {exc}")
    assert "vision backend" not in str(job.manifest.get("warning") or "").lower()
    page = job._page("page_001")
    kind = str((page.get("classification") or {}).get("kind") or "")
    assert kind in {"terminal_plan", "other"}, kind
    tags = {
        comp.id
        for sheet in built.project.sheets
        for comp in sheet.components
        if "offpage" not in str(comp.library_id or "")
    }
    recall = _device_recall(tags, set(truth["devices"]))
    assert recall + 1e-9 >= floor, f"terminal/{variant} recall {recall:.3f} < {floor}"


def test_panel_and_terminal_default_layout_is_preserve(tmp_path: Path):
    panel = ImportJob.start([_fixture("panel", "clean_300")], tmp_path / "panel", backend="raster")
    panel.build(target_standard="NEMA/ANSI")
    assert panel.manifest.get("layout_mode") == "preserve"

    terminal = ImportJob.start([_fixture("terminal", "clean_300")], tmp_path / "terminal", backend="raster")
    terminal.build(target_standard="NEMA/ANSI")
    assert terminal.manifest.get("layout_mode") == "preserve"
