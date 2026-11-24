from __future__ import annotations

from panelcad.importer.raster import (
    _drop_ocr_ghosts_on_transformers,
    _fuse_transformer_wrong_pin,
    _preferred_fuse_pin_for_transformer,
    _pin_links,
)
from panelcad.importer.stitch import _share_symbols
from panelcad.importer.tile import _prefer_incoming, mark_secondary_occurrences


def test_preferred_fuse_pin_faces_transformer_for_left_of_t1_primary():
    """Fuses left of T1: both H1 and H4 land on pin 2 (facing), pin 1 free for the rail."""

    assert _preferred_fuse_pin_for_transformer("H1") == "2"
    assert _preferred_fuse_pin_for_transformer("H4") == "2"
    assert _preferred_fuse_pin_for_transformer("X1") == "1"
    # Geometry override: fuse to the right of T1 faces pin 1.
    assert _preferred_fuse_pin_for_transformer("H4", fuse_x=200.0, xfmr_x=100.0) == "1"
    assert _preferred_fuse_pin_for_transformer("H1", fuse_x=40.0, xfmr_x=100.0) == "2"
    assert _preferred_fuse_pin_for_transformer("X1", fuse_x=200.0, xfmr_x=100.0) == "1"


def test_cross_winding_secondary_fuse_never_links_to_h1():
    """FU3 (right of T1) must not invent a hop onto T1.H1."""

    import numpy as np

    from panelcad.importer.raster import _fuse_transformer_cross_winding

    fuse = ("FU3", "2", 155.0, 24.0)
    h1 = ("T1", "H1", 100.0, 24.0)
    x1 = ("T1", "X1", 130.0, 24.0)
    assert _fuse_transformer_cross_winding(fuse, h1) is True
    assert _fuse_transformer_cross_winding(fuse, x1) is False
    mask = np.ones((200, 400), dtype=np.uint8) * 255
    pins = [
        ("FU3", "1", 150.0, 24.0),
        ("FU3", "2", 165.0, 24.0),
        ("T1", "H1", 100.0, 24.0),
        ("T1", "X1", 130.0, 24.0),
    ]
    roles = {"FU3": "fuse", "T1": "transformer"}
    links = _pin_links(pins, mask, 2.0, roles=roles)
    ends = {(a, b) for a, b, _s, _e in links}
    ends |= {(b, a) for a, b, _s, _e in links}
    assert (("FU3", "2"), ("T1", "H1")) not in ends
    assert (("FU3", "1"), ("T1", "H1")) not in ends
    assert (("FU3", "1"), ("T1", "X1")) in ends or (("T1", "X1"), ("FU3", "1")) in ends


def test_wrong_pin_detector_for_h4():
    roles = {"FU2": "fuse", "T1": "transformer"}
    # Default left-of-T1: H4 faces pin 2; pin 1 is the through-body far pin.
    assert _fuse_transformer_wrong_pin(("FU2", "1"), ("T1", "H4"), roles) is True
    assert _fuse_transformer_wrong_pin(("FU2", "2"), ("T1", "H4"), roles) is False
    assert _fuse_transformer_wrong_pin(("FU1", "2"), ("T1", "H1"), roles) is False


def test_ocr_ghosts_on_transformer_dropped_and_do_not_inherit_coil():
    """H-pin OCR misread as M1/1M must not steal the real coil on the sheet."""

    devices = [
        {
            "tag": "T1",
            "library_id": "nema.power.control_transformer",
            "source": "symbol",
            "x": 100.0,
            "y": 20.0,
            "confidence": 0.9,
        },
        {
            "tag": "M1",
            "library_id": "",
            "source": "ocr",
            "x": 107.0,
            "y": 31.0,
            "confidence": 0.48,
            "unconfirmed": True,
            "guess": True,
        },
        {
            "tag": "M1",
            "library_id": "nema.contactors.coil",
            "source": "symbol",
            "x": 328.0,
            "y": 115.0,
            "confidence": 0.83,
            "type": "coil",
            "symbol": "coil",
        },
        {
            "tag": "1M",
            "library_id": "",
            "source": "ocr",
            "x": 107.0,
            "y": 53.0,
            "confidence": 0.48,
            "unconfirmed": True,
            "guess": True,
        },
    ]
    _drop_ocr_ghosts_on_transformers(devices)
    assert [d["tag"] for d in devices] == ["T1", "M1"]
    assert devices[1]["library_id"] == "nema.contactors.coil"

    ghost = {
        "tag": "M1",
        "library_id": "",
        "source": "ocr",
        "x": 107.0,
        "y": 31.0,
        "confidence": 0.48,
        "unconfirmed": True,
    }
    coil = {
        "tag": "M1",
        "library_id": "nema.contactors.coil",
        "source": "symbol",
        "x": 328.0,
        "y": 115.0,
        "confidence": 0.83,
        "type": "coil",
        "symbol": "coil",
    }
    _share_symbols(
        [{"devices": [ghost, coil]}],
        {"M1": {"tag": "M1", "library_id": "nema.contactors.coil", "symbol": "coil", "confidence": 0.83}},
    )
    assert not ghost.get("library_id")
    assert not ghost.get("inherited")
    # Even if a ghost somehow inherited the coil library, the symbol wins placement.
    stolen = {
        "tag": "M1",
        "library_id": "nema.contactors.coil",
        "source": "ocr",
        "unconfirmed": True,
        "inherited": True,
        "confidence": 0.9,
    }
    assert _prefer_incoming(stolen, coil) is True
    mark_secondary_occurrences([stolen, coil], [], None)
    assert stolen.get("secondary") is True
    assert coil.get("secondary") is False


