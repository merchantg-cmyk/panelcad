"""Deskew, denoise, and auto-rotate a scan. Pure Pillow, no model."""

from __future__ import annotations

import re
from io import BytesIO

from PIL import Image, ImageFilter


def prepare_scan(image: Image.Image) -> tuple[Image.Image, dict[str, float]]:
    """EXIF orientation, 90-degree auto-rotate, small deskew, then denoise.

    Returns the corrected image and the angles that were applied.
    """

    image = ImageOps_exif(image)
    turned, quarter = auto_rotate(image)
    fixed, skew = deskew(turned)
    clean = denoise(fixed)
    return clean, {"quarter_turns": float(quarter), "deskew_deg": skew}


def ImageOps_exif(image: Image.Image) -> Image.Image:
    try:
        from PIL import ImageOps

        return ImageOps.exif_transpose(image)
    except Exception:  # noqa: BLE001
        return image


def denoise(image: Image.Image) -> Image.Image:
    return image.convert("RGB").filter(ImageFilter.MedianFilter(size=3))


def auto_rotate(image: Image.Image) -> tuple[Image.Image, int]:
    """Put the page upright. Returns quarter turns applied (each is 90° CCW).

    A line drawing is decided by reading tags and title words at 0/90/180/270.
    That reading wins over a title-band guess, so an upright sheet is not turned
    over by one faint rule. A ruled title band is used when the reading is not
    decisive, and it is checked before OSD so a sideways OSD cannot leave an
    upside-down band on its edge. Bottom-band ink uses a binarized probe.
    The page stays as scanned when none of those is decisive.
    """

    # Tags and title words decide before a title-band guess, including on a
    # line drawing whose BOX thumbnail looks blank.
    scores = _text_scores(image)
    oriented = _orientation_from_scores(image, scores)
    if oriented is not None:
        return oriented
    titled = _decisive_title(image)
    if titled is not None:
        return titled
    degrees, confidence = _osd_reading(image)
    if confidence >= 4.0 and degrees in {90, 180, 270}:
        return _turn_clockwise(image, degrees)
    if confidence >= 4.0 and degrees == 0:
        return image, 0
    return _ink_rotate(image)


def _title_orientations(image: Image.Image) -> list[tuple[Image.Image, float]]:
    """Each quarter turn and how much longer the bottom rule is than the top."""

    found: list[tuple[Image.Image, float]] = []
    current = image
    for quarter in range(4):
        if quarter:
            current = current.transpose(Image.Transpose.ROTATE_270)
        found.append((current, _title_line_score(current)))
    return found


def _decisive_title(image: Image.Image) -> tuple[Image.Image, int] | None:
    """The turn that puts a sheet-wide rule on the bottom, when one exists."""

    ranked = _title_orientations(image)
    scores = [score for _turned, score in ranked]
    best_q = max(range(4), key=lambda index: scores[index])
    best = scores[best_q]
    second = max(score for index, score in enumerate(scores) if index != best_q)
    if best >= 0.45 and best >= second + 0.25:
        return ranked[best_q][0], best_q
    return None


def _ink_rotate(image: Image.Image) -> tuple[Image.Image, int]:
    """A solid title band at the bottom beats the same band on another edge.

    Used when no sheet-wide rule is decisive. A corner title block still has
    more ink on the bottom than on the top.
    """

    original = _bottom_ink(image)
    best_img = image
    best_q = 0
    best_score = original
    current = image
    for quarter in range(1, 4):
        current = current.transpose(Image.Transpose.ROTATE_270)
        score = _bottom_ink(current)
        if score > best_score:
            best_score = score
            best_img = current
            best_q = quarter
    if best_score > original + 0.008:
        return best_img, best_q
    return image, 0


def _title_line_score(image: Image.Image) -> float:
    """Longest horizontal rule in the bottom band, minus the same in the top.

    A filled pixel count treats a rotated title edge as a title band. A rule
    has to cross the page, so a 90° turn scores near zero and 180° does not.
    """

    probe = _binary_probe(image, 420)
    width, height = probe.size
    if width < 8 or height < 8:
        return 0.0
    pix = probe.tobytes()

    def longest(y0: int, y1: int) -> float:
        best = 0
        start = max(0, y0)
        stop = min(height, max(start + 1, y1))
        for y in range(start, stop):
            run = 0
            row = pix[y * width : (y + 1) * width]
            for value in row:
                if value < 128:
                    run += 1
                    if run > best:
                        best = run
                else:
                    run = 0
        return best / float(width)

    top = longest(int(height * 0.02), int(height * 0.16))
    bottom = longest(int(height * 0.80), int(height * 0.98))
    return bottom - top


def _turn_clockwise(image: Image.Image, degrees: int) -> tuple[Image.Image, int]:
    """Clockwise correction, reported as CCW quarter-turns so callers stay consistent."""

    quarters = {90: 3, 180: 2, 270: 1}.get(int(degrees) % 360, 0)
    turned = image
    for _ in range(quarters):
        turned = turned.transpose(Image.Transpose.ROTATE_270)
    return turned, quarters


