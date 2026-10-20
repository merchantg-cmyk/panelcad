"""SVG print import reads vectors. Synthetic files only — no shop print."""

from __future__ import annotations

from pathlib import Path

import pytest

from panelcad.importer.ingest import PRINT_EXTS, PRINT_FILE_FILTER, is_print_path
from panelcad.importer.read import read_title_block
from panelcad.importer.svg import SvgReadError, read_svg

_NS = 'xmlns="http://www.w3.org/2000/svg"'


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def _svg(
    body: str,
    *,
    width: str = "420mm",
    height: str = "297mm",
    view: str = "0 0 420 297",
    extra: str = "",
) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg {_NS} width="{width}" height="{height}" viewBox="{view}"{extra}>\n'
        f"{body}\n</svg>\n"
    )


def _line(strokes: list[dict], start: tuple[float, float], end: tuple[float, float]) -> dict:
    for stroke in strokes:
        if stroke.get("kind") != "line":
            continue
        ends = {tuple(stroke["a"]), tuple(stroke["b"])}
        if ends == {start, end}:
            return stroke
    raise AssertionError(f"no line {start} — {end} in {strokes}")


def test_svg_promotes_a_long_conductor_from_the_page_widths(tmp_path: Path) -> None:
    """A stroke thicker than the page's symbol ink stays a wire past the border cut.

    The fixed black-phase band does not include this width, and the run is
    longer than a sheet-border ink line. The page's own widths decide.
    """

    body = """
    <line x1="20" y1="20" x2="30" y2="20" stroke="#111" stroke-width="0.2"/>
    <line x1="40" y1="20" x2="50" y2="20" stroke="#111" stroke-width="0.2"/>
    <line x1="10" y1="40" x2="150" y2="40" stroke="#111" stroke-width="0.35"/>
    <line x1="10" y1="80" x2="150" y2="80" stroke="#111" stroke-width="0.2"/>
    """
    _tokens, strokes, _meta = read_svg(_write(tmp_path / "widths.svg", _svg(body)))
    wire = _line(strokes, (10.0, 40.0), (150.0, 40.0))
    assert wire["role"] == "wire"
    assert "pt" not in wire and "color" not in wire
    assert not any(
        stroke.get("kind") == "line" and tuple(stroke["a"]) == (10.0, 80.0) for stroke in strokes
    )


def test_svg_is_an_accepted_print_type() -> None:
    assert ".svg" in PRINT_EXTS
    assert "*.svg" in PRINT_FILE_FILTER
    assert "*.bmp" in PRINT_FILE_FILTER
    assert "*.webp" in PRINT_FILE_FILTER
    assert is_print_path("sheet.svg")
    from panelcad.guidance import help_topic
    from panelcad.learn import print_import_note

    assert "SVG" in print_import_note()["body"]
    assert "not sent to the photo reader" in print_import_note()["body"]
    assert "SVG" in help_topic("print-import")["body"]


def test_svg_file_routes_to_print_import(tmp_path: Path) -> None:
    from panelcad.guidance import classify_import

    path = _write(tmp_path / "drawing.svg", _svg("<line x1='0' y1='0' x2='1' y2='0' stroke='black'/>"))
    advice = classify_import(path)
    assert advice.route == "print"
    assert "SVG" in advice.message


def test_ladder_rung_feeds_the_vector_importer(tmp_path: Path) -> None:
    from panelcad.importer.pipeline import ImportJob
    from panelcad.importer.vision_config import VisionSettings

    # The title block names IEC. One K tag is not enough to guess the standard.
    body = """
    <style>.rung { stroke: #0033cc; stroke-width: 0.45; fill: none; }</style>
    <text x="60" y="70" font-size="4" text-anchor="middle">K1</text>
    <text x="100" y="86" font-size="2.2">101</text>
    <path class="rung" d="M 40 90 L 180 90"/>
    <text x="300" y="270" font-size="3">IEC</text>
    """
    path = _write(tmp_path / "rung.svg", _svg(body))

    def transport(*_args, **_kwargs):
        raise AssertionError("svg import called the vision model")

    job = ImportJob.start(
        [path],
        tmp_path / "ws",
        vision=VisionSettings(preset="grok", model="grok-4.7"),
        transport=transport,
    )
    status = job.status()
    assert status["pending"] is False
    assert status["pages"][0]["vector"] is True
    strokes = job.ws.read_json("pages", "page_001", "strokes.json")
    rung = _line(strokes, (40.0, 90.0), (180.0, 90.0))
    assert rung["role"] == "wire"
    words = job.ws.read_json("pages", "page_001", "words.json")
    assert {tok["text"] for tok in words} >= {"K1", "101"}
    merged = job.ws.read_json("pages", "page_001", "merged.json")
    assert any(item.get("tag") == "K1" for item in merged["devices"])
    assert any(item.get("number") == "101" for item in merged["wires"])
    built = job.build()
    assert "K1" in {comp.id for sheet in built.project.sheets for comp in sheet.components}


