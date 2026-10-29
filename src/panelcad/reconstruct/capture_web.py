"""Mobile Web Field Capture Server for PanelCAD Reconstruct.

Provides a zero-external-dependency HTTP/PWA server (`ThreadingHTTPServer`) so
field technicians on Android / iOS phones can:
- Snap or upload real cabinet photos (`<input type="file" accept="image/*" capture="environment">`)
- Immediately check Capture-Phase Site-Exit Readiness (`⚠ DO NOT LEAVE SITE YET` vs `✓ SITE CAPTURE READY`)
- Inspect the annotated cabinet photo with green (confirmed) and red/amber (missing-info) bounding boxes
- Finger-drag a bounding box right on the phone screen over any device the bot/software missed (`reconstruct_add_missed_device`)
- Tap any flagged device in the photo or in the "Before You Leave Site" shot list to snap/attach a close-up nameplate photo (`reconstruct_link_closeup`) and confirm its Tag, Brand, Part #, W×H×D (mm), and Mounting
- Download the `.panelcad` session bundle or send a notification webhook (`PANELCAD_CAPTURE_WEBHOOK_URL`).
"""

from __future__ import annotations

import base64
import datetime
import email.parser
import email.policy
import json
import os
import re
import shutil
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from panelcad.api.session import Session
from panelcad.reconstruct.feedback import (
    build_photo_feedback_report,
    render_annotated_cabinet_photo,
)
from panelcad.reconstruct.models import ensure_reconstruction, public_item, public_photo
from panelcad.reconstruct.store import resolve_path


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def _slugify(value: str, fallback: str = "session") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip()).strip("-").lower()
    return cleaned[:64] if cleaned else fallback


def _sanitize_filename(name: str, fallback: str = "capture.jpg") -> str:
    base = os.path.basename(str(name or "").strip())
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    if not cleaned:
        return fallback
    if "." not in cleaned:
        cleaned += ".jpg"
    return cleaned


def _qimage_to_png_bytes(qimg: Any) -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    qimg.save(buf, "PNG")
    data = bytes(buf.data())
    buf.close()
    return data


