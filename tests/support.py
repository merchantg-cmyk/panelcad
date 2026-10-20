"""Shared test helpers. Not imported by the application."""

from __future__ import annotations

from panelcad.library.panel import (
    apply_confirmed_envelope,
    effective_footprint,
    is_layout_ready,
    occupies_panel_layout,
)


def confirm_layout_occupants(session=None, *, project=None, catalog=None, confirmed_by: str = "test") -> list[str]:
    """Confirm measured envelopes from the current library footprint.

    Packing-algorithm tests need layout-ready parts. Production code must not
    do this — it would stamp generic table sizes as if a person confirmed them.
    """

    project = project or session.project
    catalog = catalog or session.catalog
    seen: set[str] = set()
    confirmed: list[str] = []
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id in seen:
                continue
            device = catalog.devices.get(comp.library_id)
            if device is None:
                continue
            if not occupies_panel_layout(device, comp) or is_layout_ready(device, comp):
                continue
            seen.add(comp.id)
            fp = effective_footprint(device, comp)
            depth = float(fp.depth_mm or 0.0)
            if depth <= 0:
                depth = 70.0
            envelope = apply_confirmed_envelope(
                comp,
                width_mm=max(float(fp.width_mm), 1.0),
                height_mm=max(float(fp.height_mm), 1.0),
                depth_mm=depth,
                mount=str(fp.mount or "din_rail"),
                confirmed_by=confirmed_by,
            )
            for other in project.sheets:
                try:
                    twin = other.component(comp.id)
                except KeyError:
                    continue
                if twin is comp:
                    continue
                twin.properties.update(envelope)
            confirmed.append(comp.id)
    return confirmed


def patch_layout_place_confirms(monkeypatch) -> None:
    """Let packing/cutout tests place a library symbol on a layout sheet.

    Production ``Session.place`` refuses generics. Tests that exercise packing
    or hole math confirm the current library millimetres first — they do not
    change production behavior.
    """

    from panelcad.api.session import Session
    from panelcad.core.layout import is_layout_sheet
    from panelcad.core.models import Component
    from panelcad.library.panel import (
        apply_confirmed_envelope,
        effective_footprint,
        is_layout_ready,
        occupies_panel_layout,
    )

    orig = Session.place
    orig_bom = Session.place_from_bom

    def _place(self, library_id, ref=None, x=40.0, y=40.0, rotation=0, sheet_id=None, properties=None, **kwargs):
        sheet = self.sheet(sheet_id)
        props = dict(properties or {})
        if is_layout_sheet(sheet):
            device = self.catalog.get(library_id)
            ghost = Component(id=str(ref or "tmp"), library_id=library_id, x=0.0, y=0.0, properties=props)
            if occupies_panel_layout(device, ghost) and not is_layout_ready(device, ghost):
                fp = effective_footprint(device, ghost)
                extra = apply_confirmed_envelope(
                    ghost,
                    width_mm=max(float(fp.width_mm), 1.0),
                    height_mm=max(float(fp.height_mm), 1.0),
                    depth_mm=max(float(fp.depth_mm), 1.0) or 70.0,
                    mount=str(fp.mount or "din_rail"),
                    confirmed_by="test",
                )
                props.update(extra)
        kwargs.pop("_sanitize", None)
        return orig(
            self,
            library_id,
            ref=ref,
            x=x,
            y=y,
            rotation=rotation,
            sheet_id=sheet_id,
            properties=props,
            _sanitize=False,
            **kwargs,
        )

    def _place_bom(self, library_id, ref=None, x=40.0, y=40.0, sheet_id=None):
        sheet = self.sheet(sheet_id)
        if is_layout_sheet(sheet) and ref:
            for other in self.project.sheets:
                try:
                    src = other.component(ref)
                except KeyError:
                    continue
                device = self.catalog.get(src.library_id)
                if occupies_panel_layout(device, src) and not is_layout_ready(device, src):
                    fp = effective_footprint(device, src)
                    apply_confirmed_envelope(
                        src,
                        width_mm=max(float(fp.width_mm), 1.0),
                        height_mm=max(float(fp.height_mm), 1.0),
                        depth_mm=max(float(fp.depth_mm), 1.0) or 70.0,
                        mount=str(fp.mount or "din_rail"),
                        confirmed_by="test",
                    )
                break
        return orig_bom(self, library_id, ref=ref, x=x, y=y, sheet_id=sheet_id)

    orig_layout = Session.layout_from_schematic

    def _layout_from_schematic(self, sheet_id=None):
        confirm_layout_occupants(self)
        return orig_layout(self, sheet_id)

    monkeypatch.setattr(Session, "place", _place)
    monkeypatch.setattr(Session, "place_from_bom", _place_bom)
    monkeypatch.setattr(Session, "layout_from_schematic", _layout_from_schematic)

    from panelcad.core import circuit_layout

    orig_pack = circuit_layout.pack_circuit_layouts

    def _pack(session, parts, **kwargs):
        confirm_layout_occupants(session)
        return orig_pack(session, parts, **kwargs)

    monkeypatch.setattr(circuit_layout, "pack_circuit_layouts", _pack)
