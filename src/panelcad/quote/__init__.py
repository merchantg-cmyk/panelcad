"""Shop quote built from the drawing.

Prices come only from a price list the user imports, fields the user stored on
a library part, or amounts typed into the quote. Labor minutes and shop rates
ship as example defaults the shop is expected to edit. They are not industry
standards. A quote is written to a file. Nothing is sent.
"""

from panelcad.quote.build import build_quote
from panelcad.quote.labor import drawing_counts, example_labor_table, set_cutout_counter

__all__ = [
    "build_quote",
    "drawing_counts",
    "example_labor_table",
    "set_cutout_counter",
]