def _parse_multipart_form(content_type: str, body: bytes) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Parse multipart/form-data using Python stdlib email parser."""
    fields: dict[str, str] = {}
    files: list[dict[str, Any]] = []
    if not body:
        return fields, files
    header_bytes = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    )
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(header_bytes + body)
    if not msg.is_multipart():
        return fields, files
    for part in msg.iter_parts():
        disp = part.get_content_disposition()
        if disp != "form-data":
            continue
        name = part.get_param("name", header="content-disposition") or "field"
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None:
            files.append(
                {
                    "field": str(name),
                    "filename": _sanitize_filename(filename),
                    "content_type": part.get_content_type() or "application/octet-stream",
                    "data": payload,
                }
            )
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[str(name)] = payload.decode(charset, errors="replace")
    return fields, files


def _opt_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        num = float(val)
        return num if num > 0 else None
    except (TypeError, ValueError):
        return None


class CaptureWorkspace:
    """Thread-safe manager for field capture `.panelcad` sessions on disk."""

    def __init__(
        self,
        workspace_dir: Path | str,
        *,
        webhook_url: str = "",
        webhook_key: str = "",
        access_token: str = "",
        on_change: Any = None,
    ) -> None:
        self.root = Path(workspace_dir).resolve()
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root / "server_config.json"
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._meta_cache: dict[str, dict[str, Any]] = {}
        self._on_change = on_change
        self.access_token = str(access_token or os.environ.get("PANELCAD_CAPTURE_TOKEN") or "").strip()
        self._init_config(webhook_url=webhook_url, webhook_key=webhook_key)
        self._discover_existing()

    def _init_config(self, *, webhook_url: str = "", webhook_key: str = "") -> None:
        cfg = self.get_server_config()
        if webhook_url:
            cfg["webhook_url"] = webhook_url.strip()
        if webhook_key:
            cfg["webhook_key"] = webhook_key.strip()
        self.config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def get_server_config(self) -> dict[str, str]:
        cfg: dict[str, str] = {
            "webhook_url": os.environ.get("PANELCAD_CAPTURE_WEBHOOK_URL", "").strip(),
            "webhook_key": os.environ.get("PANELCAD_CAPTURE_WEBHOOK_KEY", "").strip(),
        }
        if self.config_path.is_file():
            try:
                loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    if loaded.get("webhook_url"):
                        cfg["webhook_url"] = str(loaded["webhook_url"]).strip()
                    if loaded.get("webhook_key"):
                        cfg["webhook_key"] = str(loaded["webhook_key"]).strip()
            except Exception:
                pass
        return cfg

    def update_server_config(self, *, webhook_url: str | None = None, webhook_key: str | None = None) -> dict[str, str]:
        with self._lock:
            cfg = self.get_server_config()
            if webhook_url is not None:
                cfg["webhook_url"] = str(webhook_url).strip()
            if webhook_key is not None:
                cfg["webhook_key"] = str(webhook_key).strip()
            self.config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            return {
                "webhook_url": cfg["webhook_url"],
                "webhook_configured": bool(cfg["webhook_url"]),
                "has_webhook_key": bool(cfg["webhook_key"]),
            }

    def attach_live_session(self, session: Session, *, session_id: str = "desktop-live") -> str:
        """Attach an in-memory Desktop PanelCAD Session so phone uploads sync directly into it."""
        with self._lock:
            sid = _slugify(session_id or getattr(session.project, "name", "") or "desktop-live")
            sdir = self._session_dir(sid)
            sdir.mkdir(parents=True, exist_ok=True)
            if session.path is None:
                session.path = self._project_path(sid)
            self._sessions[sid] = session
            meta = self._read_meta(sid)
            meta.setdefault("session_id", sid)
            meta.setdefault("name", getattr(session.project, "name", "") or "Desktop Live Session")
            meta["updated_at"] = _now_iso()
            self._write_meta(sid, meta)
        self._fire_change(sid)
        return sid

    def _fire_change(self, session_id: str) -> None:
        cb = self._on_change
        if callable(cb):
            try:
                cb(session_id)
            except Exception:
                pass

    def _session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / _slugify(session_id)

    def _project_path(self, session_id: str) -> Path:
        sdir = self._session_dir(session_id)
        return sdir / f"{_slugify(session_id)}.panelcad"

    def _meta_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "capture_meta.json"

    def _discover_existing(self) -> None:
        for child in sorted(self.sessions_dir.iterdir()):
            if not child.is_dir():
                continue
            sid = child.name
            proj = child / f"{sid}.panelcad"
            if not proj.is_file():
                candidates = sorted(child.glob("*.panelcad"))
                if candidates:
                    proj = candidates[0]
            if proj.is_file():
                try:
                    sess = Session.open(proj)
                    self._sessions[sid] = sess
                except Exception:
                    continue

    def _read_meta(self, session_id: str) -> dict[str, Any]:
        mpath = self._meta_path(session_id)
        if mpath.is_file():
            try:
                data = json.loads(mpath.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _write_meta(self, session_id: str, meta: dict[str, Any]) -> None:
        mpath = self._meta_path(session_id)
        mpath.parent.mkdir(parents=True, exist_ok=True)
        mpath.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self._meta_cache[session_id] = meta

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            out: list[dict[str, Any]] = []
            for sid, sess in sorted(self._sessions.items()):
                out.append(self._summarize_session(sid, sess))
            out.sort(key=lambda r: (r.get("updated_at") or "", r.get("session_id") or ""), reverse=True)
            return out

    def _summarize_session(self, session_id: str, sess: Session) -> dict[str, Any]:
        store = ensure_reconstruction(sess.project)
        meta = self._read_meta(session_id)
        unboxed_map = meta.get("unboxed_callouts") or {}
        primary_photos = [
            p for p in store.photos if p.role in {"backpanel", "overview", "door", "other"}
        ] or list(store.photos)
        active_pid = primary_photos[0].id if primary_photos else (store.photos[0].id if store.photos else "")
        safe = False
        status = "needs_analysis"
        banner = "⚠ CAPTURE NOT ANALYZED YET — Add a Cabinet Photo"
        total_detected = 0
        confirmed_count = 0
        missing_count = 0
        if active_pid:
            try:
                rep = build_photo_feedback_report(
                    sess,
                    photo_id=active_pid,
                    unboxed_callouts=unboxed_map.get(active_pid) or [],
                )
                safe = bool(rep.get("safe_to_leave_site"))
                status = str(rep.get("site_exit_status") or "needs_analysis")
                banner = str(rep.get("site_exit_banner") or banner)
                total_detected = int(rep.get("total_detected") or 0)
                confirmed_count = int(rep.get("confirmed_count") or 0)
                missing_count = int(rep.get("missing_info_count") or 0)
            except Exception:
                pass
        return {
            "session_id": session_id,
            "name": getattr(sess.project, "name", "") or meta.get("name") or session_id,
            "site_name": meta.get("site_name") or "",
            "photo_count": len(store.photos),
            "item_count": len([i for i in store.items if i.role == "part"]),
            "active_photo_id": active_pid,
            "safe_to_leave_site": safe,
            "site_exit_status": status,
            "site_exit_banner": banner,
            "total_detected": total_detected,
            "confirmed_count": confirmed_count,
            "missing_info_count": missing_count,
            "updated_at": meta.get("updated_at") or _now_iso(),
            "project_path": str(sess.path or self._project_path(session_id)),
        }

    def create_or_get_session(
        self,
        name: str,
        *,
        session_id: str = "",
        site_name: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            sid = _slugify(session_id or name, fallback="field-capture")
            if sid in self._sessions:
                sess = self._sessions[sid]
                meta = self._read_meta(sid)
                if site_name:
                    meta["site_name"] = site_name
                    meta["updated_at"] = _now_iso()
                    self._write_meta(sid, meta)
                return self._summarize_session(sid, sess)

            sdir = self._session_dir(sid)
            sdir.mkdir(parents=True, exist_ok=True)
            proj_path = self._project_path(sid)
            sess = Session.create(name=name or sid)
            sess.save(proj_path)
            self._sessions[sid] = sess
            meta = {
                "session_id": sid,
                "name": name or sid,
                "site_name": site_name,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "unboxed_callouts": {},
            }
            self._write_meta(sid, meta)
            return self._summarize_session(sid, sess)

    def get_session(self, session_id: str) -> Session:
        with self._lock:
            sid = _slugify(session_id)
            if sid not in self._sessions:
                raise KeyError(f"Unknown capture session: {session_id}")
            return self._sessions[sid]

    def save_session(self, session_id: str) -> None:
        with self._lock:
            sid = _slugify(session_id)
            sess = self.get_session(sid)
            proj_path = sess.path or self._project_path(sid)
            sess.save(proj_path)
            meta = self._read_meta(sid)
            meta["updated_at"] = _now_iso()
            self._write_meta(sid, meta)

    def set_unboxed_callouts(
        self, session_id: str, photo_id: str, callouts: list[dict[str, Any]]
    ) -> None:
        with self._lock:
            sid = _slugify(session_id)
            meta = self._read_meta(sid)
            unboxed = dict(meta.get("unboxed_callouts") or {})
            unboxed[photo_id] = list(callouts)
            meta["unboxed_callouts"] = unboxed
            meta["updated_at"] = _now_iso()
            self._write_meta(sid, meta)

    def get_unboxed_callouts(self, session_id: str, photo_id: str) -> list[dict[str, Any]]:
        with self._lock:
            sid = _slugify(session_id)
            meta = self._read_meta(sid)
            unboxed = meta.get("unboxed_callouts") or {}
            return list(unboxed.get(photo_id) or [])

    def get_status(
        self,
        session_id: str,
        *,
        photo_id: str | None = None,
        filter_mode: str = "all",
    ) -> dict[str, Any]:
        with self._lock:
            sid = _slugify(session_id)
            sess = self.get_session(sid)
            store = ensure_reconstruction(sess.project)
            meta = self._read_meta(sid)
            photos = [public_photo(p) for p in store.photos]
            items = [public_item(i) for i in store.items]

            if not store.photos:
                return {
                    "session": self._summarize_session(sid, sess),
                    "photo_id": "",
                    "safe_to_leave_site": False,
                    "site_exit_status": "needs_analysis",
                    "site_exit_banner": "⚠ NO CABINET PHOTOS YET — Snap or Upload a Cabinet Photo to Begin",
                    "on_site_shot_list": [
                        "Tap '📷 Snap / Upload Cabinet Photo' to capture the open panel before leaving the site."
                    ],
                    "boxes": [],
                    "unboxed_callouts": [],
                    "photos": photos,
                    "items": items,
                }

            chosen_pid = str(photo_id or "").strip()
            if not chosen_pid:
                primary = [
                    p for p in store.photos if p.role in {"backpanel", "overview", "door", "other"}
                ]
                chosen_pid = primary[0].id if primary else store.photos[0].id

            callouts = self.get_unboxed_callouts(sid, chosen_pid)
            mode = filter_mode if filter_mode in {"all", "missing_only", "confirmed_only"} else "all"
            report = build_photo_feedback_report(
                sess,
                photo_id=chosen_pid,
                filter_mode=mode,  # type: ignore[arg-type]
                unboxed_callouts=callouts,
            )
            # Compute top_banner_h and bottom_banner_h so the phone canvas can map touch coordinates
            # directly between the rendered annotated PNG and original photo pixels.
            photo_w = max(int(report.get("width_px") or 1280), 320)
            ui_scale = max(1.0, min(3.2, photo_w / 1280.0))
            top_banner_h = int(round(58 * ui_scale))
            shot_lines = (report.get("on_site_shot_list") or [])[:3]
            bottom_extra = len(shot_lines) if not report.get("safe_to_leave_site") else 0
            bottom_banner_h = int(round((42 + bottom_extra * 18) * ui_scale))

            report["session"] = self._summarize_session(sid, sess)
            report["photos"] = photos
            report["items"] = items
            report["unboxed_callouts"] = callouts
            report["overlay_geometry"] = {
                "photo_width_px": int(report.get("width_px") or 0),
                "photo_height_px": int(report.get("height_px") or 0),
                "top_banner_px": top_banner_h,
                "bottom_banner_px": bottom_banner_h,
                "canvas_width_px": int(report.get("width_px") or 0),
                "canvas_height_px": int(report.get("height_px") or 0) + top_banner_h + bottom_banner_h,
            }
            report["meta"] = meta
            return report

    def add_uploaded_photo(
        self,
        session_id: str,
        *,
        filename: str,
        image_bytes: bytes,
        role: str = "backpanel",
        note: str = "",
        analyze: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            sid = _slugify(session_id)
            sess = self.get_session(sid)
            sdir = self._session_dir(sid)
            uploads_dir = sdir / "uploads"
            uploads_dir.mkdir(parents=True, exist_ok=True)
            safe_name = _sanitize_filename(filename)
            dest = uploads_dir / f"{len(list(uploads_dir.iterdir())) + 1:02d}_{safe_name}"
            dest.write_bytes(image_bytes)

            valid_roles = {
                "overview",
                "backpanel",
                "door",
                "terminal_strip",
                "nameplate",
                "part_label",
                "wire_numbers",
                "other",
            }
            role_val = role if role in valid_roles else "backpanel"
            added = sess.reconstruct_add_photos([dest], role=role_val, note=note)
            added_photos = added.get("added") or []
            new_pid = str(added_photos[0]["id"]) if added_photos else ""

            if analyze and new_pid and role_val in {"backpanel", "overview", "door"}:
                try:
                    sess.reconstruct_site_exit_check(photo_id=new_pid)
                except Exception:
                    pass

            self.save_session(sid)
            return self.get_status(sid, photo_id=new_pid or None)

    def add_missed_device(
        self,
        session_id: str,
        *,
        photo_id: str,
        region: dict[str, float],
        tag: str = "",
        part_number: str = "",
        brand: str = "",
        label_text: str = "",
        envelope_w_mm: float | None = None,
        envelope_h_mm: float | None = None,
        envelope_d_mm: float | None = None,
        mount: str = "",
        closeup_bytes: bytes | None = None,
        closeup_filename: str = "",
        snap_to_edges: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            sid = _slugify(session_id)
            sess = self.get_session(sid)
            res = sess.reconstruct_add_missed_device(
                photo_id,
                region,
                tag=tag,
                part_number=part_number,
                brand=brand,
                label_text=label_text,
                envelope_w_mm=envelope_w_mm,
                envelope_h_mm=envelope_h_mm,
                envelope_d_mm=envelope_d_mm,
                mount=mount,
                snap_to_edges=snap_to_edges,
            )
            item_dict = res.get("item") or {}
            item_id = str(item_dict.get("id") or "")

            # Remove any unboxed_callout that significantly overlaps the newly boxed region
            callouts = self.get_unboxed_callouts(sid, photo_id)
            if callouts:
                rx = float(region.get("x") or 0.0)
                ry = float(region.get("y") or 0.0)
                rw = float(region.get("w") or 0.0)
                rh = float(region.get("h") or 0.0)
                remaining: list[dict[str, Any]] = []
                for c in callouts:
                    cx = float(c.get("x") or 0.0)
                    cy = float(c.get("y") or 0.0)
                    cw = float(c.get("w") or 0.0)
                    ch = float(c.get("h") or 0.0)
                    ix1 = max(rx, cx)
                    iy1 = max(ry, cy)
                    ix2 = min(rx + rw, cx + cw)
                    iy2 = min(ry + rh, cy + ch)
                    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
                    c_area = max(1.0, cw * ch)
                    if inter / c_area < 0.25:
                        remaining.append(c)
                self.set_unboxed_callouts(sid, photo_id, remaining)

            if closeup_bytes and item_id:
                sdir = self._session_dir(sid)
                uploads_dir = sdir / "uploads"
                uploads_dir.mkdir(parents=True, exist_ok=True)
                safe_name = _sanitize_filename(closeup_filename or f"closeup_{item_id}.jpg")
                cpath = uploads_dir / f"closeup_{item_id}_{safe_name}"
                cpath.write_bytes(closeup_bytes)
                sess.reconstruct_link_closeup(
                    item_id,
                    closeup_photo_path=cpath,
                    tag=tag,
                    part_number=part_number,
                    brand=brand,
                    envelope_w_mm=envelope_w_mm,
                    envelope_h_mm=envelope_h_mm,
                    envelope_d_mm=envelope_d_mm,
                    mount=mount,
                )

            self.save_session(sid)
            status = self.get_status(sid, photo_id=photo_id)
            status["created_item"] = res.get("item")
            return status

    def link_closeup(
        self,
        session_id: str,
        item_id: str,
        *,
        closeup_bytes: bytes | None = None,
        closeup_filename: str = "",
        closeup_photo_id: str = "",
        tag: str = "",
        part_number: str = "",
        brand: str = "",
        envelope_w_mm: float | None = None,
        envelope_h_mm: float | None = None,
        envelope_d_mm: float | None = None,
        mount: str = "",
        library_id: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            sid = _slugify(session_id)
            sess = self.get_session(sid)
            cpath: Path | None = None
            if closeup_bytes:
                sdir = self._session_dir(sid)
                uploads_dir = sdir / "uploads"
                uploads_dir.mkdir(parents=True, exist_ok=True)
                safe_name = _sanitize_filename(closeup_filename or f"closeup_{item_id}.jpg")
                cpath = uploads_dir / f"closeup_{item_id}_{len(list(uploads_dir.iterdir())) + 1:02d}_{safe_name}"
                cpath.write_bytes(closeup_bytes)

            res = sess.reconstruct_link_closeup(
                item_id,
                closeup_photo_path=cpath,
                closeup_photo_id=closeup_photo_id,
                tag=tag,
                part_number=part_number,
                brand=brand,
                envelope_w_mm=envelope_w_mm,
                envelope_h_mm=envelope_h_mm,
                envelope_d_mm=envelope_d_mm,
                mount=mount,
                library_id=library_id,
            )
            self.save_session(sid)
            item_obj = res.get("item") or {}
            pid = str(item_obj.get("photo_id") or "")
            status = self.get_status(sid, photo_id=pid or None)
            status["updated_item"] = item_obj
            status["closeup_photo_id"] = res.get("closeup_photo_id") or ""
            return status

    def update_device_box(
        self,
        session_id: str,
        item_id: str,
        *,
        box: dict[str, float] | tuple[float, float, float, float] | list[float] | None = None,
        snap_to_edges: bool = False,
        search_margin_ratio: float = 0.12,
    ) -> dict[str, Any]:
        with self._lock:
            sid = _slugify(session_id)
            sess = self.get_session(sid)
            res = sess.reconstruct_update_device_box(
                item_id,
                box=box,
                snap_to_edges=snap_to_edges,
                search_margin_ratio=search_margin_ratio,
            )
            self.save_session(sid)
            item_obj = res.get("item") or {}
            pid = str(item_obj.get("photo_id") or "")
            status = self.get_status(sid, photo_id=pid or None)
            status["updated_item"] = item_obj
            status["updated_region"] = res.get("region")
            status["snapped"] = bool(res.get("snapped"))
            return status

    def refine_box(
        self,
        session_id: str,
        *,
        photo_id: str,
        box: dict[str, float] | tuple[float, float, float, float] | list[float],
        search_margin_ratio: float = 0.12,
    ) -> dict[str, Any]:
        with self._lock:
            sid = _slugify(session_id)
            sess = self.get_session(sid)
            refined = sess.reconstruct_refine_device_box(
                photo_id,
                box,
                search_margin_ratio=search_margin_ratio,
            )
            return {"ok": True, "photo_id": photo_id, "box": refined, "region": refined}

    def render_annotated_png(
        self,
        session_id: str,
        *,
        photo_id: str | None = None,
        filter_mode: str = "all",
        selected_item_id: str | None = None,
        include_banner: bool = True,
    ) -> bytes:
        with self._lock:
            sid = _slugify(session_id)
            sess = self.get_session(sid)
            store = ensure_reconstruction(sess.project)
            chosen_pid = str(photo_id or "").strip()
            if not chosen_pid and store.photos:
                primary = [
                    p for p in store.photos if p.role in {"backpanel", "overview", "door", "other"}
                ]
                chosen_pid = primary[0].id if primary else store.photos[0].id
            callouts = self.get_unboxed_callouts(sid, chosen_pid) if chosen_pid else []
            mode = filter_mode if filter_mode in {"all", "missing_only", "confirmed_only"} else "all"
            qimg = render_annotated_cabinet_photo(
                sess,
                photo_id=chosen_pid or None,
                filter_mode=mode,  # type: ignore[arg-type]
                selected_item_id=selected_item_id,
                unboxed_callouts=callouts,
                include_banner=include_banner,
            )
            return _qimage_to_png_bytes(qimg)

    def render_raw_photo_bytes(self, session_id: str, photo_id: str) -> tuple[bytes, str]:
        with self._lock:
            sid = _slugify(session_id)
            sess = self.get_session(sid)
            store = ensure_reconstruction(sess.project)
            photo = store.photo(photo_id)
            resolved = resolve_path(photo.path, sess.path)
            if not resolved.is_file() and photo.source_path and Path(photo.source_path).is_file():
                resolved = Path(photo.source_path)
            if not resolved.is_file():
                raise FileNotFoundError(f"Photo file not found for {photo_id}")
            ext = resolved.suffix.lower()
            mime = "image/png" if ext == ".png" else "image/jpeg"
            return resolved.read_bytes(), mime

    def notify_webhook(self, session_id: str, note: str = "") -> dict[str, Any]:
        with self._lock:
            sid = _slugify(session_id)
            status = self.get_status(sid)
            self.save_session(sid)
            cfg = self.get_server_config()
            webhook_url = cfg.get("webhook_url", "").strip()
            webhook_key = cfg.get("webhook_key", "").strip()
            payload = {
                "event": "panelcad.capture_session_ready",
                "session_id": sid,
                "safe_to_leave_site": status.get("safe_to_leave_site"),
                "site_exit_banner": status.get("site_exit_banner"),
                "on_site_shot_list": status.get("on_site_shot_list"),
                "project_path": status.get("session", {}).get("project_path"),
                "note": note,
                "timestamp": _now_iso(),
            }
            if not webhook_url:
                return {
                    "ok": True,
                    "sent": False,
                    "detail": "Session saved on server (set PANELCAD_CAPTURE_WEBHOOK_URL to post webhook notifications).",
                    "payload": payload,
                }
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    **(
                        {"Authorization": f"Bearer {webhook_key}"}
                        if webhook_key
                        else {}
                    ),
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp_body = resp.read().decode("utf-8", errors="replace")
                return {
                    "ok": True,
                    "sent": True,
                    "detail": "Sent capture session to webhook.",
                    "response": resp_body[:400],
                    "payload": payload,
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "sent": False,
                    "detail": f"Saved locally; webhook failed: {exc}",
                    "payload": payload,
                }


# ---------------------------------------------------------------------------
# Network interface discovery & zero-dependency ISO-18004 QR code generator
# so any end user can scan a QR code from Desktop PanelCAD or the Web UI
# to open the Mobile Capture Feed on their phone over Wi-Fi / Hotspot / VPN.
# ---------------------------------------------------------------------------

def discover_reachable_urls(port: int, *, access_token: str = "") -> list[dict[str, str]]:
    """Return a prioritized list of reachable HTTP URLs for this machine."""
    import socket

    candidates: list[tuple[str, str]] = []
    seen_ips: set[str] = set()

    def _add(ip: str, label: str) -> None:
        ip = ip.strip()
        if not ip or ip.startswith("127.") or ip == "0.0.0.0" or ":" in ip:
            return
        if ip in seen_ips:
            return
        seen_ips.add(ip)
        candidates.append((ip, label))

    # 1. Primary outbound interface IP via UDP connect trick (no packets sent)
    for target in (("10.255.255.255", 1), ("192.168.255.255", 1), ("8.8.8.8", 80)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.1)
            s.connect(target)
            _add(s.getsockname()[0], "Primary LAN / Wi-Fi")
            s.close()
        except Exception:
            pass

    # 2. All local interface IPv4s via `ip -4 addr` (Linux) or gethostbyname_ex
    try:
        import subprocess
        out = subprocess.check_output(["ip", "-4", "-o", "addr", "show"], text=True, timeout=1.0)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                iface = parts[1]
                cidr = parts[3]
                ip = cidr.split("/")[0]
                if iface.startswith("tailscale") or ip.startswith("100."):
                    _add(ip, f"Tailscale / Mesh VPN ({iface})")
                elif iface.startswith(("wl", "wlan", "wifi")):
                    _add(ip, f"Wi-Fi ({iface})")
                elif iface.startswith(("en", "eth")):
                    _add(ip, f"LAN ({iface})")
                elif not iface.startswith(("docker", "br-", "veth", "virbr", "lo")):
                    _add(ip, f"Network ({iface})")
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            lbl = "Tailscale / Mesh VPN" if ip.startswith("100.") else "Local Network"
            _add(ip, lbl)
    except Exception:
        pass

    suffix = f"/?token={urllib.parse.quote(access_token)}" if access_token else "/"
    results: list[dict[str, str]] = []
    for ip, label in candidates:
        results.append(
            {
                "label": label,
                "host": ip,
                "url": f"http://{ip}:{port}{suffix}",
            }
        )
    results.append(
        {
            "label": "This Machine (Localhost)",
            "host": "127.0.0.1",
            "url": f"http://127.0.0.1:{port}{suffix}",
        }
    )
    return results


# Compact ISO/IEC 18004 QR Code matrix generator (Versions 1..6, ECC Level L, Byte mode)
_QR_VERSION_PARAMS: dict[int, tuple[int, int, tuple[int, ...]]] = {
    # version: (total_codewords, ec_codewords_per_block, alignment_centers)
    1: (26, 7, ()),
    2: (44, 10, (6, 18)),
    3: (70, 15, (6, 22)),
    4: (100, 20, (6, 26)),
    5: (134, 26, (6, 30)),
    6: (172, 18, (6, 34)),  # V6-L uses 2 blocks of (68 data, 18 ec)
}


def _gf_mul(x: int, y: int) -> int:
    res = 0
    while y:
        if y & 1:
            res ^= x
        y >>= 1
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    return res


def _rs_encode(data: list[int], nsym: int) -> list[int]:
    gen = [1]
    x = 1
    for _ in range(nsym):
        nxt = [0] * (len(gen) + 1)
        for j, g in enumerate(gen):
            nxt[j] ^= _gf_mul(g, x)
            nxt[j + 1] ^= g
        gen = nxt
        x = _gf_mul(x, 2)
    # gen[0] is lowest degree in our construction if reversed; let's build standard monic polynomial:
    poly = [1]
    root = 1
    for _ in range(nsym):
        new_p = [0] * (len(poly) + 1)
        for i, c in enumerate(poly):
            new_p[i] ^= c
            new_p[i + 1] ^= _gf_mul(c, root)
        poly = new_p
        root = _gf_mul(root, 2)
    rem = [0] * nsym
    for b in data:
        factor = b ^ rem[0]
        rem = rem[1:] + [0]
        for i in range(nsym):
            rem[i] ^= _gf_mul(poly[i + 1], factor)
    return rem


def generate_qr_matrix(text: str) -> list[list[bool]]:
    """Generate a standard scannable QR code boolean matrix for `text` (up to 132 bytes)."""
    payload = text.encode("utf-8")
    # Pick smallest version (1..6) that fits payload in Byte mode (4-bit mode + 8-bit count + data + 4-bit terminator)
    chosen_ver = 6
    for ver in range(1, 7):
        total_cw, ec_cw, _ = _QR_VERSION_PARAMS[ver]
        blocks = 2 if ver == 6 else 1
        data_cw = total_cw - ec_cw * blocks
        if len(payload) + 2 <= data_cw:
            chosen_ver = ver
            break
    else:
        payload = payload[:132]

    total_cw, ec_cw, align_centers = _QR_VERSION_PARAMS[chosen_ver]
    blocks = 2 if chosen_ver == 6 else 1
    data_cw = total_cw - ec_cw * blocks

    bits: list[int] = [0, 1, 0, 0]  # Byte mode (0100)
    for shift in range(7, -1, -1):
        bits.append((len(payload) >> shift) & 1)
    for byte in payload:
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 1)
    for _ in range(min(4, data_cw * 8 - len(bits))):
        bits.append(0)
    while len(bits) % 8 != 0:
        bits.append(0)
    codewords: list[int] = []
    for i in range(0, len(bits), 8):
        val = 0
        for b in bits[i : i + 8]:
            val = (val << 1) | b
        codewords.append(val)
    pad_bytes = (0xEC, 0x11)
    p_idx = 0
    while len(codewords) < data_cw:
        codewords.append(pad_bytes[p_idx % 2])
        p_idx += 1

    if blocks == 1:
        ec = _rs_encode(codewords, ec_cw)
        final_cw = codewords + ec
    else:
        half = data_cw // 2
        b1, b2 = codewords[:half], codewords[half:]
        e1, e2 = _rs_encode(b1, ec_cw), _rs_encode(b2, ec_cw)
        final_cw = []
        for a, b in zip(b1, b2):
            final_cw.extend([a, b])
        for a, b in zip(e1, e2):
            final_cw.extend([a, b])

    size = 17 + 4 * chosen_ver
    grid: list[list[bool | None]] = [[None] * size for _ in range(size)]
    reserved: list[list[bool]] = [[False] * size for _ in range(size)]

    def _set_mod(r: int, c: int, val: bool, *, is_reserved: bool = True) -> None:
        if 0 <= r < size and 0 <= c < size:
            grid[r][c] = val
            if is_reserved:
                reserved[r][c] = True

    def _finder(r0: int, c0: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if 0 <= r < size and 0 <= c < size:
                    in_outer = 0 <= dr <= 6 and 0 <= dc <= 6 and (dr in (0, 6) or dc in (0, 6))
                    in_inner = 2 <= dr <= 4 and 2 <= dc <= 4
                    _set_mod(r, c, in_outer or in_inner)

    _finder(0, 0)
    _finder(0, size - 7)
    _finder(size - 7, 0)

    for i in range(8, size - 8):
        if not reserved[6][i]:
            _set_mod(6, i, i % 2 == 0)
        if not reserved[i][6]:
            _set_mod(i, 6, i % 2 == 0)

    if align_centers:
        for ar in align_centers:
            for ac in align_centers:
                if reserved[ar][ac]:
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        val = max(abs(dr), abs(dc)) in (0, 2)
                        _set_mod(ar + dr, ac + dc, val)

    # Reserve format info modules + dark module
    for i in range(9):
        if i != 6:
            reserved[8][i] = True
            reserved[i][8] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True
    _set_mod(size - 8, 8, True)

    stream_bits: list[int] = []
    for cw in final_cw:
        for shift in range(7, -1, -1):
            stream_bits.append((cw >> shift) & 1)
    # Remainder bits (7 for V2..V6)
    if chosen_ver >= 2:
        stream_bits.extend([0] * 7)

    bit_idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for dc in (0, -1):
                c = col + dc
                if not reserved[r][c]:
                    bval = bool(stream_bits[bit_idx]) if bit_idx < len(stream_bits) else False
                    bit_idx += 1
                    # Apply Mask 0: (r + c) % 2 == 0
                    if (r + c) % 2 == 0:
                        bval = not bval
                    grid[r][c] = bval
        col -= 2
        upward = not upward

    # Format bits for ECC Level L (01) + Mask 0 (000) -> 0x77C4 (111011111000100)
    fmt = 0x77C4
    fmt_bits = [(fmt >> i) & 1 for i in range(15)]  # LSB first (bit 0..14)
    # Place around top-left finder
    tl_coords = [
        (8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
        (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8),
    ]
    for idx, (r, c) in enumerate(tl_coords):
        grid[r][c] = bool(fmt_bits[14 - idx])
    # Place along bottom-left and top-right
    for idx in range(7):
        grid[size - 1 - idx][8] = bool(fmt_bits[14 - idx])
    for idx in range(7, 15):
        grid[8][size - 15 + idx] = bool(fmt_bits[14 - idx])

    return [[bool(cell) for cell in row] for row in grid]


def render_qr_svg(text: str, *, border: int = 3) -> str:
    """Render a crisp SVG QR code for `text`."""
    matrix = generate_qr_matrix(text)
    n = len(matrix)
    total = n + border * 2
    rects: list[str] = []
    for r, row in enumerate(matrix):
        for c, val in enumerate(row):
            if val:
                rects.append(f'<rect x="{c + border}" y="{r + border}" width="1" height="1"/>')
    body = "".join(rects)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" '
        f'shape-rendering="crispEdges">'
        f'<rect width="{total}" height="{total}" fill="#ffffff"/>'
        f'<g fill="#0f172a">{body}</g></svg>'
    )


def render_qr_qimage(text: str, *, scale: int = 6, border: int = 3) -> Any:
    """Render a `QImage` QR code for `text` for use in Qt dialogs."""
    from panelcad.reconstruct.feedback import _ensure_qt_gui_app
    _ensure_qt_gui_app()
    from PySide6.QtGui import QColor, QImage, QPainter

    matrix = generate_qr_matrix(text)
    n = len(matrix)
    total = (n + border * 2) * scale
    img = QImage(total, total, QImage.Format.Format_RGB32)
    img.fill(QColor("#ffffff"))
    painter = QPainter(img)
    dark = QColor("#0f172a")
    for r, row in enumerate(matrix):
        for c, val in enumerate(row):
            if val:
                painter.fillRect((c + border) * scale, (r + border) * scale, scale, scale, dark)
    painter.end()
    return img


PWA_MANIFEST = {
    "name": "PanelCAD Mobile Field Capture",
    "short_name": "PanelCAD Capture",
    "description": "Mobile cabinet photo capture, site-exit readiness check, close-up linking, and missed-device boxing for PanelCAD.",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#0b0f17",
    "theme_color": "#0f172a",
}

MOBILE_CAPTURE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=5, user-scalable=yes">
<meta name="theme-color" content="#0f172a">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.json">
<title>PanelCAD — Mobile Field Capture</title>
<style>
  :root {
    --bg: #0b0f17;
    --panel: #111827;
    --panel-alt: #1e293b;
    --border: #334155;
    --text: #f8fafc;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --green: #10b981;
    --green-bg: #064e3b;
    --red: #ef4444;
    --red-bg: #7f1d1d;
    --amber: #f59e0b;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex;
    flex-direction: column;
    min-height: 100vh;
  }
  header {
    background: #0f172a;
    border-bottom: 1px solid var(--border);
    padding: 10px 12px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    position: sticky;
    top: 0;
    z-index: 40;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 9px;
  }
  .brand-badge {
    width: 32px;
    height: 32px;
    border-radius: 8px;
    background: linear-gradient(135deg, #0284c7, #10b981);
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 800;
    font-size: 16px;
    color: #fff;
  }
  .brand-title b {
    font-size: 15px;
    display: block;
    line-height: 1.15;
  }
  .brand-title small {
    font-size: 11px;
    color: var(--muted);
  }
  .header-controls {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }
  select, input, button {
    font: inherit;
  }
  .select-pill {
    background: var(--panel-alt);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 7px;
    padding: 6px 9px;
    font-size: 13px;
    max-width: 210px;
  }
  .btn {
    background: var(--panel-alt);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 7px;
    padding: 7px 11px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
  }
  .btn:active { transform: scale(0.98); }
  .btn-primary {
    background: #0284c7;
    border-color: #38bdf8;
    color: #fff;
  }
  .btn-success {
    background: #059669;
    border-color: #10b981;
    color: #fff;
  }
  .btn-amber {
    background: #b45309;
    border-color: #f59e0b;
    color: #fff;
  }
  .btn-active-mode {
    background: #0369a1 !important;
    border: 2px solid #38bdf8 !important;
    color: #f0f9ff !important;
    box-shadow: 0 0 12px rgba(56, 189, 248, 0.45);
  }
  .site-banner {
    padding: 11px 14px;
    font-weight: 700;
    font-size: 14px;
    line-height: 1.35;
    border-bottom: 2px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .site-banner.do-not-leave {
    background: var(--red-bg);
    border-bottom-color: var(--amber);
    color: #fffbeb;
  }
  .site-banner.ready {
    background: var(--green-bg);
    border-bottom-color: var(--green);
    color: #ecfdf5;
  }
  .site-banner.needs-analysis {
    background: #1e293b;
    border-bottom-color: #64748b;
    color: #f1f5f9;
  }
  .banner-pills {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    font-size: 12px;
    font-weight: 600;
  }
  .pill {
    padding: 2px 9px;
    border-radius: 999px;
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }
  .pill-green { background: rgba(16, 185, 129, 0.25); color: #a7f3d0; border: 1px solid #10b981; }
  .pill-amber { background: rgba(245, 158, 11, 0.28); color: #fde68a; border: 1px solid #f59e0b; }
  .pill-cyan  { background: rgba(56, 189, 248, 0.22); color: #bae6fd; border: 1px solid #38bdf8; }

  .toolbar-bar {
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    padding: 9px 12px;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    align-items: center;
  }
  .photo-tabs {
    display: flex;
    gap: 6px;
    overflow-x: auto;
    padding: 8px 12px;
    background: #0d131f;
    border-bottom: 1px solid var(--border);
  }
  .photo-tab {
    padding: 5px 10px;
    border-radius: 6px;
    background: var(--panel-alt);
    border: 1px solid var(--border);
    color: var(--muted);
    font-size: 12px;
    white-space: nowrap;
    cursor: pointer;
  }
  .photo-tab.active {
    background: #0284c7;
    color: #fff;
    border-color: #38bdf8;
    font-weight: 600;
  }

  .workspace-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 12px;
    padding: 12px;
    max-width: 1440px;
    width: 100%;
    margin: 0 auto;
    flex: 1;
  }
  @media (min-width: 960px) {
    .workspace-grid {
      grid-template-columns: 3fr 2fr;
      align-items: start;
    }
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }
  .card-header {
    padding: 10px 13px;
    background: #162032;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    font-weight: 700;
    font-size: 13px;
  }
  .canvas-stage {
    position: relative;
    width: 100%;
    background: #090c10;
    min-height: 240px;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    user-select: none;
  }
  .canvas-stage.draw-mode {
    touch-action: none;
    outline: 2px dashed var(--accent);
    outline-offset: -4px;
  }
  #annotatedImg {
    display: block;
    width: 100%;
    height: auto;
    max-height: 78vh;
    object-fit: contain;
  }
  #touchOverlay {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    cursor: pointer;
  }
  .mode-hint {
    background: rgba(3, 105, 161, 0.92);
    color: #f0f9ff;
    padding: 8px 12px;
    font-size: 12px;
    font-weight: 600;
    border-bottom: 1px solid #38bdf8;
    display: none;
  }
  .mode-hint.visible { display: block; }

  .shot-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
  }
  .shot-card {
    background: #182234;
    border: 1px solid var(--border);
    border-left: 4px solid var(--amber);
    border-radius: 8px;
    padding: 10px 12px;
    cursor: pointer;
    transition: background 0.12s;
  }
  .shot-card:hover { background: #1e2b42; }
  .shot-card.confirmed {
    border-left-color: var(--green);
    opacity: 0.9;
  }
  .shot-card.unboxed {
    border-left-color: var(--accent);
    background: #0f2438;
  }
  .shot-card-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    font-weight: 700;
    font-size: 13px;
    margin-bottom: 4px;
  }
  .shot-card-reasons {
    font-size: 12px;
    color: #fde68a;
    line-height: 1.4;
  }
  .shot-card.confirmed .shot-card-reasons {
    color: #86efac;
  }
  .shot-card-action {
    margin-top: 7px;
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }

  /* Slide-up Drawer / Modal for Device Close-Up, Missed Device & Server Setup */
  .drawer-backdrop {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.76);
    z-index: 90;
    align-items: flex-end;
    justify-content: center;
  }
  @media (min-width: 700px) {
    .drawer-backdrop { align-items: center; }
  }
  .drawer-backdrop.open { display: flex; }
  .drawer {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px 14px 0 0;
    width: 100%;
    max-width: 560px;
    max-height: 90vh;
    overflow-y: auto;
    padding: 16px;
  }
  @media (min-width: 700px) {
    .drawer { border-radius: 12px; }
  }
  .drawer h3 {
    margin: 0 0 6px 0;
    font-size: 16px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .form-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    margin-top: 10px;
  }
  .form-field {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .form-field.full { grid-column: 1 / -1; }
  .form-field label {
    font-size: 11px;
    color: var(--muted);
    font-weight: 600;
  }
  .form-field input, .form-field select {
    background: var(--panel-alt);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px 10px;
    font-size: 14px;
  }
  .missing-alert {
    background: rgba(127, 29, 29, 0.55);
    border: 1px solid #f59e0b;
    color: #fef3c7;
    padding: 8px 10px;
    border-radius: 7px;
    font-size: 12px;
    line-height: 1.4;
    margin: 8px 0;
  }
  #toast {
    position: fixed;
    bottom: 20px;
    left: 50%;
    transform: translateX(-50%);
    background: #0284c7;
    color: #fff;
    padding: 9px 15px;
    border-radius: 999px;
    font-size: 13px;
    font-weight: 600;
    display: none;
    z-index: 120;
    box-shadow: 0 6px 20px rgba(0,0,0,0.45);
  }
  .qr-box {
    background: #fff;
    padding: 10px;
    border-radius: 8px;
    width: 170px;
    height: 170px;
    margin: 8px auto;
  }
  .url-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin: 8px 0;
  }
  .url-row {
    background: #0b0f17;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 7px 10px;
    font-family: monospace;
    font-size: 12px;
    color: #7dd3fc;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    word-break: break-all;
  }
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="brand-badge">P</div>
    <div class="brand-title">
      <b>PanelCAD Field Capture</b>
      <small>On-Site Cabinet Photo &amp; Exit-Readiness Check</small>
    </div>
  </div>
  <div class="header-controls">
    <select id="sessionSelect" class="select-pill" onchange="onSessionChange()"></select>
    <button class="btn" onclick="openNewSessionDrawer()">＋ New Job</button>
    <button class="btn" onclick="openSetupDrawer()" title="Phone QR Code, LAN/VPN URLs &amp; Webhook Setup">⚙ Connect / Setup</button>
  </div>
</header>

<div id="siteBanner" class="site-banner needs-analysis">
  <div id="siteBannerText">Loading capture session…</div>
  <div class="banner-pills" id="bannerPills"></div>
</div>

<div class="toolbar-bar">
  <!-- Hidden file inputs for rear camera capture OR gallery upload -->
  <input type="file" id="cameraCabinetInput" accept="image/*" capture="environment" style="display:none" onchange="uploadCabinetPhoto(this.files)">
  <input type="file" id="galleryCabinetInput" accept="image/*" style="display:none" onchange="uploadCabinetPhoto(this.files)">
  <select id="photoRoleSelect" class="select-pill" style="max-width:145px;">
    <option value="backpanel">Role: Backpanel</option>
    <option value="overview">Role: Overview</option>
    <option value="door">Role: Door Inner</option>
    <option value="terminal_strip">Role: Terminals</option>
    <option value="nameplate">Role: Nameplate</option>
  </select>
  <button class="btn btn-primary" onclick="document.getElementById('cameraCabinetInput').click()">📷 Snap Cabinet Photo</button>
  <button class="btn" onclick="document.getElementById('galleryCabinetInput').click()">🖼 Upload Photo</button>
  <button class="btn btn-amber" id="drawBoxModeBtn" onclick="toggleDrawMissedMode()" style="font-weight:700;">＋ Draw Box on Missed Device</button>
  <select id="filterModeSelect" class="select-pill" onchange="refreshStatus()">
    <option value="all">Show: All Boxes</option>
    <option value="missing_only">Show: Missing Info Only</option>
    <option value="confirmed_only">Show: Confirmed Only</option>
  </select>
  <button class="btn btn-success" onclick="downloadPanelcadFile()">⬇ Download .panelcad</button>
  <button class="btn" onclick="sendToAgentWebhook()">📤 Notify / Sync</button>
</div>

<div class="photo-tabs" id="photoTabs"></div>

<div class="workspace-grid">
  <!-- Left/Top: Interactive Annotated Cabinet Photo -->
  <div class="card">
    <div class="card-header">
      <span id="photoCardTitle">Annotated Cabinet Photo (Tap any box to inspect/confirm)</span>
      <span style="font-size:11px;color:var(--muted);">Green = Ready · Red = Needs Info · Cyan = Unboxed</span>
    </div>
    <div id="drawModeHint" class="mode-hint">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;">
        <span id="drawModeBannerText">🎯 DRAW MODE: Drag a box around the missed device on the photo (or tap 2 opposite corners)</span>
        <span style="display:inline-flex;gap:6px;align-items:center;">
          <label style="display:inline-flex;align-items:center;gap:4px;font-size:11px;cursor:pointer;">
            <input type="checkbox" id="autoSnapDrawChk" checked> ✨ Auto-Snap to Edges
          </label>
          <button class="btn" style="padding:3px 8px;font-size:11px;" onclick="toggleDrawMissedMode(false)">Cancel</button>
        </span>
      </div>
    </div>
    <div class="canvas-stage" id="canvasStage">
      <img id="annotatedImg" alt="Annotated cabinet photo" onload="onAnnotatedImageLoaded()">
      <canvas id="touchOverlay"></canvas>
    </div>
  </div>

  <!-- Right/Bottom: Before You Leave Site Shot List & All Detected Devices -->
  <div class="card">
    <div class="card-header">
      <span>Before You Leave Site — Device Checklist</span>
      <span id="shotCountBadge" class="pill pill-amber">0 items</span>
    </div>
    <div class="shot-list" id="deviceShotList"></div>
  </div>
</div>

<!-- Device Close-Up & Quick Confirm Drawer -->
<div class="drawer-backdrop" id="deviceDrawer">
  <div class="drawer">
    <h3>
      <span id="drawerTitle">Device Context &amp; Close-Up</span>
      <button class="btn" onclick="closeDeviceDrawer()">✕</button>
    </h3>
    <div id="drawerMissingAlert" class="missing-alert" style="display:none;"></div>

    <input type="file" id="closeupCameraInput" accept="image/*" capture="environment" style="display:none" onchange="onCloseupFilePicked(this.files)">
    <input type="file" id="closeupGalleryInput" accept="image/*" style="display:none" onchange="onCloseupFilePicked(this.files)">
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0;">
      <button class="btn btn-amber" style="flex:1;" onclick="document.getElementById('closeupCameraInput').click()">📷 Snap Close-Up Nameplate Photo</button>
      <button class="btn" onclick="document.getElementById('closeupGalleryInput').click()">🖼 Pick Close-Up</button>
    </div>
    <div id="closeupPickedStatus" style="font-size:12px;color:#86efac;margin-bottom:6px;display:none;"></div>

    <div style="background:#162032;border:1px solid var(--border);border-radius:8px;padding:9px 10px;margin:8px 0;">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:6px;flex-wrap:wrap;margin-bottom:6px;">
        <span style="font-size:11px;font-weight:700;color:#bae6fd;">📐 Bounding Box Coordinates (px on photo)</span>
        <div style="display:flex;gap:5px;flex-wrap:wrap;">
          <button type="button" class="btn btn-primary" style="padding:4px 8px;font-size:11px;" onclick="snapDrawerBoxToEdges()">✨ Snap Box to Device Edges</button>
          <button type="button" class="btn" style="padding:4px 8px;font-size:11px;" onclick="startRedrawFromDrawer()">📐 Adjust / Redraw on Photo</button>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(4, 1fr);gap:6px;">
        <div class="form-field"><label>X (left)</label><input type="number" id="boxX" step="1"></div>
        <div class="form-field"><label>Y (top)</label><input type="number" id="boxY" step="1"></div>
        <div class="form-field"><label>W (width)</label><input type="number" id="boxW" step="1"></div>
        <div class="form-field"><label>H (height)</label><input type="number" id="boxH" step="1"></div>
      </div>
    </div>

    <div class="form-grid">
      <div class="form-field">
        <label>Device Tag (e.g. PSU1, PLC1, K1, SR1)</label>
        <input type="text" id="devTag" placeholder="e.g. PSU1">
      </div>
      <div class="form-field">
        <label>Brand / Manufacturer</label>
        <input type="text" id="devBrand" placeholder="e.g. Eaton, Keyence, Dold">
      </div>
      <div class="form-field full">
        <label>Part Number / Catalog SKU</label>
        <input type="text" id="devPart" placeholder="e.g. PSG240E24RM or LG5924">
      </div>
      <div class="form-field">
        <label>Width W (mm)</label>
        <input type="number" step="0.1" id="devW" placeholder="e.g. 85">
      </div>
      <div class="form-field">
        <label>Height H (mm)</label>
        <input type="number" step="0.1" id="devH" placeholder="e.g. 125">
      </div>
      <div class="form-field">
        <label>Depth D (mm)</label>
        <input type="number" step="0.1" id="devD" placeholder="e.g. 128">
      </div>
      <div class="form-field">
        <label>Mounting Type</label>
        <select id="devMount">
          <option value="din_rail">DIN Rail</option>
          <option value="backplate">Backplate</option>
          <option value="door">Door</option>
          <option value="field">Field</option>
        </select>
      </div>
    </div>

    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px;">
      <button class="btn" onclick="closeDeviceDrawer()">Cancel</button>
      <button class="btn btn-success" id="saveDeviceBtn" onclick="saveDeviceDrawer()">✓ Save &amp; Confirm Device</button>
    </div>
  </div>
</div>

<!-- New Session Drawer -->
<div class="drawer-backdrop" id="newSessionDrawer">
  <div class="drawer">
    <h3>
      <span>New Field Capture Job / Session</span>
      <button class="btn" onclick="document.getElementById('newSessionDrawer').classList.remove('open')">✕</button>
    </h3>
    <div class="form-grid">
      <div class="form-field full">
        <label>Job / Panel Name</label>
        <input type="text" id="newSessionName" placeholder="e.g. Line 2 Main Control Cabinet">
      </div>
      <div class="form-field full">
        <label>Site / Facility (Optional)</label>
        <input type="text" id="newSessionSite" placeholder="e.g. North Plant Building B">
      </div>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px;">
      <button class="btn" onclick="document.getElementById('newSessionDrawer').classList.remove('open')">Cancel</button>
      <button class="btn btn-primary" onclick="createNewSession()">Create Capture Session</button>
    </div>
  </div>
</div>

<!-- Self-Serve Phone QR & Webhook Connection Setup Drawer -->
<div class="drawer-backdrop" id="setupDrawer">
  <div class="drawer">
    <h3>
      <span>⚙ Connect Phone &amp; Optional Agent Webhook</span>
      <button class="btn" onclick="document.getElementById('setupDrawer').classList.remove('open')">✕</button>
    </h3>
    <p style="font-size:12px;color:var(--muted);margin:4px 0 8px 0;">
      Scan this QR code with your phone camera (on the same Wi-Fi, phone hotspot, or VPN/Tailscale) to open this live capture session on your phone:
    </p>
    <div class="qr-box" id="qrContainer"></div>
    <div class="url-list" id="reachableUrlList"></div>

    <hr style="border:0;border-top:1px solid var(--border);margin:12px 0;">
    <p style="font-size:12px;color:var(--muted);margin:4px 0 8px 0;">
      <b>Optional Webhook / Agent Notification URL:</b> When configured, tapping <i>📤 Notify / Sync</i> posts a JSON summary of your capture session and site-exit readiness to your automation or AI agent endpoint.
    </p>
    <div class="form-grid">
      <div class="form-field full">
        <label>Webhook URL (optional, e.g. https://...)</label>
        <input type="url" id="cfgWebhookUrl" placeholder="https://your-agent-or-webhook.example/hook">
      </div>
      <div class="form-field full">
        <label>Bearer Token / Secret (optional)</label>
        <input type="password" id="cfgWebhookKey" placeholder="Optional Authorization Bearer token">
      </div>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px;">
      <button class="btn" onclick="document.getElementById('setupDrawer').classList.remove('open')">Close</button>
      <button class="btn btn-primary" onclick="saveServerConfig()">Save Connection Settings</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
let currentSessionId = "";
let currentPhotoId = "";
let currentStatus = null;
let drawMissedMode = false;
let redrawTargetItemId = ""; // non-empty when redrawing an existing device's box
let dragStartPt = null;
let dragCurPt = null;
let firstTapCornerPt = null; // supports 2-Tap Corner Mode on mobile!
let drawerMode = "edit"; // "edit" (existing item) or "missed" (new finger-drawn box)
let activeItemId = "";
let activeBoxObj = null;
let pendingMissedRegion = null;
let pendingCloseupFile = null;

function getTokenParam() {
  const params = new URLSearchParams(window.location.search);
  return params.get("token") || "";
}

function apiHeaders(extra = {}) {
  const tok = getTokenParam();
  return tok ? { ...extra, "X-PanelCAD-Token": tok } : extra;
}

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.style.display = "block";
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.style.display = "none"; }, 3200);
}

async function initApp() {
  const params = new URLSearchParams(window.location.search);
  await loadSessions(params.get("session") || "");
}

async function loadSessions(preferredId = "") {
  const res = await fetch("/api/sessions", { headers: apiHeaders() });
  const data = await res.json();
  const list = data.sessions || [];
  const sel = document.getElementById("sessionSelect");
  sel.innerHTML = "";
  if (list.length === 0) {
    const created = await fetch("/api/sessions", {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ name: "Field Panel Capture" })
    }).then(r => r.json());
    if (created.session) list.push(created.session);
  }
  for (const s of list) {
    const opt = document.createElement("option");
    opt.value = s.session_id;
    const badge = s.safe_to_leave_site ? "✓" : "⚠";
    opt.textContent = `${badge} ${s.name} (${s.photo_count} photo${s.photo_count === 1 ? "" : "s"})`;
    sel.appendChild(opt);
  }
  currentSessionId = preferredId || currentSessionId || (list[0] && list[0].session_id) || "";
  if (currentSessionId) sel.value = currentSessionId;
  await refreshStatus();
}

async function onSessionChange() {
  currentSessionId = document.getElementById("sessionSelect").value;
  currentPhotoId = "";
  await refreshStatus();
}

async function refreshStatus() {
  if (!currentSessionId) return;
  const filterMode = document.getElementById("filterModeSelect").value || "all";
  const q = new URLSearchParams({ mode: filterMode });
  if (currentPhotoId) q.set("photo_id", currentPhotoId);
  const res = await fetch(`/api/sessions/${encodeURIComponent(currentSessionId)}/status?${q}`, {
    headers: apiHeaders()
  });
  currentStatus = await res.json();
  currentPhotoId = currentStatus.photo_id || "";
  renderBanner(currentStatus);
  renderPhotoTabs(currentStatus);
  renderShotList(currentStatus);
  renderAnnotatedImage(currentStatus);
}

function renderBanner(st) {
  const bannerEl = document.getElementById("siteBanner");
  const textEl = document.getElementById("siteBannerText");
  const pillsEl = document.getElementById("bannerPills");
  bannerEl.className = "site-banner " + (
    st.safe_to_leave_site ? "ready" : (st.site_exit_status === "do_not_leave" ? "do-not-leave" : "needs-analysis")
  );
  textEl.textContent = st.site_exit_banner || "Check Site-Exit Readiness";
  const unboxedLen = (st.unboxed_callouts || []).length;
  pillsEl.innerHTML = `
    <span class="pill pill-green">✓ ${st.confirmed_count || 0} Confirmed</span>
    <span class="pill pill-amber">⚠ ${st.missing_info_count || 0} Missing Info</span>
    ${unboxedLen > 0 ? `<span class="pill pill-cyan">? ${unboxedLen} Unboxed Gap(s)</span>` : ""}
    <span style="color:var(--muted);font-weight:500;">Tap any device box on photo or card below to attach close-up / confirm</span>
  `;
}

function renderPhotoTabs(st) {
  const tabsEl = document.getElementById("photoTabs");
  const photos = (st.photos || []).filter(p => ["backpanel", "overview", "door", "other"].includes(p.role));
  const list = photos.length > 0 ? photos : (st.photos || []);
  tabsEl.innerHTML = "";
  for (const p of list) {
    const btn = document.createElement("button");
    btn.className = "photo-tab" + (p.id === currentPhotoId ? " active" : "");
    btn.textContent = `${p.original_name || p.id} (${(p.role || "").replace("_", " ")})`;
    btn.onclick = () => {
      currentPhotoId = p.id;
      refreshStatus();
    };
    tabsEl.appendChild(btn);
  }
}

function renderAnnotatedImage(st) {
  const img = document.getElementById("annotatedImg");
  if (!st.photo_id) {
    img.removeAttribute("src");
    return;
  }
  const filterMode = document.getElementById("filterModeSelect").value || "all";
  const tok = getTokenParam();
  const q = new URLSearchParams({
    photo_id: st.photo_id,
    mode: filterMode,
    t: String(Date.now())
  });
  if (tok) q.set("token", tok);
  img.src = `/api/sessions/${encodeURIComponent(currentSessionId)}/annotated.png?${q}`;
}

function onAnnotatedImageLoaded() {
  const img = document.getElementById("annotatedImg");
  const canvas = document.getElementById("touchOverlay");
  canvas.width = img.clientWidth || 1;
  canvas.height = img.clientHeight || 1;
  drawDragRect();
}

window.addEventListener("resize", () => {
  onAnnotatedImageLoaded();
});

function renderShotList(st) {
  const container = document.getElementById("deviceShotList");
  const badge = document.getElementById("shotCountBadge");
  container.innerHTML = "";
  const boxes = st.boxes || [];
  const unboxed = st.unboxed_callouts || [];
  const missingCount = (st.missing_info_count || 0) + unboxed.length;

  if (st.safe_to_leave_site) {
    badge.className = "pill pill-green";
    badge.textContent = "✓ Safe to Leave Site";
  } else {
    badge.className = "pill pill-amber";
    badge.textContent = `${missingCount} Action(s) Needed`;
  }

  for (const u of unboxed) {
    const card = document.createElement("div");
    card.className = "shot-card unboxed";
    card.innerHTML = `
      <div class="shot-card-top">
        <span style="color:#38bdf8;">${u.label || "? UNBOXED DEVICE — MISSED BY BOT"}</span>
        <span class="pill pill-cyan">Missed Device</span>
      </div>
      <div class="shot-card-reasons" style="color:#bae6fd;">${u.detail || "Drag a box around this device or tap below to register it."}</div>
      <div class="shot-card-action">
        <button class="btn btn-primary" style="font-size:12px;padding:5px 9px;">＋ Box &amp; Identify This Device</button>
      </div>
    `;
    card.onclick = () => {
      openMissedDeviceDrawer({ x: u.x, y: u.y, w: u.w, h: u.h }, u.label || "");
    };
    container.appendChild(card);
  }

  // Sort missing_info first, then confirmed
  const sorted = [...boxes].sort((a, b) => {
    if (a.status_category === b.status_category) return 0;
    return a.status_category === "missing_info" ? -1 : 1;
  });

  for (const b of sorted) {
    const isOk = b.status_category === "confirmed";
    const card = document.createElement("div");
    card.className = "shot-card" + (isOk ? " confirmed" : "");
    const reasonsHtml = isOk
      ? `✓ Confirmed (${b.dimensions_label || "layout ready"})${b.closeup_photo_id ? " · Close-Up Linked: " + b.closeup_photo_id : ""}`
      : (b.missing_reasons || []).map(r => `• ${r}`).join("<br>");
    card.innerHTML = `
      <div class="shot-card-top">
        <span>${b.header_label}</span>
        <span class="pill ${isOk ? "pill-green" : "pill-amber"}">${isOk ? "✓ Ready" : (b.needs_closeup ? "📷 Close-Up Needed" : "⚠ Needs Info")}</span>
      </div>
      <div class="shot-card-reasons">${reasonsHtml}</div>
      ${!isOk ? `<div class="shot-card-action"><button class="btn btn-amber" style="font-size:12px;padding:5px 9px;">📷 Attach Close-Up / Confirm Specs</button></div>` : ""}
    `;
    card.onclick = () => openDeviceDrawer(b);
    container.appendChild(card);
  }
}

/* Coordinate mapping between touchOverlay client coords and source photo pixel coords */
function clientToPhotoCoords(clientX, clientY) {
  const img = document.getElementById("annotatedImg");
  const rect = img.getBoundingClientRect();
  if (!currentStatus || !currentStatus.overlay_geometry || rect.width <= 0 || rect.height <= 0) {
    return { x: 0, y: 0 };
  }
  const geom = currentStatus.overlay_geometry;
  const scaleX = geom.canvas_width_px / rect.width;
  const scaleY = geom.canvas_height_px / rect.height;
  const canvasX = (clientX - rect.left) * scaleX;
  const canvasY = (clientY - rect.top) * scaleY;
  const photoX = Math.max(0, Math.min(geom.photo_width_px, canvasX));
  const photoY = Math.max(0, Math.min(geom.photo_height_px, canvasY - geom.top_banner_px));
  return {
    x: Math.round(photoX * 10) / 10,
    y: Math.round(photoY * 10) / 10,
    relX: clientX - rect.left,
    relY: clientY - rect.top
  };
}

function toggleDrawMissedMode(forceVal, forItemId = "") {
  drawMissedMode = typeof forceVal === "boolean" ? forceVal : !drawMissedMode;
  redrawTargetItemId = drawMissedMode ? (forItemId || "") : "";
  const btn = document.getElementById("drawBoxModeBtn");
  const stage = document.getElementById("canvasStage");
  const hint = document.getElementById("drawModeHint");
  const bannerTxt = document.getElementById("drawModeBannerText");
  btn.classList.toggle("btn-active-mode", drawMissedMode);
  stage.classList.toggle("draw-mode", drawMissedMode);
  hint.classList.toggle("visible", drawMissedMode);
  if (bannerTxt) {
    bannerTxt.textContent = redrawTargetItemId
      ? `🎯 ADJUST BOX MODE (${redrawTargetItemId}): Drag a new tight box around the device (or tap 2 opposite corners)`
      : "🎯 DRAW MODE: Drag a box around the missed device on the photo (or tap 2 opposite corners)";
  }
  dragStartPt = null;
  dragCurPt = null;
  firstTapCornerPt = null;
  drawDragRect();
}

function drawDragRect() {
  const canvas = document.getElementById("touchOverlay");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const ptA = dragStartPt || firstTapCornerPt;
  const ptB = dragCurPt || firstTapCornerPt;
  if (!ptA) return;

  // Draw crosshair on active point
  const chPt = dragCurPt || ptA;
  ctx.save();
  ctx.strokeStyle = "rgba(56, 189, 248, 0.75)";
  ctx.lineWidth = 1.5;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(chPt.relX, 0);
  ctx.lineTo(chPt.relX, canvas.height);
  ctx.moveTo(0, chPt.relY);
  ctx.lineTo(canvas.width, chPt.relY);
  ctx.stroke();
  ctx.restore();

  // Draw corner anchor dot
  ctx.fillStyle = "#22c55e";
  ctx.beginPath();
  ctx.arc(ptA.relX, ptA.relY, 6, 0, Math.PI * 2);
  ctx.fill();

  if (!ptB || (ptA === ptB)) return;
  const x = Math.min(ptA.relX, ptB.relX);
  const y = Math.min(ptA.relY, ptB.relY);
  const w = Math.abs(ptB.relX - ptA.relX);
  const h = Math.abs(ptB.relY - ptA.relY);
  ctx.fillStyle = "rgba(34, 197, 94, 0.22)";
  ctx.fillRect(x, y, w, h);
  ctx.strokeStyle = "#38bdf8";
  ctx.lineWidth = 3;
  ctx.setLineDash([7, 5]);
  ctx.strokeRect(x, y, w, h);

  // Live W x H px pill
  const pw = Math.round(Math.abs(ptB.x - ptA.x));
  const ph = Math.round(Math.abs(ptB.y - ptA.y));
  const label = `${pw} × ${ph} px`;
  ctx.setLineDash([]);
  ctx.font = "bold 12px sans-serif";
  const tw = ctx.measureText(label).width + 14;
  const pillY = Math.max(4, y - 24);
  ctx.fillStyle = "rgba(15, 23, 42, 0.92)";
  ctx.fillRect(x, pillY, tw, 20);
  ctx.strokeStyle = "#38bdf8";
  ctx.lineWidth = 1.5;
  ctx.strokeRect(x, pillY, tw, 20);
  ctx.fillStyle = "#f0f9ff";
  ctx.fillText(label, x + 7, pillY + 14);
}

async function finalizeDrawnBox(rawRegion) {
  let region = {
    x: Math.round(rawRegion.x),
    y: Math.round(rawRegion.y),
    w: Math.round(rawRegion.w),
    h: Math.round(rawRegion.h)
  };
  const autoSnap = document.getElementById("autoSnapDrawChk") && document.getElementById("autoSnapDrawChk").checked;
  if (autoSnap && currentSessionId && currentPhotoId) {
    try {
      const res = await fetch(`/api/sessions/${encodeURIComponent(currentSessionId)}/refine-box`, {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ photo_id: currentPhotoId, box: region })
      });
      if (res.ok) {
        const data = await res.json();
        if (data.box && data.box.w >= 8 && data.box.h >= 8) {
          region = {
            x: Math.round(data.box.x),
            y: Math.round(data.box.y),
            w: Math.round(data.box.w),
            h: Math.round(data.box.h)
          };
        }
      }
    } catch (e) {}
  }

  const targetItem = redrawTargetItemId;
  toggleDrawMissedMode(false);

  if (targetItem) {
    const res = await fetch(`/api/sessions/${encodeURIComponent(currentSessionId)}/items/${encodeURIComponent(targetItem)}/box`, {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ box: region, snap_to_edges: false })
    });
    if (res.ok) {
      showToast(`✓ Updated bounding box for ${targetItem} (${region.w}×${region.h} px)`);
      await loadSessions(currentSessionId);
    } else {
      showToast("Could not update device box");
    }
    return;
  }

  openMissedDeviceDrawer(region);
}

function setupTouchCanvas() {
  const canvas = document.getElementById("touchOverlay");

  function onPointerDown(clientX, clientY, ev) {
    const pt = clientToPhotoCoords(clientX, clientY);
    if (drawMissedMode) {
      if (ev && ev.cancelable) ev.preventDefault();
      dragStartPt = pt;
      dragCurPt = pt;
      drawDragRect();
      return;
    }
    if (!currentStatus) return;
    for (const u of (currentStatus.unboxed_callouts || [])) {
      if (pt.x >= u.x && pt.x <= u.x + u.w && pt.y >= u.y && pt.y <= u.y + u.h) {
        openMissedDeviceDrawer({ x: u.x, y: u.y, w: u.w, h: u.h }, u.label || "");
        return;
      }
    }
    for (const b of (currentStatus.boxes || [])) {
      const r = b.region || b;
      const bx = Number(r.x || 0), by = Number(r.y || 0), bw = Number(r.w || 0), bh = Number(r.h || 0);
      if (pt.x >= bx && pt.x <= bx + bw && pt.y >= by && pt.y <= by + bh) {
        openDeviceDrawer(b);
        return;
      }
    }
  }

  function onPointerMove(clientX, clientY, ev) {
    if (!drawMissedMode) return;
    if (!dragStartPt && !firstTapCornerPt) return;
    if (ev && ev.cancelable) ev.preventDefault();
    dragCurPt = clientToPhotoCoords(clientX, clientY);
    drawDragRect();
  }

  function onPointerUp(ev) {
    if (!drawMissedMode || !dragStartPt || !dragCurPt) return;
    if (ev && ev.cancelable) ev.preventDefault();
    const dx = Math.abs(dragCurPt.x - dragStartPt.x);
    const dy = Math.abs(dragCurPt.y - dragStartPt.y);

    // If user dragged a full rectangle (>= 12x12 photo px), finalize immediately
    if (dx >= 12 && dy >= 12) {
      const x = Math.min(dragStartPt.x, dragCurPt.x);
      const y = Math.min(dragStartPt.y, dragCurPt.y);
      dragStartPt = null;
      dragCurPt = null;
      firstTapCornerPt = null;
      drawDragRect();
      finalizeDrawnBox({ x, y, w: dx, h: dy });
      return;
    }

    // Otherwise support Two-Tap Corner Mode: 1st tap sets Corner 1, 2nd tap sets opposite Corner 2!
    if (!firstTapCornerPt) {
      firstTapCornerPt = dragStartPt;
      dragStartPt = null;
      dragCurPt = firstTapCornerPt;
      drawDragRect();
      showToast("Corner 1 set — now tap the opposite bottom-right corner of the device!");
    } else {
      const x = Math.min(firstTapCornerPt.x, dragCurPt.x);
      const y = Math.min(firstTapCornerPt.y, dragCurPt.y);
      const w = Math.abs(dragCurPt.x - firstTapCornerPt.x);
      const h = Math.abs(dragCurPt.y - firstTapCornerPt.y);
      dragStartPt = null;
      dragCurPt = null;
      firstTapCornerPt = null;
      drawDragRect();
      if (w >= 10 && h >= 10) {
        finalizeDrawnBox({ x, y, w, h });
      } else {
        showToast("Tap two opposite corners further apart.");
      }
    }
  }

  canvas.addEventListener("mousedown", e => onPointerDown(e.clientX, e.clientY, e));
  canvas.addEventListener("mousemove", e => onPointerMove(e.clientX, e.clientY, e));
  window.addEventListener("mouseup", e => onPointerUp(e));

  canvas.addEventListener("touchstart", e => {
    if (e.touches.length === 1) {
      onPointerDown(e.touches[0].clientX, e.touches[0].clientY, e);
    }
  }, { passive: false });
  canvas.addEventListener("touchmove", e => {
    if (e.touches.length === 1) {
      onPointerMove(e.touches[0].clientX, e.touches[0].clientY, e);
    }
  }, { passive: false });
  canvas.addEventListener("touchend", e => onPointerUp(e), { passive: false });
}

function setDrawerBoxInputs(region) {
  const r = region || { x: 0, y: 0, w: 0, h: 0 };
  document.getElementById("boxX").value = Math.round(Number(r.x || 0));
  document.getElementById("boxY").value = Math.round(Number(r.y || 0));
  document.getElementById("boxW").value = Math.round(Number(r.w || 0));
  document.getElementById("boxH").value = Math.round(Number(r.h || 0));
}

function getDrawerBoxInputs() {
  return {
    x: Number(document.getElementById("boxX").value || 0),
    y: Number(document.getElementById("boxY").value || 0),
    w: Number(document.getElementById("boxW").value || 0),
    h: Number(document.getElementById("boxH").value || 0)
  };
}

async function snapDrawerBoxToEdges() {
  const curBox = getDrawerBoxInputs();
  if (curBox.w < 8 || curBox.h < 8 || !currentSessionId) return;
  if (drawerMode === "edit" && activeItemId) {
    const res = await fetch(`/api/sessions/${encodeURIComponent(currentSessionId)}/items/${encodeURIComponent(activeItemId)}/box`, {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ box: curBox, snap_to_edges: true })
    });
    if (res.ok) {
      const data = await res.json();
      if (data.updated_region) setDrawerBoxInputs(data.updated_region);
      showToast("✨ Snapped bounding box to physical component edges!");
      await loadSessions(currentSessionId);
    }
  } else if (currentPhotoId) {
    const res = await fetch(`/api/sessions/${encodeURIComponent(currentSessionId)}/refine-box`, {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ photo_id: currentPhotoId, box: curBox })
    });
    if (res.ok) {
      const data = await res.json();
      if (data.box) {
        pendingMissedRegion = data.box;
        setDrawerBoxInputs(data.box);
        showToast("✨ Snapped new box to physical component edges!");
      }
    }
  }
}

function startRedrawFromDrawer() {
  const targetId = drawerMode === "edit" ? activeItemId : "";
  closeDeviceDrawer();
  toggleDrawMissedMode(true, targetId);
  showToast(targetId ? `Drag a new tight box for ${targetId} on the photo` : "Drag a box around the missed device on the photo");
}

function openDeviceDrawer(box) {
  drawerMode = "edit";
  activeItemId = box.item_id;
  activeBoxObj = box;
  pendingMissedRegion = null;
  pendingCloseupFile = null;
  document.getElementById("closeupPickedStatus").style.display = "none";
  document.getElementById("drawerTitle").textContent = box.header_label || `Device ${box.item_id}`;
  const alertEl = document.getElementById("drawerMissingAlert");
  if (box.missing_reasons && box.missing_reasons.length > 0) {
    alertEl.style.display = "block";
    alertEl.innerHTML = box.missing_reasons.map(r => `<div>• ${r}</div>`).join("");
  } else {
    alertEl.style.display = "none";
  }
  setDrawerBoxInputs(box.region || box);
  document.getElementById("devTag").value = box.tag || box.placed_ref || "";
  document.getElementById("devBrand").value = box.brand || "";
  document.getElementById("devPart").value = box.part_number || "";
  document.getElementById("devW").value = box.envelope_w_mm || "";
  document.getElementById("devH").value = box.envelope_h_mm || "";
  document.getElementById("devD").value = box.envelope_d_mm || "";
  document.getElementById("devMount").value = box.mount || "din_rail";
  document.getElementById("deviceDrawer").classList.add("open");
}

function openMissedDeviceDrawer(region, hintLabel = "") {
  drawerMode = "missed";
  activeItemId = "";
  activeBoxObj = null;
  pendingMissedRegion = region;
  pendingCloseupFile = null;
  document.getElementById("closeupPickedStatus").style.display = "none";
  document.getElementById("drawerTitle").textContent = `＋ New Missed Device Added (${region.w}×${region.h} px)`;
  const alertEl = document.getElementById("drawerMissingAlert");
  alertEl.style.display = "block";
  alertEl.textContent = hintLabel
    ? `Adding missed device at (${region.x}, ${region.y}): ${hintLabel}`
    : `Box drawn at (${region.x}, ${region.y}) [${region.w}×${region.h} px]. Snap a close-up or enter Tag/Part/Size so you don't leave the site without context.`;
  setDrawerBoxInputs(region);
  document.getElementById("devTag").value = "";
  document.getElementById("devBrand").value = "";
  document.getElementById("devPart").value = "";
  document.getElementById("devW").value = "";
  document.getElementById("devH").value = "";
  document.getElementById("devD").value = "";
  document.getElementById("devMount").value = "din_rail";
  document.getElementById("deviceDrawer").classList.add("open");
}

function closeDeviceDrawer() {
  document.getElementById("deviceDrawer").classList.remove("open");
}

function onCloseupFilePicked(files) {
  if (!files || files.length === 0) return;
  pendingCloseupFile = files[0];
  const st = document.getElementById("closeupPickedStatus");
  st.style.display = "block";
  st.textContent = `✓ Close-up photo ready: ${pendingCloseupFile.name}`;
}

async function saveDeviceDrawer() {
  const tag = document.getElementById("devTag").value.trim();
  const brand = document.getElementById("devBrand").value.trim();
  const part_number = document.getElementById("devPart").value.trim();
  const w = document.getElementById("devW").value.trim();
  const h = document.getElementById("devH").value.trim();
  const d = document.getElementById("devD").value.trim();
  const mount = document.getElementById("devMount").value;
  const editedBox = getDrawerBoxInputs();

  const fd = new FormData();
  fd.append("tag", tag);
  fd.append("brand", brand);
  fd.append("part_number", part_number);
  if (w) fd.append("envelope_w_mm", w);
  if (h) fd.append("envelope_h_mm", h);
  if (d) fd.append("envelope_d_mm", d);
  fd.append("mount", mount);
  if (pendingCloseupFile) {
    fd.append("photo", pendingCloseupFile, pendingCloseupFile.name);
  }

  let url = "";
  if (drawerMode === "missed") {
    fd.append("photo_id", currentPhotoId);
    fd.append("x", String(editedBox.x));
    fd.append("y", String(editedBox.y));
    fd.append("w", String(editedBox.w));
    fd.append("h", String(editedBox.h));
    url = `/api/sessions/${encodeURIComponent(currentSessionId)}/missed-device`;
  } else {
    // Also persist any manual X/Y/W/H box edits made in the drawer
    if (editedBox.w >= 8 && editedBox.h >= 8) {
      await fetch(`/api/sessions/${encodeURIComponent(currentSessionId)}/items/${encodeURIComponent(activeItemId)}/box`, {
        method: "POST",
        headers: apiHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ box: editedBox, snap_to_edges: false })
      });
    }
    url = `/api/sessions/${encodeURIComponent(currentSessionId)}/items/${encodeURIComponent(activeItemId)}/closeup`;
  }

  const res = await fetch(url, { method: "POST", headers: apiHeaders(), body: fd });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    showToast(err.error || "Could not save device");
    return;
  }
  closeDeviceDrawer();
  showToast(drawerMode === "missed" ? "✓ Missed device boxed & saved" : "✓ Device close-up & box saved");
  await loadSessions(currentSessionId);
}

async function uploadCabinetPhoto(files) {
  if (!files || files.length === 0 || !currentSessionId) return;
  const file = files[0];
  const role = document.getElementById("photoRoleSelect").value || "backpanel";
  const fd = new FormData();
  fd.append("role", role);
  fd.append("photo", file, file.name);
  showToast("Uploading & checking site-exit readiness…");
  const res = await fetch(`/api/sessions/${encodeURIComponent(currentSessionId)}/photos`, {
    method: "POST",
    headers: apiHeaders(),
    body: fd
  });
  const data = await res.json();
  if (data.photo_id) currentPhotoId = data.photo_id;
  await loadSessions(currentSessionId);
  showToast("✓ Photo analyzed for Site-Exit Readiness");
}

function openNewSessionDrawer() {
  document.getElementById("newSessionName").value = "";
  document.getElementById("newSessionSite").value = "";
  document.getElementById("newSessionDrawer").classList.add("open");
}

async function createNewSession() {
  const name = document.getElementById("newSessionName").value.trim() || "Field Panel Capture";
  const site_name = document.getElementById("newSessionSite").value.trim();
  const res = await fetch("/api/sessions", {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ name, site_name })
  });
  const data = await res.json();
  document.getElementById("newSessionDrawer").classList.remove("open");
  if (data.session) {
    currentSessionId = data.session.session_id;
    currentPhotoId = "";
    await loadSessions(currentSessionId);
    showToast(`Created session: ${data.session.name}`);
  }
}

function downloadPanelcadFile() {
  if (!currentSessionId) return;
  const tok = getTokenParam();
  const q = tok ? `?token=${encodeURIComponent(tok)}` : "";
  window.location.href = `/api/sessions/${encodeURIComponent(currentSessionId)}/download${q}`;
}

async function sendToAgentWebhook() {
  if (!currentSessionId) return;
  const res = await fetch(`/api/sessions/${encodeURIComponent(currentSessionId)}/notify`, {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({})
  });
  const data = await res.json();
  showToast(data.detail || "Session saved");
}

async function openSetupDrawer() {
  const res = await fetch("/api/server-info", { headers: apiHeaders() });
  const info = await res.json();
  const urls = info.urls || [];
  const primaryUrl = (urls[0] && urls[0].url) || window.location.origin;
  document.getElementById("qrContainer").innerHTML = `<img src="/api/qr.svg?text=${encodeURIComponent(primaryUrl)}" style="width:100%;height:100%;display:block;" alt="QR Code">`;
  const listEl = document.getElementById("reachableUrlList");
  listEl.innerHTML = "";
  for (const u of urls) {
    const row = document.createElement("div");
    row.className = "url-row";
    row.innerHTML = `<span><b style="color:#f8fafc;">${u.label}:</b> ${u.url}</span>`;
    listEl.appendChild(row);
  }
  document.getElementById("cfgWebhookUrl").value = (info.config && info.config.webhook_url) || "";
  document.getElementById("cfgWebhookKey").value = "";
  document.getElementById("setupDrawer").classList.add("open");
}

async function saveServerConfig() {
  const webhook_url = document.getElementById("cfgWebhookUrl").value.trim();
  const webhook_key = document.getElementById("cfgWebhookKey").value.trim();
  const payload = { webhook_url };
  if (webhook_key) payload.webhook_key = webhook_key;
  await fetch("/api/config", {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload)
  });
  document.getElementById("setupDrawer").classList.remove("open");
  showToast("✓ Connection settings saved");
}

setupTouchCanvas();
initApp();
</script>
</body>
</html>
"""