def test_pin_links_prefers_h4_to_fuse_pin_2_when_t1_is_on_the_right():
    """When both FU2 pins reach T1.H4, prefer nearer pin 2 over through-body pin 1."""

    import numpy as np

    # Dense ink so both pin pairs qualify.
    mask = np.ones((200, 400), dtype=np.uint8) * 255
    ppm = 2.0
    # Fuse at left: pin2 closer to T1.H4 — prefer pin2 (facing).
    pins = [
        ("FU2", "1", 20.0, 50.0),
        ("FU2", "2", 35.0, 50.0),
        ("T1", "H4", 48.0, 50.0),
        ("T1", "H1", 48.0, 20.0),
    ]
    roles = {"FU2": "fuse", "T1": "transformer"}
    links = _pin_links(pins, mask, ppm, roles=roles)
    ends = {(a, b) for a, b, _s, _e in links}
    ends |= {(b, a) for a, b, _s, _e in links}
    assert (("FU2", "2"), ("T1", "H4")) in ends or (("T1", "H4"), ("FU2", "2")) in ends
    assert (("FU2", "1"), ("T1", "H4")) not in ends and (("T1", "H4"), ("FU2", "1")) not in ends


def test_drop_ocr_ghosts_handles_ocr_t1_width_and_preserves_iec_pilots_and_timers():
    """OCR T1 drops left-side NEMA H1..H4 pins, never drops IEC H1 pilot lights or timer neighbors."""

    # 1. Centered OCR-only T1 caption on NEMA sheet drops H1..H4 up to 24 mm left of hx.
    nema_devices = [
        {"tag": "T1", "library_id": "", "source": "ocr", "x": 120.0, "y": 20.0, "unconfirmed": True},
        {"tag": "H1", "library_id": "", "source": "ocr", "x": 96.0, "y": 32.0, "unconfirmed": True},
        {"tag": "H4", "library_id": "", "source": "ocr", "x": 98.0, "y": 56.0, "unconfirmed": True},
        {"tag": "X1", "library_id": "", "source": "ocr", "x": 144.0, "y": 32.0, "unconfirmed": True},
        {"tag": "PB1", "library_id": "nema.operators.pb_no", "source": "symbol", "x": 80.0, "y": 110.0},
    ]
    _drop_ocr_ghosts_on_transformers(nema_devices, standard="NEMA/ANSI")
    assert [d["tag"] for d in nema_devices] == ["T1", "PB1"]

    # 2. Confirmed non-transformer timer tagged T1 must NEVER act as a transformer host.
    timer_devices = [
        {"tag": "T1", "library_id": "iec.timers.on_delay", "source": "symbol", "x": 120.0, "y": 20.0},
        {"tag": "H1", "library_id": "", "source": "ocr", "x": 125.0, "y": 32.0, "unconfirmed": True},
    ]
    _drop_ocr_ghosts_on_transformers(timer_devices, standard="IEC")
    assert [d["tag"] for d in timer_devices] == ["T1", "H1"]

    # 3. OCR-only T1 on an IEC sheet must NOT silently drop unconfirmed IEC H1 pilot light.
    iec_devices = [
        {"tag": "T1", "library_id": "", "source": "ocr", "x": 120.0, "y": 20.0, "unconfirmed": True},
        {"tag": "H1", "library_id": "", "source": "ocr", "x": 115.0, "y": 32.0, "unconfirmed": True},
    ]
    _drop_ocr_ghosts_on_transformers(iec_devices, standard="IEC")
    assert [d["tag"] for d in iec_devices] == ["T1", "H1"]