def test_title_block_text_is_read_from_the_svg(tmp_path: Path) -> None:
    from panelcad.importer.pipeline import ImportJob

    body = """
    <text x="40" y="250" font-size="3">COMPANY</text>
    <text x="40" y="260" font-size="3">ACME CONTROLS</text>
    <text x="40" y="274" font-size="3">TITLE</text>
    <text x="40" y="284" font-size="3">ELEMENTARY DIAGRAM</text>
    <text x="220" y="250" font-size="3">DWG</text>
    <text x="220" y="260" font-size="3">E-100</text>
    """
    path = _write(tmp_path / "title.svg", _svg(body))
    tokens, _strokes, meta = read_svg(path)
    block = read_title_block(tokens, meta["height_mm"])
    assert block["company"] == "ACME CONTROLS"
    assert block["title"] == "ELEMENTARY DIAGRAM"
    assert block["drawing_number"] == "E-100"

    def transport(*_args, **_kwargs):
        raise AssertionError("svg import called the vision model")

    job = ImportJob.start([path], tmp_path / "ws", backend="external", transport=transport)
    page = job.ws.read_json("pages", "page_001", "page.json")
    assert page["vector"] is True
    assert page["classification"]["company"] == "ACME CONTROLS"
    assert page["classification"]["title"] == "ELEMENTARY DIAGRAM"
    assert page["classification"]["drawing_number"] == "E-100"
    assert job.status()["pending"] is False


def test_transformed_group_and_viewbox_land_in_millimetres(tmp_path: Path) -> None:
    body = """
    <g transform="translate(10 5) scale(2)">
      <g transform="matrix(1 0 0 1 3 4)">
        <line x1="1" y1="2" x2="6" y2="2" stroke="#0000ff" stroke-width="0.2"/>
        <text x="1" y="1" font-size="1.5" text-anchor="start">K2</text>
      </g>
    </g>
    """
    _tokens, strokes, _meta = read_svg(
        _write(tmp_path / "group.svg", _svg(body, width="200mm", height="150mm", view="0 0 100 75"))
    )
    rung = _line(strokes, (36.0, 34.0), (56.0, 34.0))
    assert rung["role"] == "wire"
    tokens, _strokes, _meta = read_svg(
        _write(tmp_path / "group.svg", _svg(body, width="200mm", height="150mm", view="0 0 100 75"))
    )
    text = next(tok for tok in tokens if tok["text"] == "K2")
    assert text["x"] == pytest.approx(39.3, abs=0.05)
    assert text["y"] == pytest.approx(27.9, abs=0.05)

    nested = """
    <svg x="10" y="20" width="40" height="20" viewBox="0 0 10 5">
      <line x1="0" y1="0" x2="10" y2="0" stroke="#0000ff" stroke-width="0.05"/>
    </svg>
    """
    _tokens, strokes, _meta = read_svg(
        _write(tmp_path / "nested.svg", _svg(nested, width="100mm", height="80mm", view="0 0 100 80"))
    )
    _line(strokes, (10.0, 20.0), (50.0, 20.0))

    fitted = """
    <line x1="0" y1="0" x2="100" y2="0" stroke="#0000ff" stroke-width="1"/>
    """
    _tokens, strokes, _meta = read_svg(
        _write(
            tmp_path / "fit.svg",
            _svg(fitted, width="100mm", height="40mm", view="0 0 100 100", extra=' preserveAspectRatio="xMidYMid meet"'),
        )
    )
    _line(strokes, (30.0, 0.0), (70.0, 0.0))


