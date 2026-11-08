"""Synthetic electrical pages for the OCR benchmark and the engine tests.

The drawings are generated here. They are not scanned from a published sheet.
Two cohorts:

* ``in_bank`` uses the DejaVu files in ``tests/fonts``. Those faces are also
  prototype glyphs, so a high score there is a same-font check.
* ``held_out`` is the fair comparison. It is drawn with a stick alphabet
  written in this file, and with Pillow's built-in bitmap font. Neither is
  loaded into the prototype bank. Held-out pages include small type, a quarter
  turn, low contrast, a rule through the glyphs, a symbol box, and a blue sheet.
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
    in_bank: bool = True


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
        *_held_out_samples(),
    ]
    return [sample for sample in samples if sample is not None]


def _held_out_samples() -> list[OcrSample]:
    """Pages whose renderer is not a prototype font."""

    found = [
        _bitmap_label("held-out-bitmap-k1", "held-out", "K1", "Pillow built-in font, JPEG. Not in the bank.", size=28, jpeg=True),
        _bitmap_label("held-out-bitmap-small-fu1", "small", "FU1", "Pillow built-in font, small.", size=14),
        _bitmap_label("held-out-bitmap-blueprint-q1", "blueprint", "Q1", "Pillow built-in font on a blue sheet.", size=28, blueprint=True),
        _bitmap_label("held-out-bitmap-low-m1", "low-contrast", "M1", "Pillow built-in font, gray on gray.", size=28, low_contrast=True),
        _bitmap_label("held-out-bitmap-struck-104", "struck", "104", "Pillow built-in font with a rule through the digits.", size=28, strike=True),
        _bitmap_label("held-out-bitmap-rotated-w12", "rotated", "W12", "Pillow built-in font, turned 90 degrees clockwise.", size=28, rotate=True),
        _stick_label("held-out-stick-k1", "held-out", "K1", "Stick alphabet. Not a bank font."),
        _stick_label("held-out-stick-cr12", "held-out", "CR12", "Stick alphabet, multi-letter tag."),
        _stick_label("held-out-stick-tb1", "held-out", "TB1", "Stick alphabet, terminal tag."),
        _stick_label("held-out-stick-10m", "held-out", "10M", "Stick alphabet, number-first tag."),
        _stick_label("held-out-stick-104", "wire", "104", "Stick alphabet, wire number."),
        _stick_label("held-out-stick-title", "title", "SHEET 3 OF 7", "Stick alphabet title line.", truths=("SHEET", "3", "OF", "7"), wide=True),
        _stick_label("held-out-stick-struck-104", "struck", "104", "Stick digits with a rule through them.", strike=True),
        _stick_label("held-out-stick-blueprint-q1", "blueprint", "Q1", "Stick tag on a blue sheet.", blueprint=True),
        _stick_label("held-out-stick-low-m1", "low-contrast", "M1", "Stick tag, gray on gray.", low_contrast=True),
        _stick_label("held-out-stick-small-fu1", "small", "FU1", "Stick tag at about half the usual size.", scale=2, thickness=1),
        _stick_label("held-out-stick-rotated-w12", "rotated", "W12", "Stick tag turned 90 degrees clockwise.", rotate=True),
        _stick_page(),
    ]
    return [sample for sample in found if sample is not None]


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


def _bitmap_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _bitmap_label(
    name: str,
    category: str,
    text: str,
    note: str,
    *,
    size: int,
    jpeg: bool = False,
    blueprint: bool = False,
    low_contrast: bool = False,
    strike: bool = False,
    rotate: bool = False,
) -> OcrSample:
    """Pillow's built-in font. It is not one of the prototype faces."""

    if blueprint:
        bg, fg = (25, 55, 150), (245, 245, 245)
    elif low_contrast:
        bg, fg = (196, 196, 196), (150, 150, 150)
    else:
        bg, fg = (255, 255, 255), (10, 10, 10)
    image = Image.new("RGB", (max(280, 18 * len(text) + 40), 96), bg)
    draw = ImageDraw.Draw(image)
    draw.text((18, 28), text, fill=fg, font=_bitmap_font(size))
    arr = np.asarray(image)
    if strike:
        mid = arr.shape[0] // 2
        arr = arr.copy()
        arr[mid - 1 : mid + 2, 8 : arr.shape[1] - 8] = fg
    if jpeg:
        encoded = io.BytesIO()
        Image.fromarray(arr).save(encoded, format="JPEG", quality=40)
        encoded.seek(0)
        arr = np.asarray(Image.open(encoded).convert("RGB"))
    if rotate:
        arr = cv2.rotate(arr, cv2.ROTATE_90_CLOCKWISE)
    return OcrSample(name, category, arr, (text,), note, in_bank=False)


