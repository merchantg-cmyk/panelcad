"""A cleanup call asks the harness question. A tile call still asks for detections."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from panelcad.importer.cleanup import CLEANUP_SYSTEM_PROMPT, cleanup_harness, review_cleanup, wrap_up_scan
from panelcad.importer.grok_build import DETECTION_SCHEMA, SYSTEM_PROMPT, GrokBuildVision, parse_cleanup_result
from panelcad.importer.pipeline import ImportJob
from panelcad.importer.vision import TILE_PROMPT, OpenAICompatibleVision
from panelcad.importer.vision_config import VisionSettings
from tests.test_scan_cleanup import _numbers, _scan, _sheet, _starter


def _png(path: Path) -> Path:
    Image.new("RGB", (8, 8), "white").save(path)
    return path


def _cleanup_work(image: Path, gaps: list[dict]) -> dict:
    return {
        "id": "cleanup",
        "kind": "cleanup",
        "image": str(image),
        "instructions": cleanup_harness(),
        "facts": [{"kind": "device", "tag": "FU1", "library_id": "nema.protection.fuse_10a"}],
        "gaps": gaps,
    }


def test_grok_build_cleanup_asks_the_harness_and_returns_facts(tmp_path: Path):
    image = _png(tmp_path / "scan.png")
    gaps = [{"id": "unconnected:FU1", "kind": "unconnected", "tag": "FU1", "library_id": "nema.protection.fuse_10a"}]
    reply = {
        "devices": [],
        "wires": [{"gap": "wire:w4", "number": "107", "confidence": 0.95, "unreadable": False}],
        "connections": [{"gap": "unconnected:FU1", "from": "L1", "to": "FU1.1", "confidence": 0.95, "unreadable": False}],
    }
    captured: list[list[str]] = []

    def runner(argv: list[str], cwd: str | None, timeout: float) -> tuple[int, str, str]:
        captured.append(list(argv))
        blocks = json.loads(Path(argv[argv.index("--prompt-file") + 1]).read_text(encoding="utf-8"))
        text = blocks[0]["text"]
        assert "Extract facts only" in text
        assert "unconnected:FU1" in text
        assert "FU1" in text
        assert "Put bbox in crop millimetres" not in text
        assert "four lists: devices, conductors" not in text
        assert TILE_PROMPT.strip()[:40] not in text
        images = [block for block in blocks if block["type"] == "image"]
        assert images and images[0]["data"]
        return 0, json.dumps({"structuredOutput": reply, "total_cost_usd": 0.01}), ""

    client = GrokBuildVision(VisionSettings(preset="grok-build"), runner=runner)
    data = client.extract(_cleanup_work(image, gaps))
    assert data == reply
    assert "detections" not in data
    argv = captured[0]
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema != DETECTION_SCHEMA
    assert "conductors" not in schema.get("properties", {})
    assert "connections" in schema.get("properties", {})
    assert json.dumps(DETECTION_SCHEMA, separators=(",", ":")) not in argv
    override = argv[argv.index("--system-prompt-override") + 1]
    assert override == CLEANUP_SYSTEM_PROMPT
    assert override != SYSTEM_PROMPT
    assert "conductors" not in override
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "Bash" in argv


def test_parse_cleanup_result_does_not_flatten_devices():
    body = {
        "devices": [{"tag": "T1", "library_id": "nema.power.control_transformer", "confidence": 0.9}],
        "wires": [{"gap": "wire:w1", "number": "101", "confidence": 0.9}],
        "connections": [],
    }
    found, _event, parsed = parse_cleanup_result(json.dumps({"structuredOutput": body}))
    assert parsed is True
    assert found == body
    assert "detections" not in found


def test_grok_cleanup_tile_reply_is_unchanged_and_dropped(tmp_path: Path):
    image = _png(tmp_path / "scan.png")
    tile = {
        "devices": [{"tag": "K1", "symbol_class": "contactor", "bbox": [1, 2, 8, 6], "confidence": 0.8}],
        "conductors": [{"from_tag": "ES1", "from_pin": "2", "to_tag": "M1", "to_pin": "A1", "bbox": [0, 0, 4, 1]}],
        "wire_numbers": [],
        "buses": [],
    }

    def runner(argv: list[str], cwd: str | None, timeout: float) -> tuple[int, str, str]:
        return 0, json.dumps({"structuredOutput": tile}), ""

    client = GrokBuildVision(VisionSettings(preset="grok-build"), runner=runner)
    data = client.extract(_cleanup_work(image, [{"id": "unconnected:FU1", "kind": "unconnected", "tag": "FU1"}]))
    assert data == tile
    assert "detections" not in data
    session = _starter()
    _accepted, dropped = review_cleanup(session, data, [{"id": "unread_device:elementary", "kind": "unread_device"}])
    assert _accepted == []
    assert dropped


def test_wrap_up_accepts_a_wire_number_from_grok_build(tmp_path: Path):
    session = _starter()
    # PL1's return is the other blank on this starter. Land it so the sheet
    # is complete once the missing wire number is filled.
    session.wire("PL1.X2", "L2", number="104")
    wire = next(item for item in _sheet(session).wires if item.number == "?")
    image = Path(_scan(tmp_path))

    def runner(argv: list[str], cwd: str | None, timeout: float) -> tuple[int, str, str]:
        text = json.loads(Path(argv[argv.index("--prompt-file") + 1]).read_text(encoding="utf-8"))[0]["text"]
        assert f"wire:{wire.id}" in text
        assert "Extract facts only" in text
        body = {
            "devices": [],
            "wires": [{"gap": f"wire:{wire.id}", "number": "107", "confidence": 0.95, "unreadable": False}],
            "connections": [],
        }
        return 0, json.dumps({"structuredOutput": body}), ""

    vision = GrokBuildVision(VisionSettings(preset="grok-build"), runner=runner)
    result = wrap_up_scan(session, image=str(image), backend=vision)
    assert result["issued"] is True, result
    assert "107" in _numbers(session)


def test_ollama_cleanup_skips_the_tile_schema(tmp_path: Path):
    image = _png(tmp_path / "scan.png")
    captured: dict = {}

    def transport(method: str, url: str, body: dict, headers: dict, timeout: float) -> dict:
        captured["body"] = body
        reply = {"devices": [], "wires": [{"gap": "wire:w4", "number": "107", "confidence": 0.9}], "connections": []}
        return {"message": {"content": json.dumps(reply)}}

    client = OpenAICompatibleVision(
        VisionSettings(preset="ollama", model="qwen3-vl:8b-instruct"),
        transport=transport,
    )
    data = client.extract(
        _cleanup_work(image, [{"id": "unconnected:FU1", "kind": "unconnected", "tag": "FU1"}])
    )
    assert data["wires"][0]["number"] == "107"
    blob = json.dumps(captured["body"])
    assert "Extract facts only" in blob
    assert "unconnected:FU1" in blob
    assert "Put bbox in crop millimetres" not in blob
    assert "four lists: devices, conductors" not in blob
    assert "format" not in captured["body"]
    assert captured["body"]["messages"][1]["images"]


def test_open_rewrites_a_missing_page_image(tmp_path: Path):
    root = tmp_path / "job"
    page = root / "pages" / "page_001"
    page.mkdir(parents=True)
    _png(page / "page.png")
    (root / "manifest.json").write_text(
        json.dumps({"stage": "ready", "pages": ["page_001"], "sources": [], "queue": [], "pending": ""}),
        encoding="utf-8",
    )
    (page / "page.json").write_text(
        json.dumps(
            {
                "id": "page_001",
                "image": r"D:\old\machine\pages\page_001\page.png",
                "width_mm": 431.8,
                "height_mm": 279.4,
            }
        ),
        encoding="utf-8",
    )
    (page / "merged.json").write_text(json.dumps({"devices": [], "wires": []}), encoding="utf-8")
    job = ImportJob.open(root)
    saved = json.loads((page / "page.json").read_text(encoding="utf-8"))
    assert Path(saved["image"]).is_file()
    assert Path(saved["image"]).name == "page.png"
    assert saved["image"] == str((page / "page.png").resolve())