def test_path_text_without_characters_is_needs_info(tmp_path: Path) -> None:
    from panelcad.importer.pipeline import ImportJob

    # The title block names IEC so the stated tag can be placed. The path is not.
    body = """
    <style>.rung { stroke: #0033cc; stroke-width: 0.45; fill: none; }</style>
    <text x="50" y="40" font-size="5" text-anchor="middle">K1</text>
    <path class="rung" d="M 30 120 L 180 120"/>
    <text x="40" y="180" font-size="3">IEC</text>
    <g id="K9" font-family="isocp" font-size="3" transform="translate(40,80)">
      <path d="M0 0 L4 0 L4 6 L0 6 Z" fill="#111111" stroke="none"/>
    </g>
    """
    path = _write(tmp_path / "glyphs.svg", _svg(body, width="220mm", height="200mm", view="0 0 220 200"))
    tokens, strokes, _meta = read_svg(path)
    assert "K9" not in {tok.get("text") for tok in tokens}
    assert any(tok.get("unreadable") and tok.get("reason") == "needs_info" for tok in tokens)
    assert not any(
        stroke.get("kind") == "line" and {tuple(stroke["a"]), tuple(stroke["b"])} == {(40.0, 80.0), (44.0, 80.0)}
        for stroke in strokes
    )
    job = ImportJob.start([path], tmp_path / "ws", backend="external")
    built = job.build()
    info = built.project.properties["print_import"]
    assert info["not_for_build"] == "yes"
    assert any(item.get("reason") == "needs_info" for item in info["items"])
    assert "K9" not in {comp.id for sheet in built.project.sheets for comp in sheet.components}
    assert "K1" in {comp.id for sheet in built.project.sheets for comp in sheet.components}


def test_explicit_path_text_is_kept_and_conflicts_are_not_guessed(tmp_path: Path) -> None:
    labeled = """
    <g font-family="isocp" data-text="CR12" transform="translate(10,10)">
      <path d="M0 0 L4 6" fill="#111" stroke="none"/>
    </g>
    """
    tokens, strokes, _meta = read_svg(_write(tmp_path / "labeled.svg", _svg(labeled, width="80mm", height="80mm", view="0 0 80 80")))
    assert any(tok.get("text") == "CR12" for tok in tokens)
    assert strokes == []

    clash = """
    <g font-family="arial" data-text="K1" aria-label="M1">
      <path d="M0 0 L3 3" fill="#111" stroke="none"/>
    </g>
    """
    tokens, _strokes, _meta = read_svg(_write(tmp_path / "clash.svg", _svg(clash, width="40mm", height="40mm", view="0 0 40 40")))
    assert "K1" not in {tok.get("text") for tok in tokens}
    assert "M1" not in {tok.get("text") for tok in tokens}
    assert any("disagree" in str(tok.get("detail") or "") for tok in tokens)

    named = """
    <g id="K1" font-family="arial">
      <path d="M0 0 L3 4" fill="#111" stroke="none"/>
    </g>
    """
    tokens, _strokes, _meta = read_svg(_write(tmp_path / "id.svg", _svg(named, width="40mm", height="40mm", view="0 0 40 40")))
    assert "K1" not in {tok.get("text") for tok in tokens}
    assert any(tok.get("unreadable") for tok in tokens)


def test_tspan_joins_and_unknown_transform_is_not_placed(tmp_path: Path) -> None:
    joined = '<text x="30" y="40" font-size="4"><tspan>K</tspan><tspan>1</tspan></text>'
    tokens, _strokes, _meta = read_svg(_write(tmp_path / "span.svg", _svg(joined, width="80mm", height="80mm", view="0 0 80 80")))
    assert any(tok.get("text") == "K1" for tok in tokens)

    bad = """
    <g transform="foo(1) translate(5)">
      <text x="10" y="20" font-size="4">K9</text>
    </g>
    """
    tokens, strokes, _meta = read_svg(_write(tmp_path / "bad.svg", _svg(bad, width="100mm", height="100mm", view="0 0 100 100")))
    assert "K9" not in {tok.get("text") for tok in tokens}
    assert strokes == []
    assert any(tok.get("reason") == "needs_info" for tok in tokens)


