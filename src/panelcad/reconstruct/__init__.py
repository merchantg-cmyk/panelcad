"""As-built reconstruction from real panel photos."""

from panelcad.reconstruct.feedback import (
    DeviceFeedbackBox,
    PhotoFeedbackReport,
    add_missed_device_on_photo,
    add_missed_strip_on_photo,
    build_photo_feedback_model,
    build_photo_feedback_report,
    export_photo_feedback,
    link_closeup_to_device,
    refine_device_box_on_photo,
    render_annotated_cabinet_photo,
    snap_drawn_box_to_device_edges,
    update_device_box_on_photo,
)
from panelcad.reconstruct.models import (
    DIN_RAIL_HEIGHT_MM,
    PERSPECTIVE_SCALE_FRACTION,
    PHOTO_ROLES,
    Reconstruction,
    ensure_reconstruction,
    item_is_layout_ready,
)
from panelcad.reconstruct.guided import run_photos_to_drawings
from panelcad.reconstruct.review_queue import FlaggedItem, ReviewQueue
from panelcad.reconstruct.service import (
    apply_to_print,
    record_wire_landing,
    synthesize_reconstruct_schematic,
)
from panelcad.reconstruct.vision import FakeVision, StubVision, get_backend

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
