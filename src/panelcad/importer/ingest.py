"""Ingest a PDF, a DXF, an SVG, or scan images into one workspace page per sheet."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from panelcad.importer.imageutil import auto_rotate, prepare_scan
from panelcad.importer.read import dxf_pages, release_mupdf_store, tokens_from_page
from panelcad.importer.vectors import strokes_from_page
from panelcad.importer.workspace import Workspace

PRINT_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".dxf", ".svg"}
PRINT_FILE_FILTER = (
    "Prints (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.dxf *.svg);;All files (*.*)"
)
RASTER_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def plain_print_error(exc: BaseException, path: Path | str) -> str:
    """One sentence for a file the importer cannot read. No traceback and no library text."""

    file = Path(path)
    suffix = file.suffix.lower()
    text = str(exc).lower()
    if suffix == ".svg":
        return "This file could not be read as an SVG."
    if "password" in text or "encrypted" in text:
        return "This PDF is password protected."
    if "empty" in text:
        return "The file is empty or not a PDF."
    if suffix == ".pdf" or "as type pdf" in text or "cannot open" in text:
        return "This file could not be read as a PDF."
    if suffix not in PRINT_EXTS:
        return f"{file.name} is not a supported print. Use a PDF, a photo or scan, a DXF, or an SVG."
    return "This file could not be read as a print."


def pdf_page_is_scan(page: Any) -> bool:
    """A scan is an image that covers most of the page.

    A title-block logo, or one compound path plus a small image, stays vector.
    Real words in the drawing area the image does not cover keep the text layer.
    """

    try:
        rect = page.rect
        page_area = abs(float(rect.width) * float(rect.height))
    except (AttributeError, TypeError, ValueError):
        return False
    if page_area <= 0:
        return False
    try:
        infos = list(page.get_image_info() or [])
    except Exception:  # noqa: BLE001 — a page we cannot inspect is not treated as a scan
        infos = []
    boxes: list[tuple[float, float, float, float]] = []
    largest = 0.0
    for info in infos:
        if not isinstance(info, dict):
            continue
        bbox = info.get("bbox")
        if bbox is None:
            continue
        try:
            x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        except (TypeError, ValueError, IndexError):
            continue
        left, right = min(x0, x1), max(x0, x1)
        top, bottom = min(y0, y1), max(y0, y1)
        area = (right - left) * (bottom - top)
        if area <= 0:
            continue
        largest = max(largest, area)
        boxes.append((left, top, right, bottom))
    if largest < page_area * 0.60:
        return False
    if _words_outside_images(page, boxes):
        return False
    return True


def _words_outside_images(page: Any, boxes: list[tuple[float, float, float, float]]) -> bool:
    """True when the text layer has real words the scan image does not cover."""

    try:
        words = list(page.get_text("words") or [])
    except Exception:  # noqa: BLE001
        return False
    found = 0
    for word in words:
        try:
            x0, y0, x1, y1, text = word[:5]
            cx = (float(x0) + float(x1)) * 0.5
            cy = (float(y0) + float(y1)) * 0.5
        except (TypeError, ValueError, IndexError):
            continue
        label = str(text or "").strip()
        if len(label) < 2:
            continue
        if any(left - 1.0 <= cx <= right + 1.0 and top - 1.0 <= cy <= bottom + 1.0 for left, top, right, bottom in boxes):
            continue
        found += 1
        if found >= 6:
            return True
    return False


def open_print_pdf(path: Path | str) -> Any:
    """Open a PDF or raise a plain-language ValueError."""

    import pymupdf

    file = Path(path)
    try:
        doc = pymupdf.open(str(file))
    except Exception as exc:  # noqa: BLE001 — pymupdf names the library, not the problem
        raise ValueError(plain_print_error(exc, file)) from None
    if getattr(doc, "needs_pass", False) or (
        getattr(doc, "is_encrypted", False) and getattr(doc, "needs_pass", True)
    ):
        # needs_pass is the signal. An encrypted file that still needs a password
        # cannot be read. A file that opened cleanly is not this case.
        if getattr(doc, "needs_pass", False):
            doc.close()
            raise ValueError("This PDF is password protected.")
    if int(getattr(doc, "page_count", 0) or 0) <= 0:
        doc.close()
        raise ValueError("The file is empty or not a PDF.")
    return doc


def is_print_path(path: Path | str) -> bool:
    return Path(path).suffix.lower() in PRINT_EXTS


def ingest_paths(
    paths: list[Path | str],
    workspace: Workspace,
    *,
    preview_dpi: int = 140,
    dpi: float | None = None,
    sheet_mm: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    index = 0
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            pages.extend(
                _ingest_pdf(
                    path,
                    workspace,
                    start=index,
                    preview_dpi=preview_dpi,
                    sheet_mm=sheet_mm,
                )
            )
        elif suffix == ".dxf":
            pages.extend(_ingest_dxf(path, workspace, index=index))
        elif suffix == ".svg":
            pages.append(_ingest_svg(path, workspace, index=index))
        elif suffix in RASTER_EXTS:
            pages.append(_ingest_image(path, workspace, index=index, dpi=dpi, sheet_mm=sheet_mm))
        else:
            raise ValueError(plain_print_error(ValueError(path.name), path))
        index = len(pages)
    return pages


def rasterize_pdf(path: Path | str, dest_dir: Path, *, dpi: int = 170) -> list[Path]:
    """Write one PNG per PDF page. Used to build fake scans from our own prints."""

    import pymupdf

    dest_dir.mkdir(parents=True, exist_ok=True)
    doc = open_print_pdf(path)
    written: list[Path] = []
    try:
        zoom = dpi / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        for index, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            out = dest_dir / f"page_{index + 1:03d}.png"
            try:
                pix.save(str(out))
            finally:
                release_pixmap(pix)
            written.append(out)
    finally:
        doc.close()
        release_mupdf_store()
    return written


def _ingest_pdf(
    path: Path,
    workspace: Workspace,
    *,
    start: int,
    preview_dpi: int,
    sheet_mm: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    doc = open_print_pdf(path)
    pages: list[dict[str, Any]] = []
    try:
        for offset, _page in enumerate(doc):
            index = start + offset
            page_id = f"page_{index + 1:03d}"
            page_obj = doc[offset]
            try:
                tokens, meta = tokens_from_page(page_obj)
            except Exception as exc:  # noqa: BLE001 — the open already passed; this is a bad page
                raise ValueError(plain_print_error(exc, path)) from None
            if pdf_page_is_scan(doc[offset]):
                # A scanner text layer is not the drawing. Local OCR reads the image.
                meta = dict(meta)
                meta["vector"] = False
                meta["text"] = ""
                tokens = []
            folder = workspace.page_dir(page_id)
            preview = folder / "page.png"
            raster_dpi = preview_dpi if meta.get("vector") else _native_scan_dpi(page_obj, preview_dpi)
            _save_pdf_preview(page_obj, preview, raster_dpi)
            if not meta.get("vector"):
                image = Image.open(preview)
                turned = image
                try:
                    turned, quarters = auto_rotate(image)
                    applied = int(quarters) * 90 % 360
                    if applied:
                        _save_upright(turned, preview, image)
                finally:
                    image.close()
                    if turned is not image:
                        closer = getattr(turned, "close", None)
                        if callable(closer):
                            closer()
                meta = dict(meta)
                meta["rotation"] = applied
                if applied in {90, 270}:
                    meta["width_mm"], meta["height_mm"] = meta["height_mm"], meta["width_mm"]
            if sheet_mm:
                meta = dict(meta)
                meta["width_mm"], meta["height_mm"] = float(sheet_mm[0]), float(sheet_mm[1])
            record = _page_record(
                page_id,
                index=index,
                source=str(path),
                meta=meta,
                image=str(preview),
                vector=bool(meta.get("vector")),
                tokens=tokens,
            )
            record["dpi"] = int(raster_dpi)
            record["pdf_page"] = offset
            workspace.write_json(tokens, "pages", page_id, "words.json")
            workspace.write_json(strokes_from_page(page_obj), "pages", page_id, "strokes.json")
            workspace.write_json(record, "pages", page_id, "page.json")
            pages.append(record)
    finally:
        doc.close()
        release_mupdf_store()
    return pages


def _native_scan_dpi(page: Any, preview_dpi: int) -> int:
    """DPI of an embedded scan. Vector pages keep the preview DPI."""

    infos: list[Any] = []
    try:
        infos = list(page.get_image_info() or [])
    except Exception:  # noqa: BLE001 — a page without images stays at the preview DPI
        infos = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        try:
            xres = float(info.get("xres") or 0)
        except (TypeError, ValueError):
            xres = 0
        if xres >= 150:
            return int(round(xres))
    try:
        rect_w = float(page.rect.width) or 0
    except (AttributeError, TypeError, ValueError):
        rect_w = 0
    if rect_w > 0:
        for info in infos:
            if not isinstance(info, dict):
                continue
            try:
                width = float(info.get("width") or 0)
            except (TypeError, ValueError):
                continue
            if width <= 0:
                continue
            dpi = width / (rect_w / 72.0)
            if dpi >= 150:
                return int(round(dpi))
    return int(preview_dpi)


def release_pixmap(pix: Any) -> None:
    """Free a MuPDF pixmap buffer. Saving it does not release the pixels."""

    if pix is None:
        return
    closer = getattr(pix, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001 — dropping the reference still frees it
            pass


def _save_pdf_preview(page: Any, dest: Path, dpi: int) -> None:
    zoom = dpi / 72.0
    import pymupdf

    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    try:
        pix.save(str(dest))
    finally:
        release_pixmap(pix)


def _ingest_dxf(path: Path, workspace: Workspace, *, index: int) -> list[dict[str, Any]]:
    """One workspace page per DXF layout. Geometry is stored so symbols can match.

    Each layout's strokes already include conductors from the resolved lineweight.
    """

    records: list[dict[str, Any]] = []
    try:
        found = dxf_pages(path)
    except Exception as exc:  # noqa: BLE001 — ezdxf names the library, not the problem
        raise ValueError("This file could not be read as a DXF.") from exc
    for offset, page in enumerate(found):
        page_index = index + offset
        page_id = f"page_{page_index + 1:03d}"
        tokens = list(page["tokens"])
        meta = dict(page["meta"])
        folder = workspace.page_dir(page_id)
        preview = folder / "page.png"
        _blank_preview(preview, meta["width_mm"], meta["height_mm"])
        record = _page_record(
            page_id,
            index=page_index,
            source=str(path),
            meta=meta,
            image=str(preview),
            vector=bool(meta.get("vector")),
            tokens=tokens,
        )
        workspace.write_json(tokens, "pages", page_id, "words.json")
        workspace.write_json(list(page.get("strokes") or []), "pages", page_id, "strokes.json")
        workspace.write_json(record, "pages", page_id, "page.json")
        records.append(record)
    return records


def _ingest_svg(path: Path, workspace: Workspace, *, index: int) -> dict[str, Any]:
    """Vector SVG. Lines and text are read from the file, not from a photo."""

    from panelcad.importer.svg import read_svg

    page_id = f"page_{index + 1:03d}"
    tokens, strokes, meta = read_svg(path)
    folder = workspace.page_dir(page_id)
    preview = folder / "page.png"
    _blank_preview(preview, float(meta["width_mm"]), float(meta["height_mm"]))
    record = _page_record(
        page_id,
        index=index,
        source=str(path),
        meta=meta,
        image=str(preview),
        vector=True,
        tokens=tokens,
    )
    if meta.get("svg_notice"):
        record["svg_notice"] = meta["svg_notice"]
    workspace.write_json(tokens, "pages", page_id, "words.json")
    workspace.write_json(strokes, "pages", page_id, "strokes.json")
    workspace.write_json(record, "pages", page_id, "page.json")
    return record


def _ingest_image(
    path: Path,
    workspace: Workspace,
    *,
    index: int,
    dpi: float | None = None,
    sheet_mm: tuple[float, float] | None = None,
) -> dict[str, Any]:
    page_id = f"page_{index + 1:03d}"
    image = Image.open(path)
    source_dpi = _image_dpi(image)
    corrected, angles = prepare_scan(image)
    quarters = int(angles["quarter_turns"]) % 4
    if quarters % 2 == 1 and source_dpi is not None:
        source_dpi = (source_dpi[1], source_dpi[0])
    dpi_note = ""
    if sheet_mm:
        width_mm, height_mm = float(sheet_mm[0]), float(sheet_mm[1])
    else:
        (width_mm, height_mm), dpi_note = resolve_sheet_size_mm(corrected, dpi=dpi, image_dpi=source_dpi)
    folder = workspace.page_dir(page_id)
    preview = folder / "page.png"
    _save_upright(corrected, preview, image, dpi=source_dpi)
    meta = {
        "width_mm": width_mm,
        "height_mm": height_mm,
        "rotation": int(angles["quarter_turns"]) * 90,
        "vector": False,
        "text": "",
        "deskew_deg": angles["deskew_deg"],
    }
    record = _page_record(
        page_id,
        index=index,
        source=str(path),
        meta=meta,
        image=str(preview),
        vector=False,
        tokens=[],
    )
    record["prepare"] = angles
    record["rectified"] = bool(angles.get("rectified", False))
    if angles.get("perspective_quad") is not None:
        record["perspective_quad"] = angles["perspective_quad"]
    if angles.get("homography") is not None:
        record["homography"] = angles["homography"]
    if angles.get("illumination_normalized") is not None:
        record["illumination_normalized"] = bool(angles["illumination_normalized"])
    effective_dpi = (
        round(float(corrected.width) * 25.4 / max(float(width_mm), 1e-6), 2),
        round(float(corrected.height) * 25.4 / max(float(height_mm), 1e-6), 2),
    )
    if dpi_note:
        record["dpi_note"] = dpi_note
        record["dpi_tag_ignored"] = True
        if source_dpi:
            record["source_dpi"] = round(float(source_dpi[0]), 3)
        record["dpi"] = round(float(effective_dpi[0]), 3)
        _save_upright(corrected, preview, image, dpi=effective_dpi)
    elif source_dpi:
        record["dpi"] = round(float(source_dpi[0]), 3)
    elif dpi:
        record["dpi"] = float(dpi)
    elif max(corrected.width, corrected.height) >= 1600:
        record["dpi"] = round(float(effective_dpi[0]), 3)
        _save_upright(corrected, preview, image, dpi=effective_dpi)
    workspace.write_json([], "pages", page_id, "words.json")
    workspace.write_json(record, "pages", page_id, "page.json")
    return record


def _page_record(
    page_id: str,
    *,
    index: int,
    source: str,
    meta: dict[str, Any],
    image: str,
    vector: bool,
    tokens: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": page_id,
        "index": index,
        "source": source,
        "vector": vector,
        "width_mm": meta.get("width_mm") or 420.0,
        "height_mm": meta.get("height_mm") or 297.0,
        "rotation": meta.get("rotation") or 0,
        "deskew_deg": meta.get("deskew_deg") or 0.0,
        "image": image,
        "token_count": len(tokens),
        "text": meta.get("text") or "",
        "classification": None,
    }


def _image_dpi(image: Image.Image) -> tuple[float, float] | None:
    info = getattr(image, "info", {}) or {}
    raw = info.get("dpi")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and float(raw) >= 72:
        return float(raw), float(raw)
    if isinstance(raw, (tuple, list)) and len(raw) >= 2:
        try:
            xdpi, ydpi = float(raw[0]), float(raw[1])
        except (TypeError, ValueError):
            return None
        if xdpi >= 72 and ydpi >= 72:
            return xdpi, ydpi
    try:
        exif = image.getexif()
        if exif:
            xres = exif.get(282)
            yres = exif.get(283)
            unit = exif.get(296, 2)
            if xres and yres:
                xdpi = float(xres[0] / xres[1]) if isinstance(xres, tuple) else float(xres)
                ydpi = float(yres[0] / yres[1]) if isinstance(yres, tuple) else float(yres)
                if unit == 3:
                    xdpi *= 2.54
                    ydpi *= 2.54
                if xdpi >= 72 and ydpi >= 72:
                    return xdpi, ydpi
    except Exception:  # noqa: BLE001
        pass
    return None


# Phone cameras write 72 DPI into the file. That is a screen default, not the sheet.
_SCREEN_DPI = 72.0
_SCREEN_DPI_SLACK = 2.0
# Longer than ANSI E. A 72 DPI reading past this is the screen tag, not the paper.
_MAX_SHEET_MM = 1120.0
# (name, short side mm, long side mm). Used only when a 72 DPI tag is not a real scale.
_STANDARD_SHEETS_MM = (
    ("ANSI A", 215.9, 279.4),
    ("ANSI B", 279.4, 431.8),
    ("ANSI C", 431.8, 558.8),
    ("ANSI D", 558.8, 863.6),
    ("ANSI E", 863.6, 1117.6),
    ("A4", 210.0, 297.0),
    ("A3", 297.0, 420.0),
    ("A2", 420.0, 594.0),
    ("A1", 594.0, 841.0),
)


def _screen_dpi(xdpi: float, ydpi: float) -> bool:
    return 0.0 < xdpi <= 96.5 and 0.0 < ydpi <= 96.5


def _near_standard_sheet(width_mm: float, height_mm: float, tol: float = 0.08) -> bool:
    for _name, short, long in _STANDARD_SHEETS_MM:
        for page_w, page_h in ((short, long), (long, short)):
            if page_w <= 0 or page_h <= 0:
                continue
            if abs(width_mm - page_w) / page_w <= tol and abs(height_mm - page_h) / page_h <= tol:
                return True
    return False


def _sheet_for_photo(px_w: int, px_h: int) -> tuple[float, float]:
    """Nearest standard sheet for a phone photo whose DPI tag is 72.

    The photo's aspect ratio is the sensor, often with margin around the paper.
    Prefer a sheet that the pixel count would photograph at roughly 160–400 DPI.
    """

    long_px = float(max(px_w, px_h))
    short_px = float(max(1, min(px_w, px_h)))
    aspect = long_px / short_px
    best: tuple[float, float] | None = None
    best_score = 1e9
    for _name, short, long in _STANDARD_SHEETS_MM:
        long_mm = float(max(short, long))
        short_mm = float(min(short, long))
        cand_aspect = long_mm / short_mm
        aspect_err = abs(aspect - cand_aspect) / cand_aspect
        implied = long_px / (long_mm / 25.4)
        if 160.0 <= implied <= 400.0:
            dpi_pen = abs(implied - 250.0) / 250.0
        else:
            dpi_pen = 2.0 + abs(implied - 250.0) / 250.0
        score = aspect_err * 8.0 + dpi_pen
        if score < best_score:
            best_score = score
            best = (long_mm, short_mm) if px_w >= px_h else (short_mm, long_mm)
    if best is None:
        return _assumed_sheet_mm_from_pixels(px_w, px_h)
    return (round(best[0], 3), round(best[1], 3))


def _assumed_sheet_mm_from_pixels(px_w: int, px_h: int) -> tuple[float, float]:
    if px_w >= px_h:
        return 420.0, 297.0
    return 297.0, 420.0


def sheet_size_mm(
    image: Image.Image,
    *,
    dpi: float | None = None,
    image_dpi: tuple[float, float] | None = None,
) -> tuple[float, float]:
    """Millimetres from a user DPI, the file's DPI, or the nearest standard sheet.

    3300×5100 px at 300 dpi is 11×17 in (279.4×431.8 mm), not A3.
    A phone photo tagged 72 DPI is not scaled at 72 DPI when that size is
    larger than a real drawing sheet.
    """

    size, _note = resolve_sheet_size_mm(image, dpi=dpi, image_dpi=image_dpi)
    return size


def resolve_sheet_size_mm(
    image: Image.Image,
    *,
    dpi: float | None = None,
    image_dpi: tuple[float, float] | None = None,
) -> tuple[tuple[float, float], str]:
    """Sheet millimetres and a note when a 72 DPI photo tag was not used as scale.

    An explicit ``dpi`` argument is the caller's scale, including 72.
    """

    explicit = bool(dpi and float(dpi) >= 72)
    if explicit:
        xdpi = ydpi = float(dpi)  # type: ignore[arg-type]
    else:
        found = image_dpi if image_dpi is not None else _image_dpi(image)
        if found is None:
            return _assumed_sheet_mm(image), ""
        xdpi, ydpi = found
    width = round(image.width / xdpi * 25.4, 3)
    height = round(image.height / ydpi * 25.4, 3)
    if explicit or not _screen_dpi(xdpi, ydpi):
        return (width, height), ""
    high_res_photo = max(int(image.width), int(image.height)) >= 1600
    if not high_res_photo and _near_standard_sheet(width, height):
        return (width, height), ""
    if not high_res_photo and max(width, height) <= _MAX_SHEET_MM:
        return (width, height), ""
    assumed = _sheet_for_photo(int(image.width), int(image.height))
    tag_label = "72 DPI" if abs(xdpi - _SCREEN_DPI) <= _SCREEN_DPI_SLACK else f"{xdpi:g} DPI"
    note = (
        f"{tag_label} photo tag ignored; sheet size taken from the nearest standard sheet. "
        f"The tag would have made this {width:.0f}×{height:.0f} mm."
    )
    return assumed, note


def _save_upright(
    image: Image.Image,
    dest: Path,
    source: Image.Image,
    *,
    dpi: tuple[float, float] | None = None,
) -> None:
    chosen = dpi or _image_dpi(source)
    if chosen:
        image.save(dest, dpi=(float(chosen[0]), float(chosen[1])))
    else:
        image.save(dest)


def _assumed_sheet_mm(image: Image.Image) -> tuple[float, float]:
    if image.width >= image.height:
        return 420.0, 297.0
    return 297.0, 420.0


def _blank_preview(dest: Path, width_mm: float, height_mm: float) -> None:
    width = max(20, int(width_mm * 2))
    height = max(20, int(height_mm * 2))
    Image.new("RGB", (width, height), (255, 255, 255)).save(dest)
