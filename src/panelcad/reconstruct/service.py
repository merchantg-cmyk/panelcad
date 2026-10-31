"""Session-facing reconstruct API. Photos first; prints only cross-check."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from panelcad.reconstruct.checklist import next_open, parse_landing, public_step, rebuild_checklist
from panelcad.reconstruct.geometry import (
    build_scale,
    mm_per_px,
    photo_to_mm,
    scale_payload,
    validate_corners,
)
from panelcad.reconstruct.layout import (
    fit_sheet_to_photo,
    measured_xy,
    placement_properties,
    refuse_generic,
    target_sheet_kind,
)
from panelcad.reconstruct.match import catalog_dimension_mm, match_catalog
from panelcad.reconstruct.models import (
    ENVELOPE_MAX_MM,
    ENVELOPE_MIN_MM,
    ENVELOPE_MOUNTS,
    PHONE_EXPORT_EXTS,
    PHOTO_ROLES,
    ObservedItem,
    Photo,
    PhotoCorner,
    PhotoRegion,
    PrintSource,
    Reconstruction,
    catalog_match_has_footprint,
    ensure_reconstruction,
    item_is_layout_ready,
    public_item,
    public_photo,
)
from panelcad.reconstruct.prints import classify_print_path, compare_to_photos, load_print_index
from panelcad.reconstruct.store import (
    PHOTO_DIR,
    PRINT_DIR,
    copy_into,
    image_size,
    is_photo_file,
    next_id,
    relocate,
    resolve_path,
)
from panelcad.reconstruct.vision import (
    NO_AI_MESSAGE,
    PANEL_PHOTO_KIND,
    VisionBackend,
    get_backend,
    normalize_photo_extract,
    vision_is_offline,
)


def status(session: Any) -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    return {
        "photos": [public_photo(item) for item in store.photos],
        "items": [public_item(item) for item in store.items],
        "checklist": [public_step(item) for item in store.checklist],
        "prints": [item.model_dump(mode="json") for item in store.prints],
        "mismatches": [item.model_dump(mode="json") for item in store.mismatches],
        "unconfirmed": [public_item(item) for item in store.items if not item_is_layout_ready(item) and item.role == "part"],
        "scaled": [photo.id for photo in store.photos if mm_per_px(photo) is not None],
        "panel_sheet": store.panel_sheet,
        "door_sheet": store.door_sheet,
        "schematic_sheet": store.schematic_sheet,
    }


def add_photos(
    session: Any,
    paths: list[Path | str],
    role: str = "other",
    note: str = "",
) -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    token = _role(role)
    added: list[dict[str, Any]] = []
    dest_root = session.path.parent / PHOTO_DIR if session.path else None
    for raw in paths:
        src = Path(raw)
        if not src.is_file():
            raise FileNotFoundError(f"Can't find {src.name}. Was it moved or deleted?")
        suffix = src.suffix.lower()
        if suffix in PHONE_EXPORT_EXTS:
            raise ValueError(
                "iPhone HEIC photos can't be read yet. "
                "Set the camera to 'Most Compatible' (JPG) or export as JPG."
            )
        if suffix == ".pdf":
            raise ValueError("That's a PDF. Add it under 'Old drawing (optional)'.")
        if not is_photo_file(src):
            raise ValueError(
                f"not a panel photo: {src.name} "
                "(attach old drawings or scans with reconstruct_attach_print)"
            )
        photo_id = next_id("p", [item.id for item in store.photos])
        if dest_root is not None:
            dest = copy_into(src, dest_root, photo_id)
            stored = dest.relative_to(session.path.parent).as_posix()
        else:
            stored = str(src.resolve())
        width, height = image_size(src)
        photo = Photo(
            id=photo_id,
            path=stored,
            role=token,  # type: ignore[arg-type]
            note=note,
            width_px=width,
            height_px=height,
            original_name=src.name,
            resolved_path=str(src.resolve()),
        )
        store.photos.append(photo)
        added.append(public_photo(photo))
    rebuild_checklist(store)
    return {"added": added, "count": len(store.photos)}


def tag_photo(session: Any, photo_id: str, role: str, note: str | None = None) -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    photo = store.photo(photo_id)
    photo.role = _role(role)  # type: ignore[assignment]
    if note is not None:
        photo.note = note
    return public_photo(photo)


def set_corners(session: Any, photo_id: str, corners: list[Any]) -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    photo = store.photo(photo_id)
    points = validate_corners(_corners(corners), photo.width_px, photo.height_px)
    photo.corners = [PhotoCorner(x=x, y=y) for x, y in points]
    photo.scale = None
    photo.scale_b = None
    photo.face_width_mm = None
    photo.face_height_mm = None
    photo.face_is_backpanel = False
    photo.scale_error_mm = None
    return public_photo(photo)


def _same_kind_direction(existing: Any, incoming: Any) -> bool:
    if str(getattr(existing, "kind", "") or "") != str(getattr(incoming, "kind", "") or ""):
        return False
    ev = (
        float(existing.p2_x) - float(existing.p1_x),
        float(existing.p2_y) - float(existing.p1_y),
    )
    nv = (
        float(incoming.p2_x) - float(incoming.p1_x),
        float(incoming.p2_y) - float(incoming.p1_y),
    )
    e_horiz = abs(ev[0]) >= abs(ev[1])
    n_horiz = abs(nv[0]) >= abs(nv[1])
    return e_horiz == n_horiz


def set_scale(
    session: Any,
    photo_id: str,
    kind: str,
    p1: tuple[float, float] | list[float],
    p2: tuple[float, float] | list[float],
    *,
    known_mm: float | None = None,
    known_height_mm: float | None = None,
    catalog: str = "",
    manufacturer: str = "",
    dimension: str = "width",
    note: str = "",
    face_width_mm: float | None = None,
    face_height_mm: float | None = None,
    backpanel: bool = False,
    slot: int | str | None = None,
    replace: str | None = None,
) -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    photo = store.photo(photo_id)
    catalog_mm = None
    token = str(kind).lower()
    if token in {"catalog_part", "catalog", "part", "sku"} and catalog:
        hits = match_catalog(
            session.parts,
            part_number=catalog,
            brand=manufacturer,
            limit=1,
            devices=getattr(session, "catalog", None),
        )
        if not hits:
            raise ValueError(f"no catalog part '{catalog}' to scale from")
        catalog_mm = catalog_dimension_mm(hits[0], dimension)
        if catalog_mm is None:
            raise ValueError(f"{hits[0].manufacturer} {hits[0].catalog} has no published {dimension}")
        manufacturer = manufacturer or hits[0].manufacturer
    if token in {"face", "rectangle", "enclosure", "backpanel", "backplate"}:
        width = float(known_mm or face_width_mm or 0)
        height = float(known_height_mm or face_height_mm or 0)
        if width <= 0 or height <= 0:
            raise ValueError("enclosure / face scale needs both width_mm and height_mm")
        photo.face_width_mm = width
        photo.face_height_mm = height
        # A part's face sets the scale only. The enclosure changes when the
        # tech says these corners are the backpanel.
        photo.face_is_backpanel = token in {"enclosure", "backpanel", "backplate"} or bool(backpanel)
        rebuild_checklist(store)
        return scale_payload(photo)
    ignored_height = ""
    if token not in {"face", "rectangle", "enclosure"} and (
        known_height_mm not in (None, 0, "") or face_height_mm not in (None, 0, "")
    ):
        ignored_height = (
            "known_height_mm is ignored unless kind is the enclosure face — "
            "two-length scale uses two independent length marks, not a fake face"
        )
        known_height_mm = None
        face_height_mm = None
    ref = build_scale(
        kind,
        (float(p1[0]), float(p1[1])),
        (float(p2[0]), float(p2[1])),
        known_mm=known_mm,
        catalog_mm=catalog_mm,
        note=note,
        catalog=catalog,
        manufacturer=manufacturer,
        dimension=dimension,
    )
    face_set = bool(float(photo.face_width_mm or 0) > 0 and float(photo.face_height_mm or 0) > 0)
    if face_set:
        rebuild_checklist(store)
        payload = scale_payload(photo)
        payload["message"] = (
            "face scale is set; length mark not applied. "
            "Call reconstruct_clear_scale first if you want to replace the face."
        )
        payload["face"] = True
        if ignored_height:
            payload["message"] = f"{payload['message']} {ignored_height}"
        return payload
    target = str(replace or slot or "").strip().lower()
    if target in {"1", "scale"}:
        photo.scale = ref
    elif target in {"2", "scale_b"}:
        photo.scale_b = ref
    elif photo.scale is not None and _same_kind_direction(photo.scale, ref):
        photo.scale = ref
    elif photo.scale_b is not None and _same_kind_direction(photo.scale_b, ref):
        photo.scale_b = ref
    elif (
        photo.scale is not None
        and abs(float(photo.scale.p1_x) - ref.p1_x) < 1e-6
        and abs(float(photo.scale.p1_y) - ref.p1_y) < 1e-6
        and abs(float(photo.scale.p2_x) - ref.p2_x) < 1e-6
        and abs(float(photo.scale.p2_y) - ref.p2_y) < 1e-6
    ):
        photo.scale = ref
    elif photo.scale is None:
        photo.scale = ref
    else:
        photo.scale_b = ref
    rebuild_checklist(store)
    payload = scale_payload(photo)
    if ignored_height:
        payload["message"] = ignored_height
        payload["face"] = False
    return payload


def clear_scale(session: Any, photo_id: str) -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    photo = store.photo(photo_id)
    photo.scale = None
    photo.scale_b = None
    photo.face_width_mm = None
    photo.face_height_mm = None
    photo.face_is_backpanel = False
    photo.scale_error_mm = None
    rebuild_checklist(store)
    payload = scale_payload(photo)
    payload["cleared"] = True
    payload["message"] = "scale cleared"
    return payload


def identify(
    session: Any,
    photo_id: str | None = None,
    *,
    vision: VisionBackend | None = None,
    recordings: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    backend = vision or get_backend(recordings=recordings)
    photos = [store.photo(photo_id)] if photo_id else list(store.photos)
    if not photos:
        raise ValueError("add panel photos before identify")
    note = ""
    if getattr(backend, "name", "") == "stub" and str(getattr(backend, "fallback_reason", "") or ""):
        note = str(backend.fallback_reason)
    offline = vision_is_offline(backend, note)
    created: list[ObservedItem] = []
    for photo in photos:
        path = resolve_path(photo.path, session.path)
        work = {
            "id": photo.id,
            "kind": PANEL_PHOTO_KIND,
            "image": str(path),
            "role": photo.role,
        }
        extract = normalize_photo_extract(backend.extract(work))
        found = _items_from_extract(session, store, photo, extract)
        created.extend(found)
        from panelcad.reconstruct.terminal_photo import absorb_terminal_strips

        created.extend(absorb_terminal_strips(session, store, photo, extract))
        rejected = any(
            isinstance(row, dict)
            and (
                _bad_region_number(row)
                or ("confidence" in row and _safe_float(row.get("confidence"), None) is None)
            )
            for row in (extract.get("devices") or [])
        )
        photo.unreadable = (bool(extract.get("unreadable")) or rejected) and not found
        if offline:
            # No model is connected. Do not tell the tech to reshoot a sharp photo.
            photo.unreadable = False
            photo.vision_note = ""
        elif photo.unreadable:
            photo.vision_note = "photo could not be read — reshoot the nameplate square-on, out of glare"
        else:
            place_needs_info(session, photo.id)
    if offline:
        note = NO_AI_MESSAGE
    rebuild_checklist(store)
    return {
        "backend": backend.name,
        "items": [public_item(item) for item in created],
        "unconfirmed": [public_item(item) for item in store.items if not item_is_layout_ready(item)],
        "warning": note,
    }


def confirm_item(
    session: Any,
    item_id: str,
    *,
    catalog: str = "",
    manufacturer: str = "",
    envelope_w_mm: float | None = None,
    envelope_h_mm: float | None = None,
    envelope_d_mm: float | None = None,
    tag: str = "",
    library_id: str = "",
    mount: str = "",
) -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    item = store.item(item_id)
    if library_id:
        session.catalog.get(library_id)
        item.library_id = library_id
    if catalog:
        hits = match_catalog(
            session.parts,
            part_number=catalog,
            brand=manufacturer,
            limit=3,
            devices=getattr(session, "catalog", None),
        )
        if not hits:
            raise ValueError(f"no catalog match for '{catalog}'")
        item.matches = hits
        item.part_number = hits[0].catalog
        item.brand = hits[0].manufacturer
        item.status = "catalog"
        item.envelope_confirmed = False
        item.mount = ""
        if hits[0].library_id:
            item.library_id = hits[0].library_id
        elif item.library_id == "reconstruct.measured_envelope":
            item.library_id = ""
        if not catalog_match_has_footprint(hits[0]):
            item.status = "catalog"
    else:
        from panelcad.library.panel import require_explicit_envelope

        if not str(mount or item.mount or "").strip() and None not in (
            envelope_w_mm,
            envelope_h_mm,
            envelope_d_mm,
        ):
            from panelcad.library.panel import LayoutNotReadyError

            raise LayoutNotReadyError("pick a mounting (din_rail, backplate, door, field)")
        width, height, depth, token = require_explicit_envelope(
            envelope_w_mm, envelope_h_mm, envelope_d_mm, mount or item.mount
        )
        item.envelope_w_mm = width
        item.envelope_h_mm = height
        item.envelope_d_mm = depth
        item.mount = token
        item.envelope_confirmed = True
        item.status = "envelope"
        item.library_id = "reconstruct.measured_envelope"
        # A catalog pick is not the label. Keep only what the photo actually read.
        item.part_number = item.label_part_number
        item.brand = item.label_brand
        item.matches = []
    if item_is_layout_ready(item):
        item.unreadable = False
    if tag:
        item.tag = tag
    for step in store.checklist:
        if step.kind in {"identify_part", "needs_info"} and step.item_id == item.id and step.status == "open":
            step.status = "confirmed"
            step.confirmed = True
            step.answer = item.part_number or f"{item.envelope_w_mm}x{item.envelope_h_mm}x{item.envelope_d_mm} mm"
    if item_is_layout_ready(item):
        _resolve_placeholder_component(session, item)
    rebuild_checklist(store)
    reply = public_item(item)
    if item.placed_ref:
        from panelcad.library.panel import is_layout_ready

        ready = True
        found = False
        for sheet in session.project.sheets:
            try:
                placed = sheet.component(item.placed_ref)
            except KeyError:
                continue
            found = True
            device = session.catalog.get(placed.library_id)
            if not is_layout_ready(device, placed, parts=session.parts):
                ready = False
        if found:
            reply["layout_ready"] = ready
    return reply


def place_confirmed(session: Any, sheet_id: str | None = None) -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    placed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in store.items:
        if item.role != "part":
            continue
        if item.placed_ref:
            continue
        photo = store.photo(item.photo_id)
        if not item_is_layout_ready(item):
            skipped.append({"id": item.id, "reason": "no_footprint" if item.status == "catalog" else "unconfirmed"})
            continue
        refuse_generic(item)
        if mm_per_px(photo) is None:
            skipped.append({"id": item.id, "reason": "unscaled"})
            continue
        kind = target_sheet_kind(photo.role)
        sheet = _ensure_layout_sheet(session, store, kind, sheet_id if kind == "panel_layout" else None)
        _fit_sheet(sheet, photo, store.items)
        xy = measured_xy(store, item)
        if xy is None:
            skipped.append({"id": item.id, "reason": "unscaled"})
            continue
        library_id = _library_id(item)
        props = placement_properties(item)
        ref = _unique_ref(session, item.tag or _guess_ref(item, library_id), sheet.id)
        session.place(
            library_id,
            ref=ref,
            x=xy[0],
            y=xy[1],
            sheet_id=sheet.id,
            properties=props,
            snap=False,
            from_reconstruct_confirm=True,
        )
        if item.status == "catalog" and item.matches:
            try:
                session.assign_part(ref, item.matches[0].catalog, manufacturer=item.matches[0].manufacturer)
            except (KeyError, ValueError):
                pass
            comp = sheet.component(ref)
            keep = {k: v for k, v in props.items() if str(k).startswith("photo") or str(k).startswith("reconstruct")}
            keep.update(
                {
                    k: v
                    for k, v in props.items()
                    if k in {
                        "width_mm",
                        "height_mm",
                        "depth_mm",
                        "dim_source",
                        "envelope_confirmed",
                        "mount",
                        "ratings",
                        "terminal",
                        "wire_number",
                    }
                }
            )
            comp.properties.update(keep)
        item.placed_ref = ref
        item.sheet = sheet.id
        item.x_mm = xy[0]
        item.y_mm = xy[1]
        placed.append({"id": item.id, "ref": ref, "sheet": sheet.id, "x": xy[0], "y": xy[1]})
    rebuild_checklist(store)
    return {"placed": placed, "skipped": skipped}


def answer(
    session: Any,
    step_id: str,
    text: str = "",
    *,
    photo: Path | str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    step = store.step(step_id)
    step.answer = str(text or "").strip()
    if photo is not None:
        added = add_photos(session, [photo], role="other", note=f"answer {step.id}")
        if added["added"]:
            step.answer_photo_id = added["added"][0]["id"]
    if step.kind == "identify_part" and step.item_id:
        landing_catalog = step.answer
        if landing_catalog:
            try:
                confirm_item(session, step.item_id, catalog=landing_catalog)
                step.status = "confirmed"
                step.confirmed = True
                rebuild_checklist(store)
                return public_step(step)
            except ValueError:
                pass
    if step.kind == "trace_wire":
        landing = parse_landing(step.answer)
        if not landing:
            step.status = "open"
            step.to_ref = ""
            step.confirmed = False
            return {
                **public_step(step),
                "message": "I can't find that pin: type it like K1:A1",
            }
        step.to_ref = landing
        wire = _record_inferred_wire(session, store, step)
        if wire is None:
            step.status = "open"
            step.to_ref = ""
            step.wire_id = ""
            step.confirmed = False
            return {
                **public_step(step),
                "message": "I can't find that pin: type it like K1:A1",
            }
        step.wire_id = wire
        step.status = "answered"
        step.inferred = True
    elif step.kind == "confirm_connection":
        if confirm or str(text).strip().lower() in {"yes", "confirm", "ok", "true"}:
            _confirm_wire(session, step.wire_id)
            step.status = "confirmed"
            step.confirmed = True
            step.inferred = False
        else:
            step.status = "answered"
    elif step.kind == "confirm_scale":
        step.status = "open"
        step.answer = ""
        step.confirmed = False
        return {
            **public_step(step),
            "message": "that number was not stored — mark two known lengths or the enclosure width and height on the photo",
        }
    elif step.kind == "compare":
        step.status = "answered"
    else:
        step.status = "answered"
    rebuild_checklist(store)
    return public_step(step)


def confirm_connection(session: Any, step_id: str | None = None, wire_id: str | None = None) -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    if step_id:
        return answer(session, step_id, "confirm", confirm=True)
    if not wire_id:
        raise ValueError("confirm_connection needs step_id or wire_id")
    _confirm_wire(session, wire_id)
    for step in store.checklist:
        if step.wire_id == wire_id:
            step.status = "confirmed"
            step.confirmed = True
            step.inferred = False
    synthesize_reconstruct_schematic(session)
    return {"wire_id": wire_id, "confirmed": True}


def attach_print(session: Any, path: Path | str, note: str = "") -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(str(src))
    print_id = next_id("d", [item.id for item in store.prints])
    dest_root = session.path.parent / PRINT_DIR if session.path else None
    if dest_root is not None:
        dest = copy_into(src, dest_root, print_id)
        stored = dest.relative_to(session.path.parent).as_posix()
    else:
        stored = str(src.resolve())
    index = load_print_index(src)
    item = PrintSource(
        id=print_id,
        path=stored,
        kind=index.get("kind") or classify_print_path(src),  # type: ignore[arg-type]
        note=note or str(index.get("note") or ""),
        extracted=bool(index.get("extracted")),
        extractor=str(index.get("extractor") or ""),
    )
    store.prints.append(item)
    return {**item.model_dump(mode="json"), "index": {k: index.get(k) for k in ("extracted", "extractor", "tags", "wires", "note") if k in index}}


def compare_print(session: Any, path: Path | str | None = None) -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    if path is not None:
        attach_print(session, path)
    if not store.prints:
        raise ValueError("attach a secondary print (PanelCAD project, print-index YAML, or PDF) first")
    src = resolve_path(store.prints[-1].path, session.path)
    index = load_print_index(src)
    store.prints[-1].extracted = bool(index.get("extracted"))
    store.prints[-1].extractor = str(index.get("extractor") or store.prints[-1].extractor)
    store.mismatches = compare_to_photos(store, index)
    rebuild_checklist(store)
    return {
        "print": store.prints[-1].model_dump(mode="json"),
        "extracted": bool(index.get("extracted")),
        "extractor": index.get("extractor") or "",
        "mismatches": [item.model_dump(mode="json") for item in store.mismatches],
    }


def photo_region(
    session: Any,
    item_id: str | None = None,
    ref: str | None = None,
    photo_id: str | None = None,
) -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    item = None
    if photo_id and not item_id and not ref:
        photo = store.photo(photo_id)
        path = resolve_path(photo.path, session.path)
        if not path.is_file() and photo.source_path and Path(photo.source_path).is_file():
            path = Path(photo.source_path)
        return {
            "item": None,
            "photo": public_photo(photo),
            "path": str(path),
            "region": {},
            "mm": None,
            "missing": not path.is_file(),
        }
    if item_id:
        try:
            item = store.item(item_id)
        except KeyError:
            item = None
            for candidate in store.items:
                if candidate.wire_number and item_id in {candidate.placed_ref, candidate.id}:
                    item = candidate
                    break
            if item is None:
                for sheet in session.project.sheets:
                    for wire in sheet.wires:
                        if wire.id != item_id:
                            continue
                        for candidate in store.items:
                            if candidate.wire_number and str(candidate.wire_number) == str(wire.number or ""):
                                item = candidate
                                break
                        if item is None:
                            return {
                                "item": None,
                                "photo": None,
                                "path": "",
                                "region": {},
                                "mm": None,
                                "message": f"inferred wire {item_id} has no source photo; focusing the schematic",
                                "wire_id": item_id,
                                "sheet": sheet.id,
                            }
    elif ref:
        for candidate in store.items:
            if candidate.placed_ref == ref or candidate.tag == ref:
                item = candidate
                break
        if item is None:
            for sheet in session.project.sheets:
                try:
                    comp = sheet.component(ref)
                except KeyError:
                    continue
                token = str((comp.properties or {}).get("reconstruct_item") or "")
                if token:
                    item = store.item(token)
                    break
    if item is None:
        raise KeyError("no reconstruct item for that ref")
    photo = store.photo(item.photo_id)
    path = resolve_path(photo.path, session.path)
    mm = photo_to_mm(photo, item.region.x + item.region.w / 2.0, item.region.y + item.region.h / 2.0)
    return {
        "item": public_item(item),
        "photo": public_photo(photo),
        "path": str(path),
        "region": item.region.model_dump(),
        "mm": None if mm is None else {"x": mm[0], "y": mm[1]},
    }


def relocate_files(session: Any, previous: Any = None) -> None:
    if session.path is None or session.project.reconstruct is None:
        return
    from pathlib import Path

    prev = Path(previous) if previous is not None else None
    relocate(session.project.reconstruct, session.path, previous=prev)
    from panelcad.reconstruct.store import embed_reconstruct, write_sidecar

    embed_reconstruct(session.project)
    write_sidecar(session.project, session.path)


def place_needs_info(session: Any, photo_id: str | None = None) -> dict[str, Any]:
    """Drop reconstruct-only INFO NEEDED markers on the layout. Never a generic body."""

    from panelcad.reconstruct.placeholder import (
        NEEDS_INFO_LIBRARY_ID,
        placeholder_properties,
        reconstruct_mode,
    )

    if not reconstruct_mode(session):
        from panelcad.library.panel import LayoutNotReadyError

        raise LayoutNotReadyError("needs-info placeholders can only be created in reconstruct mode")
    store = ensure_reconstruction(session.project)
    placed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in store.items:
        if item.role != "part" or item.placed_ref or item_is_layout_ready(item):
            continue
        if photo_id and item.photo_id != photo_id:
            continue
        item.status = "needs_info"
        photo = store.photo(item.photo_id)
        if mm_per_px(photo) is None:
            skipped.append({"id": item.id, "reason": "unplaced"})
            continue
        kind = target_sheet_kind(photo.role)
        sheet = _ensure_layout_sheet(session, store, kind, None)
        _fit_sheet(sheet, photo, store.items)
        xy = measured_xy(store, item)
        if xy is None:
            skipped.append({"id": item.id, "reason": "unplaced"})
            continue
        ref = _unique_ref(session, _marker_stem(session, item), sheet.id)
        props = placeholder_properties(item, photo)
        session.place(NEEDS_INFO_LIBRARY_ID, ref=ref, x=xy[0], y=xy[1], sheet_id=sheet.id, properties=props, snap=False)
        item.placed_ref = ref
        item.sheet = sheet.id
        item.x_mm = xy[0]
        item.y_mm = xy[1]
        placed.append({"id": item.id, "ref": ref, "sheet": sheet.id, "x": xy[0], "y": xy[1]})
    rebuild_checklist(store)
    return {"placed": placed, "skipped": skipped}


def _envelope_mm(value: float | None, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"envelope {name} must be a number in millimetres") from None
    if number < ENVELOPE_MIN_MM or number > ENVELOPE_MAX_MM:
        raise ValueError(
            f"envelope {name} {number:g} mm is not a sane panel-scale size "
            f"({ENVELOPE_MIN_MM:g}–{ENVELOPE_MAX_MM:g} mm)"
        )
    return number


def _resolve_placeholder_component(session: Any, item: ObservedItem) -> None:
    if not item.placed_ref:
        return
    from panelcad.library.panel import MEASURED_ENVELOPE_LIBRARY_ID, apply_confirmed_envelope
    from panelcad.reconstruct.layout import (
        apply_item_status_to_component,
        drop_envelope_confirmation,
        mark_confirmed_measured_description,
        strip_other_status_keys,
    )
    from panelcad.reconstruct.placeholder import is_needs_info_component

    library_id = _library_id(item)
    session.catalog.get(library_id)
    placed: list[Any] = []
    for sheet in session.project.sheets:
        try:
            placed.append(sheet.component(item.placed_ref))
        except KeyError:
            pass
        for comp in sheet.components:
            if str((comp.properties or {}).get("reconstruct_item") or "") == item.id and comp not in placed:
                placed.append(comp)
    for comp in placed:
        keep_x, keep_y = comp.x, comp.y
        if (
            is_needs_info_component(comp)
            or str(comp.library_id or "") == MEASURED_ENVELOPE_LIBRARY_ID
            or item.status == "envelope"
        ):
            comp.library_id = library_id
        apply_item_status_to_component(comp, item)
        if item.status == "catalog":
            drop_envelope_confirmation(comp)
        if item.status == "envelope" and item.envelope_confirmed:
            apply_confirmed_envelope(
                comp,
                width_mm=float(item.envelope_w_mm),
                height_mm=float(item.envelope_h_mm),
                depth_mm=float(item.envelope_d_mm),
                mount=str(item.mount),
                confirmed_by=str(session.project.title_block.drawn_by or "user"),
            )
            mark_confirmed_measured_description(
                comp,
                float(item.envelope_w_mm),
                float(item.envelope_h_mm),
                float(item.envelope_d_mm),
            )
            comp.properties = strip_other_status_keys(comp.properties, "envelope")
            from panelcad.reconstruct.layout import label_identity

            for key in ("manufacturer", "part_number", "catalog", "catalog_key"):
                comp.properties.pop(key, None)
            comp.properties.update(label_identity(item))
        comp.x = keep_x
        comp.y = keep_y
        item.x_mm = keep_x
        item.y_mm = keep_y
    if item.status == "catalog" and item.matches and item.placed_ref:
        try:
            session.assign_part(
                item.placed_ref,
                item.matches[0].catalog,
                manufacturer=item.matches[0].manufacturer,
            )
        except (KeyError, ValueError):
            pass
        for comp in placed:
            drop_envelope_confirmation(comp)


def _role(role: str) -> str:
    token = str(role or "other").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "back": "backpanel",
        "backplate": "backpanel",
        "panel": "backpanel",
        "door": "door_inside",
        "inside": "door_inside",
        "outside": "door_outside",
        "label": "part_label",
        "name": "nameplate",
        "terminals": "terminal_strip",
        "strip": "terminal_strip",
        "wires": "wire_numbers",
        "wire": "wire_numbers",
    }
    token = aliases.get(token, token)
    if token not in PHOTO_ROLES:
        raise ValueError(f"photo role must be one of {', '.join(PHOTO_ROLES)}")
    return token


def _corners(raw: list[Any]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for item in raw:
        if isinstance(item, dict):
            points.append((float(item.get("x")), float(item.get("y"))))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            points.append((float(item[0]), float(item[1])))
        elif hasattr(item, "x"):
            points.append((float(item.x), float(item.y)))
    return points


def _items_from_extract(session: Any, store: Reconstruction, photo: Photo, extract: dict[str, Any]) -> list[ObservedItem]:
    created: list[ObservedItem] = []
    next_item = _id_factory("i", [item.id for item in store.items])
    width = float(photo.width_px or 1)
    height = float(photo.height_px or 1)
    for raw in extract.get("devices") or []:
        if not isinstance(raw, dict):
            continue
        tag = str(raw.get("tag") or "").strip()
        brand = str(raw.get("brand") or raw.get("manufacturer") or "").strip()
        part_number = str(raw.get("part_number") or raw.get("catalog") or "").strip()
        label_text = str(raw.get("label_text") or raw.get("text") or "").strip()
        if not (tag or brand or part_number or label_text):
            continue
        if "confidence" in raw and _safe_float(raw.get("confidence"), None) is None:
            continue
        if _bad_region_number(raw):
            continue
        region = _region(raw, width, height)
        if region is None:
            continue
        if any(
            existing.photo_id == photo.id
            and existing.role == "part"
            and ((tag and existing.tag == tag) or (not tag and _same_region(existing.region, region)))
            for existing in store.items
        ):
            continue
        conf = _safe_float(raw.get("confidence"), 0.0) or 0.0
        item = ObservedItem(
            id=next_item(),
            photo_id=photo.id,
            role="part",
            tag=tag,
            brand=brand,
            part_number=part_number,
            label_text=label_text,
            region=region,
            confidence=conf,
            unreadable=bool(raw.get("unreadable")),
            status="needs_info",
            label_part_number=part_number,
            label_brand=brand,
        )
        if item.part_number or item.brand:
            item.matches = match_catalog(
                session.parts,
                part_number=item.part_number,
                brand=item.brand,
                devices=getattr(session, "catalog", None),
            )
        store.items.append(item)
        created.append(item)
    for strip in extract.get("strips") or []:
        strip_tag = str(strip.get("tag") or "").strip()
        for term in strip.get("terminals") or []:
            item = ObservedItem(
                id=next_item(),
                photo_id=photo.id,
                role="terminal",
                tag=strip_tag,
                terminal=str(term.get("id") or term.get("label") or "").strip(),
                region=_region(term, width, height) or PhotoRegion(),
                confidence=_safe_float(term.get("confidence"), 0.0) or 0.0,
            )
            store.items.append(item)
            created.append(item)
        for wire in strip.get("wire_numbers") or []:
            item = ObservedItem(
                id=next_item(),
                photo_id=photo.id,
                role="wire_number",
                tag=strip_tag,
                terminal=str(wire.get("terminal") or "").strip(),
                wire_number=str(wire.get("number") or wire.get("text") or "").strip(),
                region=_region(wire, width, height),
            )
            store.items.append(item)
            created.append(item)
    for text in extract.get("texts") or []:
        kind = str(text.get("kind") or "").strip().lower()
        if kind not in {"wire_number", "terminal", "tag"}:
            continue
        item = ObservedItem(
            id=next_item(),
            photo_id=photo.id,
            role="wire_number" if kind == "wire_number" else ("terminal" if kind == "terminal" else "device_tag"),
            tag=str(text.get("tag") or "").strip(),
            terminal=str(text.get("terminal") or "").strip(),
            wire_number=str(text.get("text") or text.get("number") or "").strip() if kind == "wire_number" else "",
            label_text=str(text.get("text") or "").strip(),
            region=_region(text, width, height),
        )
        store.items.append(item)
        created.append(item)
    return created


def _bad_region_number(raw: dict[str, Any]) -> bool:
    for key in ("nx", "ny", "nw", "nh", "x", "y", "w", "h", "width", "height"):
        if key in raw and raw[key] not in (None, "") and _safe_float(raw.get(key), None) is None:
            return True
    return False


def _region(raw: dict[str, Any], width: float, height: float) -> PhotoRegion | None:
    if _bad_region_number(raw):
        return None
    nx = _safe_float(raw.get("nx"), None)
    ny = _safe_float(raw.get("ny"), None)
    nw = _safe_float(raw.get("nw"), None)
    nh = _safe_float(raw.get("nh"), None)
    if "nx" in raw or "nw" in raw or "ny" in raw or "nh" in raw:
        if nx is None and ny is None and nw is None and nh is None:
            return None
        nx = 0.0 if nx is None else nx
        ny = 0.0 if ny is None else ny
        nw = 0.0 if nw is None else nw
        nh = 0.0 if nh is None else nh
        if nx > 1.5 or ny > 1.5:
            if nx > width or ny > height or nw > width or nh > height or nx < 0 or ny < 0:
                return None
            return PhotoRegion(x=nx, y=ny, w=nw, h=nh)
        if nx < 0 or ny < 0 or nx > 1.0 or ny > 1.0:
            return None
        return PhotoRegion(x=nx * width, y=ny * height, w=nw * width, h=nh * height)
    x = _safe_float(raw.get("x"), 0.0) or 0.0
    y = _safe_float(raw.get("y"), 0.0) or 0.0
    w = _safe_float(raw.get("w") or raw.get("width"), 0.0) or 0.0
    h = _safe_float(raw.get("h") or raw.get("height"), 0.0) or 0.0
    if w <= 0 and h <= 0 and x == 0 and y == 0:
        return PhotoRegion()
    if x > width or y > height:
        return None
    return PhotoRegion(x=x, y=y, w=w, h=h)


def _safe_float(value: Any, default: float | None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _same_region(a: PhotoRegion, b: PhotoRegion) -> bool:
    return abs(a.x - b.x) < 2 and abs(a.y - b.y) < 2 and abs(a.w - b.w) < 2 and abs(a.h - b.h) < 2


def _ensure_layout_sheet(session: Any, store: Reconstruction, kind: str, sheet_id: str | None) -> Any:
    if sheet_id:
        sheet = session.project.sheet(sheet_id)
        if kind == "panel_layout":
            store.panel_sheet = sheet.id
        else:
            store.door_sheet = sheet.id
        return sheet
    current = store.panel_sheet if kind == "panel_layout" else store.door_sheet
    if current:
        try:
            return session.project.sheet(current)
        except KeyError:
            pass
    for sheet in session.project.sheets:
        if sheet.kind == kind:
            if kind == "panel_layout":
                store.panel_sheet = sheet.id
            else:
                store.door_sheet = sheet.id
            return sheet
    sid = "panel" if kind == "panel_layout" else "door"
    if any(sheet.id == sid for sheet in session.project.sheets):
        sid = next_id(sid, [sheet.id for sheet in session.project.sheets])
    sheet = session.add_sheet(sid, title="Panel" if kind == "panel_layout" else "Door", kind=kind)
    if kind == "panel_layout":
        store.panel_sheet = sheet.id
    else:
        store.door_sheet = sheet.id
    return sheet


def _library_id(item: ObservedItem) -> str:
    if item.status == "envelope":
        return "reconstruct.measured_envelope"
    if item.status == "catalog":
        if item.matches and item.matches[0].library_id:
            return item.matches[0].library_id
        if item.library_id and item.library_id not in {
            "reconstruct.needs_info",
            "reconstruct.measured_envelope",
        }:
            return item.library_id
    if item.library_id and item.library_id != "reconstruct.needs_info":
        return item.library_id
    if item.matches:
        return item.matches[0].library_id
    raise ValueError(f"{item.id} has no library_id; confirm a catalog part or a symbol for the envelope")


def _fit_sheet(sheet: Any, photo: Photo, items: list[Any] | None = None) -> None:
    fit_sheet_to_photo(sheet, photo, items)


def _marker_stem(session: Any, item: ObservedItem) -> str:
    """Type prefix or the tag read off the photo. Never a bare U."""

    tag = str(item.tag or "").strip()
    if tag and tag.upper() != "U":
        return tag
    library_id = ""
    if item.matches and item.matches[0].library_id:
        library_id = item.matches[0].library_id
    elif item.library_id and item.library_id not in {
        "reconstruct.needs_info",
        "reconstruct.measured_envelope",
    }:
        library_id = item.library_id
    device = None
    if library_id:
        try:
            device = session.catalog.get(library_id)
        except KeyError:
            device = None
    if device is not None:
        prefix = str(device.ref_prefix or "").strip()
        if prefix and prefix.upper() != "U":
            return prefix
        guessed = _prefix_for_category(device.category)
        if guessed:
            return guessed
    return "NI"


def _prefix_for_category(category: str) -> str:
    """Prefix from a category word. ``io`` does not match inside ``isolation``."""

    token = str(category or "").casefold().replace("-", ".").replace("/", ".")
    words = {part for part in token.split(".") if part}
    if "contactor" in words or "relay" in words:
        return "K"
    if words & {"breaker", "mcb", "fuse"}:
        return "Q"
    if words & {"supply", "psu"}:
        return "G"
    if "plc" in words or "io" in words:
        return "A"
    if "terminal" in words:
        return "X"
    if words & {"drive", "vfd"}:
        return "V"
    return ""


def _guess_ref(item: ObservedItem, library_id: str) -> str:
    if item.tag:
        return item.tag
    if "contactor" in library_id:
        return "K"
    if "breaker" in library_id:
        return "Q"
    if "terminal" in library_id:
        return "X"
    return "P"


def _unique_ref(session: Any, wanted: str, sheet_id: str) -> str:
    base = str(wanted or "P").strip() or "P"
    used = {comp.id for sheet in session.project.sheets for comp in sheet.components}
    used.update(comp.id for comp in session.project.sheet(sheet_id).components)
    # A short prefix with no digit (P, NI, A) collides with the numbered series.
    if not any(ch.isdigit() for ch in base) and len(base) <= 3:
        n = 1
        while f"{base}{n}" in used:
            n += 1
        return f"{base}{n}"
    if base not in used:
        return base
    if base[-1:].isdigit():
        stem = base.rstrip("0123456789") or base
        n = 1
        while f"{stem}{n}" in used:
            n += 1
        return f"{stem}{n}"
    n = 1
    while f"{base}{n}" in used:
        n += 1
    return f"{base}{n}"


def _record_inferred_wire(session: Any, store: Reconstruction, step) -> str | None:
    landing = step.to_ref
    origin = step.from_ref.replace(":", ".") if step.from_ref else ""
    if not landing:
        return None
    sheet = _ensure_schematic(session, store)
    _ensure_schematic_end(session, store, sheet, origin)
    _ensure_schematic_end(session, store, sheet, landing)
    frm, frm_note = _usable_pin_ref(session, sheet, origin)
    to, to_note = _usable_pin_ref(session, sheet, landing)
    if not frm:
        frm = _named_pin(session, sheet, origin)
    if not to:
        to = _named_pin(session, sheet, landing)
    if not frm or not to or frm.upper() == to.upper():
        return None
    if frm_note:
        from panelcad.render.sheet import parse_pin_ref

        try:
            _ref, pin = parse_pin_ref(frm)
        except ValueError:
            pin = ""
        if pin and step.item_id:
            try:
                observed = store.item(step.item_id)
                observed.terminal = pin
            except KeyError:
                pass
        step.from_ref = frm.replace(".", ":")
        step.prompt = (
            f"{step.prompt} Pin {origin} is not on the symbol; using {frm} instead."
        )
    if to_note:
        step.to_ref = to.replace(".", ":")
        step.prompt = f"{step.prompt} Landing stored as {to}."
    for existing in sheet.wires:
        if (
            existing.frm.upper() == frm.upper()
            and existing.to.upper() == to.upper()
            and str(existing.number or "") == str(step.wire_number or existing.number or "")
        ):
            existing.inferred = True
            return existing.id
    try:
        wire = session.wire(frm, to, sheet_id=sheet.id, number=step.wire_number or None)
    except (KeyError, ValueError):
        return None
    wire.inferred = True
    if step.wire_number and not wire.number:
        wire.number = step.wire_number
    synthesize_reconstruct_schematic(session)
    return wire.id


def _usable_pin_ref(session: Any, sheet, pin_ref: str) -> tuple[str, str]:
    if not pin_ref:
        return "", ""
    try:
        session._assert_pin(sheet, pin_ref)
        return pin_ref, ""
    except (KeyError, ValueError):
        pass
    from panelcad.render.sheet import parse_pin_ref

    try:
        ref, pin = parse_pin_ref(pin_ref)
    except ValueError:
        return "", ""
    for alt in ("A", "13", "A1", "1", "X1"):
        candidate = f"{ref}.{alt}"
        try:
            session._assert_pin(sheet, candidate)
            return candidate, f"pin {pin} is not on {ref}; using {alt}"
        except (KeyError, ValueError):
            continue
    return "", ""


def _ensure_schematic(session: Any, store: Reconstruction):
    if store.schematic_sheet:
        try:
            return session.project.sheet(store.schematic_sheet)
        except KeyError:
            pass
    for sheet in session.project.sheets:
        if sheet.kind == "schematic":
            store.schematic_sheet = sheet.id
            return sheet
    sheet = session.add_sheet("asbuilt", title="As-built schematic", kind="schematic")
    store.schematic_sheet = sheet.id
    return sheet


def _ensure_schematic_end(session: Any, store: Reconstruction, sheet, pin_ref: str) -> None:
    """Put a real device or a real strip terminal on the schematic.

    A part that is already an envelope or a catalog match is never turned into
    a terminal block. An unknown tag is left alone.
    """

    from panelcad.library.panel import MEASURED_ENVELOPE_LIBRARY_ID
    from panelcad.render.sheet import parse_pin_ref

    if not pin_ref or "." not in pin_ref:
        return
    try:
        ref, _pin = parse_pin_ref(pin_ref)
    except ValueError:
        return
    try:
        sheet.component(ref)
        return
    except KeyError:
        pass
    observed = _observed_for_tag(store, ref)
    library_id = ""
    props: dict[str, Any] = {"reconstruct_schematic_stub": "yes", "in_panel": "no", "bom": "no"}
    if observed is not None and observed.role == "part":
        if observed.status == "envelope" or observed.library_id == MEASURED_ENVELOPE_LIBRARY_ID:
            library_id = MEASURED_ENVELOPE_LIBRARY_ID
            if observed.envelope_w_mm and observed.envelope_h_mm and observed.envelope_d_mm:
                props.update(
                    {
                        "width_mm": observed.envelope_w_mm,
                        "height_mm": observed.envelope_h_mm,
                        "depth_mm": observed.envelope_d_mm,
                        "mount": observed.mount,
                        "dim_source": "measured",
                        "envelope_confirmed": "yes",
                    }
                )
        elif observed.matches and observed.matches[0].library_id:
            library_id = observed.matches[0].library_id
        elif observed.library_id and observed.library_id not in {
            "reconstruct.needs_info",
            MEASURED_ENVELOPE_LIBRARY_ID,
        }:
            library_id = observed.library_id
        if not library_id:
            return
    elif observed is not None and observed.role in {"terminal", "wire_number"}:
        library_id = "iec.terminals.x_2p5"
    else:
        existing = _component_library(session, ref)
        if not existing or existing == "iec.terminals.x_2p5":
            return
        library_id = existing
    try:
        session.catalog.get(library_id)
    except KeyError:
        return
    x = 40 + 30 * len(sheet.components)
    session.place(
        library_id,
        ref=ref,
        x=x,
        y=40,
        sheet_id=sheet.id,
        properties=props,
        snap=False,
        from_reconstruct_confirm=library_id == MEASURED_ENVELOPE_LIBRARY_ID,
    )


def _observed_for_tag(store: Reconstruction, ref: str) -> ObservedItem | None:
    parts = [item for item in store.items if item.role == "part" and (item.placed_ref == ref or item.tag == ref)]
    if parts:
        return parts[0]
    for item in store.items:
        if item.placed_ref == ref or item.tag == ref:
            return item
    return None


def _component_library(session: Any, ref: str) -> str:
    for sheet in session.project.sheets:
        try:
            return str(sheet.component(ref).library_id or "")
        except KeyError:
            continue
    return ""


def _named_pin(session: Any, sheet, pin_ref: str) -> str:
    """Keep a pin the tech named on a symbol that does not list it.

    This does not create a terminal block. The wire stays inferred.
    """

    from panelcad.render.sheet import parse_pin_ref

    if not pin_ref:
        return ""
    try:
        ref, pin = parse_pin_ref(pin_ref)
    except ValueError:
        return ""
    try:
        comp = sheet.component(ref)
    except KeyError:
        return ""
    try:
        device = session.catalog.get(comp.library_id)
        device.pin(pin)
        return pin_ref
    except KeyError:
        pass
    raw = str((comp.properties or {}).get("reconstruct_named_pins") or "")
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if pin not in names:
        names.append(pin)
    comp.properties["reconstruct_named_pins"] = ",".join(names)
    return pin_ref


def _confirm_wire(session: Any, wire_id: str) -> None:
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            if wire.id == wire_id:
                wire.inferred = False
                return
    raise KeyError(f"no wire '{wire_id}'")


def _id_factory(prefix: str, used: list[str]):
    seen = set(used)
    index = 1

    def _next() -> str:
        nonlocal index
        while f"{prefix}{index}" in seen:
            index += 1
        token = f"{prefix}{index}"
        seen.add(token)
        index += 1
        return token

    return _next


def next_question(session: Any) -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    rebuild_checklist(store)
    step = next_open(store)
    if step is None:
        return {"done": True, "step": None}
    return {"done": False, "step": public_step(step)}


def list_unconfirmed(session: Any) -> dict[str, Any]:
    store = ensure_reconstruction(session.project)
    return {
        "items": [
            public_item(item)
            for item in store.items
            if item.role == "part" and not item_is_layout_ready(item)
        ]
    }


_LADDER_RAILS = {"L1", "L2"}
_CONTROL_COIL_PINS = {"A1", "A2"}
_CONTROL_OL_PINS = {"95", "96", "97", "98"}
_CONTROL_AUX_NO_PINS = {"13", "14"}
_CONTROL_AUX_NC_PINS = {"21", "22"}
_CONTROL_PB_NC_PINS = {"1", "2"}
_CONTROL_PB_NO_PINS = {"3", "4"}
_CONTROL_LIGHT_PINS = {"X1", "X2"}
_POWER_NAMED_PINS = {"L1", "L2", "L3", "T1", "T2", "T3", "U", "V", "W", "R", "S", "T"}


def _normalize_landing_end(tag: str = "", pin: str = "", ref: str = "") -> str:
    raw_ref = str(ref or "").strip()
    if raw_ref:
        cleaned = raw_ref.replace(":", ".")
        upper = cleaned.upper()
        if upper in _LADDER_RAILS or upper in {"L1.L1", "L2.L2"}:
            return upper.split(".", 1)[0]
        return cleaned
    t = str(tag or "").strip()
    p = str(pin or "").strip()
    if not t and p.upper() in _LADDER_RAILS:
        return p.upper()
    if t.upper() in _LADDER_RAILS and (not p or p.upper() == t.upper()):
        return t.upper()
    if "." in t and not p:
        return t
    if ":" in t and not p:
        return t.replace(":", ".", 1)
    if t and p:
        return f"{t}.{p}"
    raise ValueError("wire landing requires both from and to endpoints")


def _split_endpoint(endpoint: str) -> tuple[str, str]:
    token = str(endpoint or "").strip()
    if token.upper() in _LADDER_RAILS:
        return token.upper(), ""
    from panelcad.render.sheet import parse_pin_ref

    return parse_pin_ref(token)


def _tag_prefix_letters(ref: str) -> str:
    base = str(ref or "").split("-", 1)[0].strip().upper()
    letters: list[str] = []
    for ch in base:
        if ch.isalpha():
            letters.append(ch)
        else:
            break
    return "".join(letters)


def _base_library_for_tag(session: Any, store: Reconstruction, ref: str) -> str:
    from panelcad.library.panel import MEASURED_ENVELOPE_LIBRARY_ID

    observed = _observed_for_tag(store, ref)
    if observed is None and "-" in ref:
        observed = _observed_for_tag(store, ref.split("-", 1)[0])
    if observed is not None and observed.role == "part":
        if observed.library_id and observed.library_id not in {"reconstruct.needs_info", MEASURED_ENVELOPE_LIBRARY_ID}:
            return str(observed.library_id)
        if observed.status == "envelope" or observed.library_id == MEASURED_ENVELOPE_LIBRARY_ID:
            return MEASURED_ENVELOPE_LIBRARY_ID
        if observed.matches and observed.matches[0].library_id:
            return str(observed.matches[0].library_id)
    elif observed is not None and observed.role in {"terminal", "wire_number"}:
        return "iec.terminals.x_2p5"
    existing = _component_library(session, ref)
    if not existing and "-" in ref:
        existing = _component_library(session, ref.split("-", 1)[0])
    return existing


def _is_power_endpoint(session: Any, store: Reconstruction, endpoint: str) -> bool:
    if not endpoint or endpoint.upper() in _LADDER_RAILS or "." not in endpoint:
        return False
    try:
        ref, pin = _split_endpoint(endpoint)
    except ValueError:
        return False
    pin_u = pin.upper()
    prefix = _tag_prefix_letters(ref)
    base_lib = _base_library_for_tag(session, store, ref).lower()
    if "-" in ref:
        return False
    if pin_u in (
        _CONTROL_COIL_PINS
        | _CONTROL_OL_PINS
        | _CONTROL_AUX_NO_PINS
        | _CONTROL_AUX_NC_PINS
        | _CONTROL_LIGHT_PINS
    ):
        return False
    if prefix in {"S", "PB", "PL", "LT", "H", "CR", "X", "TB"}:
        return False
    if any(tok in base_lib for tok in ("operators.", "terminals.", "pilot_light", "pb_n", "aux_n", "ol_contact", "coil")):
        return False
    if pin_u in _POWER_NAMED_PINS:
        return True
    if pin_u in {"1", "2", "3", "4", "5", "6"} and (
        prefix in {"CB", "Q", "FU", "F", "M", "K", "OL", "MTR", "MOT", "VFD"}
        or any(tok in base_lib for tok in ("breaker", "contactor", "overload", "motor", "fuse", "disconnect", "vfd"))
    ):
        return True
    return False


def _is_power_wire_landing(session: Any, store: Reconstruction, frm: str, to: str) -> bool:
    if frm.upper() in _LADDER_RAILS or to.upper() in _LADDER_RAILS:
        return False
    return _is_power_endpoint(session, store, frm) or _is_power_endpoint(session, store, to)


def _control_library_for_ref(
    session: Any,
    store: Reconstruction,
    ref: str,
    used_pins: set[str],
    current_lib: str = "",
) -> tuple[str, dict[str, Any]]:
    from panelcad.library.panel import MEASURED_ENVELOPE_LIBRARY_ID

    observed = _observed_for_tag(store, ref)
    base_ref = ref.split("-", 1)[0]
    if observed is None and "-" in ref:
        observed = _observed_for_tag(store, base_ref)
    props: dict[str, Any] = {"reconstruct_schematic_stub": "yes", "in_panel": "no", "bom": "no"}
    if current_lib == MEASURED_ENVELOPE_LIBRARY_ID:
        if observed is not None and observed.envelope_w_mm and observed.envelope_h_mm and observed.envelope_d_mm:
            props.update(
                {
                    "width_mm": observed.envelope_w_mm,
                    "height_mm": observed.envelope_h_mm,
                    "depth_mm": observed.envelope_d_mm,
                    "mount": observed.mount,
                    "dim_source": "measured",
                    "envelope_confirmed": "yes",
                }
            )
        return MEASURED_ENVELOPE_LIBRARY_ID, props

    pins_u = {p.upper() for p in used_pins if p}
    prefix = _tag_prefix_letters(ref)
    base_lib = (current_lib or _base_library_for_tag(session, store, ref)).lower()

    if "-" in ref:
        props["device_tag"] = base_ref
        if pins_u and pins_u <= _CONTROL_AUX_NC_PINS:
            return "iec.contactors.aux_nc", props
        if pins_u and pins_u <= _CONTROL_PB_NO_PINS:
            return "nema.contactors.aux_no", props
        return "iec.contactors.aux_no", props

    if pins_u and pins_u <= _CONTROL_OL_PINS and (prefix in {"OL", "F"} or "overload" in base_lib or "ol_contact" in base_lib):
        props["device_tag"] = base_ref
        return "nema.protection.ol_contact", props

    if pins_u and pins_u <= _CONTROL_COIL_PINS and (
        prefix in {"M", "K", "CR"} or "contactor" in base_lib or "coil" in base_lib or "relay" in base_lib
    ):
        props["device_tag"] = base_ref
        return "nema.contactors.coil", props

    if pins_u and pins_u <= _CONTROL_AUX_NO_PINS and (
        prefix in {"M", "K", "CR"} or "contactor" in base_lib or "aux" in base_lib
    ):
        props["device_tag"] = base_ref
        return "iec.contactors.aux_no", props

    if pins_u and pins_u <= _CONTROL_AUX_NC_PINS and (
        prefix in {"M", "K", "CR"} or "contactor" in base_lib or "aux" in base_lib
    ):
        props["device_tag"] = base_ref
        return "iec.contactors.aux_nc", props

    if pins_u and pins_u <= _CONTROL_LIGHT_PINS and (prefix in {"PL", "LT", "H"} or "pilot_light" in base_lib):
        return "nema.operators.pilot_light", props

    if prefix in {"S", "PB"} or "pb_nc" in base_lib or "pb_no" in base_lib:
        if pins_u and pins_u <= _CONTROL_PB_NC_PINS:
            return "nema.operators.pb_nc", props
        if pins_u and pins_u <= _CONTROL_PB_NO_PINS:
            return "nema.operators.pb_no", props
        if "pb_nc" in base_lib:
            return "nema.operators.pb_nc", props
        return "nema.operators.pb_no", props

    if (observed is not None and observed.role in {"terminal", "wire_number"}) or prefix in {"X", "TB"} or "terminals." in base_lib:
        return "iec.terminals.x_2p5", props

    chosen = current_lib or _base_library_for_tag(session, store, ref)
    if not chosen:
        if prefix in {"PL", "LT", "H"}:
            chosen = "nema.operators.pilot_light"
        elif prefix in {"M", "K", "CR"}:
            chosen = "nema.contactors.coil"
        elif prefix in {"OL"}:
            chosen = "nema.protection.ol_contact"
        elif prefix in {"S", "PB"}:
            chosen = "nema.operators.pb_no"
        else:
            chosen = "iec.terminals.x_2p5"
    return chosen, props


def _power_library_for_ref(
    session: Any,
    store: Reconstruction,
    ref: str,
    current_lib: str = "",
) -> tuple[str, dict[str, Any]]:
    from panelcad.library.panel import MEASURED_ENVELOPE_LIBRARY_ID

    props: dict[str, Any] = {"reconstruct_schematic_stub": "yes", "in_panel": "no", "bom": "no"}
    base_lib = current_lib or _base_library_for_tag(session, store, ref)
    if current_lib == MEASURED_ENVELOPE_LIBRARY_ID:
        observed = _observed_for_tag(store, ref)
        if observed is not None and observed.envelope_w_mm and observed.envelope_h_mm and observed.envelope_d_mm:
            props.update(
                {
                    "width_mm": observed.envelope_w_mm,
                    "height_mm": observed.envelope_h_mm,
                    "depth_mm": observed.envelope_d_mm,
                    "mount": observed.mount,
                    "dim_source": "measured",
                    "envelope_confirmed": "yes",
                }
            )
        return MEASURED_ENVELOPE_LIBRARY_ID, props
    low = base_lib.lower()
    prefix = _tag_prefix_letters(ref)
    if any(tok in low for tok in ("breaker", "contactor", "overload", "motor", "fuse", "disconnect", "vfd")) and not low.endswith(
        (".coil", ".ol_contact", ".aux_no", ".aux_nc")
    ):
        return base_lib, props
    if prefix in {"CB", "Q", "FU", "F"}:
        return "iec.breakers.mcb_3p_32a", props
    if prefix in {"M", "K"}:
        return "iec.contactors.ac3_9a", props
    if prefix in {"OL"}:
        return "iec.protection.overload_9a", props
    if prefix in {"MTR", "MOT"}:
        return "nema.loads.motor_3ph", props
    return (base_lib or "iec.contactors.ac3_9a"), props


def _ensure_asbuilt_sheet(session: Any, store: Reconstruction):
    for sheet in session.project.sheets:
        if sheet.id == "asbuilt" and sheet.kind == "schematic":
            store.schematic_sheet = "asbuilt"
            return sheet
    sheet = _ensure_schematic(session, store)
    if sheet.id == "main" and not sheet.components and not sheet.wires:
        sheet.id = "asbuilt"
        sheet.title = "As-built schematic"
        store.schematic_sheet = "asbuilt"
    return sheet


def _ensure_power_schematic(session: Any, store: Reconstruction):
    for sheet in session.project.sheets:
        if sheet.id in {"asbuilt_power", "asbuilt-power"} and sheet.kind == "schematic":
            sheet.id = "asbuilt_power"
            sheet.drawing_style = "freeform"
            sheet.rail_hot = ""
            sheet.rail_return = ""
            return sheet
    sheet = session.add_sheet("asbuilt_power", title="As-built power schematic", kind="schematic", make_active=False)
    sheet.id = "asbuilt_power"
    sheet.drawing_style = "freeform"
    sheet.rail_hot = ""
    sheet.rail_return = ""
    return sheet


def _ensure_named_pin_and_override(
    session: Any,
    comp: Any,
    pin_id: str,
    *,
    is_power: bool = False,
    pin_role: str = "right",
) -> None:
    from panelcad.core.models import PinOverride

    device = session.catalog.get(comp.library_id)
    try:
        device.pin(pin_id)
        return
    except KeyError:
        pass
    raw = str((comp.properties or {}).get("reconstruct_named_pins") or "")
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if pin_id not in names:
        names.append(pin_id)
    comp.properties["reconstruct_named_pins"] = ",".join(names)

    width = float(getattr(device, "schematic_width", 0) or 28.0)
    height = float(getattr(device, "schematic_height", 0) or 20.0)
    pin_u = pin_id.upper()
    if is_power:
        pole_dx = {
            "L1": 8.0,
            "1": 8.0,
            "U": 8.0,
            "R": 8.0,
            "T1": 8.0,
            "2": 8.0,
            "L2": 18.0,
            "3": 18.0,
            "V": 18.0,
            "S": 18.0,
            "T2": 18.0,
            "4": 18.0,
            "L3": 28.0,
            "5": 28.0,
            "W": 28.0,
            "T": 28.0,
            "T3": 28.0,
            "6": 28.0,
        }
        if "contactors.ac3" in str(comp.library_id or ""):
            pole_dx = {k: v + 2.0 for k, v in pole_dx.items()}
        dx = pole_dx.get(pin_u, 18.0)
        is_bottom = pin_role == "bottom" or pin_u in {"T1", "T2", "T3", "2", "4", "6"}
        if "loads.motor" in str(comp.library_id or ""):
            is_bottom = False
        dy = height if is_bottom else 0.0
        side = "bottom" if is_bottom else "top"
    else:
        py = height / 2.0
        if device.pins:
            py = float(device.pins[0].y)
        is_left = pin_role == "left" or pin_u in {"1", "3", "11", "13", "21", "95", "A1", "X1", "A"}
        dx = 0.0 if is_left else width
        dy = py
        side = "left" if is_left else "right"

    overrides = dict(comp.pin_overrides or {})
    overrides[pin_id] = PinOverride(x=dx, y=dy, side=side)
    comp.pin_overrides = overrides


def record_wire_landing(
    session: Any,
    wire_number: str = "",
    *,
    number: str = "",
    from_tag: str = "",
    from_pin: str = "",
    to_tag: str = "",
    to_pin: str = "",
    from_ref: str = "",
    to_ref: str = "",
    confirmed: bool = True,
    inferred: bool | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Record a traced From-To wire landing and synthesize a clean schematic."""

    from panelcad.library.panel import MEASURED_ENVELOPE_LIBRARY_ID

    store = ensure_reconstruction(session.project)
    frm = _normalize_landing_end(from_tag, from_pin, from_ref)
    to = _normalize_landing_end(to_tag, to_pin, to_ref)
    wire_no = str(wire_number or number or "").strip()
    is_inferred = bool(inferred) if inferred is not None else (not confirmed)
    is_power = _is_power_wire_landing(session, store, frm, to)

    asbuilt = _ensure_asbuilt_sheet(session, store)
    target_sheet = _ensure_power_schematic(session, store) if is_power else asbuilt
    if not is_power and (frm.upper() in _LADDER_RAILS or to.upper() in _LADDER_RAILS):
        target_sheet.drawing_style = "ladder"
        target_sheet.rail_hot = target_sheet.rail_hot or "L1"
        target_sheet.rail_return = target_sheet.rail_return or "L2"

    for idx, endpoint in enumerate((frm, to)):
        if endpoint.upper() in _LADDER_RAILS:
            target_sheet.drawing_style = "ladder"
            target_sheet.rail_hot = target_sheet.rail_hot or "L1"
            target_sheet.rail_return = target_sheet.rail_return or "L2"
            continue
        ref, pin = _split_endpoint(endpoint)
        try:
            comp = target_sheet.component(ref)
        except KeyError:
            if is_power:
                lib_id, props = _power_library_for_ref(session, store, ref)
            else:
                lib_id, props = _control_library_for_ref(session, store, ref, {pin})
            comp = session.place(
                lib_id,
                ref=ref,
                x=92.0,
                y=48.0,
                sheet_id=target_sheet.id,
                properties=props,
                snap=False,
                from_reconstruct_confirm=lib_id == MEASURED_ENVELOPE_LIBRARY_ID,
            )
        role = ("bottom" if idx == 0 else "top") if is_power else ("right" if idx == 0 else "left")
        _ensure_named_pin_and_override(session, comp, pin, is_power=is_power, pin_role=role)

    existing_wire = None
    for candidate in target_sheet.wires:
        if candidate.frm == frm and candidate.to == to and (not wire_no or str(candidate.number or "") == wire_no):
            existing_wire = candidate
            break
    if existing_wire is None:
        wire = session.wire(frm, to, sheet_id=target_sheet.id, number=wire_no or None)
    else:
        wire = existing_wire
        if wire_no:
            wire.number = wire_no
    wire.inferred = is_inferred
    if note:
        wire.properties = dict(wire.properties or {})
        wire.properties["note"] = str(note)

    synthesize_reconstruct_schematic(session)
    rebuild_checklist(store)
    for step in store.checklist:
        if step.kind == "trace_wire" and step.status == "open":
            if (wire_no and step.wire_number == wire_no) or (
                step.from_ref and step.from_ref.replace(":", ".").upper() == frm.upper()
            ):
                step.to_ref = to
                step.wire_id = wire.id
                step.status = "confirmed" if not is_inferred else "answered"
                step.confirmed = not is_inferred
                step.inferred = is_inferred
    return {
        "wire_id": wire.id,
        "number": str(wire.number or ""),
        "from_ref": wire.frm,
        "to_ref": wire.to,
        "sheet": target_sheet.id,
        "inferred": bool(wire.inferred),
        "confirmed": not bool(wire.inferred),
    }


