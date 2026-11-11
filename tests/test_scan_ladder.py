"""Demo scan ladder fixtures stay small and keep the DPI the importer scales from."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "scan_ladder"


def test_ladder_covers_demo_sheets_and_degradations():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"] == ["schematic", "panel", "terminal"]
    assert set(manifest["variants"]) == {
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
    }
    for source in manifest["sources"]:
        truth = json.loads((ROOT / "truth" / f"{source}.json").read_text(encoding="utf-8"))
        assert truth["devices"]
        assert (ROOT / source).is_dir()


def test_phone_photo_is_tagged_72_dpi_and_fax_is_bilevel():
    phone = Image.open(ROOT / "schematic" / "phone.jpg")
    assert phone.info.get("dpi") == (72, 72)
    fax = Image.open(ROOT / "schematic" / "fax.png")
    colors = fax.convert("L").getcolors(2)
    assert colors is not None and len(colors) <= 2
    skew = Image.open(ROOT / "terminal" / "skew.png")
    dpi = skew.info.get("dpi")
    assert dpi is not None and abs(float(dpi[0]) - 200) < 0.1 and abs(float(dpi[1]) - 200) < 0.1


def test_fixture_tree_stays_under_nine_megabytes():
    # The backplate is an ANSI D sheet, so the panel scans are larger than the old A3 set.
    total = sum(path.stat().st_size for path in ROOT.rglob("*") if path.is_file())
    assert total < 9_000_000