_TITLE_WORD = re.compile(
    r"^(?:MOTOR|STARTERS?|ELEMENTARY|DIAGRAM|SHEET|JOB|DATE|DRAWN|POWER|CONTROL|"
    r"WIRE|TITLE|REV|CONTACTOR|OVERLOAD|NEMA|ANSI|IEC|TERMINAL|PANEL|LAYOUT)$"
)
_TAG_LIKE = re.compile(r"^(?:[A-Z]{1,4}\d{1,4}[A-Z]?|\d{1,4}[A-Z]{1,4}(?:-[A-Z0-9]{1,4})?)$")


def _osd_reading(image: Image.Image) -> tuple[int | None, float]:
    """Tesseract OSD. Confidence 0 when the binary is missing or the page is blank."""

    try:
        import pytesseract

        from panelcad.importer.raster import configure_tesseract

        configure_tesseract()
    except ImportError:
        return None, 0.0
    probe = image.convert("L")
    probe.thumbnail((1000, 1000), Image.Resampling.BOX)
    try:
        raw = str(pytesseract.image_to_osd(probe, config="--psm 0") or "")
    except Exception:  # noqa: BLE001 — a missing tessdata or a blank page is not a rotation
        return None, 0.0
    rotate_match = re.search(r"Rotate:\s*(\d+)", raw)
    conf_match = re.search(r"Orientation confidence:\s*([0-9.]+)", raw)
    if not rotate_match or not conf_match:
        return None, 0.0
    try:
        rotate = int(rotate_match.group(1))
        confidence = float(conf_match.group(1))
    except ValueError:
        return None, 0.0
    if rotate not in {0, 90, 180, 270}:
        return None, 0.0
    return rotate, confidence


def _text_orientation(image: Image.Image) -> tuple[Image.Image, int] | None:
    """Pick 0/90/180/270 by which way device tags and title words read.

    Upside-down OCR still emits letters, but not MOTOR or 10M-G. A page with
    no such tokens returns None so the title-band ink test can run.
    """

    return _orientation_from_scores(image, _text_scores(image))


def _text_scores(image: Image.Image) -> list[float]:
    try:
        import pytesseract

        from panelcad.importer.raster import configure_tesseract

        configure_tesseract()
    except ImportError:
        return []
    scores: list[float] = []
    current = image
    for quarter in range(4):
        if quarter:
            current = current.transpose(Image.Transpose.ROTATE_270)
        scores.append(_text_score(current, pytesseract))
    return scores


def _orientation_from_scores(image: Image.Image, scores: list[float]) -> tuple[Image.Image, int] | None:
    if len(scores) != 4:
        return None
    best_q = max(range(4), key=lambda index: scores[index])
    best = scores[best_q]
    if best < 80.0:
        return None
    second = max(score for index, score in enumerate(scores) if index != best_q)
    if best < second + 60.0:
        return None
    turned = image
    for _ in range(best_q):
        turned = turned.transpose(Image.Transpose.ROTATE_270)
    return turned, best_q


def _text_score(image: Image.Image, pytesseract: object) -> float:
    """Score tags and title words on a probe large enough to read a line drawing.

    An 800 px thumbnail of a wide sheet scores nothing, so a top rule would
    decide the rotation. The long side stays near 2400 px.
    """

    probe = image.convert("L")
    long_side = max(probe.size)
    # Shrink a large sheet, and enlarge a small scan, so tags stay readable.
    if long_side >= 800 and abs(long_side - 2400) > 1:
        scale = 2400.0 / float(long_side)
        probe = probe.resize(
            (max(1, int(round(probe.width * scale))), max(1, int(round(probe.height * scale)))),
            Image.Resampling.LANCZOS,
        )
    try:
        data = pytesseract.image_to_data(probe, output_type=pytesseract.Output.DICT, config="--psm 11")
    except Exception:  # noqa: BLE001 — missing binary or a blank crop is not a rotation
        return 0.0
    total = 0.0
    for raw, conf in zip(data.get("text") or [], data.get("conf") or []):
        text = str(raw or "").strip().upper()
        try:
            confidence = float(conf)
        except (TypeError, ValueError):
            continue
        if confidence < 40 or not text:
            continue
        if _TAG_LIKE.match(text) or _TITLE_WORD.match(text):
            total += confidence
    return total


def _dark_fraction(image: Image.Image) -> float:
    probe = image.convert("L")
    probe.thumbnail((180, 180), Image.Resampling.BOX)
    pix = probe.tobytes()
    if not pix:
        return 0.0
    dark = sum(1 for value in pix if value < 128)
    return dark / len(pix)


