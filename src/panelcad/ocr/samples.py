"""Synthetic electrical pages for the OCR benchmark and the engine tests.

The drawings are generated here. They are not scanned from a published sheet,
so the benchmark does not need a copyrighted print. Clean samples use the
DejaVu files in ``tests/fonts`` (those faces are also in the prototype bank).
The bitmap sample uses Pillow's built-in font, which is not in the bank.
Degraded samples start from DejaVu and then add blur, a rule, a blue sheet,
or a quarter turn.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class OcrSample:
    name: str
    category: str
    image: np.ndarray
    truths: tuple[str, ...]
    note: str


def electrical_samples() -> list[OcrSample]:
    font = _dejavu(36)
    small = _dejavu(16)
    samples = [
        _label("clean-k1", "clean", "K1", font, "DejaVu tag, in the prototype bank."),
        _label("clean-cr12", "clean", "CR12", font, "Multi-letter IEC tag."),
        _label("clean-tb1", "clean", "TB1", font, "Terminal strip tag."),
        _label("clean-10m", "clean", "10M", font, "Number-first device tag."),
        _label("wire-104", "wire", "104", font, "Wire number with no rule through it."),
        _title_line(),
        _struck("struck-104", "104", font),
        _blueprint("blueprint-q1", "Q1", font),
        _low_contrast("low-contrast-m1", "M1", font),
        _label("small-fu1", "small", "FU1", small, "Cap height about 16 px."),
        _rotated("rotated-w12", "W12", font),
        _page(),
        _bitmap_held_out(),
    ]
    return [sample for sample in samples if sample is not None]


def _dejavu(size: int, *, bold: bool = False) -> ImageFont.ImageFont | None:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for root in _font_roots():
        path = root / name
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return None


def _font_roots() -> list[Path]:
    here = Path(__file__).resolve()
    return [here.parents[3] / "tests" / "fonts", Path.cwd() / "tests" / "fonts"]


def _label(
    name: str,
    category: str,
    text: str,
    font: ImageFont.ImageFont | None,
    note: str,
    *,
    size: tuple[int, int] = (280, 96),
) -> OcrSample | None:
    if font is None:
        return None
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.text((18, 24), text, fill=(10, 10, 10), font=font)
    return OcrSample(name, category, np.asarray(image), (text,), note)


def _title_line() -> OcrSample | None:
    font = _dejavu(28)
    if font is None:
        return None
    image = Image.new("RGB", (520, 90), (255, 255, 255))
    ImageDraw.Draw(image).text((16, 24), "SHEET 3 OF 7", fill=(10, 10, 10), font=font)
    return OcrSample(
        "title-sheet",
        "title",
        np.asarray(image),
        ("SHEET", "3", "OF", "7"),
        "Title-block line. Each word is a truth token.",
    )


def _struck(name: str, text: str, font: ImageFont.ImageFont | None) -> OcrSample | None:
    sample = _label(name, "struck", text, font, "A horizontal rule crosses the digits.")
    if sample is None:
        return None
    image = sample.image.copy()
    mid = image.shape[0] // 2
    image[mid - 1 : mid + 2, 8 : image.shape[1] - 8] = (0, 0, 0)
    return OcrSample(sample.name, sample.category, image, sample.truths, sample.note)


def _blueprint(name: str, text: str, font: ImageFont.ImageFont | None) -> OcrSample | None:
    if font is None:
        return None
    image = Image.new("RGB", (280, 96), (25, 55, 150))
    ImageDraw.Draw(image).text((18, 24), text, fill=(245, 245, 245), font=font)
    return OcrSample(
        name,
        "blueprint",
        np.asarray(image),
        (text,),
        "Light glyphs on a blue sheet.",
    )


def _low_contrast(name: str, text: str, font: ImageFont.ImageFont | None) -> OcrSample | None:
    if font is None:
        return None
    image = Image.new("RGB", (280, 96), (196, 196, 196))
    ImageDraw.Draw(image).text((18, 24), text, fill=(150, 150, 150), font=font)
    return OcrSample(
        name,
        "low-contrast",
        np.asarray(image),
        (text,),
        "Gray glyphs on a gray sheet.",
    )


def _rotated(name: str, text: str, font: ImageFont.ImageFont | None) -> OcrSample | None:
    sample = _label(name, "rotated", text, font, "The crop is turned 90 degrees clockwise.", size=(240, 80))
    if sample is None:
        return None
    turned = cv2.rotate(sample.image, cv2.ROTATE_90_CLOCKWISE)
    return OcrSample(sample.name, sample.category, turned, sample.truths, sample.note)


def _page() -> OcrSample | None:
    font = _dejavu(26)
    if font is None:
        return None
    image = Image.new("RGB", (720, 280), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.line((40, 70, 680, 70), fill=(0, 0, 0), width=2)
    draw.rectangle((48, 90, 120, 160), outline=(0, 0, 0), width=2)
    draw.text((56, 168), "K1", fill=(0, 0, 0), font=font)
    draw.rectangle((240, 90, 312, 160), outline=(0, 0, 0), width=2)
    draw.text((248, 168), "M1", fill=(0, 0, 0), font=font)
    draw.text((160, 40), "104", fill=(0, 0, 0), font=font)
    draw.text((480, 180), "TB1", fill=(0, 0, 0), font=font)
    return OcrSample(
        "page-tags",
        "page",
        np.asarray(image),
        ("K1", "M1", "104", "TB1"),
        "Tags beside symbol boxes, plus a wire number on a rule.",
    )


def _bitmap_held_out() -> OcrSample | None:
    try:
        font = ImageFont.load_default(size=28)
    except TypeError:
        font = ImageFont.load_default()
    image = Image.new("RGB", (280, 96), (255, 255, 255))
    ImageDraw.Draw(image).text((18, 28), "K1", fill=(0, 0, 0), font=font)
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG", quality=40)
    encoded.seek(0)
    jpeg = np.asarray(Image.open(encoded).convert("RGB"))
    return OcrSample(
        "held-out-bitmap-k1",
        "held-out",
        jpeg,
        ("K1",),
        "Pillow's built-in font, JPEG'd. That face is not in the prototype bank.",
    )
