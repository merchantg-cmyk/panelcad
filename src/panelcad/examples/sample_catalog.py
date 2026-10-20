"""Sample project made only of catalog parts with published outlines.

The start page opens a copy. Generic placeholder sizes are not used.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from panelcad.api.session import Session

# Published footprints. Each row is (ref, library_id, catalog, manufacturer, x, y).
_POWER = (
    ("Q1", "iec.breakers.mcb_3p_32a", "FAZ-C6-3-NA-L", "AutomationDirect", 40, 22),
    ("K1", "iec.contactors.ac3_9a", "GH15BN-3-10A", "AutomationDirect", 38, 82),
    ("F1", "iec.protection.overload_9a", "MT-32/3K7.5", "LS Electric", 40, 155),
)
_CONTROL = (
    ("G1", "iec.power.psu_24v_5a", "PSP24-120S", "AutomationDirect", 40, 24),
    ("A1", "iec.plc.compact_16dio", "C0-00DD1-D", "AutomationDirect", 120, 80),
    ("H1", "iec.operators.pilot_light", "GCX1232-24L", "AutomationDirect", 250, 30),
)

SAMPLE_FILENAME = "sample_catalog.pcad.yaml"


def sample_yaml_path() -> Path:
    return Path(__file__).resolve().parents[3] / "examples" / SAMPLE_FILENAME


def _place_catalog(session: Session, rows: tuple[tuple[str, str, str, str, int, int], ...]) -> None:
    for ref, library_id, catalog, manufacturer, x, y in rows:
        session.place(library_id, ref=ref, x=x, y=y)
        session.assign_part(ref, catalog, manufacturer=manufacturer)


def _prefer_catalog_box(recommendation: dict) -> dict:
    """Keep a listed box that has a real catalog number when one fits the pack."""

    from panelcad.library.enclosures import load_enclosures

    box = recommendation.get("enclosure") or {}
    if str(box.get("catalog") or "").strip():
        return recommendation
    needed = recommendation.get("needed") or {}
    need_w = float(needed.get("packed_width_mm") or needed.get("width_mm") or 0)
    need_h = float(needed.get("packed_height_mm") or needed.get("height_mm") or 0)
    need_d = float(needed.get("depth_mm") or 0)
    fits = [
        item
        for item in load_enclosures().boxes
        if item.catalog
        and item.panel_width_mm + 1.0 >= need_w
        and item.panel_height_mm + 1.0 >= need_h
        and item.depth_mm + 1.0 >= need_d
    ]
    if not fits:
        fits = [item for item in load_enclosures().boxes if item.catalog]
    if not fits:
        return recommendation
    chosen = min(fits, key=lambda item: (item.panel_width_mm * item.panel_height_mm, item.depth_mm))
    updated = dict(recommendation)
    row = chosen.to_row()
    row["name"] = chosen.display_name()
    updated["enclosure"] = row
    return updated


def build_sample(name: str = "Sample catalog starter") -> Session:
    """DOL fragment: breaker, contactor, overload, supply, PLC, and pilot light."""

    session = Session.create(name, sheet_id="power", title="Power")
    session.set_title_block(
        company="PanelCAD",
        drawing_number="SAMPLE-001",
        drawn_by="PanelCAD",
        scale="NTS",
        standard="IEC",
        rev="A",
        date="2026-10-14",
        rev_description="Catalog-only sample",
        description="Power: catalog breaker, contactor, and overload",
    )
    _place_catalog(session, _POWER)
    session.wire("Q1.2", "K1.L1", net="L1")
    session.wire("Q1.4", "K1.L2", net="L2")
    session.wire("Q1.6", "K1.L3", net="L3")
    session.wire("K1.T1", "F1.1", net="T1")
    session.wire("K1.T2", "F1.3", net="T2")
    session.wire("K1.T3", "F1.5", net="T3")

    session.add_sheet("control", title="Control", kind="schematic")
    session.sheet().description = "24 V supply, PLC, and pilot light — catalog outlines"
    _place_catalog(session, _CONTROL)
    session.wire("G1.plus", "A1.Lplus", net="24V")
    session.wire("G1.minus", "A1.M", net="0V")
    session.wire("A1.Q0_1", "H1.X1", net="RUN_LT")
    session.wire("H1.X2", "G1.minus", net="0V")

    session.add_sheet("enclosure", title="Enclosure", kind="panel_layout")
    session.sheet().description = "Listed enclosure. Devices use published catalog sizes."
    recommendation = _prefer_catalog_box(session.recommend_enclosure(sheet_id="enclosure"))
    session.apply_enclosure(sheet_id="enclosure", place_parts=True, recommendation=recommendation)
    # The packer drops generic end stops on the rails. They are not catalog parts.
    for sheet in session.project.sheets:
        sheet.components = [
            comp for comp in sheet.components if not str(comp.library_id).endswith("end_stop")
        ]
    session.set_active_sheet("power")
    return session


def write_sample(path: Path | str | None = None) -> Path:
    dest = Path(path) if path is not None else sample_yaml_path()
    return build_sample().save(dest)


def copy_sample_project(dest_dir: Path | str) -> Path:
    """Copy the shipped sample so the user edits a new file."""

    folder = Path(dest_dir)
    folder.mkdir(parents=True, exist_ok=True)
    source = sample_yaml_path()
    if not source.is_file():
        write_sample(source)
    dest = folder / "Sample catalog starter.pcad.yaml"
    number = 2
    while dest.exists():
        dest = folder / f"Sample catalog starter {number}.pcad.yaml"
        number += 1
    shutil.copy(source, dest)
    return dest