class _CaptureHTTPHandler(BaseHTTPRequestHandler):
    workspace: CaptureWorkspace
    server_port: int = 8788

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _check_auth(self, query: dict[str, list[str]]) -> bool:
        expected = self.workspace.access_token
        if not expected:
            return True
        header_tok = (self.headers.get("X-PanelCAD-Token") or "").strip()
        auth_hdr = (self.headers.get("Authorization") or "").strip()
        if auth_hdr.lower().startswith("bearer "):
            header_tok = header_tok or auth_hdr[7:].strip()
        query_tok = (query.get("token") or [""])[0].strip()
        return expected in {header_tok, query_tok}

    def _send_json(self, status: int, data: Any) -> None:
        raw = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_bytes(
        self,
        status: int,
        raw: bytes,
        content_type: str,
        *,
        disposition: str = "",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(raw)

    def _read_body_payload(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        ctype = (self.headers.get("Content-Type") or "").strip()
        if ctype.lower().startswith("multipart/form-data"):
            fields, files = _parse_multipart_form(ctype, body)
            return fields, files
        if body:
            try:
                parsed = json.loads(body.decode("utf-8"))
                if isinstance(parsed, dict):
                    files_out: list[dict[str, Any]] = []
                    if parsed.get("image_base64"):
                        b64_str = str(parsed["image_base64"])
                        if "," in b64_str:
                            b64_str = b64_str.split(",", 1)[1]
                        files_out.append(
                            {
                                "field": "photo",
                                "filename": _sanitize_filename(str(parsed.get("filename") or "capture.jpg")),
                                "content_type": "image/jpeg",
                                "data": base64.b64decode(b64_str),
                            }
                        )
                    return parsed, files_out
            except Exception:
                pass
        return {}, []

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/manifest.json":
            self._send_json(200, PWA_MANIFEST)
            return

        if path == "/api/health":
            self._send_json(200, {"ok": True, "service": "panelcad-capture-server"})
            return

        if not self._check_auth(query):
            self._send_json(401, {"error": "Unauthorized — valid token required"})
            return

        if path == "/":
            self._send_bytes(200, MOBILE_CAPTURE_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/server-info":
            port = int(getattr(self.server, "server_port", self.server_port))
            urls = discover_reachable_urls(port, access_token=self.workspace.access_token)
            cfg = self.workspace.get_server_config()
            self._send_json(
                200,
                {
                    "port": port,
                    "urls": urls,
                    "workspace": str(self.workspace.root),
                    "config": {
                        "webhook_url": cfg.get("webhook_url") or "",
                        "webhook_configured": bool(cfg.get("webhook_url")),
                        "has_webhook_key": bool(cfg.get("webhook_key")),
                    },
                },
            )
            return

        if path == "/api/qr.svg":
            text = (query.get("text") or ["http://127.0.0.1:8788/"])[0]
            svg = render_qr_svg(text)
            self._send_bytes(200, svg.encode("utf-8"), "image/svg+xml; charset=utf-8")
            return

        if path == "/api/config":
            cfg = self.workspace.get_server_config()
            self._send_json(
                200,
                {
                    "webhook_url": cfg.get("webhook_url") or "",
                    "webhook_configured": bool(cfg.get("webhook_url")),
                    "has_webhook_key": bool(cfg.get("webhook_key")),
                },
            )
            return

        if path == "/api/sessions":
            self._send_json(200, {"sessions": self.workspace.list_sessions()})
            return

        m_status = re.match(r"^/api/sessions/([^/]+)/status$", path)
        if m_status:
            sid = urllib.parse.unquote(m_status.group(1))
            pid = (query.get("photo_id") or [""])[0] or None
            mode = (query.get("mode") or ["all"])[0]
            try:
                st = self.workspace.get_status(sid, photo_id=pid, filter_mode=mode)
                self._send_json(200, st)
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            return

        m_png = re.match(r"^/api/sessions/([^/]+)/annotated\.png$", path)
        if m_png:
            sid = urllib.parse.unquote(m_png.group(1))
            pid = (query.get("photo_id") or [""])[0] or None
            mode = (query.get("mode") or ["all"])[0]
            selected_id = (query.get("selected_item_id") or [""])[0] or None
            try:
                png_bytes = self.workspace.render_annotated_png(
                    sid,
                    photo_id=pid,
                    filter_mode=mode,
                    selected_item_id=selected_id,
                )
                self._send_bytes(200, png_bytes, "image/png")
            except (KeyError, ValueError) as exc:
                self._send_json(404, {"error": str(exc)})
            return

        m_raw = re.match(r"^/api/sessions/([^/]+)/photos/([^/]+)/raw$", path)
        if m_raw:
            sid = urllib.parse.unquote(m_raw.group(1))
            pid = urllib.parse.unquote(m_raw.group(2))
            try:
                img_bytes, mime = self.workspace.render_raw_photo_bytes(sid, pid)
                self._send_bytes(200, img_bytes, mime)
            except (KeyError, FileNotFoundError) as exc:
                self._send_json(404, {"error": str(exc)})
            return

        m_dl = re.match(r"^/api/sessions/([^/]+)/download$", path)
        if m_dl:
            sid = urllib.parse.unquote(m_dl.group(1))
            try:
                self.workspace.save_session(sid)
                sess = self.workspace.get_session(sid)
                proj_path = Path(sess.path) if sess.path else self.workspace._project_path(sid)
                raw = proj_path.read_bytes()
                self._send_bytes(
                    200,
                    raw,
                    "application/octet-stream",
                    disposition=f'attachment; filename="{proj_path.name}"',
                )
            except (KeyError, OSError) as exc:
                self._send_json(404, {"error": str(exc)})
            return

        self._send_json(404, {"error": f"Not found: {path}"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if not self._check_auth(query):
            self._send_json(401, {"error": "Unauthorized — valid token required"})
            return

        fields, files = self._read_body_payload()

        if path == "/api/config":
            res = self.workspace.update_server_config(
                webhook_url=fields.get("webhook_url"),
                webhook_key=fields.get("webhook_key"),
            )
            self._send_json(200, {"ok": True, "config": res})
            return

        if path == "/api/sessions":
            name = str(fields.get("name") or "Field Panel Capture").strip()
            sid = str(fields.get("session_id") or "").strip()
            site = str(fields.get("site_name") or "").strip()
            summary = self.workspace.create_or_get_session(name, session_id=sid, site_name=site)
            self._send_json(200, {"ok": True, "session": summary})
            return

        m_photos = re.match(r"^/api/sessions/([^/]+)/photos$", path)
        if m_photos:
            sid = urllib.parse.unquote(m_photos.group(1))
            if not files:
                self._send_json(400, {"error": "No photo uploaded"})
                return
            file_info = files[0]
            role = str(fields.get("role") or "backpanel").strip()
            note = str(fields.get("note") or "").strip()
            analyze = str(fields.get("analyze", "true")).lower() not in {"0", "false", "no"}
            try:
                st = self.workspace.add_uploaded_photo(
                    sid,
                    filename=file_info["filename"],
                    image_bytes=file_info["data"],
                    role=role,
                    note=note,
                    analyze=analyze,
                )
                self._send_json(200, st)
            except (KeyError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
            return

        m_missed = re.match(r"^/api/sessions/([^/]+)/missed-device$", path)
        if m_missed:
            sid = urllib.parse.unquote(m_missed.group(1))
            region_obj = fields.get("region") if isinstance(fields.get("region"), dict) else {}
            rx = float(region_obj.get("x", fields.get("x", 0.0)))
            ry = float(region_obj.get("y", fields.get("y", 0.0)))
            rw = float(region_obj.get("w", fields.get("w", 0.0)))
            rh = float(region_obj.get("h", fields.get("h", 0.0)))
            pid = str(fields.get("photo_id") or "").strip()
            if not pid:
                st_cur = self.workspace.get_status(sid)
                pid = str(st_cur.get("photo_id") or "")
            closeup_bytes = files[0]["data"] if files else None
            closeup_name = files[0]["filename"] if files else ""
            try:
                st = self.workspace.add_missed_device(
                    sid,
                    photo_id=pid,
                    region={"x": rx, "y": ry, "w": rw, "h": rh},
                    tag=str(fields.get("tag") or ""),
                    part_number=str(fields.get("part_number") or ""),
                    brand=str(fields.get("brand") or ""),
                    label_text=str(fields.get("label_text") or ""),
                    envelope_w_mm=_opt_float(fields.get("envelope_w_mm")),
                    envelope_h_mm=_opt_float(fields.get("envelope_h_mm")),
                    envelope_d_mm=_opt_float(fields.get("envelope_d_mm")),
                    mount=str(fields.get("mount") or ""),
                    closeup_bytes=closeup_bytes,
                    closeup_filename=closeup_name,
                    snap_to_edges=str(fields.get("snap_to_edges", "")).lower() in {"1", "true", "yes"},
                )
                self._send_json(200, st)
            except (KeyError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
            return

        m_closeup = re.match(r"^/api/sessions/([^/]+)/items/([^/]+)/closeup$", path)
        if m_closeup:
            sid = urllib.parse.unquote(m_closeup.group(1))
            iid = urllib.parse.unquote(m_closeup.group(2))
            closeup_bytes = files[0]["data"] if files else None
            closeup_name = files[0]["filename"] if files else ""
            try:
                st = self.workspace.link_closeup(
                    sid,
                    iid,
                    closeup_bytes=closeup_bytes,
                    closeup_filename=closeup_name,
                    closeup_photo_id=str(fields.get("closeup_photo_id") or ""),
                    tag=str(fields.get("tag") or ""),
                    part_number=str(fields.get("part_number") or ""),
                    brand=str(fields.get("brand") or ""),
                    envelope_w_mm=_opt_float(fields.get("envelope_w_mm")),
                    envelope_h_mm=_opt_float(fields.get("envelope_h_mm")),
                    envelope_d_mm=_opt_float(fields.get("envelope_d_mm")),
                    mount=str(fields.get("mount") or ""),
                    library_id=str(fields.get("library_id") or ""),
                )
                self._send_json(200, st)
            except (KeyError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
            return

        m_box = re.match(r"^/api/sessions/([^/]+)/items/([^/]+)/box$", path)
        if m_box:
            sid = urllib.parse.unquote(m_box.group(1))
            iid = urllib.parse.unquote(m_box.group(2))
            box_obj = fields.get("box") if isinstance(fields.get("box"), dict) else (fields.get("region") if isinstance(fields.get("region"), dict) else None)
            if box_obj is None and any(k in fields for k in ("x", "y", "w", "h")):
                box_obj = {
                    "x": float(fields.get("x", 0.0)),
                    "y": float(fields.get("y", 0.0)),
                    "w": float(fields.get("w", 0.0)),
                    "h": float(fields.get("h", 0.0)),
                }
            snap = str(fields.get("snap_to_edges", "")).lower() in {"1", "true", "yes"}
            try:
                st = self.workspace.update_device_box(
                    sid,
                    iid,
                    box=box_obj,
                    snap_to_edges=snap,
                )
                self._send_json(200, st)
            except (KeyError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
            return

        m_refine = re.match(r"^/api/sessions/([^/]+)/refine-box$", path)
        if m_refine:
            sid = urllib.parse.unquote(m_refine.group(1))
            pid = str(fields.get("photo_id") or "").strip()
            if not pid:
                st_cur = self.workspace.get_status(sid)
                pid = str(st_cur.get("photo_id") or "")
            box_obj = fields.get("box") if isinstance(fields.get("box"), dict) else (fields.get("region") if isinstance(fields.get("region"), dict) else {
                "x": float(fields.get("x", 0.0)),
                "y": float(fields.get("y", 0.0)),
                "w": float(fields.get("w", 0.0)),
                "h": float(fields.get("h", 0.0)),
            })
            try:
                res = self.workspace.refine_box(sid, photo_id=pid, box=box_obj)
                self._send_json(200, res)
            except (KeyError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
            return

        m_notify = re.match(r"^/api/sessions/([^/]+)/notify$", path)
        if m_notify:
            sid = urllib.parse.unquote(m_notify.group(1))
            note = str(fields.get("note") or "")
            try:
                res = self.workspace.notify_webhook(sid, note=note)
                self._send_json(200, res)
            except KeyError as exc:
                self._send_json(404, {"error": str(exc)})
            return

        self._send_json(404, {"error": f"Not found: {path}"})


class CaptureWebServer:
    """Lifecycle wrapper around ThreadingHTTPServer for PanelCAD Mobile Field Capture."""

    def __init__(
        self,
        workspace_dir: Path | str,
        *,
        host: str = "0.0.0.0",
        port: int = 8788,
        webhook_url: str = "",
        webhook_key: str = "",
        access_token: str = "",
        live_session: Session | None = None,
        on_change: Any = None,
    ) -> None:
        self.workspace = CaptureWorkspace(
            workspace_dir,
            webhook_url=webhook_url,
            webhook_key=webhook_key,
            access_token=access_token,
            on_change=on_change,
        )
        self.live_session_id = ""
        if live_session is not None:
            self.live_session_id = self.workspace.attach_live_session(live_session)
        self.host = host
        self.requested_port = int(port)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._httpd is not None:
            return int(self._httpd.server_port)
        return self.requested_port

    @property
    def base_url(self) -> str:
        connect_host = "127.0.0.1" if self.host in {"0.0.0.0", "::", ""} else self.host
        return f"http://{connect_host}:{self.port}"

    def reachable_urls(self) -> list[dict[str, str]]:
        return discover_reachable_urls(self.port, access_token=self.workspace.access_token)

    def start(self, *, daemon: bool = True) -> CaptureWebServer:
        if self._httpd is not None:
            return self
        ws = self.workspace

        class BoundHandler(_CaptureHTTPHandler):
            workspace = ws

        self._httpd = ThreadingHTTPServer((self.host, self.requested_port), BoundHandler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="panelcad-capture-web",
            daemon=daemon,
        )
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        if self._httpd is None:
            ws = self.workspace

            class BoundHandler(_CaptureHTTPHandler):
                workspace = ws

            self._httpd = ThreadingHTTPServer((self.host, self.requested_port), BoundHandler)
        self._httpd.serve_forever()

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
