"""Pydantic AI agent for one PanelCAD sheet.

Tools call the existing JSONL operator, so a bad library id or a bad pin
comes back as a retry instead of a diagonal or a stand-in box. The model
string defaults to local Ollama ``gemma3:4b`` and can be swapped without
changing the tools.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal
from pydantic import Field
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.providers.ollama import OllamaProvider

from panelcad.cli.agent import Agent as PanelOperator

DEFAULT_MODEL = "ollama:gemma3:4b"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/v1"

INSTRUCTIONS = """
You draw one industrial control panel sheet in PanelCAD by calling tools.
Search the library before you invent a library_id.
Search manufacturer catalog parts with parts_search before you invent a SKU.
Assign a verified catalog with assign_part. Do not invent catalog numbers.
A backpanel or door part is layout-ready only with a real catalog footprint or a person-confirmed envelope. Never invent width, height, or depth. confirm_envelope / set_panel confirm=true require explicit positive width, height, depth, and mount — do not confirm a table-fill. If the user did not give a SKU or sizes, ask — then assign_part or confirm_envelope. Do not place a generic symbol on panel_layout or door_layout.
insert_circuit can take a manufacturer (AutomationDirect, LS Electric, Yaskawa) and assigns that vendor's published SKUs. secondary_manufacturer fills categories that brand does not make (omit it for the project vendor, else AutomationDirect; none leaves those symbols generic). Fallback rows are marked catalog_fallback. Generic symbols stay off the layout until a catalog part or confirmed envelope exists.
On a ladder, a control relay (CR, MCR) is a coil circle (contactors.coil), not a 4-pole relay box.
Do not place power.load_24v or notes.block. Those are not the device.
Wire pin to pin, for example CR1.A2 to LT1.X1. The router keeps the path orthogonal and off device bodies.
Place devices on the spacing grid: minimum horizontal and vertical clearance, spread across the usable sheet width. Call cleanup after place or insert_circuit. page_density reports how full each page is. The limit is the usable area (max devices and rungs), not a fixed count. When a page is over that limit the Problems list warns. Call split_sheet with apply=true to continue on the next sheet with off-page references. Do not keep adding devices to an over-dense page.
To size the box, call recommend_enclosure (does not write) then apply_enclosure. It aims for 60-70% back-panel fill plus the spare-space percent and wire-duct clearance, and picks the next real catalog enclosure up from the tightest fit. Never invent a width, height, or depth.
For heat in the box that is already chosen, call compute_heat_load. set_ambient stores the temperatures. Neither resizes the enclosure.
insert_circuit builds a motor starter (dol, vfd, reversing, star_delta, soft_starter, plc_motor).
place_layout (default true) puts in-cabinet devices on the panel and operators on the door.
Do not invent tags, wire numbers, or ratings the user did not give you.
To convert between IEC and NEMA, call convert_standard, or step with convert_standard_plan then convert_standard_next until it is done.
Troubleshooting tools (troubleshoot_guide, circuit_for, checkpoints, upstream, downstream, plc_point, likely_causes) are read-only. troubleshoot_guide is the same walk as the Troubleshoot dock: the next check, the fault window, and ranked causes. Cite only wire numbers and terminals those tools return. Leave safety devices, overloads, and PLC points in the circuit. Do not tell the user to jumper, bypass, force, defeat, or override them.
check_control_power reports supply loading. It does not invent coil or PSU ratings.
Size a motor branch with size_motor_circuit and review it with check_sizing. Do not invent breaker, fuse, overload, or wire ratings.
Cutout drawings: cutouts_generate, then cutouts_export_dxf or cutouts_export_pdf for the 1:1 shop file. Do not invent a hole; part_set_cutout unknown=true when the drawing is not on file. unknown=false restores the opening.
Finish with a short sentence naming the refs you placed and the pins you wired.
For as-built work, photos of the physical panel are the source of truth. Use reconstruct_* tools: add photos, identify labels, confirm catalog or envelope (never place a generic placeholder), ask the next floor question, record the answer, show a photo crop, list unconfirmed items. An old print is only a secondary cross-check (reconstruct_compare_print).
""".strip()


@dataclass
class Drawing:
    """The open PanelCAD operator. Tools mutate this, not a copy of the YAML."""

    operator: PanelOperator
    path: Path | None = None


def model_for(spec: str) -> Model | str:
    """Turn ``ollama:gemma3:4b`` into a local client. Other specs pass through."""

    if spec.startswith("ollama:"):
        name = spec.split(":", 1)[1]
        base = os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL)
        return OllamaModel(name, provider=OllamaProvider(base_url=base))
    return spec


drawing_agent = Agent(
    model_for(DEFAULT_MODEL),
    name="panelcad_drawing",
    deps_type=Drawing,
    instructions=INSTRUCTIONS,
)


def open_drawing(path: Path | None = None, *, name: str = "Panel") -> Drawing:
    """Open a ``.pcad.yaml`` or start a new sheet the tools can save later."""

    operator = PanelOperator()
    if path is not None and path.is_file():
        reply = operator.handle({"op": "open", "path": str(path)})
        if not reply.get("ok"):
            raise RuntimeError(str(reply.get("error") or "open failed"))
        return Drawing(operator=operator, path=path)
    reply = operator.handle({"op": "create_project", "name": name})
    if not reply.get("ok"):
        raise RuntimeError(str(reply.get("error") or "create_project failed"))
    drawing = Drawing(operator=operator, path=path)
    if path is not None:
        saved = operator.handle({"op": "save", "path": str(path)})
        if not saved.get("ok"):
            raise RuntimeError(str(saved.get("error") or "save failed"))
    return drawing


def _run(ctx: RunContext[Drawing], op: str, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"op": op}
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    reply = ctx.deps.operator.handle(payload)
    if not reply.get("ok"):
        message = str(reply.get("error") or f"{op} failed")
        crash = reply.get("crash") if isinstance(reply.get("crash"), dict) else {}
        if crash.get("id"):
            message += f" (crash_id={crash.get('id')} crash_path={crash.get('path')})"
        raise ModelRetry(message)
    result = reply.get("result")
    if isinstance(result, dict):
        return result
    return {"result": json.dumps(result, default=str)}


@drawing_agent.tool
def list_sheets(ctx: RunContext[Drawing]) -> dict[str, Any]:
    """List the pages already in this project and which one is active."""

    return _run(ctx, "list_sheets")


@drawing_agent.tool
def library_search(ctx: RunContext[Drawing], query: str) -> dict[str, Any]:
    """Find catalog symbols by words such as coil, fuse, pilot, or plc. Use the returned library_id."""

    return _run(ctx, "library_search", query=query)


@drawing_agent.tool
def parts_search(
    ctx: RunContext[Drawing],
    query: str = "",
    manufacturer: str | None = None,
    category: str | None = None,
    library_id: str | None = None,
    amps_min: float | None = None,
    amps_max: float | None = None,
) -> dict[str, Any]:
    """Search verified manufacturer catalog SKUs. Filter by manufacturer, category, library_id, or amps_min/amps_max (adjustable thermal range). Returns catalog, verified, source, ratings, range. Do not invent catalog numbers."""

    return _run(
        ctx,
        "parts_search",
        query=query,
        manufacturer=manufacturer,
        category=category,
        library_id=library_id,
        amps_min=amps_min,
        amps_max=amps_max,
    )


@drawing_agent.tool
def assign_part(
    ctx: RunContext[Drawing],
    ref: str,
    catalog: str,
    manufacturer: str | None = None,
) -> dict[str, Any]:
    """Assign a verified catalog SKU to a placed tag. Keeps library_id and wiring. One undo. Writes ratings including amps_min/amps_max range onto the device."""

    return _run(ctx, "assign_part", ref=ref, catalog=catalog, manufacturer=manufacturer)


@drawing_agent.tool
def confirm_envelope(
    ctx: RunContext[Drawing],
    ref: str,
    width_mm: float,
    height_mm: float,
    depth_mm: float,
    mount: Literal["din_rail", "backplate", "door", "field", "none"],
    confirmed_by: str | None = None,
) -> dict[str, Any]:
    """Store a person-confirmed body size. width_mm, height_mm, depth_mm, and mount are required. Only call with millimetres the user gave. Never invent dimensions or confirm a table-fill."""

    return _run(
        ctx,
        "confirm_envelope",
        ref=ref,
        width_mm=width_mm,
        height_mm=height_mm,
        depth_mm=depth_mm,
        mount=mount,
        confirmed_by=confirmed_by,
    )


@drawing_agent.tool
def set_drawing_style(
    ctx: RunContext[Drawing],
    drawing_style: Literal["ladder", "schematic", "freeform"],
) -> dict[str, Any]:
    """Set how this sheet is drawn. Power and network pages are schematic, not ladder."""

    return _run(ctx, "set_title_block", drawing_style=drawing_style)


@drawing_agent.tool
def place(
    ctx: RunContext[Drawing],
    library_id: str,
    x: float,
    y: float,
    ref: str | None = None,
) -> dict[str, Any]:
    """Place one catalog symbol at x, y millimetres. ref is the tag on the sheet, such as CR1.

    On a panel_layout or door_layout sheet the part must already be layout-ready
    (assigned catalog dimensions, or a confirmed envelope). Do not invent sizes.
    After a batch of places, call cleanup so symbols sit on the spacing grid.
    """

    lib = library_id.lower()
    if "power.load" in lib or lib.endswith("notes.block"):
        raise ModelRetry(
            "That symbol is not a device. Search the library and place the coil, button, "
            "light, stack light, PLC, HMI, or drive that is drawn."
        )
    return _run(ctx, "place", library_id=library_id, x=x, y=y, ref=ref)


@drawing_agent.tool
def cleanup(ctx: RunContext[Drawing], sheet: str | None = None) -> dict[str, Any]:
    """Spread devices on the spacing grid and report page fullness. Run after place or insert_circuit."""

    return _run(ctx, "cleanup", sheet=sheet)


@drawing_agent.tool
def page_density(ctx: RunContext[Drawing], sheet: str | None = None) -> dict[str, Any]:
    """How full each page is. max_devices and max_rungs come from the usable area. over_dense means split the page."""

    return _run(ctx, "page_density", sheet=sheet)


@drawing_agent.tool
def split_sheet(
    ctx: RunContext[Drawing],
    sheet: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Recommend a continuation when the page is over the area limit. apply=true moves the overflow and adds off-page references."""

    return _run(ctx, "split_sheet", sheet=sheet, apply=apply)