def _primary_pin_y(session: Any, comp: Any) -> float:
    device = session.catalog.get(comp.library_id)
    if device.pins:
        return float(device.pins[0].y)
    return float(getattr(device, "schematic_height", 0) or 20.0) / 2.0


def _layout_control_ladder_sheet(session: Any, store: Reconstruction, sheet: Any) -> int:
    from panelcad.core.models import Point
    from panelcad.library.panel import MEASURED_ENVELOPE_LIBRARY_ID

    pins_by_ref: dict[str, set[str]] = {}
    pin_roles_by_ref: dict[str, dict[str, str]] = {}
    has_rail_ref = False
    for wire in sheet.wires:
        for idx, endpoint in enumerate((wire.frm, wire.to)):
            if endpoint.upper() in _LADDER_RAILS:
                has_rail_ref = True
                continue
            if "." not in endpoint:
                continue
            try:
                ref, pin = _split_endpoint(endpoint)
            except ValueError:
                continue
            pins_by_ref.setdefault(ref, set()).add(pin)
            role = "right" if idx == 0 else "left"
            pin_roles_by_ref.setdefault(ref, {}).setdefault(pin, role)

    for comp in list(sheet.components):
        used_pins = pins_by_ref.get(comp.id, set())
        if not used_pins:
            continue
        new_lib, new_props = _control_library_for_ref(
            session,
            store,
            comp.id,
            used_pins,
            current_lib=str(comp.library_id or ""),
        )
        if new_lib != comp.library_id and comp.library_id != MEASURED_ENVELOPE_LIBRARY_ID:
            try:
                session.catalog.get(new_lib)
                comp.library_id = new_lib
                comp.pin_overrides = {}
            except KeyError:
                pass
        merged = dict(comp.properties or {})
        for k, v in new_props.items():
            merged.setdefault(k, v)
        comp.properties = merged
        for pin, role in pin_roles_by_ref.get(comp.id, {}).items():
            _ensure_named_pin_and_override(session, comp, pin, is_power=False, pin_role=role)

    if has_rail_ref or sheet.wires or sheet.components:
        from panelcad.convert.layout_convert import is_power_sheet

        if not is_power_sheet(sheet):
            sheet.drawing_style = "ladder"
            sheet.rail_hot = sheet.rail_hot or "L1"
            sheet.rail_return = sheet.rail_return or "L2"

    comp_map = {comp.id: comp for comp in sheet.components}
    if not comp_map:
        return 0

    adj: dict[str, list[str]] = {cid: [] for cid in comp_map}
    hot_connected: set[str] = set()
    return_connected: set[str] = set()
    for wire in sheet.wires:
        u_raw, v_raw = wire.frm, wire.to
        u_ref = u_raw.upper() if u_raw.upper() in _LADDER_RAILS else (_split_endpoint(u_raw)[0] if "." in u_raw else "")
        v_ref = v_raw.upper() if v_raw.upper() in _LADDER_RAILS else (_split_endpoint(v_raw)[0] if "." in v_raw else "")
        if u_ref == "L1" and v_ref in adj:
            hot_connected.add(v_ref)
        elif v_ref == "L1" and u_ref in adj:
            hot_connected.add(u_ref)
        if u_ref == "L2" and v_ref in adj:
            return_connected.add(v_ref)
        elif v_ref == "L2" and u_ref in adj:
            return_connected.add(u_ref)
        if u_ref in adj and v_ref in adj and u_ref != v_ref:
            if v_ref not in adj[u_ref]:
                adj[u_ref].append(v_ref)
            if u_ref not in adj[v_ref]:
                adj[v_ref].append(u_ref)

    visited: set[str] = set()
    rung_groups: list[list[str]] = []
    for cid in comp_map:
        if cid in visited:
            continue
        queue = [cid]
        visited.add(cid)
        group: list[str] = []
        while queue:
            cur = queue.pop(0)
            group.append(cur)
            for nb in adj.get(cur, []):
                if nb not in visited:
                    visited.add(nb)
                    queue.append(nb)
        rung_groups.append(group)

    rung_y = 48.0
    comp_rung_y: dict[str, float] = {}
    parallel_meta: dict[str, tuple[float, float, float]] = {}

    for group in rung_groups:
        return_contacts: list[str] = []
        loads: list[str] = []
        contacts: list[str] = []

        for cid in group:
            comp = comp_map[cid]
            lib = str(comp.library_id or "").lower()
            prefix = _tag_prefix_letters(cid)
            if lib.endswith(".ol_contact") or prefix == "OL":
                return_contacts.append(cid)
            elif (
                lib.endswith((".coil", ".pilot_light"))
                or comp.library_id == MEASURED_ENVELOPE_LIBRARY_ID
                or prefix in {"PL", "LT", "H"}
                or (prefix in {"M", "K", "CR"} and "-" not in cid and not lib.endswith((".aux_no", ".aux_nc")))
            ):
                loads.append(cid)
            else:
                contacts.append(cid)

        if not loads and len(contacts) >= 2 and not return_contacts:
            loads.append(contacts.pop(-1))

        series_contacts: list[str] = []
        parallel_contacts: list[str] = []
        for cid in contacts:
            comp = comp_map[cid]
            lib = str(comp.library_id or "").lower()
            is_aux_branch = (
                ("-" in cid or lib.endswith((".aux_no", ".aux_nc")))
                and cid not in hot_connected
                and len(contacts) >= 2
            )
            if is_aux_branch:
                parallel_contacts.append(cid)
            else:
                series_contacts.append(cid)

        series_contacts.sort(key=lambda cid: (0 if cid in hot_connected else 1, group.index(cid)))

        for idx, cid in enumerate(series_contacts):
            comp = comp_map[cid]
            py = _primary_pin_y(session, comp)
            comp.x = 92.0 + idx * 56.0
            comp.y = rung_y - py
            comp_rung_y[cid] = rung_y

        anchor_x = comp_map[series_contacts[-1]].x if series_contacts else 148.0
        for p_idx, cid in enumerate(parallel_contacts):
            comp = comp_map[cid]
            py = _primary_pin_y(session, comp)
            sub_y = rung_y + 20.0 * (p_idx + 1)
            comp.x = anchor_x
            comp.y = sub_y - py
            comp_rung_y[cid] = sub_y
            parallel_meta[cid] = (anchor_x, sub_y, rung_y)

        for l_idx, cid in enumerate(loads):
            comp = comp_map[cid]
            py = _primary_pin_y(session, comp)
            row_y = rung_y + 24.0 * l_idx
            comp.x = 310.0
            comp.y = row_y - py
            comp_rung_y[cid] = row_y

        for r_idx, cid in enumerate(return_contacts):
            comp = comp_map[cid]
            py = _primary_pin_y(session, comp)
            comp.x = 354.0 + r_idx * 30.0
            comp.y = rung_y - py
            comp_rung_y[cid] = rung_y

        extra_rows = max(len(parallel_contacts), max(len(loads) - 1, 0))
        rung_y += 48.0 + 20.0 * extra_rows

    for wire in sheet.wires:
        u_raw, v_raw = wire.frm, wire.to
        u_ref = "" if u_raw.upper() in _LADDER_RAILS else (_split_endpoint(u_raw)[0] if "." in u_raw else "")
        u_pin = "" if u_raw.upper() in _LADDER_RAILS else (_split_endpoint(u_raw)[1] if "." in u_raw else "")
        v_ref = "" if v_raw.upper() in _LADDER_RAILS else (_split_endpoint(v_raw)[0] if "." in v_raw else "")
        v_pin = "" if v_raw.upper() in _LADDER_RAILS else (_split_endpoint(v_raw)[1] if "." in v_raw else "")

        if u_ref in parallel_meta and v_ref not in parallel_meta:
            bx, sub_y, main_y = parallel_meta[u_ref]
            comp = comp_map[u_ref]
            dev = session.catalog.get(comp.library_id)
            w_sym = float(getattr(dev, "schematic_width", 0) or 28.0)
            is_left = u_pin.upper() in {"1", "3", "11", "13", "21", "A1", "X1", "A"}
            tap_x = (bx - 8.0) if is_left else (bx + w_sym + 8.0)
            wire.points = [Point(x=tap_x, y=sub_y), Point(x=tap_x, y=main_y)]
        elif v_ref in parallel_meta and u_ref not in parallel_meta:
            bx, sub_y, main_y = parallel_meta[v_ref]
            comp = comp_map[v_ref]
            dev = session.catalog.get(comp.library_id)
            w_sym = float(getattr(dev, "schematic_width", 0) or 28.0)
            is_left = v_pin.upper() in {"1", "3", "11", "13", "21", "A1", "X1", "A"}
            tap_x = (bx - 8.0) if is_left else (bx + w_sym + 8.0)
            wire.points = [Point(x=tap_x, y=main_y), Point(x=tap_x, y=sub_y)]
        else:
            wire.points = []

    return len(rung_groups)