def test_percent_size_and_embedded_image_stay_needs_info(tmp_path: Path) -> None:
    from panelcad.importer.pipeline import ImportJob

    percent = '<text x="20" y="30" font-size="4">K1</text>'
    tokens, _strokes, meta = read_svg(
        _write(tmp_path / "pct.svg", _svg(percent, width="100%", height="100%", view="0 0 200 100"))
    )
    assert any(tok.get("text") == "K1" for tok in tokens)
    assert any("absolute length" in str(tok.get("detail") or "") for tok in tokens)
    assert meta["vector"] is True

    image = '<image href="data:image/png;base64,aaaa" x="0" y="0" width="100" height="100"/>'
    path = _write(tmp_path / "photo.svg", _svg(image, width="100mm", height="100mm", view="0 0 100 100"))

    def transport(*_args, **_kwargs):
        raise AssertionError("svg import called the vision model")

    job = ImportJob.start([path], tmp_path / "ws", backend="external", transport=transport)
    assert job.status()["pending"] is False
    assert job.status()["pages"][0]["vector"] is True
    words = job.ws.read_json("pages", "page_001", "words.json")
    assert any("only contains an embedded image" in str(tok.get("detail") or "") for tok in words)
    from panelcad.importer.pipeline import EmptyImport

    with pytest.raises(EmptyImport) as raised:
        job.build()
    assert "only contains an embedded image" in str(raised.value)
    assert "vision" not in str(raised.value).lower()
    assert "rotation" not in str(raised.value).lower()


def test_tspan_x_list_splits_into_words(tmp_path: Path) -> None:
    body = '<text font-size="4"><tspan x="10 15 20 70 75 80 130 135" y="40">K1 M2 Q1</tspan></text>'
    tokens, _strokes, _meta = read_svg(_write(tmp_path / "words.svg", _svg(body, width="200mm", height="80mm", view="0 0 200 80")))
    assert {tok.get("text") for tok in tokens} >= {"K1", "M2", "Q1"}
    assert not any("K1 M2" in str(tok.get("text") or "") for tok in tokens)


def test_glyph_uses_and_inline_outlines_are_needs_info(tmp_path: Path) -> None:
    uses = """
    <defs>
      <path id="gA" d="M0 0 L3 0 L3 5 L0 5 Z" fill="#111" stroke="none"/>
    </defs>
    <use href="#gA" x="10" y="20"/>
    <use href="#gA" x="16" y="20"/>
    <use href="#gA" x="22" y="20"/>
    <line x1="10" y1="40" x2="80" y2="40" stroke="#0033cc" stroke-width="0.45"/>
    """
    tokens, strokes, _meta = read_svg(_write(tmp_path / "uses.svg", _svg(uses, width="200mm", height="100mm", view="0 0 200 100")))
    assert any(tok.get("detail") == "text was exported as paths and the characters are not in the file" for tok in tokens)
    assert not any(tok.get("text") for tok in tokens)
    assert _line(strokes, (10.0, 40.0), (80.0, 40.0))["role"] == "wire"
    assert not any(
        stroke.get("kind") == "line" and min(stroke["a"][1], stroke["b"][1]) < 30 for stroke in strokes
    )

    inline = """
    <path d="M10 20 L13 20 L13 26 L10 26 Z" fill="#111" stroke="none"/>
    <path d="M16 20 L19 20 L19 26 L16 26 Z" fill="#111" stroke="none"/>
    <path d="M22 20 L25 20 L25 26 L22 26 Z" fill="#111" stroke="none"/>
    """
    tokens, strokes, _meta = read_svg(_write(tmp_path / "inline.svg", _svg(inline, width="80mm", height="60mm", view="0 0 80 60")))
    assert any(tok.get("unreadable") and "paths" in str(tok.get("detail") or "") for tok in tokens)
    assert strokes == []


def test_unitless_sheet_size_is_css_pixels_and_flagged(tmp_path: Path) -> None:
    body = '<text x="20" y="30" font-size="4">K1</text>'
    tokens, _strokes, meta = read_svg(_write(tmp_path / "px.svg", _svg(body, width="200", height="100", view="0 0 200 100")))
    assert any(tok.get("text") == "K1" for tok in tokens)
    assert any("96 px/in" in str(tok.get("detail") or "") for tok in tokens)
    assert meta["width_mm"] == pytest.approx(200 * 25.4 / 96, abs=0.1)


