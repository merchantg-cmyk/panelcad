"""PEP 440 version comparison, including pre-release tags.

Ordering follows PEP 440: ``1.0.0.dev1 < 1.0.0a1 < 1.0.0b1 < 1.0.0rc1 < 1.0.0
< 1.0.0.post1``, and ``1.0`` equals ``1.0.0``. A trailing local version
(``1.0.0+shop``) sorts after the same version without one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PRE_TAGS = {
    "a": "a",
    "alpha": "a",
    "b": "b",
    "beta": "b",
    "c": "rc",
    "rc": "rc",
    "preview": "rc",
}
_PRE_RE = re.compile(r"(?i)(?:[._-])?(a|alpha|b|beta|rc|c|preview)(\d*)$")
_POST_RE = re.compile(r"(?i)(?:[._-])?post(\d+)$")
_DEV_RE = re.compile(r"(?i)(?:[._-])?dev(\d+)$")
_IMPLICIT_POST_RE = re.compile(r"-(\d+)$")
_RELEASE_RE = re.compile(r"\d+(?:\.\d+)*")


class _NegInf:
    def __repr__(self) -> str:
        return "-inf"

    def __lt__(self, other: object) -> bool:
        return not isinstance(other, _NegInf)

    def __le__(self, other: object) -> bool:
        return isinstance(other, _NegInf) or self < other

    def __gt__(self, other: object) -> bool:
        return False

    def __ge__(self, other: object) -> bool:
        return isinstance(other, _NegInf)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _NegInf)


class _PosInf:
    def __repr__(self) -> str:
        return "inf"

    def __lt__(self, other: object) -> bool:
        return False

    def __le__(self, other: object) -> bool:
        return isinstance(other, _PosInf)

    def __gt__(self, other: object) -> bool:
        return not isinstance(other, _PosInf)

    def __ge__(self, other: object) -> bool:
        return True

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _PosInf)


_NEG = _NegInf()
_POS = _PosInf()


@dataclass(frozen=True)
class Version:
    epoch: int
    release: tuple[int, ...]
    pre: tuple[str, int] | None
    post: int | None
    dev: int | None
    local: tuple[tuple[int, int | str], ...] | None
    text: str

    def key(self) -> tuple:
        release = self.release
        while len(release) > 1 and release[-1] == 0:
            release = release[:-1]
        if self.pre is None and self.post is None and self.dev is not None:
            pre_key: object = _NEG
        elif self.pre is None:
            pre_key = _POS
        else:
            pre_key = self.pre
        post_key: object = _NEG if self.post is None else self.post
        dev_key: object = _POS if self.dev is None else self.dev
        local_key: object = _NEG if self.local is None else self.local
        return (self.epoch, release, pre_key, post_key, dev_key, local_key)

    def __lt__(self, other: Version) -> bool:
        return self.key() < other.key()

    def __le__(self, other: Version) -> bool:
        return self.key() <= other.key()

    def __gt__(self, other: Version) -> bool:
        return self.key() > other.key()

    def __ge__(self, other: Version) -> bool:
        return self.key() >= other.key()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self.key() == other.key()


def _local_parts(local: str) -> tuple[tuple[int, int | str], ...]:
    parts: list[tuple[int, int | str]] = []
    for piece in local.split("."):
        if not piece or not re.fullmatch(r"[0-9A-Za-z]+", piece):
            raise ValueError(f"invalid local version {local!r}")
        if piece.isdigit():
            parts.append((0, int(piece)))
        else:
            parts.append((1, piece.lower()))
    return tuple(parts)


def parse_version(text: str) -> Version:
    """Parse a version string. Raises ValueError when it is not a version."""

    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty version")
    body = raw
    if body[0] in "vV" and len(body) > 1 and body[1].isdigit():
        body = body[1:]
    epoch = 0
    if "!" in body:
        epoch_text, body = body.split("!", 1)
        if not epoch_text.isdigit():
            raise ValueError(f"not a version: {text}")
        epoch = int(epoch_text)
    local = None
    if "+" in body:
        body, local_text = body.split("+", 1)
        local = _local_parts(local_text)
    dev = None
    dev_match = _DEV_RE.search(body)
    if dev_match and re.search(r"(?i)dev\d+$", body):
        dev = int(dev_match.group(1))
        body = body[: dev_match.start()].rstrip("._-")
    post = None
    post_match = _POST_RE.search(body)
    if post_match and re.search(r"(?i)post\d+$", body):
        post = int(post_match.group(1))
        body = body[: post_match.start()].rstrip("._-")
    else:
        implicit = _IMPLICIT_POST_RE.search(body)
        if implicit:
            post = int(implicit.group(1))
            body = body[: implicit.start()]
    pre = None
    pre_match = _PRE_RE.search(body)
    if pre_match and re.search(r"(?i)(?:a|alpha|b|beta|rc|c|preview)\d*$", body):
        tag = _PRE_TAGS[pre_match.group(1).lower()]
        pre = (tag, int(pre_match.group(2) or "0"))
        body = body[: pre_match.start()].rstrip("._-")
    if not _RELEASE_RE.fullmatch(body):
        raise ValueError(f"not a version: {text}")
    release = tuple(int(part) for part in body.split("."))
    return Version(epoch=epoch, release=release, pre=pre, post=post, dev=dev, local=local, text=raw)


def compare_versions(left: str, right: str) -> int:
    """Return -1, 0, or 1 when ``left`` is older, equal, or newer than ``right``."""

    a = parse_version(left)
    b = parse_version(right)
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def is_newer(candidate: str, current: str) -> bool:
    """True when ``candidate`` is a strictly newer version than ``current``."""

    return compare_versions(candidate, current) > 0