@drawing_agent.tool
def wire(
    ctx: RunContext[Drawing],
    frm: str,
    to: str,
    net: str | None = None,
) -> dict[str, Any]:
    """Connect two pins, such as CR1.A2 and LT1.X1. Leave the path to the router."""

    return _run(ctx, "wire", **{"from": frm, "to": to, "net": net})


@drawing_agent.tool
def list_components(ctx: RunContext[Drawing]) -> dict[str, Any]:
    """List devices on the active sheet, including pin ids, after a place."""

    return _run(ctx, "list_components")


@drawing_agent.tool
def validate(ctx: RunContext[Drawing]) -> dict[str, Any]:
    """Check the open sheet. Fix reported errors before saving."""

    return _run(ctx, "validate")


@drawing_agent.tool
def update_cross_references(ctx: RunContext[Drawing]) -> dict[str, Any]:
    """Write coil/contact cross-references into the project (properties.xref and off-page callouts). Display is already live; this persists them."""

    return _run(ctx, "update_cross_references")


@drawing_agent.tool
def device_index(
    ctx: RunContext[Drawing],
    ref: Annotated[str | None, Field(description="Device tag (M1, CB1, VFD1). Omit for the full index.")] = None,
) -> dict[str, Any]:
    """Where every element of a multi-page device lives: page, zone, element type, terminals, wire numbers."""

    return _run(ctx, "device_index", ref=ref)


@drawing_agent.tool
def lookup_zone(
    ctx: RunContext[Drawing],
    zone: Annotated[str, Field(description="Printed page-zone: 5-A2, A2, /1.3, 105, or sheet:A2.")],
    sheet: Annotated[str | None, Field(description="Sheet id when the zone has no page number.")] = None,
) -> dict[str, Any]:
    """What device or element sits at a printed page-zone."""

    return _run(ctx, "lookup_zone", zone=zone, sheet=sheet)


@drawing_agent.tool
def trace_wire(
    ctx: RunContext[Drawing],
    number: Annotated[str, Field(description="Wire number as drawn (101, L1, 24V).")],
) -> dict[str, Any]:
    """Follow a wire number across sheets: from/to, pages, and devices."""

    return _run(ctx, "trace_wire", number=number)


@drawing_agent.tool
def circuit_for(
    ctx: RunContext[Drawing],
    ref: Annotated[str, Field(description="Device tag on the drawing (M1, K1, CR1).")],
) -> dict[str, Any]:
    """Read-only control and power path for a device. Missing voltages are needs-info."""

    return _run(ctx, "circuit_for", ref=ref)


@drawing_agent.tool
def checkpoints(
    ctx: RunContext[Drawing],
    ref: Annotated[str, Field(description="Device tag on the drawing.")],
    symptom: Annotated[
        str,
        Field(
            description=(
                "motor_wont_start, motor_trips, wont_stop, no_control_power, "
                "plc_input_not_seen, plc_output_on_load_off, or vfd_faulted."
            )
        ),
    ],
) -> dict[str, Any]:
    """Ordered checks. Each check is LIVE or DE-ENERGIZED. Expected values come from the drawing."""

    return _run(ctx, "checkpoints", ref=ref, symptom=symptom)


@drawing_agent.tool
def upstream(
    ctx: RunContext[Drawing],
    endpoint: Annotated[str, Field(description="Drawing terminal as ref.pin, for example K1.A1.")],
) -> dict[str, Any]:
    """Read-only walk from a terminal toward the supply. Cites only drawing wires and terminals."""

    return _run(ctx, "upstream", endpoint=endpoint)


@drawing_agent.tool
def downstream(
    ctx: RunContext[Drawing],
    endpoint: Annotated[str, Field(description="Drawing terminal as ref.pin, for example K1.A1.")],
) -> dict[str, Any]:
    """Read-only walk from a terminal toward the load or return."""

    return _run(ctx, "downstream", endpoint=endpoint)


@drawing_agent.tool
def plc_point(
    ctx: RunContext[Drawing],
    address: Annotated[str, Field(description="PLC address, point label, or tag that is on the drawing.")],
) -> dict[str, Any]:
    """One PLC I/O point from the drawing. Does not read or write a PLC program."""

    return _run(ctx, "plc_point", address=address)