def test_outlined_svg_does_not_blame_the_vision_backend(tmp_path: Path) -> None:
    from panelcad.importer.pipeline import EmptyImport, ImportJob

    body = """
    <defs><path id="gA" d="M0 0 L2 0 L2 4 L0 4 Z" fill="#111" stroke="none"/></defs>
    <use href="#gA" x="10" y="12"/>
    <use href="#gA" x="14" y="12"/>
    """
    path = _write(tmp_path / "letters.svg", _svg(body, width="80mm", height="40mm", view="0 0 80 40"))
    job = ImportJob.start([path], tmp_path / "ws", backend="external")
    with pytest.raises(EmptyImport) as raised:
        job.build()
    message = str(raised.value).lower()
    assert "outlines" in message
    assert "characters are not in the file" in message
    assert "pdf" in message
    assert "vision" not in message
    assert "rotation" not in message


def test_cairo_glyph_paths_with_a_trailing_moveto_are_needs_info(tmp_path: Path) -> None:
    from panelcad.importer.pipeline import EmptyImport, ImportJob
    from panelcad.importer.svg import _path_closed

    cairo = "M0 0 L2 0 L1 3 Z M 3.39 -3 L 1.36 -3 L 3.39 -6.34 Z M 3.390625 -3"
    assert _path_closed(cairo)
    body = f"""
    <defs>
      <g id="glyph-0-1">
        <path d="{cairo}" fill="#000" stroke="none"/>
      </g>
    </defs>
    <use href="#glyph-0-1" x="10" y="20"/>
    <use href="#glyph-0-1" x="16" y="20"/>
    <use href="#glyph-0-1" x="22" y="20"/>
    <line x1="10" y1="50" x2="90" y2="50" stroke="#0033cc" stroke-width="0.45"/>
    """
    path = _write(tmp_path / "cairo.svg", _svg(body, width="200mm", height="100mm", view="0 0 200 100"))
    tokens, strokes, meta = read_svg(path)
    assert any("characters are not in the file" in str(tok.get("detail") or "") for tok in tokens)
    assert not any(tok.get("text") for tok in tokens)
    assert _line(strokes, (10.0, 50.0), (90.0, 50.0))["role"] == "wire"
    assert not any(min(stroke["a"][1], stroke["b"][1]) < 40 for stroke in strokes if stroke.get("kind") == "line")
    assert "outlines" in meta["svg_notice"].lower()
    job = ImportJob.start([path], tmp_path / "ws", backend="external")
    with pytest.raises(EmptyImport) as raised:
        job.build()
    message = str(raised.value).lower()
    assert "outlines" in message
    assert "characters are not in the file" in message
    assert "vision" not in message


def test_pymupdf_glyph_uses_keep_the_stated_characters(tmp_path: Path) -> None:
    from panelcad.importer.svg import _path_closed

    # One def has no close; one ends with a moveto. Both are fill-only glyphs.
    open_path = "M0 0 L2 0 L2 4"
    trailing = "M0 0 L2 0 L2 4 Z M 0.2 0.2"
    assert not _path_closed(open_path)
    assert _path_closed(trailing)
    body = f"""
    <defs>
      <path id="font_1_21" d="{open_path}" fill="#000" stroke="none"/>
      <path id="font_1_22" d="{trailing}" fill="#000" stroke="none"/>
    </defs>
    <use data-text="K" xlink:href="#font_1_21" transform="matrix(1 0 0 1 10 20)"/>
    <use data-text="1" xlink:href="#font_1_22" transform="matrix(1 0 0 1 12.150 20)"/>
    <use data-text="M" xlink:href="#font_1_21" transform="matrix(1 0 0 1 40 20)"/>
    <use data-text="2" xlink:href="#font_1_22" transform="matrix(1 0 0 1 42.150 20)"/>
    """
    svg = _svg(body, width="200mm", height="80mm", view="0 0 200 80").replace(
        "<svg ",
        '<svg xmlns:xlink="http://www.w3.org/1999/xlink" ',
        1,
    )
    tokens, strokes, meta = read_svg(_write(tmp_path / "mupdf.svg", svg))
    assert {tok.get("text") for tok in tokens} >= {"K1", "M2"}
    assert "K1M2" not in {tok.get("text") for tok in tokens}
    assert not any(tok.get("unreadable") for tok in tokens)
    assert strokes == []
    assert meta["svg_notice"] == ""


