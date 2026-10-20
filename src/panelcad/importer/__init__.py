"""Staged print importer.

An old print (PDF, scan, DXF, or SVG) becomes a native project through a workspace
on disk. Each vision call sees one crop plus a short summary. Vector pages
are read from embedded text and are not sent to a model.
"""

from panelcad.importer.pipeline import ImportJob

__all__ = ["ImportJob"]