@drawing_agent.tool
def likely_causes(
    ctx: RunContext[Drawing],
    ref: Annotated[str, Field(description="Device tag on the drawing.")],
    symptom: Annotated[str, Field(description="Same symptom ids as checkpoints.")],
    readings: Annotated[
        str | None,
        Field(
            description=(
                "Optional JSON list. Each item is {id or device plus terminal or wire, "
                "result: ok|wrong|skipped, measured?}."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Ranked causes from the drawing. A first wrong reading narrows the fault to that segment."""

    return _run(ctx, "likely_causes", ref=ref, symptom=symptom, readings=readings)


@drawing_agent.tool
def troubleshoot_guide(
    ctx: RunContext[Drawing],
    ref: Annotated[str, Field(description="Device tag on the drawing.")],
    symptom: Annotated[str, Field(description="Same symptom ids as checkpoints.")],
    readings: Annotated[
        str | None,
        Field(
            description=(
                "Optional JSON list. Each item is {id or device plus terminal or wire, "
                "result: ok|wrong|skipped, measured?}."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Read-only guided next check. Same walk the Troubleshoot dock shows. Cite only returned wires and terminals."""

    return _run(ctx, "troubleshoot_guide", ref=ref, symptom=symptom, readings=readings)


@drawing_agent.tool
def set_cross_reference(
    ctx: RunContext[Drawing],
    rung_pitch_mm: Annotated[
        float | None,
        Field(description="NEMA/JIC line pitch in millimetres. 12.7 mm is 0.5 in. Line = sheet*100 + rung."),
    ] = None,
    nc_mark: Literal["underline", "bar", "parentheses"] | None = None,
    rung_numbers: Literal["used", "all"] | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Set NEMA/JIC line pitch (rung_pitch_mm, default 12.7 mm = 0.5 in; line = sheet*100 + rung) and other cross-reference display settings (nc_mark, rung_numbers, enabled)."""

    return _run(
        ctx,
        "set_cross_reference",
        rung_pitch_mm=rung_pitch_mm,
        nc_mark=nc_mark,
        rung_numbers=rung_numbers,
        enabled=enabled,
    )


@drawing_agent.tool
def set_phase_colors(
    ctx: RunContext[Drawing],
    enabled: bool | None = None,
    system_voltage: Annotated[
        str | None,
        Field(
            description=(
                "Volts that pick the NEMA palette, or 'auto' to follow the drawing. "
                "Below nema_high_voltage (default 300) the 208/240 V US shop convention is black, red, blue. "
                "At or above it the 480 V US shop convention is brown, orange, yellow."
            )
        ),
    ] = None,
    nema_high_voltage: Annotated[
        float | None,
        Field(description="NEMA uses the 480 V palette at or above this voltage. Default 300."),
    ] = None,
    nema_low: Annotated[
        str | None,
        Field(description="208/240 V colors as L1,L2,L3. US shop convention: black,red,blue."),
    ] = None,
    nema_high: Annotated[
        str | None,
        Field(description="480 V colors as L1,L2,L3. US shop convention: brown,orange,yellow."),
    ] = None,
    iec: Annotated[
        str | None,
        Field(
            description="IEC L1,L2,L3 colors. Brown, black, and grey are a permitted default, not a required order."
        ),
    ] = None,
    neutral: Annotated[str | None, Field(description="NEMA neutral stroke. Default white/grey.")] = None,
    iec_neutral: Annotated[
        str | None,
        Field(
            description=(
                "IEC neutral stroke. Blue (default light blue). "
                "IEC 60445 §6.2.2 is unverified from an official source."
            )
        ),
    ] = None,
    ground: Annotated[
        str | None,
        Field(description="Ground stroke. Default green. IEC draws a yellow edge."),
    ] = None,
) -> dict[str, Any]:
    """Set 3-phase power colors. Black/red/blue and brown/orange/yellow are a US shop convention. Brown/black/grey is a permitted IEC default, not a required order. IEC neutral: blue (default light blue). IEC 60445 §6.2.2 is unverified from an official source. Control wiring is unchanged."""

    return _run(
        ctx,
        "set_phase_colors",
        enabled=enabled,
        system_voltage=system_voltage,
        nema_high_voltage=nema_high_voltage,
        nema_low=nema_low,
        nema_high=nema_high,
        iec=iec,
        neutral=neutral,
        iec_neutral=iec_neutral,
        ground=ground,
    )


@drawing_agent.tool
def save(ctx: RunContext[Drawing], path: str | None = None) -> dict[str, Any]:
    """Write the project to a .pcad.yaml path."""

    dest = path or (str(ctx.deps.path) if ctx.deps.path else None)
    if not dest:
        raise ModelRetry("Pass a path ending in .pcad.yaml.")
    return _run(ctx, "save", path=dest)


@drawing_agent.tool
def check_control_power(
    ctx: RunContext[Drawing],
    supply: str | None = None,
    margin: float | None = None,
    path: str | None = None,
    pdf: str | None = None,
) -> dict[str, Any]:
    """Check control power on each supply. Traces schematic loads. Does not invent ratings; unpublished loads stay estimates. Suggests an output breaker only when the supply's published trip test shows that rating will trip; otherwise says the tripping capability is not published and does not suggest a size. When no single breaker fits, groups the loads into the fewest branches inside that trip limit and names a sourced library breaker, or the rating range when the library has none."""

    return _run(ctx, "check_control_power", supply=supply, margin=margin, path=path, pdf=pdf)


@drawing_agent.tool
def recommend_enclosure(
    ctx: RunContext[Drawing],
    sheet: str | None = None,
    spare_pct: float | None = None,
    fill_target: float | None = None,
    nema: str | None = None,
    material: str | None = None,
    ambient_c: float | None = None,
    max_internal_c: float | None = None,
    duct_face_in: float | None = None,
    rail_to_duct_in: float | None = None,
    separate_control_rail: bool | None = None,
) -> dict[str, Any]:
    """Recommend a catalog enclosure from footprints, wire duct, spare space, and a 60-70% fill target. Steps up to the next real catalog size when the tightest box is too full. Does not write the project. Never invents dimensions."""

    return _run(
        ctx,
        "recommend_enclosure",
        sheet=sheet,
        spare_pct=spare_pct,
        fill_target=fill_target,
        nema=nema,
        material=material,
        ambient_c=ambient_c,
        max_internal_c=max_internal_c,
        duct_face_in=duct_face_in,
        rail_to_duct_in=rail_to_duct_in,
        separate_control_rail=separate_control_rail,
    )


@drawing_agent.tool
def apply_enclosure(
    ctx: RunContext[Drawing],
    sheet: str | None = None,
    place_parts: bool = False,
    spare_pct: float | None = None,
    fill_target: float | None = None,
    nema: str | None = None,
    material: str | None = None,
    ambient_c: float | None = None,
    max_internal_c: float | None = None,
    duct_face_in: float | None = None,
    rail_to_duct_in: float | None = None,
    separate_control_rail: bool | None = None,
) -> dict[str, Any]:
    """Write the recommended enclosure as one undo. Uses the fill target and duct clearance. Set place_parts to lay out the shop arrangement."""

    return _run(
        ctx,
        "apply_enclosure",
        sheet=sheet,
        place_parts=place_parts,
        spare_pct=spare_pct,
        fill_target=fill_target,
        nema=nema,
        material=material,
        ambient_c=ambient_c,
        max_internal_c=max_internal_c,
        duct_face_in=duct_face_in,
        rail_to_duct_in=rail_to_duct_in,
        separate_control_rail=separate_control_rail,
    )


@drawing_agent.tool
def compute_heat_load(
    ctx: RunContext[Drawing],
    path: str | None = None,
    ambient_c: float | None = None,
    max_internal_c: float | None = None,
    min_internal_c: float | None = None,
    altitude_m: float | None = None,
) -> dict[str, Any]:
    """Watt loss, temperature rise, and cooling for the enclosure already on the panel. Does not resize the box. Optional path writes a PDF."""

    return _run(
        ctx,
        "compute_heat_load",
        path=path,
        ambient_c=ambient_c,
        max_internal_c=max_internal_c,
        min_internal_c=min_internal_c,
        altitude_m=altitude_m,
    )


@drawing_agent.tool
def set_ambient(
    ctx: RunContext[Drawing],
    ambient_c: float | None = None,
    max_internal_c: float | None = None,
    min_internal_c: float | None = None,
    min_ambient_c: float | None = None,
    component_max_c: float | None = None,
    altitude_m: float | None = None,
    outdoor: bool | None = None,
    mount: str | None = None,
    material: str | None = None,
    nema: str | None = None,
) -> dict[str, Any]:
    """Store ambient and temperature limits for the thermal report. Does not change the enclosure size. One undo."""

    return _run(
        ctx,
        "set_ambient",
        ambient_c=ambient_c,
        max_internal_c=max_internal_c,
        min_internal_c=min_internal_c,
        min_ambient_c=min_ambient_c,
        component_max_c=component_max_c,
        altitude_m=altitude_m,
        outdoor=outdoor,
        mount=mount,
        material=material,
        nema=nema,
    )


@drawing_agent.tool
def propose_cables(
    ctx: RunContext[Drawing],
    spares: int | None = None,
    replace: bool = False,
    show_tags: bool = True,
    route: str | None = None,
) -> dict[str, Any]:
    """Group field wires from panel terminals to each field device into one cable. Picks a standard cable description and maps conductors to terminals. Does not invent manufacturer part numbers."""

    return _run(
        ctx,
        "propose_cables",
        spares=spares,
        replace=replace,
        show_tags=show_tags,
        route=route,
    )


@drawing_agent.tool
def add_cable(
    ctx: RunContext[Drawing],
    to_device: str,
    tag: str | None = None,
    from_strip: str | None = None,
    from_device: str | None = None,
    from_terminals: str | None = None,
    cable_type: str | None = None,
    conductors: int | None = None,
    gauge: str | None = None,
    shield: bool | None = None,
    drain: bool | None = None,
    shield_bond: Literal["", "both", "panel", "field", "none"] | None = None,
    length: float | None = None,
    length_unit: Literal["ft", "m"] | None = None,
    slack_pct: float | None = None,
    gland: str | None = None,
    route: str | None = None,
    part_number: str | None = None,
    part_source: str | None = None,
    show_tag: bool | None = None,
) -> dict[str, Any]:
    """Add one field cable. Leave part_number blank unless the user supplied it or it is a library part with a source URL. length is the installed run; slack_pct is optional."""

    return _run(
        ctx,
        "add_cable",
        to_device=to_device,
        tag=tag,
        from_strip=from_strip,
        from_device=from_device,
        from_terminals=from_terminals,
        cable_type=cable_type,
        conductors=conductors,
        gauge=gauge,
        shield=shield,
        drain=drain,
        shield_bond=shield_bond,
        length=length,
        length_unit=length_unit,
        slack_pct=slack_pct,
        gland=gland,
        route=route,
        part_number=part_number,
        part_source=part_source,
        show_tag=show_tag,
    )


@drawing_agent.tool
def export_cable_schedule(
    ctx: RunContext[Drawing],
    path: Annotated[str, Field(description="Schedule CSV path, or a PDF path ending in .pdf.")],
    pull: Annotated[str | None, Field(description="Optional pull-list CSV. Total length per cable type.")] = None,
    terminations: Annotated[
        str | None,
        Field(description="Optional termination CSV. Conductor to the panel terminal and the field terminal."),
    ] = None,
) -> dict[str, Any]:
    """Write the field cable schedule. Use a .pdf path for the schedule page. pull and terminations are optional CSVs."""

    return _run(ctx, "export_cable_schedule", path=path, pull=pull, terminations=terminations)


@drawing_agent.tool
def report_bug(
    ctx: RunContext[Drawing],
    title: str,
    description: str,
    include_project: bool = False,
    send: bool = False,
) -> dict[str, Any]:
    """File a bug-report zip. Put crash_id from a failed tool in the description.

    send posts only when bug reports are configured.
    """

    return _run(
        ctx,
        "report_bug",
        title=title,
        description=description,
        include_project=include_project,
        include_screenshot=False,
        send=send,
    )


@drawing_agent.tool
def size_motor_circuit(
    ctx: RunContext[Drawing],
    hp: float | None = None,
    volts: float | None = None,
    phases: int = 3,
    fla: float | None = None,
    kw: float | None = None,
    device: str = "inverse_time_breaker",
    wire_table: str = "nec_310_16",
    standard: str = "NEMA/ANSI",
    apply: bool = False,
    ref: str | None = None,
) -> dict[str, Any]:
    """Size a motor branch from published NEC tables, or record an IEC nameplate current.

    volts is the system voltage (480 uses the 460 V column). fla is nameplate current
    for the overload only. apply writes the result onto an existing motor ref.
    """

    return _run(
        ctx,
        "size_motor_circuit",
        hp=hp,
        volts=volts,
        phases=phases,
        fla=fla,
        kw=kw,
        device=device,
        wire_table=wire_table,
        standard=standard,
        apply=apply,
        ref=ref,
    )


@drawing_agent.tool
def insert_circuit(
    ctx: RunContext[Drawing],
    kind: Annotated[
        Literal["dol", "vfd", "reversing", "star_delta", "soft_starter", "plc_motor"],
        Field(description="dol, vfd, reversing, star_delta, soft_starter, or plc_motor."),
    ] = "dol",
    drive: str | None = None,
    branch: str = "breaker",
    line_contactor: bool = False,
    control: str = "none",
    kw: float | None = None,
    hp: float | None = None,
    sheet: str | None = None,
    new_sheet: bool = False,
    voltage: float | None = None,
    control_voltage: float | None = None,
    fla: float | None = None,
    pilot_lights: bool = False,
    bypass: bool = False,
    control_wiring: str = "3wire",
    starter: str = "dol",
    timer_seconds: float | None = None,
    overload_placement: str = "windings",
    hoa: bool = True,
    plc_model: str | None = None,
    io_run: str | None = None,
    io_ol: str | None = None,
    io_auto: str | None = None,
    io_out: str | None = None,
    manufacturer: str | None = None,
    secondary_manufacturer: str | None = None,
    place_layout: Annotated[
        bool,
        Field(
            description=(
                "Place in-cabinet devices on the panel layout and operators on the door. "
                "Default true. False draws the schematic only."
            )
        ),
    ] = True,
    shared_supply: Annotated[
        bool,
        Field(
            description=(
                "Reuse the panel's existing control supply. Default true. "
                "False adds a supply for this circuit."
            )
        ),
    ] = True,
    shared_estop: Annotated[
        bool,
        Field(
            description=(
                "One E-stop for the panel, wired into this circuit. Default true. "
                "False adds an E-stop for this motor."
            )
        ),
    ] = True,
    separate_control_rail: Annotated[
        bool,
        Field(
            description=(
                "Put timer, relays, the PSU, and the PLC on their own DIN rail. "
                "Default false: they sit on the right of the power rail when they fit."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Insert a motor circuit on the schematic. Power columns and control rungs use the spacing grid and spread across the sheet. density reports page fullness; split_sheet when a page is over the area limit. place_layout also fills the panel and door without moving parts already there. When manufacturer is set, assign that vendor's published catalog SKUs. secondary_manufacturer fills categories that brand does not make."""

    return _run(
        ctx,
        "insert_circuit",
        kind=kind,
        drive=drive,
        branch=branch,
        line_contactor=line_contactor,
        control=control,
        kw=kw,
        hp=hp,
        sheet=sheet,
        new_sheet=new_sheet,
        voltage=voltage,
        control_voltage=control_voltage,
        fla=fla,
        pilot_lights=pilot_lights,
        bypass=bypass,
        control_wiring=control_wiring,
        starter=starter,
        timer_seconds=timer_seconds,
        overload_placement=overload_placement,
        hoa=hoa,
        plc_model=plc_model,
        io_run=io_run,
        io_ol=io_ol,
        io_auto=io_auto,
        io_out=io_out,
        manufacturer=manufacturer,
        secondary_manufacturer=secondary_manufacturer,
        place_layout=place_layout,
        shared_supply=shared_supply,
        shared_estop=shared_estop,
        separate_control_rail=separate_control_rail,
    )


@drawing_agent.tool
def check_sizing(ctx: RunContext[Drawing]) -> dict[str, Any]:
    """Flag undersized motor wire, a branch device above NEC 430.52, and an overload range that misses the current."""

    return _run(ctx, "check_sizing")


@drawing_agent.tool
def print_package(
    ctx: RunContext[Drawing],
    path: Annotated[str, Field(description="Output PDF path for the print package.")],
    paper: Annotated[
        str,
        Field(
            description=(
                "Paper size. mixed (default) keeps each drawing at its own sheet size; "
                "letter, a4, a3, or tabloid forces one size."
            )
        ),
    ] = "mixed",
    sections: Annotated[
        str | list[str] | None,
        Field(
            description=(
                "Optional subset of cover, index, revisions, nameplate, drawings, cutouts, bom, wires, "
                "from_to, xref, plc_io, terminals, checks, thermal, power. cutouts and power are off unless named. "
                "Comma string or list. Empty report sections are skipped."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Write the one-click customer/shop PDF: cover, index, revisions, nameplate, drawings, then BOM / wires / from-to / device index / PLC I/O list / terminals / checks / thermal. cutouts and power are optional sections."""

    return _run(ctx, "print_package", path=path, paper=paper, sections=sections)


@drawing_agent.tool
def cutouts_generate(
    ctx: RunContext[Drawing],
    datum: str | None = None,
    style: str | None = None,
    view: str | None = None,
    units: str | None = None,
    persist: bool = False,
    surfaces: str | None = None,
) -> dict[str, Any]:
    """Build cutout and drill sheets for the door, backplate, and walls. persist writes datum, view, and units."""

    return _run(
        ctx,
        "cutouts_generate",
        datum=datum,
        style=style,
        view=view,
        units=units,
        persist=persist,
        surfaces=surfaces,
    )


@drawing_agent.tool
def cutouts_export_dxf(
    ctx: RunContext[Drawing],
    path: Annotated[str, Field(description="DXF file or folder. One 1:1 file per surface.")],
    units: Annotated[str | None, Field(description="mm or inch. Drawing units of the DXF.")] = None,
    view: Annotated[str | None, Field(description="front or back. Back mirrors X.")] = None,
    datum: str | None = None,
    surfaces: str | None = None,
) -> dict[str, Any]:
    """Export shop DXF with layers CUT, DRILL, DIM, TEXT, and OUTLINE. Unknown cutouts are not cut."""

    return _run(
        ctx,
        "cutouts_export_dxf",
        path=path,
        units=units,
        view=view,
        datum=datum,
        surfaces=surfaces,
    )


@drawing_agent.tool
def cutouts_export_pdf(
    ctx: RunContext[Drawing],
    path: Annotated[str, Field(description="PDF path. One 1:1 cutout sheet per surface that has openings.")],
    units: Annotated[str | None, Field(description="mm or inch.")] = None,
    view: Annotated[str | None, Field(description="front or back. Back mirrors X.")] = None,
    datum: str | None = None,
    surfaces: str | None = None,
) -> dict[str, Any]:
    """Export the cutout PDF. This is the 1:1 sheet, not the print-package page."""

    return _run(
        ctx,
        "cutouts_export_pdf",
        path=path,
        units=units,
        view=view,
        datum=datum,
        surfaces=surfaces,
    )


@drawing_agent.tool
def part_set_cutout(
    ctx: RunContext[Drawing],
    ref: str,
    cutout: dict[str, Any] | None = None,
    unknown: bool | None = None,
    clear: bool = False,
    surface: str | None = None,
    include_key: bool | None = None,
) -> dict[str, Any]:
    """Set a device cutout from a published drawing, or mark it unknown.

    ``unknown=false`` restores the opening, key, and centre minimums from before
    it was marked unknown. ``clear`` drops the override so the library record applies.
    """

    return _run(
        ctx,
        "part_set_cutout",
        ref=ref,
        cutout=cutout,
        unknown=unknown,
        clear=clear,
        surface=surface,
        include_key=include_key,
    )


@drawing_agent.tool
def wire_list(
    ctx: RunContext[Drawing],
    path: Annotated[
        str | None,
        Field(description="Optional CSV path. Writes the from-to report (one row per conductor)."),
    ] = None,
    sort: Annotated[
        str,
        Field(
            description=(
                "number (default) sorts by wire number, numerically then IEC d-d. "
                "device sorts by the from tag and pin, then the to tag, then the number."
            )
        ),
    ] = "number",
) -> dict[str, Any]:
    """From-to wire list. One row per conductor taken from the netlist. A contact's two sides are separate rows with separate wire numbers. Columns are wire_number, from (tag:pin or strip:terminal), to, sheet/line/column at each end, size, color, type, and crossing. An unknown gauge is TBD."""

    return _run(ctx, "wire_list", path=path, sort=sort, from_to=True)


@drawing_agent.tool
def export_wire_markers(
    ctx: RunContext[Drawing],
    path: Annotated[str, Field(description="Output path. Required. One marker per wire end.")],
    target: Annotated[
        str,
        Field(
            description=(
                "generic (default) writes the PanelCAD CSV with header "
                "text,end,device,pin,sheet,location,size,color,type,style. "
                "phoenix writes the documented PROJECT complete / CLIP PROJECT / MARKING system "
                "file: one column, no header, one wire number per row. "
                "weidmueller and brady write that same generic CSV. M-Print PRO and Brady "
                "Workstation do not publish a fixed wire-marker header. "
                "result.confidence is documented or unverified. result.doc_urls cites the pages."
            )
        ),
    ] = "generic",
    style: Annotated[
        str,
        Field(
            description=(
                "heat-shrink (default) or clip-on. Stored on the generic CSV. "
                "Phoenix heat-shrink versus clip-on is the material chosen in the marking software, not a file column."
            )
        ),
    ] = "heat-shrink",
    sort: Annotated[
        str,
        Field(description="number (default) or device. Same order as the from-to report."),
    ] = "number",
) -> dict[str, Any]:
    """Print-ready wire markers. One row per wire end, so each wire number is printed twice, or once per distinct end. A junction or off-page arrow is not an end."""

    return _run(ctx, "export_wire_markers", path=path, target=target, style=style, sort=sort)


@drawing_agent.tool
def plc_io_list(
    ctx: RunContext[Drawing],
    path: Annotated[
        str | None,
        Field(description="Optional CSV path for the PLC I/O list. Omit to return the rows only."),
    ] = None,
) -> dict[str, Any]:
    """List every PLC and I/O-module point: address, DI/DO/AI/AO, tag, description from the connected device (tag, function, wire, sheet and line), the device, and spares."""

    return _run(ctx, "plc_io_list", path=path)


@drawing_agent.tool
def io_add_module(
    ctx: RunContext[Drawing],
    module: Annotated[str, Field(description="Verified module id, such as brx.bx_08nd3 or 5069-IB16.")],
    ref: Annotated[str | None, Field(description="Sheet tag for the module.")] = None,
    slot: Annotated[int, Field(description="Logix or Micro800 slot. Default 1.")] = 1,
    position: Annotated[int, Field(description="CLICK module position. 1 is the first expansion module.")] = 1,
    polarity: Annotated[str, Field(description="sink or source, when the module allows both.")] = "",
    address_base: Annotated[int | None, Field(description="TIA process-image byte, or Do-more element base.")] = None,
) -> dict[str, Any]:
    """Place one verified PLC I/O module. Unverified modules are refused. Does not draw field devices."""

    return _run(
        ctx,
        "io_add_module",
        module=module,
        ref=ref,
        slot=slot,
        position=position,
        polarity=polarity,
        address_base=address_base,
    )


@drawing_agent.tool
def io_assign_point(
    ctx: RunContext[Drawing],
    module: Annotated[str, Field(description="Module tag on the I/O sheet.")],
    pin: Annotated[str, Field(description="Pin id, the I/O list address column.")],
    tag: Annotated[str | None, Field(description="Field-device tag.")] = None,
    description: Annotated[str | None, Field(description="Point description.")] = None,
    field: Annotated[
        str | None,
        Field(
            description=(
                "pushbutton, limit_switch, prox_3wire, relay_coil, solenoid, pilot_light, "
                "transmitter_2wire, transmitter_4wire, or spare."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Set the tag, description, and field device for one module point. Updates the I/O list."""

    return _run(ctx, "io_assign_point", module=module, pin=pin, tag=tag, description=description, field=field)


@drawing_agent.tool
def io_generate_sheets(
    ctx: RunContext[Drawing],
    modules: Annotated[
        list[str] | None,
        Field(description="Module tags to draw. Omit to draw every module already added."),
    ] = None,
    fuses: Annotated[bool | None, Field(description="Fuse the hot feed.")] = None,
) -> dict[str, Any]:
    """Draw field devices, terminals, commons, and wire numbers. Regenerating keeps user field devices and reports conflicts."""

    return _run(ctx, "io_generate_sheets", modules=modules, fuses=fuses)


@drawing_agent.tool
def export_plc_tags(
    ctx: RunContext[Drawing],
    path: Annotated[str, Field(description="Output file path for the PLC tag export.")],
    target: Annotated[
        Literal["studio5000", "tia", "tia_csv", "domore", "keyence", "sysmac", "cxprogrammer"],
        Field(
            description=(
                "studio5000 writes a Logix Designer CSV tag import. "
                "tia writes the TIA Portal Excel tag table (PLCTags and TagTableProperties). "
                "tia_csv writes a labeled generic CSV. "
                "domore writes a Do-more element documentation file. "
                "keyence and sysmac write labeled generic CSVs. "
                "cxprogrammer writes the CX-Programmer symbol-table paste file."
            )
        ),
    ],
    prefix: Annotated[
        str,
        Field(description="Optional tag-name prefix. A missing underscore is inserted."),
    ] = "",
    name_from: Annotated[
        Literal["device", "description"],
        Field(
            description=(
                "device uses the I/O tag (parent device plus the contact function). "
                "description uses the I/O description text."
            )
        ),
    ] = "device",
    logix_style: Annotated[
        Literal["alias", "base"],
        Field(
            description=(
                "studio5000 only. alias points the tag at Local:x:I.Pt00.Data. "
                "base writes a BOOL or REAL tag and leaves the specifier empty."
            )
        ),
    ] = "alias",
    logix_slot: Annotated[
        int | None,
        Field(description="studio5000 slot of the first module. Default 1. Later modules increment."),
    ] = None,
    analog_start: Annotated[
        int | None,
        Field(
            description=(
                "TIA %IW/%QW byte address used when the module has no start_address. "
                "Omit to use the project analog_start property, then the next free even "
                "word after the highest digital byte."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Export PLC tags for Studio 5000, TIA, Do-more, Keyence, or Omron from the drawing's I/O list."""

    return _run(
        ctx,
        "export_plc_tags",
        path=path,
        target=target,
        prefix=prefix,
        name_from=name_from,
        logix_style=logix_style,
        logix_slot=logix_slot,
        analog_start=analog_start,
    )


@drawing_agent.tool
def issue_revision(
    ctx: RunContext[Drawing],
    description: str = "",
    rev: str | None = None,
    scheme: Literal["letter", "number"] | None = None,
    date: str | None = None,
    drawn: str | None = None,
    checked: str | None = None,
    approved: str | None = None,
    clear_clouds: bool | None = None,
    clouds: bool = True,
) -> dict[str, Any]:
    """Snapshot the drawing, bump the rev, and fill the title-block revision table.

    scheme is letter (A, B, C) or number (0, 1, 2). drawn, checked, and approved
    are initials. clouds draws revision clouds around changes. clear_clouds
    removes clouds from earlier revisions when this one is issued.
    """

    return _run(
        ctx,
        "issue_revision",
        description=description,
        rev=rev,
        scheme=scheme,
        date=date,
        drawn=drawn,
        checked=checked,
        approved=approved,
        clear_clouds=clear_clouds,
        clouds=clouds,
    )


@drawing_agent.tool
def compute_sccr(ctx: RunContext[Drawing]) -> dict[str, Any]:
    """UL 508A Supplement SB short-circuit current rating of the open panel. Does not edit the drawing."""

    return _run(ctx, "compute_sccr")


@drawing_agent.tool
def set_available_fault(
    ctx: RunContext[Drawing],
    ka: float,
    voltage: float | None = None,
    sccr_voltage: float | None = None,
) -> dict[str, Any]:
    """Set the available fault current at the panel, in kA, and optionally the voltages."""

    return _run(ctx, "set_available_fault", ka=ka, voltage=voltage, sccr_voltage=sccr_voltage)


@drawing_agent.tool
def set_part_sccr(
    ctx: RunContext[Drawing],
    ref: str,
    sccr_ka: float | None = None,
    ir_ka: float | None = None,
    fuse_class: str | None = None,
    fuse_amps: float | None = None,
    let_through_ka: float | None = None,
    let_through_at_ka: float | None = None,
) -> dict[str, Any]:
    """Set one device's short-circuit rating or its fuse / breaker let-through data."""

    return _run(
        ctx,
        "set_part_sccr",
        ref=ref,
        sccr_ka=sccr_ka,
        ir_ka=ir_ka,
        fuse_class=fuse_class,
        fuse_amps=fuse_amps,
        let_through_ka=let_through_ka,
        let_through_at_ka=let_through_at_ka,
    )


@drawing_agent.tool
def list_revisions(ctx: RunContext[Drawing]) -> dict[str, Any]:
    """List issued revisions and revision clouds. current is the rev on every sheet."""

    return _run(ctx, "list_revisions")


@drawing_agent.tool
def compare_revisions(
    ctx: RunContext[Drawing],
    left: str,
    right: str,
    path: str | None = None,
    pdf: str | None = None,
) -> dict[str, Any]:
    """Diff two revisions, or 'working' for the open drawing.

    Reports devices, wires, terminals, BOM quantity and part changes, and layout
    moves. path writes a CSV. pdf writes the compare report.
    """

    return _run(ctx, "compare_revisions", left=left, right=right, path=path, pdf=pdf)


@drawing_agent.tool
def add_revision_cloud(
    ctx: RunContext[Drawing],
    x: float,
    y: float,
    w: float,
    h: float,
    sheet: str | None = None,
    rev: str | None = None,
) -> dict[str, Any]:
    """Draw a manual revision cloud. x, y, w, h are sheet millimetres."""

    return _run(ctx, "add_revision_cloud", x=x, y=y, w=w, h=h, sheet=sheet, rev=rev)


@drawing_agent.tool
def import_start(
    ctx: RunContext[Drawing],
    path: str,
    photos: str | None = None,
    standard: str | None = None,
    workspace: str | None = None,
    resume: str | None = None,
    vision_backend: str | None = None,
    vision_model: str | None = None,
    vision_base_url: str | None = None,
    vision_api_key_env: str | None = None,
    vision_workers: int | None = None,
    vision_timeout_s: float | None = None,
    vision_page_timeout_s: float | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    think: bool | None = None,
    reasoning_effort: str | None = None,
    clean_grok_sessions: bool | None = None,
    grok_tools_none: bool | None = None,
) -> dict[str, Any]:
    """Start a print import.

    Vector PDFs are read from the file. For a scan, vision_backend is none
    (pixel only), grok, grok-build, ollama, or custom. vision_model is the
    model id. vision_workers is how many tiles run at once (default 3, max 8).
    vision_timeout_s is one request. vision_page_timeout_s is one page of
    tiles, not the whole job. Ollama defaults are 300 s, 3600 s, num_ctx
    16384, and think off. reasoning_effort is for a /v1 server.
    The API key is the environment variable named by vision_api_key_env.
    Local Grok Build does not take a key. clean_grok_sessions deletes only
    that tile's folder under ~/.grok/sessions/<encoded cwd>/<id> (default on).
    grok_tools_none passes --tools none and defaults off. The result summary
    includes tiles_done, tiles_total, spent_usd, input_tokens, estimated_usd,
    fallbacks, fallback_reasons, and ocr_skipped.
    """

    return _run(
        ctx,
        "import_start",
        path=path,
        photos=photos,
        standard=standard,
        workspace=workspace,
        resume=resume,
        backend="external" if not vision_backend else vision_backend,
        vision_backend=vision_backend,
        vision_model=vision_model,
        vision_base_url=vision_base_url,
        vision_api_key_env=vision_api_key_env,
        vision_workers=vision_workers,
        vision_timeout_s=vision_timeout_s,
        vision_page_timeout_s=vision_page_timeout_s,
        num_ctx=num_ctx,
        num_predict=num_predict,
        think=think,
        reasoning_effort=reasoning_effort,
        clean_grok_sessions=clean_grok_sessions,
        grok_tools_none=grok_tools_none,
    )


@drawing_agent.tool
def import_next(ctx: RunContext[Drawing]) -> dict[str, Any]:
    """Return the current print-import crop without submitting."""

    return _run(ctx, "import_next")


@drawing_agent.tool
def import_submit(
    ctx: RunContext[Drawing],
    data: dict[str, Any],
    work_id: str | None = None,
) -> dict[str, Any]:
    """Submit the JSON read from the current crop. Unreadable text stays empty."""

    return _run(ctx, "import_submit", data=data, work_id=work_id)


@drawing_agent.tool
def import_status(ctx: RunContext[Drawing]) -> dict[str, Any]:
    """Compact print-import status. Counts and page kinds only."""

    return _run(ctx, "import_status")


@drawing_agent.tool
def import_build(ctx: RunContext[Drawing], path: str | None = None) -> dict[str, Any]:
    """Build the imported sheets through the same Session calls as the rest of this agent."""

    return _run(ctx, "import_build", path=path)


@drawing_agent.tool
def import_add_photos(ctx: RunContext[Drawing], path: str) -> dict[str, Any]:
    """Add photos of the real panel. They propose layout and do not change the schematic."""

    return _run(ctx, "import_add_photos", path=path)


@drawing_agent.tool
def convert_standard_plan(
    ctx: RunContext[Drawing],
    to: str,
    sheets: str | None = None,
    out: str | None = None,
    workspace: str | None = None,
    keep_numbers: bool = False,
) -> dict[str, Any]:
    """Plan an IEC <-> NEMA conversion.

    Saves a workspace JSON and does not change the drawing. sheets is a comma
    list; omit it for the whole project. keep_numbers leaves PB2, PB3, PB4;
    otherwise each letter family is numbered 1, 2, 3.
    """

    return _run(
        ctx,
        "convert_standard_plan",
        to=to,
        sheets=sheets,
        out=out,
        workspace=workspace,
        keep_numbers=keep_numbers,
    )


@drawing_agent.tool
def convert_standard_next(
    ctx: RunContext[Drawing],
    workspace: str | None = None,
) -> dict[str, Any]:
    """Convert the next unfinished chunk. Safe to call again after a stop. One undo. Does not overwrite the source file."""

    return _run(ctx, "convert_standard_next", workspace=workspace)


@drawing_agent.tool
def convert_standard_apply(
    ctx: RunContext[Drawing],
    workspace: str | None = None,
    out: str | None = None,
) -> dict[str, Any]:
    """Finish every pending conversion chunk and save-as. One undo."""

    return _run(ctx, "convert_standard_apply", workspace=workspace, out=out)


@drawing_agent.tool
def convert_standard(
    ctx: RunContext[Drawing],
    to: str,
    sheets: str | None = None,
    out: str | None = None,
    workspace: str | None = None,
    keep_numbers: bool = False,
) -> dict[str, Any]:
    """Convert the whole project, or the comma-separated sheets, between IEC and NEMA.

    One undo. Save-as; the source file stays put. keep_numbers leaves the
    existing device numbers.
    """

    return _run(
        ctx,
        "convert_standard",
        to=to,
        sheets=sheets,
        out=out,
        workspace=workspace,
        keep_numbers=keep_numbers,
    )


@drawing_agent.tool
def ui_set_theme(
    ctx: RunContext[Drawing],
    theme: Literal["light", "dark", "system"],
) -> dict[str, Any]:
    """Set the window theme. system follows the operating system color scheme."""

    return _run(ctx, "ui_set_theme", theme=theme)


@drawing_agent.tool
def ui_set_canvas(
    ctx: RunContext[Drawing],
    canvas: Literal["paper", "white", "dark", "auto"],
) -> dict[str, Any]:
    """Set the on-screen sheet. Print, PDF, DXF, and SVG stay on white paper."""

    return _run(ctx, "ui_set_canvas", canvas=canvas)


@drawing_agent.tool
def ui_set_phase_colors(
    ctx: RunContext[Drawing],
    enabled: bool = True,
) -> dict[str, Any]:
    """Phase colours on the sheet. Print, PDF, DXF, and SVG stay unchanged."""

    return _run(ctx, "ui_set_phase_colors", enabled=enabled)


@drawing_agent.tool
def reconstruct_add_photos(
    ctx: RunContext[Drawing],
    paths: list[str],
    role: str = "other",
    note: str = "",
) -> dict[str, Any]:
    """Add phone photos of a physical panel (backpanel, door, labels). Not drawings."""

    return _run(ctx, "reconstruct_add_photos", paths=paths, role=role, note=note)


@drawing_agent.tool
def reconstruct_next_question(ctx: RunContext[Drawing]) -> dict[str, Any]:
    """Ask the tech on the floor the next reconstruction question."""

    return _run(ctx, "reconstruct_next_question")


@drawing_agent.tool
def reconstruct_answer(
    ctx: RunContext[Drawing],
    step_id: str,
    text: str = "",
    photo: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Record a typed landing (K1:A1) or a new photo. confirm=true accepts an inferred wire."""

    return _run(ctx, "reconstruct_answer", step_id=step_id, text=text, photo=photo, confirm=confirm)


@drawing_agent.tool
def reconstruct_photo_region(
    ctx: RunContext[Drawing],
    item_id: str | None = None,
    ref: str | None = None,
) -> dict[str, Any]:
    """Show the source photo crop for an observed item or a placed layout part."""

    return _run(ctx, "reconstruct_photo_region", item_id=item_id, ref=ref)


@drawing_agent.tool
def reconstruct_unconfirmed(ctx: RunContext[Drawing]) -> dict[str, Any]:
    """List items that must not go on the layout until a catalog match or confirmed envelope."""

    return _run(ctx, "reconstruct_unconfirmed")


@drawing_agent.tool
def reconstruct_identify(ctx: RunContext[Drawing], photo_id: str | None = None) -> dict[str, Any]:
    """Read nameplates and wire numbers from panel photos. Does not invent part numbers."""

    return _run(ctx, "reconstruct_identify", photo_id=photo_id)


@drawing_agent.tool
def reconstruct_confirm_item(
    ctx: RunContext[Drawing],
    item_id: str,
    catalog: str = "",
    manufacturer: str = "",
    envelope_w_mm: float | None = None,
    envelope_h_mm: float | None = None,
    envelope_d_mm: float | None = None,
    mount: str = "",
    library_id: str = "",
    tag: str = "",
) -> dict[str, Any]:
    """Confirm a catalog SKU or a measured envelope (W, H, D, mount). Converts a needs-info placeholder in place."""

    return _run(
        ctx,
        "reconstruct_confirm_item",
        item_id=item_id,
        catalog=catalog,
        manufacturer=manufacturer,
        envelope_w_mm=envelope_w_mm,
        envelope_h_mm=envelope_h_mm,
        envelope_d_mm=envelope_d_mm,
        mount=mount,
        library_id=library_id,
        tag=tag,
    )


@drawing_agent.tool
def reconstruct_place_needs_info(ctx: RunContext[Drawing], photo_id: str | None = None) -> dict[str, Any]:
    """Place reconstruct-only INFO NEEDED markers. Not generic parts."""

    return _run(ctx, "reconstruct_place_needs_info", photo_id=photo_id)


@drawing_agent.tool
def reconstruct_set_corners(ctx: RunContext[Drawing], photo_id: str, corners: list[Any]) -> dict[str, Any]:
    """Mark the panel-face quad on a photo (NW, NE, SE, SW)."""

    return _run(ctx, "reconstruct_set_corners", photo_id=photo_id, corners=corners)


@drawing_agent.tool
def reconstruct_set_scale(
    ctx: RunContext[Drawing],
    photo_id: str,
    kind: str,
    p1: Any,
    p2: Any,
    known_mm: float | None = None,
    known_height_mm: float | None = None,
    catalog: str = "",
    slot: int | str | None = None,
    replace: str | None = None,
) -> dict[str, Any]:
    """Set metric scale from two known lengths or the enclosure face width and height.

    ``known_height_mm`` applies only to kind=face. A length mark does not drop a
    face scale. Pass ``slot`` / ``replace`` to overwrite scale or scale_b.
    """

    return _run(
        ctx,
        "reconstruct_set_scale",
        photo_id=photo_id,
        kind=kind,
        p1=p1,
        p2=p2,
        known_mm=known_mm,
        known_height_mm=known_height_mm,
        catalog=catalog,
        slot=slot,
        replace=replace,
    )


@drawing_agent.tool
def reconstruct_clear_scale(ctx: RunContext[Drawing], photo_id: str) -> dict[str, Any]:
    """Clear face and length scale marks on a photo."""

    return _run(ctx, "reconstruct_clear_scale", photo_id=photo_id)


@drawing_agent.tool
def reconstruct_place_confirmed(ctx: RunContext[Drawing], sheet: str | None = None) -> dict[str, Any]:
    """Place only layout-ready parts at measured millimetres."""

    return _run(ctx, "reconstruct_place_confirmed", sheet=sheet)


@drawing_agent.tool
def reconstruct_compare_print(ctx: RunContext[Drawing], path: str | None = None) -> dict[str, Any]:
    """Cross-check panel photos against a secondary old print. Photos stay authoritative."""

    return _run(ctx, "reconstruct_compare_print", path=path)


@drawing_agent.tool
def reconstruct_tag_photo(ctx: RunContext[Drawing], photo_id: str, role: str, note: str | None = None) -> dict[str, Any]:
    """Tag a stored panel photo (backpanel, door, nameplate, terminals, wires)."""

    return _run(ctx, "reconstruct_tag_photo", photo_id=photo_id, role=role, note=note)


@drawing_agent.tool
def reconstruct_list_photos(ctx: RunContext[Drawing]) -> dict[str, Any]:
    """List photos stored with the project."""

    return _run(ctx, "reconstruct_list_photos")


@drawing_agent.tool
def reconstruct_checklist(ctx: RunContext[Drawing]) -> dict[str, Any]:
    """Floor checklist (trace wires, confirm parts, compare an old print)."""

    return _run(ctx, "reconstruct_checklist")


@drawing_agent.tool
def reconstruct_confirm_connection(
    ctx: RunContext[Drawing],
    step_id: str | None = None,
    wire_id: str | None = None,
) -> dict[str, Any]:
    """Mark an inferred wire as confirmed."""

    return _run(ctx, "reconstruct_confirm_connection", step_id=step_id, wire_id=wire_id)


@drawing_agent.tool
def reconstruct_attach_print(ctx: RunContext[Drawing], path: str, note: str = "") -> dict[str, Any]:
    """Attach a secondary old print. Photos stay primary."""

    return _run(ctx, "reconstruct_attach_print", path=path, note=note)


@drawing_agent.tool
def reconstruct_status(ctx: RunContext[Drawing]) -> dict[str, Any]:
    """Photos, items, checklist, prints, mismatches. status.scaled is only photos with a real mm/px."""

    return _run(ctx, "reconstruct_status")


@drawing_agent.tool
def nameplate_build(ctx: RunContext[Drawing]) -> dict[str, Any]:
    """Nameplate fields, full-load working, and checks for the open panel. Does not edit the drawing."""

    return _run(ctx, "nameplate_build")


@drawing_agent.tool
def nameplate_set(
    ctx: RunContext[Drawing],
    manufacturer: str | None = None,
    trademark: str | None = None,
    catalog_model: str | None = None,
    job_number: str | None = None,
    voltage: str | None = None,
    phases: str | None = None,
    frequency: str | None = None,
    fla: str | None = None,
    sccr: str | None = None,
    enclosure_type: str | None = None,
    drawing_number: str | None = None,
    date_code: str | None = None,
    service_equipment: str | None = None,
    control_circuit_only: str | None = None,
    disconnect_locations: str | None = None,
    caution_text: str | None = None,
    caution_confirmed: str | None = None,
    largest_load: str | None = None,
    max_ocpd: str | None = None,
    preset: str | None = None,
    width_in: float | None = None,
    height_in: float | None = None,
) -> dict[str, Any]:
    """Override nameplate fields. service_equipment and control_circuit_only are yes or no."""

    fields = {
        key: value
        for key, value in {
            "manufacturer": manufacturer,
            "trademark": trademark,
            "catalog_model": catalog_model,
            "job_number": job_number,
            "voltage": voltage,
            "phases": phases,
            "frequency": frequency,
            "fla": fla,
            "sccr": sccr,
            "enclosure_type": enclosure_type,
            "drawing_number": drawing_number,
            "date_code": date_code,
            "service_equipment": service_equipment,
            "control_circuit_only": control_circuit_only,
            "disconnect_locations": disconnect_locations,
            "caution_text": caution_text,
            "caution_confirmed": caution_confirmed,
            "largest_load": largest_load,
            "max_ocpd": max_ocpd,
            "preset": preset,
            "width_in": width_in,
            "height_in": height_in,
        }.items()
        if value is not None
    }
    return _run(ctx, "nameplate_set", **fields)


@drawing_agent.tool
def nameplate_export(
    ctx: RunContext[Drawing],
    pdf: str | None = None,
    dxf: str | None = None,
    png: str | None = None,
) -> dict[str, Any]:
    """Write the real-size nameplate PDF, the engraving DXF, or a preview PNG."""

    return _run(ctx, "nameplate_export", pdf=pdf, dxf=dxf, png=png)
