"""Which OCR backend the importer and reconstruct paths call."""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

# Empty means "read PANELCAD_OCR_BACKEND, otherwise electrical".
_CHOICE: ContextVar[str] = ContextVar("panelcad_ocr_backend", default="")

_ALIASES = {
    "tess": "tesseract",
    "panelcad": "electrical",
    "shop": "electrical",
}
_NAMES = {"tesseract", "electrical"}


def normalize_backend(name: str | None) -> str | None:
    """``tesseract``, ``electrical``, or None when the caller did not choose.

    None leaves the environment variable in charge. An unknown name is an
    error so a typo does not silently change a shop import.
    """

    if name is None:
        return None
    text = str(name).strip().lower()
    if not text:
        return None
    text = _ALIASES.get(text, text)
    if text not in _NAMES:
        raise ValueError(f"OCR backend must be 'tesseract' or 'electrical'. Got {name!r}.")
    return text


def active_backend() -> str:
    """The backend for this call. The context choice wins over the environment."""

    chosen = _CHOICE.get().strip().lower()
    if not chosen:
        chosen = os.environ.get("PANELCAD_OCR_BACKEND", "").strip().lower()
    chosen = _ALIASES.get(chosen, chosen)
    if chosen in _NAMES:
        return chosen
    return "electrical"


def use_backend(name: str | None) -> Token[str]:
    """Pin the backend until ``reset_backend``. None clears the pin."""

    normalized = normalize_backend(name)
    return _CHOICE.set(normalized or "")


def reset_backend(token: Token[str]) -> None:
    _CHOICE.reset(token)


def choice_is_explicit() -> bool:
    """True when this call or the environment named a backend."""

    if _CHOICE.get().strip():
        return True
    return bool(os.environ.get("PANELCAD_OCR_BACKEND", "").strip())


def page_engine_ready() -> bool:
    """True when the shipped weights and ONNX Runtime can load."""

    from panelcad.ocr.ppocr import available
    from panelcad.ocr.weights import weights_ready

    return weights_ready() and available()


def tesseract_required() -> bool:
    """True only for an explicit tesseract choice, or a failed electrical load.

    The default is electrical. Tesseract is the fallback when that engine
    cannot load and a tesseract binary is already on the machine. A missing
    binary does not make the scan ask for one.
    """

    if active_backend() == "tesseract":
        return True
    if choice_is_explicit():
        return False
    if page_engine_ready():
        return False
    from panelcad.importer.tesseract_runtime import find_tesseract

    return bool(find_tesseract())


@contextmanager
def ocr_scope(name: str | None) -> Iterator[str]:
    """Use ``name`` for the block. None keeps the current environment choice."""

    if normalize_backend(name) is None:
        yield active_backend()
        return
    token = use_backend(name)
    try:
        yield active_backend()
    finally:
        reset_backend(token)
