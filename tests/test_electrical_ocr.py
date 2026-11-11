"""Electrical OCR contract, local engine, and the importer/reconstruct call sites.

The default backend is electrical. These tests do not need the tesseract
binary. They do not claim a win over Tesseract or Paddle. That comparison
is ``scripts/bench_electrical_ocr.py``.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from panelcad.ocr.backend import active_backend, normalize_backend, ocr_scope
from panelcad.ocr.dispatch import get_engine, image_to_data, image_to_string
from panelcad.ocr.electrical import ElectricalOcr
from panelcad.ocr.samples import electrical_samples


def _texts(spans) -> set[str]:
    return {span.text for span in spans}


def test_default_backend_is_electrical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PANELCAD_OCR_BACKEND", raising=False)
    assert active_backend() == "electrical"
    assert get_engine().name == "electrical"


def test_explicit_tesseract_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANELCAD_OCR_BACKEND", "tesseract")
    assert active_backend() == "tesseract"
    assert get_engine().name == "tesseract"


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PANELCAD_OCR_BACKEND", raising=False)
    with pytest.raises(ValueError, match="electrical"):
        normalize_backend("paddle")
    with ocr_scope("electrical"):
        assert active_backend() == "electrical"
    assert active_backend() == "electrical"


def test_span_contract_on_a_clean_tag() -> None:
    sample = next(item for item in electrical_samples() if item.name == "clean-k1")
    spans = ElectricalOcr().recognize(sample.image, psm=7)
    assert spans, "clean K1 produced no words"
    span = next(item for item in spans if item.text == "K1")
    assert span.source == "electrical"
    assert 0.0 <= span.confidence <= 1.0
    assert span.rotation in {0, 90, 180, 270}
    assert span.width > 0 and span.height > 0
    assert 0 <= span.left < sample.image.shape[1]
    assert 0 <= span.top < sample.image.shape[0]
    assert span.left + span.width <= sample.image.shape[1] + 1
    assert span.top + span.height <= sample.image.shape[0] + 1


def test_electrical_reads_clean_shop_labels() -> None:
    engine = ElectricalOcr()
    expected = {
        "clean-k1": {"K1"},
        "clean-cr12": {"CR12"},
        "clean-tb1": {"TB1"},
        "clean-10m": {"10M"},
        "wire-104": {"104"},
        "title-sheet": {"SHEET", "3", "OF", "7"},
        "blueprint-q1": {"Q1"},
        "low-contrast-m1": {"M1"},
        "small-fu1": {"FU1"},
        "struck-104": {"104"},
        "page-tags": {"K1", "M1", "104", "TB1"},
    }
    by_name = {sample.name: sample for sample in electrical_samples()}
    for name, truths in expected.items():
        spans = engine.recognize(by_name[name].image, psm=6)
        assert truths <= _texts(spans), name


def test_electrical_reads_a_quarter_turn() -> None:
    sample = next(item for item in electrical_samples() if item.name == "rotated-w12")
    spans = ElectricalOcr().recognize(sample.image, psm=7, rotations=(0, 90, 180, 270))
    match = [span for span in spans if span.text == "W12"]
    assert match, [span.text for span in spans]
    assert match[0].rotation == 90
    assert match[0].width > 0 and match[0].height > 0


def test_blank_image_emits_nothing() -> None:
    blank = np.full((80, 200, 3), 255, np.uint8)
    assert ElectricalOcr().recognize(blank) == []


def test_dispatch_matches_tesseract_dict_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANELCAD_OCR_BACKEND", "electrical")
    sample = next(item for item in electrical_samples() if item.name == "clean-tb1")
    data = image_to_data(sample.image, config="--psm 7")
    for key in ("text", "conf", "left", "top", "width", "height"):
        assert key in data
        assert len(data[key]) == len(data["text"])
    assert "TB1" in data["text"]
    assert image_to_string(sample.image, config="--psm 8") == "TB1"
    heard = image_to_string(sample.image, config="--psm 7 -c tessedit_char_whitelist=0123456789")
    assert "TB1" not in heard


def test_electrical_backend_does_not_call_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    import pytesseract

    monkeypatch.setenv("PANELCAD_OCR_BACKEND", "electrical")

    def boom(*_args, **_kwargs):
        raise AssertionError("tesseract was called")

    monkeypatch.setattr(pytesseract, "image_to_data", boom)
    monkeypatch.setattr(pytesseract, "image_to_string", boom)
    sample = next(item for item in electrical_samples() if item.name == "clean-k1")
    assert "K1" in image_to_string(sample.image, config="--psm 7")


def test_importer_word_read_uses_the_electrical_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.importer.raster import _ocr_word

    monkeypatch.setenv("PANELCAD_OCR_BACKEND", "electrical")
    sample = next(item for item in electrical_samples() if item.name == "clean-k1")
    gray = np.asarray(Image.fromarray(sample.image).convert("L"))
    height, width = gray.shape
    assert _ocr_word(gray, (0, 0, width, height), expand_left_mm=0.0, psm=7, ppm=4.0) == "K1"


def test_pixel_reader_finds_a_tag_without_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    import pytesseract

    from panelcad.importer.raster import _ocr_tokens

    monkeypatch.setenv("PANELCAD_OCR_BACKEND", "electrical")

    def boom(*_args, **_kwargs):
        raise AssertionError("tesseract was called")

    monkeypatch.setattr(pytesseract, "image_to_data", boom)
    monkeypatch.setattr(pytesseract, "image_to_string", boom)
    font_sample = next(item for item in electrical_samples() if item.name == "clean-k1")
    # Place the same K1 crop in the band the page reader keeps: below 8 mm
    # and above the bottom title strip. 4 px/mm, 320 x 200 mm.
    page_w, page_h = 1280, 800
    rgb = np.full((page_h, page_w, 3), 255, np.uint8)
    crop = font_sample.image
    rgb[280 : 280 + crop.shape[0], 200 : 200 + crop.shape[1]] = crop
    gray = np.asarray(Image.fromarray(rgb).convert("L"))
    tokens = _ocr_tokens(gray, rgb, ppm=4.0, width_mm=320.0, height_mm=200.0)
    assert any(tok["text"] == "K1" for tok in tokens), tokens
    assert all(tok["source"] == "ocr" for tok in tokens)


def test_strip_scan_reads_a_wire_number(monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.importer.strip_scan import _ocr

    monkeypatch.setenv("PANELCAD_OCR_BACKEND", "electrical")
    sample = next(item for item in electrical_samples() if item.name == "wire-104")
    gray = np.asarray(Image.fromarray(sample.image).convert("L"))
    assert "104" in _ocr(gray, digits=True)


def test_terminal_photo_reader_accepts_tb1(monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.reconstruct.terminal_photo import _accepted_ocr

    monkeypatch.setenv("PANELCAD_OCR_BACKEND", "electrical")
    sample = next(item for item in electrical_samples() if item.name == "clean-tb1")
    gray = np.asarray(Image.fromarray(sample.image).convert("L"))
    assert _accepted_ocr(gray) == "TB1"


def test_read_pixels_keeps_the_token_on_the_electrical_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.importer.raster import read_pixels
    from panelcad.library.catalog import default_catalog

    monkeypatch.setenv("PANELCAD_OCR_BACKEND", "electrical")
    sample = next(item for item in electrical_samples() if item.name == "clean-k1")
    page = Image.new("RGB", (1200, 800), (255, 255, 255))
    page.paste(Image.fromarray(sample.image), (240, 200))
    # 4 px/mm. The tag centre sits in the band read_pixels keeps.
    facts = read_pixels(page, 300.0, 200.0, default_catalog(), allow_missing_ocr=False)
    assert facts["ocr_skipped"] is False
    assert any(tok["text"] == "K1" for tok in facts["ocr_tokens"]), facts["ocr_tokens"]


def test_cli_accepts_the_electrical_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    monkeypatch.setenv("PANELCAD_OCR_BACKEND", "tesseract")
    image = tmp_path / "blank.png"
    Image.new("RGB", (80, 40), (255, 255, 255)).save(image)
    runner = CliRunner()
    help_text = runner.invoke(app, ["import", "--help"]).output
    assert "--ocr-backend" in help_text
    rejected = runner.invoke(
        app,
        ["import", str(image), "--vision-backend", "none", "--ocr-backend", "paddle", "--workspace", str(tmp_path / "ws")],
    )
    assert rejected.exit_code != 0
    assert "electrical" in (rejected.output + (rejected.stderr or ""))
    # A blank page still runs the electrical reader. It must not ask for Tesseract.
    accepted = runner.invoke(
        app,
        [
            "import",
            str(image),
            "--vision-backend",
            "none",
            "--ocr-backend",
            "electrical",
            "--workspace",
            str(tmp_path / "elec"),
            "--out",
            str(tmp_path / "elec.pcad.yaml"),
        ],
    )
    combined = accepted.output + (accepted.stderr or "")
    assert "Tesseract OCR was not found" not in combined
    assert active_backend() == "tesseract"


def test_missing_weights_do_not_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import urllib.request

    from panelcad.ocr.ppocr import read_print
    from panelcad.ocr.weights import model_root, weights_ready

    monkeypatch.setenv("PANELCAD_OCR_MODELS", str(tmp_path / "absent"))

    def boom(*_args, **_kwargs):
        raise AssertionError("OCR tried to download")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert model_root() is None
    assert weights_ready() is False
    page = np.full((1200, 1600, 3), 255, np.uint8)
    assert read_print(page) is None


def test_shipped_model_dir_is_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from panelcad.ocr.weights import MODEL_DIRS, model_directory, model_root

    root = tmp_path / "models"
    for folder in MODEL_DIRS:
        model = root / folder
        model.mkdir(parents=True)
        (model / "inference.onnx").write_bytes(b"onnx")
        (model / "inference.yml").write_bytes(b"yml")
    monkeypatch.setenv("PANELCAD_OCR_MODELS", str(root))
    assert model_root() == root
    assert model_directory("PP-OCRv5_mobile_det") == root / "PP-OCRv5_mobile_det_onnx"
    assert model_directory("PP-LCNet_x1_0_textline_ori") == root / "PP-LCNet_x1_0_textline_ori_onnx"


def test_local_target_gate() -> None:
    from panelcad.ocr.bench import counts_as_local_target, machine_role

    assert machine_role("MichellesPC") == "michelles-pc"
    assert machine_role("DESKTOP-3S1HOH0") == "home-pc"
    assert machine_role("cloud-vm") == "other"
    assert counts_as_local_target("MichellesPC") is True
    assert counts_as_local_target("DESKTOP-3S1HOH0") is True
    assert counts_as_local_target("Drew-PC") is False
    assert counts_as_local_target("Pixel 6 Pro") is False
    samples = electrical_samples()
    by_name = {sample.name: sample for sample in samples}
    assert by_name["clean-k1"].in_bank is True
    assert by_name["held-out-bitmap-k1"].in_bank is False
    assert by_name["held-out-stick-k1"].in_bank is False
    assert by_name["held-out-stick-page"].in_bank is False
    held = [sample for sample in samples if not sample.in_bank]
    assert {sample.category for sample in held} >= {"small", "rotated", "low-contrast", "struck", "blueprint", "page"}


def test_grayscale_crop_reaches_predict_as_three_channels() -> None:
    from panelcad.ocr.ppocr import _predict

    seen: dict[str, np.ndarray] = {}

    class Reader:
        def predict(self, image, **_kwargs):
            seen["image"] = image
            return []

    gray = np.zeros((12, 20), np.uint8)
    gray[1, 2] = 40
    assert _predict(Reader(), gray, 32) == []
    image = seen["image"]
    assert image.shape == (12, 20, 3)
    assert image[1, 2, 0] == 40
    assert np.array_equal(image[:, :, 0], image[:, :, 1])
    assert np.array_equal(image[:, :, 0], image[:, :, 2])

    single = gray.reshape(12, 20, 1)
    _predict(Reader(), single, 32)
    assert seen["image"].shape == (12, 20, 3)

    rgba = np.zeros((6, 7, 4), np.uint8)
    rgba[..., 0] = 3
    rgba[..., 1] = 5
    rgba[..., 2] = 7
    rgba[..., 3] = 255
    _predict(Reader(), rgba, 32)
    assert seen["image"].shape == (6, 7, 3)
    assert seen["image"][0, 0].tolist() == [3, 5, 7]

    color = np.zeros((4, 5, 3), np.uint8)
    color[..., 0] = 1
    color[..., 2] = 9
    _predict(Reader(), color, 32)
    assert seen["image"] is color
