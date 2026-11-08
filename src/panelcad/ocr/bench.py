"""Compare the electrical engine with Tesseract and Paddle OCR.

Run this on MichellesPC (shop) or DESKTOP-3S1HOH0 (home). Do not run it on
Drew-PC or the Pixel 6 Pro. A result from any other machine, including a
Cursor cloud VM, is not a shop-PC or home-PC number.

Shop or home, after the usual bench setup (see the module docstring of
``main``)::

    cd %USERPROFILE%\\panelcad-test\\src-<rev>
    %USERPROFILE%\\panelcad-test\\venv\\Scripts\\python.exe scripts\\bench_electrical_ocr.py --out %USERPROFILE%\\panelcad-test\\ocr-bench.json

The electrical engine needs the PanelCAD install only (Pillow, OpenCV, numpy).
Tesseract and Paddle are optional baselines. If either is missing, the JSON
says so and the other engines still run. This script does not invent a score.

The fair accuracy number is ``held_out_sample_recall``. Those pages are drawn
with a stick alphabet and Pillow's built-in font, not with DejaVu.
``in_bank_sample_recall`` is the same-font ceiling and is not the comparison.

Paddle is run with oneDNN/MKLDNN off (``FLAGS_use_mkldnn=0`` and
``enable_mkldnn=False``). The JSON records on-disk bytes for each engine.
Runtime memory is left null: Tesseract is a separate process, so one
resident-set counter would not cover it the same way as the in-process
engines. A missing size is null. It is not filled in.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np

from panelcad.ocr.electrical import ElectricalOcr
from panelcad.ocr.samples import OcrSample, electrical_samples

_LOCAL = {
    "MICHELLESPC": "michelles-pc",
    "DESKTOP-3S1HOH0": "home-pc",
}
_NOT_TARGETS = ("Drew-PC", "Pixel 6 Pro")


def machine_role(hostname: str | None = None) -> str:
    name = (hostname if hostname is not None else platform.node()).strip().upper()
    return _LOCAL.get(name, "other")


def counts_as_local_target(hostname: str | None = None) -> bool:
    return machine_role(hostname) in {"michelles-pc", "home-pc"}


def run_benchmark() -> dict[str, Any]:
    samples = electrical_samples()
    engines = {
        "electrical": _run_electrical(samples),
        "tesseract": _run_tesseract(samples, rotations=(0,)),
        "tesseract_four_angles": _run_tesseract(samples, rotations=(0, 90, 180, 270)),
        "paddle": _run_paddle(samples),
    }
    hostname = platform.node()
    role = machine_role(hostname)
    local = {
        "MichellesPC": None,
        "DESKTOP-3S1HOH0": None,
    }
    payload = {
        "measured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hostname": hostname,
        "machine_role": role,
        "counts_as_local_target": role != "other",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "local_targets": {
            "MichellesPC": "Shop PC. Leave null until this script is run there.",
            "DESKTOP-3S1HOH0": "Home PC. Leave null until this script is run there.",
        },
        "not_targets": list(_NOT_TARGETS),
        "local_results": local,
        "samples": [
            {
                "name": sample.name,
                "category": sample.category,
                "truths": list(sample.truths),
                "note": sample.note,
                "in_bank": sample.in_bank,
            }
            for sample in samples
        ],
        "size": measure_footprints(),
        "engines": engines,
        "how_to_read": (
            "held_out_sample_recall is the fair accuracy number: pages not drawn "
            "with a prototype font. in_bank_sample_recall uses DejaVu, which is in "
            "the glyph bank, and is only a same-font check. "
            "size.footprint_bytes is a sum of files this process could stat. "
            "Null means that path was not on disk. "
            "runtime_memory is null: Tesseract is a separate process, so resident "
            "set was not compared. "
            "A machine_role of other is not a MichellesPC or DESKTOP-3S1HOH0 result."
        ),
    }
    _attach_memory(payload)
    _attach_size(payload)
    if role == "michelles-pc":
        local["MichellesPC"] = _public_scores(engines)
    elif role == "home-pc":
        local["DESKTOP-3S1HOH0"] = _public_scores(engines)
    return payload


def _public_scores(engines: dict[str, Any]) -> dict[str, Any]:
    scores = {}
    for name, result in engines.items():
        if not result.get("available"):
            scores[name] = None
            continue
        scores[name] = {
            "held_out_sample_recall": result.get("held_out_sample_recall"),
            "held_out_token_recall": result.get("held_out_token_recall"),
            "in_bank_sample_recall": result.get("in_bank_sample_recall"),
            "seconds": result.get("seconds"),
            "footprint_bytes": (result.get("size") or {}).get("footprint_bytes"),
            "added_rss_bytes": (result.get("runtime_memory") or {}).get("added_rss_bytes"),
        }
    return scores


def _run_electrical(samples: list[OcrSample]) -> dict[str, Any]:
    engine = ElectricalOcr()

    def read(sample: OcrSample) -> list[str]:
        spans = engine.recognize(sample.image, psm=11, rotations=(0, 90, 180, 270))
        return [span.text for span in spans]

    return _score("electrical", samples, read, available=True, reason="")


def _run_tesseract(samples: list[OcrSample], *, rotations: tuple[int, ...]) -> dict[str, Any]:
    from panelcad.importer.raster import find_tesseract

    name = "tesseract" if rotations == (0,) else "tesseract_four_angles"
    binary = find_tesseract() or shutil.which("tesseract") or ""
    if not binary:
        return _score(name, samples, None, available=False, reason="tesseract binary not found")
    import pytesseract

    from panelcad.importer.raster import configure_tesseract

    configure_tesseract()

    def read(sample: OcrSample) -> list[str]:
        texts: list[str] = []
        for rotation in rotations:
            view = _rotate(sample.image, rotation)
            data = pytesseract.image_to_data(view, output_type=pytesseract.Output.DICT, config="--psm 11")
            for raw, conf in zip(data.get("text") or [], data.get("conf") or []):
                token = str(raw or "").strip()
                try:
                    score = float(conf)
                except (TypeError, ValueError):
                    continue
                if token and score >= 0:
                    texts.append(token)
        return texts

    try:
        return _score(name, samples, read, available=True, reason="")
    except Exception as exc:  # noqa: BLE001 — a baseline that cannot start is a recorded skip
        return _score(name, samples, None, available=False, reason=f"{type(exc).__name__}: {exc}")


def _quiet_paddle() -> None:
    """Paddle 3.3 CPU defaults to oneDNN, which raised NotImplementedError here."""

    os.environ["FLAGS_use_mkldnn"] = "0"
    os.environ["FLAGS_enable_mkldnn"] = "0"
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")


def _run_paddle(samples: list[OcrSample]) -> dict[str, Any]:
    _quiet_paddle()
    try:
        from paddleocr import PaddleOCR
    except Exception as exc:  # noqa: BLE001 — missing optional baseline
        return _score(
            "paddle",
            samples,
            None,
            available=False,
            reason=f"paddleocr is not installed ({type(exc).__name__}: {exc})",
        )
    try:
        reader = _paddle_reader(PaddleOCR)
    except Exception as exc:  # noqa: BLE001
        return _score("paddle", samples, None, available=False, reason=f"{type(exc).__name__}: {exc}")

    def read(sample: OcrSample) -> list[str]:
        return _paddle_texts(reader, sample.image)

    try:
        scored = _score("paddle", samples, read, available=True, reason="")
    except Exception as exc:  # noqa: BLE001
        scored = _score("paddle", samples, None, available=False, reason=f"{type(exc).__name__}: {exc}")
    scored["cpu_path"] = "FLAGS_use_mkldnn=0; PaddleOCR(lang='en', enable_mkldnn=False, device='cpu')"
    return scored


def _paddle_reader(cls: Any) -> Any:
    attempts = (
        {
            "lang": "en",
            "device": "cpu",
            "enable_mkldnn": False,
            "enable_cinn": False,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": True,
        },
        {"lang": "en", "device": "cpu", "enable_mkldnn": False},
        {"use_angle_cls": True, "lang": "en", "use_gpu": False},
        {"lang": "en"},
    )
    last = "PaddleOCR could not be constructed"
    for kwargs in attempts:
        try:
            return cls(**kwargs)
        except Exception as exc:  # noqa: BLE001 — try the next constructor the installed version accepts
            last = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(last)


def _paddle_texts(reader: Any, image: np.ndarray) -> list[str]:
    # PaddleOCR 3 still has ocr(), and it warns to use predict(). The old
    # parser drops predict()'s rec_texts, which made every sample look blank.
    if hasattr(reader, "predict"):
        return _texts_from_paddle_v3(reader.predict(image))
    if hasattr(reader, "ocr"):
        try:
            result = reader.ocr(image, cls=True)
        except TypeError:
            result = reader.ocr(image)
        return _texts_from_paddle_v2(result)
    raise RuntimeError("PaddleOCR has neither ocr nor predict")


def _texts_from_paddle_v2(result: Any) -> list[str]:
    texts: list[str] = []
    for page in result or []:
        if not page:
            continue
        for item in page:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            heard = item[1]
            if isinstance(heard, (list, tuple)) and heard:
                texts.append(str(heard[0]))
            elif isinstance(heard, str):
                texts.append(heard)
    return texts


def _texts_from_paddle_v3(result: Any) -> list[str]:
    texts: list[str] = []
    pages = result if isinstance(result, list) else [result]
    for page in pages:
        rec = None
        if isinstance(page, dict):
            rec = page.get("rec_texts")
        if rec is None:
            rec = getattr(page, "rec_texts", None)
        if isinstance(rec, list):
            texts.extend(str(item) for item in rec)
    return texts


def _rotate(image: np.ndarray, rotation: int) -> np.ndarray:
    import cv2

    if rotation == 90:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if rotation == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    if rotation == 270:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    return image


def _score(
    name: str,
    samples: list[OcrSample],
    read: Any,
    *,
    available: bool,
    reason: str,
) -> dict[str, Any]:
    if not available or read is None:
        return {
            "name": name,
            "available": False,
            "reason": reason,
            "sample_recall": None,
            "token_recall": None,
            "seconds": None,
            "samples": [],
        }
    rows = []
    started = time.perf_counter()
    token_hit = 0
    token_total = 0
    sample_hit = 0
    for sample in samples:
        t0 = time.perf_counter()
        try:
            emitted = [str(item) for item in read(sample)]
            error = ""
        except Exception as exc:  # noqa: BLE001 — one sample must not erase the rest
            emitted = []
            error = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - t0
        pieces: list[str] = []
        for item in emitted:
            pieces.extend(str(item).split())
        got = {_norm(item) for item in pieces}
        got.discard("")
        truths = [_norm(item) for item in sample.truths]
        hits = [item in got for item in truths]
        token_hit += sum(1 for item in hits if item)
        token_total += len(truths)
        if truths and all(hits):
            sample_hit += 1
        rows.append(
            {
                "name": sample.name,
                "category": sample.category,
                "in_bank": sample.in_bank,
                "truths": list(sample.truths),
                "emitted": emitted,
                "token_hits": hits,
                "hit": bool(truths) and all(hits),
                "seconds": round(elapsed, 4),
                "error": error,
            }
        )
    elapsed = time.perf_counter() - started
    # A crash on every sample is not a recall of zero. There was no measurement.
    held_recall, held_tokens, held_seconds = _cohort(rows, in_bank=False)
    bank_recall, bank_tokens, bank_seconds = _cohort(rows, in_bank=True)
    if rows and all(row["error"] for row in rows) and not any(row["emitted"] for row in rows):
        return {
            "name": name,
            "available": False,
            "reason": rows[0]["error"],
            "sample_recall": None,
            "token_recall": None,
            "held_out_sample_recall": None,
            "held_out_token_recall": None,
            "in_bank_sample_recall": None,
            "in_bank_token_recall": None,
            "seconds": round(elapsed, 4),
            "samples": rows,
        }
    return {
        "name": name,
        "available": True,
        "reason": "",
        "sample_recall": round(sample_hit / len(samples), 4) if samples else None,
        "token_recall": round(token_hit / token_total, 4) if token_total else None,
        "held_out_sample_recall": held_recall,
        "held_out_token_recall": held_tokens,
        "held_out_seconds": held_seconds,
        "in_bank_sample_recall": bank_recall,
        "in_bank_token_recall": bank_tokens,
        "in_bank_seconds": bank_seconds,
        "sample_hits": sample_hit,
        "sample_count": len(samples),
        "token_hits": token_hit,
        "token_count": token_total,
        "seconds": round(elapsed, 4),
        "samples": rows,
    }


def _cohort(rows: list[dict[str, Any]], *, in_bank: bool) -> tuple[float | None, float | None, float | None]:
    subset = [row for row in rows if row.get("in_bank") is in_bank]
    if not subset:
        return None, None, None
    sample_hit = sum(1 for row in subset if row["hit"])
    token_hit = sum(sum(1 for hit in row["token_hits"] if hit) for row in subset)
    token_total = sum(len(row["token_hits"]) for row in subset)
    seconds = round(sum(row["seconds"] for row in subset), 4)
    return (
        round(sample_hit / len(subset), 4),
        round(token_hit / token_total, 4) if token_total else None,
        seconds,
    )


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(text).upper())


def _dir_bytes(path: Path) -> int | None:
    if not path.exists():
        return None
    total = 0
    if path.is_file():
        return path.stat().st_size
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def _package_dir(module_name: str) -> Path | None:
    try:
        module = __import__(module_name)
    except Exception:  # noqa: BLE001 — a missing baseline has no directory to measure
        return None
    file = getattr(module, "__file__", None)
    if not file:
        return None
    return Path(file).resolve().parent


def measure_footprints() -> dict[str, Any]:
    """On-disk bytes this machine can stat. Missing paths stay null."""

    return {
        "electrical": _electrical_footprint(),
        "tesseract": _tesseract_footprint(),
        "paddle": _paddle_footprint(),
        "note": (
            "footprint_bytes sums only the paths listed. "
            "OpenCV, Pillow, and numpy are already part of PanelCAD and are not added to any engine. "
            "A null byte count means that path was not found, not that the size is zero unless model_bytes is 0."
        ),
    }


def _electrical_footprint() -> dict[str, Any]:
    root = Path(__file__).resolve().parent
    package = _dir_bytes(root)
    return {
        "footprint_bytes": package,
        "package_bytes": package,
        "model_bytes": 0,
        "paths": [str(root)],
        "note": "Python sources for panelcad.ocr. No separate model file.",
    }


def _tesseract_footprint() -> dict[str, Any]:
    from panelcad.importer.raster import find_tesseract

    binary = find_tesseract() or shutil.which("tesseract") or ""
    if not binary:
        return {
            "footprint_bytes": None,
            "binary_bytes": None,
            "tessdata_bytes": None,
            "library_bytes": None,
            "paths": [],
            "note": "tesseract binary not found",
        }
    binary_path = Path(binary)
    binary_bytes = binary_path.stat().st_size if binary_path.is_file() else None
    tessdata = _tessdata_dir(binary_path)
    tessdata_bytes = _dir_bytes(tessdata) if tessdata is not None else None
    libraries = _tesseract_libraries(binary_path)
    library_bytes = sum(item["bytes"] for item in libraries) if libraries else 0
    install = _tesseract_install_dir(binary_path, tessdata)
    paths = [str(binary_path)]
    if tessdata is not None:
        paths.append(str(tessdata))
    paths.extend(item["path"] for item in libraries)
    if install is not None and _contains(install, binary_path) and (tessdata is None or _contains(install, tessdata)):
        footprint = _dir_bytes(install)
        note = "Whole Tesseract install directory, which already contains the binary and tessdata."
        paths = [str(install)]
    else:
        parts = [item for item in (binary_bytes, tessdata_bytes, library_bytes) if item is not None]
        footprint = sum(parts) if parts else None
        note = "Binary, tessdata directory, and shared libraries named by ldd. System directories are not included."
    return {
        "footprint_bytes": footprint,
        "binary_bytes": binary_bytes,
        "tessdata_bytes": tessdata_bytes,
        "library_bytes": library_bytes,
        "install_directory_bytes": _dir_bytes(install) if install is not None else None,
        "paths": paths,
        "note": note,
    }


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _tesseract_install_dir(binary: Path, tessdata: Path | None) -> Path | None:
    parent = binary.resolve().parent
    if parent.name.lower() in {"tesseract-ocr", "tesseract"}:
        return parent
    if tessdata is not None and tessdata.parent.name.lower() in {"tesseract-ocr", "tesseract"}:
        return tessdata.parent
    return None


def _tessdata_dir(binary: Path) -> Path | None:
    prefix = os.environ.get("TESSDATA_PREFIX", "").strip()
    candidates = []
    if prefix:
        candidates.append(Path(prefix))
        candidates.append(Path(prefix) / "tessdata")
    candidates.extend(
        [
            binary.parent / "tessdata",
            binary.parent.parent / "share" / "tessdata",
            binary.parent.parent / "share" / "tesseract-ocr" / "5" / "tessdata",
            Path("/usr/share/tesseract-ocr/5/tessdata"),
            Path("/usr/share/tesseract-ocr/4.00/tessdata"),
            Path("/usr/share/tessdata"),
        ]
    )
    for candidate in candidates:
        if (candidate / "eng.traineddata").is_file():
            return candidate
    return None


def _tesseract_libraries(binary: Path) -> list[dict[str, Any]]:
    if os.name != "posix" or not shutil.which("ldd"):
        return []
    try:
        completed = subprocess.run(
            ["ldd", str(binary)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    found: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        if "=>" not in line:
            continue
        _soname, _arrow, rest = line.partition("=>")
        path_text = rest.strip().split()[0] if rest.strip() else ""
        if path_text in {"", "not"}:
            continue
        lower = path_text.lower()
        if "tesseract" not in lower and "lept" not in lower:
            continue
        path = Path(path_text)
        if path.is_file():
            found.append({"path": str(path), "bytes": path.stat().st_size})
    return found


def _paddle_footprint() -> dict[str, Any]:
    packages: list[dict[str, Any]] = []
    for name in ("paddle", "paddleocr", "paddlex"):
        directory = _package_dir(name)
        if directory is None:
            packages.append({"name": name, "bytes": None, "path": None})
            continue
        packages.append({"name": name, "bytes": _dir_bytes(directory), "path": str(directory)})
    model = Path.home() / ".paddlex" / "official_models"
    model_bytes = _dir_bytes(model)
    parts = [item["bytes"] for item in packages if isinstance(item["bytes"], int)]
    if isinstance(model_bytes, int):
        parts.append(model_bytes)
    footprint = sum(parts) if parts else None
    if all(item["bytes"] is None for item in packages) and model_bytes is None:
        footprint = None
    return {
        "footprint_bytes": footprint,
        "package_bytes": sum(item["bytes"] for item in packages if isinstance(item["bytes"], int)) or None,
        "model_bytes": model_bytes,
        "packages": packages,
        "model_path": str(model) if model.exists() else None,
        "note": (
            "paddle, paddleocr, and paddlex package directories plus the PaddleX model cache. "
            "Shared libraries such as numpy are not included."
        ),
    }


def _attach_size(payload: dict[str, Any]) -> None:
    size = payload.get("size") or {}
    engines = payload.get("engines") or {}
    engines.get("electrical", {})["size"] = size.get("electrical")
    engines.get("tesseract", {})["size"] = size.get("tesseract")
    engines.get("tesseract_four_angles", {})["size"] = size.get("tesseract")
    engines.get("paddle", {})["size"] = size.get("paddle")


def _attach_memory(payload: dict[str, Any]) -> None:
    """Say why resident memory is not a number.

    Tesseract's recognizer is a separate process. The electrical engine and
    Paddle run inside Python. One counter would miss Tesseract or mix heaps,
    so it is left null instead of printed as a comparison.
    """

    note = (
        "Not measured. Tesseract runs as its own process, while the electrical "
        "engine and Paddle run inside Python. A resident-set reading would not "
        "cover all three the same way."
    )
    blank = {
        "added_rss_bytes": None,
        "peak_rss_bytes": None,
        "baseline_rss_bytes": None,
        "note": note,
    }
    for result in (payload.get("engines") or {}).values():
        result["runtime_memory"] = dict(blank)



def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Benchmark electrical OCR against Tesseract and Paddle.")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the JSON here. Omit to print it.",
    )
    args = parser.parse_args(argv)
    payload = run_benchmark()
    text = json.dumps(payload, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    _print_summary(payload)
    if args.out is not None:
        print(f"wrote {args.out}")
    return 0


def _print_summary(payload: dict[str, Any]) -> None:
    role = payload["machine_role"]
    print(
        f"host {payload['hostname']}  role {role}  "
        f"local_target {payload['counts_as_local_target']}"
    )
    if role == "other":
        print("This run is not MichellesPC or DESKTOP-3S1HOH0. Local result slots stay empty.")
    for name, result in payload["engines"].items():
        if not result.get("available"):
            size = result.get("size") or {}
            print(f"{name}: unavailable — {result.get('reason')}")
            print(f"  footprint_bytes {size.get('footprint_bytes')}")
            continue
        size = result.get("size") or {}
        memory = result.get("runtime_memory") or {}
        print(
            f"{name}: held_out {result.get('held_out_sample_recall')}  "
            f"in_bank {result.get('in_bank_sample_recall')}  "
            f"{result.get('seconds')} s  "
            f"footprint_bytes {size.get('footprint_bytes')}  "
            f"added_rss_bytes {memory.get('added_rss_bytes')}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
