"""Public electrical print fixtures, structural checks, smoke import, and scan_ladder variant tests."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from PIL import Image, ImageDraw

from panelcad.importer.tesseract_runtime import find_tesseract
from panelcad.ocr.backend import active_backend, page_engine_ready

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = ROOT / "tests" / "fixtures" / "public_prints"
MANIFEST_PATH = PUBLIC_DIR / "manifest.yaml"
LICENSES_PATH = PUBLIC_DIR / "LICENSES.md"
SCORECARD_JSON_PATH = PUBLIC_DIR / "baseline_scorecard.json"
SCORECARD_MD_PATH = PUBLIC_DIR / "baseline_scorecard.md"

_USER_PATH_RE = re.compile(r"[A-Za-z]:[/\\]Users[/\\]", re.IGNORECASE)

_ORIGINAL_SCAN_LADDER_VARIANTS = (
    "clean_300",
    "dpi_200",
    "dpi_150",
    "dpi_100",
    "jpeg",
    "skew",
    "phone",
    "faded",
    "speckle",
    "fax",
)
_NEW_SCAN_LADDER_VARIANTS = (
    "rot90",
    "rot180",
    "low_contrast",
    "cropped_edge",
    "two_up",
)
_BASELINE_RECALL_SOFT_BAR = 0.50
_BASELINE_PRECISION_SOFT_BAR = 0.50


def _load_manifest() -> dict[str, Any]:
    data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _load_truth(sheet: dict[str, Any]) -> dict[str, Any]:
    sheet_id = str(sheet.get("id") or "<unknown>")
    rel = str(sheet.get("truth_file") or "").strip()
    if not rel:
        raise ValueError(f"Sheet {sheet_id!r} has has_truth=True but no truth_file configured")
    path = PUBLIC_DIR / rel
    if not path.is_file():
        raise FileNotFoundError(f"Missing truth_file for sheet {sheet_id!r}: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"Truth file for {sheet_id!r} must be a YAML mapping: {path}"
    return data


def _load_scan_ladder_module():
    path = ROOT / "scripts" / "scan_ladder.py"
    spec = importlib.util.spec_from_file_location("_panelcad_scan_ladder_public", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ocr_available() -> bool:
    if active_backend() == "tesseract":
        return bool(find_tesseract())
    return page_engine_ready() or bool(find_tesseract())


def _norm_token(value: Any) -> str:
    # Strip all internal whitespace so e.g. "FU 4" matches "FU4".
    return "".join(str(value or "").upper().split())


def _prf(truth_items: set[str], found_items: set[str]) -> tuple[float | None, float | None, int, int, int]:
    matched = len(truth_items & found_items)
    precision = (matched / len(found_items)) if found_items else None
    recall = (matched / len(truth_items)) if truth_items else None
    return precision, recall, matched, len(truth_items), len(found_items)


def _evaluate_sheet(sheet: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    from panelcad.importer.pipeline import EmptyImport, ImportJob

    sheet_id = str(sheet["id"])
    img_path = PUBLIC_DIR / str(sheet["images"]["dpi_150"])
    row: dict[str, Any] = {
        "sheet_id": sheet_id,
        "source_id": str(sheet["source_id"]),
        "kind": str(sheet["kind"]),
        "standard": str(sheet["standard"]),
        "has_truth": bool(sheet.get("has_truth")),
        "truth_mode": str(sheet.get("truth_mode") or ""),
        "image": str(sheet["images"]["dpi_150"]),
        "status": "",
        "error_type": "",
        "error": "",
        "devices_found": 0,
        "wires_found": 0,
        "found_device_tags": [],
        "found_wire_numbers": [],
        "raster_devices_count": 0,
        "raster_wires_count": 0,
        "truth_devices": None,
        "matched_devices": None,
        "extra_devices": None,
        "extra_device_tags": [],
        "missing_device_tags": [],
        "device_precision": None,
        "device_recall": None,
        "truth_wires": None,
        "matched_wires": None,
        "extra_wires": None,
        "extra_wire_numbers": [],
        "wire_precision": None,
        "wire_recall": None,
    }

    built = None
    job = None
    try:
        job = ImportJob.start([img_path], work_dir, backend="raster")
        layout = "ladder" if sheet["kind"] == "schematic" else "preserve"
        built = job.build(layout_mode=layout, target_standard=str(sheet.get("standard") or "auto"))
        row["status"] = "built"
    except EmptyImport as exc:
        row["status"] = "empty"
        row["error_type"] = "EmptyImport"
        row["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — smoke test records unexpected exceptions cleanly
        row["status"] = "error"
        row["error_type"] = type(exc).__name__
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row

    if job is not None:
        raster = job.ws.read_json("pages", "page_001", "raster.json", default={}) or {}
        if isinstance(raster, dict):
            row["raster_devices_count"] = len(raster.get("devices") or [])
            row["raster_wires_count"] = len(raster.get("wires") or [])

    found_tags: set[str] = set()
    found_wires: set[str] = set()
    if built is not None:
        for proj_sheet in built.project.sheets:
            for comp in proj_sheet.components:
                lib = str(comp.library_id or "")
                if "offpage" in lib:
                    continue
                tag = str(comp.id or "").strip()
                if tag and tag != "?" and not tag.startswith("?"):
                    found_tags.add(_norm_token(tag))
                props = comp.properties or {}
                for key in ("wire_top", "wire_bottom"):
                    val = str(props.get(key) or "").strip()
                    if val.isdigit():
                        found_wires.add(_norm_token(val))
            for wire in proj_sheet.wires:
                num = str(wire.number or "").strip()
                if num and num != "?" and not num.startswith("?"):
                    found_wires.add(_norm_token(num))

    row["devices_found"] = len(found_tags)
    row["wires_found"] = len(found_wires)
    row["found_device_tags"] = sorted(found_tags)
    row["found_wire_numbers"] = sorted(found_wires)

    if sheet.get("has_truth"):
        truth = _load_truth(sheet)
        truth_tags = {_norm_token(item) for item in (truth.get("devices") or []) if _norm_token(item)}
        truth_wires = {_norm_token(item) for item in (truth.get("wire_numbers") or []) if _norm_token(item)}
        d_prec, d_rec, d_match, d_truth, _ = _prf(truth_tags, found_tags)
        w_prec, w_rec, w_match, w_truth, _ = _prf(truth_wires, found_wires)
        extra_device_tags = sorted(found_tags - truth_tags)
        missing_device_tags = sorted(truth_tags - found_tags)
        extra_wire_numbers = sorted(found_wires - truth_wires)
        row.update(
            {
                "truth_devices": d_truth,
                "matched_devices": d_match,
                "extra_devices": len(extra_device_tags),
                "extra_device_tags": extra_device_tags,
                "missing_device_tags": missing_device_tags,
                "device_precision": round(d_prec, 4) if d_prec is not None else None,
                "device_recall": round(d_rec, 4) if d_rec is not None else None,
                "truth_wires": w_truth,
                "matched_wires": w_match,
                "extra_wires": len(extra_wire_numbers),
                "extra_wire_numbers": extra_wire_numbers,
                "wire_precision": round(w_prec, 4) if w_prec is not None else None,
                "wire_recall": round(w_rec, 4) if w_rec is not None else None,
            }
        )
    return row


def write_baseline_scorecard() -> dict[str, Any]:
    """Run all public print sheets at 150 DPI and persist baseline scorecard JSON + Markdown."""

    manifest = _load_manifest()
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="public-prints-baseline-") as tmp:
        root_tmp = Path(tmp)
        for sheet in manifest["sheets"]:
            print(f"scoring public print: {sheet['id']}", flush=True)
            row = _evaluate_sheet(sheet, root_tmp / str(sheet["id"]))
            rows.append(row)
            print(
                f"  -> status={row['status']} dev={row['devices_found']} "
                f"extra={row['extra_devices']} wire={row['wires_found']} "
                f"d_rec={row['device_recall']} d_prec={row['device_precision']}",
                flush=True,
            )

    truth_rows = [r for r in rows if r["has_truth"] and r["device_recall"] is not None]
    avg_dev_recall = (
        round(sum(float(r["device_recall"]) for r in truth_rows) / len(truth_rows), 4) if truth_rows else None
    )
    prec_rows = [r for r in truth_rows if r["device_precision"] is not None]
    avg_dev_prec = (
        round(sum(float(r["device_precision"]) for r in prec_rows) / len(prec_rows), 4) if prec_rows else None
    )
    wire_rec_rows = [r for r in truth_rows if r["wire_recall"] is not None]
    avg_wire_recall = (
        round(sum(float(r["wire_recall"]) for r in wire_rec_rows) / len(wire_rec_rows), 4) if wire_rec_rows else None
    )

    payload = {
        "schema_version": 1,
        "dpi": 150,
        "backend": "raster",
        "ocr_backend": active_backend(),
        "total_sheets": len(rows),
        "truth_sheets": sum(1 for r in rows if r["has_truth"]),
        "smoke_only_sheets": sum(1 for r in rows if not r["has_truth"]),
        "built_count": sum(1 for r in rows if r["status"] == "built"),
        "empty_count": sum(1 for r in rows if r["status"] == "empty"),
        "error_count": sum(1 for r in rows if r["status"] == "error"),
        "mean_device_recall": avg_dev_recall,
        "mean_device_precision": avg_dev_prec,
        "mean_wire_recall": avg_wire_recall,
        "rows": rows,
    }
    SCORECARD_JSON_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _pct(val: float | None) -> str:
        if val is None:
            return "-"
        return f"{val * 100:.1f}%"

    md_lines = [
        "# Public Prints Baseline Scorecard",
        "",
        "- **DPI:** 150",
        f"- **Backend:** `raster` (`{active_backend()}` OCR)",
        f"- **Sheets:** {payload['total_sheets']} total ({payload['truth_sheets']} with ground truth, {payload['smoke_only_sheets']} smoke-only)",
        f"- **Outcomes:** {payload['built_count']} built, {payload['empty_count']} empty (`EmptyImport`), {payload['error_count']} errors",
        f"- **Mean Device Recall (truth sheets):** {_pct(avg_dev_recall)}",
        f"- **Mean Device Precision (truth sheets):** {_pct(avg_dev_prec)}",
        f"- **Mean Wire Recall (truth sheets with wire numbers):** {_pct(avg_wire_recall)}",
        "",
        "| sheet_id | standard | kind | truth | status | dev_found | dev_matched | dev_extra | wire_found | wire_matched | dev_prec | dev_rec | wire_prec | wire_rec |",
        "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        dev_match = f"{r['matched_devices']}/{r['truth_devices']}" if r["truth_devices"] is not None else "-"
        dev_extra = str(r["extra_devices"]) if r["extra_devices"] is not None else "-"
        wire_match = (
            f"{r['matched_wires']}/{r['truth_wires']}"
            if r["truth_wires"] is not None and r["truth_wires"] > 0
            else "-"
        )
        md_lines.append(
            f"| {r['sheet_id']} | {r['standard']} | {r['kind']} | {r['truth_mode']} | {r['status']} | "
            f"{r['devices_found']} | {dev_match} | {dev_extra} | {r['wires_found']} | {wire_match} | "
            f"{_pct(r['device_precision'])} | {_pct(r['device_recall'])} | "
            f"{_pct(r['wire_precision'])} | {_pct(r['wire_recall'])} |"
        )
    md_lines.append("")
    SCORECARD_MD_PATH.write_text("\n".join(md_lines), encoding="utf-8")
    return payload


_MANIFEST = _load_manifest()
_ALL_SHEETS = {str(s["id"]): s for s in _MANIFEST["sheets"]}
_TRUTH_SHEET_IDS = [str(s["id"]) for s in _MANIFEST["sheets"] if s.get("has_truth")]


def test_manifest_and_licenses_cover_all_public_sources() -> None:
    manifest = _load_manifest()
    assert manifest.get("schema_version") == 1
    sources = manifest.get("sources") or []
    sheets = manifest.get("sheets") or []
    assert len(sources) > 0
    assert len(sheets) >= len(sources)

    licenses_text = LICENSES_PATH.read_text(encoding="utf-8")
    assert "https://creativecommons.org/licenses/by/4.0/" in licenses_text
    assert "https://creativecommons.org/licenses/by-sa/3.0/" in licenses_text

    source_ids: set[str] = set()
    pages_by_source: dict[str, list[int]] = {}
    for src in sources:
        sid = str(src["id"])
        assert sid not in source_ids
        source_ids.add(sid)
        for field in ("title", "author", "url", "license", "license_url", "style", "source_type"):
            assert str(src.get(field) or "").strip(), f"{sid} missing {field}"
        assert sid in licenses_text, f"{sid} missing from LICENSES.md"
        assert str(src["url"]) in licenses_text, f"{sid} URL missing from LICENSES.md"
        assert str(src["author"]) in licenses_text, f"{sid} author missing from LICENSES.md"
        assert str(src["license"]) in licenses_text, f"{sid} license missing from LICENSES.md"
        assert str(src["license_url"]) in licenses_text, f"{sid} license URL missing from LICENSES.md"
        pages_by_source[sid] = sorted(int(p) for p in (src.get("pages_used") or []))

    represented = {str(sheet["source_id"]) for sheet in sheets}
    assert represented == source_ids
    for sid, expected_pages in pages_by_source.items():
        actual_pages = sorted(int(sheet["page"]) for sheet in sheets if str(sheet["source_id"]) == sid)
        assert actual_pages == expected_pages, (sid, actual_pages, expected_pages)


def test_every_sheet_has_300dpi_and_150dpi_images_and_valid_truth() -> None:
    manifest = _load_manifest()
    source_ids = {str(src["id"]) for src in manifest["sources"]}
    seen_sheets: set[str] = set()
    for sheet in manifest["sheets"]:
        sid = str(sheet["id"])
        assert sid not in seen_sheets
        seen_sheets.add(sid)
        assert str(sheet["source_id"]) in source_ids
        assert str(sheet["kind"]) in {"schematic", "panel_layout", "terminal"}
        assert str(sheet["standard"]) in {"NEMA/ANSI", "IEC", "mixed"}

        p300 = PUBLIC_DIR / str(sheet["images"]["dpi_300"])
        p150 = PUBLIC_DIR / str(sheet["images"]["dpi_150"])
        assert p300.is_file(), f"Missing 300 DPI image: {p300}"
        assert p150.is_file(), f"Missing 150 DPI image: {p150}"

        with Image.open(p300) as im300, Image.open(p150) as im150:
            w300, h300 = im300.size
            w150, h150 = im150.size
            assert w300 > 100 and h300 > 100
            assert w150 > 100 and h150 > 100
            dpi300 = im300.info.get("dpi")
            dpi150 = im150.info.get("dpi")
            assert dpi300 and abs(float(dpi300[0]) - 300.0) < 1.0, (sid, dpi300)
            assert dpi150 and abs(float(dpi150[0]) - 150.0) < 1.0, (sid, dpi150)
            aspect300 = w300 / float(h300)
            aspect150 = w150 / float(h150)
            assert abs(aspect300 - aspect150) < 0.02, (sid, aspect300, aspect150)

        if sheet.get("has_truth"):
            assert sheet.get("truth_mode") == "full"
            truth = _load_truth(sheet)
            assert truth.get("id") == sid
            assert truth.get("source_id") == sheet["source_id"]
            devices = truth.get("devices")
            wires = truth.get("wire_numbers")
            terminals = truth.get("terminal_labels")
            assert isinstance(devices, list) and len(devices) > 0
            assert isinstance(wires, list)
            if terminals is not None:
                assert isinstance(terminals, list)
            for item in devices + wires + (terminals or []):
                val = str(item or "").strip()
                assert val and val != "?" and not val.startswith("?"), (sid, val)
        else:
            assert sheet.get("truth_mode") == "smoke_only"
            assert not sheet.get("truth_file")
            assert str(sheet.get("notes") or "").strip()


def test_truth_and_scoring_helpers() -> None:
    assert _norm_token("FU 4") == _norm_token("FU4") == "FU4"
    assert _norm_token("  fu\t1-3 ") == "FU1-3"
    assert _prf(set(), set()) == (None, None, 0, 0, 0)
    assert _prf({"M1"}, set()) == (None, 0.0, 0, 1, 0)
    assert _prf(set(), {"M1"}) == (0.0, None, 0, 0, 1)
    assert _prf({"M1", "FU4"}, {"M1", "FU4", "BOGUS"}) == (pytest.approx(2 / 3), 1.0, 2, 2, 3)

    with pytest.raises(ValueError, match="no truth_file configured"):
        _load_truth({"id": "missing_entry", "truth_file": None})
    with pytest.raises(FileNotFoundError, match="does_not_exist.yaml"):
        _load_truth({"id": "missing_path", "truth_file": "truth/does_not_exist.yaml"})


def test_no_windows_user_paths_or_pii_in_public_fixtures() -> None:
    files = list(PUBLIC_DIR.rglob("*.yaml")) + list(PUBLIC_DIR.rglob("*.md")) + list(PUBLIC_DIR.rglob("*.json"))
    files.append(Path(__file__).resolve())
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not _USER_PATH_RE.search(text), f"Windows user path leaked in {path}"


def test_baseline_scorecard_files_are_valid() -> None:
    assert SCORECARD_JSON_PATH.is_file(), "Missing baseline_scorecard.json"
    assert SCORECARD_MD_PATH.is_file(), "Missing baseline_scorecard.md"
    data = json.loads(SCORECARD_JSON_PATH.read_text(encoding="utf-8"))
    assert data.get("schema_version") == 1
    assert data.get("dpi") == 150
    assert data.get("error_count") == 0
    rows = data.get("rows") or []
    manifest = _load_manifest()
    expected_ids = [str(s["id"]) for s in manifest["sheets"]]
    assert [str(r["sheet_id"]) for r in rows] == expected_ids
    md_text = SCORECARD_MD_PATH.read_text(encoding="utf-8")
    for row in rows:
        assert row["status"] in {"built", "empty", "error"}
        assert isinstance(row["devices_found"], int)
        assert isinstance(row["wires_found"], int)
        if row["has_truth"]:
            assert isinstance(row["extra_devices"], int)
            assert isinstance(row["extra_device_tags"], list)
        assert str(row["sheet_id"]) in md_text


def test_scan_ladder_keeps_existing_sources_and_variants_intact() -> None:
    ladder = _load_scan_ladder_module()
    assert [src["id"] for src in ladder.SOURCES] == ["schematic", "panel", "terminal"]
    variant_ids = [item["id"] for item in ladder.VARIANTS]
    assert tuple(variant_ids[:10]) == _ORIGINAL_SCAN_LADDER_VARIANTS
    for new_id in _NEW_SCAN_LADDER_VARIANTS:
        assert new_id in variant_ids


def test_scan_ladder_new_degradations() -> None:
    ladder = _load_scan_ladder_module()
    by_id = {item["id"]: item for item in ladder.VARIANTS}

    sample = Image.new("RGB", (600, 400), (255, 255, 255))
    draw = ImageDraw.Draw(sample)
    # Outer border at 8 px inside sheet (< 3.2% crop of dx=19 px, dy=13 px) + interior marks.
    draw.rectangle((8, 8, 591, 391), outline=(0, 0, 0), width=3)
    draw.rectangle((48, 36, 130, 88), fill=(0, 0, 0))
    draw.line((80, 200, 520, 200), fill=(0, 0, 0), width=3)

    rot90_img, rot90_opts = ladder.degrade(sample, "schematic", by_id["rot90"])
    assert rot90_opts["format"] == "PNG"
    assert rot90_opts["dpi"] == (200, 200)
    assert rot90_img.size == (400, 600)
    assert np.array_equal(np.asarray(rot90_img), np.asarray(sample.transpose(Image.Transpose.ROTATE_270)))

    rot180_img, rot180_opts = ladder.degrade(sample, "schematic", by_id["rot180"])
    assert rot180_opts["format"] == "PNG"
    assert rot180_opts["dpi"] == (200, 200)
    assert rot180_img.size == (600, 400)
    assert not np.array_equal(np.asarray(rot180_img), np.asarray(sample))
    assert np.array_equal(np.asarray(rot180_img), np.asarray(sample.transpose(Image.Transpose.ROTATE_180)))

    low_img, low_opts = ladder.degrade(sample, "schematic", by_id["low_contrast"])
    assert low_opts["format"] == "PNG"
    assert low_opts["dpi"] == (200, 200)
    assert low_img.size == (600, 400)
    low_arr = np.asarray(low_img.convert("L"))
    assert int(low_arr.min()) >= 140
    assert 30 <= int(low_arr.max()) - int(low_arr.min()) <= 120

    sample_arr = np.asarray(sample.convert("L"))
    # Confirm the 8 px outer border exists on all four edges of the source image before cropping.
    assert int((sample_arr[:20, :] < 128).sum()) > 1000
    assert int((sample_arr[-20:, :] < 128).sum()) > 1000
    assert int((sample_arr[:, :20] < 128).sum()) > 500
    assert int((sample_arr[:, -20:] < 128).sum()) > 500

    crop_img, crop_opts = ladder.degrade(sample, "schematic", by_id["cropped_edge"])
    assert crop_opts["format"] == "PNG"
    assert crop_opts["dpi"] == (200, 200)
    dx, dy = round(sample.width * 0.032), round(sample.height * 0.032)
    assert crop_img.size == (sample.width - 2 * dx, sample.height - 2 * dy)
    crop_arr = np.asarray(crop_img.convert("L"))
    # The entire 8 px outer border was clipped off on all four edges; interior marks survive.
    assert int((crop_arr[:20, :] < 128).sum()) == 0
    assert int((crop_arr[-20:, :] < 128).sum()) == 0
    assert int((crop_arr[:, :20] < 128).sum()) == 0
    assert int((crop_arr[:, -20:] < 128).sum()) == 0
    # Interior horizontal line at y=200 shifted to y=200-dy remains intact.
    assert int((crop_arr[200 - dy, 80 - dx : 520 - dx] < 128).sum()) > 400

    two_img, two_opts = ladder.degrade(sample, "schematic", by_id["two_up"])
    assert two_opts["format"] == "PNG"
    assert two_opts["dpi"] == (200, 200)
    assert two_img.width > two_img.height
    assert two_img.width > sample.width

    portrait = Image.new("RGB", (300, 900), (255, 255, 255))
    portrait_two, _ = ladder.degrade(portrait, "schematic", by_id["two_up"])
    assert portrait_two.width > portrait_two.height
    assert portrait_two.width > portrait.width


def test_scan_ladder_generate_includes_new_variants(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pytest.importorskip("PySide6.QtWidgets")
    ladder = _load_scan_ladder_module()
    dest_root = tmp_path / "scan_ladder"
    monkeypatch.setattr(ladder, "FIXTURE_ROOT", dest_root)
    monkeypatch.setattr(ladder, "SOURCES", (ladder.SOURCES[0],))
    try:
        ladder.generate()
    except (ImportError, OSError, RuntimeError) as exc:
        if any(tok in str(exc).lower() for tok in ("qt", "pyside6", "display", "xcb", "libgl")):
            pytest.skip(f"Qt unavailable for scan_ladder.generate(): {exc}")
        raise

    manifest = json.loads((dest_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"] == ["schematic"]
    all_variants = _ORIGINAL_SCAN_LADDER_VARIANTS + _NEW_SCAN_LADDER_VARIANTS
    assert manifest["variants"] == list(all_variants)
    assert (dest_root / "truth" / "schematic.json").is_file()
    expected_suffix = {
        item["id"]: ".jpg" if item["kind"] in {"jpeg", "phone"} else ".png"
        for item in ladder.VARIANTS
    }
    for vid in all_variants:
        out = dest_root / "schematic" / f"{vid}{expected_suffix[vid]}"
        assert out.is_file() and out.stat().st_size > 0, out


def test_scan_ladder_score_handles_missing_variants_and_external_fixture_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ladder = _load_scan_ladder_module()
    ext_root = tmp_path / "older_scan_ladder"
    (ext_root / "truth").mkdir(parents=True)
    (ext_root / "schematic").mkdir(parents=True)
    truth = {
        "id": "schematic",
        "kind": "schematic",
        "standard": "NEMA/ANSI",
        "devices": ["CR1"],
        "offpage": [],
        "wire_numbers": [],
        "edges": [],
        "texts": [],
    }
    (ext_root / "truth" / "schematic.json").write_text(json.dumps(truth), encoding="utf-8")
    # Populate only the original 10 variants; omit the 5 new variants to mimic an older folder.
    blank = Image.new("RGB", (120, 80), (255, 255, 255))
    for vid in _ORIGINAL_SCAN_LADDER_VARIANTS:
        suffix = ".jpg" if vid in {"jpeg", "phone"} else ".png"
        blank.save(ext_root / "schematic" / f"{vid}{suffix}")

    monkeypatch.setattr(ladder, "FIXTURE_ROOT", ext_root)
    monkeypatch.setattr(ladder, "RUN_ROOT", tmp_path / "runs")

    # Missing new variant returns an error row rather than raising FileNotFoundError.
    missing_row = ladder.score_variant("schematic", "rot90", workspace=tmp_path / "ws_rot90")
    assert missing_row["source"] == "schematic"
    assert missing_row["variant"] == "rot90"
    assert "FileNotFoundError" in missing_row["error"]
    assert missing_row["devices"] == 0.0

    # Present variant outside ROOT does not crash image_path.relative_to(ROOT).
    from panelcad.importer import pipeline as pipeline_mod

    def _fake_start(*_args: Any, **_kwargs: Any) -> Any:
        raise pipeline_mod.EmptyImport("mock empty for path check")

    monkeypatch.setattr(pipeline_mod.ImportJob, "start", staticmethod(_fake_start))
    rows = ladder.score("partial", only="schematic")
    assert len(rows) == len(_ORIGINAL_SCAN_LADDER_VARIANTS) + len(_NEW_SCAN_LADDER_VARIANTS)
    by_var = {r["variant"]: r for r in rows}
    assert by_var["clean_300"]["image"] == str(ext_root / "schematic" / "clean_300.png")
    assert "mock empty" in by_var["clean_300"]["error"]
    for vid in _NEW_SCAN_LADDER_VARIANTS:
        assert "FileNotFoundError" in by_var[vid]["error"], vid


@pytest.fixture(scope="module")
def imported_public_prints(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    if not _ocr_available():
        pytest.skip("neither bundled electrical OCR weights nor Tesseract binary is installed")

    work = tmp_path_factory.mktemp("public-prints-import")
    results: dict[str, dict[str, Any]] = {}
    for sheet in _MANIFEST["sheets"]:
        sid = str(sheet["id"])
        results[sid] = _evaluate_sheet(sheet, work / sid)
    return results


@pytest.mark.parametrize("sheet_id", list(_ALL_SHEETS.keys()))
def test_public_print_smoke(sheet_id: str, imported_public_prints: dict[str, dict[str, Any]]) -> None:
    row = imported_public_prints[sheet_id]
    assert row["status"] in {"built", "empty"}, row["error"]
    assert row["error_type"] in {"", "EmptyImport"}, row["error"]


@pytest.mark.parametrize("sheet_id", _TRUTH_SHEET_IDS)
def test_public_print_baseline_truth_xfail(sheet_id: str, imported_public_prints: dict[str, dict[str, Any]]) -> None:
    row = imported_public_prints[sheet_id]
    assert row["status"] in {"built", "empty"}, row["error"]
    rec = float(row["device_recall"] or 0.0)
    prec = row["device_precision"]
    extra_count = int(row["extra_devices"] or 0)
    extra_sample = (row.get("extra_device_tags") or [])[:8]
    w_rec = row["wire_recall"]
    w_prec = row["wire_precision"]
    w_extra = int(row["extra_wires"] or 0)
    below_bar = (
        row["status"] == "empty"
        or rec < _BASELINE_RECALL_SOFT_BAR
        or prec is None
        or float(prec) < _BASELINE_PRECISION_SOFT_BAR
    )
    if below_bar:
        pytest.xfail(
            f"{sheet_id} baseline: status={row['status']} dev={row['matched_devices']}/{row['truth_devices']} "
            f"(found={row['devices_found']}, extra={extra_count}, rec={rec:.3f}, prec={prec}, "
            f"extra_tags={extra_sample}) "
            f"wire={row['matched_wires']}/{row['truth_wires']} "
            f"(found={row['wires_found']}, extra={w_extra}, rec={w_rec}, prec={w_prec})"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate public print baseline scorecard.")
    parser.add_argument("--write-scorecard", action="store_true", help="Run OCR baseline and update scorecard files.")
    args = parser.parse_args()
    if args.write_scorecard:
        write_baseline_scorecard()


if __name__ == "__main__":
    main()
