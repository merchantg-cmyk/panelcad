"""Photo reconstruction methods for :class:`panelcad.api.session.Session`."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class SessionReconstructMixin:
    """Photo-set, scaling, identification, feedback, and synthesis methods."""

    def reconstruct_add_photos(
        self,
        paths: list[Path | str] | Path | str,
        role: str = "other",
        note: str = "",
    ) -> dict[str, Any]:
        from panelcad.reconstruct.service import add_photos

        items = paths if isinstance(paths, (list, tuple)) else [paths]
        return add_photos(self, list(items), role=role, note=note)

    def reconstruct_tag_photo(self, photo_id: str, role: str, note: str | None = None) -> dict[str, Any]:
        from panelcad.reconstruct.service import tag_photo

        return tag_photo(self, photo_id, role, note)

    def reconstruct_list_photos(self) -> dict[str, Any]:
        from panelcad.reconstruct.service import status

        return {"photos": status(self)["photos"]}

    def reconstruct_set_corners(self, photo_id: str, corners: list[Any]) -> dict[str, Any]:
        from panelcad.reconstruct.service import set_corners

        return set_corners(self, photo_id, corners)

    def reconstruct_set_scale(
        self,
        photo_id: str,
        kind: str,
        p1: Any,
        p2: Any,
        *,
        known_mm: float | None = None,
        catalog: str = "",
        manufacturer: str = "",
        dimension: str = "width",
        note: str = "",
        known_height_mm: float | None = None,
        face_width_mm: float | None = None,
        face_height_mm: float | None = None,
        backpanel: bool = False,
        slot: int | str | None = None,
        replace: str | None = None,
    ) -> dict[str, Any]:
        from panelcad.reconstruct.service import set_scale

        return set_scale(
            self,
            photo_id,
            kind,
            p1,
            p2,
            known_mm=known_mm,
            catalog=catalog,
            manufacturer=manufacturer,
            dimension=dimension,
            note=note,
            known_height_mm=known_height_mm,
            face_width_mm=face_width_mm,
            face_height_mm=face_height_mm,
            backpanel=backpanel,
            slot=slot,
            replace=replace,
        )

    def reconstruct_clear_scale(self, photo_id: str) -> dict[str, Any]:
        from panelcad.reconstruct.service import clear_scale

        return clear_scale(self, photo_id)

    def reconstruct_identify(
        self,
        photo_id: str | None = None,
        *,
        vision: Any = None,
        recordings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from panelcad.reconstruct.service import identify

        return identify(self, photo_id, vision=vision, recordings=recordings)

    def reconstruct_library_choices(self, query: str = "", *, limit: int = 8) -> list[dict[str, Any]]:
        """Published parts for the missing-info editor. Does not change the project."""

        from panelcad.reconstruct.feedback import library_choices

        return library_choices(self, query, limit=limit)

    def reconstruct_apply_missing_info(
        self,
        item_id: str,
        field: str,
        *,
        value: str = "",
        catalog: str = "",
        manufacturer: str = "",
        library_id: str = "",
        width_mm: float | None = None,
        height_mm: float | None = None,
        depth_mm: float | None = None,
        mount: str = "",
        terminal: str = "",
        wire_number: str = "",
        to_ref: str = "",
    ) -> dict[str, Any]:
        """Save one missing-info badge onto the project. Refuses placeholder text."""

        from panelcad.reconstruct.feedback import apply_missing_info

        return apply_missing_info(
            self,
            item_id,
            field,
            value=value,
            catalog=catalog,
            manufacturer=manufacturer,
            library_id=library_id,
            width_mm=width_mm,
            height_mm=height_mm,
            depth_mm=depth_mm,
            mount=mount,
            terminal=terminal,
            wire_number=wire_number,
            to_ref=to_ref,
        )

    def reconstruct_confirm_item(
        self,
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
        from panelcad.reconstruct.service import confirm_item

        return confirm_item(
            self,
            item_id,
            catalog=catalog,
            manufacturer=manufacturer,
            envelope_w_mm=envelope_w_mm,
            envelope_h_mm=envelope_h_mm,
            envelope_d_mm=envelope_d_mm,
            tag=tag,
            library_id=library_id,
            mount=mount,
        )

    def reconstruct_place_needs_info(self, photo_id: str | None = None) -> dict[str, Any]:
        from panelcad.reconstruct.service import place_needs_info

        return place_needs_info(self, photo_id)

    def reconstruct_place_confirmed(self, sheet_id: str | None = None) -> dict[str, Any]:
        from panelcad.reconstruct.service import place_confirmed

        return place_confirmed(self, sheet_id)

    def reconstruct_checklist(self) -> dict[str, Any]:
        from panelcad.reconstruct.checklist import rebuild_checklist
        from panelcad.reconstruct.models import ensure_reconstruction
        from panelcad.reconstruct.service import status

        rebuild_checklist(ensure_reconstruction(self.project))
        return {"checklist": status(self)["checklist"]}

    def reconstruct_next_question(self) -> dict[str, Any]:
        from panelcad.reconstruct.service import next_question

        return next_question(self)

    def reconstruct_answer(
        self,
        step_id: str,
        text: str = "",
        *,
        photo: Path | str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        from panelcad.reconstruct.service import answer

        return answer(self, step_id, text, photo=photo, confirm=confirm)

    def reconstruct_confirm_connection(
        self,
        step_id: str | None = None,
        wire_id: str | None = None,
    ) -> dict[str, Any]:
        from panelcad.reconstruct.service import confirm_connection

        return confirm_connection(self, step_id=step_id, wire_id=wire_id)

    def reconstruct_attach_print(self, path: Path | str, note: str = "") -> dict[str, Any]:
        from panelcad.reconstruct.service import attach_print

        return attach_print(self, path, note=note)

    def reconstruct_compare_print(self, path: Path | str | None = None) -> dict[str, Any]:
        from panelcad.reconstruct.service import compare_print

        return compare_print(self, path)

    def reconstruct_photo_region(
        self,
        item_id: str | None = None,
        ref: str | None = None,
        photo_id: str | None = None,
    ) -> dict[str, Any]:
        from panelcad.reconstruct.service import photo_region

        return photo_region(self, item_id=item_id, ref=ref, photo_id=photo_id)

    def reconstruct_unconfirmed(self) -> dict[str, Any]:
        from panelcad.reconstruct.service import list_unconfirmed

        return list_unconfirmed(self)

    def reconstruct_status(self) -> dict[str, Any]:
        from panelcad.reconstruct.service import status

        return status(self)

    def reconstruct_record_wire_landing(
        self,
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
        from panelcad.reconstruct.service import record_wire_landing

        return record_wire_landing(
            self,
            wire_number=wire_number,
            number=number,
            from_tag=from_tag,
            from_pin=from_pin,
            to_tag=to_tag,
            to_pin=to_pin,
            from_ref=from_ref,
            to_ref=to_ref,
            confirmed=confirmed,
            inferred=inferred,
            note=note,
        )

    def reconstruct_synthesize_schematic(self, sheet_id: str | None = None) -> dict[str, Any]:
        from panelcad.reconstruct.service import synthesize_reconstruct_schematic

        return synthesize_reconstruct_schematic(self, sheet_id=sheet_id)

    def reconstruct_apply_to_print(self, sheet_id: str | None = None) -> dict[str, Any]:
        from panelcad.reconstruct.service import apply_to_print

        return apply_to_print(self, sheet_id=sheet_id)

    def reconstruct_photos_to_drawings(
        self,
        *,
        vision: Any = None,
        recordings: dict[str, Any] | None = None,
        unboxed_callouts: Any = None,
        extra_flags: list[dict[str, Any]] | None = None,
        accepted_ids: list[str] | None = None,
        dismissed_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run imported photos through to drawings, stopping only for flagged items."""

        from panelcad.reconstruct.guided import run_photos_to_drawings

        return run_photos_to_drawings(
            self,
            vision=vision,
            recordings=recordings,
            unboxed_callouts=unboxed_callouts,
            extra_flags=extra_flags,
            accepted_ids=accepted_ids,
            dismissed_ids=dismissed_ids,
        )

    def reconstruct_photo_feedback(
        self,
        photo_id: str | None = None,
        *,
        filter_mode: str = "all",
        include_terminals: bool = False,
        unboxed_callouts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        from panelcad.reconstruct.feedback import build_photo_feedback_report

        return build_photo_feedback_report(
            self,
            photo_id=photo_id,
            filter_mode=filter_mode,  # type: ignore[arg-type]
            include_terminals=include_terminals,
            unboxed_callouts=unboxed_callouts,
        )

    def reconstruct_site_exit_check(
        self,
        photo_id: str | None = None,
        *,
        unboxed_callouts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Capture-phase check: may the tech close the cabinet and leave site?"""
        from panelcad.reconstruct.feedback import capture_site_exit_readiness

        report = capture_site_exit_readiness(self, photo_id=photo_id, unboxed_callouts=unboxed_callouts)
        keys = (
            "photo_id",
            "safe_to_leave_site",
            "site_exit_status",
            "site_exit_banner",
            "on_site_shot_list",
            "total_detected",
            "confirmed_count",
            "missing_info_count",
            "closeup_needed_count",
            "unboxed_count",
        )
        return {k: report[k] for k in keys}

    def reconstruct_link_closeup(
        self,
        item_id: str,
        *,
        closeup_photo_path: Path | str | None = None,
        closeup_photo_id: str = "",
        closeup_role: str = "nameplate",
        tag: str = "",
        part_number: str = "",
        brand: str = "",
        envelope_w_mm: float | None = None,
        envelope_h_mm: float | None = None,
        envelope_d_mm: float | None = None,
        mount: str = "",
        library_id: str = "",
    ) -> dict[str, Any]:
        from panelcad.reconstruct.feedback import link_closeup_to_device

        return link_closeup_to_device(
            self,
            item_id,
            closeup_photo_path=closeup_photo_path,
            closeup_photo_id=closeup_photo_id,
            closeup_role=closeup_role,
            tag=tag,
            part_number=part_number,
            brand=brand,
            envelope_w_mm=envelope_w_mm,
            envelope_h_mm=envelope_h_mm,
            envelope_d_mm=envelope_d_mm,
            mount=mount,
            library_id=library_id,
        )

    def reconstruct_add_missed_device(
        self,
        photo_id: str,
        region: dict[str, Any] | tuple[float, float, float, float] | list[float],
        *,
        tag: str = "",
        part_number: str = "",
        brand: str = "",
        label_text: str = "",
        envelope_w_mm: float | None = None,
        envelope_h_mm: float | None = None,
        envelope_d_mm: float | None = None,
        mount: str = "",
        library_id: str = "",
        note: str = "",
        closeup_photo_id: str = "",
        snap_to_edges: bool = False,
    ) -> dict[str, Any]:
        from panelcad.reconstruct.feedback import add_missed_device_on_photo

        return add_missed_device_on_photo(
            self,
            photo_id=photo_id,
            region=region,
            tag=tag,
            part_number=part_number,
            brand=brand,
            label_text=label_text,
            envelope_w_mm=envelope_w_mm,
            envelope_h_mm=envelope_h_mm,
            envelope_d_mm=envelope_d_mm,
            mount=mount,
            library_id=library_id,
            note=note,
            closeup_photo_id=closeup_photo_id,
            snap_to_edges=snap_to_edges,
        )

    def reconstruct_add_missed_strip(
        self,
        photo_id: str,
        region: dict[str, Any] | tuple[float, float, float, float] | list[float],
        *,
        tag: str = "",
        snap_to_edges: bool = False,
        block_count: int | None = None,
    ) -> dict[str, Any]:
        from panelcad.reconstruct.feedback import add_missed_strip_on_photo

        return add_missed_strip_on_photo(
            self,
            photo_id,
            region,
            tag=tag,
            snap_to_edges=snap_to_edges,
            block_count=block_count,
        )

    def reconstruct_accept_terminal_strip(
        self,
        item_id: str,
        *,
        tag: str = "",
        block_count: int | None = None,
    ) -> dict[str, Any]:
        """Accept a suggested terminal strip. Does not require width, height, or depth."""

        from panelcad.reconstruct.feedback import accept_terminal_strip_on_photo

        return accept_terminal_strip_on_photo(self, item_id, tag=tag, block_count=block_count)

    def reconstruct_update_device_box(
        self,
        item_id: str,
        *,
        box: dict[str, Any] | tuple[float, float, float, float] | list[float] | None = None,
        snap_to_edges: bool = False,
        search_margin_ratio: float = 0.12,
    ) -> dict[str, Any]:
        from panelcad.reconstruct.feedback import update_device_box_on_photo

        return update_device_box_on_photo(
            self,
            item_id,
            box=box,
            snap_to_edges=snap_to_edges,
            search_margin_ratio=search_margin_ratio,
        )

    def reconstruct_refine_device_box(
        self,
        photo_id: str,
        box: dict[str, Any] | tuple[float, float, float, float] | list[float],
        *,
        search_margin_ratio: float = 0.12,
    ) -> dict[str, float]:
        from panelcad.reconstruct.feedback import refine_device_box_on_photo
        from panelcad.reconstruct.models import ensure_reconstruction
        from panelcad.reconstruct.store import resolve_path

        store = ensure_reconstruction(self.project)
        photo = store.photo(photo_id)
        resolved = resolve_path(photo.path, getattr(self, "path", None))
        if not resolved.is_file() and photo.source_path and Path(photo.source_path).is_file():
            resolved = Path(photo.source_path)
        rx, ry, rw, rh = refine_device_box_on_photo(resolved, box, search_margin_ratio=search_margin_ratio)
        return {"x": float(rx), "y": float(ry), "w": float(rw), "h": float(rh)}

    def reconstruct_export_photo_feedback(
        self,
        output_path: Path | str,
        photo_id: str | None = None,
        *,
        filter_mode: str = "all",
        selected_item_id: str | None = None,
        unboxed_callouts: list[dict[str, Any]] | None = None,
        include_banner: bool = True,
    ) -> dict[str, Any]:
        from panelcad.reconstruct.feedback import export_photo_feedback

        return export_photo_feedback(
            self,
            output_path=output_path,
            photo_id=photo_id,
            filter_mode=filter_mode,  # type: ignore[arg-type]
            selected_item_id=selected_item_id,
            unboxed_callouts=unboxed_callouts,
            include_banner=include_banner,
        )