def deskew(image: Image.Image, *, limit: float = 5.0, step: float = 0.5) -> tuple[Image.Image, float]:
    """Rotate by a small angle so ruled lines run horizontally. Angle is degrees CCW."""

    probe = _binary_probe(image, 520)
    best_angle = 0.0
    best_var = _row_variance(probe)
    angle = -limit
    while angle <= limit + 1e-6:
        if abs(angle) > 1e-6:
            trial = probe.rotate(angle, resample=Image.Resampling.NEAREST, expand=False, fillcolor=255)
            score = _row_variance(trial)
            if score > best_var * 1.04:
                best_var = score
                best_angle = angle
        angle += step
    if abs(best_angle) < 1e-6:
        return image.convert("RGB"), 0.0
    fixed = image.convert("RGB").rotate(
        best_angle,
        resample=Image.Resampling.BICUBIC,
        expand=False,
        fillcolor=(255, 255, 255),
    )
    return fixed, best_angle


def degrade_scan(
    image: Image.Image,
    *,
    quarter_turns: int = 1,
    skew_deg: float = 1.7,
    jpeg_quality: int = 42,
    blur: float = 1.1,
    stain: bool = False,
    fold: bool = False,
) -> Image.Image:
    """Turn a clean render into a fake shop scan: rotate, skew, blur, noise, JPEG.

    A stain or fold is drawn on the upright page, in the drawing area, before the rotation.
    """

    image = image.convert("RGB")
    if stain:
        image = _coffee_stain(image)
    if fold:
        image = _fold_line(image)
    turns = quarter_turns % 4
    for _ in range(turns):
        image = image.transpose(Image.Transpose.ROTATE_270)
    if abs(skew_deg) > 0.05:
        image = image.rotate(skew_deg, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(255, 255, 255))
    if blur > 0.05:
        image = image.filter(ImageFilter.GaussianBlur(radius=blur))
    image = _speckle(image, amount=18 if not stain else 26)
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=jpeg_quality)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def _binary_probe(image: Image.Image, width: int) -> Image.Image:
    gray = image.convert("L")
    gray.thumbnail((width, width), Image.Resampling.BOX)
    return gray.point(lambda value: 0 if value < 200 else 255)


def _bottom_ink(image: Image.Image) -> float:
    """Ink in the bottom title-block band minus ink along the top margin.

    The probe is binarized, so a thin rule counts. A BOX thumbnail does not.
    """

    probe = _binary_probe(image, 420)
    width, height = probe.size
    pix = probe.tobytes()

    def band(y0: int, y1: int) -> float:
        total = 0
        count = 0
        for y in range(max(0, y0), min(height, y1)):
            row = pix[y * width : (y + 1) * width]
            total += sum(1 for value in row if value < 128)
            count += width
        return total / max(count, 1)

    top = band(int(height * 0.02), int(height * 0.14))
    bottom = band(int(height * 0.82), int(height * 0.98))
    return bottom - top


def _row_variance(image: Image.Image) -> float:
    width, height = image.size
    if height < 4 or width < 4:
        return 0.0
    pix = image.tobytes()
    rows = [sum(1 for value in pix[y * width : (y + 1) * width] if value < 128) for y in range(height)]
    mean = sum(rows) / len(rows)
    return sum((value - mean) ** 2 for value in rows) / len(rows)


def _speckle(image: Image.Image, *, amount: int) -> Image.Image:
    import random

    out = image.copy()
    pix = out.load()
    width, height = out.size
    rnd = random.Random(7)
    step = 3
    for y in range(0, height, step):
        for x in range(0, width, step):
            red, green, blue = pix[x, y]
            delta = rnd.randint(-amount, amount)
            pix[x, y] = (_clamp(red + delta), _clamp(green + delta), _clamp(blue + delta))
    return out


def _clamp(value: int) -> int:
    return 0 if value < 0 else 255 if value > 255 else value


def _coffee_stain(image: Image.Image) -> Image.Image:
    """A brown ring in the upper-middle of the sheet, clear of the title block."""

    from PIL import ImageDraw

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    width, height = image.size
    cx, cy = int(width * 0.62), int(height * 0.38)
    radius = int(min(width, height) * 0.08)
    box = (cx - radius, cy - radius, cx + radius, cy + radius)
    draw.ellipse(box, fill=(92, 52, 24, 70))
    inner = int(radius * 0.55)
    draw.ellipse((cx - inner, cy - inner, cx + inner, cy + inner), fill=(255, 255, 255, 0))
    stained = Image.alpha_composite(image.convert("RGBA"), overlay)
    return stained.convert("RGB")


def _fold_line(image: Image.Image) -> Image.Image:
    """A vertical crease through the drawing, stopping above the title band."""

    from PIL import ImageDraw

    out = image.convert("RGB")
    draw = ImageDraw.Draw(out)
    width, height = out.size
    x = int(width * 0.46)
    y0 = int(height * 0.12)
    y1 = int(height * 0.72)
    draw.line((x, y0, x, y1), fill=(186, 176, 160), width=max(2, width // 400))
    return out
