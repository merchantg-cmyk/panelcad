"""Which OCR backend the importer and reconstruct paths call."""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

# Empty means "read PANELCAD_OCR_BACKEND, otherwise tesseract".
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
    return "tesseract"


def use_backend(name: str | None) -> Token[str]:
    """Pin the backend until ``reset_backend``. None clears the pin."""

    normalized = normalize_backend(name)
    return _CHOICE.set(normalized or "")


def reset_backend(token: Token[str]) -> None:
    _CHOICE.reset(token)


def tesseract_required() -> bool:
    """False when the electrical engine is selected. It does not need the binary."""

    return active_backend() != "electrical"


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