def _layout_power_feeder_sheet(session: Any, store: Reconstruction, sheet: Any) -> None:
    sheet.drawing_style = "freeform"
    sheet.rail_hot = ""
    sheet.rail_return = ""

    pin_roles_by_ref: dict[str, dict[str, str]] = {}
    for wire in sheet.wires:
        for idx, endpoint in enumerate((wire.frm, wire.to)):
            if "." not in endpoint:
                continue
            try:
                ref, pin = _split_endpoint(endpoint)
            except ValueError:
                continue
            role = "bottom" if idx == 0 else "top"
            pin_roles_by_ref.setdefault(ref, {})[pin] = role

    rank_y = {0: 22.0, 1: 79.0, 2: 151.0, 3: 203.0}
    rank_counts = {0: 0, 1: 0, 2: 0, 3: 0}

    def _power_rank(comp: Any) -> int:
        lib = str(comp.library_id or "").lower()
        prefix = _tag_prefix_letters(comp.id)
        if "breaker" in lib or "fuse" in lib or "disconnect" in lib or prefix in {"CB", "Q", "FU", "F"}:
            return 0
        if "contactor" in lib or prefix in {"M", "K"}:
            return 1
        if "overload" in lib or prefix in {"OL"}:
            return 2
        return 3

    for comp in sorted(sheet.components, key=_power_rank):
        new_lib, new_props = _power_library_for_ref(session, store, comp.id, current_lib=str(comp.library_id or ""))
        if new_lib != comp.library_id:
            try:
                session.catalog.get(new_lib)
                comp.library_id = new_lib
                comp.pin_overrides = {}
            except KeyError:
                pass
        merged = dict(comp.properties or {})
        for k, v in new_props.items():
            merged.setdefault(k, v)
        comp.properties = merged

        rank = _power_rank(comp)
        col = rank_counts.get(rank, 0)
        rank_counts[rank] = col + 1
        col_x = 60.0 + col * 90.0
        comp.x = (col_x - 2.0) if "contactors.ac3" in str(comp.library_id or "") else col_x
        comp.y = rank_y.get(rank, 203.0)
        for pin, role in pin_roles_by_ref.get(comp.id, {}).items():
            _ensure_named_pin_and_override(session, comp, pin, is_power=True, pin_role=role)

    for wire in sheet.wires:
        wire.points = []


