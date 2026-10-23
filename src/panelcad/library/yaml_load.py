"""Library YAML reader. Uses the libyaml C loader when PyYAML has it.

The parts library is read on every start. The pure-Python ``SafeLoader``
spent about 2 s on the shop PC parsing it; ``CSafeLoader`` builds the same
plain data (dicts, lists, str, int, float, bool, None, dates) in a fraction
of that. Both are safe loaders, so no tags beyond the YAML core schema.
"""

from __future__ import annotations

from typing import Any

import yaml

try:
    _Loader: Any = yaml.CSafeLoader
except AttributeError:  # PyYAML built without libyaml
    _Loader = yaml.SafeLoader


def safe_load(text: str) -> Any:
    """Parse ``text`` like ``yaml.safe_load``.

    If libyaml rejects a user file that the pure-Python loader accepts, the
    pure-Python result is used, so a speed-up never turns into an open error.
    """

    if _Loader is yaml.SafeLoader:
        return yaml.safe_load(text)
    try:
        return yaml.load(text, Loader=_Loader)
    except yaml.YAMLError:
        return yaml.safe_load(text)


def loader_name() -> str:
    return _Loader.__name__
