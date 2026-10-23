"""Startup must not pay for the AI stack or the pure-Python YAML parser."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from panelcad.library import yaml_load

ROOT = Path(__file__).resolve().parents[1]


def _modules_after(code: str) -> set[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", code + "\nimport sys\nprint('\\n'.join(sys.modules))"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=120,
    ).stdout
    return set(out.split())


def test_troubleshoot_explain_does_not_load_drawing_agent() -> None:
    mods = _modules_after(
        "from panelcad.ai.troubleshoot_explain import explain_reason\nexplain_reason()"
    )
    assert "panelcad.ai.drawing" not in mods
    assert "pydantic_ai" not in mods


def test_ai_config_does_not_load_drawing_agent() -> None:
    mods = _modules_after("import panelcad.ai.config")
    assert "panelcad.ai.drawing" not in mods


def test_package_names_still_resolve() -> None:
    pytest.importorskip("pydantic_ai")
    from panelcad import ai
    from panelcad.ai import drawing

    assert ai.drawing_agent is drawing.drawing_agent
    assert ai.Drawing is drawing.Drawing
    assert ai.open_drawing is drawing.open_drawing
    assert "drawing_agent" in dir(ai)


def test_unknown_name_still_raises() -> None:
    from panelcad import ai

    with pytest.raises(AttributeError):
        ai.not_a_name  # noqa: B018


def test_main_window_import_does_not_load_drawing_agent() -> None:
    pytest.importorskip("PySide6")
    mods = _modules_after(
        "import os\nos.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')\n"
        "import panelcad.gui.main_window, panelcad.gui.troubleshoot_dock"
    )
    assert "pydantic_ai" not in mods


@pytest.mark.parametrize(
    "path",
    sorted((ROOT / "libraries").rglob("*.yaml")) + sorted((ROOT / "libraries").rglob("*.yml")),
    ids=lambda p: p.name,
)
def test_library_yaml_matches_pure_python_loader(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert yaml_load.safe_load(text) == yaml.safe_load(text)


def test_loader_is_safe() -> None:
    assert yaml_load.loader_name() in {"CSafeLoader", "SafeLoader"}
    with pytest.raises(yaml.YAMLError):
        yaml_load.safe_load("!!python/object/apply:os.system ['echo hi']")


def test_c_loader_error_falls_back_to_pure_python(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(yaml.SafeLoader):
        def get_single_data(self):  # type: ignore[override]
            raise yaml.YAMLError("libyaml said no")

    monkeypatch.setattr(yaml_load, "_Loader", Broken)
    assert yaml_load.safe_load("a: 1\n") == {"a": 1}
