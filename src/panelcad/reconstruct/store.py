"""Copy panel photos and secondary prints next to the project file."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

from panelcad.reconstruct.models import PHOTO_EXTS, Photo, PrintSource, Reconstruction

PHOTO_DIR = "photos"
PRINT_DIR = "prints"
EMBED_KEY = "panelcad_reconstruct"


def load_upright_image(path: Path | str):
    """Decode an image honoring EXIF orientation."""

    from PySide6.QtGui import QImage, QImageReader

    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        image = QImage(str(path))
    return image


def image_size(path: Path) -> tuple[int, int]:
    image = load_upright_image(path)
    if image.isNull():
        return (0, 0)
    return (int(image.width()), int(image.height()))


def stored_suffix(src: Path) -> str:
    name = src.name.lower()
    if name.endswith(".pcad.yaml"):
        return ".pcad.yaml"
    return src.suffix.lower() or ".png"


def copy_into(
    src: Path,
    dest_dir: Path,
    stem: str,
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{stem}{stored_suffix(src)}"
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return dest


def relocate(
    store: Reconstruction,
    project_path: Path,
    *,
    previous: Path | None = None,
) -> None:
    """Make photo and print paths relative after save / Save As."""

    root = project_path.parent
    previous_root = previous.parent if previous is not None else None
    for photo in store.photos:
        if not resolve_path(photo.path, project_path).is_file():
            hint = photo.resolved_path or photo.source_path
            if hint and Path(hint).is_file():
                photo.path = hint
        photo.path = _relocate_one(photo.path, root / PHOTO_DIR, photo.id, root, previous_root)
        here = resolve_path(photo.path, project_path)
        photo.source_path = ""
        if here.is_file():
            photo.resolved_path = str(here.resolve())
    for item in store.prints:
        item.path = _relocate_one(item.path, root / PRINT_DIR, item.id, root, previous_root)


def resolve_path(stored: str, project_path: Path | None) -> Path:
    raw = Path(stored)
    if raw.is_file():
        return raw
    if project_path is not None:
        alt = project_path.parent / stored
        if alt.is_file():
            return alt
    return raw


def recover_photos(store: Reconstruction, project_path: Path | None, *, original_root: str = "") -> None:
    """Remember a recovered photo path in memory. Copy happens on the next save."""

    if store is None or project_path is None:
        return
    origin = Path(original_root) if original_root else None
    for photo in store.photos:
        here = resolve_path(photo.path, project_path)
        if here.is_file():
            photo.resolved_path = str(here.resolve())
            continue
        candidates: list[Path] = []
        if photo.resolved_path:
            candidates.append(Path(photo.resolved_path))
        if photo.source_path:
            candidates.append(Path(photo.source_path))
        if origin is not None:
            candidates.append(origin / photo.path)
            candidates.append(origin / Path(photo.path).name)
            candidates.append(origin / PHOTO_DIR / f"{photo.id}{Path(photo.path).suffix}")
        for src in candidates:
            if not src.is_file():
                continue
            photo.resolved_path = str(src.resolve())
            break


def next_id(prefix: str, existing: list[str]) -> str:
    used = set(existing)
    index = 1
    while f"{prefix}{index}" in used:
        index += 1
    return f"{prefix}{index}"


def _relocate_one(
    stored: str,
    dest_dir: Path,
    stem: str,
    root: Path,
    previous_root: Path | None,
) -> str:
    src = Path(stored)
    if not src.is_file():
        candidates = [root / stored]
        if previous_root is not None:
            candidates.append(previous_root / stored)
            candidates.append(previous_root / Path(stored).name)
        for alt in candidates:
            if alt.is_file():
                src = alt
                break
        else:
            return stored
    dest = copy_into(src, dest_dir, stem)
    try:
        return dest.relative_to(root).as_posix()
    except ValueError:
        return dest.as_posix()


def is_photo_file(path: Path) -> bool:
    return path.suffix.lower() in PHOTO_EXTS


def sidecar_path(project_path: Path) -> Path:
    name = project_path.name
    if name.endswith(".pcad.yaml"):
        stem = name[: -len(".pcad.yaml")]
    else:
        stem = project_path.stem
    return project_path.with_name(f"{stem}.reconstruct.yaml")


def embed_reconstruct(project: Any) -> None:
    """Copy reconstruct + inferred wires into project.properties so main Save As cannot drop them."""

    store = getattr(project, "reconstruct", None)
    props = getattr(project, "properties", None)
    if props is None:
        project.properties = {}
        props = project.properties
    if store is None:
        props.pop(EMBED_KEY, None)
        return
    inferred = []
    for sheet in getattr(project, "sheets", []) or []:
        for wire in getattr(sheet, "wires", []) or []:
            if getattr(wire, "inferred", False):
                inferred.append(
                    {"sheet": sheet.id, "id": wire.id, "number": wire.number, "from": wire.frm, "to": wire.to}
                )
    dumped = store.model_dump(mode="json")
    for photo in dumped.get("photos") or []:
        if isinstance(photo, dict):
            photo.pop("source_path", None)
            photo.pop("resolved_path", None)
    props[EMBED_KEY] = {
        "format": "panelcad-reconstruct",
        "version": 1,
        "reconstruct": dumped,
        "inferred_wires": inferred,
    }


def apply_embedded(project: Any, project_path: Path | None = None) -> None:
    props = getattr(project, "properties", None) or {}
    data = props.get(EMBED_KEY)
    if not isinstance(data, dict):
        return
    raw = data.get("reconstruct")
    if getattr(project, "reconstruct", None) is None and raw:
        from panelcad.reconstruct.models import Reconstruction

        project.reconstruct = Reconstruction.model_validate(raw)
    if project_path is not None and getattr(project, "reconstruct", None) is not None:
        recover_photos(project.reconstruct, project_path, original_root=str(data.get("original_root") or ""))
    for row in data.get("inferred_wires") or []:
        if not isinstance(row, dict):
            continue
        wire_id = str(row.get("id") or "")
        sheet_id = str(row.get("sheet") or "")
        for sheet in getattr(project, "sheets", []) or []:
            if sheet_id and sheet.id != sheet_id:
                continue
            for wire in getattr(sheet, "wires", []) or []:
                if wire.id == wire_id or (
                    row.get("number") and wire.number == row.get("number") and wire.frm == row.get("from")
                ):
                    wire.inferred = True


def write_sidecar(project: Any, project_path: Path) -> Path | None:
    store = getattr(project, "reconstruct", None)
    if store is None:
        return None
    inferred = []
    for sheet in getattr(project, "sheets", []) or []:
        for wire in getattr(sheet, "wires", []) or []:
            if getattr(wire, "inferred", False):
                inferred.append({"sheet": sheet.id, "id": wire.id, "number": wire.number, "from": wire.frm, "to": wire.to})
    dest = sidecar_path(project_path)
    dest.write_text(
        yaml.safe_dump(
            {
                "format": "panelcad-reconstruct",
                "version": 1,
                "reconstruct": store.model_dump(mode="json"),
                "inferred_wires": inferred,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return dest


def load_sidecar(project_path: Path) -> dict[str, Any] | None:
    src = sidecar_path(project_path)
    if not src.is_file():
        return None
    data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else None


def apply_sidecar(project: Any, project_path: Path) -> None:
    apply_embedded(project, project_path)
    data = load_sidecar(project_path)
    if not data:
        store = getattr(project, "reconstruct", None)
        if store is not None:
            recover_photos(store, project_path)
        return
    store = getattr(project, "reconstruct", None)
    raw = data.get("reconstruct")
    if store is None and raw:
        from panelcad.reconstruct.models import Reconstruction

        project.reconstruct = Reconstruction.model_validate(raw)
    store = getattr(project, "reconstruct", None)
    if store is not None:
        recover_photos(store, project_path, original_root=str(data.get("original_root") or ""))
    for row in data.get("inferred_wires") or []:
        if not isinstance(row, dict):
            continue
        wire_id = str(row.get("id") or "")
        sheet_id = str(row.get("sheet") or "")
        for sheet in getattr(project, "sheets", []) or []:
            if sheet_id and sheet.id != sheet_id:
                continue
            for wire in getattr(sheet, "wires", []) or []:
                if wire.id == wire_id or (row.get("number") and wire.number == row.get("number") and wire.frm == row.get("from")):
                    wire.inferred = True


def public_paths(store: Reconstruction) -> dict[str, Any]:
    return {
        "photos": [Photo.model_dump(item) if hasattr(item, "model_dump") else item for item in store.photos],
        "prints": [PrintSource.model_dump(item) if hasattr(item, "model_dump") else item for item in store.prints],
    }
