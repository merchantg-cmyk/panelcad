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
"""

from __future__ import annotations

import json
import platform
import re
import shutil
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
            {"name": sample.name, "category": sample.category, "truths": list(sample.truths), "note": sample.note}
            for sample in samples
        ],
        "engines": engines,
        "how_to_read": (
            "sample_recall is the fraction of samples whose every truth token was emitted. "
            "token_recall is the fraction of truth tokens emitted. "
            "A machine_role of other is not a MichellesPC or DESKTOP-3S1HOH0 result."
        ),
    }
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
            "sample_recall": result.get("sample_recall"),
            "token_recall": result.get("token_recall"),
            "seconds": result.get("seconds"),
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


def _run_paddle(samples: list[OcrSample]) -> dict[str, Any]:
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
        return _score("paddle", samples, read, available=True, reason="")
    except Exception as exc:  # noqa: BLE001
        return _score("paddle", samples, None, available=False, reason=f"{type(exc).__name__}: {exc}")


def _paddle_reader(cls: Any) -> Any:
    attempts = (
        {"use_angle_cls": True, "lang": "en", "show_log": False, "use_gpu": False},
        {"use_angle_cls": True, "lang": "en"},
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
    if hasattr(reader, "ocr"):
        try:
            result = reader.ocr(image, cls=True)
        except TypeError:
            result = reader.ocr(image)
        return _texts_from_paddle_v2(result)
    if hasattr(reader, "predict"):
        result = reader.predict(image)
        return _texts_from_paddle_v3(result)
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
        rec = getattr(page, "rec_texts", None)
        if rec is None and isinstance(page, dict):
            rec = page.get("rec_texts")
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
        got = {_norm(item) for item in emitted}
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
    if rows and all(row["error"] for row in rows) and not any(row["emitted"] for row in rows):
        return {
            "name": name,
            "available": False,
            "reason": rows[0]["error"],
            "sample_recall": None,
            "token_recall": None,
            "seconds": round(elapsed, 4),
            "samples": rows,
        }
    return {
        "name": name,
        "available": True,
        "reason": "",
        "sample_recall": round(sample_hit / len(samples), 4) if samples else None,
        "token_recall": round(token_hit / token_total, 4) if token_total else None,
        "sample_hits": sample_hit,
        "sample_count": len(samples),
        "token_hits": token_hit,
        "token_count": token_total,
        "seconds": round(elapsed, 4),
        "samples": rows,
    }


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(text).upper())


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
            print(f"{name}: unavailable — {result.get('reason')}")
            continue
        print(
            f"{name}: sample_recall {result['sample_recall']}  "
            f"token_recall {result['token_recall']}  "
            f"{result['seconds']} s"
        )


if __name__ == "__main__":
    raise SystemExit(main())
