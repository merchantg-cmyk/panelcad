"""A terminal-strip row is mounting order, and a green block is ground."""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

from panelcad.importer.raster import _upscale_low_res
from panelcad.importer.strip_scan import find_block_row


def test_blocks_are_left_to_right_and_a_green_body_is_ground():
    gray = np.full((420, 700), 255, np.uint8)
    rgb = np.full((420, 700, 3), 255, np.uint8)
    ppm = 10.0
    edges = [40, 120, 200, 280, 360]
    for x in edges:
        cv2.line(gray, (x, 40), (x, 360), 0, 2)
    # The second gap is a ground block.
    rgb[50:350, 124:196] = (30, 170, 40)
    blocks = find_block_row(gray, rgb, ppm)
    assert [round(block["x_mm"], 1) for block in blocks] == [4.2, 12.2, 20.2, 28.2]
    assert [block["ground"] for block in blocks] == [False, True, False, False]


def test_a_coarse_scan_is_enlarged_without_changing_sheet_millimetres():
    # About 100 dpi on an A3 sheet. The millimetres stay 420 x 297.
    image = Image.new("RGB", (1654, 1169), "white")
    larger = _upscale_low_res(image, 420.0, 297.0)
    assert larger.width > image.width
    assert abs(larger.width / 420.0 - 200.0 / 25.4) < 0.4
    same = _upscale_low_res(Image.new("RGB", (3300, 2300), "white"), 420.0, 297.0)
    assert same.size == (3300, 2300)
