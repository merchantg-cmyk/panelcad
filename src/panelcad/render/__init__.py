"""SVG and DXF writers.

Importing a sibling such as ``titleblock`` must not import ezdxf. The DXF
writer pulls that library in, and the GUI paints a title block on every startup.
"""

from __future__ import annotations

__all__ = ["sheet_to_svg", "sheet_to_dxf"]


def __getattr__(name: str):
    if name == "sheet_to_svg":
        from panelcad.render.svg import sheet_to_svg

        globals()["sheet_to_svg"] = sheet_to_svg
        return sheet_to_svg
    if name == "sheet_to_dxf":
        from panelcad.render.dxf import sheet_to_dxf

        globals()["sheet_to_dxf"] = sheet_to_dxf
        return sheet_to_dxf
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