# Unit-cell strokes, 10 wide by 14 tall, origin at the top left. This alphabet
# is drawn here. It is not DejaVu and not an OpenCV Hershey face.
_STICK: dict[str, tuple[tuple[tuple[int, int], tuple[int, int]], ...]] = {
    "0": (((2, 2), (8, 2)), ((8, 2), (8, 12)), ((8, 12), (2, 12)), ((2, 12), (2, 2))),
    "1": (((5, 2), (5, 12)), ((3, 4), (5, 2)), ((3, 12), (7, 12))),
    "2": (((2, 2), (8, 2)), ((8, 2), (8, 7)), ((8, 7), (2, 7)), ((2, 7), (2, 12)), ((2, 12), (8, 12))),
    "3": (((2, 2), (8, 2)), ((8, 2), (8, 12)), ((8, 12), (2, 12)), ((2, 7), (8, 7))),
    "4": (((2, 2), (2, 7)), ((2, 7), (8, 7)), ((8, 2), (8, 12))),
    "5": (((8, 2), (2, 2)), ((2, 2), (2, 7)), ((2, 7), (8, 7)), ((8, 7), (8, 12)), ((8, 12), (2, 12))),
    "6": (((8, 2), (2, 2)), ((2, 2), (2, 12)), ((2, 12), (8, 12)), ((8, 12), (8, 7)), ((8, 7), (2, 7))),
    "7": (((2, 2), (8, 2)), ((8, 2), (4, 12))),
    "8": (((2, 2), (8, 2)), ((8, 2), (8, 12)), ((8, 12), (2, 12)), ((2, 12), (2, 2)), ((2, 7), (8, 7))),
    "9": (((8, 12), (8, 2)), ((8, 2), (2, 2)), ((2, 2), (2, 7)), ((2, 7), (8, 7))),
    "A": (((1, 12), (5, 2)), ((5, 2), (9, 12)), ((3, 8), (7, 8))),
    "B": (((2, 2), (2, 12)), ((2, 2), (7, 2)), ((7, 2), (7, 7)), ((7, 7), (2, 7)), ((2, 7), (8, 7)), ((8, 7), (8, 12)), ((8, 12), (2, 12))),
    "C": (((8, 2), (2, 2)), ((2, 2), (2, 12)), ((2, 12), (8, 12))),
    "D": (((2, 2), (2, 12)), ((2, 2), (6, 2)), ((6, 2), (8, 4)), ((8, 4), (8, 10)), ((8, 10), (6, 12)), ((6, 12), (2, 12))),
    "E": (((8, 2), (2, 2)), ((2, 2), (2, 12)), ((2, 12), (8, 12)), ((2, 7), (6, 7))),
    "F": (((8, 2), (2, 2)), ((2, 2), (2, 12)), ((2, 7), (6, 7))),
    "G": (((8, 2), (2, 2)), ((2, 2), (2, 12)), ((2, 12), (8, 12)), ((8, 12), (8, 7)), ((8, 7), (5, 7))),
    "H": (((2, 2), (2, 12)), ((8, 2), (8, 12)), ((2, 7), (8, 7))),
    "I": (((3, 2), (7, 2)), ((5, 2), (5, 12)), ((3, 12), (7, 12))),
    "J": (((3, 2), (8, 2)), ((6, 2), (6, 10)), ((6, 10), (2, 12))),
    "K": (((2, 2), (2, 12)), ((8, 2), (2, 7)), ((2, 7), (8, 12))),
    "L": (((2, 2), (2, 12)), ((2, 12), (8, 12))),
    "M": (((1, 12), (1, 2)), ((1, 2), (5, 8)), ((5, 8), (9, 2)), ((9, 2), (9, 12))),
    "N": (((2, 12), (2, 2)), ((2, 2), (8, 12)), ((8, 12), (8, 2))),
    "O": (((2, 2), (8, 2)), ((8, 2), (8, 12)), ((8, 12), (2, 12)), ((2, 12), (2, 2))),
    "P": (((2, 12), (2, 2)), ((2, 2), (8, 2)), ((8, 2), (8, 7)), ((8, 7), (2, 7))),
    "Q": (((2, 2), (8, 2)), ((8, 2), (8, 12)), ((8, 12), (2, 12)), ((2, 12), (2, 2)), ((5, 8), (9, 13))),
    "R": (((2, 12), (2, 2)), ((2, 2), (8, 2)), ((8, 2), (8, 7)), ((8, 7), (2, 7)), ((4, 7), (8, 12))),
    "S": (((8, 2), (2, 2)), ((2, 2), (2, 7)), ((2, 7), (8, 7)), ((8, 7), (8, 12)), ((8, 12), (2, 12))),
    "T": (((1, 2), (9, 2)), ((5, 2), (5, 12))),
    "U": (((2, 2), (2, 12)), ((2, 12), (8, 12)), ((8, 12), (8, 2))),
    "V": (((1, 2), (5, 12)), ((5, 12), (9, 2))),
    "W": (((1, 2), (3, 12)), ((3, 12), (5, 6)), ((5, 6), (7, 12)), ((7, 12), (9, 2))),
    "X": (((2, 2), (8, 12)), ((8, 2), (2, 12))),
    "Y": (((1, 2), (5, 7)), ((9, 2), (5, 7)), ((5, 7), (5, 12))),
    "Z": (((2, 2), (8, 2)), ((8, 2), (2, 12)), ((2, 12), (8, 12))),
}