def synthesize_reconstruct_schematic(session: Any, sheet_id: str | None = None) -> dict[str, Any]:
    """Organize the reconstruct schematic into a collision-free NEMA ladder layout."""

    from panelcad.library.panel import MEASURED_ENVELOPE_LIBRARY_ID

    store = ensure_reconstruction(session.project)
    asbuilt = session.project.sheet(sheet_id) if sheet_id else _ensure_schematic(session, store)
    power_sheet = None
    for s in session.project.sheets:
        if s.id in {"asbuilt_power", "asbuilt-power"} and s.kind == "schematic":
            s.id = "asbuilt_power"
            power_sheet = s
            break

    all_wires: list[Any] = list(asbuilt.wires)
    if power_sheet is not None and power_sheet is not asbuilt:
        for w in power_sheet.wires:
            if all(existing.id != w.id for existing in all_wires):
                all_wires.append(w)

    power_wires: list[Any] = []
    control_wires: list[Any] = []
    for w in all_wires:
        if _is_power_wire_landing(session, store, w.frm, w.to):
            power_wires.append(w)
        else:
            control_wires.append(w)

    if power_wires and (control_wires or asbuilt.components or power_sheet is not None):
        if power_sheet is None:
            power_sheet = _ensure_power_schematic(session, store)
        asbuilt.wires = control_wires
        power_sheet.wires = power_wires
    elif power_wires and not control_wires:
        asbuilt.wires = power_wires
        _layout_power_feeder_sheet(session, store, asbuilt)
        return {
            "schematic_sheet": asbuilt.id,
            "power_sheet": asbuilt.id,
            "rungs": 0,
            "wires": len(asbuilt.wires),
        }

    control_refs: set[str] = set()
    for w in asbuilt.wires:
        for ep in (w.frm, w.to):
            if ep.upper() not in _LADDER_RAILS and "." in ep:
                try:
                    r, _p = _split_endpoint(ep)
                    control_refs.add(r)
                except ValueError:
                    pass

    power_refs: set[str] = set()
    if power_sheet is not None:
        for w in power_sheet.wires:
            for ep in (w.frm, w.to):
                if "." in ep:
                    try:
                        r, p = _split_endpoint(ep)
                        power_refs.add(r)
                        try:
                            power_sheet.component(r)
                        except KeyError:
                            lib_id, props = _power_library_for_ref(session, store, r)
                            session.place(
                                lib_id,
                                ref=r,
                                x=60.0,
                                y=22.0,
                                sheet_id=power_sheet.id,
                                properties=props,
                                snap=False,
                                from_reconstruct_confirm=lib_id == MEASURED_ENVELOPE_LIBRARY_ID,
                            )
                    except ValueError:
                        pass
        _layout_power_feeder_sheet(session, store, power_sheet)

    if power_refs and control_refs:
        asbuilt.components = [
            comp for comp in asbuilt.components if comp.id in control_refs or comp.id not in power_refs
        ]

    for w in asbuilt.wires:
        for idx, ep in enumerate((w.frm, w.to)):
            if ep.upper() in _LADDER_RAILS or "." not in ep:
                continue
            ref, pin = _split_endpoint(ep)
            try:
                asbuilt.component(ref)
            except KeyError:
                lib_id, props = _control_library_for_ref(session, store, ref, {pin})
                session.place(
                    lib_id,
                    ref=ref,
                    x=92.0,
                    y=48.0,
                    sheet_id=asbuilt.id,
                    properties=props,
                    snap=False,
                    from_reconstruct_confirm=lib_id == MEASURED_ENVELOPE_LIBRARY_ID,
                )

    rungs = _layout_control_ladder_sheet(session, store, asbuilt)
    try:
        session.update_cross_references()
    except Exception:  # noqa: BLE001
        pass

    total_wires = len(asbuilt.wires) + (len(power_sheet.wires) if power_sheet is not None else 0)
    return {
        "schematic_sheet": asbuilt.id,
        "power_sheet": power_sheet.id if power_sheet is not None else "",
        "rungs": rungs,
        "wires": total_wires,
    }


def apply_to_print(session: Any, sheet_id: str | None = None) -> dict[str, Any]:
    """Place confirmed parts, hold needs-info parts, and synthesize the clean ladder schematic."""

    from panelcad.reconstruct.placeholder import project_has_needs_info, reconstruct_mode

    confirmed = place_confirmed(session, sheet_id=sheet_id)
    if reconstruct_mode(session):
        held = place_needs_info(session)
    else:
        held = {"placed": [], "skipped": []}
    _ensure_asbuilt_sheet(session, ensure_reconstruction(session.project))
    synth = synthesize_reconstruct_schematic(session)
    return {
        "placed": confirmed["placed"],
        "needs_info": held["placed"],
        "skipped": list(confirmed["skipped"]) + list(held["skipped"]),
        "schematic_sheet": synth["schematic_sheet"],
        "power_sheet": synth.get("power_sheet", ""),
        "rungs": synth["rungs"],
        "wires": synth["wires"],
        "not_for_build": project_has_needs_info(session.project),
    }