@pytest.mark.parametrize("scale", [1.0, 3.0])
@pytest.mark.parametrize("letter_frac", [0.0, 0.1, 0.15])
def test_box_glyph_words_split_only_at_the_word_gap(tmp_path: Path, scale: float, letter_frac: float) -> None:
    """Unit-scale box glyphs split on a word space, not on ordinary letter spacing."""

    height = 4.0 * scale
    glyph_w = 2.0 * scale
    letter_gap = letter_frac * height
    word_gap = 0.50 * height
    glyph = f"M0 0 L{glyph_w} 0 L{glyph_w} {height}"

    def line(text: str, x: float, y: float) -> str:
        parts: list[str] = []
        cursor = x
        for index, ch in enumerate(text):
            if ch == " ":
                continue
            parts.append(
                f'<use data-text="{ch}" xlink:href="#font_1_21" '
                f'transform="matrix(1 0 0 1 {cursor:.3f} {y:.3f})"/>'
            )
            advance = word_gap if index + 1 < len(text) and text[index + 1] == " " else letter_gap
            cursor += glyph_w + advance
        return "\n".join(parts)

    body = f"""
    <defs>
      <path id="font_1_21" d="{glyph}" fill="#000" stroke="none"/>
    </defs>
    {line("POWER DISTRIBUTION", 10, 20)}
    {line("CR 411", 10, 40 + 16 * scale)}
    {line("480VAC, 40AMP SERVICE", 10, 60 + 32 * scale)}
    """
    svg = _svg(body, width="500mm", height="200mm", view="0 0 500 200").replace(
        "<svg ",
        '<svg xmlns:xlink="http://www.w3.org/1999/xlink" ',
        1,
    )
    tokens, strokes, _meta = read_svg(_write(tmp_path / "spaces.svg", svg))
    words = {tok.get("text") for tok in tokens}
    assert words == {"POWER", "DISTRIBUTION", "CR", "411", "480VAC,", "40AMP", "SERVICE"}
    assert strokes == []


def test_pymupdf_helvetica_words_match_the_pdf(tmp_path: Path) -> None:
    """Outlined Helvetica keeps the PDF's words, including narrow glyphs."""

    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page(width=520, height=640)
    text = "CR 411\nFU-1A\nR41000\nMACHINE\nSHEET\nTITLE BLOCK\nKV8000\nTB2 24"
    page.insert_text((72, 48), text, fontname="helv", fontsize=12)
    page.insert_text((72, 360), text, fontname="helv", fontsize=28)
    pdf_words = [word[4] for word in page.get_text("words")]
    svg_path = tmp_path / "helv.svg"
    svg_path.write_text(page.get_svg_image(text_as_path=True), encoding="utf-8")
    tokens, strokes, _meta = read_svg(svg_path)
    glyph_words = [str(tok.get("text") or "") for tok in tokens if tok.get("text")]
    assert glyph_words == pdf_words
    assert "CR411" not in glyph_words
    assert "KV8000" in glyph_words
    assert strokes == []


def test_pymupdf_base_fonts_keep_stated_spaces(tmp_path: Path) -> None:
    """A stated space glyph stays a space across the base-14 fonts."""

    pymupdf = pytest.importorskip("pymupdf")
    text = "\n".join(
        [
            "CONTROL TRANSFORMER 480/120 VAC",
            "SEE SHEET 3 FOR DETAILS",
            "L1 L2 L3 GND PE",
            "TITLE BLOCK 480VAC, 40AMP SERVICE",
        ]
    )
    for font in ("helv", "heit", "tiro", "cour"):
        doc = pymupdf.open()
        page = doc.new_page(width=720, height=220)
        page.insert_text((36, 48), text, fontname=font, fontsize=12)
        pdf_words = [word[4] for word in page.get_text("words")]
        assert "480/120" in pdf_words
        assert "3" in pdf_words and "FOR" in pdf_words
        assert "L3" in pdf_words and "GND" in pdf_words
        svg_path = tmp_path / f"{font}.svg"
        svg_path.write_text(page.get_svg_image(text_as_path=True), encoding="utf-8")
        tokens, _strokes, _meta = read_svg(svg_path)
        glyph_words = [str(tok.get("text") or "") for tok in tokens if tok.get("text")]
        assert glyph_words == pdf_words, font


