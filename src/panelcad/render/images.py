"""Bounded cache of decoded drawing images.

A layout photo and a title-block logo are read again on every repaint. The
cache keeps the decode. A scan larger than a third of the budget is not kept:
one preview must not pin the whole allowance. The cap shrinks on a machine
with less RAM.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PySide6.QtGui import QImage

from panelcad.memory import pixmap_cache_bytes

_ORDER: OrderedDict[tuple, QImage] = OrderedDict()
_BYTES = 0


def clear_image_cache() -> None:
    global _BYTES
    _ORDER.clear()
    _BYTES = 0


def image_cache_bytes() -> int:
    return _BYTES


def cached_qimage(path: str | Path) -> QImage:
    """Decoded image for ``path``. A missing file is an empty image and is not stored."""

    file = Path(path)
    key = _key(file)
    if key is None:
        return QImage(str(file))
    found = _ORDER.get(key)
    if found is not None:
        _ORDER.move_to_end(key)
        return found
    image = QImage(str(file))
    if image.isNull():
        return image
    _store(key, image)
    return image


def _key(file: Path) -> tuple | None:
    try:
        stat = file.stat()
    except OSError:
        return None
    return (str(file), stat.st_mtime_ns, stat.st_size)


def _store(key: tuple, image: QImage) -> None:
    global _BYTES
    weight = _image_bytes(image)
    budget = pixmap_cache_bytes()
    if weight <= 0 or weight > max(1, budget // 3):
        return
    _ORDER[key] = image
    _ORDER.move_to_end(key)
    _BYTES += weight
    while _ORDER and _BYTES > budget:
        _drop_oldest()


def _drop_oldest() -> None:
    global _BYTES
    _key_old, image = _ORDER.popitem(last=False)
    _BYTES = max(0, _BYTES - _image_bytes(image))


def _image_bytes(image: QImage) -> int:
    size = getattr(image, "sizeInBytes", None)
    if callable(size):
        try:
            return max(0, int(size()))
        except (TypeError, ValueError):
            pass
    return max(0, image.width() * image.height() * 4)
