"""Communication-port, network, and wire-by-wire methods for :class:`panelcad.api.session.Session`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from panelcad.core.models import CommPort, Component, NetworkInfo, Sheet
from panelcad.library.ports import (
    default_kind_side,
    effective_ports,
    parse_port,
    port_matches_medium,
    retain_known_ip,
)


class SessionPortsMixin:
    """Port/network CRUD, layout, and wire-by-wire walkthrough methods."""

    def _assert_pin(self, sheet: Sheet, pin_ref: str) -> None:
        from panelcad.core.checks import assert_pin_ref

        assert_pin_ref(self.project, self.catalog, sheet, pin_ref)

    def _ensure_instance_ports(self, ref: str, sheet_id: str | None = None) -> Component:
        sheet = self.sheet(sheet_id)
        comp = sheet.component(ref)
        if not comp.ports:
            device = self.catalog.get(comp.library_id)
            comp.ports = [p.model_copy() for p in device.ports]
        return comp

    def list_ports(self, ref: str, sheet_id: str | None = None) -> list[dict[str, Any]]:
        sheet = self.sheet(sheet_id)
        comp = sheet.component(ref)
        device = self.catalog.get(comp.library_id)
        return [p.model_dump() for p in effective_ports(device, comp)]

    def add_port(
        self,
        ref: str,
        port_id: str,
        kind: str = "serial",
        medium: str = "",
        protocol: str = "",
        connector: str = "",
        side: str | None = None,
        sheet_id: str | None = None,
    ) -> CommPort:
        comp = self._ensure_instance_ports(ref, sheet_id)
        pid = port_id.strip() or "COM"
        if any(p.id == pid for p in comp.ports):
            raise ValueError(f"{ref} already has port '{pid}'")
        port = parse_port(
            {
                "id": pid,
                "kind": kind,
                "medium": medium,
                "protocol": protocol,
                "connector": connector,
                "side": side or default_kind_side(kind),  # type: ignore[arg-type]
            }
        )
        comp.ports.append(port)
        return port

    def set_port(self, ref: str, port_id: str, sheet_id: str | None = None, **fields: Any) -> CommPort:
        comp = self._ensure_instance_ports(ref, sheet_id)
        for i, p in enumerate(comp.ports):
            if p.id != port_id:
                continue
            data = p.model_dump()
            data.update({k: v for k, v in fields.items() if v is not None})
            if "id" in fields and fields["id"]:
                data["id"] = str(fields["id"])
            comp.ports[i] = parse_port(data)
            return comp.ports[i]
        raise KeyError(f"{ref} has no port '{port_id}'")

    def delete_port(self, ref: str, port_id: str, sheet_id: str | None = None) -> None:
        comp = self._ensure_instance_ports(ref, sheet_id)
        before = len(comp.ports)
        comp.ports = [p for p in comp.ports if p.id != port_id]
        if len(comp.ports) == before:
            raise KeyError(f"{ref} has no port '{port_id}'")

    def set_network(
        self,
        name: str | None = None,
        medium: str | None = None,
        protocol: str | None = None,
        sheet_id: str | None = None,
    ) -> NetworkInfo:
        sheet = self.sheet(sheet_id)
        info = sheet.network or NetworkInfo()
        if name is not None:
            info.name = name
        if medium is not None:
            info.medium = medium  # type: ignore[assignment]
        if protocol is not None:
            info.protocol = protocol
        sheet.network = info
        if sheet.kind != "network":
            sheet.kind = "network"
        return info

    def layout_network(self, sheet_id: str | None = None, medium: str | None = None) -> dict[str, Any]:
        """Place devices that have matching comms ports onto a network sheet.

        A stored IP is copied onto the network node. A blank IP stays blank,
        and an explicit TBD stays TBD. This does not assign 192.168.x.x or
        any other address.
        """
        dest = self.sheet(sheet_id)
        if dest.kind != "network":
            dest.kind = "network"
        info = dest.network or NetworkInfo(name=dest.title or dest.id)
        filt = medium or (info.medium if info.medium != "mixed" else None)
        if medium:
            info.medium = medium  # type: ignore[assignment]
        dest.network = info
        dest.components = []
        dest.wires = []
        x, y = 36.0, 36.0
        placed = 0
        seen: set[str] = set()
        next_node = 1
        for src in self.project.sheets:
            if src.kind not in ("schematic", "io"):
                continue
            for comp in src.components:
                if comp.id in seen:
                    continue
                device = self.catalog.get(comp.library_id)
                ports = [p for p in effective_ports(device, comp) if port_matches_medium(p, filt)]
                if not ports:
                    continue
                seen.add(comp.id)
                props = dict(comp.properties)
                retain_known_ip(props)
                if filt == "serial" or any(p.kind == "serial" for p in ports):
                    if not str(props.get("node") or props.get("station") or "").strip():
                        props["node"] = str(next_node)
                        props["station"] = str(next_node)
                        next_node += 1
                for key in ("node", "station"):
                    if key in props:
                        comp.properties[key] = props[key]
                for key in ("ip", "address"):
                    if key in props and str(props.get(key) or "").strip():
                        comp.properties[key] = props[key]
                    else:
                        comp.properties.pop(key, None)
                dest.components.append(
                    Component(
                        id=comp.id,
                        library_id=comp.library_id,
                        x=x,
                        y=y,
                        properties=props,
                        ports=list(comp.ports),
                    )
                )
                placed += 1
                x += 90.0
                if x > 340.0:
                    x = 36.0
                    y += 70.0
        return {"sheet": dest.id, "medium": info.medium, "devices": placed}

    def mark_reference(
        self,
        ref: str,
        sheet_id: str | None = None,
        primary_sheet: str | None = None,
    ) -> dict[str, Any]:
        """Mark the copy of ``ref`` on ``sheet_id`` as a reference of the primary device.

        The copy keeps its library_id, position, and wires. It stops counting
        as a second device for duplicate_device_tag, and the index and print
        show "see <sheet-zone>" for it. ``primary_sheet`` defaults to the first
        other sheet that has ``ref`` and is not itself a reference. One undo.
        """

        from panelcad.core.references import REFERENCE_PROP, reference_target

        sheet = self.sheet(sheet_id)
        comp = sheet.component(ref)
        others = [
            other
            for other in self.project.sheets
            if other is not sheet and any(c.id == comp.id for c in other.components)
        ]
        if primary_sheet:
            target = self.project.sheet(primary_sheet)
            if target is sheet:
                raise ValueError(f"{comp.id}: the primary sheet must be a different sheet than {sheet.id}")
            if target not in others:
                raise KeyError(f"no component '{comp.id}' on sheet '{target.id}'")
        else:
            usable = [o for o in others if not reference_target(o.component(comp.id))]
            if not usable:
                raise ValueError(f"{comp.id} is not drawn on any other sheet to reference")
            target = usable[0]
        comp.properties[REFERENCE_PROP] = target.id
        return {"id": comp.id, "sheet": sheet.id, "reference_of": target.id}

    def clear_reference(self, ref: str, sheet_id: str | None = None) -> dict[str, Any]:
        """Undo ``mark_reference``: the copy on ``sheet_id`` counts as its own device again."""

        from panelcad.core.references import REFERENCE_PROP

        sheet = self.sheet(sheet_id)
        comp = sheet.component(ref)
        was = str(comp.properties.pop(REFERENCE_PROP, "") or "")
        return {"id": comp.id, "sheet": sheet.id, "cleared": bool(was)}

    def set_sheet_overview(self, overview: bool = True, sheet_id: str | None = None) -> dict[str, Any]:
        """Flag a sheet as an overview page. Its device copies become references. One undo."""

        sheet = self.sheet(sheet_id)
        sheet.overview = bool(overview)
        return {"sheet": sheet.id, "overview": sheet.overview}

    def layout_io(self, sheet_id: str | None = None) -> dict[str, Any]:
        """ANSI I/O: field in from the left, loads out to the right.

        Input cards default to the right, output cards to the left, combo in the middle.
        """
        from panelcad.core.cleanup import arrange_io
        from panelcad.render.io_ladder import is_io_module

        dest = self.sheet(sheet_id)
        dest.kind = "io"
        if not dest.components:
            seen: set[str] = set()
            for src in self.project.sheets:
                if src.id == dest.id or src.kind not in ("schematic", "io"):
                    continue
                for c in src.components:
                    if c.id in seen:
                        continue
                    device = self.catalog.get(c.library_id)
                    cat = device.category
                    if not (
                        is_io_module(device)
                        or cat.startswith("operator")
                        or "relay" in cat
                        or cat.startswith("drive")
                        or cat.startswith("load")
                    ):
                        continue
                    seen.add(c.id)
                    dest.components.append(
                        Component(
                            id=c.id,
                            library_id=c.library_id,
                            x=c.x,
                            y=c.y,
                            properties=dict(c.properties),
                            ports=list(c.ports),
                        )
                    )
        packed = arrange_io(dest, self.catalog)
        return {"sheet": dest.id, "modules": packed["modules"], "field": packed["field"]}


    # ------------------------------------------------------------------
    # Backpanel Wire Routing, Zip-Tie Bundles & Technician Walkthrough
    # ------------------------------------------------------------------
    def route_panel_wires(self, sheet_id: str | None = None) -> list[Any]:
        from panelcad.core.panel_wiring import route_panel_wires

        return route_panel_wires(self, sheet_id=sheet_id)

    def build_wire_by_wire_schedule(self, sheet_id: str | None = None) -> Any:
        from panelcad.core.panel_wiring import build_wire_by_wire_schedule

        return build_wire_by_wire_schedule(self, sheet_id=sheet_id)

    def add_zip_tie_slice(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        sheet_id: str | None = None,
    ) -> Any:
        from panelcad.core.panel_wiring import add_zip_tie_slice

        return add_zip_tie_slice(self, sheet_id, x1, y1, x2, y2)

    def add_zip_tie_waypoint(
        self,
        x: float,
        y: float,
        wire_numbers: list[str] | None = None,
        orientation: str = "h",
        sheet_id: str | None = None,
    ) -> Any:
        from panelcad.core.panel_wiring import add_zip_tie_waypoint

        return add_zip_tie_waypoint(
            self,
            sheet_id,
            x,
            y,
            wire_numbers=wire_numbers,
            orientation=orientation,
        )

    def move_zip_tie_waypoint(
        self,
        zip_tie_id: str,
        new_x: float,
        new_y: float,
        sheet_id: str | None = None,
    ) -> Any:
        from panelcad.core.panel_wiring import move_zip_tie_waypoint

        return move_zip_tie_waypoint(self, sheet_id, zip_tie_id, new_x, new_y)

    def remove_zip_tie_waypoint(
        self,
        zip_tie_id: str,
        sheet_id: str | None = None,
    ) -> bool:
        from panelcad.core.panel_wiring import remove_zip_tie_waypoint

        return remove_zip_tie_waypoint(self, sheet_id, zip_tie_id)

    def list_zip_tie_waypoints(self, sheet_id: str | None = None) -> list[Any]:
        from panelcad.core.panel_wiring import list_zip_tie_waypoints

        return list_zip_tie_waypoints(self, sheet_id=sheet_id)

    def set_panel_wiring_mode(self, mode: str, sheet_id: str | None = None) -> dict[str, Any]:
        from panelcad.core.panel_wiring import set_panel_wiring_mode

        return set_panel_wiring_mode(self, mode, sheet_id=sheet_id)

    def get_panel_wiring_mode(self, sheet_id: str | None = None) -> str:
        from panelcad.core.panel_wiring import get_panel_wiring_mode

        return get_panel_wiring_mode(self, sheet_id=sheet_id)

    def set_walkthrough_step(self, step_index: int, sheet_id: str | None = None) -> dict[str, Any]:
        from panelcad.core.panel_wiring import set_walkthrough_step

        return set_walkthrough_step(self, step_index, sheet_id=sheet_id)

    def get_walkthrough_step(self, sheet_id: str | None = None) -> int:
        from panelcad.core.panel_wiring import get_walkthrough_step

        return get_walkthrough_step(self, sheet_id=sheet_id)

    def step_walkthrough(self, delta: int = 1, sheet_id: str | None = None) -> dict[str, Any]:
        from panelcad.core.panel_wiring import step_walkthrough

        return step_walkthrough(self, delta=delta, sheet_id=sheet_id)

    def mark_wire_installed(
        self,
        wire_number_or_index: str | int,
        installed: bool = True,
        sheet_id: str | None = None,
    ) -> dict[str, Any]:
        from panelcad.core.panel_wiring import mark_wire_installed

        return mark_wire_installed(
            self,
            wire_number_or_index,
            installed=installed,
            sheet_id=sheet_id,
        )

    def add_wire_by_wire_sheet(
        self,
        sheet_id: str = "wire_by_wire",
        title: str = "Wire-by-Wire Cut Sheet",
        layout_sheet_id: str | None = None,
    ) -> Sheet:
        from panelcad.core.panel_wiring import add_wire_by_wire_sheet

        return add_wire_by_wire_sheet(
            self,
            sheet_id=sheet_id,
            title=title,
            layout_sheet_id=layout_sheet_id,
        )

    def export_wire_by_wire_csv(
        self,
        path: Path | str,
        sheet_id: str | None = None,
    ) -> Path:
        from panelcad.core.panel_wiring import export_wire_by_wire_csv

        return export_wire_by_wire_csv(self, path, sheet_id=sheet_id)

    def render_walkthrough_step_png(
        self,
        step_index: int,
        output_path: Path | str,
        sheet_id: str | None = None,
        dpi: float = 200.0,
    ) -> Path:
        from panelcad.core.panel_wiring import render_walkthrough_step_png

        return render_walkthrough_step_png(
            self,
            step_index,
            output_path,
            sheet_id=sheet_id,
            dpi=dpi,
        )