def _stick_advance(char: str, scale: int) -> int:
    # A tag is set tight, the way a drawing sets it. A wide gap was splitting
    # letters the reader then scored as separate words.
    if char == " ":
        return 8 * scale
    return 10 * scale


def _draw_stick(
    image: np.ndarray,
    x: int,
    y: int,
    text: str,
    *,
    scale: int,
    color: tuple[int, int, int],
    thickness: int,
) -> None:
    cursor = x
    for char in text:
        strokes = _STICK.get(char)
        if strokes is None:
            cursor += _stick_advance(char, scale)
            continue
        for (x0, y0), (x1, y1) in strokes:
            cv2.line(
                image,
                (cursor + x0 * scale, y + y0 * scale),
                (cursor + x1 * scale, y + y1 * scale),
                color,
                thickness,
                cv2.LINE_8,
            )
        cursor += _stick_advance(char, scale)


def _stick_canvas(
    text: str,
    *,
    scale: int = 3,
    thickness: int = 2,
    blueprint: bool = False,
    low_contrast: bool = False,
    strike: bool = False,
    symbol: bool = False,
) -> np.ndarray:
    pad = 16
    width = pad * 2 + sum(_stick_advance(char, scale) for char in text) + 8
    height = pad * 2 + 14 * scale + 8
    if blueprint:
        bg, fg = (25, 55, 150), (245, 245, 245)
    elif low_contrast:
        bg, fg = (196, 196, 196), (148, 148, 148)
    else:
        bg, fg = (255, 255, 255), (10, 10, 10)
    image = np.full((height, max(width, 64), 3), bg, np.uint8)
    if symbol:
        cv2.rectangle(image, (8, 8), (image.shape[1] - 8, image.shape[0] - 8), fg, 2)
    _draw_stick(image, pad, pad, text, scale=scale, color=fg, thickness=thickness)
    if strike:
        mid = pad + 7 * scale
        cv2.line(image, (8, mid), (image.shape[1] - 8, mid), fg, max(2, thickness), cv2.LINE_8)
    return image


def _stick_label(
    name: str,
    category: str,
    text: str,
    note: str,
    *,
    truths: tuple[str, ...] | None = None,
    scale: int = 3,
    thickness: int = 2,
    blueprint: bool = False,
    low_contrast: bool = False,
    strike: bool = False,
    symbol: bool = False,
    rotate: bool = False,
    wide: bool = False,
) -> OcrSample:
    del wide
    image = _stick_canvas(
        text,
        scale=scale,
        thickness=thickness,
        blueprint=blueprint,
        low_contrast=low_contrast,
        strike=strike,
        symbol=symbol,
    )
    if rotate:
        image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    tokens = truths if truths is not None else (text,)
    return OcrSample(name, category, image, tokens, note, in_bank=False)


def _stick_page() -> OcrSample:
    """Tags beside symbol boxes, drawn with the stick alphabet."""

    image = np.full((280, 760, 3), 255, np.uint8)
    cv2.line(image, (40, 70), (720, 70), (0, 0, 0), 2, cv2.LINE_8)
    cv2.rectangle(image, (48, 90), (130, 170), (0, 0, 0), 2)
    _draw_stick(image, 56, 188, "K1", scale=3, color=(0, 0, 0), thickness=2)
    cv2.rectangle(image, (250, 90), (332, 170), (0, 0, 0), 2)
    _draw_stick(image, 258, 188, "M1", scale=3, color=(0, 0, 0), thickness=2)
    _draw_stick(image, 160, 28, "104", scale=3, color=(0, 0, 0), thickness=2)
    cv2.line(image, (150, 28 + 21), (250, 28 + 21), (0, 0, 0), 2, cv2.LINE_8)
    _draw_stick(image, 500, 180, "TB1", scale=3, color=(0, 0, 0), thickness=2)
    return OcrSample(
        "held-out-stick-page",
        "page",
        image,
        ("K1", "M1", "104", "TB1"),
        "Stick tags beside symbol boxes, one wire number crossed by a rule.",
        in_bank=False,
    )
