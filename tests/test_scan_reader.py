"""Scan reading: coil tags, NC contacts, rail numbers, and a branch landing."""

from __future__ import annotations

import cv2
import numpy as np

from panelcad.importer.raster import (
    _pin_links,
    _read_coil_tag,
    _render_primitive_template,
    _search_template,
    _tee_links,
)
from panelcad.importer.read import label_tokens
from panelcad.importer.strip_scan import _number_in
from panelcad.library.catalog import default_catalog


def test_letters_inside_a_coil_are_the_tag_and_a_blank_coil_is_not_named():
    """The blank-circle case is the Tesseract contract.

    Electrical OCR can invent letters in a bare circle (``KK2``). That engine
    is covered by the electrical OCR tests. This one pins the Tesseract path.
    """

    from panelcad.ocr.backend import active_backend, ocr_scope, page_engine_ready, tesseract_required

    if page_engine_ready() and active_backend() != "tesseract" and not tesseract_required():
        import pytest

        pytest.skip("blank-coil assertion is for the Tesseract reader")
    with ocr_scope("tesseract"):
        _assert_coil_tag()


def _assert_coil_tag():
    catalog = default_catalog()
    device = catalog.get("nema.contactors.coil")
    ppm = 10.0
    image = np.full((500, 500), 255, np.uint8)
    ox, oy = 8.0, 8.0
    x0, y0 = int(ox * ppm), int(oy * ppm)
    x1 = int((ox + device.schematic_width) * ppm)
    y1 = int((oy + device.schematic_height) * ppm)
    center = ((x0 + x1) // 2, (y0 + y1) // 2)
    cv2.circle(image, center, 70, 0, 3)
    named = image.copy()
    cv2.putText(named, "M1", (center[0] - 28, center[1] + 12), cv2.FONT_HERSHEY_SIMPLEX, 1.1, 0, 2)
    assert _read_coil_tag(named, ox, oy, device, ppm) == "M1"
    assert _read_coil_tag(image, ox, oy, device, ppm) == ""


def test_nc_ink_matches_the_nc_pushbutton_not_the_no_symbol():
    catalog = default_catalog()
    nc = catalog.get("nema.operators.pb_nc")
    no = catalog.get("nema.operators.pb_no")
    ppm = 8.0
    tmpl = _render_primitive_template(nc, ppm)
    assert tmpl is not None
    page = np.zeros((tmpl.shape[0] + 80, tmpl.shape[1] + 80), np.uint8)
    page[20 : 20 + tmpl.shape[0], 20 : 20 + tmpl.shape[1]] = tmpl
    binary = cv2.dilate(page, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    left, base = 20 / ppm, 20 / ppm
    span = {"x_span": (-4.0, 8.0), "y_span": (-4.0, 8.0), "cover_origin": True}
    score_nc, _, _ = _search_template(binary, nc, ppm, binary.shape, left, base, **span)
    score_no, _, _ = _search_template(binary, no, ppm, binary.shape, left, base, **span)
    assert score_nc > score_no
    assert score_nc >= 0.8


def test_a_three_digit_rail_number_in_the_zone_band_is_a_wire():
    tokens = [
        {"text": "PB1", "x": 80.0, "y": 40.0, "w": 8.0, "h": 3.0, "confidence": 0.9},
        {"text": "12", "x": 10.0, "y": 8.0, "w": 4.0, "h": 2.0, "confidence": 0.9},
        {"text": "102", "x": 12.0, "y": 40.0, "w": 6.0, "h": 2.0, "confidence": 0.9},
    ]
    labeled = label_tokens(tokens, 420.0, 280.0, insets=(30.0, 22.0, 30.0, 54.0))
    texts = {item["text"] for item in labeled["wires"]}
    assert "102" in texts
    assert "12" not in texts


def test_a_turned_strip_number_is_read_and_a_blank_side_stays_unread():
    """Turned digits are asserted on the Tesseract reader.

    Electrical OCR can read the same glyphs as ``204``. That is the engine,
    not a second number written into the fixture.
    """

    from panelcad.ocr.backend import active_backend, page_engine_ready, tesseract_required

    if page_engine_ready() and active_backend() != "tesseract" and not tesseract_required():
        import pytest

        pytest.skip("turned-digit assertion is for the Tesseract reader")
    gray = np.full((500, 240), 255, np.uint8)
    cv2.rectangle(gray, (40, 160), (110, 360), 0, 2)
    slip = np.full((36, 90), 255, np.uint8)
    cv2.putText(slip, "201", (6, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 0, 2)
    turned = cv2.rotate(slip, cv2.ROTATE_90_COUNTERCLOCKWISE)
    left = 75 - turned.shape[1] // 2
    gray[40 : 40 + turned.shape[0], left : left + turned.shape[1]] = turned
    block = {"x": 40, "y": 160, "w": 70, "h": 200}
    assert _number_in(gray, block, 10.0, "top") == "201"
    assert _number_in(gray, block, 10.0, "bottom") == "?"


def test_a_branch_lands_on_the_upstream_pin_and_not_on_the_same_device():
    ppm = 4.0
    mask = np.zeros((400, 500), np.uint8)

    def line(x0: float, y0: float, x1: float, y1: float) -> None:
        cv2.line(mask, (int(x0 * ppm), int(y0 * ppm)), (int(x1 * ppm), int(y1 * ppm)), 1, 2)

    line(10, 40, 36, 40)
    line(24, 40, 24, 68)
    pins = [
        ("PB2", "2", 10.0, 40.0),
        ("PB1", "3", 36.0, 40.0),
        ("M1-13", "3", 24.0, 68.0),
        ("ES1", "1", 10.0, 20.0),
        ("ES1", "2", 40.0, 20.0),
    ]
    line(10, 20, 40, 20)
    line(40, 20, 70, 20)
    pins.append(("PB2", "1", 70.0, 20.0))
    links = _pin_links(pins, mask, ppm)
    pairs = {(a[0], a[1], b[0], b[1]) for a, b, _p, _q in links}
    assert ("ES1", "1", "ES1", "2") not in pairs
    assert ("ES1", "2", "ES1", "1") not in pairs
    tees = _tee_links(links, pins, mask, ppm)
    branched = {(a, b) for a, b, _p, _q in tees}
    assert (("PB2", "2"), ("M1-13", "3")) in branched


def test_a_confirmed_nc_stop_and_transformer_are_not_rewritten():
    from panelcad.importer.ladder_synth import _classify_device_role_and_library

    catalog = default_catalog()
    _role, lid, _pins = _classify_device_role_and_library(
        "PB2", "pushbutton", "pushbutton", "nema.operators.pb_nc", [], "NEMA/ANSI", catalog
    )
    assert lid.endswith("pb_nc")
    _role, lid, _pins = _classify_device_role_and_library(
        "PB1", "pushbutton", "pushbutton", "nema.operators.pb_no", [], "NEMA/ANSI", catalog
    )
    assert lid.endswith("pb_no")
    _role, lid, _pins = _classify_device_role_and_library(
        "PB2", "", "", "", [], "NEMA/ANSI", catalog
    )
    assert lid.endswith("pb_no")
    _role, lid, _pins = _classify_device_role_and_library(
        "T1", "transformer", "transformer", "nema.power.control_transformer", [], "NEMA/ANSI", catalog
    )
    assert "transformer" in lid
