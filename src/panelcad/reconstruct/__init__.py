"""As-built reconstruction from real panel photos.

Names load on first use. Importing a submodule (the placeholder registered
when a session opens) must not import photo rendering or vision.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {}


def _from(module: str, names: tuple[str, ...]) -> None:
    for name in names:
        _EXPORTS[name] = (module, name)


_from(
    "panelcad.reconstruct.feedback",
    (
        "DeviceFeedbackBox",
        "PhotoFeedbackReport",
        "add_missed_device_on_photo",
        "add_missed_strip_on_photo",
        "build_photo_feedback_model",
        "build_photo_feedback_report",
        "export_photo_feedback",
        "link_closeup_to_device",
        "refine_device_box_on_photo",
        "render_annotated_cabinet_photo",
        "snap_drawn_box_to_device_edges",
        "update_device_box_on_photo",
    ),
)
_from(
    "panelcad.reconstruct.models",
    (
        "DIN_RAIL_HEIGHT_MM",
        "PERSPECTIVE_SCALE_FRACTION",
        "PHOTO_ROLES",
        "Reconstruction",
        "ensure_reconstruction",
        "item_is_layout_ready",
    ),
)
_from("panelcad.reconstruct.guided", ("run_photos_to_drawings",))
_from("panelcad.reconstruct.review_queue", ("FlaggedItem", "ReviewQueue"))
_from(
    "panelcad.reconstruct.service",
    (
        "apply_to_print",
        "record_wire_landing",
        "synthesize_reconstruct_schematic",
    ),
)
_from(
    "panelcad.reconstruct.vision",
    (
        "FakeVision",
        "StubVision",
        "get_backend",
    ),
)

__all__ = [
    "DeviceFeedbackBox",
    "PhotoFeedbackReport",
    "add_missed_device_on_photo",
    "add_missed_strip_on_photo",
    "FlaggedItem",
    "ReviewQueue",
    "apply_to_print",
    "run_photos_to_drawings",
    "build_photo_feedback_model",
    "build_photo_feedback_report",
    "export_photo_feedback",
    "link_closeup_to_device",
    "record_wire_landing",
    "refine_device_box_on_photo",
    "render_annotated_cabinet_photo",
    "snap_drawn_box_to_device_edges",
    "update_device_box_on_photo",
    "synthesize_reconstruct_schematic",
    "DIN_RAIL_HEIGHT_MM",
    "PERSPECTIVE_SCALE_FRACTION",
    "PHOTO_ROLES",
    "FakeVision",
    "Reconstruction",
    "StubVision",
    "ensure_reconstruction",
    "get_backend",
    "item_is_layout_ready",
]


def __getattr__(name: str) -> Any:
    spec = _EXPORTS.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(spec[0])
    value = getattr(module, spec[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