def test_raised_punctuation_stays_on_its_word(tmp_path: Path) -> None:
    """Stars, quotes, and degree marks share the pen baseline of the word."""

    pymupdf = pytest.importorskip("pymupdf")
    text = "\n".join(
        [
            "NOW: Unit 4 KV-B16X*   Unit 5/6 KV-B16T*.",
            "SEE NOTE* REF** 3*4 A*B",
            "12'-6\" 45° 1/2\" ~24V ^TOP",
            "M1_AUX (OL) [CR1] {X} \"Q\"",
            "x*y a*b go* end.",
        ]
    )
    for fontname in ("helv", "tiro", "cour", "heit"):
        for size in (10, 14):
            doc = pymupdf.open()
            page = doc.new_page(width=900, height=280)
            page.insert_text((36, 48), text, fontname=fontname, fontsize=size)
            pdf_words = [word[4] for word in page.get_text("words")]
            assert "KV-B16X*" in pdf_words
            svg_path = tmp_path / f"{fontname}-{size}.svg"
            svg_path.write_text(page.get_svg_image(text_as_path=True), encoding="utf-8")
            tokens, _strokes, _meta = read_svg(svg_path)
            glyph_words = [str(tok.get("text") or "") for tok in tokens if tok.get("text")]
            assert glyph_words == pdf_words, f"{fontname} {size}"


def test_overlapping_labels_a_fraction_of_an_em_apart_stay_separate(tmp_path: Path) -> None:
    """Baselines about 0.3 em apart are two lines, even when the ink overlaps in x."""

    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    size = 12.0
    page.insert_text((72, 80), "GS-M51P", fontname="helv", fontsize=size)
    page.insert_text((72, 80 + 0.3 * size), "LOCKING", fontname="helv", fontsize=size)
    # Two points on a 10 pt label is 0.2 em. A looser ink-bottom tolerance merges them.
    page.insert_text((72, 140), "ALPHA", fontname="helv", fontsize=10)
    page.insert_text((72, 142), "BETA", fontname="helv", fontsize=10)
    pdf_words = [word[4] for word in page.get_text("words")]
    svg_path = tmp_path / "overlap.svg"
    svg_path.write_text(page.get_svg_image(text_as_path=True), encoding="utf-8")
    tokens, _strokes, _meta = read_svg(svg_path)
    glyph_words = [str(tok.get("text") or "") for tok in tokens if tok.get("text")]
    assert glyph_words == pdf_words
    assert glyph_words == ["GS-M51P", "LOCKING", "ALPHA", "BETA"]


def test_plain_svg_read_error_is_not_the_project_import_sentence() -> None:
    from panelcad.importer.ingest import plain_print_error

    message = plain_print_error(ValueError("not an svg"), "drawing.svg")
    assert message == "This file could not be read as an SVG."
    assert "project file" not in message


def test_rotated_glyph_words_match_the_pdf(tmp_path: Path) -> None:
    """Vertical glyph text is read along its advance, not as one letter per line."""

    from collections import Counter

    pymupdf = pytest.importorskip("pymupdf")
    vertical = "TB1-12 CR4 480VAC M1*"
    horizontal = "WIRE 101 TO X2"
    for fontname in ("helv", "tiro", "cour"):
        for size in (7, 12):
            for rotate in (90, 270):
                doc = pymupdf.open()
                page = doc.new_page(width=500, height=500)
                origin = (70, 430) if rotate == 90 else (70, 40)
                page.insert_text(origin, vertical, fontname=fontname, fontsize=size, rotate=rotate)
                page.insert_text((200, 240), horizontal, fontname=fontname, fontsize=size)
                pdf_words = [word[4] for word in page.get_text("words")]
                assert "TB1-12" in pdf_words
                assert "WIRE" in pdf_words
                svg_path = tmp_path / f"{fontname}-{size}-{rotate}.svg"
                svg_path.write_text(page.get_svg_image(text_as_path=True), encoding="utf-8")
                tokens, _strokes, _meta = read_svg(svg_path)
                glyph_words = [str(tok.get("text") or "") for tok in tokens if tok.get("text")]
                assert Counter(glyph_words) == Counter(pdf_words), f"{fontname} {size} {rotate}"


