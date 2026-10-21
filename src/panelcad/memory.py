"""Memory budgets for drawing caches.

Only a machine with 8 GB of RAM or less gets the tight limits. A machine
above 16 GB keeps a deep scene pool and the original cache sizes (larger
still at 32 GB, where every sheet of a real job stays built). The middle
band is in between. When the OS will not say how much RAM there is, the
middle band is used.

``PANELCAD_RAM_BYTES`` overrides detection so a test can pretend to be a
small computer. ``PANELCAD_SCENE_SLOTS`` overrides how many sheet scenes
stay built.
"""

from __future__ import annotations

import os
import sys
from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

_GIB = 1024**3

K = TypeVar("K")
V = TypeVar("V")


def _ram_linux() -> int | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def _ram_windows() -> int | None:
    if os.name != "nt":
        return None
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = (
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        )

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except (AttributeError, OSError):
        return None
    if not ok:
        return None
    total = int(status.ullTotalPhys)
    return total or None


def _ram_macos() -> int | None:
    if sys.platform != "darwin":
        return None
    import subprocess

    try:
        out = subprocess.check_output(
            ["sysctl", "-n", "hw.memsize"],
            text=True,
            timeout=2,
        )
        total = int(str(out).strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return total or None


# Detection is expensive on macOS (a sysctl process). Read it once.
_RAM_BYTES: int | None = None


def total_ram_bytes() -> int:
    """Installed RAM. Unknown machines use the middle tier, not the tight one.

    An explicit ``PANELCAD_RAM_BYTES`` is not cached, so tests can change it.
    The detected value is cached for the process.
    """

    override = os.environ.get("PANELCAD_RAM_BYTES", "").strip()
    if override:
        try:
            return max(0, int(override))
        except ValueError:
            pass
    global _RAM_BYTES
    if _RAM_BYTES is None:
        found = None
        for reader in (_ram_linux, _ram_windows, _ram_macos):
            found = reader()
            if found:
                break
        _RAM_BYTES = int(found) if found else 12 * _GIB
    return _RAM_BYTES


# Above this, a real multi-sheet job keeps a scene per sheet.
_PLENTY_SCENE_SLOTS = 256


def scene_cache_slots() -> int:
    """How many sheet scenes stay built. Inactive sheets are released.

    8 GB or less keeps only the sheet on screen. Above 16 GB keeps at least
    eight. 32 GB and up keeps every sheet of a real job.
    """

    override = os.environ.get("PANELCAD_SCENE_SLOTS", "").strip()
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    ram = total_ram_bytes()
    if ram <= 8 * _GIB:
        return 1
    if ram <= 16 * _GIB:
        return 4
    if ram < 32 * _GIB:
        return 8
    return _PLENTY_SCENE_SLOTS


def sheet_cache_limit() -> int:
    """Route and label entries kept after a sheet switch.

    The historical cap was 24. Machines above 16 GB keep that. 32 GB and up
    keep more. Only 8 GB and under is cut down.
    """

    ram = total_ram_bytes()
    if ram <= 8 * _GIB:
        return 12
    if ram <= 16 * _GIB:
        return 16
    if ram < 32 * _GIB:
        return 24
    return 48


def pixmap_cache_bytes() -> int:
    """Decoded drawing images kept for repaints."""

    override = os.environ.get("PANELCAD_PIXMAP_BYTES", "").strip()
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    ram = total_ram_bytes()
    if ram <= 8 * _GIB:
        return 24 * 1024 * 1024
    if ram <= 16 * _GIB:
        return 64 * 1024 * 1024
    if ram < 32 * _GIB:
        return 128 * 1024 * 1024
    return 256 * 1024 * 1024


def import_cache_limit() -> int:
    """Title-block and similar import lookups kept in memory."""

    ram = total_ram_bytes()
    if ram <= 8 * _GIB:
        return 8
    if ram <= 16 * _GIB:
        return 32
    if ram < 32 * _GIB:
        return 128
    return 512


class CountLRU(Generic[K, V]):
    """Least-recently-used map with a live size cap."""

    def __init__(self, limit: Callable[[], int] | int) -> None:
        self._limit = limit
        self._data: OrderedDict[K, V] = OrderedDict()

    def cap(self) -> int:
        raw = self._limit() if callable(self._limit) else self._limit
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    def get(self, key: K, default: V | None = None) -> V | None:
        if key not in self._data:
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def __setitem__(self, key: K, value: V) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        cap = self.cap()
        while len(self._data) > cap:
            self._data.popitem(last=False)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self):
        return iter(self._data)

    def clear(self) -> None:
        self._data.clear()