def test_rotated_glyph_does_not_join_a_horizontal_word(tmp_path: Path) -> None:
    """A rotated digit whose pen sits on a horizontal baseline stays its own word."""

    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    font = pymupdf.Font("helv")
    size = 14.0
    text = "COMPANY"
    page.insert_text((72, 100), text, fontname="helv", fontsize=size)
    width = font.text_length(text, fontsize=size)
    page.insert_text((72 + width / 2, 100), "2", fontname="helv", fontsize=size, rotate=90)
    pdf_words = [word[4] for word in page.get_text("words")]
    svg_path = tmp_path / "mixed-direction.svg"
    svg_path.write_text(page.get_svg_image(text_as_path=True), encoding="utf-8")
    tokens, _strokes, _meta = read_svg(svg_path)
    glyph_words = [str(tok.get("text") or "") for tok in tokens if tok.get("text")]
    assert "COMPANY" in glyph_words
    assert "COMPAN2Y" not in glyph_words
    assert "2" in glyph_words
    assert set(glyph_words) == set(pdf_words)


def test_mixed_font_sizes_on_one_baseline_keep_pdf_words(tmp_path: Path) -> None:
    """A small label and a larger tag on one baseline keep their own em."""

    pymupdf = pytest.importorskip("pymupdf")
    phrases = (
        ("INCOMING ", "small"),
        ("DISC1 ", "large"),
        ("VFD1 ", "large"),
        ("1/3 HP ", "small"),
        ("FU-10 ", "small"),
        ("MOTOR 01", "large"),
    )
    for fontname in ("helv", "tiro", "cour"):
        font = pymupdf.Font(fontname)
        for small, large in ((7.5, 10.0), (8.0, 12.0), (6.0, 9.0)):
            doc = pymupdf.open()
            page = doc.new_page(width=900, height=160)
            x = 36.0
            y = 90.0
            for text, which in phrases:
                size = small if which == "small" else large
                page.insert_text((x, y), text, fontname=fontname, fontsize=size)
                x += font.text_length(text, fontsize=size)
            pdf_words = [word[4] for word in page.get_text("words")]
            assert "DISC1" in pdf_words
            assert "INCOMING" in pdf_words
            svg_path = tmp_path / f"{fontname}-{small}-{large}.svg"
            svg_path.write_text(page.get_svg_image(text_as_path=True), encoding="utf-8")
            tokens, _strokes, _meta = read_svg(svg_path)
            glyph_words = [str(tok.get("text") or "") for tok in tokens if tok.get("text")]
            assert glyph_words == pdf_words, f"{fontname} {small}/{large}"


def test_svg_import_matches_pdf_sheet_by_sheet(tmp_path: Path) -> None:
    from panelcad.api.session import Session
    from panelcad.importer.pipeline import EmptyImport, ImportJob

    session = Session.open(Path("examples/dol_starter.pcad.yaml"))

    def found(path: Path, workspace: Path) -> tuple[set[str], int]:
        job = ImportJob.start([path], workspace, backend="external")
        try:
            job.build()
        except EmptyImport:
            pass
        merged = job.ws.read_json("pages", "page_001", "merged.json") or {}
        tags = {str(item.get("tag") or "") for item in merged.get("devices") or [] if item.get("tag")}
        return tags, len(merged.get("wires") or [])

    for sheet in session.project.sheets:
        svg = tmp_path / f"{sheet.id}.svg"
        pdf = tmp_path / f"{sheet.id}.pdf"
        session.export_svg(svg, sheet_id=sheet.id)
        session.export_pdf(pdf, sheet_id=sheet.id)
        svg_tags, svg_wires = found(svg, tmp_path / f"svg-{sheet.id}")
        pdf_tags, pdf_wires = found(pdf, tmp_path / f"pdf-{sheet.id}")
        assert svg_tags == pdf_tags, sheet.id
        assert svg_wires == pdf_wires, sheet.id


def test_broken_svg_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SvgReadError, match="could not be read as an SVG"):
        read_svg(_write(tmp_path / "nope.svg", "<html></html>"))
    with pytest.raises(SvgReadError, match="could not be read as an SVG"):
        read_svg(_write(tmp_path / "bad.svg", "<<"))
